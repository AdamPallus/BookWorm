"""Thin git wrapper for accept/reject of the agent's working-tree edits.

The agent edits files directly via its Write/Edit tools. This module either
commits and tags the result (accept) or discards every uncommitted change
(reject). No file rewriting, no section management — git is the source of
truth.

The accept path is split into stage → commit so the worker can compute the
commit message from the staged file counts before committing.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

WIKI_AUTHOR_NAME = "Bookworm Wiki Builder"
WIKI_AUTHOR_EMAIL = "bookworm@wiki-builder"


@dataclass
class StagedDiff:
  files_added: int
  files_modified: int
  files_deleted: int
  files_renamed: int


@dataclass
class CommitResult:
  snapshot_tag: str
  commit_sha: str


def _run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
  return subprocess.run(
    ["git", *args],
    cwd=repo,
    check=check,
    capture_output=True,
    text=True,
  )


def stage_all(wiki_dir: Path) -> StagedDiff:
  """Stage every working-tree change and return a count summary."""
  wiki_repo = wiki_dir.parent
  if not (wiki_repo / ".git").exists():
    raise RuntimeError(f"not a git repo: {wiki_repo}")
  _run_git(wiki_repo, "add", "-A")
  out = _run_git(wiki_repo, "diff", "--cached", "--name-status").stdout
  counts = {"A": 0, "M": 0, "D": 0, "R": 0}
  for line in out.splitlines():
    if not line:
      continue
    code = line[0]
    counts[code] = counts.get(code, 0) + 1
  return StagedDiff(
    files_added=counts.get("A", 0),
    files_modified=counts.get("M", 0),
    files_deleted=counts.get("D", 0),
    files_renamed=counts.get("R", 0),
  )


def commit_and_tag(wiki_dir: Path, *, snapshot_tag: str, commit_message: str) -> CommitResult:
  """Commit staged changes and apply the snapshot tag. Caller stages first."""
  wiki_repo = wiki_dir.parent
  _run_git(
    wiki_repo,
    "-c", f"user.name={WIKI_AUTHOR_NAME}",
    "-c", f"user.email={WIKI_AUTHOR_EMAIL}",
    "commit", "-m", commit_message,
  )
  _run_git(wiki_repo, "tag", snapshot_tag)
  sha = _run_git(wiki_repo, "rev-parse", "HEAD").stdout.strip()
  return CommitResult(snapshot_tag=snapshot_tag, commit_sha=sha)


def reject(wiki_dir: Path) -> None:
  """Discard the agent's pending edits. Used on reject and quarantine."""
  wiki_repo = wiki_dir.parent
  _run_git(wiki_repo, "reset", "--hard", "HEAD", check=False)
  _run_git(wiki_repo, "clean", "-fd", check=False)
