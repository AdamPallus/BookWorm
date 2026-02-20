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
  sources_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bookmarks (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  cfi TEXT NOT NULL,
  chapter_index INTEGER,
  chapter_percent REAL,
  book_percent REAL,
  label TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(book_id, cfi)
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
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
        sources_json TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
      );
      DROP TABLE conversations_old;
      """
    )

  _ensure_column(conn, "conversations", "ask_cfi", "TEXT")
  _ensure_column(conn, "conversations", "ask_chapter_index", "INTEGER")
  _ensure_column(conn, "conversations", "ask_chapter_percent", "REAL")
  _ensure_column(conn, "conversations", "ask_book_percent", "REAL")

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
      label TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(book_id, cfi)
    )
    """
  )
  _ensure_column(conn, "bookmarks", "chapter_index", "INTEGER")
  _ensure_column(conn, "bookmarks", "chapter_percent", "REAL")
  _ensure_column(conn, "bookmarks", "book_percent", "REAL")
  _ensure_column(conn, "bookmarks", "label", "TEXT")
  _ensure_column(conn, "bookmarks", "created_at", "TEXT")


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
