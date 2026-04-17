"""Apply a validated diff to the wiki working tree, git-commit, and tag.

The applier is purely deterministic: by the time it runs, the diff has passed
the deterministic checks AND the LLM supervisor. It does file I/O and runs
git commands; no logic decisions.

Page format (kept simple — readable without templating):

    # Title

    ## Summary
    {summary text}

    ## Detail
    {detail text}

    ## Sources
    - seg-NNNN: source_note text

When updating, we preserve any existing Sources entries and append the new
one.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

PAGE_DIRS = ("characters", "concepts", "places", "factions", "events")


@dataclass
class ApplyResult:
  pages_created: List[str]
  pages_updated: List[str]
  questions_added: List[str]
  questions_resolved: List[str]
  snapshot_tag: str
  commit_sha: str


SECTION_HEADINGS = ("## Summary", "## Detail", "## Sources")
SUMMARY_RE = re.compile(r"^##\s+Summary\s*$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
DETAIL_RE = re.compile(r"^##\s+Detail\s*$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
SOURCES_RE = re.compile(r"^##\s+Sources\s*$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
ACTIVE_RE = re.compile(r"^##\s+Active\s*$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
RESOLVED_RE = re.compile(r"^##\s+Resolved\s*$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
QUESTION_BLOCK_RE = re.compile(
  r"^###\s+(q-\d{4})\s+—\s+(.*?)\n((?:(?!^###\s+q-\d{4})[\s\S])*)",
  re.MULTILINE,
)


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
  return subprocess.run(
    ["git", *args],
    cwd=repo,
    check=check,
    capture_output=True,
    text=True,
  )


def _ensure_section(content: str, heading: str, body: str) -> str:
  """Replace `## heading` body if present, else append the section."""
  pattern = re.compile(rf"^##\s+{re.escape(heading.lstrip('# ').strip())}\s*$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
  block = f"{heading}\n{body.rstrip()}\n\n"
  if pattern.search(content):
    return pattern.sub(block, content, count=1)
  return content.rstrip() + "\n\n" + block


def _format_new_page(title: str, summary: str, detail: str, source_block: str) -> str:
  return (
    f"# {title}\n\n"
    f"## Summary\n{summary.strip()}\n\n"
    f"## Detail\n{detail.strip()}\n\n"
    f"## Sources\n{source_block.strip()}\n"
  )


def _quote_for_yaml(s: str) -> str:
  return s.replace("\\", "\\\\").replace('"', '\\"')


def _format_source_line(snapshot_tag: str, source_note: str) -> str:
  note = source_note.strip()
  if len(note) > 140:
    note = note[:137] + "..."
  return f"- {snapshot_tag}: \"{_quote_for_yaml(note)}\""


def _update_existing_page(
  content: str,
  *,
  summary: Optional[str],
  detail_append: Optional[str],
  detail_replace: Optional[str],
  source_line: str,
) -> str:
  if summary is not None:
    content = _ensure_section(content, "## Summary", summary)
  if detail_replace is not None:
    content = _ensure_section(content, "## Detail", detail_replace)
  elif detail_append:
    m = DETAIL_RE.search(content)
    if m:
      existing_detail = m.group(1).rstrip()
      new_body = existing_detail + "\n\n" + detail_append.strip()
      content = _ensure_section(content, "## Detail", new_body)
    else:
      content = _ensure_section(content, "## Detail", detail_append)

  m = SOURCES_RE.search(content)
  if m:
    existing = m.group(1).rstrip()
    new_sources = (existing + "\n" + source_line).strip()
    content = _ensure_section(content, "## Sources", new_sources)
  else:
    content = _ensure_section(content, "## Sources", source_line)
  return content


def _format_question_block(qid: str, title: str, body_lines: List[str]) -> str:
  lines = [f"### {qid} — {title.strip()}"]
  lines.extend(body_lines)
  return "\n".join(lines).rstrip() + "\n"


def _update_open_questions(
  raw: str,
  *,
  added: List[dict],
  resolved: List[dict],
  snapshot_tag: str,
) -> str:
  if not raw.strip():
    raw = "# Open Questions\n\n## Active\n\n## Resolved\n"

  active_match = ACTIVE_RE.search(raw)
  resolved_match = RESOLVED_RE.search(raw)

  active_body = active_match.group(1).strip() if active_match else ""
  resolved_body = resolved_match.group(1).strip() if resolved_match else ""

  active_blocks = list(QUESTION_BLOCK_RE.finditer("\n" + active_body)) if active_body else []
  active_map: dict = {}
  for m in active_blocks:
    qid = m.group(1)
    title = m.group(2).strip()
    body = m.group(3).strip("\n")
    active_map[qid] = {"title": title, "body": body}

  for q in added:
    qid = q["id"]
    title = q.get("title", "").strip() or qid
    text = (q.get("text") or "").strip()
    body_lines = [
      f"- raised in: {snapshot_tag}",
      f"- text: {text}",
    ]
    body = "\n".join(body_lines)
    active_map[qid] = {"title": title, "body": body}

  resolved_appendices: List[str] = []
  for q in resolved:
    qid = q["id"]
    resolution = (q.get("resolution") or "").strip()
    quote = (q.get("evidence_quote") or "").strip()

    title = qid
    raised_line = "- raised in: (unknown)"

    if qid in active_map:
      title = active_map[qid]["title"]
      old_body = active_map[qid]["body"]
      raised = re.search(r"^- raised in:\s*(.+)$", old_body, re.MULTILINE)
      if raised:
        raised_line = f"- raised in: {raised.group(1).strip()}"
      del active_map[qid]
    else:
      title = (q.get("title") or "").strip() or qid

    safe_quote = quote.replace('"', "'")
    res_lines = [
      raised_line,
      f"- resolved in: {snapshot_tag}",
      f'- resolution: "{resolution}"',
      f'- evidence_quote: "{safe_quote}"',
    ]
    resolved_appendices.append(_format_question_block(qid, title, res_lines))

  new_active = "\n".join(
    _format_question_block(qid, info["title"], info["body"].splitlines())
    for qid, info in sorted(active_map.items())
  ).strip()
  if not new_active:
    new_active = ""

  if resolved_appendices:
    if resolved_body:
      new_resolved = (resolved_body + "\n\n" + "\n".join(resolved_appendices)).strip()
    else:
      new_resolved = "\n".join(resolved_appendices).strip()
  else:
    new_resolved = resolved_body

  out = "# Open Questions\n\n## Active\n"
  if new_active:
    out += new_active.rstrip() + "\n"
  out += "\n## Resolved\n"
  if new_resolved:
    out += new_resolved.rstrip() + "\n"
  return out


def _append_log(raw: str, snapshot_tag: str, log_entry: str) -> str:
  if not raw.strip():
    raw = "# Wiki Log\n\n"
  entry = f"## {snapshot_tag}\n{log_entry.strip()}\n\n"
  return raw.rstrip() + "\n\n" + entry


def _update_index(raw: str, new_pages: Iterable[dict]) -> str:
  """Add bullets for newly created pages under category sections."""
  if not raw.strip():
    raw = "# Wiki Index\n\n"

  by_category: dict = {}
  for p in new_pages:
    cat = p["path"].split("/", 1)[0]
    by_category.setdefault(cat, []).append(p)

  for cat, pages in by_category.items():
    heading = f"## {cat.title()}"
    section_re = re.compile(rf"^{re.escape(heading)}\s*$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
    bullets = []
    for p in pages:
      title = p["title"]
      summ = (p.get("summary") or "").strip().split("\n")[0]
      bullets.append(f"- [{title}]({p['path']}) — {summ}")
    bullet_block = "\n".join(bullets)
    m = section_re.search(raw)
    if m:
      existing = m.group(1).rstrip()
      new_body = (existing + "\n" + bullet_block).strip() + "\n\n"
      raw = section_re.sub(f"{heading}\n{new_body}", raw, count=1)
    else:
      raw = raw.rstrip() + f"\n\n{heading}\n{bullet_block}\n"
  return raw


def apply_diff(
  *,
  wiki_dir: Path,
  diff: dict,
  snapshot_tag: str,
  commit_message: str,
) -> ApplyResult:
  wiki_repo = wiki_dir.parent  # wiki_dir is .../{slug}/wiki, repo is .../{slug}
  if not (wiki_repo / ".git").exists():
    raise RuntimeError(f"not a git repo: {wiki_repo}")

  pages_created_paths: List[str] = []
  pages_updated_paths: List[str] = []

  for entry in diff.get("pages_created", []):
    rel = entry["path"]
    file = wiki_dir / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    if file.exists():
      # mini hallucinated a "create" but the file exists — convert to update.
      content = file.read_text(encoding="utf-8")
      source_line = _format_source_line(snapshot_tag, entry.get("source_note", ""))
      content = _update_existing_page(
        content,
        summary=entry.get("summary"),
        detail_append=entry.get("detail"),
        detail_replace=None,
        source_line=source_line,
      )
      file.write_text(content, encoding="utf-8")
      pages_updated_paths.append(rel)
    else:
      source_line = _format_source_line(snapshot_tag, entry.get("source_note", ""))
      file.write_text(
        _format_new_page(
          entry["title"],
          entry.get("summary", ""),
          entry.get("detail", ""),
          source_line,
        ),
        encoding="utf-8",
      )
      pages_created_paths.append(rel)

  for entry in diff.get("pages_updated", []):
    rel = entry["path"]
    file = wiki_dir / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    source_line = _format_source_line(snapshot_tag, entry.get("source_note", ""))
    if file.exists():
      content = file.read_text(encoding="utf-8")
    else:
      # mini said "update" on a missing page — start a fresh page using
      # whatever it gave us.
      title = rel.rsplit("/", 1)[1].removesuffix(".md").replace("-", " ").title()
      content = _format_new_page(
        title,
        entry.get("summary") or "",
        entry.get("detail_replace") or entry.get("detail_append") or "",
        source_line,
      )
      file.write_text(content, encoding="utf-8")
      pages_updated_paths.append(rel)
      continue

    content = _update_existing_page(
      content,
      summary=entry.get("summary"),
      detail_append=entry.get("detail_append"),
      detail_replace=entry.get("detail_replace"),
      source_line=source_line,
    )
    file.write_text(content, encoding="utf-8")
    pages_updated_paths.append(rel)

  # open-questions.md
  oq_file = wiki_dir / "open-questions.md"
  oq_raw = oq_file.read_text(encoding="utf-8") if oq_file.exists() else ""
  oq_new = _update_open_questions(
    oq_raw,
    added=diff.get("questions_added", []),
    resolved=diff.get("questions_resolved", []),
    snapshot_tag=snapshot_tag,
  )
  oq_file.write_text(oq_new, encoding="utf-8")

  # log.md
  log_file = wiki_dir / "log.md"
  log_raw = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
  log_new = _append_log(log_raw, snapshot_tag, diff.get("log_entry", "(no log entry)"))
  log_file.write_text(log_new, encoding="utf-8")

  # index.md
  if diff.get("pages_created"):
    idx_file = wiki_dir / "index.md"
    idx_raw = idx_file.read_text(encoding="utf-8") if idx_file.exists() else ""
    idx_new = _update_index(idx_raw, diff["pages_created"])
    idx_file.write_text(idx_new, encoding="utf-8")

  _run_git(wiki_repo, "add", "-A")
  _run_git(wiki_repo, "commit", "-m", commit_message)
  _run_git(wiki_repo, "tag", snapshot_tag)
  sha = _run_git(wiki_repo, "rev-parse", "HEAD").stdout.strip()

  return ApplyResult(
    pages_created=pages_created_paths,
    pages_updated=pages_updated_paths,
    questions_added=[q["id"] for q in diff.get("questions_added", [])],
    questions_resolved=[q["id"] for q in diff.get("questions_resolved", [])],
    snapshot_tag=snapshot_tag,
    commit_sha=sha,
  )


def revert_working_tree(wiki_dir: Path) -> None:
  """Roll back uncommitted changes after a quarantine."""
  wiki_repo = wiki_dir.parent
  _run_git(wiki_repo, "reset", "--hard", "HEAD", check=False)
  _run_git(wiki_repo, "clean", "-fd", check=False)
