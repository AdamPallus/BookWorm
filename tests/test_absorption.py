"""Tests for the segment-absorption helper used at init time."""

import unittest

from scripts.init_wiki import choose_absorbed_segment_ids


class ChooseAbsorbedSegmentIdsTests(unittest.TestCase):
  def test_empty(self):
    self.assertEqual(choose_absorbed_segment_ids([], 1000), [])

  def test_single_segment_never_absorbed(self):
    # No "next" segment to fold into, so even a tiny one stays pending.
    self.assertEqual(choose_absorbed_segment_ids([(1, 0, 50)], 1000), [])

  def test_short_front_matter_marked(self):
    rows = [(10, 0, 80), (11, 1, 200), (12, 2, 5000), (13, 3, 6000)]
    self.assertEqual(choose_absorbed_segment_ids(rows, 1000), [10, 11])

  def test_short_tail_left_alone(self):
    rows = [(1, 0, 7000), (2, 1, 6000), (3, 2, 100)]
    self.assertEqual(choose_absorbed_segment_ids(rows, 1000), [])

  def test_short_in_middle_marked(self):
    rows = [(1, 0, 7000), (2, 1, 200), (3, 2, 6000)]
    self.assertEqual(choose_absorbed_segment_ids(rows, 1000), [2])

  def test_threshold_exclusive(self):
    rows = [(1, 0, 1000), (2, 1, 999), (3, 2, 7000)]
    self.assertEqual(choose_absorbed_segment_ids(rows, 1000), [2])

  def test_zero_threshold_disables(self):
    rows = [(1, 0, 50), (2, 1, 60), (3, 2, 7000)]
    self.assertEqual(choose_absorbed_segment_ids(rows, 0), [])

  def test_unsorted_rows_normalised(self):
    rows = [(3, 2, 7000), (1, 0, 80), (2, 1, 200)]
    self.assertEqual(choose_absorbed_segment_ids(rows, 1000), [1, 2])


if __name__ == "__main__":
  unittest.main()
