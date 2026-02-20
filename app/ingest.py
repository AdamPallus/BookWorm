import re
from typing import List, Dict, Tuple, Optional

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub

try:
  import tiktoken
  _enc = tiktoken.get_encoding("cl100k_base")

  def _count_tokens(text: str) -> int:
    return len(_enc.encode(text))
except ImportError:
  def _count_tokens(text: str) -> int:
    return max(1, len(text) // 4)

TOKEN_TARGET = 800
MAX_CHUNK_TOKENS = 1200


def _guess_ext(media_type: Optional[str], fallback: str = ".jpg") -> str:
  if not media_type:
    return fallback
  if "png" in media_type:
    return ".png"
  if "webp" in media_type:
    return ".webp"
  if "gif" in media_type:
    return ".gif"
  return ".jpg"


def _extract_cover(book: epub.EpubBook) -> Tuple[Optional[bytes], Optional[str]]:
  meta_cover = book.get_metadata("OPF", "cover")
  if meta_cover:
    cover_id = meta_cover[0][0]
    item = book.get_item_with_id(cover_id)
    if item is not None:
      return item.get_content(), _guess_ext(getattr(item, "media_type", None))

  cover_items = list(book.get_items_of_type(ebooklib.ITEM_COVER))
  if cover_items:
    item = cover_items[0]
    return item.get_content(), _guess_ext(getattr(item, "media_type", None))

  image_items = list(book.get_items_of_type(ebooklib.ITEM_IMAGE))
  for item in image_items:
    name = (item.get_name() or "").lower()
    if "cover" in name:
      return item.get_content(), _guess_ext(getattr(item, "media_type", None))

  return None, None


def clean_text(text: str) -> str:
  text = re.sub(r"\r", "", text)
  text = re.sub(r"\n{3,}", "\n\n", text)
  return text.strip()


def _split_text_by_token_limit(text: str, token_limit: int) -> List[str]:
  raw = (text or "").strip()
  if not raw:
    return []

  if _count_tokens(raw) <= token_limit:
    return [raw]

  chunks: List[str] = []
  remaining = raw
  while remaining:
    est_tokens = _count_tokens(remaining)
    if est_tokens <= token_limit:
      chunks.append(remaining.strip())
      break

    # Approximate cut location by chars/token ratio, then snap to whitespace.
    approx_cut = max(120, int(len(remaining) * (token_limit / max(1, est_tokens))))
    cut = approx_cut
    while cut > 80 and not remaining[cut - 1].isspace():
      cut -= 1
    if cut <= 80:
      cut = approx_cut

    piece = remaining[:cut].strip()
    if not piece:
      # Ensure progress even on pathological inputs.
      piece = remaining[:max(100, approx_cut)].strip()
      cut = len(piece)

    chunks.append(piece)
    remaining = remaining[cut:].strip()

  return [c for c in chunks if c]


def extract_book(epub_path: str):
  book = epub.read_epub(epub_path)

  chapters: List[Dict] = []
  chapter_index = 0
  for item_id, _linear in book.spine:
    if item_id == "nav":
      continue

    item = book.get_item_with_id(item_id)
    if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
      continue

    soup = BeautifulSoup(item.get_content(), "html.parser")
    text = clean_text(soup.get_text("\n"))
    if not text:
      continue

    heading = soup.find(["h1", "h2", "h3"])
    title = heading.get_text(strip=True) if heading and heading.get_text(strip=True) else None

    chapters.append({
      "chapter_index": chapter_index,
      "title": title or f"Chapter {chapter_index + 1}",
      "spine_href": item.get_name(),
      "text": text,
    })
    chapter_index += 1

  metadata_title = book.get_metadata("DC", "title")
  metadata_author = book.get_metadata("DC", "creator")
  title = metadata_title[0][0] if metadata_title else "Untitled"
  author = metadata_author[0][0] if metadata_author else "Unknown"

  cover_bytes, cover_ext = _extract_cover(book)
  return title, author, chapters, cover_bytes, cover_ext


def chunk_chapter(
  text: str,
  chapter_index: int,
  chapter_title: str,
  start_position: int,
  spine_href: Optional[str],
) -> Tuple[List[Dict], int]:
  raw_paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
  paragraphs: List[str] = []
  paragraph_canonical_lens: List[int] = []
  for para in raw_paragraphs:
    pieces = _split_text_by_token_limit(para, MAX_CHUNK_TOKENS)
    for piece in pieces:
      paragraphs.append(piece)
      paragraph_canonical_lens.append(len(re.sub(r"[^a-z0-9]+", "", piece.lower())))

  chunks = []
  current = []
  current_tokens = 0
  current_canonical_len = 0
  chapter_canonical_cursor = 0
  position_index = start_position

  def _flush_current(cur_parts: List[str], pos: int, canonical_len: int):
    nonlocal chapter_canonical_cursor
    chunk_text = "\n\n".join(cur_parts)
    anchor = re.sub(r"\s+", " ", chunk_text).strip()[:80]
    canonical_start = chapter_canonical_cursor
    canonical_end = chapter_canonical_cursor + max(0, canonical_len)
    chapter_canonical_cursor = canonical_end
    return {
      "chapter_index": chapter_index,
      "chapter_title": chapter_title,
      "position_index": pos,
      "spine_href": spine_href,
      "anchor_text": anchor,
      "canonical_start": canonical_start,
      "canonical_end": canonical_end,
      "text": chunk_text,
    }

  for idx, para in enumerate(paragraphs):
    tokens = _count_tokens(para)
    canonical_len = paragraph_canonical_lens[idx]
    if current and current_tokens + tokens > TOKEN_TARGET:
      chunks.append(_flush_current(current, position_index, current_canonical_len))
      position_index += 1
      current = []
      current_tokens = 0
      current_canonical_len = 0

    current.append(para)
    current_tokens += tokens
    current_canonical_len += canonical_len

  if current:
    chunks.append(_flush_current(current, position_index, current_canonical_len))
    position_index += 1

  return chunks, position_index
