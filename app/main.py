import os
import tempfile
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from pathlib import Path

from .db import connect
from .ingest import extract_chapters, chunk_chapter
from .rag import embed_texts, answer_question

try:
  import sqlite_vec
except Exception:
  sqlite_vec = None

app = FastAPI(title="Bookworm")

app.add_middleware(
  CORSMiddleware,
  allow_origins=["*"],
  allow_credentials=True,
  allow_methods=["*"],
  allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class PositionUpdate(BaseModel):
  chapter_index: int
  percent: float


class QueryRequest(BaseModel):
  question: str
  position_index: int | None = None


@app.get("/", response_class=HTMLResponse)
def index():
  return (STATIC_DIR / "index.html").read_text()


@app.get("/api/books")
def list_books():
  conn = connect()
  rows = conn.execute("SELECT * FROM books ORDER BY created_at DESC").fetchall()
  data = [dict(r) for r in rows]
  return {"books": data}


@app.post("/api/books/import")
def import_book(file: UploadFile = File(...)):
  if not file.filename.endswith(".epub"):
    raise HTTPException(status_code=400, detail="Only .epub supported in MVP")

  with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
    tmp.write(file.file.read())
    tmp_path = tmp.name

  title, author, chapters = extract_chapters(tmp_path)

  conn = connect()
  cur = conn.cursor()
  cur.execute("INSERT INTO books (title, author, total_chunks) VALUES (?, ?, ?)", (title, author, 0))
  book_id = cur.lastrowid

  position_index = 0
  all_chunks = []

  for ch in chapters:
    ch_chunks, next_pos = chunk_chapter(ch["text"], ch["chapter_index"], ch["title"], position_index)
    if ch_chunks:
      start_pos = ch_chunks[0]["position_index"]
      end_pos = ch_chunks[-1]["position_index"]
    else:
      start_pos = position_index
      end_pos = position_index

    cur.execute(
      "INSERT INTO chapters (book_id, chapter_index, title, start_position, end_position) VALUES (?, ?, ?, ?, ?)",
      (book_id, ch["chapter_index"], ch["title"], start_pos, end_pos),
    )

    all_chunks.extend(ch_chunks)
    position_index = next_pos

  # embed and insert chunks
  texts = [c["text"] for c in all_chunks]
  embeddings = embed_texts(texts) if texts else []

  for chunk, emb in zip(all_chunks, embeddings):
    cur.execute(
      "INSERT INTO chunks (book_id, chapter_index, chapter_title, position_index, text) VALUES (?, ?, ?, ?, ?)",
      (book_id, chunk["chapter_index"], chunk["chapter_title"], chunk["position_index"], chunk["text"]),
    )
    chunk_id = cur.lastrowid
    try:
      if sqlite_vec:
        if hasattr(sqlite_vec, "serialize"):
          vec = sqlite_vec.serialize(emb)
        elif hasattr(sqlite_vec, "serialize_float32"):
          vec = sqlite_vec.serialize_float32(emb)
        else:
          vec = emb
      else:
        vec = emb

      cur.execute(
        "INSERT INTO vec_chunks (embedding, chunk_id, book_id, position_index) VALUES (?, ?, ?, ?)",
        (vec, chunk_id, book_id, chunk["position_index"]),
      )
    except Exception:
      pass

  cur.execute("UPDATE books SET total_chunks = ? WHERE id = ?", (len(all_chunks), book_id))
  conn.commit()

  return {"book_id": book_id, "title": title, "author": author, "total_chunks": len(all_chunks)}


@app.post("/api/books/{book_id}/position")
def set_position(book_id: int, payload: PositionUpdate):
  conn = connect()
  # map chapter+percent to position_index
  chapter = conn.execute(
    "SELECT * FROM chapters WHERE book_id = ? AND chapter_index = ?",
    (book_id, payload.chapter_index),
  ).fetchone()

  if not chapter:
    raise HTTPException(status_code=404, detail="Chapter not found")

  start_pos = chapter["start_position"]
  end_pos = chapter["end_position"]
  pos_index = int(start_pos + (end_pos - start_pos) * (payload.percent / 100.0))

  conn.execute(
    "INSERT INTO book_positions (book_id, chapter_index, percent, position_index) VALUES (?, ?, ?, ?)\n"
    "ON CONFLICT(book_id) DO UPDATE SET chapter_index=excluded.chapter_index, percent=excluded.percent, position_index=excluded.position_index",
    (book_id, payload.chapter_index, payload.percent, pos_index),
  )
  conn.commit()
  return {"book_id": book_id, "position_index": pos_index}


@app.post("/api/books/{book_id}/query")
def query(book_id: int, payload: QueryRequest):
  conn = connect()

  if payload.position_index is None:
    pos_row = conn.execute("SELECT position_index FROM book_positions WHERE book_id = ?", (book_id,)).fetchone()
    if not pos_row:
      raise HTTPException(status_code=400, detail="No reading position set for this book")
    position_index = pos_row["position_index"]
  else:
    position_index = payload.position_index

  query_emb = embed_texts([payload.question])[0]
  if sqlite_vec:
    if hasattr(sqlite_vec, "serialize"):
      vec = sqlite_vec.serialize(query_emb)
    elif hasattr(sqlite_vec, "serialize_float32"):
      vec = sqlite_vec.serialize_float32(query_emb)
    else:
      vec = query_emb
  else:
    vec = query_emb

  try:
    rows = conn.execute(
      "SELECT chunk_id, distance FROM vec_chunks WHERE book_id = ? AND position_index <= ? AND embedding MATCH ? ORDER BY distance LIMIT 12",
      (book_id, position_index, vec),
    ).fetchall()
  except Exception:
    raise HTTPException(status_code=500, detail="Vector index not available. Is sqlite-vec installed?")

  chunk_ids = [r["chunk_id"] for r in rows]
  if not chunk_ids:
    return {"answer": "I don't have enough information from the text you've read so far.", "sources": []}

  placeholders = ",".join(["?"] * len(chunk_ids))
  chunk_rows = conn.execute(
    f"SELECT * FROM chunks WHERE id IN ({placeholders}) ORDER BY position_index",
    chunk_ids,
  ).fetchall()

  excerpts = [dict(r) for r in chunk_rows]
  answer = answer_question(payload.question, excerpts)

  return {
    "answer": answer["answer"],
    "sources": excerpts,
  }
