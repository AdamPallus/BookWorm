import re
from typing import List, Dict, Tuple
from bs4 import BeautifulSoup
from ebooklib import epub
import tiktoken

TOKEN_TARGET = 800


def extract_chapters(epub_path: str) -> List[Dict]:
  book = epub.read_epub(epub_path)
  chapters = []
  chapter_index = 0

  for item in book.get_items():
    if item.get_type() != epub.ITEM_DOCUMENT:
      continue

    soup = BeautifulSoup(item.get_content(), "html.parser")
    text = soup.get_text("\n")
    text = clean_text(text)
    if not text.strip():
      continue

    title = None
    # try to find first heading
    heading = soup.find(["h1", "h2", "h3"])
    if heading and heading.get_text(strip=True):
      title = heading.get_text(strip=True)

    chapters.append({
      "chapter_index": chapter_index,
      "title": title or f"Chapter {chapter_index + 1}",
      "text": text,
    })
    chapter_index += 1

  metadata_title = book.get_metadata('DC', 'title')
  metadata_author = book.get_metadata('DC', 'creator')
  title = metadata_title[0][0] if metadata_title else "Untitled"
  author = metadata_author[0][0] if metadata_author else "Unknown"

  return title, author, chapters


def clean_text(text: str) -> str:
  text = re.sub(r"\r", "", text)
  text = re.sub(r"\n{3,}", "\n\n", text)
  return text.strip()


def chunk_chapter(text: str, chapter_index: int, chapter_title: str, start_position: int) -> Tuple[List[Dict], int]:
  enc = tiktoken.get_encoding("cl100k_base")
  paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

  chunks = []
  current = []
  current_tokens = 0
  position_index = start_position

  for para in paragraphs:
    tokens = len(enc.encode(para))
    if current and current_tokens + tokens > TOKEN_TARGET:
      chunk_text = "\n\n".join(current)
      chunks.append({
        "chapter_index": chapter_index,
        "chapter_title": chapter_title,
        "position_index": position_index,
        "text": chunk_text,
      })
      position_index += 1
      current = []
      current_tokens = 0

    current.append(para)
    current_tokens += tokens

  if current:
    chunk_text = "\n\n".join(current)
    chunks.append({
      "chapter_index": chapter_index,
      "chapter_title": chapter_title,
      "position_index": position_index,
      "text": chunk_text,
    })
    position_index += 1

  return chunks, position_index
