# Wiki Builder — Roadmap

The phased rollout for the autonomous wiki builder. Read [SPEC.md](SPEC.md) first — this doc tracks *what's done and what's next*; SPEC.md is the design contract.

## Status (last updated: Phase 0 complete)

| Phase | Status | Notes |
|---|---|---|
| 0. Foundation | ✅ done (commit `5cf857c`) | Spec, schema, segmenter, deterministic checks, 45 unit tests. No LLM calls. |
| 1. End-to-end on Red Rising book 1 | 🔜 next | Builds prompts, OpenClaw client wrapper, worker loop, runs ~15-20 segments on a small book. |
| 2. Q&A spoiler cutoff | pending | Replace chapter-based cutoff with segment-based one. |
| 3. Malazan omnibus | pending | Use virtual_chapters to slice 9.epub into per-novel chapters. |
| 4. Multi-EPUB series | pending | Wire `wiki_books` table for series spanning multiple book IDs. |

## Key decisions already made (do not re-debate without cause)

- **Snapshot-per-segment, not living-wiki-with-source-tracking.** Safer; easier to "roll back" the wiki for a reader at story_order N. The user explicitly confirmed they want snapshots ("I can select from the dropdown to see the state of the wiki for whatever chapter I want").
- **Always gpt-5.4 for supervisor (no Opus retries in v1).** User: "Let's just have always 5.4 via openclaw for right now."
- **Wikis live in `data/wikis/{slug}/`** — keep current location. User confirmed.
- **Defer the "request page expansion" tool for mini.** "The wiki doesn't need to be perfect since we are pulling raw text for summaries anyway."
- **Mini gets a fresh OpenClaw session per segment.** Continuity flows through (a) the wiki digest and (b) the previous segment's structured summary card. Within one segment, retries reuse the same session so mini sees its prior attempt + the supervisor's critique.
- **Paced worker.** `--segment-interval-seconds` default 90 to respect the user's OpenClaw monthly subscription budget (allotment is per-5-hour-window + weekly limit, not per-token).
- **Test target: Red Rising book 1 (5.epub).** Discard existing `data/wikis/red-rising/` (49 chapter-tagged snapshots from the manual pipeline). Archive as `data/wikis/red-rising.legacy/` before starting Phase 1.

## Phase 1 plan (next session)

Build the actual worker. New files expected:

- `app/wiki_builder/digest.py` — deterministic builder for the wiki digest the mini sees per segment. Pulls index.md, open-questions Active section, name-matched pages from the segment text, and characters from the previous summary card.
- `app/wiki_builder/prompts.py` — system prompts and message templates for mini and supervisor. Frozen JSON schemas.
- `app/wiki_builder/openclaw_client.py` — thin wrapper around the existing OpenClaw `/v1/responses` call (already wired in `app/rag.py`). Returns parsed JSON for diffs / supervisor verdicts.
- `app/wiki_builder/applier.py` — applies a validated diff to the wiki working tree (page create/update, sources block append, open-questions move/add/resolve), commits, tags `seg-NNNN`, writes summary card.
- `app/wiki_builder/worker.py` — the loop. Picks next pending segment, calls mini, runs deterministic checks, calls supervisor, applies-or-quarantines.
- `scripts/build_wiki.py` — CLI entry point: `python -m scripts.build_wiki --wiki-slug red-rising --book-id 5 --segment-interval-seconds 90 [--max-segments 3]`.
- `scripts/init_wiki.py` — one-shot initializer: creates the `wikis` row, the `wiki_books` row, the git repo with empty page directories, segments the book, populates `wiki_segments` with status=pending.

Phase 1 acceptance:
- A complete run on Red Rising book 1 with zero quarantined segments.
- Spot-check ~5 segments by hand: no banned phrases, sources blocks correct, open questions tracked sanely.
- Total Phase 1 OpenClaw spend should be modest (~15-20 segments × small mini + supervisor calls). Don't kick off the full run without confirming pacing.

## Phase 2 plan

- Replace `_safe_story_order` in `app/wiki.py:473` (currently infers chapter number from chapter text headers) with a segment-based lookup against `wiki_segments` keyed on `(book_id, end_position_index)`.
- Update `_select_safe_snapshot` to filter by `wiki_segments.story_order` rather than the old chapter-derived order.
- The Q&A retrieval path (`load_query_wiki_context` in app/wiki.py) otherwise stays the same — sections still live in `wiki_sections` keyed by snapshot_tag, just now `seg-NNNN` instead of `ch-NN`.
- Migration concern: if any chapter-tagged wiki is still ingested, retrieval needs to handle the older `ch-NN` tags too. Plan: archive `data/wikis/red-rising/` to `red-rising.legacy/` and don't ingest the legacy one once the new one is built.

## Open questions to revisit

- **Mini retries — same session or fresh?** Spec currently says retries reuse the mini's session within one segment. If we see retry loops where mini doubles down on the same mistake, switch to fresh-session-per-attempt and pass prior critique as user message in attempt 2+.
- **Legacy `wiki_sections` rows.** The Phase 0 schema is purely additive; old chapter-keyed sections still ingest fine via `app/wiki.py`. Phase 2 needs to either (a) delete legacy ingests when a new segment-based wiki for the same book exists, or (b) prefer the new one in retrieval. Decide before Phase 2 ships.
- **Page-expansion tool.** Deferred. Re-evaluate after Phase 1: if mini frequently hallucinates because the digest was too sparse on a relevant page, add the tool.

## Files to read on resume

If a future session needs to pick this up:

1. `app/wiki_builder/SPEC.md` — full design.
2. `app/wiki_builder/ROADMAP.md` — this file.
3. `app/wiki_builder/segmenter.py`, `checks.py` — what's already built.
4. `tests/test_segmenter.py`, `tests/test_checks.py` — invariants we must not break.
5. `app/wiki.py` — current wiki ingest + retrieval (will be modified in Phase 2).
6. `app/rag.py` — existing OpenClaw streaming client; reuse pattern in Phase 1's `openclaw_client.py`.
7. `skills/bookworm-wiki-builder/SKILL.md` — the manual ancestor of this pipeline; useful for prompt design (how the original instructions were phrased).
