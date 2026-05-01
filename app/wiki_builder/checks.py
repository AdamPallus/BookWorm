"""Deterministic checks on the wiki working tree after the agent edited it.

The agent uses its Write/Edit tools directly, so checks operate on git status:
which files are added/modified/deleted, and what they now contain.

Pass = harness commits and tags. Fail = harness reverts and tells the agent
what to fix. The agent never sees this code; it only sees the rejection
message the harness builds from the issue list.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

PAGE_DIRS = ("characters", "concepts", "places", "factions", "events")
PAGE_PATH_RE = re.compile(
  r"^(?:" + "|".join(PAGE_DIRS) + r")/[a-z0-9][a-z0-9-]*\.md$"
)
ROOT_FILES = frozenset(("index.md", "open-questions.md"))
PROTECTED_FILES = frozenset(("index.md", "open-questions.md"))

BANNED_PATTERNS = [
  ("as-of-chapter", re.compile(r"\bas\s+of\s+(?:ch(?:apter)?\.?\s*\d+)\b", re.IGNORECASE)),
  ("currently-as-of", re.compile(r"\bcurrent(?:ly)?\s+as\s+of\b", re.IGNORECASE)),
  ("inline-ch-citation", re.compile(r"\(\s*ch\.\s*\d+\s*\)", re.IGNORECASE)),
  ("bare-ch-citation", re.compile(r"(?<![A-Za-z])ch\.\s*\d+\b", re.IGNORECASE)),
  ("later-revealed", re.compile(r"\blater\s+(?:revealed|shown|discovered|learned)\b", re.IGNORECASE)),
  ("will-eventually", re.compile(
    r"\bwill\s+(?:later|eventually|soon)\s+(?:be\s+)?(?:revealed|reveal|become|turn\s+out|happen)\b",
    re.IGNORECASE,
  )),
  ("next-chapter", re.compile(r"\bin\s+the\s+next\s+chapter\b", re.IGNORECASE)),
  ("eventually-becomes", re.compile(r"\beventually\s+(?:becomes|reveals|turns\s+out)\b", re.IGNORECASE)),
]

SUMMARY_RE = re.compile(r"^##\s+Summary\s*$\n(.+?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


@dataclass
class CheckIssue:
  kind: str
  where: str
  detail: str


@dataclass
class CheckResult:
  ok: bool
  issues: List[CheckIssue]


def _run_git(repo: Path, *args: str) -> str:
  result = subprocess.run(
    ["git", *args],
    cwd=repo,
    check=True,
    capture_output=True,
    text=True,
  )
  return result.stdout


def _status_entries(wiki_repo: Path) -> List[Tuple[str, str]]:
  """Return (status_code, path) for each working-tree change.

  Status code is the two-char porcelain code: '??' untracked, ' M' modified,
  ' D' deleted, etc. We don't enable rename detection — the agent renaming
  via mv shows as a delete + untracked-add, which we handle independently.
  """
  out = _run_git(wiki_repo, "status", "--porcelain", "-z", "--untracked-files=all")
  entries: List[Tuple[str, str]] = []
  for tok in out.split("\0"):
    if not tok:
      continue
    if len(tok) < 4:
      continue
    status = tok[:2]
    path = tok[3:]
    entries.append((status, path))
  return entries


def _rel_to_wiki(path: str) -> Optional[str]:
  if path.startswith("wiki/"):
    return path[len("wiki/"):]
  return None


def _valid_path(rel: str) -> bool:
  if rel in ROOT_FILES:
    return True
  return bool(PAGE_PATH_RE.match(rel))


def _has_summary(content: str) -> bool:
  m = SUMMARY_RE.search(content)
  if not m:
    return False
  return bool(m.group(1).strip())


def _scan_banned(text: str, where: str, issues: List[CheckIssue]) -> None:
  for label, pattern in BANNED_PATTERNS:
    m = pattern.search(text)
    if m:
      issues.append(CheckIssue(
        kind="banned-phrase",
        where=where,
        detail=f"{label}: {m.group(0)!r}",
      ))


def check_working_tree(wiki_dir: Path) -> CheckResult:
  """Run all checks on the wiki working tree. wiki_dir is .../{slug}/wiki."""
  wiki_repo = wiki_dir.parent
  issues: List[CheckIssue] = []

  entries = _status_entries(wiki_repo)
  if not entries:
    issues.append(CheckIssue(
      kind="no-changes",
      where="<tree>",
      detail="agent returned without modifying any files",
    ))
    return CheckResult(ok=False, issues=issues)

  for status, path in entries:
    rel = _rel_to_wiki(path)
    if rel is None:
      issues.append(CheckIssue(
        kind="path-outside-wiki",
        where=path,
        detail=f"change outside wiki/ subtree: {path}",
      ))
      continue

    is_deletion = "D" in status
    if is_deletion:
      if rel in PROTECTED_FILES:
        issues.append(CheckIssue(
          kind="protected-file-deleted",
          where=rel,
          detail=f"{rel} must not be deleted",
        ))
      continue

    if not _valid_path(rel):
      issues.append(CheckIssue(
        kind="invalid-path",
        where=rel,
        detail=f"path does not match allowed shape: {rel}",
      ))
      continue

    abs_path = wiki_repo / path
    try:
      content = abs_path.read_text(encoding="utf-8")
    except FileNotFoundError:
      issues.append(CheckIssue(
        kind="missing-file",
        where=rel,
        detail="file not found on disk despite git status entry",
      ))
      continue
    except UnicodeDecodeError as e:
      issues.append(CheckIssue(
        kind="encoding",
        where=rel,
        detail=f"file is not utf-8: {e}",
      ))
      continue

    if rel not in ROOT_FILES and not _has_summary(content):
      issues.append(CheckIssue(
        kind="missing-summary",
        where=rel,
        detail="page must contain a non-empty `## Summary` section",
      ))

    if rel.endswith(".md"):
      _scan_banned(content, rel, issues)

  return CheckResult(ok=not issues, issues=issues)
