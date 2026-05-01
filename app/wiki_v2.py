"""Segment-aware wiki loading for Q&A.

The wiki-builder v4 pipeline produces a git repo of markdown pages per wiki,
with a `seg-NNNN` tag for every applied segment. This module answers two
questions for the Q&A endpoint:

- Which `seg-NNNN` tag is the spoiler-safe snapshot for the reader at
  (book, chapter_index, char_offset)?
- What does the wiki look like at that tag?

Design choice (v1): at a given safe tag, we concatenate *all* content pages
into one string and hand it to the agent. Wikis built by the segment pipeline
are modest in size (dozens of pages, ~5-15k tokens total), so retrieval over
them isn't worth building yet. If wikis grow, add a retrieval layer later —
the calling contract in main.py doesn't need to change.

The legacy wiki pipeline (one-shot bundle → snapshots.json) lives in
`app/wiki.py` and stays in use for books where `books.selected_wiki_id` is
NULL. This module is only invoked when a book has an explicit selection.
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger("wiki_v2")


def pick_safe_seg_tag(
  conn: sqlite3.Connection,
  wiki_id: int,
  book_id: int,
  chapter_index: Optional[int],
  char_offset: Optional[int],
) -> Optional[str]:
  """Return the seg-NNNN tag for the highest applied segment whose coverage
  the reader has already finished.

  Policy:
    - Any segment from a book with a *lower* `series_index` than the
      reader's current book is always safe (the reader has finished those
      books in this wiki).
    - Within the reader's current book: a segment in a chapter strictly
      before the reader's current chapter is safe. A segment in the
      current chapter is safe iff its `end_char <= char_offset`.

  If chapter_index is None we fall back to "no safe segments yet". If
  char_offset is None we conservatively treat the reader as being at the
  start of their current chapter (only earlier-chapter / earlier-book
  segments count as safe).
  """
  if chapter_index is None:
    return None

  # Find the reader's series position in this wiki. If the book isn't
  # linked to the wiki at all, there's nothing safe to show.
  series_row = conn.execute(
    "SELECT series_index FROM wiki_books WHERE wiki_id = ? AND book_id = ?",
    (wiki_id, book_id),
  ).fetchone()
  if not series_row:
    return None
  current_series_index = int(series_row["series_index"])

  best_tag: Optional[str] = None
  best_order: int = -1

  # Prior-book segments (any book with smaller series_index): always safe.
  if current_series_index > 0:
    prior_books = conn.execute(
      """
      SELECT ws.snapshot_tag, ws.story_order
      FROM wiki_segments ws
      JOIN wiki_books wb ON wb.wiki_id = ws.wiki_id AND wb.book_id = ws.book_id
      WHERE ws.wiki_id = ? AND ws.status = 'applied'
        AND ws.snapshot_tag IS NOT NULL
        AND wb.series_index < ?
      ORDER BY ws.story_order DESC
      LIMIT 1
      """,
      (wiki_id, current_series_index),
    ).fetchone()
    if prior_books:
      best_tag = prior_books["snapshot_tag"]
      best_order = int(prior_books["story_order"])

  # Same-book, earlier-chapter segments: always safe.
  earlier = conn.execute(
    """
    SELECT snapshot_tag, story_order
    FROM wiki_segments
    WHERE wiki_id = ? AND book_id = ? AND status = 'applied'
      AND snapshot_tag IS NOT NULL
      AND chapter_index < ?
    ORDER BY story_order DESC
    LIMIT 1
    """,
    (wiki_id, book_id, chapter_index),
  ).fetchone()
  if earlier and int(earlier["story_order"]) > best_order:
    best_tag = earlier["snapshot_tag"]
    best_order = int(earlier["story_order"])

  # Same-book, same-chapter segments: safe iff end_char <= char_offset.
  if char_offset is not None:
    same_chapter = conn.execute(
      """
      SELECT snapshot_tag, story_order
      FROM wiki_segments
      WHERE wiki_id = ? AND book_id = ? AND status = 'applied'
        AND snapshot_tag IS NOT NULL
        AND chapter_index = ? AND end_char <= ?
      ORDER BY story_order DESC
      LIMIT 1
      """,
      (wiki_id, book_id, chapter_index, char_offset),
    ).fetchone()
    if same_chapter and int(same_chapter["story_order"]) > best_order:
      best_tag = same_chapter["snapshot_tag"]
      best_order = int(same_chapter["story_order"])

  return best_tag


def _git(repo_path: Path, *args: str) -> str:
  result = subprocess.run(
    ["git", "-C", str(repo_path), *args],
    check=True,
    capture_output=True,
    text=True,
  )
  return result.stdout


def load_wiki_at_tag(repo_path: Path, tag: str) -> dict:
  """Return the wiki at `tag` as a concatenated markdown dump.

  Only `.md` files under `wiki/` are included. Each page is prefaced with a
  `## File: <relative-path>` header so the agent can cite pages by path.

  Returns:
    {"content": <str>, "pages": [<relative-path>, ...], "tag": <tag>}
  """
  try:
    raw = _git(repo_path, "ls-tree", "-r", "--name-only", tag, "--", "wiki")
  except subprocess.CalledProcessError as exc:
    log.warning("git ls-tree failed for %s @ %s: %s", repo_path, tag, exc.stderr)
    return {"content": "", "pages": [], "tag": tag}

  paths = [
    line.strip()
    for line in raw.splitlines()
    if line.strip().endswith(".md")
  ]
  # Deterministic, index-first ordering so index.md is the first thing the
  # agent sees.
  def _sort_key(p: str):
    return (
      0 if p.endswith("wiki/index.md") else
      1 if p.endswith("wiki/open-questions.md") else
      2,
      p,
    )
  paths.sort(key=_sort_key)

  chunks: list[str] = []
  included: list[str] = []
  for path in paths:
    try:
      body = _git(repo_path, "show", f"{tag}:{path}")
    except subprocess.CalledProcessError as exc:
      log.warning("git show failed for %s:%s: %s", tag, path, exc.stderr)
      continue
    # Strip the leading "wiki/" so cited paths match what lives on disk
    # (e.g. "characters/darrow.md" not "wiki/characters/darrow.md").
    display_path = path[len("wiki/"):] if path.startswith("wiki/") else path
    chunks.append(f"## File: {display_path}\n\n{body.strip()}\n")
    included.append(display_path)

  return {
    "content": "\n\n".join(chunks),
    "pages": included,
    "tag": tag,
  }


def _wiki_row(conn: sqlite3.Connection, wiki_id: int) -> Optional[sqlite3.Row]:
  return conn.execute(
    "SELECT id, slug, name, repo_path FROM wikis WHERE id = ?",
    (wiki_id,),
  ).fetchone()


def _seg_progress(
  conn: sqlite3.Connection, wiki_id: int, book_id: int, tag: str,
) -> str:
  """Short human label like 'seg 42 of 156' for UI display."""
  cur = conn.execute(
    """
    SELECT story_order
    FROM wiki_segments
    WHERE wiki_id = ? AND book_id = ? AND snapshot_tag = ?
    LIMIT 1
    """,
    (wiki_id, book_id, tag),
  ).fetchone()
  total = conn.execute(
    """
    SELECT COUNT(*) AS n
    FROM wiki_segments
    WHERE wiki_id = ? AND book_id = ? AND status = 'applied'
    """,
    (wiki_id, book_id),
  ).fetchone()
  if not cur or not total:
    return tag
  return f"segment {cur['story_order']} of {total['n']} applied"


def load_selected_wiki_context(
  conn: sqlite3.Connection,
  book_id: int,
  chapter_index: Optional[int],
  char_offset: Optional[int],
) -> Optional[dict]:
  """Top-level entry for the Q&A endpoint.

  Returns None if:
    - the book has no selected_wiki_id, OR
    - the selected wiki has no applied segments at/before the reader's
      position (reader is earlier than any wiki content).

  Otherwise returns a dict compatible with the shape main.py already handles
  for legacy wiki_context:

    {
      "context": <full markdown dump of the wiki at the safe tag>,
      "tag": <seg-NNNN>,
      "story_label": <human label>,
      "pages": [<relative-path>, ...],
      "wiki_slug": <slug>,
      "wiki_name": <name>,
      "search_terms": [],   # v1: unused by the v2 pipeline
      "sources": [],        # v1: unused by the v2 pipeline
    }
  """
  book_row = conn.execute(
    "SELECT selected_wiki_id FROM books WHERE id = ?", (book_id,),
  ).fetchone()
  if not book_row or book_row["selected_wiki_id"] is None:
    return None

  wiki_id = int(book_row["selected_wiki_id"])
  wiki = _wiki_row(conn, wiki_id)
  if not wiki:
    return None

  tag = pick_safe_seg_tag(conn, wiki_id, book_id, chapter_index, char_offset)
  if not tag:
    return None

  loaded = load_wiki_at_tag(Path(wiki["repo_path"]), tag)
  if not loaded["content"]:
    return None

  return {
    "context": loaded["content"],
    "tag": tag,
    "story_label": _seg_progress(conn, wiki_id, book_id, tag),
    "pages": loaded["pages"],
    "wiki_slug": wiki["slug"],
    "wiki_name": wiki["name"],
    "search_terms": [],
    "sources": [],
  }


def list_candidate_wikis(conn: sqlite3.Connection, book_id: int) -> list[dict]:
  """Wikis linked to this book via wiki_books, for the UI dropdown."""
  rows = conn.execute(
    """
    SELECT w.id, w.slug, w.name, wb.series_index,
           (SELECT COUNT(*) FROM wiki_segments
              WHERE wiki_id = w.id AND book_id = ? AND status = 'applied') AS applied_segments
    FROM wikis w
    JOIN wiki_books wb ON wb.wiki_id = w.id
    WHERE wb.book_id = ? AND w.status = 'active'
    ORDER BY w.name ASC
    """,
    (book_id, book_id),
  ).fetchall()
  return [
    {
      "id": r["id"],
      "slug": r["slug"],
      "name": r["name"],
      "series_index": r["series_index"],
      "applied_segments": r["applied_segments"],
    }
    for r in rows
  ]
