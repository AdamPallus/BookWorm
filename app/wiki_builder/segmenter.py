import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

try:
  import tiktoken
  _enc = tiktoken.get_encoding("cl100k_base")

  def _default_count_tokens(text: str) -> int:
    return len(_enc.encode(text))
except ImportError:
  def _default_count_tokens(text: str) -> int:
    return max(1, len(text) // 4)


TARGET_SEGMENT_TOKENS = 7000
MIN_SEGMENT_TOKENS = 3000
MAX_SEGMENT_TOKENS = 12000

SCENE_BREAK_RE = re.compile(
  r"^[ \t]*(?:\*\s*\*\s*\*\*?|#\s*#\s*#|-{3,}|—{2,}|×)[ \t]*$",
  re.MULTILINE,
)


@dataclass
class Segment:
  chapter_index: int
  segment_in_chapter: int
  start_char: int
  end_char: int
  token_count: int


@dataclass
class SegmenterConfig:
  target: int = TARGET_SEGMENT_TOKENS
  minimum: int = MIN_SEGMENT_TOKENS
  maximum: int = MAX_SEGMENT_TOKENS
  count_tokens: Callable[[str], int] = _default_count_tokens

  def __post_init__(self):
    if not (0 < self.minimum < self.target < self.maximum):
      raise ValueError(
        f"segmenter bounds invalid: 0 < min ({self.minimum}) < target ({self.target}) < max ({self.maximum})"
      )


def find_scene_breaks(text: str) -> List[int]:
  positions: List[int] = []
  for m in SCENE_BREAK_RE.finditer(text):
    line_start = m.start()
    if line_start > 0 and text[line_start - 1] != "\n":
      continue
    line_end = m.end()
    after = line_end
    while after < len(text) and text[after] in " \t":
      after += 1
    if after < len(text) and text[after] != "\n":
      continue
    while after < len(text) and text[after] == "\n":
      after += 1
    positions.append(after)
  return positions


def _normalize_pieces(text: str, cuts: List[int]) -> List[Tuple[int, int]]:
  bounded = [c for c in cuts if 0 < c < len(text)]
  bounded = sorted(set(bounded))
  pieces: List[Tuple[int, int]] = []
  prev = 0
  for c in bounded:
    if c > prev:
      pieces.append((prev, c))
      prev = c
  if prev < len(text):
    pieces.append((prev, len(text)))
  return pieces


def _pack_by_breaks(
  text: str,
  breaks: List[int],
  cfg: SegmenterConfig,
) -> Optional[List[Tuple[int, int]]]:
  if not breaks:
    return None
  cuts: List[int] = []
  acc_start = 0
  for b in breaks:
    candidate_end = b
    candidate_tokens = cfg.count_tokens(text[acc_start:candidate_end])
    if candidate_tokens >= cfg.target:
      cuts.append(b)
      acc_start = b
  pieces = _normalize_pieces(text, cuts)
  for start, end in pieces:
    if cfg.count_tokens(text[start:end]) > cfg.maximum:
      return None
  if any(cfg.count_tokens(text[s:e]) < cfg.minimum for s, e in pieces[:-1]):
    return None
  if len(pieces) > 1:
    last_start, last_end = pieces[-1]
    last_tokens = cfg.count_tokens(text[last_start:last_end])
    if last_tokens < cfg.minimum:
      penultimate_start, _ = pieces[-2]
      pieces = pieces[:-2] + [(penultimate_start, last_end)]
      if cfg.count_tokens(text[pieces[-1][0]:pieces[-1][1]]) > cfg.maximum:
        return None
  return pieces


PARAGRAPH_SPLIT_RE = re.compile(r"\n{2,}")


def _paragraph_spans(text: str) -> List[Tuple[int, int]]:
  if not text:
    return []
  spans: List[Tuple[int, int]] = []
  cursor = 0
  for m in PARAGRAPH_SPLIT_RE.finditer(text):
    spans.append((cursor, m.end()))
    cursor = m.end()
  if cursor < len(text):
    spans.append((cursor, len(text)))
  return spans


def _pack_by_paragraphs(text: str, cfg: SegmenterConfig) -> List[Tuple[int, int]]:
  paragraphs = _paragraph_spans(text)
  if not paragraphs:
    return [(0, len(text))]

  pieces: List[Tuple[int, int]] = []
  cur_start = paragraphs[0][0]
  cur_end = paragraphs[0][0]

  for p_start, p_end in paragraphs:
    if cur_end == cur_start:
      cur_start = p_start
      cur_end = p_end
      continue

    candidate_text = text[cur_start:p_end]
    candidate_tokens = cfg.count_tokens(candidate_text)
    cur_tokens = cfg.count_tokens(text[cur_start:cur_end])

    if candidate_tokens > cfg.maximum:
      pieces.append((cur_start, cur_end))
      cur_start = p_start
      cur_end = p_end
      continue

    if cur_tokens >= cfg.target:
      pieces.append((cur_start, cur_end))
      cur_start = p_start
      cur_end = p_end
    else:
      cur_end = p_end

  if cur_end > cur_start:
    pieces.append((cur_start, cur_end))

  pieces = _merge_undersized_tail(pieces, text, cfg)
  pieces = _hard_split_oversize(pieces, text, cfg)
  return pieces


def _merge_undersized_tail(
  pieces: List[Tuple[int, int]],
  text: str,
  cfg: SegmenterConfig,
) -> List[Tuple[int, int]]:
  if len(pieces) <= 1:
    return pieces
  last_start, last_end = pieces[-1]
  if cfg.count_tokens(text[last_start:last_end]) >= cfg.minimum:
    return pieces
  prev_start, _ = pieces[-2]
  merged_tokens = cfg.count_tokens(text[prev_start:last_end])
  if merged_tokens <= cfg.maximum:
    return pieces[:-2] + [(prev_start, last_end)]
  return pieces


def _hard_split_oversize(
  pieces: List[Tuple[int, int]],
  text: str,
  cfg: SegmenterConfig,
) -> List[Tuple[int, int]]:
  out: List[Tuple[int, int]] = []
  for start, end in pieces:
    piece_text = text[start:end]
    if cfg.count_tokens(piece_text) <= cfg.maximum:
      out.append((start, end))
      continue
    cursor = start
    while cursor < end:
      remaining_tokens = cfg.count_tokens(text[cursor:end])
      if remaining_tokens <= cfg.maximum:
        out.append((cursor, end))
        break
      ratio = cfg.maximum / remaining_tokens
      approx = max(1, int((end - cursor) * ratio))
      cut = cursor + approx
      while cut > cursor + 1 and not text[cut - 1].isspace():
        cut -= 1
      if cut <= cursor + 1:
        cut = cursor + max(1, approx)
      out.append((cursor, cut))
      cursor = cut
  return out


def _wrap_pieces(
  text: str,
  pieces: List[Tuple[int, int]],
  chapter_index: int,
  cfg: SegmenterConfig,
) -> List[Segment]:
  segments: List[Segment] = []
  for i, (start, end) in enumerate(pieces):
    segments.append(
      Segment(
        chapter_index=chapter_index,
        segment_in_chapter=i,
        start_char=start,
        end_char=end,
        token_count=cfg.count_tokens(text[start:end]),
      )
    )
  return segments


def segment_chapter(
  text: str,
  chapter_index: int,
  config: Optional[SegmenterConfig] = None,
) -> List[Segment]:
  cfg = config or SegmenterConfig()
  if not text:
    return []
  total = cfg.count_tokens(text)
  if total <= cfg.maximum:
    return [Segment(chapter_index, 0, 0, len(text), total)]

  packed = _pack_by_breaks(text, find_scene_breaks(text), cfg)
  if packed is None:
    packed = _pack_by_paragraphs(text, cfg)

  return _wrap_pieces(text, packed, chapter_index, cfg)


def segment_book(
  chapters: List[Tuple[int, str]],
  config: Optional[SegmenterConfig] = None,
) -> List[Segment]:
  out: List[Segment] = []
  for chapter_index, text in chapters:
    out.extend(segment_chapter(text, chapter_index, config))
  return out


def assign_story_orders(segments: List[Segment], starting_order: int = 0) -> List[Tuple[Segment, int]]:
  return [(seg, starting_order + i) for i, seg in enumerate(segments)]
