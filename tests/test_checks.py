"""Tests for the working-tree-based wiki checks.

Each test sets up a fake wiki repo with a baseline commit, mutates the working
tree to simulate what the agent would do, then runs check_working_tree.
"""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.wiki_builder.checks import check_working_tree


PAGE_BODY = (
  "# Sample\n"
  "\n"
  "## Summary\n"
  "A short tagline that satisfies the summary check.\n"
  "\n"
  "## Detail\n"
  "More content.\n"
)


def _git(repo: Path, *args: str) -> None:
  subprocess.run(
    ["git", *args],
    cwd=repo, check=True, capture_output=True, text=True,
  )


def _init_wiki() -> Path:
  tmp = Path(tempfile.mkdtemp(prefix="wikitest-"))
  repo = tmp / "wiki-repo"
  repo.mkdir()
  _git(repo, "init", "-q", "-b", "main")
  _git(repo, "config", "user.name", "Test")
  _git(repo, "config", "user.email", "test@example.com")
  wiki = repo / "wiki"
  wiki.mkdir()
  for sub in ("characters", "concepts", "places", "factions", "events"):
    (wiki / sub).mkdir()
  (wiki / "index.md").write_text("# Index\n", encoding="utf-8")
  (wiki / "open-questions.md").write_text("# Open Questions\n", encoding="utf-8")
  _git(repo, "add", "-A")
  _git(repo, "commit", "-q", "-m", "init")
  return wiki


class CheckWorkingTreeTest(unittest.TestCase):
  def setUp(self):
    self.wiki = _init_wiki()
    self.repo = self.wiki.parent

  def tearDown(self):
    shutil.rmtree(self.repo.parent, ignore_errors=True)

  def test_no_changes_fails(self):
    result = check_working_tree(self.wiki)
    self.assertFalse(result.ok)
    self.assertEqual(result.issues[0].kind, "no-changes")

  def test_valid_new_page_passes(self):
    (self.wiki / "characters" / "darrow.md").write_text(PAGE_BODY, encoding="utf-8")
    result = check_working_tree(self.wiki)
    self.assertTrue(result.ok, msg=[i.detail for i in result.issues])

  def test_invalid_path_fails(self):
    (self.wiki / "characters" / "Bad_Name.md").write_text(PAGE_BODY, encoding="utf-8")
    result = check_working_tree(self.wiki)
    self.assertFalse(result.ok)
    self.assertTrue(any(i.kind == "invalid-path" for i in result.issues))

  def test_disallowed_directory_fails(self):
    (self.wiki / "people").mkdir()
    (self.wiki / "people" / "darrow.md").write_text(PAGE_BODY, encoding="utf-8")
    result = check_working_tree(self.wiki)
    self.assertFalse(result.ok)
    self.assertTrue(any(i.kind == "invalid-path" for i in result.issues))

  def test_missing_summary_fails(self):
    (self.wiki / "characters" / "darrow.md").write_text(
      "# Darrow\n\nNo summary here.\n", encoding="utf-8",
    )
    result = check_working_tree(self.wiki)
    self.assertFalse(result.ok)
    self.assertTrue(any(i.kind == "missing-summary" for i in result.issues))

  def test_index_md_does_not_need_summary(self):
    (self.wiki / "index.md").write_text(
      "# Wiki Index\n\nNo summary section here, that's fine.\n", encoding="utf-8",
    )
    result = check_working_tree(self.wiki)
    self.assertTrue(result.ok, msg=[i.detail for i in result.issues])

  def test_protected_file_deletion_fails(self):
    (self.wiki / "open-questions.md").unlink()
    result = check_working_tree(self.wiki)
    self.assertFalse(result.ok)
    self.assertTrue(any(i.kind == "protected-file-deleted" for i in result.issues))

  def test_unprotected_file_deletion_ok(self):
    page = self.wiki / "characters" / "darrow.md"
    page.write_text(PAGE_BODY, encoding="utf-8")
    _git(self.repo, "add", "-A")
    _git(self.repo, "commit", "-q", "-m", "add darrow")
    page.unlink()
    result = check_working_tree(self.wiki)
    self.assertTrue(result.ok, msg=[i.detail for i in result.issues])

  def test_banned_phrase_fails(self):
    body = PAGE_BODY + "\nThis is later revealed in a twist.\n"
    (self.wiki / "characters" / "darrow.md").write_text(body, encoding="utf-8")
    result = check_working_tree(self.wiki)
    self.assertFalse(result.ok)
    self.assertTrue(any(i.kind == "banned-phrase" for i in result.issues))

  def test_outside_wiki_fails(self):
    (self.repo / "outside.md").write_text(PAGE_BODY, encoding="utf-8")
    (self.wiki / "characters" / "darrow.md").write_text(PAGE_BODY, encoding="utf-8")
    result = check_working_tree(self.wiki)
    self.assertFalse(result.ok)
    self.assertTrue(any(i.kind == "path-outside-wiki" for i in result.issues))


if __name__ == "__main__":
  unittest.main()
