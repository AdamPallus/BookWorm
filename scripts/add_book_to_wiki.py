"""Append a book to an existing wiki — the next book in a series.

Unlike `init_wiki`, this does NOT create a wiki repo or `wikis` row. It just:
- Verifies the wiki exists and the book is not already linked.
- Picks the next `series_index` for this wiki (max + 1).
- Picks the next `story_order` for this wiki (max + 1) — so the new book's
  segments are appended in story order after the existing book's segments.
- Segments the new book and inserts `wiki_segments` rows + the `wiki_books`
  link row.
- Marks short new segments 'absorbed' (only within the new book — does not
  re-evaluate already-applied segments from earlier books).

The on-disk wiki repo is left untouched; the agent will modify it as it
processes the new pending segments.

Usage:
  python -m scripts.add_book_to_wiki \
    --slug red-rising-v5 \
    --book-id 6 \
    [--target-tokens 7000] [--min-tokens 3000] [--max-tokens 12000] \
    [--absorb-below-tokens 1000] \
    [--skip-front-matter-chapters 0,1]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Set

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import db  # noqa: E402
from app.wiki_builder.segmenter import SegmenterConfig  # noqa: E402
from scripts.init_wiki import (  # noqa: E402
  mark_absorbed_segments,
  populate_segments,
)


def main() -> None:
  parser = argparse.ArgumentParser(
    description="Add a book to an existing Bookworm wiki (next in a series)."
  )
  parser.add_argument("--slug", required=True, help="Existing wiki slug to append to.")
  parser.add_argument("--book-id", type=int, required=True,
                      help="Book ID to append. Must not already be linked to this wiki.")
  parser.add_argument("--target-tokens", type=int, default=7000)
  parser.add_argument("--min-tokens", type=int, default=3000)
  parser.add_argument("--max-tokens", type=int, default=12000)
  parser.add_argument("--absorb-below-tokens", type=int, default=1000)
  parser.add_argument("--skip-front-matter-chapters", type=str, default="",
                      help="Comma-separated chapter_index values to skip in this book.")
  args = parser.parse_args()

  skip_chapters: Set[int] = set()
  if args.skip_front_matter_chapters.strip():
    for raw in args.skip_front_matter_chapters.split(","):
      raw = raw.strip()
      if raw:
        skip_chapters.add(int(raw))

  conn = db.connect()

  wiki = conn.execute(
    "SELECT id, name FROM wikis WHERE slug = ?",
    (args.slug,),
  ).fetchone()
  if not wiki:
    raise SystemExit(f"no wiki with slug {args.slug!r}")
  wiki_id = wiki["id"]

  book = conn.execute(
    "SELECT id, title FROM books WHERE id = ?", (args.book_id,),
  ).fetchone()
  if not book:
    raise SystemExit(f"no book with id {args.book_id}")

  already = conn.execute(
    "SELECT 1 FROM wiki_books WHERE wiki_id = ? AND book_id = ?",
    (wiki_id, args.book_id),
  ).fetchone()
  if already:
    raise SystemExit(
      f"book id {args.book_id} ({book['title']!r}) is already linked to wiki "
      f"{args.slug!r}. Nothing to do."
    )

  # Continue series_index and story_order past the existing rows so the
  # new book sits cleanly after the prior book(s).
  next_series_index = (conn.execute(
    "SELECT COALESCE(MAX(series_index), -1) + 1 FROM wiki_books WHERE wiki_id = ?",
    (wiki_id,),
  ).fetchone()[0])
  next_story_order = (conn.execute(
    "SELECT COALESCE(MAX(story_order), -1) + 1 FROM wiki_segments WHERE wiki_id = ?",
    (wiki_id,),
  ).fetchone()[0])

  print(
    f"[add-book] adding book id={args.book_id} ({book['title']!r}) to wiki "
    f"{args.slug!r} as series_index={next_series_index}, "
    f"starting story_order={next_story_order}",
    flush=True,
  )

  cfg = SegmenterConfig(
    target=args.target_tokens,
    minimum=args.min_tokens,
    maximum=args.max_tokens,
  )
  inserted = populate_segments(
    conn,
    wiki_id=wiki_id,
    book_ids=[args.book_id],
    segmenter_config=cfg,
    skip_chapters=skip_chapters,
    start_story_order=next_story_order,
    start_series_index=next_series_index,
  )
  print(f"[add-book] inserted {inserted} pending segments", flush=True)

  absorbed = mark_absorbed_segments(
    conn, wiki_id, args.absorb_below_tokens, book_id=args.book_id,
  )
  if absorbed:
    print(
      f"[add-book] marked {absorbed} segments 'absorbed' "
      f"(below {args.absorb_below_tokens} tokens)",
      flush=True,
    )


if __name__ == "__main__":
  main()
