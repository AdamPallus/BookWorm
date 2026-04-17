"""Initialize a wiki for a book (or series).

- Creates the `wikis` row.
- Creates the `wiki_books` row(s) — one per book in the series, ordered by
  `--book-id` order on the CLI.
- Creates `data/wikis/{slug}/` with empty wiki/ subdirs and `git init`.
- Segments each book's chapters and populates `wiki_segments` with status='pending'.
- For Phase 1 we only support a single book; multi-book is structurally
  supported but unused until Phase 4.

Usage:
  python -m scripts.init_wiki \
    --slug red-rising-v2 \
    --name "Red Rising" \
    --book-id 5 \
    [--target-tokens 7000] [--min-tokens 3000] [--max-tokens 12000] \
    [--skip-front-matter-chapters 0,1] \
    [--force-recreate]

The `--skip-front-matter-chapters` flag accepts a comma-separated list of
chapter_index values to omit from segmentation (copyright pages, dedications,
etc.).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import db  # noqa: E402
from app.wiki_builder.segmenter import SegmenterConfig, segment_chapter  # noqa: E402
from app.wiki_builder.worker import reconstruct_chapter_text  # noqa: E402

WIKI_ROOT = REPO_ROOT / "data" / "wikis"


def _git(repo: Path, *args: str) -> None:
  subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(wiki_repo: Path, name: str) -> None:
  if (wiki_repo / ".git").exists():
    return
  wiki_repo.mkdir(parents=True, exist_ok=True)
  wiki_dir = wiki_repo / "wiki"
  wiki_dir.mkdir(exist_ok=True)
  for sub in ("characters", "concepts", "places", "factions", "events"):
    (wiki_dir / sub).mkdir(exist_ok=True)
  (wiki_dir / "index.md").write_text(
    f"# {name} — Wiki Index\n\n"
    "Pages will be added as the wiki builder processes the book.\n",
    encoding="utf-8",
  )
  (wiki_dir / "open-questions.md").write_text(
    "# Open Questions\n\n## Active\n\n## Resolved\n",
    encoding="utf-8",
  )
  (wiki_dir / "log.md").write_text(
    "# Wiki Log\n\nAppend-only chronological record of segment commits.\n",
    encoding="utf-8",
  )
  _git(wiki_repo, "init")
  _git(wiki_repo, "config", "user.email", "bookworm@wiki-builder")
  _git(wiki_repo, "config", "user.name", "Bookworm Wiki Builder")
  _git(wiki_repo, "add", "-A")
  _git(wiki_repo, "commit", "-m", f"Initialize wiki for {name}")
  _git(wiki_repo, "tag", "init")


def _chapter_indices_for_book(conn, book_id: int) -> List[int]:
  rows = conn.execute(
    "SELECT DISTINCT chapter_index FROM chunks WHERE book_id = ? ORDER BY chapter_index",
    (book_id,),
  ).fetchall()
  return [r[0] for r in rows]


def _position_index_range(conn, book_id: int, chapter_index: int) -> tuple:
  row = conn.execute(
    """
    SELECT MIN(position_index), MAX(position_index)
    FROM chunks
    WHERE book_id = ? AND chapter_index = ?
    """,
    (book_id, chapter_index),
  ).fetchone()
  if row is None or row[0] is None:
    return (None, None)
  return (row[0], row[1])


def upsert_wiki(
  conn,
  *,
  slug: str,
  name: str,
  repo_path: Path,
  target: int,
  minimum: int,
  maximum: int,
  force_recreate: bool,
) -> int:
  existing = conn.execute("SELECT id FROM wikis WHERE slug = ?", (slug,)).fetchone()
  if existing and not force_recreate:
    raise SystemExit(
      f"wiki slug {slug!r} already exists (id={existing[0]}). Use --force-recreate to drop and rebuild."
    )
  if existing and force_recreate:
    wiki_id = existing[0]
    print(f"[init] dropping existing wiki rows for slug={slug!r} (id={wiki_id})", flush=True)
    conn.execute(
      "DELETE FROM wiki_segment_summaries WHERE segment_id IN (SELECT id FROM wiki_segments WHERE wiki_id = ?)",
      (wiki_id,),
    )
    conn.execute("DELETE FROM wiki_segments WHERE wiki_id = ?", (wiki_id,))
    conn.execute("DELETE FROM wiki_books WHERE wiki_id = ?", (wiki_id,))
    conn.execute("DELETE FROM wikis WHERE id = ?", (wiki_id,))
    conn.commit()

  cur = conn.execute(
    """
    INSERT INTO wikis (slug, name, repo_path, target_segment_tokens, min_segment_tokens, max_segment_tokens)
    VALUES (?, ?, ?, ?, ?, ?)
    """,
    (slug, name, str(repo_path), target, minimum, maximum),
  )
  conn.commit()
  return cur.lastrowid


def populate_segments(
  conn,
  *,
  wiki_id: int,
  book_ids: List[int],
  segmenter_config: SegmenterConfig,
  skip_chapters: Set[int],
) -> int:
  story_order = 0
  inserted = 0
  for series_index, book_id in enumerate(book_ids):
    conn.execute(
      "INSERT INTO wiki_books (wiki_id, book_id, series_index) VALUES (?, ?, ?)",
      (wiki_id, book_id, series_index),
    )
    chapters = _chapter_indices_for_book(conn, book_id)
    print(f"[init] book_id={book_id}: {len(chapters)} chapters", flush=True)
    for chapter_index in chapters:
      if chapter_index in skip_chapters:
        print(f"[init]   skipping front-matter chapter_index={chapter_index}", flush=True)
        continue
      text = reconstruct_chapter_text(conn, book_id, chapter_index)
      if not text.strip():
        continue
      segments = segment_chapter(text, chapter_index, segmenter_config)
      pos_min, pos_max = _position_index_range(conn, book_id, chapter_index)
      for seg in segments:
        # Approximate per-segment position_index range using proportional
        # mapping within the chapter. Good enough for Phase 2's spoiler
        # cutoff; can be tightened later if needed.
        chap_len = len(text) or 1
        if pos_min is None or pos_max is None:
          start_pos, end_pos = None, None
        else:
          span = pos_max - pos_min if pos_max > pos_min else 0
          start_pos = pos_min + int(round((seg.start_char / chap_len) * span)) if span else pos_min
          end_pos = pos_min + int(round((seg.end_char / chap_len) * span)) if span else pos_max

        conn.execute(
          """
          INSERT INTO wiki_segments
            (wiki_id, book_id, chapter_index, segment_in_chapter, story_order,
             start_char, end_char, start_position_index, end_position_index, token_count)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          """,
          (
            wiki_id, book_id, chapter_index, seg.segment_in_chapter, story_order,
            seg.start_char, seg.end_char, start_pos, end_pos, seg.token_count,
          ),
        )
        story_order += 1
        inserted += 1
  conn.commit()
  return inserted


def main() -> None:
  parser = argparse.ArgumentParser(description="Initialize a Bookworm wiki for a book or series.")
  parser.add_argument("--slug", required=True, help="URL-safe wiki slug (used as data/wikis/{slug}/)")
  parser.add_argument("--name", required=True, help="Human-readable wiki name")
  parser.add_argument("--book-id", type=int, action="append", required=True,
                      help="Book ID(s) to include. Repeat for multi-book series, in series order.")
  parser.add_argument("--target-tokens", type=int, default=7000)
  parser.add_argument("--min-tokens", type=int, default=3000)
  parser.add_argument("--max-tokens", type=int, default=12000)
  parser.add_argument("--skip-front-matter-chapters", type=str, default="",
                      help="Comma-separated list of chapter_index values to skip (e.g. '0,1').")
  parser.add_argument("--force-recreate", action="store_true",
                      help="Drop the existing wiki rows and re-init. Does NOT delete the on-disk repo.")
  args = parser.parse_args()

  skip_chapters: Set[int] = set()
  if args.skip_front_matter_chapters.strip():
    for raw in args.skip_front_matter_chapters.split(","):
      raw = raw.strip()
      if raw:
        skip_chapters.add(int(raw))

  repo_path = WIKI_ROOT / args.slug
  if repo_path.exists() and not args.force_recreate:
    raise SystemExit(f"{repo_path} already exists. Use --force-recreate or pick another slug.")
  if repo_path.exists() and args.force_recreate:
    print(f"[init] removing existing repo path {repo_path}", flush=True)
    shutil.rmtree(repo_path)

  conn = db.connect()
  wiki_id = upsert_wiki(
    conn,
    slug=args.slug,
    name=args.name,
    repo_path=repo_path,
    target=args.target_tokens,
    minimum=args.min_tokens,
    maximum=args.max_tokens,
    force_recreate=args.force_recreate,
  )
  print(f"[init] created wikis row id={wiki_id}", flush=True)

  _init_repo(repo_path, args.name)
  print(f"[init] initialized git repo at {repo_path}", flush=True)

  cfg = SegmenterConfig(
    target=args.target_tokens,
    minimum=args.min_tokens,
    maximum=args.max_tokens,
  )
  inserted = populate_segments(
    conn,
    wiki_id=wiki_id,
    book_ids=args.book_id,
    segmenter_config=cfg,
    skip_chapters=skip_chapters,
  )
  print(f"[init] inserted {inserted} pending segments", flush=True)


if __name__ == "__main__":
  main()
