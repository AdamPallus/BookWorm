"""Message templates the harness sends to the Bookworm OpenClaw agent.

The Bookworm agent itself is task-agnostic — its own AGENTS.md / TOOLS.md
inside the OpenClaw install just establish identity and working style. The
task-specific brief (how to build a book wiki, what a good page looks like)
is injected by this harness on the first message of each new agent session,
so the instructions sit in the conversation as a recent authoritative user
message instead of drifting into background system-prompt territory.

Per-turn messages come in three shapes:

- Session opener: full wiki-builder instructions + the reference example
  page, followed by the segment. Used on the first segment of a fresh
  session, and on the first segment after a context-compaction rotation.
- Ordinary segment turn: slug + segment text + short reminder checklist.
- Rejection turn: sent after deterministic checks fail on a segment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

_THIS_DIR = Path(__file__).resolve().parent
_INSTRUCTIONS_PATH = _THIS_DIR / "wiki_builder_instructions.md"
_EXAMPLE_PATH = _THIS_DIR / "bookworm-wiki-creator.example.md"


def _load(path: Path) -> str:
  return path.read_text(encoding="utf-8")


# Loaded lazily so import-time failures in tests don't cascade. Callers go
# through the helper below.
_cache: dict[str, str] = {}


def _get(name: str, path: Path) -> str:
  if name not in _cache:
    _cache[name] = _load(path)
  return _cache[name]


def _opener_block() -> str:
  instructions = _get("instructions", _INSTRUCTIONS_PATH)
  example = _get("example", _EXAMPLE_PATH)
  return (
    "This is the first message in a new agent session. Read this whole brief\n"
    "before you touch any files — it is the authoritative description of the\n"
    "job. Subsequent turns in this session will be short operational messages\n"
    "(the next segment, or a rejection list); they assume you have read this.\n"
    "\n"
    "===== WIKI-BUILDER TASK BRIEF =====\n"
    f"{instructions}\n"
    "===== END TASK BRIEF =====\n"
    "\n"
    "===== EXAMPLE REFERENCE PAGE =====\n"
    f"{example}\n"
    "===== END EXAMPLE =====\n"
    "\n"
  )


def build_segment_message(
  *,
  wiki_slug: str,
  wiki_dir: str,
  snapshot_tag: str,
  chapter_label: str,
  segment_text: str,
  prelude_text: Optional[str] = None,
  compaction_context: Optional[str] = None,
  is_session_opener: bool = False,
  new_book_label: Optional[str] = None,
) -> str:
  opener_block = _opener_block() if is_session_opener else ""

  compaction_block = ""
  if new_book_label and is_session_opener:
    # First segment of a new book in a multi-book / series wiki. The wiki
    # already contains everything known from prior books; the agent's job
    # now is to extend it with this new book's events.
    compaction_block = (
      f"This is the first segment of {new_book_label}. The wiki working\n"
      "tree already contains pages built from earlier books in the series.\n"
      "Those pages are authoritative for everything known through the end\n"
      "of the prior book — treat them as your starting state. Read them as\n"
      "you normally would (Glob / Grep / Read) before deciding what to add\n"
      "or change.\n"
      "\n"
      "Integrate the new segment below as if continuing reading: characters\n"
      "from earlier books may reappear (extend their pages with new\n"
      "developments), and new characters / places / concepts introduced in\n"
      "this book get fresh pages. Don't rewrite the prior books' material;\n"
      "extend it.\n\n"
    )
  elif compaction_context:
    # On a rotated session we always combine the opener block with a
    # compaction-context block: the instructions above establish ground
    # rules, and this block reminds the agent that the wiki already
    # contains work from prior turns.
    compaction_block = (
      "The previous session was compacted after growing past its context\n"
      "budget. The existing wiki files on disk are the continuing source of\n"
      "truth; read them as you normally would. Below is the book text from\n"
      "the last few segments, already integrated into the wiki, included\n"
      "only so you have recent narrative context in mind. Do NOT re-integrate\n"
      "it as if it were new — only integrate the NEW SEGMENT text further down.\n"
      "\n"
      "===== RECENT BOOK TEXT (already integrated — for your reference) =====\n"
      f"{compaction_context}\n"
      "===== END RECENT BOOK TEXT =====\n\n"
    )
  elif is_session_opener:
    # Fresh session, no prior work — no compaction block, but a short
    # orienting note is helpful since the wiki directory may already exist
    # with content from earlier runs, or may be empty.
    compaction_block = (
      "If the wiki working tree already contains pages from prior runs,\n"
      "treat them as the continuing source of truth and integrate into them.\n"
      "If the tree is empty, you are starting the wiki from scratch with\n"
      "this segment.\n\n"
    )

  prelude_block = ""
  if prelude_text:
    prelude_block = (
      "Note: the segment below also includes earlier short text that was\n"
      "below the threshold for its own turn (dedication, copyright, very\n"
      "short chapter, etc.). Treat the prelude as part of the same turn —\n"
      "absorb anything useful into the wiki, ignore boilerplate.\n"
      "\n"
      "===== PRELUDE TEXT =====\n"
      f"{prelude_text}\n"
      "===== END PRELUDE =====\n\n"
    )

  return (
    f"{opener_block}"
    f"Wiki slug: {wiki_slug}\n"
    f"Wiki working tree: {wiki_dir}\n"
    f"Segment: {snapshot_tag} ({chapter_label})\n\n"
    f"{compaction_block}"
    f"{prelude_block}"
    f"===== SEGMENT TEXT =====\n"
    f"{segment_text}\n"
    f"===== END SEGMENT =====\n\n"
    f"Update the wiki to reflect everything known through this segment. "
    f"Investigate existing pages first, then write/rewrite/rename/delete as needed.\n\n"
    f"Before you hand back:\n"
    f"- On every page you edited, re-read it end-to-end. The first mention "
    f"of any subject with its own page MUST be a markdown link. This is the "
    f"single most common thing to miss on long pages, especially in later "
    f"sections that were added after the page got long. A page with rich "
    f"prose but no links to known subjects is wrong even if every fact is "
    f"right.\n"
    f"- Don't wrap a name in link syntax unless the target file actually "
    f"exists. If a subject deserves a link but has no page yet, either "
    f"create the page or leave the name as plain text — never point a link "
    f"at an unrelated file.\n"
    f"- In open-questions.md, keep exactly one \"Recently resolved\" "
    f"section. If prior turns left duplicate sections, merge them into "
    f"one rolling window and prune entries that have been fully absorbed "
    f"into the body pages.\n\n"
    f"Reply briefly when done."
  )


def build_rejection_message(issues: Iterable) -> str:
  lines = [
    "Your edits were rejected by the deterministic checks. The working tree",
    "has been reverted to the last accepted state. Try again, addressing:",
    "",
  ]
  for issue in issues:
    kind = getattr(issue, "kind", None)
    where = getattr(issue, "where", None)
    detail = getattr(issue, "detail", None)
    if kind is None and isinstance(issue, dict):
      kind = issue.get("kind", "?")
      where = issue.get("where", "?")
      detail = issue.get("detail", "")
    lines.append(f"- [{kind}] {where}: {detail}")
  lines.append("")
  lines.append("Re-read whatever files you need; the tree is back to its prior state.")
  return "\n".join(lines)
