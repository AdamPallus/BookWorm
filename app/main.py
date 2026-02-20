import json
import re
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from .db import COVER_DIR, EPUB_DIR, connect, get_setting, set_setting
from .ingest import chunk_chapter, extract_book
from .rag import embed_texts, get_api_key, set_api_key, stream_answer

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

ALLOWED_MODELS = ["gpt-5-mini", "gpt-5", "gpt-5.2"]
DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_FONT_SIZE = 100
ALLOWED_READER_SPREADS = ["single", "double"]
DEFAULT_READER_SPREAD = "single"
DEFAULT_READER_WIDTH_PX = 980
MIN_READER_WIDTH_PX = 760
MAX_READER_WIDTH_PX = 1320
DEFAULT_READER_BOTTOM_PADDING_PX = 34
MIN_READER_BOTTOM_PADDING_PX = 12
MAX_READER_BOTTOM_PADDING_PX = 120
DEFAULT_CITATION_DEBUG_MODE = False
CANONICAL_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


class PositionUpdate(BaseModel):
  chapter_index: Optional[int] = None
  chapter_percent: Optional[float] = None
  book_percent: Optional[float] = None
  percent: Optional[float] = None
  cfi: Optional[str] = None


class QueryRequest(BaseModel):
  question: str
  position_index: Optional[int] = None
  model: Optional[str] = None
  ask_cfi: Optional[str] = None
  ask_chapter_index: Optional[int] = None
  ask_chapter_percent: Optional[float] = None
  ask_book_percent: Optional[float] = None


class SettingsUpdate(BaseModel):
  api_key: Optional[str] = None
  model: Optional[str] = None
  reader_font_size: Optional[int] = None
  reader_spread: Optional[str] = None
  reader_width_px: Optional[int] = None
  reader_bottom_padding_px: Optional[int] = None
  citation_debug_mode: Optional[bool] = None


class ChunkCfiUpdate(BaseModel):
  chunk_id: int
  cfi_range: Optional[str] = None


class ChunkCfiBatchUpdate(BaseModel):
  updates: List[ChunkCfiUpdate]


class BookmarkPayload(BaseModel):
  cfi: str
  chapter_index: Optional[int] = None
  chapter_percent: Optional[float] = None
  book_percent: Optional[float] = None
  label: Optional[str] = None


def _serialize_vec(embedding):
  if not sqlite_vec:
    return embedding
  if hasattr(sqlite_vec, "serialize"):
    return sqlite_vec.serialize(embedding)
  if hasattr(sqlite_vec, "serialize_float32"):
    return sqlite_vec.serialize_float32(embedding)
  return embedding


def _validate_model(model: str) -> str:
  if model not in ALLOWED_MODELS:
    raise HTTPException(status_code=400, detail=f"Unsupported model '{model}'")
  return model


def _current_model(conn) -> str:
  stored = get_setting(conn, "qa_model", DEFAULT_MODEL)
  return stored if stored in ALLOWED_MODELS else DEFAULT_MODEL


def _current_font_size(conn) -> int:
  raw = get_setting(conn, "reader_font_size", str(DEFAULT_FONT_SIZE))
  try:
    value = int(raw)
  except (TypeError, ValueError):
    value = DEFAULT_FONT_SIZE
  return max(70, min(160, value))


def _current_reader_spread(conn) -> str:
  raw = get_setting(conn, "reader_spread", DEFAULT_READER_SPREAD)
  return raw if raw in ALLOWED_READER_SPREADS else DEFAULT_READER_SPREAD


def _current_reader_width_px(conn) -> int:
  raw = get_setting(conn, "reader_width_px", str(DEFAULT_READER_WIDTH_PX))
  try:
    value = int(raw)
  except (TypeError, ValueError):
    value = DEFAULT_READER_WIDTH_PX
  return max(MIN_READER_WIDTH_PX, min(MAX_READER_WIDTH_PX, value))


def _current_reader_bottom_padding_px(conn) -> int:
  raw = get_setting(conn, "reader_bottom_padding_px", str(DEFAULT_READER_BOTTOM_PADDING_PX))
  try:
    value = int(raw)
  except (TypeError, ValueError):
    value = DEFAULT_READER_BOTTOM_PADDING_PX
  return max(MIN_READER_BOTTOM_PADDING_PX, min(MAX_READER_BOTTOM_PADDING_PX, value))


def _current_citation_debug_mode(conn) -> bool:
  raw = str(get_setting(conn, "citation_debug_mode", "1" if DEFAULT_CITATION_DEBUG_MODE else "0")).strip().lower()
  return raw in {"1", "true", "yes", "on"}


def _book_row_or_404(conn, book_id: int):
  row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
  if not row:
    raise HTTPException(status_code=404, detail="Book not found")
  return row


def _save_conversation(
  conn,
  book_id: int,
  question: str,
  answer: str,
  model: str,
  position_index: int,
  sources,
  ask_cfi: Optional[str] = None,
  ask_chapter_index: Optional[int] = None,
  ask_chapter_percent: Optional[float] = None,
  ask_book_percent: Optional[float] = None,
):
  conn.execute(
    """
    INSERT INTO conversations
      (book_id, question, answer, model, position_context, ask_cfi, ask_chapter_index, ask_chapter_percent, ask_book_percent, sources_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
      book_id,
      question,
      answer,
      model,
      position_index,
      ask_cfi,
      ask_chapter_index,
      ask_chapter_percent,
      ask_book_percent,
      json.dumps(sources, ensure_ascii=False),
    ),
  )
  conn.commit()


def _canonical_alnum_len(text: str) -> int:
  return len(CANONICAL_NON_ALNUM_RE.sub("", (text or "").lower()))


@app.get("/", response_class=HTMLResponse)
def index():
  return (STATIC_DIR / "index.html").read_text()


@app.get("/read/{book_id}", response_class=HTMLResponse)
def read_page(book_id: int):
  return (STATIC_DIR / "index.html").read_text()


def _static_asset_response(filename: str, media_type: str):
  file_path = STATIC_DIR / filename
  if not file_path.exists():
    return Response(status_code=404)
  return FileResponse(file_path, media_type=media_type)


@app.get("/favicon.ico")
def favicon():
  return _static_asset_response("favicon.ico", "image/x-icon")


@app.get("/favicon-16.png")
def favicon_16():
  return _static_asset_response("favicon-16.png", "image/png")


@app.get("/favicon-32.png")
def favicon_32():
  return _static_asset_response("favicon-32.png", "image/png")


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
  return _static_asset_response("apple-touch-icon.png", "image/png")


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools_probe():
  return Response(status_code=204)


@app.get("/api/settings")
def get_settings():
  conn = connect()
  key = get_api_key()
  data = {
    "api_key": key,
    "has_key": key is not None,
    "model": _current_model(conn),
    "models": ALLOWED_MODELS,
    "reader_font_size": _current_font_size(conn),
    "reader_spread": _current_reader_spread(conn),
    "reader_spread_options": ALLOWED_READER_SPREADS,
    "reader_width_px": _current_reader_width_px(conn),
    "reader_width_px_min": MIN_READER_WIDTH_PX,
    "reader_width_px_max": MAX_READER_WIDTH_PX,
    "reader_bottom_padding_px": _current_reader_bottom_padding_px(conn),
    "reader_bottom_padding_px_min": MIN_READER_BOTTOM_PADDING_PX,
    "reader_bottom_padding_px_max": MAX_READER_BOTTOM_PADDING_PX,
    "citation_debug_mode": _current_citation_debug_mode(conn),
  }
  conn.close()
  return data


@app.post("/api/settings")
def save_settings(payload: SettingsUpdate):
  conn = connect()

  try:
    if payload.api_key is not None:
      if not payload.api_key or not payload.api_key.startswith("sk-"):
        raise HTTPException(status_code=400, detail="Invalid API key format")
      set_api_key(payload.api_key)

    if payload.model is not None:
      _validate_model(payload.model)
      set_setting(conn, "qa_model", payload.model)

    if payload.reader_font_size is not None:
      size = max(70, min(160, int(payload.reader_font_size)))
      set_setting(conn, "reader_font_size", str(size))

    if payload.reader_spread is not None:
      if payload.reader_spread not in ALLOWED_READER_SPREADS:
        raise HTTPException(status_code=400, detail=f"Unsupported reader_spread '{payload.reader_spread}'")
      set_setting(conn, "reader_spread", payload.reader_spread)

    if payload.reader_width_px is not None:
      width = max(MIN_READER_WIDTH_PX, min(MAX_READER_WIDTH_PX, int(payload.reader_width_px)))
      set_setting(conn, "reader_width_px", str(width))

    if payload.reader_bottom_padding_px is not None:
      bottom = max(
        MIN_READER_BOTTOM_PADDING_PX,
        min(MAX_READER_BOTTOM_PADDING_PX, int(payload.reader_bottom_padding_px)),
      )
      set_setting(conn, "reader_bottom_padding_px", str(bottom))

    if payload.citation_debug_mode is not None:
      set_setting(conn, "citation_debug_mode", "1" if payload.citation_debug_mode else "0")

    conn.commit()
    key = get_api_key()
    return {
      "api_key": key,
      "has_key": key is not None,
      "model": _current_model(conn),
      "models": ALLOWED_MODELS,
      "reader_font_size": _current_font_size(conn),
      "reader_spread": _current_reader_spread(conn),
      "reader_spread_options": ALLOWED_READER_SPREADS,
      "reader_width_px": _current_reader_width_px(conn),
      "reader_width_px_min": MIN_READER_WIDTH_PX,
      "reader_width_px_max": MAX_READER_WIDTH_PX,
      "reader_bottom_padding_px": _current_reader_bottom_padding_px(conn),
      "reader_bottom_padding_px_min": MIN_READER_BOTTOM_PADDING_PX,
      "reader_bottom_padding_px_max": MAX_READER_BOTTOM_PADDING_PX,
      "citation_debug_mode": _current_citation_debug_mode(conn),
    }
  finally:
    conn.close()


@app.get("/api/books")
def list_books():
  conn = connect()
  rows = conn.execute(
    """
    SELECT
      b.*,
      bp.chapter_index,
      bp.chapter_percent,
      bp.book_percent,
      bp.position_index,
      bp.updated_at AS last_read_at,
      c.title AS chapter_title
    FROM books b
    LEFT JOIN book_positions bp ON bp.book_id = b.id
    LEFT JOIN chapters c ON c.book_id = b.id AND c.chapter_index = bp.chapter_index
    WHERE b.epub_path IS NOT NULL
    ORDER BY COALESCE(bp.updated_at, b.created_at) DESC, b.id DESC
    """
  ).fetchall()

  data = {"books": [dict(r) for r in rows]}
  conn.close()
  return data


@app.get("/api/books/{book_id}")
def get_book(book_id: int):
  conn = connect()
  try:
    book = _book_row_or_404(conn, book_id)

    chapters = conn.execute(
      "SELECT chapter_index, title, spine_href, start_position, end_position FROM chapters WHERE book_id = ? ORDER BY chapter_index",
      (book_id,),
    ).fetchall()
    pos = conn.execute("SELECT * FROM book_positions WHERE book_id = ?", (book_id,)).fetchone()

    return {
      "book": dict(book),
      "chapters": [dict(r) for r in chapters],
      "current_position": dict(pos) if pos else None,
    }
  finally:
    conn.close()


@app.get("/api/books/{book_id}/chapters")
def get_chapters(book_id: int):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)
    rows = conn.execute(
      "SELECT chapter_index, title, spine_href, start_position, end_position FROM chapters WHERE book_id = ? ORDER BY chapter_index",
      (book_id,),
    ).fetchall()
    return {"chapters": [dict(r) for r in rows]}
  finally:
    conn.close()


@app.get("/api/books/{book_id}/chapters/{chapter_index}/chunks")
def get_chapter_chunks(book_id: int, chapter_index: int):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)
    rows = conn.execute(
      """
      SELECT id, chapter_index, chapter_title, position_index, spine_href, anchor_text, canonical_start, canonical_end, cfi_range, text
      FROM chunks
      WHERE book_id = ? AND chapter_index = ?
      ORDER BY position_index
      """,
      (book_id, chapter_index),
    ).fetchall()
    return {"chunks": [dict(r) for r in rows]}
  finally:
    conn.close()


@app.get("/api/books/{book_id}/cover")
def get_cover(book_id: int):
  conn = connect()
  try:
    row = _book_row_or_404(conn, book_id)

    path = row["cover_path"]
    if not path:
      raise HTTPException(status_code=404, detail="Cover not available")

    file_path = Path(path)
    if not file_path.exists():
      raise HTTPException(status_code=404, detail="Cover file missing")

    media_type = "image/jpeg"
    suffix = file_path.suffix.lower()
    if suffix == ".png":
      media_type = "image/png"
    elif suffix == ".webp":
      media_type = "image/webp"
    elif suffix == ".gif":
      media_type = "image/gif"

    return FileResponse(file_path, media_type=media_type)
  finally:
    conn.close()


def _epub_response(book_id: int):
  conn = connect()
  try:
    row = _book_row_or_404(conn, book_id)

    path = row["epub_path"]
    if not path:
      raise HTTPException(status_code=404, detail="EPUB not available")

    file_path = Path(path)
    if not file_path.exists():
      raise HTTPException(status_code=404, detail="EPUB file missing")

    return FileResponse(file_path, media_type="application/epub+zip", filename=file_path.name)
  finally:
    conn.close()


@app.get("/api/books/{book_id}/epub")
def get_epub(book_id: int):
  return _epub_response(book_id)


@app.get("/api/books/{book_id}/book.epub")
def get_epub_with_extension(book_id: int):
  return _epub_response(book_id)


@app.post("/api/books/import")
def import_book(file: UploadFile = File(...)):
  filename = file.filename or "book.epub"
  if not filename.lower().endswith(".epub"):
    raise HTTPException(status_code=400, detail="Only .epub supported")

  conn = connect()
  cur = conn.cursor()

  cur.execute(
    "INSERT INTO books (title, author, total_chunks, embedding_status) VALUES (?, ?, ?, ?)",
    (Path(filename).stem, "Unknown", 0, "processing"),
  )
  book_id = cur.lastrowid
  epub_path = EPUB_DIR / f"{book_id}.epub"

  with epub_path.open("wb") as out:
    shutil.copyfileobj(file.file, out)

  try:
    title, author, chapters, cover_bytes, cover_ext = extract_book(str(epub_path))

    cover_path = None
    if cover_bytes:
      ext = cover_ext or ".jpg"
      cover_file = COVER_DIR / f"{book_id}{ext}"
      cover_file.write_bytes(cover_bytes)
      cover_path = str(cover_file)

    position_index = 0
    all_chunks = []

    for ch in chapters:
      ch_chunks, next_pos = chunk_chapter(
        ch["text"],
        ch["chapter_index"],
        ch["title"],
        position_index,
        ch.get("spine_href"),
      )

      if ch_chunks:
        start_pos = ch_chunks[0]["position_index"]
        end_pos = ch_chunks[-1]["position_index"]
      else:
        start_pos = position_index
        end_pos = position_index

      cur.execute(
        "INSERT INTO chapters (book_id, chapter_index, title, spine_href, start_position, end_position) VALUES (?, ?, ?, ?, ?, ?)",
        (book_id, ch["chapter_index"], ch["title"], ch.get("spine_href"), start_pos, end_pos),
      )

      all_chunks.extend(ch_chunks)
      position_index = next_pos

    embeddings = embed_texts([c["text"] for c in all_chunks]) if all_chunks else []

    for chunk, emb in zip(all_chunks, embeddings):
      cur.execute(
        """
        INSERT INTO chunks
          (book_id, chapter_index, chapter_title, position_index, spine_href, anchor_text, canonical_start, canonical_end, text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
          book_id,
          chunk["chapter_index"],
          chunk["chapter_title"],
          chunk["position_index"],
          chunk.get("spine_href"),
          chunk.get("anchor_text"),
          chunk.get("canonical_start"),
          chunk.get("canonical_end"),
          chunk["text"],
        ),
      )
      chunk_id = cur.lastrowid

      try:
        cur.execute(
          "INSERT INTO vec_chunks (embedding, chunk_id, book_id, position_index) VALUES (?, ?, ?, ?)",
          (_serialize_vec(emb), chunk_id, book_id, chunk["position_index"]),
        )
      except Exception:
        pass

    cur.execute(
      "UPDATE books SET title = ?, author = ?, total_chunks = ?, cover_path = ?, epub_path = ?, embedding_status = 'ready' WHERE id = ?",
      (title, author, len(all_chunks), cover_path, str(epub_path), book_id),
    )
    conn.commit()

    return {
      "book_id": book_id,
      "title": title,
      "author": author,
      "total_chunks": len(all_chunks),
      "embedding_status": "ready",
    }
  except Exception as exc:
    cur.execute(
      "UPDATE books SET epub_path = ?, embedding_status = 'failed' WHERE id = ?",
      (str(epub_path), book_id),
    )
    conn.commit()
    raise HTTPException(status_code=500, detail=f"Import failed: {exc}")
  finally:
    conn.close()


@app.post("/api/books/{book_id}/position")
def set_position(book_id: int, payload: PositionUpdate):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)

    chapter_index = payload.chapter_index
    if chapter_index is None:
      existing = conn.execute("SELECT chapter_index FROM book_positions WHERE book_id = ?", (book_id,)).fetchone()
      if existing:
        chapter_index = existing["chapter_index"]
    if chapter_index is None:
      raise HTTPException(status_code=400, detail="chapter_index is required")

    chapter = conn.execute(
      "SELECT * FROM chapters WHERE book_id = ? AND chapter_index = ?",
      (book_id, chapter_index),
    ).fetchone()
    if not chapter:
      raise HTTPException(status_code=404, detail="Chapter not found")

    chapter_percent = payload.chapter_percent
    if chapter_percent is None:
      chapter_percent = payload.percent if payload.percent is not None else 0.0
    chapter_percent = max(0.0, min(100.0, float(chapter_percent)))

    start_pos = chapter["start_position"] or 0
    end_pos = chapter["end_position"] or start_pos
    span = max(0, end_pos - start_pos)
    pos_index = int(start_pos + span * (chapter_percent / 100.0))

    book_percent = payload.book_percent if payload.book_percent is not None else None
    if book_percent is not None:
      book_percent = max(0.0, min(100.0, float(book_percent)))

    conn.execute(
      """
      INSERT INTO book_positions (book_id, chapter_index, chapter_percent, book_percent, position_index, cfi, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
      ON CONFLICT(book_id) DO UPDATE SET
        chapter_index = excluded.chapter_index,
        chapter_percent = excluded.chapter_percent,
        book_percent = excluded.book_percent,
        position_index = excluded.position_index,
        cfi = excluded.cfi,
        updated_at = CURRENT_TIMESTAMP
      """,
      (book_id, chapter_index, chapter_percent, book_percent, pos_index, payload.cfi),
    )
    conn.commit()

    return {
      "book_id": book_id,
      "chapter_index": chapter_index,
      "chapter_percent": chapter_percent,
      "book_percent": book_percent,
      "position_index": pos_index,
      "cfi": payload.cfi,
    }
  finally:
    conn.close()


@app.get("/api/books/{book_id}/conversations")
def get_conversations(book_id: int):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)

    rows = conn.execute(
      "SELECT * FROM conversations WHERE book_id = ? ORDER BY id ASC",
      (book_id,),
    ).fetchall()

    data = []
    for row in rows:
      entry = dict(row)
      raw = entry.get("sources_json")
      try:
        entry["sources"] = json.loads(raw) if raw else []
      except Exception:
        entry["sources"] = []
      data.append(entry)

    return {"conversations": data}
  finally:
    conn.close()


@app.get("/api/books/{book_id}/bookmarks")
def get_bookmarks(book_id: int):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)
    rows = conn.execute(
      """
      SELECT id, book_id, cfi, chapter_index, chapter_percent, book_percent, label, created_at
      FROM bookmarks
      WHERE book_id = ?
      ORDER BY created_at DESC, id DESC
      """,
      (book_id,),
    ).fetchall()
    return {"bookmarks": [dict(r) for r in rows]}
  finally:
    conn.close()


@app.post("/api/books/{book_id}/bookmarks/toggle")
def toggle_bookmark(book_id: int, payload: BookmarkPayload):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)
    cfi = (payload.cfi or "").strip()
    if not cfi:
      raise HTTPException(status_code=400, detail="Bookmark CFI is required")

    existing = conn.execute(
      "SELECT id FROM bookmarks WHERE book_id = ? AND cfi = ?",
      (book_id, cfi),
    ).fetchone()
    if existing:
      conn.execute(
        "DELETE FROM bookmarks WHERE id = ? AND book_id = ?",
        (existing["id"], book_id),
      )
      conn.commit()
      return {"bookmarked": False, "bookmark": None}

    chapter_percent = None
    if payload.chapter_percent is not None:
      chapter_percent = max(0.0, min(100.0, float(payload.chapter_percent)))
    book_percent = None
    if payload.book_percent is not None:
      book_percent = max(0.0, min(100.0, float(payload.book_percent)))

    conn.execute(
      """
      INSERT INTO bookmarks (book_id, cfi, chapter_index, chapter_percent, book_percent, label)
      VALUES (?, ?, ?, ?, ?, ?)
      """,
      (
        int(book_id),
        cfi,
        payload.chapter_index,
        chapter_percent,
        book_percent,
        payload.label.strip() if payload.label else None,
      ),
    )
    row = conn.execute(
      """
      SELECT id, book_id, cfi, chapter_index, chapter_percent, book_percent, label, created_at
      FROM bookmarks
      WHERE book_id = ? AND cfi = ?
      """,
      (book_id, cfi),
    ).fetchone()
    conn.commit()
    return {"bookmarked": True, "bookmark": dict(row) if row else None}
  finally:
    conn.close()


@app.delete("/api/books/{book_id}/bookmarks/{bookmark_id}")
def delete_bookmark(book_id: int, bookmark_id: int):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)
    conn.execute(
      "DELETE FROM bookmarks WHERE id = ? AND book_id = ?",
      (bookmark_id, book_id),
    )
    conn.commit()
    return {"ok": True}
  finally:
    conn.close()


@app.post("/api/books/{book_id}/query")
def query(book_id: int, payload: QueryRequest):
  conn = connect()
  _book_row_or_404(conn, book_id)

  if not payload.question.strip():
    raise HTTPException(status_code=400, detail="Question is required")

  if payload.position_index is None:
    pos_row = conn.execute("SELECT * FROM book_positions WHERE book_id = ?", (book_id,)).fetchone()
    if not pos_row:
      raise HTTPException(status_code=400, detail="No reading position set for this book")
    position_index = pos_row["position_index"]
  else:
    position_index = payload.position_index
    pos_row = conn.execute("SELECT * FROM book_positions WHERE book_id = ?", (book_id,)).fetchone()

  ask_cfi = payload.ask_cfi.strip() if payload.ask_cfi else None
  ask_chapter_index = payload.ask_chapter_index if payload.ask_chapter_index is not None else None
  ask_chapter_percent = None
  if payload.ask_chapter_percent is not None:
    ask_chapter_percent = max(0.0, min(100.0, float(payload.ask_chapter_percent)))
  ask_book_percent = None
  if payload.ask_book_percent is not None:
    ask_book_percent = max(0.0, min(100.0, float(payload.ask_book_percent)))
  if pos_row:
    if ask_cfi is None:
      ask_cfi = pos_row["cfi"]
    if ask_chapter_index is None:
      ask_chapter_index = pos_row["chapter_index"]
    if ask_chapter_percent is None and pos_row["chapter_percent"] is not None:
      ask_chapter_percent = max(0.0, min(100.0, float(pos_row["chapter_percent"])))
    if ask_book_percent is None and pos_row["book_percent"] is not None:
      ask_book_percent = max(0.0, min(100.0, float(pos_row["book_percent"])))

  model = _validate_model(payload.model) if payload.model else _current_model(conn)
  citation_debug_mode = _current_citation_debug_mode(conn)

  def _line(obj):
    return json.dumps(obj, ensure_ascii=False) + "\n"

  try:
    query_vec = _serialize_vec(embed_texts([payload.question])[0])
    rows = conn.execute(
      "SELECT chunk_id, distance FROM vec_chunks WHERE book_id = ? AND position_index <= ? AND embedding MATCH ? ORDER BY distance LIMIT 12",
      (book_id, position_index, query_vec),
    ).fetchall()
  except Exception as exc:
    raise HTTPException(status_code=500, detail=f"Vector search unavailable: {exc}")

  chunk_ids = [r["chunk_id"] for r in rows]
  excerpts = []
  if chunk_ids:
    placeholders = ",".join(["?"] * len(chunk_ids))
    excerpt_rows = conn.execute(
      f"SELECT * FROM chunks WHERE id IN ({placeholders})",
      chunk_ids,
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in excerpt_rows}
    excerpts = [by_id[cid] for cid in chunk_ids if cid in by_id]

  sources = [
    {
      "chunk_id": ex["id"],
      "chapter_index": ex["chapter_index"],
      "chapter_title": ex.get("chapter_title"),
      "position_index": ex["position_index"],
      "spine_href": ex.get("spine_href"),
      "canonical_start": ex.get("canonical_start"),
      "canonical_end": ex.get("canonical_end"),
      "cfi_range": ex.get("cfi_range"),
      "anchor_text": ex.get("anchor_text"),
      "snippet": (ex["text"] or "")[:280],
      **({"debug_chunk_text": (ex["text"] or "")} if citation_debug_mode else {}),
    }
    for ex in excerpts
  ]

  def stream():
    try:
      if not excerpts:
        answer = "I don't have enough information from the text you've read so far."
        _save_conversation(
          conn,
          book_id,
          payload.question,
          answer,
          model,
          position_index,
          [],
          ask_cfi=ask_cfi,
          ask_chapter_index=ask_chapter_index,
          ask_chapter_percent=ask_chapter_percent,
          ask_book_percent=ask_book_percent,
        )
        yield _line({"type": "done", "data": {"answer": answer, "sources": []}})
        return

      answer_parts = []
      for delta in stream_answer(payload.question, excerpts, model):
        answer_parts.append(delta)
        yield _line({"type": "delta", "delta": delta})

      answer = "".join(answer_parts).strip()
      if not answer:
        answer = "I don't have enough information from the text you've read so far."

      _save_conversation(
        conn,
        book_id,
        payload.question,
        answer,
        model,
        position_index,
        sources,
        ask_cfi=ask_cfi,
        ask_chapter_index=ask_chapter_index,
        ask_chapter_percent=ask_chapter_percent,
        ask_book_percent=ask_book_percent,
      )
      yield _line({"type": "done", "data": {"answer": answer, "sources": sources}})
    except Exception as exc:
      yield _line({"type": "error", "error": str(exc)})
    finally:
      conn.close()

  return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/books/{book_id}/chunks/cfi")
def save_chunk_cfis(book_id: int, payload: ChunkCfiBatchUpdate):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)
    updates = payload.updates or []
    if not updates:
      return {"updated": 0}

    tuples = [
      (
        (u.cfi_range.strip() if u.cfi_range else None),
        int(u.chunk_id),
        int(book_id),
      )
      for u in updates
    ]
    conn.executemany(
      "UPDATE chunks SET cfi_range = ? WHERE id = ? AND book_id = ?",
      tuples,
    )
    conn.commit()
    return {"updated": conn.total_changes}
  finally:
    conn.close()


@app.get("/api/books/{book_id}/search")
def search_book(
  book_id: int,
  q: str = Query(..., min_length=2, max_length=120),
  limit: int = Query(40, ge=1, le=200),
):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)
    query = q.strip()
    if len(query) < 2:
      raise HTTPException(status_code=400, detail="Search query must be at least 2 characters")

    pos_row = conn.execute("SELECT position_index FROM book_positions WHERE book_id = ?", (book_id,)).fetchone()
    max_position = pos_row["position_index"] if pos_row and pos_row["position_index"] is not None else None

    scan_limit = min(1200, max(200, int(limit) * 6))
    sql = """
      SELECT id, chapter_index, chapter_title, position_index, spine_href, anchor_text, canonical_start, canonical_end, cfi_range, text
      FROM chunks
      WHERE book_id = ?
    """
    params = [book_id]
    if max_position is not None:
      sql += " AND position_index <= ?"
      params.append(int(max_position))

    sql += " AND text LIKE ? COLLATE NOCASE ORDER BY position_index LIMIT ?"
    params.extend([f"%{query}%", int(scan_limit)])
    rows = conn.execute(sql, params).fetchall()

    q_lower = query.lower()
    matches = []
    query_canonical_len = max(1, _canonical_alnum_len(query))
    for row in rows:
      text = row["text"] or ""
      text_lower = text.lower()
      cursor = 0
      while len(matches) < int(limit):
        idx = text_lower.find(q_lower, cursor)
        if idx < 0:
          break

        match_raw = text[idx: idx + len(query)]
        snippet_start = max(0, idx - 120)
        snippet_end = min(len(text), idx + len(query) + 180)
        snippet = " ".join(text[snippet_start:snippet_end].split())

        chunk_canonical_start = int(row["canonical_start"] or 0)
        prefix_canonical_len = _canonical_alnum_len(text[:idx])
        match_canonical_len = _canonical_alnum_len(match_raw) or query_canonical_len
        match_canonical_start = chunk_canonical_start + prefix_canonical_len
        match_canonical_end = match_canonical_start + max(1, match_canonical_len)

        matches.append(
          {
            "chunk_id": row["id"],
            "chapter_index": row["chapter_index"],
            "chapter_title": row["chapter_title"],
            "position_index": row["position_index"],
            "spine_href": row["spine_href"],
            "canonical_start": row["canonical_start"],
            "canonical_end": row["canonical_end"],
            "match_offset_start": idx,
            "match_offset_end": idx + len(query),
            "match_canonical_start": match_canonical_start,
            "match_canonical_end": match_canonical_end,
            "match_text": match_raw,
            "cfi_range": row["cfi_range"],
            "anchor_text": row["anchor_text"],
            "snippet": snippet,
            "debug_chunk_text": text,
          }
        )

        cursor = idx + max(1, len(query))

      if len(matches) >= int(limit):
        break

    return {"query": query, "matches": matches}
  finally:
    conn.close()


@app.delete("/api/books/{book_id}")
def delete_book(book_id: int):
  conn = connect()
  try:
    row = _book_row_or_404(conn, book_id)

    cover_path = row["cover_path"]
    epub_path = row["epub_path"]

    conn.execute("DELETE FROM conversations WHERE book_id = ?", (book_id,))
    conn.execute("DELETE FROM bookmarks WHERE book_id = ?", (book_id,))
    conn.execute("DELETE FROM book_positions WHERE book_id = ?", (book_id,))
    conn.execute("DELETE FROM chapters WHERE book_id = ?", (book_id,))
    conn.execute("DELETE FROM chunks WHERE book_id = ?", (book_id,))

    try:
      conn.execute("DELETE FROM vec_chunks WHERE book_id = ?", (book_id,))
    except Exception:
      pass

    conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    conn.commit()
  finally:
    conn.close()

  for path in [cover_path, epub_path]:
    if not path:
      continue
    file_path = Path(path)
    if file_path.exists():
      try:
        file_path.unlink()
      except Exception:
        pass

  return {"ok": True}
