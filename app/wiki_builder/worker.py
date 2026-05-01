"""The wiki-builder worker.

One OpenClaw session per run. The agent persists in that session across
segments. Per segment:
  1. Reconstruct segment text from the chunks table.
  2. Send a segment message to the agent (same session).
  3. Agent edits the wiki working tree via its Write/Edit/Glob/Grep/Read tools.
  4. Run deterministic checks on the working tree.
  5. If pass: stage, commit, tag seg-NNNN, mark applied, next segment.
  6. If fail: revert, send rejection message, retry (up to MAX_ATTEMPTS).
  7. After MAX_ATTEMPTS failures: revert and quarantine segment.

There is no supervisor stage. Deterministic checks are the only verification.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import openclaw_client
from .applier import StagedDiff, commit_and_tag, reject, stage_all
from .checks import check_working_tree
from .prompts import build_rejection_message, build_segment_message

log = logging.getLogger("wiki_builder.worker")

MAX_ATTEMPTS_PER_SEGMENT = 3

# Compaction thresholds. When the agent's session total_tokens (as reported
# on the terminal response.completed event) reaches the wiki's effective
# threshold, the worker rotates to a fresh OpenClaw session before the next
# segment and includes the last RECENT_SEGMENTS_FOR_COMPACTION segments of
# book text in the first message as labeled context.
#
# Default: 240k ≈ 60% of the 400k gpt-5.4-mini cap, leaving room for the
# next segment + the agent's wiki reads + tool-call chatter before the wall.
# Per-wiki overrides live on `wikis.compact_at_total_tokens`; use them when
# the OpenClaw config or model effective limit differs (e.g. premier models
# that read/write more aggressively per segment, or a tighter OpenClaw cap).
COMPACT_AT_TOTAL_TOKENS = 240_000
RECENT_SEGMENTS_FOR_COMPACTION = 3


def _compaction_threshold(conn: sqlite3.Connection, wiki_id: int) -> int:
  row = conn.execute(
    "SELECT compact_at_total_tokens FROM wikis WHERE id = ?",
    (wiki_id,),
  ).fetchone()
  if row is not None and row[0] is not None and int(row[0]) > 0:
    return int(row[0])
  return COMPACT_AT_TOTAL_TOKENS


@dataclass
class WorkerConfig:
  segment_interval_seconds: float = 90.0
  max_segments: Optional[int] = None
  mini_model: str = openclaw_client.DEFAULT_MINI_MODEL
  read_timeout_seconds: float = 600.0
  # If True, force a brand-new OpenClaw session even if one is already
  # persisted on the wiki row. Otherwise we resume the existing session so
  # the agent keeps its in-context memory of prior segments.
  reset_session: bool = False


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


def _chapter_label(conn: sqlite3.Connection, book_id: int, chapter_index: int) -> str:
  row = conn.execute(
    "SELECT title FROM chapters WHERE book_id = ? AND chapter_index = ?",
    (book_id, chapter_index),
  ).fetchone()
  title = row[0] if row else None
  if title:
    return f"chapter {chapter_index}: {title}"
  return f"chapter {chapter_index}"


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


def _absorbed_prelude(
  conn: sqlite3.Connection,
  wiki_id: int,
  next_story_order: int,
) -> tuple[str, list[int]]:
  """Return (prelude_text, ids) for all 'absorbed' segments preceding the
  next pending segment. The prelude rolls forward across worker runs — any
  absorbed segment with status='absorbed' and story_order < next_story_order
  gets folded in. Their status is flipped to 'applied' alongside the next
  successful commit so they don't get folded twice.
  """
  rows = conn.execute(
    """
    SELECT id, book_id, chapter_index, start_char, end_char
    FROM wiki_segments
    WHERE wiki_id = ? AND status = 'absorbed' AND story_order < ?
    ORDER BY story_order ASC
    """,
    (wiki_id, next_story_order),
  ).fetchall()
  if not rows:
    return ("", [])
  parts: list[str] = []
  ids: list[int] = []
  for row in rows:
    seg_id, book_id, chapter_index, start_char, end_char = row
    chapter_text = reconstruct_chapter_text(conn, book_id, chapter_index)
    parts.append(chapter_text[start_char:end_char])
    ids.append(seg_id)
  return ("\n\n".join(parts), ids)


def _wiki_row(conn: sqlite3.Connection, wiki_id: int) -> tuple:
  row = conn.execute(
    "SELECT slug, repo_path FROM wikis WHERE id = ?",
    (wiki_id,),
  ).fetchone()
  if not row:
    raise RuntimeError(f"wiki {wiki_id} not found")
  return row


def _resolve_session_id(
  conn: sqlite3.Connection,
  wiki_id: int,
  slug: str,
  *,
  reset: bool,
) -> tuple[str, bool]:
  """Return (session_id, was_resumed). Persists newly-minted session ids on
  the wiki row so subsequent worker runs continue the same conversation."""
  if not reset:
    row = conn.execute(
      "SELECT openclaw_session_id FROM wikis WHERE id = ?",
      (wiki_id,),
    ).fetchone()
    existing = row[0] if row else None
    if existing:
      return existing, True
  fresh = openclaw_client.new_session_id(label=f"wiki-{slug}")
  conn.execute(
    "UPDATE wikis SET openclaw_session_id = ? WHERE id = ?",
    (fresh, wiki_id),
  )
  conn.commit()
  return fresh, False


def _format_tag(story_order: int) -> str:
  return f"seg-{story_order:04d}"


def _format_summary(diff: StagedDiff) -> str:
  parts = []
  if diff.files_added:
    parts.append(f"+{diff.files_added}")
  if diff.files_modified:
    parts.append(f"~{diff.files_modified}")
  if diff.files_deleted:
    parts.append(f"-{diff.files_deleted}")
  if diff.files_renamed:
    parts.append(f"\u2192{diff.files_renamed}")
  return " ".join(parts) or "no-op"


def _mark_absorbed_applied(
  conn: sqlite3.Connection,
  absorbed_ids: list[int],
  *,
  snapshot_tag: Optional[str],
) -> None:
  if not absorbed_ids:
    return
  applied_at = datetime.utcnow().isoformat()
  for seg_id in absorbed_ids:
    conn.execute(
      "UPDATE wiki_segments SET status = 'applied', applied_at = ?, snapshot_tag = COALESCE(?, snapshot_tag) WHERE id = ?",
      (applied_at, snapshot_tag, seg_id),
    )
  conn.commit()


def _mark_segment(
  conn: sqlite3.Connection,
  seg: SegmentRow,
  *,
  status: str,
  attempts: int,
  last_error: Optional[str],
  snapshot_tag: Optional[str],
  session_total_tokens: Optional[int] = None,
) -> None:
  conn.execute(
    """
    UPDATE wiki_segments
    SET status = ?, attempts = ?, last_error = ?,
        applied_at = CASE WHEN ? = 'applied' THEN ? ELSE applied_at END,
        snapshot_tag = COALESCE(?, snapshot_tag),
        session_total_tokens = COALESCE(?, session_total_tokens)
    WHERE id = ?
    """,
    (status, attempts, last_error, status, datetime.utcnow().isoformat(),
     snapshot_tag, session_total_tokens, seg.id),
  )
  conn.commit()


def _record_session_total_tokens(
  conn: sqlite3.Connection,
  wiki_id: int,
  total_tokens: Optional[int],
) -> None:
  if total_tokens is None:
    return
  conn.execute(
    "UPDATE wikis SET openclaw_session_total_tokens = ? WHERE id = ?",
    (total_tokens, wiki_id),
  )
  conn.commit()


def _recent_applied_segment_texts(
  conn: sqlite3.Connection,
  wiki_id: int,
  before_story_order: int,
  n: int,
) -> str:
  """Return up to `n` most recently-applied segments' raw book text,
  concatenated in chronological (story_order) order — used as context on
  the first message of a new session after compaction."""
  rows = conn.execute(
    """
    SELECT book_id, chapter_index, start_char, end_char, story_order
    FROM wiki_segments
    WHERE wiki_id = ? AND status = 'applied' AND story_order < ?
    ORDER BY story_order DESC
    LIMIT ?
    """,
    (wiki_id, before_story_order, n),
  ).fetchall()
  if not rows:
    return ""
  rows = list(reversed(rows))  # chronological
  parts: list[str] = []
  for row in rows:
    book_id, chapter_index, start_char, end_char, _order = row
    chapter_text = reconstruct_chapter_text(conn, book_id, chapter_index)
    parts.append(chapter_text[start_char:end_char])
  return "\n\n".join(parts)


def process_segment(
  conn: sqlite3.Connection,
  wiki_id: int,
  seg: SegmentRow,
  config: WorkerConfig,
  session_id: str,
  *,
  compaction_context: Optional[str] = None,
  is_session_opener: bool = False,
  new_book_label: Optional[str] = None,
) -> Optional[StagedDiff]:
  slug, repo_path = _wiki_row(conn, wiki_id)
  wiki_dir = Path(repo_path) / "wiki"
  segment_text = _segment_text(conn, seg)
  tag = _format_tag(seg.story_order)

  prelude_text, absorbed_ids = _absorbed_prelude(conn, wiki_id, seg.story_order)
  if absorbed_ids:
    log.info("seg %s folding in %d absorbed segment(s)", tag, len(absorbed_ids))

  if not segment_text.strip() and not prelude_text.strip():
    log.warning("seg %s is empty; marking applied as no-op", tag)
    _mark_segment(conn, seg, status="applied", attempts=seg.attempts, last_error=None, snapshot_tag=None)
    _mark_absorbed_applied(conn, absorbed_ids, snapshot_tag=None)
    return None

  chapter_label = _chapter_label(conn, seg.book_id, seg.chapter_index)
  message = build_segment_message(
    wiki_slug=slug,
    wiki_dir=str(wiki_dir),
    snapshot_tag=tag,
    chapter_label=chapter_label,
    segment_text=segment_text,
    prelude_text=prelude_text or None,
    compaction_context=compaction_context or None,
    is_session_opener=is_session_opener,
    new_book_label=new_book_label,
  )

  check_result = None
  last_usage: Optional[dict] = None
  for attempt in range(1, MAX_ATTEMPTS_PER_SEGMENT + 1):
    log.info("seg %s attempt %d", tag, attempt)

    try:
      _text, last_usage = openclaw_client.call(
        message,
        model=config.mini_model,
        session_id=session_id,
        read_timeout=config.read_timeout_seconds,
      )
    except openclaw_client.OpenClawError as e:
      log.error("openclaw call failed on seg %s: %s", tag, e)
      reject(wiki_dir)
      _mark_segment(
        conn, seg, status="pending",
        attempts=seg.attempts + attempt,
        last_error=f"openclaw error: {e}",
        snapshot_tag=None,
      )
      return None

    # Record the session's cumulative token count on every call so we can
    # rotate even if the segment eventually quarantines.
    if last_usage is not None:
      total = last_usage.get("total_tokens")
      if isinstance(total, int):
        _record_session_total_tokens(conn, wiki_id, total)
        log.info("seg %s session_total_tokens=%d", tag, total)

    check_result = check_working_tree(wiki_dir)
    if check_result.ok:
      diff = stage_all(wiki_dir)
      commit_and_tag(
        wiki_dir,
        snapshot_tag=tag,
        commit_message=f"{tag}: {_format_summary(diff)}",
      )
      applied_total = last_usage.get("total_tokens") if isinstance(last_usage, dict) else None
      _mark_segment(
        conn, seg,
        status="applied",
        attempts=seg.attempts + attempt,
        last_error=None,
        snapshot_tag=tag,
        session_total_tokens=applied_total if isinstance(applied_total, int) else None,
      )
      _mark_absorbed_applied(conn, absorbed_ids, snapshot_tag=tag)
      log.info(
        "seg %s applied: +%d ~%d -%d \u2192%d%s",
        tag, diff.files_added, diff.files_modified, diff.files_deleted, diff.files_renamed,
        f" (+{len(absorbed_ids)} absorbed)" if absorbed_ids else "",
      )
      return diff

    log.info(
      "seg %s attempt %d failed checks: %d issues",
      tag, attempt, len(check_result.issues),
    )
    for issue in check_result.issues[:5]:
      log.info("  - [%s] %s: %s", issue.kind, issue.where, issue.detail)
    reject(wiki_dir)
    message = build_rejection_message(check_result.issues)

  log.warning("seg %s quarantined after %d attempts", tag, MAX_ATTEMPTS_PER_SEGMENT)
  reject(wiki_dir)
  last_err = (
    json.dumps([{"kind": i.kind, "where": i.where, "detail": i.detail} for i in check_result.issues])[:2000]
    if check_result is not None
    else "out of attempts"
  )
  _mark_segment(
    conn, seg,
    status="quarantined",
    attempts=seg.attempts + MAX_ATTEMPTS_PER_SEGMENT,
    last_error=last_err,
    snapshot_tag=None,
  )
  return None


def _last_applied_book_id(conn: sqlite3.Connection, wiki_id: int) -> Optional[int]:
  row = conn.execute(
    """
    SELECT book_id FROM wiki_segments
    WHERE wiki_id = ? AND status = 'applied'
    ORDER BY story_order DESC
    LIMIT 1
    """,
    (wiki_id,),
  ).fetchone()
  return int(row[0]) if row else None


def _book_label(conn: sqlite3.Connection, wiki_id: int, book_id: int) -> str:
  """Human label for a book within a multi-book wiki, e.g.
  "'Golden Son' (book 2 of the series)". Used in the new-book session
  opener brief so the agent knows which volume this is."""
  row = conn.execute(
    """
    SELECT b.title, wb.series_index,
           (SELECT COUNT(*) FROM wiki_books WHERE wiki_id = ?) AS total
    FROM books b
    JOIN wiki_books wb ON wb.book_id = b.id
    WHERE wb.wiki_id = ? AND b.id = ?
    """,
    (wiki_id, wiki_id, book_id),
  ).fetchone()
  if not row:
    return f"book id {book_id}"
  title = row[0] or f"book id {book_id}"
  series_index = int(row[1])  # zero-based
  total = int(row[2] or 1)
  # Display as 1-based for humans.
  return f"{title!r} (book {series_index + 1} of {total} in this wiki)"


def _force_new_session(
  conn: sqlite3.Connection,
  wiki_id: int,
  slug: str,
) -> str:
  """Mint a fresh OpenClaw session for the wiki and clear the token counter.
  Used on book transitions in a multi-book wiki."""
  new_id = openclaw_client.new_session_id(label=f"wiki-{slug}")
  conn.execute(
    "UPDATE wikis SET openclaw_session_id = ?, openclaw_session_total_tokens = NULL WHERE id = ?",
    (new_id, wiki_id),
  )
  conn.commit()
  return new_id


def _maybe_rotate_session(
  conn: sqlite3.Connection,
  wiki_id: int,
  slug: str,
  current_session_id: str,
) -> tuple[str, bool]:
  """If the current session's last-observed total_tokens crossed the
  compaction threshold, mint a fresh session id, clear the token counter,
  and return (new_id, True). Otherwise return (current_session_id, False).

  Called before each segment so rotation happens on the new segment's
  first message — the recent-context prelude is part of that first message.
  """
  row = conn.execute(
    "SELECT openclaw_session_total_tokens FROM wikis WHERE id = ?",
    (wiki_id,),
  ).fetchone()
  last_total = row[0] if row else None
  threshold = _compaction_threshold(conn, wiki_id)
  if last_total is None or last_total < threshold:
    return current_session_id, False
  new_id = openclaw_client.new_session_id(label=f"wiki-{slug}")
  conn.execute(
    "UPDATE wikis SET openclaw_session_id = ?, openclaw_session_total_tokens = NULL WHERE id = ?",
    (new_id, wiki_id),
  )
  conn.commit()
  log.info(
    "session for wiki %s at %d tokens (>= %d); rotating to %s",
    slug, last_total, threshold, new_id,
  )
  return new_id, True


def run_worker(conn: sqlite3.Connection, wiki_id: int, config: WorkerConfig) -> int:
  slug, _ = _wiki_row(conn, wiki_id)
  session_id, resumed = _resolve_session_id(conn, wiki_id, slug, reset=config.reset_session)
  log.info(
    "starting worker for wiki %s, session %s (%s)",
    slug, session_id, "resumed" if resumed else "new",
  )
  # The next segment is a session-opener iff the session itself is brand new.
  # Set after every rotation, consumed (and cleared) by the next call to
  # process_segment.
  opener_pending = not resumed
  processed = 0
  while True:
    if config.max_segments is not None and processed >= config.max_segments:
      log.info("reached --max-segments=%s; stopping", config.max_segments)
      break
    seg = _next_pending_segment(conn, wiki_id)
    if seg is None:
      log.info("no pending segments; worker done")
      break

    # New book in a multi-book wiki: force a fresh session and a session
    # opener with a brief tailored to this case. Book transitions take
    # precedence over compaction-based rotation since both want a new
    # session anyway, and the new-book brief is more specific.
    new_book_label: Optional[str] = None
    last_book_id = _last_applied_book_id(conn, wiki_id)
    if last_book_id is not None and last_book_id != seg.book_id:
      session_id = _force_new_session(conn, wiki_id, slug)
      opener_pending = True
      new_book_label = _book_label(conn, wiki_id, seg.book_id)
      log.info(
        "book transition (prev book_id=%s, new book_id=%s); rotating session to %s",
        last_book_id, seg.book_id, session_id,
      )

    session_id, rotated = _maybe_rotate_session(conn, wiki_id, slug, session_id)
    compaction_context: Optional[str] = None
    if rotated:
      opener_pending = True
      compaction_context = _recent_applied_segment_texts(
        conn, wiki_id, seg.story_order, RECENT_SEGMENTS_FOR_COMPACTION,
      )
      log.info(
        "compaction: including ~%d chars of recent book text (last %d applied segments) in first message",
        len(compaction_context), RECENT_SEGMENTS_FOR_COMPACTION,
      )

    is_opener = opener_pending
    if is_opener:
      log.info(
        "seg %s is a session opener; injecting full task brief + example page%s",
        _format_tag(seg.story_order),
        " (new book in series)" if new_book_label else "",
      )
    process_segment(
      conn, wiki_id, seg, config, session_id,
      compaction_context=compaction_context,
      is_session_opener=is_opener,
      new_book_label=new_book_label,
    )
    # Only clear the opener flag if the segment actually moved off pending.
    # If the openclaw call itself errored on attempt 1, the message likely
    # never reached the agent — we need to inject the brief on the retry.
    post_seg = _next_pending_segment(conn, wiki_id)
    same_seg_still_pending = post_seg is not None and post_seg.id == seg.id
    if not same_seg_still_pending:
      opener_pending = False
    processed += 1
    next_seg = _next_pending_segment(conn, wiki_id)
    is_last = next_seg is None or (
      config.max_segments is not None and processed >= config.max_segments
    )
    if not is_last and config.segment_interval_seconds > 0:
      log.info("sleeping %.1fs before next segment", config.segment_interval_seconds)
      time.sleep(config.segment_interval_seconds)
  return processed
