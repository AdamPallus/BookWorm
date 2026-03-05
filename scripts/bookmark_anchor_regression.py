#!/usr/bin/env python3
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from app.db import SCHEMA, _migrate


def main():
  conn = sqlite3.connect(":memory:")
  conn.row_factory = sqlite3.Row
  conn.executescript(SCHEMA)

  conn.execute(
    "INSERT INTO books (id, title, author, total_chunks, cover_path, epub_path, embedding_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
    (1, "Demo", "Author", 3, None, "demo.epub", "ready"),
  )
  conn.execute(
    "INSERT INTO chapters (book_id, chapter_index, title, spine_href, start_position, end_position) VALUES (?, ?, ?, ?, ?, ?)",
    (1, 0, "Chapter 1", "Text/ch1.xhtml", 0, 2),
  )
  conn.executemany(
    """
    INSERT INTO chunks (
      book_id, chapter_index, chapter_title, position_index, spine_href, anchor_text, canonical_start, canonical_end, cfi_range, text
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    [
      (1, 0, "Chapter 1", 0, "Text/ch1.xhtml", "alpha chunk", 0, 40, None, "alpha"),
      (1, 0, "Chapter 1", 1, "Text/ch1.xhtml", "middle chunk", 40, 80, None, "middle"),
      (1, 0, "Chapter 1", 2, "Text/ch1.xhtml", "omega chunk", 80, 120, None, "omega"),
    ],
  )
  conn.execute(
    """
    INSERT INTO bookmarks (
      book_id, cfi, chapter_index, chapter_percent, book_percent, chapter_href, anchor_canonical_offset, anchor_text, label
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (1, "epubcfi(/6/2!)", 0, 50.0, 10.0, None, None, None, "Legacy"),
  )

  _migrate(conn)

  row = conn.execute(
    """
    SELECT chapter_href, anchor_canonical_offset, anchor_text
    FROM bookmarks
    WHERE book_id = ? AND cfi = ?
    """,
    (1, "epubcfi(/6/2!)"),
  ).fetchone()
  assert row is not None, "Bookmark row missing after migration."
  assert row["chapter_href"] == "Text/ch1.xhtml", f"Unexpected chapter_href: {row['chapter_href']!r}"
  assert row["anchor_canonical_offset"] == 60, f"Unexpected anchor offset: {row['anchor_canonical_offset']!r}"
  assert row["anchor_text"] == "middle chunk", f"Unexpected anchor_text: {row['anchor_text']!r}"
  print("bookmark_anchor_regression: ok")


if __name__ == "__main__":
  main()
