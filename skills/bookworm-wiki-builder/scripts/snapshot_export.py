#!/usr/bin/env python3
"""
Export wiki snapshots at each chapter tag for Bookworm runtime integration.

Usage:
    python snapshot_export.py <wiki-repo-dir> --output snapshots.json

For each chapter tag (ch-01, ch-02, ..., or b01-ch-01, etc.), this script
extracts the full wiki state and writes it to a JSON file that the Bookworm
runtime can load into its database for fast query-time access.

Output format:
{
  "book_title": "Red Rising",
  "book_author": "Pierce Brown",
  "tags": ["ch-01", "ch-02", ...],
  "snapshots": {
    "ch-01": {
      "tag": "ch-01",
      "commit": "abc123...",
      "timestamp": "2026-04-08T12:00:00",
      "pages": {
        "wiki/index.md": { "path": "...", "content": "...", "frontmatter": {...} },
        "wiki/characters/darrow.md": { ... },
        ...
      },
      "stats": { "total_pages": 5, "characters": 2, "concepts": 1, ... }
    },
    ...
  }
}
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime


def run_git(args: list[str], cwd: str) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Git error: {result.stderr.strip()}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def get_chapter_tags(cwd: str) -> list[str]:
    """Get all chapter tags, sorted by chapter number."""
    raw = run_git(["tag", "-l"], cwd)
    if not raw:
        return []

    tags = raw.split("\n")
    # Filter to chapter tags (ch-NN or bNN-ch-NN)
    chapter_tags = [t for t in tags if re.match(r"(b\d+-)?ch-\d+", t)]

    # Sort by book number then chapter number
    def sort_key(tag: str):
        # Handle b01-ch-05 or ch-05
        parts = re.match(r"(?:b(\d+)-)?ch-(\d+)", tag)
        if parts:
            book = int(parts.group(1) or 0)
            chapter = int(parts.group(2))
            return (book, chapter)
        return (0, 0)

    return sorted(chapter_tags, key=sort_key)


def get_wiki_files_at_tag(tag: str, cwd: str) -> list[str]:
    """List all wiki/ files at a given tag."""
    raw = run_git(["ls-tree", "-r", "--name-only", tag, "--", "wiki/"], cwd)
    if not raw:
        return []
    return [f for f in raw.split("\n") if f.endswith(".md")]


def get_file_at_tag(tag: str, filepath: str, cwd: str) -> str:
    """Read a file's content at a specific tag."""
    return run_git(["show", f"{tag}:{filepath}"], cwd)


def parse_frontmatter(content: str) -> dict:
    """Extract simple YAML frontmatter from markdown content."""
    match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not match:
        return {}

    fm = {}
    current_key = None
    for raw_line in match.group(1).split("\n"):
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        # Handle very simple multiline list items:
        # key:
        #   - item
        if line.startswith("  - ") and current_key and isinstance(fm.get(current_key), list):
            fm[current_key].append(line[4:].strip().strip("'\""))
            continue

        if ":" not in line:
            current_key = None
            continue

        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        current_key = key

        if not value:
            fm[key] = []
            continue

        if value.startswith("[") and value.endswith("]"):
            items = value[1:-1].split(",")
            fm[key] = [item.strip().strip("'\"") for item in items if item.strip()]
        else:
            fm[key] = value.strip("'\"")
    return fm


def get_commit_for_tag(tag: str, cwd: str) -> tuple[str, str]:
    """Get commit hash and timestamp for a tag."""
    commit = run_git(["rev-list", "-1", tag], cwd)
    timestamp = run_git(["log", "-1", "--format=%aI", tag], cwd)
    return commit, timestamp


def categorize_path(path: str) -> str:
    """Determine the category of a wiki file from its path."""
    parts = path.split("/")
    filename = parts[-1]

    if path == "wiki/index.md" or filename == "index.md":
        return "index"
    if filename in ("open-questions.md", "log.md"):
        return "meta"

    if len(parts) >= 3:
        category = parts[1]  # wiki/<category>/<file>.md
        if category in ("characters", "concepts", "places", "factions", "events"):
            return category

    return "other"


def export_snapshots(repo_dir: str, output_path: str) -> None:
    """Export wiki snapshots for all chapter tags."""
    repo_dir = os.path.abspath(repo_dir)

    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        print(f"Error: {repo_dir} is not a git repository", file=sys.stderr)
        sys.exit(1)

    # Try to load metadata
    metadata_path = os.path.join(repo_dir, "raw", "metadata.json")
    book_title = "Unknown"
    book_author = "Unknown"
    if os.path.isfile(metadata_path):
        with open(metadata_path) as f:
            meta = json.load(f)
            book_title = meta.get("title", "Unknown")
            book_author = meta.get("author", "Unknown")

    tags = get_chapter_tags(repo_dir)
    if not tags:
        print("No chapter tags found in repository.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(tags)} chapter tags for '{book_title}' by {book_author}")

    snapshots = {}
    for i, tag in enumerate(tags):
        commit, timestamp = get_commit_for_tag(tag, repo_dir)
        wiki_files = get_wiki_files_at_tag(tag, repo_dir)

        pages = {}
        stats = {"total_pages": 0, "characters": 0, "concepts": 0,
                 "places": 0, "factions": 0, "events": 0, "other": 0}

        for filepath in wiki_files:
            content = get_file_at_tag(tag, filepath, repo_dir)
            frontmatter = parse_frontmatter(content)
            category = categorize_path(filepath)

            pages[filepath] = {
                "path": filepath,
                "content": content,
                "frontmatter": frontmatter,
                "category": category,
            }

            stats["total_pages"] += 1
            if category in stats:
                stats[category] += 1

        snapshots[tag] = {
            "tag": tag,
            "commit": commit,
            "timestamp": timestamp,
            "pages": pages,
            "stats": stats,
        }

        print(f"  [{i+1:3d}/{len(tags)}] {tag}: {stats['total_pages']} pages "
              f"({stats['characters']}C {stats['concepts']}K {stats['places']}P "
              f"{stats['factions']}F {stats['events']}E)")

    result = {
        "book_title": book_title,
        "book_author": book_author,
        "exported_at": datetime.now().isoformat(),
        "total_tags": len(tags),
        "tags": tags,
        "snapshots": snapshots,
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"\nExported {len(tags)} snapshots to {output_path} ({file_size_mb:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(
        description="Export wiki snapshots at each chapter tag for Bookworm runtime"
    )
    parser.add_argument("repo_dir", help="Path to the wiki git repository")
    parser.add_argument("--output", "-o", required=True,
                        help="Output path for the snapshots JSON file")
    args = parser.parse_args()

    export_snapshots(args.repo_dir, args.output)


if __name__ == "__main__":
    main()
