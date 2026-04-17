# Wiki Builder — Spec (v1)

This document defines the data model, file format, prompt contract, and quality gates for the Bookworm wiki builder pipeline. It is intentionally narrow: every rule here either defends spoiler safety, prevents the historical chunk-merge / "as of chapter X" bugs, or keeps the human-readable wiki readable.

The current `bookworm-wiki-builder` skill (`skills/bookworm-wiki-builder/SKILL.md`) is the manual-orchestration ancestor of this pipeline. This spec replaces it with code where the skill leaned on judgment calls.

## Concepts

### Wiki

A wiki is a single git-versioned markdown repository that covers one or more books. A wiki is owned by a *series* (one row in `wikis`). A series may contain one EPUB (most cases) or many (Malazan etc.). In the v1 schema we model the wiki as the entity — `wiki_books` joins it to one or more `book_id`s ordered by `series_index`.

### Segment

A segment is the unit of wiki update. Properties:

- Belongs to one virtual chapter of one book.
- Token count is in `[MIN_SEGMENT_TOKENS, MAX_SEGMENT_TOKENS]`, target `TARGET_SEGMENT_TOKENS`.
- Never crosses a chapter boundary — keeps spoiler reasoning simple.
- Has a globally-monotonic `story_order` across the entire wiki (across all books in the series).
- Has a `snapshot_tag` of the form `seg-NNNN` once applied. Tags are unique per wiki.

Defaults (tunable per-wiki):

```
TARGET_SEGMENT_TOKENS = 7000
MIN_SEGMENT_TOKENS    = 3000
MAX_SEGMENT_TOKENS    = 12000
```

### Snapshot

A snapshot is the wiki state at a specific `story_order`. It is materialized as a git tag `seg-NNNN` plus rows in `wiki_sections` keyed on `(wiki_id, snapshot_tag)`. Each segment that successfully applies produces exactly one snapshot. A reader at story_order N sees the snapshot from the highest segment whose `end_position_index ≤ reader_position_index`.

## Segmenter algorithm (deterministic, no LLM)

```
segment_chapter(chapter_text, chapter_index) -> list[Segment]:
  total = count_tokens(chapter_text)
  if total <= MAX_SEGMENT_TOKENS:
    return [single segment spanning the whole chapter]

  breaks = find_scene_breaks(chapter_text)
  if breaks:
    pieces = pack_pieces(chapter_text, breaks,
                        target=TARGET_SEGMENT_TOKENS,
                        min=MIN_SEGMENT_TOKENS,
                        max=MAX_SEGMENT_TOKENS)
  if not breaks or no valid packing:
    pieces = split_by_paragraphs(chapter_text,
                                target=TARGET_SEGMENT_TOKENS,
                                min=MIN_SEGMENT_TOKENS,
                                max=MAX_SEGMENT_TOKENS)
  return [Segment(chapter_index, i, start, end, count_tokens(slice))
          for i, (start, end) in enumerate(pieces)]
```

Invariants checked in tests:

1. Concatenating segment slices reproduces the chapter text.
2. No segment is below `MIN_SEGMENT_TOKENS` *unless* the entire chapter is below it (then one short segment is allowed).
3. No segment exceeds `MAX_SEGMENT_TOKENS` unless splitting was impossible (no paragraph boundaries — last-resort hard cut).
4. No segment crosses the chapter boundary (segments belong to exactly one chapter).

### Scene-break detection

Match on a line that contains *only* one of these tokens, surrounded by blank lines:

- `***` or `* * *`
- `# # #` or `###`
- `---` or longer
- `——` or longer (em-dashes)
- `×`

Regex (multiline):

```
^[ \t]*(\*\s*\*\s*\*\*?|#\s*#\s*#|-{3,}|—{2,}|×)[ \t]*$
```

The break position is the start of the empty line *after* the marker, so the break belongs to the segment that follows.

### Paragraph splitting fallback

If scene breaks don't yield valid packings, walk paragraphs (separated by `\n\n+`) and accumulate until the next paragraph would push past `TARGET_SEGMENT_TOKENS`. If the accumulated chunk is ≥ `MIN_SEGMENT_TOKENS`, emit it; otherwise keep going to `MAX_SEGMENT_TOKENS`. As a last resort (one paragraph longer than `MAX_SEGMENT_TOKENS`), do a hard token-based cut on whitespace boundaries — emit a warning in segmenter logs.

## Wiki on-disk format

```
data/wikis/{wiki-slug}/
  .git/
  wiki/
    index.md
    open-questions.md
    log.md
    characters/{slug}.md
    concepts/{slug}.md
    places/{slug}.md
    factions/{slug}.md
    events/{slug}.md
  raw/                 # not committed; produced by extractor
    chapters/
  build-status.json    # mirrors wiki_segments table
  snapshots.json       # produced by snapshot_export.py for ingestion
```

### Page format

```markdown
# {Title}

## Summary
{1–3 sentences. Always written from "the reader has just finished segment N
and knows everything from segments 1..N." Never qualifies with "as of ch. X"
or "later revealed". The snapshot tag IS the as-of marker.}

## Detail
{Multi-paragraph narrative. No inline (ch. N) citations.}

## Sources
- seg: {N}
  added: "{≤140-char description of what this segment contributed to this page}"
- seg: {N+k}
  added: "..."
```

### Open questions page

```markdown
# Open Questions

## Active

### q-0001 — {Short title}
- raised in: seg-0005
- text: {The actual question, written naturally.}

## Resolved

### q-0001 — {Short title}
- raised in: seg-0005
- resolved in: seg-0023
- resolution: "{≤200-char summary, including a verbatim quote from the segment text proving it.}"
```

Question IDs are stable. When a question is resolved it is moved from `## Active` to `## Resolved` in place — the ID does not change.

## Worker contract

Per segment, the worker:

1. Build the wiki digest (deterministic; no LLM).
2. Open a fresh OpenClaw session for the mini agent.
3. Send: system prompt + segment raw text + digest + previous segment's summary card.
4. Mini returns a JSON diff (schema below).
5. Run deterministic checks on the diff. If they fail, retry within the same mini session with the failure list appended as a user message. Repeat up to 3 attempts total.
6. On all-deterministic-checks pass: run supervisor (5.4 via OpenClaw) on `(diff, segment_text, relevant_prior_wiki)`. Supervisor returns `{ok, issues}`. If not ok, retry within the same mini session with supervisor's issues appended.
7. On supervisor pass: apply the diff to the working tree, git commit, git tag `seg-NNNN`, write the segment's summary card to `wiki_segment_summaries`, update `wiki_segments.status='applied'`, re-embed touched sections.
8. On 3-strikes failure: revert working tree, mark `wiki_segments.status='quarantined'`, surface in UI.

Worker pacing: a `--segment-interval-seconds` flag (default 90) sleeps between segments.

## Diff schema (mini's output)

```json
{
  "summary_card": {
    "key_events": ["string", ...],
    "active_characters": ["page_path", ...],
    "new_facts": ["string", ...],
    "questions_added": ["q-NNNN", ...],
    "questions_resolved": ["q-NNNN", ...]
  },
  "pages_created": [
    {
      "path": "characters/some-name.md",
      "title": "Some Name",
      "summary": "1–3 sentences",
      "detail": "multi-paragraph markdown",
      "source_note": "≤140-char description of what this segment contributed"
    }
  ],
  "pages_updated": [
    {
      "path": "characters/existing.md",
      "summary": "new full summary (replaces) OR null to keep",
      "detail_append": "markdown to append to detail OR null",
      "detail_replace": "full detail markdown OR null (mutually exclusive with detail_append)",
      "source_note": "≤140-char description"
    }
  ],
  "questions_added": [
    {
      "id": "q-NNNN",
      "title": "short title",
      "text": "the question"
    }
  ],
  "questions_resolved": [
    {
      "id": "q-NNNN",
      "resolution": "≤200-char summary",
      "evidence_quote": "verbatim substring from segment text"
    }
  ],
  "log_entry": "1–3 sentences for log.md describing what changed in this segment"
}
```

## Deterministic supervisor checks

Run on the diff before invoking the LLM supervisor. Cheap, always-on, no token cost.

### Diff schema validation
- All required fields present, types correct.
- `summary_card.key_events` is non-empty.
- `pages_updated` entries set exactly one of `detail_append` / `detail_replace` / both null.
- All `path` values match `^(characters|concepts|places|factions|events)/[a-z0-9-]+\.md$`.
- All `q-NNNN` IDs match the format.

### Banned phrase scan
For every prose field (`summary`, `detail`, `detail_append`, `detail_replace`, `source_note`, `log_entry`):

```
\bas\s+of\s+(?:ch(?:apter)?\.?\s*\d+)\b
\bcurrent(?:ly)?\s+as\s+of\b
\(\s*ch\.\s*\d+\s*\)
\bch\.\s*\d+\b
\blater\s+(?:revealed|shown|discovered|learned)\b
\bwill\s+(?:later|eventually|soon)\s+(?:be\s+)?(?:revealed|reveal|become|turn\s+out|happen)\b
\bin\s+the\s+next\s+chapter\b
\beventually\s+(?:becomes|reveals|turns\s+out)\b
```

Any match → reject with the offending phrase + field name.

### Source attribution check
Every entry in `pages_created` and `pages_updated` must have a non-empty `source_note`. Length ≤ 140 chars. (The worker writes the actual `## Sources` block; the LLM provides only the description.)

### Question resolution evidence check
For each `questions_resolved[i]`:
- `evidence_quote` must be a substring of the segment raw text (case-insensitive, whitespace-collapsed).
- `evidence_quote` length ≥ 12 chars and ≤ 320 chars.

If the substring isn't found, reject with the offending quote.

### Question ID hygiene
- Questions in `questions_added` must use IDs not currently present anywhere in the wiki.
- Questions in `questions_resolved` must currently exist in the open-questions page's Active section.

## LLM supervisor checks

Supervisor is given: the diff JSON, the segment raw text, and the digest the mini saw. Returns:

```json
{
  "ok": false,
  "issues": [
    {
      "kind": "spoiler|hallucination|misattribution|other",
      "where": "pages_updated[0].detail_append",
      "explanation": "...",
      "offending_text": "..."
    }
  ]
}
```

Supervisor is told:
- The mini's job is to update the wiki using only the segment text and prior wiki content.
- Reject anything that introduces facts not supported by the segment or prior wiki.
- Reject anything that uses general knowledge of the book/series.
- Reject "as of chapter X" qualifiers, "later revealed", or chapter citations.
- Resolved questions must have an evidence quote that actually appears in the segment.
- The Detail section must read as confident present-tense narrative (no hedging like "we don't yet know if…" — open questions belong on the questions page, not in Detail).

The supervisor's prompt and exact schema live in `app/wiki_builder/prompts.py` (Phase 1).

## Q&A spoiler cutoff (Phase 2 change)

Replace `_safe_story_order` (currently inferred from chapter text headers) with:

```
last_completed_segment(book_id, position_index) =
  SELECT * FROM wiki_segments
  WHERE book_id = ? AND end_position_index <= ?
  ORDER BY story_order DESC LIMIT 1
```

Then `safe_story_order = last_completed_segment.story_order`. The Q&A retrieval loads sections from snapshots with `story_order ≤ safe_story_order`.

If no segment is fully completed yet (reader is mid-first-segment), the Q&A returns no wiki context — falls back to raw excerpts only.

## Token / budget guardrails

These are conventions, not enforced limits:

| Component             | Per-call (target) |
|-----------------------|-------------------|
| Mini input total      | ≤ 16K tokens      |
| Mini output           | ≤ 3K tokens       |
| Supervisor input      | ≤ 12K tokens      |
| Supervisor output     | ≤ 1K tokens       |

The wiki digest is built to fit:
- `index.md`: full, capped at 2K tokens (truncate-with-marker if larger).
- `open-questions.md` Active section: full, capped at 1K tokens.
- Pages whose name or aliases appear in the segment text: top 8 by name-occurrence count, *Summary section only*, capped at 3K tokens combined.
- Pages named in the previous summary card's `active_characters`: top 5, Summary only, capped at 1K tokens.

## Failure-mode catalog (the ones we are explicitly defending against)

1. **Chunk merge** — multiple chapters bundled in one update. Defended by: worker processes one segment at a time atomically. The mini prompt receives one segment's text and is told that's the only material it may use.
2. **Spoiler from training data** — LLM uses its knowledge of the book. Defended by: system prompt forbids it; supervisor flags claims not in segment text or prior wiki.
3. **Spoiler from out-of-band text** — LLM uses content from outside the segment. Defended by: digest only contains prior wiki, never raw text from later chapters; mini doesn't have tool access in v1.
4. **"As of chapter X" qualifiers** — LLM hedges in prose. Defended by: deterministic banned-phrase scan + supervisor rule.
5. **Stale "as of" notes** — page says "current as of ch 5" but contains ch 6 content. Defended by: there is no "as of" prose anywhere; the snapshot tag carries that meaning.
6. **Lost open questions** — questions never get resolved as text answers them. Defended by: digest always includes the Active section of open-questions; mini is prompted to scan for resolutions; supervisor verifies the resolution evidence is in the segment.
7. **Inconsistent open-question IDs** — same question raised twice with different IDs. Defended by: deterministic ID-hygiene check; mini sees current IDs in the digest.
