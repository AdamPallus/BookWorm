import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

from .rag import embed_texts

try:
  import sqlite_vec
except Exception:
  sqlite_vec = None


WIKI_ROOT = Path(__file__).resolve().parent.parent / "data" / "wikis"
SKILL_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "bookworm-wiki-builder" / "scripts"
SNAPSHOT_EXPORT_SCRIPT = SKILL_SCRIPTS_DIR / "snapshot_export.py"
VIEWER_SCRIPT = SKILL_SCRIPTS_DIR / "wiki_viewer_multi.py"
SNAPSHOT_FILE = "snapshots.json"
VIEWER_FILE = "wiki-viewer.html"
METADATA_FILE = "raw/metadata.json"

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
INDEX_LABEL_RE = re.compile(r"knowledge through \*\*(.*?)\*\*", re.IGNORECASE)
TAG_ORDER_RE = re.compile(r"(?:(?:b|book)(\d+)-)?ch-(\d+)$", re.IGNORECASE)
LEADING_NUMBER_RE = re.compile(r"^\s*(\d{1,3})\s+([^\n]{1,120})")
CHAPTER_NUMBER_RE = re.compile(r"\bchapter\s+(\d{1,3})\b", re.IGNORECASE)
ALIAS_ITEM_RE = re.compile(r'"([^"]+)"|\'([^\']+)\'|([^,\[\]]+)')
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

SECTION_MAX_CHARS = 1100
SECTION_OVERLAP_CHARS = 180
WIKI_CONTEXT_CHAR_BUDGET = 5200

STOPWORDS = {
  "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from", "going", "happen",
  "happens", "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "of", "on", "or", "our",
  "so", "that", "the", "their", "them", "there", "these", "they", "this", "to", "up", "was", "what",
  "when", "where", "which", "who", "why", "with", "would", "you", "your",
}
GENERIC_HEADINGS = {
  "summary",
  "what we know so far",
  "current understanding",
  "open questions",
  "relationships",
  "references",
  "chapter appearances",
}

_bundle_cache: Dict[str, dict] = {}


def _serialize_vec(embedding):
  if not sqlite_vec:
    return embedding
  if hasattr(sqlite_vec, "serialize"):
    return sqlite_vec.serialize(embedding)
  if hasattr(sqlite_vec, "serialize_float32"):
    return sqlite_vec.serialize_float32(embedding)
  return embedding


def _normalize(text: Optional[str]) -> str:
  return NON_ALNUM_RE.sub(" ", (text or "").lower()).strip()


def _slugify(text: Optional[str]) -> str:
  return NON_ALNUM_RE.sub("-", (text or "").lower()).strip("-")


def _tokenize(text: Optional[str]) -> List[str]:
  normalized = _normalize(text)
  if not normalized:
    return []
  return [tok for tok in normalized.split() if len(tok) >= 3 and tok not in STOPWORDS]


def _compact_whitespace(text: Optional[str]) -> str:
  return re.sub(r"\s+", " ", text or "").strip()


def _strip_frontmatter(content: str) -> str:
  return FRONTMATTER_RE.sub("", content or "", count=1).strip()


def _parse_frontmatter(content: str) -> dict:
  match = FRONTMATTER_RE.match(content or "")
  if not match:
    return {}

  data = {}
  current_key = None
  for raw_line in match.group(1).splitlines():
    line = raw_line.rstrip()
    if not line.strip() or line.lstrip().startswith("#"):
      continue

    if line.startswith("  - ") and current_key and isinstance(data.get(current_key), list):
      data[current_key].append(line[4:].strip().strip("'\""))
      continue

    if ":" not in line:
      current_key = None
      continue

    key, _, value = line.partition(":")
    key = key.strip()
    value = value.strip()
    current_key = key

    if not value:
      data[key] = []
      continue

    if value.startswith("[") and value.endswith("]"):
      items = []
      for part in ALIAS_ITEM_RE.finditer(value[1:-1]):
        item = next((group for group in part.groups() if group), "").strip()
        if item:
          items.append(item)
      data[key] = items
      continue

    data[key] = value.strip("'\"")

  return data


def _first_heading(markdown: str) -> Optional[str]:
  for line in (markdown or "").splitlines():
    stripped = line.strip()
    if not stripped:
      continue
    match = HEADING_RE.match(stripped)
    if match:
      return match.group(2).strip()
  return None


def _section_blocks(markdown: str) -> List[dict]:
  lines = (markdown or "").splitlines()
  sections = []
  current_heading = None
  current_lines: List[str] = []

  def flush():
    text = "\n".join(current_lines).strip()
    if text:
      sections.append({
        "heading": current_heading,
        "text": text,
      })

  for line in lines:
    match = HEADING_RE.match(line.strip())
    if match and len(match.group(1)) <= 3:
      if current_lines:
        flush()
      current_heading = match.group(2).strip()
      current_lines = [line]
      continue

    if not current_lines:
      current_lines = [line]
    else:
      current_lines.append(line)

  if current_lines:
    flush()
  return sections


def _split_long_text(text: str, max_chars: int = SECTION_MAX_CHARS, overlap_chars: int = SECTION_OVERLAP_CHARS) -> List[str]:
  cleaned = (text or "").strip()
  if not cleaned:
    return []
  if len(cleaned) <= max_chars:
    return [cleaned]

  windows: List[str] = []
  start = 0
  while start < len(cleaned):
    end = min(len(cleaned), start + max_chars)
    if end < len(cleaned):
      search_floor = start + max_chars // 2
      boundary = cleaned.rfind("\n\n", search_floor, end)
      if boundary < 0:
        boundary = cleaned.rfind(". ", search_floor, end)
      if boundary < 0:
        boundary = cleaned.rfind(" ", search_floor, end)
      if boundary > start:
        end = boundary + (2 if cleaned[boundary:boundary + 2] == "\n\n" else 1)

    block = cleaned[start:end].strip()
    if block:
      windows.append(block)
    if end >= len(cleaned):
      break
    start = max(start + 1, end - overlap_chars)
  return windows


def _story_order_from_label(label: Optional[str]) -> Optional[int]:
  value = _compact_whitespace(label)
  if not value:
    return None

  lower = value.lower()
  if "prologue" in lower:
    return 0

  match = CHAPTER_NUMBER_RE.search(value)
  if match:
    return int(match.group(1))

  lead = LEADING_NUMBER_RE.match(value)
  if lead:
    return int(lead.group(1))
  return None


def _story_order_from_tag(tag: Optional[str]) -> Optional[int]:
  match = TAG_ORDER_RE.match((tag or "").strip())
  if not match:
    return None
  book_num = int(match.group(1) or 0)
  chapter_num = int(match.group(2))
  return (book_num * 1000 + chapter_num) if book_num else chapter_num


def _snapshot_story_label(snapshot: dict) -> Optional[str]:
  label = snapshot.get("story_label")
  if label:
    return label

  index_page = (snapshot.get("pages") or {}).get("wiki/index.md", {})
  content = index_page.get("content") or ""
  match = INDEX_LABEL_RE.search(content)
  if match:
    return _compact_whitespace(match.group(1))
  return None


def _is_interpretive_question(question: str) -> bool:
  q = _normalize(question)
  if not q:
    return False
  triggers = (
    "why", "how", "meaning", "mean", "significance", "important", "motivation",
    "relationship", "feel", "change", "different", "understand", "going on",
    "what does", "what changed", "what should", "what is happening",
  )
  return any(trigger in q for trigger in triggers)


def _discover_wiki_dir(book_title: Optional[str], epub_path: Optional[str]) -> Optional[Path]:
  if not WIKI_ROOT.exists():
    return None

  title_slug = _slugify(book_title)
  if title_slug:
    candidate = WIKI_ROOT / title_slug
    if candidate.is_dir():
      return candidate

  normalized_title = _normalize(book_title)
  normalized_epub = _normalize(Path(epub_path).stem) if epub_path else ""

  best: Optional[Tuple[int, Path]] = None
  for child in sorted(WIKI_ROOT.iterdir()):
    if not child.is_dir():
      continue

    score = 0
    if _slugify(child.name) == title_slug and title_slug:
      score += 100

    metadata_path = child / METADATA_FILE
    if metadata_path.exists():
      try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
      except Exception:
        metadata = {}
      meta_title = _normalize(metadata.get("title"))
      source_epub = _normalize(Path(metadata.get("source_epub", "")).stem)
      if meta_title and meta_title == normalized_title:
        score += 90
      if source_epub and normalized_epub and source_epub == normalized_epub:
        score += 40

    if score and (best is None or score > best[0]):
      best = (score, child)

  return best[1] if best else None


def _run_script(script_path: Path, args: List[str]) -> bool:
  if not script_path.exists():
    return False
  result = subprocess.run(
    [sys.executable, str(script_path), *args],
    capture_output=True,
    text=True,
  )
  return result.returncode == 0


def _expected_snapshot_path(wiki_dir: Path) -> Path:
  return wiki_dir / SNAPSHOT_FILE


def _expected_viewer_path(wiki_dir: Path) -> Path:
  return wiki_dir / VIEWER_FILE


def _ensure_snapshot_file(wiki_dir: Path) -> Optional[Path]:
  snapshot_path = _expected_snapshot_path(wiki_dir)
  if snapshot_path.exists():
    return snapshot_path
  if not (wiki_dir / ".git").exists():
    return None
  if not _run_script(SNAPSHOT_EXPORT_SCRIPT, [str(wiki_dir), "--output", str(snapshot_path)]):
    return None
  return snapshot_path if snapshot_path.exists() else None


def _ensure_viewer_file(wiki_dir: Path) -> Optional[Path]:
  viewer_path = _expected_viewer_path(wiki_dir)
  snapshot_path = _expected_snapshot_path(wiki_dir)
  snapshot_mtime = snapshot_path.stat().st_mtime_ns if snapshot_path.exists() else 0
  viewer_mtime = viewer_path.stat().st_mtime_ns if viewer_path.exists() else 0
  if viewer_path.exists() and viewer_mtime >= snapshot_mtime:
    return viewer_path
  if not (wiki_dir / ".git").exists():
    return viewer_path if viewer_path.exists() else None
  if not _run_script(VIEWER_SCRIPT, [str(wiki_dir), "--output", str(viewer_path)]):
    return viewer_path if viewer_path.exists() else None
  return viewer_path if viewer_path.exists() else None


def _load_bundle(book_title: Optional[str], epub_path: Optional[str]) -> Optional[dict]:
  wiki_dir = _discover_wiki_dir(book_title, epub_path)
  if not wiki_dir:
    return None

  snapshot_path = _expected_snapshot_path(wiki_dir)
  if not snapshot_path.exists():
    return None

  cache_key = f"{snapshot_path}:{snapshot_path.stat().st_mtime_ns}"
  cached = _bundle_cache.get(cache_key)
  if cached is not None:
    return cached

  try:
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
  except Exception:
    return None

  snapshots = payload.get("snapshots") or {}
  tags = payload.get("tags") or list(snapshots.keys())
  ordered_snapshots = []
  for snapshot_rank, tag in enumerate(tags):
    snapshot = snapshots.get(tag)
    if not snapshot:
      continue
    story_label = _snapshot_story_label(snapshot)
    story_order = _story_order_from_tag(tag)
    ordered_snapshots.append({
      **snapshot,
      "snapshot_rank": snapshot_rank,
      "story_label": story_label,
      "story_order": story_order if story_order is not None else _story_order_from_label(story_label),
    })

  bundle = {
    "wiki_dir": str(wiki_dir),
    "snapshot_path": str(snapshot_path),
    "tags": tags,
    "snapshots": ordered_snapshots,
  }
  _bundle_cache.clear()
  _bundle_cache[cache_key] = bundle
  return bundle


def ensure_wiki_artifacts(book_title: Optional[str], epub_path: Optional[str]) -> Optional[dict]:
  wiki_dir = _discover_wiki_dir(book_title, epub_path)
  if not wiki_dir:
    return None

  snapshot_path = _ensure_snapshot_file(wiki_dir)
  if not snapshot_path or not snapshot_path.exists():
    return None

  viewer_path = _ensure_viewer_file(wiki_dir)
  bundle = _load_bundle(book_title, epub_path)
  if not bundle:
    return None

  return {
    **bundle,
    "viewer_path": str(viewer_path) if viewer_path and viewer_path.exists() else None,
    "viewer_exists": bool(viewer_path and viewer_path.exists()),
  }


def _book_chapter_texts(conn, book_id: int) -> List[dict]:
  chapter_rows = conn.execute(
    "SELECT chapter_index, start_position, end_position FROM chapters WHERE book_id = ? ORDER BY chapter_index",
    (book_id,),
  ).fetchall()
  texts = []
  for row in chapter_rows:
    chunk_rows = conn.execute(
      """
      SELECT text
      FROM chunks
      WHERE book_id = ? AND chapter_index = ?
      ORDER BY position_index, id
      LIMIT 3
      """,
      (book_id, row["chapter_index"]),
    ).fetchall()
    snippet = "\n\n".join(chunk["text"] or "" for chunk in chunk_rows)
    texts.append({
      "chapter_index": int(row["chapter_index"]),
      "start_position": int(row["start_position"] or 0),
      "end_position": int(row["end_position"] or 0),
      "text": snippet,
      "story_order": None,
    })
  return texts


def _infer_book_story_orders(chapters: List[dict]) -> Dict[int, Optional[int]]:
  orders: Dict[int, Optional[int]] = {}
  for chapter in chapters:
    preview = _compact_whitespace((chapter.get("text") or "")[:900])
    lower = preview.lower()
    order = None
    if "prologue" in lower[:120]:
      order = 0
    else:
      lead = LEADING_NUMBER_RE.match(preview)
      if lead:
        order = int(lead.group(1))
      else:
        match = CHAPTER_NUMBER_RE.search(preview[:160])
        if match:
          order = int(match.group(1))
    chapter["story_order"] = order
    orders[chapter["chapter_index"]] = order

  first_one_index = next((i for i, chapter in enumerate(chapters) if chapter.get("story_order") == 1), None)
  if first_one_index is not None:
    for idx in range(first_one_index - 1, -1, -1):
      text = _compact_whitespace(chapters[idx].get("text"))
      if len(text) < 60:
        continue
      if chapters[idx].get("story_order") is None:
        chapters[idx]["story_order"] = 0
        orders[chapters[idx]["chapter_index"]] = 0
        break
      break
  return orders


def _safe_story_order(conn, book_id: int, current_chapter_index: Optional[int]) -> Optional[int]:
  if current_chapter_index is None:
    return None
  chapters = _book_chapter_texts(conn, book_id)
  if not chapters:
    return None
  story_orders = _infer_book_story_orders(chapters)
  current_order = story_orders.get(current_chapter_index)
  if current_order is None:
    prior_orders = [
      chapter["story_order"]
      for chapter in chapters
      if chapter["chapter_index"] <= current_chapter_index and chapter.get("story_order") is not None
    ]
    return prior_orders[-1] if prior_orders else None
  return current_order - 1


def _select_safe_snapshot(bundle: dict, safe_story_order: Optional[int]) -> Optional[dict]:
  if safe_story_order is None or safe_story_order < 0:
    return None
  snapshot = None
  for candidate in bundle["snapshots"]:
    order = candidate.get("story_order")
    if order is not None and order <= safe_story_order:
      snapshot = candidate
    elif order is not None and order > safe_story_order:
      break
  return snapshot


def _viewer_url(book_id: int, tag: Optional[str] = None, page_path: Optional[str] = None) -> str:
  params = []
  if tag:
    params.append(("tag", tag))
  if page_path:
    params.append(("page", page_path))
  query = urlencode(params)
  return f"/wiki/{book_id}/viewer" + (f"?{query}" if query else "")


def get_book_wiki_state(
  conn,
  book_id: int,
  book_title: Optional[str],
  epub_path: Optional[str],
  current_chapter_index: Optional[int],
) -> dict:
  try:
    bundle = ensure_wiki_artifacts(book_title, epub_path)
  except Exception:
    bundle = None

  if not bundle:
    return {
      "available": False,
      "safe_available": False,
      "ingested": False,
      "url": None,
      "tag": None,
      "story_label": None,
    }

  safe_snapshot = _select_safe_snapshot(bundle, _safe_story_order(conn, book_id, current_chapter_index))
  ingest_row = conn.execute(
    """
    SELECT snapshot_path, snapshot_mtime_ns, total_sections, status
    FROM wiki_ingests
    WHERE book_id = ?
    """,
    (book_id,),
  ).fetchone()
  snapshot_path = Path(bundle["snapshot_path"])
  snapshot_mtime_ns = snapshot_path.stat().st_mtime_ns if snapshot_path.exists() else None
  ingested = bool(
    ingest_row
    and ingest_row["status"] == "ready"
    and int(ingest_row["total_sections"] or 0) > 0
    and ingest_row["snapshot_path"] == str(snapshot_path)
    and int(ingest_row["snapshot_mtime_ns"] or 0) == int(snapshot_mtime_ns or 0)
  )

  return {
    "available": True,
    "safe_available": bool(safe_snapshot and bundle.get("viewer_exists")),
    "ingested": ingested,
    "url": f"/wiki/{book_id}" if safe_snapshot and bundle.get("viewer_exists") else None,
    "tag": safe_snapshot.get("tag") if safe_snapshot else None,
    "story_label": safe_snapshot.get("story_label") if safe_snapshot else None,
  }


def resolve_default_wiki_view(
  conn,
  book_id: int,
  book_title: Optional[str],
  epub_path: Optional[str],
  current_chapter_index: Optional[int],
) -> Optional[dict]:
  bundle = ensure_wiki_artifacts(book_title, epub_path)
  if not bundle or not bundle.get("viewer_path"):
    return None
  safe_snapshot = _select_safe_snapshot(bundle, _safe_story_order(conn, book_id, current_chapter_index))
  if not safe_snapshot:
    return None
  return {
    "viewer_path": bundle["viewer_path"],
    "tag": safe_snapshot.get("tag"),
    "story_label": safe_snapshot.get("story_label"),
    "url": _viewer_url(book_id, safe_snapshot.get("tag"), "wiki/index.md"),
  }


def _page_metadata(path: str, payload: dict) -> dict:
  content = payload.get("content") or ""
  body = _strip_frontmatter(content)
  frontmatter = payload.get("frontmatter") or _parse_frontmatter(content)
  title = _first_heading(body) or Path(path).stem.replace("-", " ").title()
  aliases = frontmatter.get("aliases") or []
  if isinstance(aliases, str):
    aliases = [aliases]
  aliases = [_compact_whitespace(alias) for alias in aliases if _compact_whitespace(alias)]
  return {
    "path": path,
    "body": body,
    "frontmatter": frontmatter,
    "title": title,
    "aliases": aliases,
    "category": payload.get("category"),
  }


def _page_record(path: str, payload: dict) -> dict:
  meta = _page_metadata(path, payload)
  sections = _section_blocks(meta["body"])
  summary = ""
  summary_heading = None
  preferred_headings = {"summary", "what we know so far", "current understanding", "open questions"}
  for section in sections:
    heading = _normalize(section.get("heading"))
    if heading in preferred_headings:
      summary = section.get("text") or ""
      summary_heading = section.get("heading")
      break
  if not summary:
    paragraphs = [block.strip() for block in meta["body"].split("\n\n") if block.strip()]
    summary = "\n\n".join(paragraphs[:2]) if paragraphs else meta["body"]
  tokens = set(_tokenize(meta["title"]))
  for alias in meta["aliases"]:
    tokens.update(_tokenize(alias))
  tokens.update(_tokenize(Path(path).stem.replace("-", " ")))
  return {
    **meta,
    "sections": sections,
    "summary": _compact_whitespace(summary),
    "summary_heading": _compact_whitespace(summary_heading) if summary_heading else None,
    "tokens": tokens,
  }


def _score_section(question_tokens: set, page: dict, section: dict) -> int:
  heading_tokens = set(_tokenize(section.get("heading")))
  section_tokens = set(_tokenize(section.get("text", "")[:1200]))
  overlap = question_tokens & (heading_tokens | section_tokens | page["tokens"])
  score = len(overlap) * 4
  heading = (section.get("heading") or "").strip().lower()
  if heading == "summary":
    score += 8
  if heading in {"what we know so far", "current understanding", "open questions", "relationships"}:
    score += 4
  return score


def _page_context(page: dict, question_tokens: set, max_chars: int) -> str:
  if page["path"] in {"wiki/index.md", "wiki/open-questions.md"}:
    body = page["body"][:max_chars].strip()
    return f"[{page['path']}]\n{body}"

  sections = page["sections"] or [{"heading": page["title"], "text": page["body"]}]
  ranked = sorted(
    sections,
    key=lambda section: (
      _score_section(question_tokens, page, section),
      1 if (section.get("heading") or "").strip().lower() == "summary" else 0,
    ),
    reverse=True,
  )

  chosen: List[str] = []
  used_headings = set()
  total = 0
  for section in ranked:
    heading = (section.get("heading") or page["title"]).strip()
    if heading in used_headings:
      continue
    text = section["text"].strip()
    if not text:
      continue
    block = text[:max(220, min(1100, max_chars - total))]
    chosen.append(block)
    used_headings.add(heading)
    total += len(block)
    if total >= max_chars or len(chosen) >= 2:
      break

  if not chosen:
    chosen.append(page["body"][:max_chars].strip())

  return f"[{page['path']}]\n" + "\n\n".join(block for block in chosen if block)


def _embedding_text(page_title: str, section_heading: Optional[str], page_path: str, section_text: str) -> str:
  heading = _compact_whitespace(section_heading) if section_heading else ""
  parts = [f"Page: {page_title}"]
  if heading and _normalize(heading) != _normalize(page_title):
    parts.append(f"Section: {heading}")
  parts.append(f"Path: {page_path}")
  parts.append(section_text.strip())
  return "\n".join(part for part in parts if part).strip()


def _build_section_records(snapshot: dict) -> List[dict]:
  snapshot_tag = snapshot.get("tag")
  snapshot_rank = int(snapshot.get("snapshot_rank") or 0)
  story_order = snapshot.get("story_order")
  records: List[dict] = []

  for path, payload in (snapshot.get("pages") or {}).items():
    page = _page_record(path, payload)
    summary_heading = page["summary_heading"] or page["title"]
    summary_text = page["summary"] or page["body"][:SECTION_MAX_CHARS]
    records.append({
      "snapshot_tag": snapshot_tag,
      "snapshot_rank": snapshot_rank,
      "story_order": story_order,
      "page_path": path,
      "page_title": page["title"],
      "category": page["category"],
      "section_heading": summary_heading,
      "section_index": 0,
      "section_text": summary_text.strip(),
      "embed_text": _embedding_text(page["title"], summary_heading, path, page["body"]),
    })
  return records


def ensure_wiki_ingested(
  conn,
  book_id: int,
  book_title: Optional[str],
  epub_path: Optional[str],
) -> Optional[dict]:
  bundle = ensure_wiki_artifacts(book_title, epub_path)
  if not bundle:
    return None

  snapshot_path = Path(bundle["snapshot_path"])
  snapshot_mtime_ns = snapshot_path.stat().st_mtime_ns if snapshot_path.exists() else None
  viewer_path = Path(bundle["viewer_path"]) if bundle.get("viewer_path") else None
  viewer_mtime_ns = viewer_path.stat().st_mtime_ns if viewer_path and viewer_path.exists() else None

  existing = conn.execute(
    """
    SELECT snapshot_path, snapshot_mtime_ns, total_sections, status
    FROM wiki_ingests
    WHERE book_id = ?
    """,
    (book_id,),
  ).fetchone()

  if (
    existing
    and existing["status"] == "ready"
    and int(existing["total_sections"] or 0) > 0
    and existing["snapshot_path"] == str(snapshot_path)
    and int(existing["snapshot_mtime_ns"] or 0) == int(snapshot_mtime_ns or 0)
  ):
    return {
      **bundle,
      "snapshot_mtime_ns": snapshot_mtime_ns,
      "viewer_mtime_ns": viewer_mtime_ns,
    }

  section_records: List[dict] = []
  for snapshot in bundle["snapshots"]:
    section_records.extend(_build_section_records(snapshot))
  if not section_records:
    return None

  embeddings = embed_texts([record["embed_text"] for record in section_records])

  conn.execute("DELETE FROM vec_wiki_sections WHERE book_id = ?", (book_id,))
  conn.execute("DELETE FROM wiki_sections WHERE book_id = ?", (book_id,))

  vec_rows = []
  for record, embedding in zip(section_records, embeddings):
    cursor = conn.execute(
      """
      INSERT INTO wiki_sections
        (book_id, snapshot_tag, snapshot_rank, story_order, page_path, page_title, category, section_heading, section_index, section_text)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        book_id,
        record["snapshot_tag"],
        record["snapshot_rank"],
        record["story_order"],
        record["page_path"],
        record["page_title"],
        record["category"],
        record["section_heading"],
        record["section_index"],
        record["section_text"],
      ),
    )
    section_id = int(cursor.lastrowid)
    vec_rows.append((_serialize_vec(embedding), section_id, book_id, record["snapshot_rank"]))

  conn.executemany(
    "INSERT INTO vec_wiki_sections (embedding, section_id, book_id, snapshot_rank) VALUES (?, ?, ?, ?)",
    vec_rows,
  )

  latest_snapshot = bundle["snapshots"][-1] if bundle["snapshots"] else None
  conn.execute(
    """
    INSERT INTO wiki_ingests
      (book_id, wiki_dir, snapshot_path, snapshot_mtime_ns, viewer_path, viewer_mtime_ns, total_snapshots, total_sections, latest_tag, latest_story_order, status, last_error, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', NULL, CURRENT_TIMESTAMP)
    ON CONFLICT(book_id) DO UPDATE SET
      wiki_dir = excluded.wiki_dir,
      snapshot_path = excluded.snapshot_path,
      snapshot_mtime_ns = excluded.snapshot_mtime_ns,
      viewer_path = excluded.viewer_path,
      viewer_mtime_ns = excluded.viewer_mtime_ns,
      total_snapshots = excluded.total_snapshots,
      total_sections = excluded.total_sections,
      latest_tag = excluded.latest_tag,
      latest_story_order = excluded.latest_story_order,
      status = 'ready',
      last_error = NULL,
      updated_at = CURRENT_TIMESTAMP
    """,
    (
      book_id,
      bundle["wiki_dir"],
      str(snapshot_path),
      snapshot_mtime_ns,
      str(viewer_path) if viewer_path else None,
      viewer_mtime_ns,
      len(bundle["snapshots"]),
      len(section_records),
      latest_snapshot.get("tag") if latest_snapshot else None,
      latest_snapshot.get("story_order") if latest_snapshot else None,
    ),
  )
  conn.commit()

  return {
    **bundle,
    "snapshot_mtime_ns": snapshot_mtime_ns,
    "viewer_mtime_ns": viewer_mtime_ns,
  }


def _fetch_sections_by_ids(conn, section_ids: List[int]) -> List[dict]:
  if not section_ids:
    return []
  placeholders = ",".join(["?"] * len(section_ids))
  rows = conn.execute(
    f"""
    SELECT id, snapshot_tag, snapshot_rank, story_order, page_path, page_title, category, section_heading, section_index, section_text
    FROM wiki_sections
    WHERE id IN ({placeholders})
    """,
    section_ids,
  ).fetchall()
  by_id = {int(row["id"]): dict(row) for row in rows}
  return [by_id[section_id] for section_id in section_ids if section_id in by_id]


def _search_wiki_sections(
  conn,
  book_id: int,
  snapshot_rank: int,
  question_embedding: List[float],
  limit: int = 6,
) -> List[dict]:
  rows = conn.execute(
    """
    SELECT section_id, distance
    FROM vec_wiki_sections
    WHERE book_id = ? AND snapshot_rank = ? AND embedding MATCH ?
    ORDER BY distance
    LIMIT ?
    """,
    (book_id, snapshot_rank, _serialize_vec(question_embedding), int(limit)),
  ).fetchall()
  section_ids = [int(row["section_id"]) for row in rows]
  return _fetch_sections_by_ids(conn, section_ids)


def _page_sections(conn, book_id: int, snapshot_rank: int, page_path: str, limit: int = 2) -> List[dict]:
  rows = conn.execute(
    """
    SELECT id, snapshot_tag, snapshot_rank, story_order, page_path, page_title, category, section_heading, section_index, section_text
    FROM wiki_sections
    WHERE book_id = ? AND snapshot_rank = ? AND page_path = ?
    ORDER BY section_index ASC, id ASC
    LIMIT ?
    """,
    (book_id, snapshot_rank, page_path, int(limit)),
  ).fetchall()
  return [dict(row) for row in rows]


def _merge_selected_sections(index_sections: List[dict], open_sections: List[dict], vector_sections: List[dict]) -> List[dict]:
  chosen: List[dict] = []
  seen_ids = set()
  for group in (index_sections, open_sections, vector_sections):
    for section in group:
      section_id = int(section["id"])
      if section_id in seen_ids:
        continue
      chosen.append(section)
      seen_ids.add(section_id)
      if len(chosen) >= 6:
        return chosen
  return chosen


def _search_terms_from_sections(selected_sections: List[dict], snapshot: dict) -> List[str]:
  pages_payload = snapshot.get("pages") or {}
  terms: List[str] = []
  seen = set()
  for section in selected_sections:
    page_path = section["page_path"]
    payload = pages_payload.get(page_path) or {}
    meta = _page_metadata(page_path, payload)
    candidates = [section["page_title"], *meta["aliases"]]
    heading = _compact_whitespace(section.get("section_heading"))
    heading_base = re.sub(r"\s+\(part\s+\d+\)$", "", heading, flags=re.IGNORECASE).strip() if heading else ""
    if heading_base and _normalize(heading_base) not in GENERIC_HEADINGS and _normalize(heading_base) != _normalize(section["page_title"]):
      candidates.append(heading_base)
    elif heading and _normalize(heading) not in GENERIC_HEADINGS and _normalize(heading) != _normalize(section["page_title"]):
      candidates.append(heading)
    for candidate in candidates:
      compact = _compact_whitespace(candidate)
      if not compact or len(compact) < 3:
        continue
      lowered = compact.lower()
      if lowered in seen:
        continue
      seen.add(lowered)
      terms.append(compact)
      if len(terms) >= 12:
        return terms
  return terms


def _wiki_context_blocks(selected_sections: List[dict], snapshot: dict, question: str, char_budget: int = WIKI_CONTEXT_CHAR_BUDGET) -> str:
  pages_payload = snapshot.get("pages") or {}
  question_tokens = set(_tokenize(question))
  blocks: List[str] = []
  remaining = char_budget
  for section in selected_sections:
    if remaining <= 180:
      break
    payload = pages_payload.get(section["page_path"])
    if not payload:
      continue
    page = _page_record(section["page_path"], payload)
    body = _page_context(page, question_tokens, remaining - 40)
    if not body:
      continue
    block = f"[w:{section['id']}] {body}".strip()
    blocks.append(block)
    remaining -= len(block)
  return "\n\n".join(blocks).strip()


def load_query_wiki_context(
  conn,
  book_id: int,
  book_title: Optional[str],
  epub_path: Optional[str],
  current_chapter_index: Optional[int],
  question: str,
  question_embedding: Optional[List[float]] = None,
) -> Optional[dict]:
  if current_chapter_index is None or not question_embedding:
    return None

  bundle = ensure_wiki_ingested(conn, book_id, book_title, epub_path)
  if not bundle:
    return None

  safe_snapshot = _select_safe_snapshot(bundle, _safe_story_order(conn, book_id, current_chapter_index))
  if not safe_snapshot:
    return None

  snapshot_rank = int(safe_snapshot["snapshot_rank"])
  vector_sections = _search_wiki_sections(conn, book_id, snapshot_rank, question_embedding, limit=6)
  index_sections = _page_sections(conn, book_id, snapshot_rank, "wiki/index.md", limit=1)
  open_sections = _page_sections(conn, book_id, snapshot_rank, "wiki/open-questions.md", limit=1) if _is_interpretive_question(question) else []
  selected_sections = _merge_selected_sections(index_sections, open_sections, vector_sections)
  if not selected_sections:
    return None

  pages = []
  seen_pages = set()
  for section in selected_sections:
    page_path = section["page_path"]
    if page_path in seen_pages:
      continue
    seen_pages.add(page_path)
    pages.append({"path": page_path, "title": section["page_title"]})

  context = _wiki_context_blocks(selected_sections, safe_snapshot, question)
  if not context:
    return None

  return {
    "wiki_dir": bundle["wiki_dir"],
    "viewer_path": bundle.get("viewer_path"),
    "tag": safe_snapshot.get("tag"),
    "story_label": safe_snapshot.get("story_label"),
    "story_order": safe_snapshot.get("story_order"),
    "snapshot_rank": snapshot_rank,
    "pages": pages,
    "search_terms": _search_terms_from_sections(selected_sections, safe_snapshot),
    "sources": [
      {
        "source_type": "wiki",
        "wiki_source_id": int(section["id"]),
        "snapshot_tag": safe_snapshot.get("tag"),
        "story_label": safe_snapshot.get("story_label"),
        "page_path": section["page_path"],
        "page_title": section["page_title"],
        "section_heading": section.get("section_heading"),
        "viewer_url": _viewer_url(book_id, safe_snapshot.get("tag"), section["page_path"]),
        "snippet": (section.get("section_text") or "")[:280],
      }
      for section in selected_sections
    ],
    "context": context,
  }
