import json
import re
import shutil
from pathlib import Path
from typing import Any, List, Optional, Tuple

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from .db import COVER_DIR, EPUB_DIR, connect, get_setting, set_setting
from .ingest import chunk_chapter, extract_book
from .rag import embed_texts, get_api_key, set_api_key, stream_answer
from .wiki import ensure_wiki_artifacts, ensure_wiki_ingested, get_book_wiki_state, load_query_wiki_context, resolve_default_wiki_view

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

ALLOWED_MODELS = ["gpt-5.4-mini", "gpt-5.4"]
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_FONT_SIZE = 100
ALLOWED_READER_SPREADS = ["single", "double"]
DEFAULT_READER_SPREAD = "single"
ALLOWED_READER_MODES = ["auto", "paginated", "scroll"]
DEFAULT_READER_MODE = "auto"
DEFAULT_READER_WIDTH_PX = 980
MIN_READER_WIDTH_PX = 760
MAX_READER_WIDTH_PX = 1320
DEFAULT_READER_BOTTOM_PADDING_PX = 34
MIN_READER_BOTTOM_PADDING_PX = 12
MAX_READER_BOTTOM_PADDING_PX = 120
DEFAULT_CITATION_DEBUG_MODE = False
ALLOWED_VIRTUAL_CHAPTER_DETECTION_MODES = ["strict", "plain"]
DEFAULT_VIRTUAL_CHAPTER_DETECTION_MODE = "strict"
ALLOWED_TOC_DISPLAY_MODES = ["merged", "separate"]
DEFAULT_TOC_DISPLAY_MODE = "merged"
CANONICAL_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


class PositionUpdate(BaseModel):
  chapter_index: Optional[int] = None
  chapter_percent: Optional[float] = None
  book_percent: Optional[float] = None
  percent: Optional[float] = None
  char_offset: Optional[int] = None
  cfi: Optional[str] = None


class QueryRequest(BaseModel):
  question: str
  position_index: Optional[int] = None
  model: Optional[str] = None
  ask_cfi: Optional[str] = None
  ask_chapter_index: Optional[int] = None
  ask_chapter_percent: Optional[float] = None
  ask_book_percent: Optional[float] = None
  ask_char_offset: Optional[int] = None


class SettingsUpdate(BaseModel):
  api_key: Optional[str] = None
  model: Optional[str] = None
  reader_font_size: Optional[int] = None
  reader_spread: Optional[str] = None
  reader_mode: Optional[str] = None
  reader_width_px: Optional[int] = None
  reader_bottom_padding_px: Optional[int] = None
  citation_debug_mode: Optional[bool] = None


class VirtualChapterDetectRequest(BaseModel):
  pattern: str
  enable: Optional[bool] = True
  detection_mode: Optional[str] = None


class VirtualChapterSettingsUpdate(BaseModel):
  enabled: Optional[bool] = None
  detection_mode: Optional[str] = None
  toc_display_mode: Optional[str] = None


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
  chapter_href: Optional[str] = None
  anchor_canonical_offset: Optional[int] = None
  anchor_text: Optional[str] = None
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


def _current_reader_mode(conn) -> str:
  raw = get_setting(conn, "reader_mode", DEFAULT_READER_MODE)
  return raw if raw in ALLOWED_READER_MODES else DEFAULT_READER_MODE


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


def _settings_payload(conn) -> dict:
  key = get_api_key()
  return {
    "api_key": key,
    "has_key": key is not None,
    "model": _current_model(conn),
    "models": ALLOWED_MODELS,
    "reader_font_size": _current_font_size(conn),
    "reader_spread": _current_reader_spread(conn),
    "reader_spread_options": ALLOWED_READER_SPREADS,
    "reader_mode": _current_reader_mode(conn),
    "reader_mode_options": ALLOWED_READER_MODES,
    "reader_width_px": _current_reader_width_px(conn),
    "reader_width_px_min": MIN_READER_WIDTH_PX,
    "reader_width_px_max": MAX_READER_WIDTH_PX,
    "reader_bottom_padding_px": _current_reader_bottom_padding_px(conn),
    "reader_bottom_padding_px_min": MIN_READER_BOTTOM_PADDING_PX,
    "reader_bottom_padding_px_max": MAX_READER_BOTTOM_PADDING_PX,
    "citation_debug_mode": _current_citation_debug_mode(conn),
  }


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
  ask_char_offset: Optional[int] = None,
  retrieval_context: Optional[dict] = None,
):
  conn.execute(
    """
    INSERT INTO conversations
      (book_id, question, answer, model, position_context, ask_cfi, ask_chapter_index, ask_chapter_percent, ask_book_percent, ask_char_offset, sources_json, retrieval_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
      ask_char_offset,
      json.dumps(sources, ensure_ascii=False),
      json.dumps(retrieval_context, ensure_ascii=False) if retrieval_context is not None else None,
    ),
  )
  conn.commit()


def _canonical_alnum_len(text: str) -> int:
  return len(CANONICAL_NON_ALNUM_RE.sub("", (text or "").lower()))


def _normalize_href(href: Optional[str]) -> Optional[str]:
  value = (href or "").strip()
  if not value:
    return None
  return value.split("#")[0].strip() or None


def _chapter_href(conn, book_id: int, chapter_index: Optional[int]) -> Optional[str]:
  if chapter_index is None:
    return None
  row = conn.execute(
    "SELECT spine_href FROM chapters WHERE book_id = ? AND chapter_index = ?",
    (book_id, chapter_index),
  ).fetchone()
  if not row:
    return None
  return _normalize_href(row["spine_href"])


def _chapter_anchor_from_percent(
  conn,
  book_id: int,
  chapter_index: Optional[int],
  chapter_percent: Optional[float],
) -> Tuple[Optional[int], Optional[str]]:
  if chapter_index is None or chapter_percent is None:
    return None, None

  max_row = conn.execute(
    "SELECT MAX(canonical_end) AS max_end FROM chunks WHERE book_id = ? AND chapter_index = ?",
    (book_id, chapter_index),
  ).fetchone()
  max_end = int(max_row["max_end"] or 0) if max_row else 0
  if max_end <= 0:
    return None, None

  pct = max(0.0, min(100.0, float(chapter_percent)))
  candidate = int(round((pct / 100.0) * max_end))
  offset = max(0, min(max_end - 1, candidate))

  chunk_row = conn.execute(
    """
    SELECT anchor_text
    FROM chunks
    WHERE book_id = ? AND chapter_index = ?
      AND canonical_start <= ? AND canonical_end > ?
    ORDER BY position_index ASC, id ASC
    LIMIT 1
    """,
    (book_id, chapter_index, offset, offset),
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
      (book_id, chapter_index, offset),
    ).fetchone()
  anchor_text = chunk_row["anchor_text"] if chunk_row and chunk_row["anchor_text"] else None
  anchor_text = anchor_text[:240] if anchor_text else None
  return offset, anchor_text


def _normalize_virtual_chapter_detection_mode(value: Optional[str]) -> str:
  raw = str(value or DEFAULT_VIRTUAL_CHAPTER_DETECTION_MODE).strip().lower()
  if raw not in ALLOWED_VIRTUAL_CHAPTER_DETECTION_MODES:
    raise HTTPException(status_code=400, detail=f"Unsupported detection_mode '{value}'")
  return raw


def _normalize_toc_display_mode(value: Optional[str]) -> str:
  raw = str(value or DEFAULT_TOC_DISPLAY_MODE).strip().lower()
  if raw not in ALLOWED_TOC_DISPLAY_MODES:
    raise HTTPException(status_code=400, detail=f"Unsupported toc_display_mode '{value}'")
  return raw


def _virtual_chapter_state(conn, book_id: int) -> dict:
  row = conn.execute(
    """
    SELECT enabled, pattern, entries_json, detection_mode, toc_display_mode
    FROM virtual_chapter_settings
    WHERE book_id = ?
    """,
    (book_id,),
  ).fetchone()
  if not row:
    return {
      "enabled": False,
      "pattern": None,
      "entries": [],
      "detection_mode": DEFAULT_VIRTUAL_CHAPTER_DETECTION_MODE,
      "toc_display_mode": DEFAULT_TOC_DISPLAY_MODE,
    }
  try:
    entries = json.loads(row["entries_json"]) if row["entries_json"] else []
  except Exception:
    entries = []
  return {
    "enabled": bool(row["enabled"]),
    "pattern": row["pattern"],
    "entries": entries if isinstance(entries, list) else [],
    "detection_mode": _normalize_virtual_chapter_detection_mode(row["detection_mode"]),
    "toc_display_mode": _normalize_toc_display_mode(row["toc_display_mode"]),
  }


def _save_virtual_chapter_state(conn, book_id: int, state: dict) -> dict:
  payload = {
    "enabled": bool(state.get("enabled")),
    "pattern": (state.get("pattern") or None),
    "entries": state.get("entries") if isinstance(state.get("entries"), list) else [],
    "detection_mode": _normalize_virtual_chapter_detection_mode(state.get("detection_mode")),
    "toc_display_mode": _normalize_toc_display_mode(state.get("toc_display_mode")),
  }
  conn.execute(
    """
    INSERT INTO virtual_chapter_settings
      (book_id, enabled, pattern, entries_json, detection_mode, toc_display_mode, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(book_id) DO UPDATE SET
      enabled = excluded.enabled,
      pattern = excluded.pattern,
      entries_json = excluded.entries_json,
      detection_mode = excluded.detection_mode,
      toc_display_mode = excluded.toc_display_mode,
      updated_at = CURRENT_TIMESTAMP
    """,
    (
      book_id,
      1 if payload["enabled"] else 0,
      payload["pattern"],
      json.dumps(payload["entries"], ensure_ascii=False),
      payload["detection_mode"],
      payload["toc_display_mode"],
    ),
  )
  conn.commit()
  return payload


def _compact_whitespace(text: str) -> str:
  return re.sub(r"\s+", " ", (text or "")).strip()


def _chapter_text_map(conn, book_id: int) -> List[dict]:
  chapter_rows = conn.execute(
    "SELECT chapter_index, title FROM chapters WHERE book_id = ? ORDER BY chapter_index",
    (book_id,),
  ).fetchall()
  chunk_rows = conn.execute(
    """
    SELECT chapter_index, text
    FROM chunks
    WHERE book_id = ?
    ORDER BY chapter_index, position_index, id
    """,
    (book_id,),
  ).fetchall()
  chapter_texts = {}
  for row in chunk_rows:
    chapter_texts.setdefault(int(row["chapter_index"]), []).append(row["text"] or "")
  return [
    {
      "chapter_index": int(ch["chapter_index"]),
      "title": ch["title"],
      "text": "\n\n".join(chapter_texts.get(int(ch["chapter_index"]), [])),
    }
    for ch in chapter_rows
  ]


def _position_chapter_index(
  conn,
  book_id: int,
  position_index: int,
  fallback: Optional[int] = None,
) -> Optional[int]:
  if fallback is not None:
    return int(fallback)
  row = conn.execute(
    """
    SELECT chapter_index
    FROM chunks
    WHERE book_id = ? AND position_index <= ?
    ORDER BY position_index DESC, id DESC
    LIMIT 1
    """,
    (book_id, position_index),
  ).fetchone()
  if not row:
    return None
  return int(row["chapter_index"])


def _fetch_chunks_by_ids(conn, chunk_ids: List[int]) -> List[dict]:
  if not chunk_ids:
    return []
  placeholders = ",".join(["?"] * len(chunk_ids))
  rows = conn.execute(
    f"SELECT * FROM chunks WHERE id IN ({placeholders})",
    chunk_ids,
  ).fetchall()
  by_id = {row["id"]: dict(row) for row in rows}
  return [by_id[chunk_id] for chunk_id in chunk_ids if chunk_id in by_id]


def _current_chapter_excerpt_rows(
  conn,
  book_id: int,
  chapter_index: Optional[int],
  position_index: int,
  limit: int = 4,
) -> List[dict]:
  if chapter_index is None:
    return []
  rows = conn.execute(
    """
    SELECT *
    FROM chunks
    WHERE book_id = ? AND chapter_index = ? AND position_index <= ?
    ORDER BY position_index DESC, id DESC
    LIMIT ?
    """,
    (book_id, chapter_index, position_index, int(limit)),
  ).fetchall()
  ordered = [dict(row) for row in rows]
  ordered.reverse()
  return ordered


def _keyword_excerpt_rows(
  conn,
  book_id: int,
  position_index: int,
  search_terms: List[str],
  current_chapter_index: Optional[int],
  limit: int = 6,
) -> List[dict]:
  if not search_terms:
    return []

  scored = {}
  seen_terms = set()
  for raw_term in search_terms[:10]:
    term = re.sub(r"\s+", " ", (raw_term or "")).strip().lower()
    if len(term) < 4 or term in seen_terms:
      continue
    seen_terms.add(term)

    rows = conn.execute(
      """
      SELECT *
      FROM chunks
      WHERE book_id = ? AND position_index <= ? AND LOWER(text) LIKE ?
      ORDER BY position_index DESC, id DESC
      LIMIT 6
      """,
      (book_id, position_index, f"%{term}%"),
    ).fetchall()

    for row in rows:
      chunk = dict(row)
      text = (chunk.get("text") or "").lower()
      hits = text.count(term)
      if hits <= 0:
        continue
      entry = scored.setdefault(
        chunk["id"],
        {
          "chunk": chunk,
          "score": 0.0,
        },
      )
      entry["score"] += hits * 7
      if current_chapter_index is not None and chunk["chapter_index"] == current_chapter_index:
        entry["score"] += 4
      entry["score"] += max(0.0, 2.0 - ((position_index - chunk["position_index"]) / 24.0))

  ranked = sorted(
    scored.values(),
    key=lambda entry: (entry["score"], entry["chunk"]["position_index"]),
    reverse=True,
  )
  return [entry["chunk"] for entry in ranked[:limit]]


def _merge_excerpt_rows(
  vector_rows: List[dict],
  local_rows: List[dict],
  keyword_rows: List[dict],
  position_index: int,
  current_chapter_index: Optional[int],
  limit: int = 12,
) -> List[dict]:
  merged = []
  seen = set()

  for row in local_rows:
    chunk_id = row["id"]
    if chunk_id in seen:
      continue
    seen.add(chunk_id)
    merged.append(dict(row))

  vector_rank = {row["id"]: idx for idx, row in enumerate(vector_rows)}
  keyword_rank = {row["id"]: idx for idx, row in enumerate(keyword_rows)}
  candidates = {}
  for row in vector_rows + keyword_rows:
    chunk_id = row["id"]
    if chunk_id in seen:
      continue
    candidates[chunk_id] = dict(row)

  ranked = sorted(
    candidates.values(),
    key=lambda row: (
      1 if current_chapter_index is not None and row["chapter_index"] == current_chapter_index else 0,
      -(vector_rank.get(row["id"], 999)),
      -(keyword_rank.get(row["id"], 999)),
      -abs(position_index - row["position_index"]),
      row["position_index"],
    ),
    reverse=True,
  )

  for row in ranked:
    if len(merged) >= limit:
      break
    seen.add(row["id"])
    merged.append(row)

  return merged


def _iter_virtual_chapter_matches(text: str, pattern: str, detection_mode: str):
  source = text or ""
  marker = (pattern or "").strip()
  if not source or not marker:
    return

  if detection_mode == "plain":
    regex = re.compile(re.escape(marker), re.IGNORECASE)
    last_end = -10**9
    for match in regex.finditer(source):
      if match.start() - last_end < 120:
        continue
      line_start = source.rfind("\n", 0, match.start()) + 1
      line_end = source.find("\n", match.start())
      if line_end < 0:
        line_end = len(source)
      label = _compact_whitespace(source[line_start:line_end])[:80]
      snippet = _compact_whitespace(source[max(0, match.start() - 100): min(len(source), match.end() + 180)])[:240]
      if not label:
        label = _compact_whitespace(source[match.start(): min(len(source), match.start() + 80)])[:80]
      if not label:
        continue
      last_end = match.end()
      yield {"char_offset": match.start(), "label": label, "snippet": snippet}
    return

  base_marker = marker.replace("<number>", "").strip() or marker
  require_suffix = "<number>" in marker
  header_re = re.compile(
    rf"(?im)^[ \t]*({re.escape(base_marker)}(?:[ \t]+[A-Za-z0-9][A-Za-z0-9'’.:-]*){{{1 if require_suffix else 0},6}})[ \t]*$"
  )
  for match in header_re.finditer(source):
    label = _compact_whitespace(match.group(1))[:80]
    if not label:
      continue
    if len(label.split()) > 8:
      continue
    snippet = _compact_whitespace(source[max(0, match.start() - 100): min(len(source), match.end() + 180)])[:240]
    yield {"char_offset": match.start(1), "label": label, "snippet": snippet}


def _detect_virtual_chapter_entries(conn, book_id: int, pattern: str, detection_mode: str) -> List[dict]:
  entries = []
  for chapter in _chapter_text_map(conn, book_id):
    seen_offsets = set()
    for match in _iter_virtual_chapter_matches(chapter["text"], pattern, detection_mode):
      char_offset = max(0, int(match["char_offset"]))
      if char_offset in seen_offsets:
        continue
      seen_offsets.add(char_offset)
      entries.append(
        {
          "id": f"v-{chapter['chapter_index']}-{char_offset}",
          "chapter_index": chapter["chapter_index"],
          "char_offset": char_offset,
          "label": match["label"],
          "snippet": match["snippet"],
          "virtual": True,
        }
      )
  return entries


@app.get("/", response_class=HTMLResponse)
def index():
  return (STATIC_DIR / "index.html").read_text()


@app.get("/read/{book_id}", response_class=HTMLResponse)
def read_page(book_id: int):
  return (STATIC_DIR / "index.html").read_text()


@app.get("/wiki/{book_id}")
def open_book_wiki(book_id: int):
  conn = connect()
  try:
    book = _book_row_or_404(conn, book_id)
    pos = conn.execute("SELECT chapter_index FROM book_positions WHERE book_id = ?", (book_id,)).fetchone()
    target = resolve_default_wiki_view(
      conn,
      book_id,
      book["title"],
      book["epub_path"],
      pos["chapter_index"] if pos and pos["chapter_index"] is not None else None,
    )
    if not target:
      raise HTTPException(status_code=404, detail="No spoiler-safe wiki checkpoint is available yet for this book")
    return RedirectResponse(target["url"], status_code=307)
  finally:
    conn.close()


@app.get("/wiki/{book_id}/viewer")
def open_book_wiki_viewer(book_id: int):
  conn = connect()
  try:
    book = _book_row_or_404(conn, book_id)
    bundle = ensure_wiki_artifacts(book["title"], book["epub_path"])
    if not bundle or not bundle.get("viewer_path"):
      raise HTTPException(status_code=404, detail="Wiki viewer is not available for this book")
    viewer_path = Path(bundle["viewer_path"])
    if not viewer_path.exists():
      raise HTTPException(status_code=404, detail="Wiki viewer file is missing")
    return FileResponse(viewer_path, media_type="text/html")
  finally:
    conn.close()


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
  try:
    return _settings_payload(conn)
  finally:
    conn.close()


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

    if payload.reader_mode is not None:
      if payload.reader_mode not in ALLOWED_READER_MODES:
        raise HTTPException(status_code=400, detail=f"Unsupported reader_mode '{payload.reader_mode}'")
      set_setting(conn, "reader_mode", payload.reader_mode)

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
    return _settings_payload(conn)
  finally:
    conn.close()


@app.get("/api/books")
def list_books():
  conn = connect()
  try:
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

    books = []
    for row in rows:
      book = dict(row)
      book["wiki"] = get_book_wiki_state(
        conn,
        book["id"],
        book.get("title"),
        book.get("epub_path"),
        book.get("chapter_index"),
      )
      books.append(book)
    return {"books": books}
  finally:
    conn.close()


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
      "virtual_chapters": _virtual_chapter_state(conn, book_id),
      "wiki": get_book_wiki_state(
        conn,
        book_id,
        book["title"],
        book["epub_path"],
        pos["chapter_index"] if pos and pos["chapter_index"] is not None else None,
      ),
    }
  finally:
    conn.close()


@app.post("/api/books/{book_id}/wiki/ingest")
def ingest_wiki(book_id: int):
  conn = connect()
  try:
    book = _book_row_or_404(conn, book_id)
    bundle = ensure_wiki_ingested(conn, book_id, book["title"], book["epub_path"])
    if not bundle:
      raise HTTPException(status_code=404, detail="No wiki found for this book")

    pos = conn.execute("SELECT chapter_index FROM book_positions WHERE book_id = ?", (book_id,)).fetchone()
    return {
      "ok": True,
      "wiki": get_book_wiki_state(
        conn,
        book_id,
        book["title"],
        book["epub_path"],
        pos["chapter_index"] if pos and pos["chapter_index"] is not None else None,
      ),
      "total_snapshots": len(bundle.get("snapshots") or []),
      "viewer_path": bundle.get("viewer_path"),
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


@app.post("/api/books/{book_id}/virtual-chapters/detect")
def detect_virtual_chapters(book_id: int, payload: VirtualChapterDetectRequest):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)
    pattern = _compact_whitespace(payload.pattern)
    if len(pattern) < 2:
      raise HTTPException(status_code=400, detail="Pattern must be at least 2 characters")
    detection_mode = _normalize_virtual_chapter_detection_mode(payload.detection_mode)
    current = _virtual_chapter_state(conn, book_id)
    next_state = {
      **current,
      "enabled": bool(payload.enable if payload.enable is not None else True),
      "pattern": pattern,
      "entries": _detect_virtual_chapter_entries(conn, book_id, pattern, detection_mode),
      "detection_mode": detection_mode,
    }
    return _save_virtual_chapter_state(conn, book_id, next_state)
  finally:
    conn.close()


@app.post("/api/books/{book_id}/virtual-chapters/settings")
def update_virtual_chapter_settings(book_id: int, payload: VirtualChapterSettingsUpdate):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)
    current = _virtual_chapter_state(conn, book_id)
    next_state = {
      **current,
      "enabled": current["enabled"] if payload.enabled is None else bool(payload.enabled),
      "detection_mode": current["detection_mode"] if payload.detection_mode is None else _normalize_virtual_chapter_detection_mode(payload.detection_mode),
      "toc_display_mode": current["toc_display_mode"] if payload.toc_display_mode is None else _normalize_toc_display_mode(payload.toc_display_mode),
    }
    return _save_virtual_chapter_state(conn, book_id, next_state)
  finally:
    conn.close()


@app.delete("/api/books/{book_id}/virtual-chapters")
def delete_virtual_chapters(book_id: int):
  conn = connect()
  try:
    _book_row_or_404(conn, book_id)
    conn.execute("DELETE FROM virtual_chapter_settings WHERE book_id = ?", (book_id,))
    conn.commit()
    return {"ok": True}
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
    char_offset = None
    if payload.char_offset is not None:
      char_offset = max(0, int(payload.char_offset))

    book_percent = payload.book_percent if payload.book_percent is not None else None
    if book_percent is not None:
      book_percent = max(0.0, min(100.0, float(book_percent)))

    conn.execute(
      """
      INSERT INTO book_positions (book_id, chapter_index, chapter_percent, book_percent, position_index, char_offset, cfi, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
      ON CONFLICT(book_id) DO UPDATE SET
        chapter_index = excluded.chapter_index,
        chapter_percent = excluded.chapter_percent,
        book_percent = excluded.book_percent,
        position_index = excluded.position_index,
        char_offset = excluded.char_offset,
        cfi = excluded.cfi,
        updated_at = CURRENT_TIMESTAMP
      """,
      (book_id, chapter_index, chapter_percent, book_percent, pos_index, char_offset, payload.cfi),
    )
    conn.commit()

    return {
      "book_id": book_id,
      "chapter_index": chapter_index,
      "chapter_percent": chapter_percent,
      "book_percent": book_percent,
      "position_index": pos_index,
      "char_offset": char_offset,
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
      raw_retrieval = entry.get("retrieval_json")
      try:
        entry["retrieval_context"] = json.loads(raw_retrieval) if raw_retrieval else None
      except Exception:
        entry["retrieval_context"] = None
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
      SELECT
        id,
        book_id,
        cfi,
        chapter_index,
        chapter_percent,
        book_percent,
        chapter_href,
        anchor_canonical_offset,
        anchor_text,
        label,
        created_at
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
    chapter_index = int(payload.chapter_index) if payload.chapter_index is not None else None
    book_percent = None
    if payload.book_percent is not None:
      book_percent = max(0.0, min(100.0, float(payload.book_percent)))
    chapter_href = _normalize_href(payload.chapter_href)
    if not chapter_href:
      chapter_href = _chapter_href(conn, book_id, chapter_index)
    anchor_offset = None
    if payload.anchor_canonical_offset is not None:
      anchor_offset = max(0, int(payload.anchor_canonical_offset))
    anchor_text = (payload.anchor_text or "").strip() or None
    if anchor_text:
      anchor_text = anchor_text[:240]

    derived_offset = None
    derived_text = None
    if anchor_offset is None:
      derived_offset, derived_text = _chapter_anchor_from_percent(conn, book_id, chapter_index, chapter_percent)
      anchor_offset = derived_offset
    if not anchor_text and derived_text:
      anchor_text = derived_text

    conn.execute(
      """
      INSERT INTO bookmarks (
        book_id,
        cfi,
        chapter_index,
        chapter_percent,
        book_percent,
        chapter_href,
        anchor_canonical_offset,
        anchor_text,
        label
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        int(book_id),
        cfi,
        chapter_index,
        chapter_percent,
        book_percent,
        chapter_href,
        anchor_offset,
        anchor_text,
        payload.label.strip() if payload.label else None,
      ),
    )
    row = conn.execute(
      """
      SELECT
        id,
        book_id,
        cfi,
        chapter_index,
        chapter_percent,
        book_percent,
        chapter_href,
        anchor_canonical_offset,
        anchor_text,
        label,
        created_at
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
  book = _book_row_or_404(conn, book_id)

  question = payload.question.strip()
  if not question:
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
  ask_char_offset = None
  if payload.ask_char_offset is not None:
    ask_char_offset = max(0, int(payload.ask_char_offset))
  if pos_row:
    if ask_cfi is None:
      ask_cfi = pos_row["cfi"]
    if ask_chapter_index is None:
      ask_chapter_index = pos_row["chapter_index"]
    if ask_chapter_percent is None and pos_row["chapter_percent"] is not None:
      ask_chapter_percent = max(0.0, min(100.0, float(pos_row["chapter_percent"])))
    if ask_book_percent is None and pos_row["book_percent"] is not None:
      ask_book_percent = max(0.0, min(100.0, float(pos_row["book_percent"])))
    if ask_char_offset is None and pos_row["char_offset"] is not None:
      ask_char_offset = max(0, int(pos_row["char_offset"]))

  model = _validate_model(payload.model) if payload.model else _current_model(conn)
  citation_debug_mode = _current_citation_debug_mode(conn)
  current_chapter_index = _position_chapter_index(conn, book_id, position_index, fallback=ask_chapter_index)

  try:
    base_question_embedding = embed_texts([question])[0]
  except Exception as exc:
    raise HTTPException(status_code=500, detail=f"Vector search unavailable: {exc}")

  try:
    wiki_context = load_query_wiki_context(
      conn,
      book_id,
      book["title"],
      book["epub_path"],
      current_chapter_index,
      question,
      question_embedding=base_question_embedding,
    )
  except Exception:
    wiki_context = None

  expanded_question = question
  if wiki_context and wiki_context.get("search_terms"):
    expanded_question = (
      f"{expanded_question}\n\nRelevant wiki terms: "
      + ", ".join(wiki_context["search_terms"][:8])
    )

  def _line(obj):
    return json.dumps(obj, ensure_ascii=False) + "\n"

  try:
    query_embedding = base_question_embedding if expanded_question == question else embed_texts([expanded_question])[0]
    query_vec = _serialize_vec(query_embedding)
    rows = conn.execute(
      "SELECT chunk_id, distance FROM vec_chunks WHERE book_id = ? AND position_index <= ? AND embedding MATCH ? ORDER BY distance LIMIT 12",
      (book_id, position_index, query_vec),
    ).fetchall()
  except Exception as exc:
    raise HTTPException(status_code=500, detail=f"Vector search unavailable: {exc}")

  chunk_ids = [r["chunk_id"] for r in rows]
  vector_rows = _fetch_chunks_by_ids(conn, chunk_ids)
  local_rows = _current_chapter_excerpt_rows(conn, book_id, current_chapter_index, position_index, limit=4)
  keyword_rows = _keyword_excerpt_rows(
    conn,
    book_id,
    position_index,
    (wiki_context or {}).get("search_terms") or [],
    current_chapter_index,
    limit=6,
  )
  excerpts = _merge_excerpt_rows(
    vector_rows,
    local_rows,
    keyword_rows,
    position_index,
    current_chapter_index,
    limit=12,
  )

  chunk_sources = [
    {
      "source_type": "chunk",
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
  wiki_sources = list((wiki_context or {}).get("sources") or [])
  sources = chunk_sources + wiki_sources
  retrieval_context = {
    "used_wiki": bool(wiki_context and wiki_context.get("context")),
    "excerpt_count": len(chunk_sources),
    "wiki_source_count": len(wiki_sources),
    "wiki": (
      {
        "tag": wiki_context.get("tag"),
        "story_label": wiki_context.get("story_label"),
        "pages": wiki_context.get("pages"),
      }
      if wiki_context
      else None
    ),
  }

  def stream():
    try:
      yield _line({"type": "status", "stage": "retrieval_complete", "data": retrieval_context})
      has_wiki_context = bool(wiki_context and wiki_context.get("context"))
      if not excerpts and not has_wiki_context:
        answer = "I don't have enough information from the text you've read so far."
        _save_conversation(
          conn,
          book_id,
          question,
          answer,
          model,
          position_index,
          [],
          ask_cfi=ask_cfi,
          ask_chapter_index=ask_chapter_index,
          ask_chapter_percent=ask_chapter_percent,
          ask_book_percent=ask_book_percent,
          ask_char_offset=ask_char_offset,
          retrieval_context=retrieval_context,
        )
        yield _line(
          {
            "type": "done",
            "data": {
              "answer": answer,
              "sources": [],
              "wiki": retrieval_context["wiki"],
              "retrieval_context": retrieval_context,
            },
          }
        )
        return

      answer_parts = []
      for delta in stream_answer(
        question,
        excerpts,
        model,
        wiki_context=(wiki_context or {}).get("context"),
      ):
        answer_parts.append(delta)
        yield _line({"type": "delta", "delta": delta})

      answer = "".join(answer_parts).strip()
      if not answer:
        answer = "I don't have enough information from the text you've read so far."

      _save_conversation(
        conn,
        book_id,
        question,
        answer,
        model,
        position_index,
        sources,
        ask_cfi=ask_cfi,
        ask_chapter_index=ask_chapter_index,
        ask_chapter_percent=ask_chapter_percent,
        ask_book_percent=ask_book_percent,
        ask_char_offset=ask_char_offset,
        retrieval_context=retrieval_context,
      )
      yield _line(
        {
          "type": "done",
          "data": {
            "answer": answer,
            "sources": sources,
            "wiki": retrieval_context["wiki"],
            "retrieval_context": retrieval_context,
          },
        }
      )
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
    conn.execute("DELETE FROM virtual_chapter_settings WHERE book_id = ?", (book_id,))
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
