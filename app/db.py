import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.environ.get("BOOKWORM_DB", Path(__file__).resolve().parent.parent / "bookworm.db"))
DATA_DIR = DB_PATH.parent / "data"
EPUB_DIR = DATA_DIR / "epubs"
COVER_DIR = DATA_DIR / "covers"

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY,
  title TEXT,
  author TEXT,
  total_chunks INTEGER DEFAULT 0,
  cover_path TEXT,
  epub_path TEXT,
  embedding_status TEXT DEFAULT 'ready',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chapters (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  chapter_index INTEGER NOT NULL,
  title TEXT,
  spine_href TEXT,
  start_position INTEGER,
  end_position INTEGER,
  UNIQUE(book_id, chapter_index)
);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  chapter_index INTEGER NOT NULL,
  chapter_title TEXT,
  position_index INTEGER NOT NULL,
  spine_href TEXT,
  anchor_text TEXT,
  canonical_start INTEGER,
  canonical_end INTEGER,
  cfi_range TEXT,
  text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_positions (
  book_id INTEGER PRIMARY KEY,
  chapter_index INTEGER,
  chapter_percent REAL,
  book_percent REAL,
  position_index INTEGER,
  char_offset INTEGER,
  cfi TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  model TEXT,
  position_context INTEGER,
  ask_cfi TEXT,
  ask_chapter_index INTEGER,
  ask_chapter_percent REAL,
  ask_book_percent REAL,
  ask_char_offset INTEGER,
  sources_json TEXT,
  retrieval_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wiki_ingests (
  book_id INTEGER PRIMARY KEY,
  wiki_dir TEXT NOT NULL,
  snapshot_path TEXT NOT NULL,
  snapshot_mtime_ns INTEGER,
  viewer_path TEXT,
  viewer_mtime_ns INTEGER,
  total_snapshots INTEGER DEFAULT 0,
  total_sections INTEGER DEFAULT 0,
  latest_tag TEXT,
  latest_story_order INTEGER,
  status TEXT DEFAULT 'ready',
  last_error TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wiki_sections (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  snapshot_tag TEXT NOT NULL,
  snapshot_rank INTEGER NOT NULL,
  story_order INTEGER,
  page_path TEXT NOT NULL,
  page_title TEXT NOT NULL,
  category TEXT,
  section_heading TEXT,
  section_index INTEGER NOT NULL DEFAULT 0,
  section_text TEXT NOT NULL,
  UNIQUE(book_id, snapshot_tag, page_path, section_index)
);

CREATE INDEX IF NOT EXISTS idx_wiki_sections_book_snapshot
  ON wiki_sections(book_id, snapshot_rank, page_path);

CREATE TABLE IF NOT EXISTS bookmarks (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  cfi TEXT NOT NULL,
  chapter_index INTEGER,
  chapter_percent REAL,
  book_percent REAL,
  chapter_href TEXT,
  anchor_canonical_offset INTEGER,
  anchor_text TEXT,
  label TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(book_id, cfi)
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS virtual_chapter_settings (
  book_id INTEGER PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 0,
  pattern TEXT,
  entries_json TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  detection_mode TEXT NOT NULL DEFAULT 'strict',
  toc_display_mode TEXT NOT NULL DEFAULT 'merged'
);
"""

VEC_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks
USING vec0(
  embedding float[1536],
  chunk_id integer,
  book_id integer,
  position_index integer
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_wiki_sections
USING vec0(
  embedding float[1536],
  section_id integer,
  book_id integer,
  snapshot_rank integer
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, col_type: str):
  rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
  existing = {r[1] for r in rows}
  if column not in existing:
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def _canonical_len(text: str) -> int:
  return len(re.sub(r"[^a-z0-9]+", "", (text or "").lower()))


def _migrate(conn: sqlite3.Connection):
  # Safe additive migrations for existing local DBs.
  _ensure_column(conn, "books", "cover_path", "TEXT")
  _ensure_column(conn, "books", "epub_path", "TEXT")
  _ensure_column(conn, "books", "embedding_status", "TEXT DEFAULT 'ready'")

  _ensure_column(conn, "chapters", "spine_href", "TEXT")

  _ensure_column(conn, "chunks", "spine_href", "TEXT")
  _ensure_column(conn, "chunks", "anchor_text", "TEXT")
  _ensure_column(conn, "chunks", "canonical_start", "INTEGER")
  _ensure_column(conn, "chunks", "canonical_end", "INTEGER")
  _ensure_column(conn, "chunks", "cfi_range", "TEXT")

  # Backfill deterministic chapter offsets for existing chunks.
  missing_offsets = conn.execute(
    """
    SELECT DISTINCT book_id, chapter_index
    FROM chunks
    WHERE canonical_start IS NULL OR canonical_end IS NULL
    """
  ).fetchall()
  for row in missing_offsets:
    book_id = row["book_id"]
    chapter_index = row["chapter_index"]
    chapter_rows = conn.execute(
      """
      SELECT id, text
      FROM chunks
      WHERE book_id = ? AND chapter_index = ?
      ORDER BY position_index, id
      """,
      (book_id, chapter_index),
    ).fetchall()
    cursor = 0
    updates = []
    for chunk in chapter_rows:
      start = cursor
      cursor += _canonical_len(chunk["text"] or "")
      updates.append((start, cursor, chunk["id"]))
    conn.executemany("UPDATE chunks SET canonical_start = ?, canonical_end = ? WHERE id = ?", updates)

  # Invalidate previously computed citation ranges when the algorithm changes.
  cfi_algo_version = "3"
  row = conn.execute("SELECT value FROM settings WHERE key = 'cfi_algo_version'").fetchone()
  if not row or row["value"] != cfi_algo_version:
    conn.execute("UPDATE chunks SET cfi_range = NULL")
    conn.execute(
      "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
      ("cfi_algo_version", cfi_algo_version),
    )

  _ensure_column(conn, "book_positions", "chapter_percent", "REAL")
  _ensure_column(conn, "book_positions", "book_percent", "REAL")
  _ensure_column(conn, "book_positions", "char_offset", "INTEGER")
  _ensure_column(conn, "book_positions", "cfi", "TEXT")
  _ensure_column(conn, "book_positions", "updated_at", "TEXT")
  conn.execute(
    "UPDATE book_positions SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL OR updated_at = ''"
  )

  # Recreate legacy conversation table if needed.
  convo_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)").fetchall()}
  if convo_cols and "question" not in convo_cols:
    conn.execute("ALTER TABLE conversations RENAME TO conversations_old")
    conn.executescript(
      """
      CREATE TABLE conversations (
        id INTEGER PRIMARY KEY,
        book_id INTEGER NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        model TEXT,
        position_context INTEGER,
        ask_cfi TEXT,
        ask_chapter_index INTEGER,
        ask_chapter_percent REAL,
        ask_book_percent REAL,
        ask_char_offset INTEGER,
        sources_json TEXT,
        retrieval_json TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
      );
      DROP TABLE conversations_old;
      """
    )

  _ensure_column(conn, "conversations", "ask_cfi", "TEXT")
  _ensure_column(conn, "conversations", "ask_chapter_index", "INTEGER")
  _ensure_column(conn, "conversations", "ask_chapter_percent", "REAL")
  _ensure_column(conn, "conversations", "ask_book_percent", "REAL")
  _ensure_column(conn, "conversations", "ask_char_offset", "INTEGER")
  _ensure_column(conn, "conversations", "retrieval_json", "TEXT")

  conn.execute(
    """
    CREATE TABLE IF NOT EXISTS virtual_chapter_settings (
      book_id INTEGER PRIMARY KEY,
      enabled INTEGER NOT NULL DEFAULT 0,
      pattern TEXT,
      entries_json TEXT,
      updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
      detection_mode TEXT NOT NULL DEFAULT 'strict',
      toc_display_mode TEXT NOT NULL DEFAULT 'merged'
    )
    """
  )
  _ensure_column(conn, "virtual_chapter_settings", "enabled", "INTEGER NOT NULL DEFAULT 0")
  _ensure_column(conn, "virtual_chapter_settings", "pattern", "TEXT")
  _ensure_column(conn, "virtual_chapter_settings", "entries_json", "TEXT")
  _ensure_column(conn, "virtual_chapter_settings", "updated_at", "TEXT")
  _ensure_column(conn, "virtual_chapter_settings", "detection_mode", "TEXT NOT NULL DEFAULT 'strict'")
  _ensure_column(conn, "virtual_chapter_settings", "toc_display_mode", "TEXT NOT NULL DEFAULT 'merged'")

  # Ensure bookmark storage exists for per-book saved reading points.
  conn.execute(
    """
    CREATE TABLE IF NOT EXISTS bookmarks (
      id INTEGER PRIMARY KEY,
      book_id INTEGER NOT NULL,
      cfi TEXT NOT NULL,
      chapter_index INTEGER,
      chapter_percent REAL,
      book_percent REAL,
      chapter_href TEXT,
      anchor_canonical_offset INTEGER,
      anchor_text TEXT,
      label TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(book_id, cfi)
    )
    """
  )
  _ensure_column(conn, "bookmarks", "chapter_index", "INTEGER")
  _ensure_column(conn, "bookmarks", "chapter_percent", "REAL")
  _ensure_column(conn, "bookmarks", "book_percent", "REAL")
  _ensure_column(conn, "bookmarks", "chapter_href", "TEXT")
  _ensure_column(conn, "bookmarks", "anchor_canonical_offset", "INTEGER")
  _ensure_column(conn, "bookmarks", "anchor_text", "TEXT")
  _ensure_column(conn, "bookmarks", "label", "TEXT")
  _ensure_column(conn, "bookmarks", "created_at", "TEXT")

  # Backfill bookmark anchors for legacy percent-based bookmarks.
  bookmarks_to_backfill = conn.execute(
    """
    SELECT id, book_id, chapter_index, chapter_percent, chapter_href, anchor_canonical_offset, anchor_text
    FROM bookmarks
    WHERE chapter_index IS NOT NULL
    """
  ).fetchall()
  for bookmark in bookmarks_to_backfill:
    bookmark_id = bookmark["id"]
    book_id = bookmark["book_id"]
    chapter_index = bookmark["chapter_index"]
    chapter_percent = bookmark["chapter_percent"]
    chapter_href = bookmark["chapter_href"]
    anchor_offset = bookmark["anchor_canonical_offset"]
    anchor_text = bookmark["anchor_text"]

    resolved_href = chapter_href
    if not resolved_href:
      chapter_row = conn.execute(
        "SELECT spine_href FROM chapters WHERE book_id = ? AND chapter_index = ?",
        (book_id, chapter_index),
      ).fetchone()
      resolved_href = chapter_row["spine_href"] if chapter_row and chapter_row["spine_href"] else None
      if resolved_href:
        conn.execute("UPDATE bookmarks SET chapter_href = ? WHERE id = ?", (resolved_href, bookmark_id))

    resolved_offset = anchor_offset
    if resolved_offset is None and chapter_percent is not None:
      max_row = conn.execute(
        "SELECT MAX(canonical_end) AS max_end FROM chunks WHERE book_id = ? AND chapter_index = ?",
        (book_id, chapter_index),
      ).fetchone()
      max_end = int(max_row["max_end"] or 0) if max_row else 0
      if max_end > 0:
        pct = max(0.0, min(100.0, float(chapter_percent)))
        candidate = int(round((pct / 100.0) * max_end))
        resolved_offset = max(0, min(max_end - 1, candidate))
        conn.execute(
          "UPDATE bookmarks SET anchor_canonical_offset = ? WHERE id = ?",
          (resolved_offset, bookmark_id),
        )

    if not anchor_text and resolved_offset is not None:
      chunk_row = conn.execute(
        """
        SELECT anchor_text
        FROM chunks
        WHERE book_id = ? AND chapter_index = ?
          AND canonical_start <= ? AND canonical_end > ?
        ORDER BY position_index ASC, id ASC
        LIMIT 1
        """,
        (book_id, chapter_index, int(resolved_offset), int(resolved_offset)),
      ).fetchone()
      if not chunk_row or not chunk_row["anchor_text"]:
        chunk_row = conn.execute(
          """
          SELECT anchor_text
          FROM chunks
          WHERE book_id = ? AND chapter_index = ?
          ORDER BY ABS(COALESCE(canonical_start, 0) - ?) ASC, position_index ASC, id ASC
          LIMIT 1
          """,
          (book_id, chapter_index, int(resolved_offset)),
        ).fetchone()
      resolved_text = chunk_row["anchor_text"] if chunk_row and chunk_row["anchor_text"] else None
      if resolved_text:
        conn.execute("UPDATE bookmarks SET anchor_text = ? WHERE id = ?", (resolved_text[:240], bookmark_id))


def connect() -> sqlite3.Connection:
  conn = sqlite3.connect(DB_PATH)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA journal_mode=WAL;")
  conn.execute("PRAGMA foreign_keys=ON;")

  try:
    conn.enable_load_extension(True)
    try:
      import sqlite_vec
      sqlite_vec.load(conn)
    except Exception:
      conn.execute("SELECT load_extension('vec0')")
  except Exception:
    pass

  conn.executescript(SCHEMA)
  _migrate(conn)

  try:
    conn.executescript(VEC_SCHEMA)
  except Exception:
    pass

  EPUB_DIR.mkdir(parents=True, exist_ok=True)
  COVER_DIR.mkdir(parents=True, exist_ok=True)

  return conn


def get_setting(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
  row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
  if not row:
    return default
  return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: str):
  conn.execute(
    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
    (key, value),
  )
