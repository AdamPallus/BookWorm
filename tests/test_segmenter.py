import unittest

from app.wiki_builder.segmenter import (
  Segment,
  SegmenterConfig,
  find_scene_breaks,
  segment_chapter,
)


def _by_chars(text: str) -> int:
  return max(1, len(text))


SMALL_CFG = SegmenterConfig(target=100, minimum=40, maximum=180, count_tokens=_by_chars)


class FindSceneBreaksTests(unittest.TestCase):
  def test_no_breaks_returns_empty(self):
    self.assertEqual(find_scene_breaks("just plain prose with no markers"), [])

  def test_basic_asterisk_break(self):
    text = "First scene.\n\n* * *\n\nSecond scene."
    breaks = find_scene_breaks(text)
    self.assertEqual(len(breaks), 1)
    self.assertEqual(text[breaks[0]:].lstrip("\n"), "Second scene.")

  def test_triple_hash_break(self):
    text = "A.\n\n# # #\n\nB."
    breaks = find_scene_breaks(text)
    self.assertEqual(len(breaks), 1)
    self.assertTrue(text[breaks[0]:].startswith("B."))

  def test_em_dash_break(self):
    text = "A.\n\n———\n\nB."
    breaks = find_scene_breaks(text)
    self.assertEqual(len(breaks), 1)

  def test_dash_run_break(self):
    text = "A.\n\n-----\n\nB."
    breaks = find_scene_breaks(text)
    self.assertEqual(len(breaks), 1)

  def test_inline_asterisks_are_not_breaks(self):
    text = "She said *yes*. They left."
    self.assertEqual(find_scene_breaks(text), [])

  def test_multiple_breaks(self):
    text = "A.\n\n***\n\nB.\n\n***\n\nC."
    breaks = find_scene_breaks(text)
    self.assertEqual(len(breaks), 2)


class SegmentChapterShortTests(unittest.TestCase):
  def test_empty_text(self):
    self.assertEqual(segment_chapter("", chapter_index=0, config=SMALL_CFG), [])

  def test_single_segment_when_under_max(self):
    text = "x" * 150
    segs = segment_chapter(text, chapter_index=2, config=SMALL_CFG)
    self.assertEqual(len(segs), 1)
    self.assertEqual(segs[0].chapter_index, 2)
    self.assertEqual(segs[0].segment_in_chapter, 0)
    self.assertEqual(segs[0].start_char, 0)
    self.assertEqual(segs[0].end_char, len(text))

  def test_single_short_segment_allowed(self):
    text = "x" * 5
    segs = segment_chapter(text, chapter_index=0, config=SMALL_CFG)
    self.assertEqual(len(segs), 1)
    self.assertEqual(segs[0].token_count, 5)


class SegmentChapterSplitTests(unittest.TestCase):
  def _make_text_with_breaks(self, sizes, sep="\n\n***\n\n"):
    parts = ["x" * n for n in sizes]
    return sep.join(parts)

  def test_splits_on_scene_breaks(self):
    text = self._make_text_with_breaks([90, 90, 90, 90])
    segs = segment_chapter(text, chapter_index=0, config=SMALL_CFG)
    self.assertGreaterEqual(len(segs), 2)
    self._assert_pieces_concat_text(text, segs)
    for s in segs:
      self.assertLessEqual(s.token_count, SMALL_CFG.maximum)

  def test_falls_back_to_paragraphs_when_no_breaks(self):
    paragraphs = "\n\n".join("x" * 60 for _ in range(8))
    segs = segment_chapter(paragraphs, chapter_index=0, config=SMALL_CFG)
    self.assertGreaterEqual(len(segs), 2)
    self._assert_pieces_concat_text(paragraphs, segs)
    for s in segs:
      self.assertLessEqual(s.token_count, SMALL_CFG.maximum)

  def test_segments_never_below_min_when_split(self):
    paragraphs = "\n\n".join("x" * 50 for _ in range(10))
    segs = segment_chapter(paragraphs, chapter_index=0, config=SMALL_CFG)
    self.assertGreater(len(segs), 1)
    for s in segs:
      self.assertGreaterEqual(s.token_count, SMALL_CFG.minimum)

  def test_segments_have_monotonic_indices(self):
    text = self._make_text_with_breaks([90, 90, 90, 90, 90])
    segs = segment_chapter(text, chapter_index=4, config=SMALL_CFG)
    for i, seg in enumerate(segs):
      self.assertEqual(seg.segment_in_chapter, i)
      self.assertEqual(seg.chapter_index, 4)

  def test_giant_paragraph_falls_back_to_hard_split(self):
    text = "x" * 1000
    segs = segment_chapter(text, chapter_index=0, config=SMALL_CFG)
    self.assertGreater(len(segs), 1)
    self._assert_pieces_concat_text(text, segs)
    for s in segs[:-1]:
      self.assertLessEqual(s.token_count, SMALL_CFG.maximum)

  def test_break_packing_groups_until_target(self):
    text = self._make_text_with_breaks([50, 50, 50, 50, 50, 50])
    segs = segment_chapter(text, chapter_index=0, config=SMALL_CFG)
    self._assert_pieces_concat_text(text, segs)
    for s in segs:
      self.assertLessEqual(s.token_count, SMALL_CFG.maximum)
      self.assertGreaterEqual(s.token_count, SMALL_CFG.minimum)

  def _assert_pieces_concat_text(self, text, segs):
    pieces = [text[s.start_char:s.end_char] for s in segs]
    self.assertEqual("".join(pieces), text)
    for i in range(1, len(segs)):
      self.assertEqual(segs[i - 1].end_char, segs[i].start_char)


class SegmenterConfigValidationTests(unittest.TestCase):
  def test_invalid_bounds_raise(self):
    with self.assertRaises(ValueError):
      SegmenterConfig(target=100, minimum=200, maximum=300)
    with self.assertRaises(ValueError):
      SegmenterConfig(target=100, minimum=50, maximum=80)


if __name__ == "__main__":
  unittest.main()
