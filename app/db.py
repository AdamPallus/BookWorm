import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("BOOKWORM_DB", Path(__file__).resolve().parent.parent / "bookworm.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY,
  title TEXT,
  author TEXT,
  total_chunks INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS chapters (
  id INTEGER PRIMARY KEY,
  book_id INTEGER NOT NULL,
  chapter_index INTEGER NOT NULL,
  title TEXT,
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
  text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_positions (
  book_id INTEGER PRIMARY KEY,
  chapter_index INTEGER,
  percent REAL,
  position_index INTEGER
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


def connect():
  conn = sqlite3.connect(DB_PATH)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA journal_mode=WAL;")

  # Load sqlite-vec extension
  try:
    conn.enable_load_extension(True)
    try:
      import sqlite_vec
      sqlite_vec.load(conn)
    except Exception:
      # fallback to extension load by name if available in system
      conn.execute("SELECT load_extension('vec0')")
  except Exception:
    pass

  conn.executescript(SCHEMA)
  try:
    conn.executescript(VEC_SCHEMA)
  except Exception:
    # If vec extension not loaded, still allow app to run (queries will fail)
    pass

  return conn
