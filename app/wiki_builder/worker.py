"""The main wiki-builder loop.

For each pending segment in story_order:
  1. Reconstruct segment text from `chunks` table.
  2. Build digest from the wiki working tree + previous summary card.
  3. Open a fresh OpenClaw session for the mini.
  4. Up to 3 attempts: send mini prompt, parse JSON, run deterministic checks,
     run supervisor. Retry within the same mini session with issue list.
  5. On supervisor pass: apply diff, commit, tag, store summary card,
     update wiki_segments row.
  6. On 3-strikes failure: revert working tree, mark segment quarantined.
  7. Sleep --segment-interval-seconds between segments.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import openclaw_client
from .applier import ApplyResult, apply_diff, revert_working_tree
from .checks import run_all_checks
from .digest import build_digest, load_all_question_ids, load_open_questions_active_ids
from .prompts import (
  MINI_SYSTEM_PROMPT,
  SUPERVISOR_SYSTEM_PROMPT,
  build_mini_prompt,
  build_supervisor_prompt,
)

log = logging.getLogger("wiki_builder.worker")

MAX_ATTEMPTS_PER_SEGMENT = 3


@dataclass
class WorkerConfig:
  segment_interval_seconds: float = 90.0
  max_segments: Optional[int] = None
  mini_model: str = openclaw_client.DEFAULT_MINI_MODEL
  supervisor_model: str = openclaw_client.DEFAULT_SUPERVISOR_MODEL
  read_timeout_seconds: float = 600.0


@dataclass
class SegmentRow:
  id: int
  wiki_id: int
  book_id: int
  chapter_index: int
  segment_in_chapter: int
  story_order: int
  start_char: int
  end_char: int
  attempts: int


def _next_pending_segment(conn: sqlite3.Connection, wiki_id: int) -> Optional[SegmentRow]:
  row = conn.execute(
    """
    SELECT id, wiki_id, book_id, chapter_index, segment_in_chapter, story_order,
           start_char, end_char, attempts
    FROM wiki_segments
    WHERE wiki_id = ? AND status = 'pending'
    ORDER BY story_order ASC
    LIMIT 1
    """,
    (wiki_id,),
  ).fetchone()
  if row is None:
    return None
  return SegmentRow(*row)


def _previous_summary_card(conn: sqlite3.Connection, wiki_id: int, story_order: int) -> Optional[dict]:
  row = conn.execute(
    """
    SELECT s.summary_json
    FROM wiki_segment_summaries s
    JOIN wiki_segments w ON w.id = s.segment_id
    WHERE w.wiki_id = ? AND w.story_order < ?
    ORDER BY w.story_order DESC
    LIMIT 1
    """,
    (wiki_id, story_order),
  ).fetchone()
  if not row:
    return None
  try:
    return json.loads(row[0])
  except json.JSONDecodeError:
    return None


def reconstruct_chapter_text(conn: sqlite3.Connection, book_id: int, chapter_index: int) -> str:
  rows = conn.execute(
    """
    SELECT text FROM chunks
    WHERE book_id = ? AND chapter_index = ?
    ORDER BY position_index ASC, id ASC
    """,
    (book_id, chapter_index),
  ).fetchall()
  return "".join(row[0] for row in rows)


def _segment_text(conn: sqlite3.Connection, seg: SegmentRow) -> str:
  chapter_text = reconstruct_chapter_text(conn, seg.book_id, seg.chapter_index)
  return chapter_text[seg.start_char : seg.end_char]


def _wiki_dir_for(conn: sqlite3.Connection, wiki_id: int) -> Path:
  row = conn.execute("SELECT repo_path FROM wikis WHERE id = ?", (wiki_id,)).fetchone()
  if not row:
    raise RuntimeError(f"wiki {wiki_id} not found")
  return Path(row[0]) / "wiki"


def _format_tag(story_order: int) -> str:
  return f"seg-{story_order:04d}"


def _commit_message(seg: SegmentRow, log_entry: str) -> str:
  first = log_entry.strip().split("\n")[0][:120]
  return f"{_format_tag(seg.story_order)}: {first}"


def _mark_segment(
  conn: sqlite3.Connection,
  seg: SegmentRow,
  *,
  status: str,
  attempts: int,
  last_error: Optional[str],
  snapshot_tag: Optional[str],
) -> None:
  conn.execute(
    """
    UPDATE wiki_segments
    SET status = ?, attempts = ?, last_error = ?,
        applied_at = CASE WHEN ? = 'applied' THEN ? ELSE applied_at END,
        snapshot_tag = COALESCE(?, snapshot_tag)
    WHERE id = ?
    """,
    (status, attempts, last_error, status, datetime.utcnow().isoformat(), snapshot_tag, seg.id),
  )
  conn.commit()


def _store_summary_card(conn: sqlite3.Connection, segment_id: int, summary_card: dict) -> None:
  conn.execute(
    """
    INSERT INTO wiki_segment_summaries (segment_id, summary_json)
    VALUES (?, ?)
    ON CONFLICT(segment_id) DO UPDATE SET summary_json = excluded.summary_json
    """,
    (segment_id, json.dumps(summary_card)),
  )
  conn.commit()


def _format_issues(issues) -> List[dict]:
  out = []
  for i in issues:
    if isinstance(i, dict):
      out.append(i)
    elif hasattr(i, "as_dict"):
      out.append(i.as_dict())
    else:
      out.append({"kind": "other", "where": "?", "explanation": str(i)})
  return out


def process_segment(
  conn: sqlite3.Connection,
  wiki_id: int,
  seg: SegmentRow,
  config: WorkerConfig,
) -> Optional[ApplyResult]:
  wiki_dir = _wiki_dir_for(conn, wiki_id)
  segment_text = _segment_text(conn, seg)
  if not segment_text.strip():
    log.warning("seg %s is empty; marking applied with no-op", _format_tag(seg.story_order))
    _mark_segment(conn, seg, status="applied", attempts=seg.attempts, last_error=None, snapshot_tag=None)
    return None

  prior_card = _previous_summary_card(conn, wiki_id, seg.story_order)
  prior_active = prior_card.get("active_characters", []) if prior_card else None
  digest = build_digest(wiki_dir, segment_text, prior_active_characters=prior_active)
  log.info(
    "digest for %s: %d tokens, %d pages",
    _format_tag(seg.story_order),
    digest.token_count,
    len(digest.pages_included),
  )

  mini_session = openclaw_client.new_session_id(label=f"mini-seg-{seg.story_order:04d}")
  prior_issues: Optional[list] = None
  prior_diff_str: Optional[str] = None

  for attempt in range(1, MAX_ATTEMPTS_PER_SEGMENT + 1):
    log.info("seg %s attempt %d", _format_tag(seg.story_order), attempt)

    if attempt == 1:
      mini_input = MINI_SYSTEM_PROMPT + "\n\n" + build_mini_prompt(
        segment_text=segment_text,
        digest_text=digest.text,
        previous_summary_card=prior_card,
        prior_attempt_issues=None,
        prior_attempt_diff=None,
      )
    else:
      mini_input = build_mini_prompt(
        segment_text=segment_text,
        digest_text=digest.text,
        previous_summary_card=prior_card,
        prior_attempt_issues=prior_issues,
        prior_attempt_diff=prior_diff_str,
      )

    try:
      mini_response = openclaw_client.call_json(
        mini_input,
        model=config.mini_model,
        session_id=mini_session,
        read_timeout=config.read_timeout_seconds,
      )
    except openclaw_client.OpenClawError as e:
      log.error("openclaw mini call failed: %s", e)
      _mark_segment(
        conn, seg, status="pending",
        attempts=seg.attempts + attempt,
        last_error=f"openclaw mini error: {e}",
        snapshot_tag=None,
      )
      return None

    if mini_response.parsed is None:
      prior_issues = [{
        "kind": "schema",
        "where": "<top>",
        "explanation": f"output was not valid JSON: {mini_response.parse_error}",
      }]
      prior_diff_str = mini_response.raw_text
      continue

    diff = mini_response.parsed
    prior_diff_str = json.dumps(diff, indent=2)

    existing_qids = load_all_question_ids(wiki_dir)
    active_qids = load_open_questions_active_ids(wiki_dir)

    det_result = run_all_checks(
      diff,
      segment_text=segment_text,
      existing_question_ids=existing_qids,
      active_question_ids=active_qids,
    )
    if not det_result.ok:
      prior_issues = _format_issues(det_result.issues)
      log.info(
        "seg %s attempt %d: %d deterministic issues",
        _format_tag(seg.story_order), attempt, len(prior_issues),
      )
      continue

    sup_input = SUPERVISOR_SYSTEM_PROMPT + "\n\n" + build_supervisor_prompt(
      segment_text=segment_text,
      digest_text=digest.text,
      diff_json=prior_diff_str,
    )
    sup_session = openclaw_client.new_session_id(label=f"sup-seg-{seg.story_order:04d}-att-{attempt}")
    try:
      sup_response = openclaw_client.call_json(
        sup_input,
        model=config.supervisor_model,
        session_id=sup_session,
        read_timeout=config.read_timeout_seconds,
      )
    except openclaw_client.OpenClawError as e:
      log.error("openclaw supervisor call failed: %s", e)
      _mark_segment(
        conn, seg, status="pending",
        attempts=seg.attempts + attempt,
        last_error=f"openclaw supervisor error: {e}",
        snapshot_tag=None,
      )
      return None

    if sup_response.parsed is None:
      log.warning(
        "supervisor output not valid JSON (attempt %d); raw: %s",
        attempt,
        sup_response.raw_text[:400],
      )
      prior_issues = [{
        "kind": "supervisor-parse",
        "where": "<top>",
        "explanation": "supervisor returned non-JSON; treating as a generic 'be more careful' signal",
      }]
      continue

    sup = sup_response.parsed
    if sup.get("ok") is True:
      tag = _format_tag(seg.story_order)
      msg = _commit_message(seg, diff.get("log_entry", ""))
      result = apply_diff(wiki_dir=wiki_dir, diff=diff, snapshot_tag=tag, commit_message=msg)
      _store_summary_card(conn, seg.id, diff["summary_card"])
      _mark_segment(
        conn, seg,
        status="applied",
        attempts=seg.attempts + attempt,
        last_error=None,
        snapshot_tag=tag,
      )
      log.info(
        "seg %s applied: +%d pages, ~%d updated, %d Q+/%d Q-",
        tag,
        len(result.pages_created),
        len(result.pages_updated),
        len(result.questions_added),
        len(result.questions_resolved),
      )
      return result

    issues = sup.get("issues") or []
    if not issues:
      issues = [{"kind": "other", "where": "<top>", "explanation": "supervisor said not ok but gave no issues"}]
    prior_issues = _format_issues(issues)
    log.info(
      "seg %s attempt %d: supervisor rejected (%d issues)",
      _format_tag(seg.story_order), attempt, len(issues),
    )

  # Out of attempts.
  log.warning("seg %s quarantined after %d attempts", _format_tag(seg.story_order), MAX_ATTEMPTS_PER_SEGMENT)
  revert_working_tree(_wiki_dir_for(conn, wiki_id))
  _mark_segment(
    conn, seg,
    status="quarantined",
    attempts=seg.attempts + MAX_ATTEMPTS_PER_SEGMENT,
    last_error=json.dumps(prior_issues)[:2000] if prior_issues else "out of attempts",
    snapshot_tag=None,
  )
  return None


def run_worker(conn: sqlite3.Connection, wiki_id: int, config: WorkerConfig) -> int:
  """Process pending segments until none remain or max_segments reached.
  Returns count of segments processed (applied + quarantined)."""
  processed = 0
  while True:
    if config.max_segments is not None and processed >= config.max_segments:
      log.info("reached --max-segments=%s; stopping", config.max_segments)
      break
    seg = _next_pending_segment(conn, wiki_id)
    if seg is None:
      log.info("no pending segments; worker done")
      break
    process_segment(conn, wiki_id, seg, config)
    processed += 1
    next_seg = _next_pending_segment(conn, wiki_id)
    is_last = next_seg is None or (
      config.max_segments is not None and processed >= config.max_segments
    )
    if not is_last and config.segment_interval_seconds > 0:
      log.info("sleeping %.1fs before next segment", config.segment_interval_seconds)
      time.sleep(config.segment_interval_seconds)
  return processed
