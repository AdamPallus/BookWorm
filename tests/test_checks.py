import unittest

from app.wiki_builder.checks import (
  check_banned_phrases,
  check_diff_schema,
  check_question_evidence,
  check_question_id_hygiene,
  check_source_attribution,
  run_all_checks,
)


def _good_diff():
  return {
    "summary_card": {
      "key_events": ["Cassius wins his first match."],
      "active_characters": ["characters/cassius.md"],
      "new_facts": ["The Institute uses standards as a ranking system."],
      "questions_added": ["q-0007"],
      "questions_resolved": [],
    },
    "pages_created": [
      {
        "path": "characters/cassius.md",
        "title": "Cassius",
        "summary": "A favored Peerless Scarred who enters the Institute as the standard-bearer of House Bellona.",
        "detail": "Cassius is introduced as the polished face of the Society's elite. He moves through the Institute's halls with practiced confidence.",
        "source_note": "introduced as Peerless Scarred entering the Institute",
      }
    ],
    "pages_updated": [
      {
        "path": "characters/darrow.md",
        "summary": "Darrow gains his first ally inside House Mars.",
        "detail_append": "Darrow's bond with Cassius will be tested in the matches that follow.",
        "detail_replace": None,
        "source_note": "first encounter with Cassius",
      }
    ],
    "questions_added": [
      {
        "id": "q-0007",
        "title": "What is the standard test?",
        "text": "What does the standard test require of each house?",
      }
    ],
    "questions_resolved": [],
    "log_entry": "Introduced Cassius and noted Darrow's first alliance. Raised an open question about the standard test.",
  }


class DiffSchemaTests(unittest.TestCase):
  def test_good_diff_passes_schema(self):
    self.assertEqual(check_diff_schema(_good_diff()), [])

  def test_missing_summary_card_fails(self):
    diff = _good_diff()
    del diff["summary_card"]
    issues = check_diff_schema(diff)
    self.assertTrue(any(i.where == "summary_card" for i in issues))

  def test_empty_key_events_fails(self):
    diff = _good_diff()
    diff["summary_card"]["key_events"] = []
    issues = check_diff_schema(diff)
    self.assertTrue(any("key_events" in i.where for i in issues))

  def test_bad_page_path_fails(self):
    diff = _good_diff()
    diff["pages_created"][0]["path"] = "characters/Cassius.md"
    issues = check_diff_schema(diff)
    self.assertTrue(any("pages_created[0].path" in i.where for i in issues))

  def test_disallowed_page_dir_fails(self):
    diff = _good_diff()
    diff["pages_created"][0]["path"] = "misc/random.md"
    issues = check_diff_schema(diff)
    self.assertTrue(any("pages_created[0].path" in i.where for i in issues))

  def test_both_detail_modes_set_fails(self):
    diff = _good_diff()
    diff["pages_updated"][0]["detail_replace"] = "Full replacement."
    issues = check_diff_schema(diff)
    self.assertTrue(any(i.where.endswith("pages_updated[0]") for i in issues))

  def test_invalid_question_id_fails(self):
    diff = _good_diff()
    diff["questions_added"][0]["id"] = "q-7"
    issues = check_diff_schema(diff)
    self.assertTrue(any("questions_added[0].id" in i.where for i in issues))


class BannedPhraseTests(unittest.TestCase):
  def test_good_diff_has_no_banned_phrases(self):
    self.assertEqual(check_banned_phrases(_good_diff()), [])

  def test_as_of_chapter_in_summary(self):
    diff = _good_diff()
    diff["pages_created"][0]["summary"] = "As of chapter 5, Cassius rules the Institute."
    issues = check_banned_phrases(diff)
    self.assertTrue(any(i.kind == "banned-phrase" for i in issues))

  def test_inline_chapter_citation(self):
    diff = _good_diff()
    diff["pages_updated"][0]["detail_append"] = "Their bond strengthens (ch. 12)."
    issues = check_banned_phrases(diff)
    self.assertTrue(any("inline-ch-citation" in i.detail for i in issues))

  def test_later_revealed(self):
    diff = _good_diff()
    diff["pages_created"][0]["detail"] = "Cassius is later revealed to harbor a secret grudge."
    issues = check_banned_phrases(diff)
    self.assertTrue(any("later-revealed" in i.detail for i in issues))

  def test_will_eventually(self):
    diff = _good_diff()
    diff["log_entry"] = "Cassius will eventually become a key antagonist."
    issues = check_banned_phrases(diff)
    self.assertTrue(any("will-eventually" in i.detail for i in issues))

  def test_currently_as_of(self):
    diff = _good_diff()
    diff["pages_updated"][0]["summary"] = "Currently as of this writing he leads House Bellona."
    issues = check_banned_phrases(diff)
    self.assertTrue(any("currently-as-of" in i.detail for i in issues))

  def test_question_resolution_field_is_scanned(self):
    diff = _good_diff()
    diff["questions_resolved"] = [
      {"id": "q-0001", "resolution": "as of chapter 5, the answer is yes", "evidence_quote": "x"}
    ]
    issues = check_banned_phrases(diff)
    self.assertTrue(any(i.where.startswith("questions_resolved[0].resolution") for i in issues))


class SourceAttributionTests(unittest.TestCase):
  def test_good_diff_passes(self):
    self.assertEqual(check_source_attribution(_good_diff()), [])

  def test_missing_source_note_in_created(self):
    diff = _good_diff()
    diff["pages_created"][0]["source_note"] = ""
    issues = check_source_attribution(diff)
    self.assertTrue(any(i.kind == "missing-source" for i in issues))

  def test_overlong_source_note(self):
    diff = _good_diff()
    diff["pages_updated"][0]["source_note"] = "x" * 200
    issues = check_source_attribution(diff)
    self.assertTrue(any(i.kind == "missing-source" for i in issues))


class QuestionEvidenceTests(unittest.TestCase):
  def test_passes_when_quote_present(self):
    segment = "Darrow drove the spike home and the helldiver's torch fell silent."
    diff = _good_diff()
    diff["questions_resolved"] = [
      {
        "id": "q-0001",
        "resolution": "Darrow's father is killed at his own execution.",
        "evidence_quote": "the helldiver's torch fell silent",
      }
    ]
    self.assertEqual(check_question_evidence(diff, segment), [])

  def test_fails_when_quote_absent(self):
    segment = "Darrow drove the spike home and the helldiver's torch fell silent."
    diff = _good_diff()
    diff["questions_resolved"] = [
      {
        "id": "q-0001",
        "resolution": "Mustang reveals her identity.",
        "evidence_quote": "Mustang quietly admitted she was Virginia au Augustus",
      }
    ]
    issues = check_question_evidence(diff, segment)
    self.assertTrue(any(i.kind == "missing-evidence" for i in issues))

  def test_normalizes_whitespace_when_matching(self):
    segment = "Cassius said,    'I will not\nforget this betrayal.'"
    diff = _good_diff()
    diff["questions_resolved"] = [
      {
        "id": "q-0001",
        "resolution": "Cassius openly threatens revenge.",
        "evidence_quote": "I will not forget this betrayal.",
      }
    ]
    self.assertEqual(check_question_evidence(diff, segment), [])

  def test_quote_too_short(self):
    segment = "x" * 500
    diff = _good_diff()
    diff["questions_resolved"] = [
      {"id": "q-0001", "resolution": "tiny", "evidence_quote": "short"}
    ]
    issues = check_question_evidence(diff, segment)
    self.assertTrue(any(i.kind == "missing-evidence" for i in issues))


class QuestionIdHygieneTests(unittest.TestCase):
  def test_existing_id_rejected_in_added(self):
    diff = _good_diff()
    issues = check_question_id_hygiene(diff, existing_question_ids={"q-0007"}, active_question_ids=set())
    self.assertTrue(any(i.kind == "duplicate-question-id" for i in issues))

  def test_unknown_id_rejected_in_resolved(self):
    diff = _good_diff()
    diff["questions_resolved"] = [
      {"id": "q-9999", "resolution": "yes.", "evidence_quote": "x" * 30}
    ]
    issues = check_question_id_hygiene(diff, existing_question_ids=set(), active_question_ids={"q-0001"})
    self.assertTrue(any(i.kind == "unknown-question-id" for i in issues))

  def test_resolved_id_can_match_active(self):
    diff = _good_diff()
    diff["questions_added"] = []
    diff["questions_resolved"] = [
      {"id": "q-0001", "resolution": "yes.", "evidence_quote": "x" * 30}
    ]
    issues = check_question_id_hygiene(diff, existing_question_ids={"q-0001"}, active_question_ids={"q-0001"})
    self.assertEqual(issues, [])

  def test_resolved_id_can_be_one_added_in_same_diff(self):
    diff = _good_diff()
    diff["questions_added"][0]["id"] = "q-0008"
    diff["questions_resolved"] = [
      {"id": "q-0008", "resolution": "yes.", "evidence_quote": "x" * 30}
    ]
    issues = check_question_id_hygiene(diff, existing_question_ids=set(), active_question_ids=set())
    self.assertEqual(issues, [])


class RunAllChecksTests(unittest.TestCase):
  def test_good_diff_passes(self):
    result = run_all_checks(_good_diff(), segment_text="any")
    self.assertTrue(result.ok, msg=str([i.as_dict() for i in result.issues]))
    self.assertEqual(result.issues, [])

  def test_schema_short_circuits(self):
    diff = _good_diff()
    diff["pages_created"][0]["path"] = "BAD/path.md"
    result = run_all_checks(diff, segment_text="any")
    self.assertFalse(result.ok)
    self.assertTrue(all(i.kind == "schema" for i in result.issues))

  def test_banned_phrase_failure_after_schema_pass(self):
    diff = _good_diff()
    diff["pages_created"][0]["summary"] = "As of chapter 5, the picture changes."
    result = run_all_checks(diff, segment_text="any")
    self.assertFalse(result.ok)
    self.assertTrue(any(i.kind == "banned-phrase" for i in result.issues))


if __name__ == "__main__":
  unittest.main()
