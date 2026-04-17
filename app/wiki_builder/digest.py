"""Deterministic builder for the wiki digest the mini sees per segment.

Per SPEC token guardrails:
- index.md (full, capped at 2K tokens, truncate-with-marker if larger)
- open-questions Active section (full, capped at 1K tokens)
- pages whose title or filename appears in the segment text — top 8, Summary
  only, capped at 3K tokens combined
- pages named in previous summary card's `active_characters` — top 5,
  Summary only, capped at 1K tokens

No LLM calls; pure file I/O + name matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .segmenter import _default_count_tokens

INDEX_TOKEN_BUDGET = 2000
OPEN_QUESTIONS_TOKEN_BUDGET = 1000
NAME_MATCHED_TOKEN_BUDGET = 3000
PRIOR_ACTIVE_TOKEN_BUDGET = 1000

NAME_MATCHED_PAGE_LIMIT = 8
PRIOR_ACTIVE_PAGE_LIMIT = 5

PAGE_DIRS = ("characters", "concepts", "places", "factions", "events")
PAGE_FILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.md$")
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
SUMMARY_RE = re.compile(r"^##\s+Summary\s*$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)
ACTIVE_SECTION_RE = re.compile(r"^##\s+Active\s*$\n(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


@dataclass
class WikiPage:
  path: str
  title: str
  summary: str
  full_text: str

  @property
  def category(self) -> str:
    return self.path.split("/", 1)[0]


@dataclass
class DigestResult:
  text: str
  pages_included: List[str] = field(default_factory=list)
  token_count: int = 0


def _read(path: Path) -> str:
  if not path.exists():
    return ""
  return path.read_text(encoding="utf-8")


def load_pages(wiki_dir: Path) -> Dict[str, WikiPage]:
  out: Dict[str, WikiPage] = {}
  for d in PAGE_DIRS:
    base = wiki_dir / d
    if not base.is_dir():
      continue
    for file in sorted(base.iterdir()):
      if not file.is_file() or not PAGE_FILE_RE.match(file.name):
        continue
      content = file.read_text(encoding="utf-8")
      title_match = TITLE_RE.search(content)
      title = title_match.group(1).strip() if title_match else file.stem.replace("-", " ").title()
      summary_match = SUMMARY_RE.search(content)
      summary = (summary_match.group(1).strip() if summary_match else "").strip()
      rel_path = f"{d}/{file.name}"
      out[rel_path] = WikiPage(path=rel_path, title=title, summary=summary, full_text=content)
  return out


def load_open_questions_active(wiki_dir: Path) -> str:
  raw = _read(wiki_dir / "open-questions.md")
  if not raw:
    return ""
  m = ACTIVE_SECTION_RE.search(raw)
  if not m:
    return ""
  return m.group(1).strip()


def load_open_questions_active_ids(wiki_dir: Path) -> Set[str]:
  body = load_open_questions_active(wiki_dir)
  if not body:
    return set()
  return set(re.findall(r"\bq-\d{4}\b", body))


def load_all_question_ids(wiki_dir: Path) -> Set[str]:
  raw = _read(wiki_dir / "open-questions.md")
  if not raw:
    return set()
  return set(re.findall(r"\bq-\d{4}\b", raw))


def _truncate_to_tokens(text: str, budget: int) -> Tuple[str, int]:
  total = _default_count_tokens(text)
  if total <= budget:
    return text, total
  # Binary-search a char cutoff that fits the budget.
  lo, hi = 0, len(text)
  best = ""
  best_tokens = 0
  while lo < hi:
    mid = (lo + hi + 1) // 2
    candidate = text[:mid]
    t = _default_count_tokens(candidate)
    if t <= budget - 20:  # leave room for truncation marker
      best = candidate
      best_tokens = t
      lo = mid
    else:
      hi = mid - 1
  marker = "\n\n[... truncated for digest budget ...]"
  return best.rstrip() + marker, best_tokens + _default_count_tokens(marker)


_NON_WORD = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
  return _NON_WORD.sub(" ", text.lower()).strip()


def _name_keys(page: WikiPage) -> List[str]:
  """Return distinct case-insensitive name tokens that can be searched in
  segment text. We use the page title (split by " ") plus the slug (split by
  "-"). Keys shorter than 3 chars are dropped (too noisy)."""
  raw_keys: Set[str] = set()
  for k in page.title.split():
    if len(k) >= 3:
      raw_keys.add(k.lower())
  slug = page.path.rsplit("/", 1)[1].removesuffix(".md")
  for k in slug.split("-"):
    if len(k) >= 3:
      raw_keys.add(k.lower())
  return sorted(raw_keys, key=len, reverse=True)


def _name_occurrences(page: WikiPage, normalized_segment: str) -> int:
  count = 0
  for key in _name_keys(page):
    # word-boundary match in normalized segment
    pattern = r"\b" + re.escape(key) + r"\b"
    count += len(re.findall(pattern, normalized_segment))
  return count


def _format_summary_block(page: WikiPage) -> str:
  return f"### {page.title}\n- path: {page.path}\n- summary: {page.summary or '(no summary yet)'}\n"


def _pack_pages(pages: List[WikiPage], budget_tokens: int) -> Tuple[List[WikiPage], str, int]:
  used: List[WikiPage] = []
  blocks: List[str] = []
  used_tokens = 0
  for p in pages:
    block = _format_summary_block(p)
    cost = _default_count_tokens(block)
    if used_tokens + cost > budget_tokens:
      break
    used.append(p)
    blocks.append(block)
    used_tokens += cost
  return used, "\n".join(blocks).strip(), used_tokens


def build_digest(
  wiki_dir: Path,
  segment_text: str,
  *,
  prior_active_characters: Optional[Iterable[str]] = None,
) -> DigestResult:
  pages = load_pages(wiki_dir)
  index_text = _read(wiki_dir / "index.md").strip()
  active_questions_text = load_open_questions_active(wiki_dir)

  index_block, index_tokens = _truncate_to_tokens(index_text, INDEX_TOKEN_BUDGET) if index_text else ("(empty index)", 0)
  questions_block, questions_tokens = _truncate_to_tokens(
    active_questions_text, OPEN_QUESTIONS_TOKEN_BUDGET
  ) if active_questions_text else ("(no active open questions)", 0)

  # Name-matched pages in the segment text.
  normalized_segment = _normalize(segment_text)

  scored: List[Tuple[int, WikiPage]] = []
  for page in pages.values():
    occ = _name_occurrences(page, normalized_segment)
    if occ > 0:
      scored.append((occ, page))
  scored.sort(key=lambda x: (-x[0], x[1].path))
  top_matches = [p for _, p in scored[:NAME_MATCHED_PAGE_LIMIT]]
  matched_used, matched_block, matched_tokens = _pack_pages(top_matches, NAME_MATCHED_TOKEN_BUDGET)
  matched_paths = {p.path for p in matched_used}

  # Pages named in prior summary card's active_characters (skip duplicates).
  prior_pages: List[WikiPage] = []
  if prior_active_characters:
    for path in prior_active_characters:
      if path in matched_paths:
        continue
      page = pages.get(path)
      if page:
        prior_pages.append(page)
        if len(prior_pages) >= PRIOR_ACTIVE_PAGE_LIMIT:
          break
  _prior_used, prior_block, prior_tokens = _pack_pages(prior_pages, PRIOR_ACTIVE_TOKEN_BUDGET)

  parts: List[str] = []
  parts.append("## Index")
  parts.append(index_block)
  parts.append("")
  parts.append("## Open questions (Active)")
  parts.append(questions_block)
  parts.append("")
  parts.append("## Relevant pages (matched by name in segment)")
  parts.append(matched_block or "(no name matches found)")
  if prior_block:
    parts.append("")
    parts.append("## Carried over from previous segment")
    parts.append(prior_block)

  text = "\n".join(parts).strip()
  total_tokens = index_tokens + questions_tokens + matched_tokens + prior_tokens
  return DigestResult(
    text=text,
    pages_included=[p.path for p in matched_used] + [p.path for p in prior_pages],
    token_count=total_tokens,
  )
