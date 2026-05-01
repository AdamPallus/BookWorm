"""Run the wiki builder worker for a single wiki.

Usage:
  python -m scripts.build_wiki --wiki-slug red-rising-v2 \
    [--segment-interval-seconds 90] \
    [--max-segments 3] \
    [--mini-model gpt-5.4-mini]

The worker resumes from the next pending segment (story_order ASC). Quarantined
segments are skipped on the next run unless their status is reset manually.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import db  # noqa: E402
from app.wiki_builder import openclaw_client  # noqa: E402
from app.wiki_builder.worker import WorkerConfig, run_worker  # noqa: E402


def _setup_logging(verbose: bool) -> None:
  level = logging.DEBUG if verbose else logging.INFO
  logging.basicConfig(
    level=level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
  )


def main() -> None:
  parser = argparse.ArgumentParser(description="Run the Bookworm wiki builder worker.")
  parser.add_argument("--wiki-slug", required=True)
  parser.add_argument("--segment-interval-seconds", type=float, default=90.0)
  parser.add_argument("--max-segments", type=int, default=None)
  parser.add_argument("--mini-model", default=openclaw_client.DEFAULT_MINI_MODEL)
  parser.add_argument("--read-timeout-seconds", type=float, default=600.0)
  parser.add_argument(
    "--reset-session",
    action="store_true",
    help="Force a brand-new OpenClaw session even if one is already persisted on the wiki.",
  )
  parser.add_argument("--verbose", action="store_true")
  args = parser.parse_args()

  _setup_logging(args.verbose)

  conn = db.connect()
  row = conn.execute("SELECT id FROM wikis WHERE slug = ?", (args.wiki_slug,)).fetchone()
  if not row:
    raise SystemExit(f"no wiki with slug {args.wiki_slug!r}; did you run init_wiki?")
  wiki_id = row[0]

  config = WorkerConfig(
    segment_interval_seconds=args.segment_interval_seconds,
    max_segments=args.max_segments,
    mini_model=args.mini_model,
    read_timeout_seconds=args.read_timeout_seconds,
    reset_session=args.reset_session,
  )
  processed = run_worker(conn, wiki_id, config)
  print(f"[build] processed {processed} segments")


if __name__ == "__main__":
  main()
