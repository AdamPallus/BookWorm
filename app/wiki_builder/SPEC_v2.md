# Wiki Builder — SPEC v2

This is a redesign of the per-segment wiki update step. Phase 0 (foundation, schema, segmenter, deterministic checks) and Phase 1 (autonomous worker, OpenClaw client, applier, init/build CLIs) stay in place. What changes is what the mini sees, what it outputs, and how much freedom it has to restructure the wiki.

Read [SPEC.md](SPEC.md) first for the things that aren't changing (segmenter, snapshot-per-segment via git tags, OpenClaw routing, paced worker loop, wiki-as-git-repo at `data/wikis/{slug}/`).

---

## Why v2

The Phase 1 wiki on Red Rising book 1 is too superficial. Concrete failures:

- **Append-only character pages.** Each segment appends a fact bullet to existing pages instead of synthesising. Pages read as a chronological list, not a narrative.
- **No restructuring.** When chapter 30 reveals "Mustang" is Virginia au Augustus, the existing `mustang.md` just gets an appended fact. It's never renamed, the page never absorbs the new identity. This is the same root cause as the bullet-list problem: `detail_append` was a primary operation in the v1 schema, and the prompt never asked "is this still the right shape for this page?"
- **Anaemic open questions.** The prompt only asked "did this segment raise/answer questions?" It never asked "what would a thoughtful reader be wondering at this point in the book?"
- **No importance signal.** All character pages are flat. A reader at chapter 12 has no way to tell "these are the people the book is about" from "these are walk-on extras."

Diagnosis: the v1 design optimised for token efficiency (small input, small diff). It bought efficiency at the cost of the model's ability to think across context.

v2 inverts the trade. Token economy is no longer the primary constraint (the supervisor is gone, mini-only). We give the model the full prior wiki plus a meaningful slice of prior raw text and let it edit aggressively.

---

## Goals (and non-goals)

**Primary goal:** enable rich Q&A retrieval. The wiki exists so the Q&A system has good ground-truth pages to retrieve from. Human readability is secondary — most snapshots will never be opened by a human.

**Specific goals:**
- The model can rename, delete, and rewrite pages — not just append.
- Each snapshot reads as a coherent stand-alone wiki, not a revision stream.
- Open questions become a first-class signal of what a thoughtful reader is tracking.
- An importance signal exists per snapshot so the reader can find the "central" pages.

**Non-goals:**
- Prescribed page templates (no "early life / death" scaffolding).
- A sources/citations block on each page.
- A user-facing change log between snapshots.
- Token-economy optimisation (deferred to Phase 2b only when context fills).

---

## Architecture in one paragraph

For each pending segment in story_order, give the mini: (1) a system prompt describing the wiki's purpose and the editing constraint, (2) the entire current wiki as files, (3) all raw book text from segment 0 through the current segment. The mini outputs a list of file edit operations (write/rename/delete). The applier executes them, commits, tags `seg-NNNN`. Deterministic checks gate the apply: schema-valid ops, paths in the wiki subtree, banned-phrase scan, mandatory `## Summary` section. No supervisor, no per-segment summary card.

---

## The per-segment loop

```
for each pending segment in story_order:
  1. Reconstruct raw text [seg 0 .. current seg] from chunks table.
  2. Read the entire current wiki working tree into memory.
  3. Build mini input: SYSTEM_PROMPT + wiki dump + raw text + task instruction.
  4. Up to 3 attempts (same OpenClaw session):
     a. Call mini → JSON list of edit operations.
     b. Run deterministic checks on the ops.
     c. If pass: apply ops, commit, tag seg-NNNN, mark applied.
     d. If fail: pass issues back to mini, retry.
  5. After 3 failures: revert working tree, mark quarantined.
  6. Sleep --segment-interval-seconds.
```

The "fresh OpenClaw session per segment" rule from Phase 1 stays. Within one segment, retries reuse the session.

---

## Mini's input

Concatenated, in order:

1. **System prompt** (constant, frozen in `prompts.py`).
2. **Current wiki dump.** Every `*.md` file under `wiki/`, formatted as:
   ```
   ===== FILE: characters/darrow.md =====
   <file contents>
   ===== END FILE =====
   ```
   Empty wiki on first segment is just an empty section.
3. **Raw text so far.** All book text from segment 0 through the current segment, with chapter markers:
   ```
   ===== CHAPTER 6: Helldiver =====
   <chapter text>
   ```
4. **Task instruction.** "Update the wiki to reflect everything in the text up to and including the most recent chapter. Output a JSON object with an `edits` array."

Token budget rough check (Red Rising book 1):
- System prompt: ~3K
- Wiki at end of book 1: ~80 pages × 1.5KB ≈ 30K tokens
- Raw text: ~170K tokens
- Total per call: ~205K tokens

Comfortably inside gpt-5.4-mini's 1M context. Compaction is unnecessary for single books and most series; deferred to Phase 2b.

---

## Mini's output: edit operations

Single JSON object:

```json
{
  "edits": [
    {"op": "write_file", "path": "characters/virginia-au-augustus.md", "content": "# Virginia au Augustus\n\n## Summary\n..."},
    {"op": "rename_file", "from": "characters/mustang.md", "to": "characters/virginia-au-augustus.md", "content": "# Virginia au Augustus\n\n## Summary\n..."},
    {"op": "delete_file", "path": "characters/some-walk-on.md"}
  ]
}
```

Three operations:
- **write_file**(path, content): create or overwrite. The model passes the full new contents.
- **rename_file**(from, to, content): atomic rename + rewrite. The model passes the new contents (which can differ from the old file). Distinct from write+delete because it makes the *intent* of "this is the same subject under a new name" explicit, both for git's rename detection and for any future auditing.
- **delete_file**(path): remove. Used when a page turns out to refer to something that the model now thinks doesn't deserve its own page (e.g., a walk-on character we initially gave a page).

No in-place section patching. To change one section of a page, the model rewrites the whole file. Keeps the model's mental model simple and the applier dumb.

---

## Page format

Only one section is required:

```markdown
# Page Title

## Summary
A 1-2 sentence opener describing what this page is about. Used by Q&A retrieval as the page tagline.

<everything else is free-form>
```

After `## Summary` the model writes whatever sections fit the subject. A character page might be a single flowing narrative; a "metaphysics" concept page might have headings for distinct mechanics; a place page might be one paragraph. No mandatory subsections.

**No `## Sources` block.** Git history is the source of truth for what changed when.

**Path constraints.** All pages must match `^(characters|concepts|places|factions|events)/[a-z0-9][a-z0-9-]*\.md$`. The applier rejects anything else.

**Special files** (model may edit but not delete):
- `wiki/index.md` — see below.
- `wiki/open-questions.md` — see below.

---

## index.md: the importance signal

The model maintains `index.md` as a curated front page. Format:

```markdown
# <Book/Series Title>

## Who and what matters now

- **[Darrow](characters/darrow.md)** — one-line tagline.
- **[Eo](characters/eo.md)** — one-line tagline.
- **[The Society](concepts/the-society.md)** — one-line tagline.

## Recently introduced

- **[Cassius au Bellona](characters/cassius-au-bellona.md)** — one-line tagline.

## Background lore worth knowing

- **[Helldiver work](concepts/helldiving.md)**
- **[Mars and the Society](concepts/mars-society.md)**
```

The exact section headings aren't enforced — just the principle that index.md is the curated "where to start reading" page for someone opening the wiki at this snapshot. The model rewrites it each segment to reflect what's currently in focus.

A page's existence is the first importance signal (minor characters get no page); index.md is the second (which existing pages are central right now).

---

## open-questions.md

Free-form. The model rewrites it each segment as a list of currently-open questions a thoughtful reader would be tracking. No q-NNNN ids, no Active/Resolved sections required. The model is free to drop questions that have been answered (the answer should be in the relevant page; no need to maintain a "resolved" archive).

The system prompt frames this explicitly: open questions are the reader's experience of the book — what's the author hinting at, what's deliberately ambiguous, what's been set up but not paid off, what would the reader be wondering on this page. This is a first-class job, not bookkeeping.

---

## Constraints baked into the prompt (the spine)

The mini's system prompt is short and centred on one constraint, which we'll call the **grounding rule**:

> Every claim in the wiki must be grounded in the book text provided. If you cannot point to where in the text a claim comes from, do not write it. You may use general world knowledge only when the text uses specialist terms (e.g., "Helldiver" — you can read it as the text describes; you may not know what it means from elsewhere).

Spoiler-safety is not stated separately. It falls out of the grounding rule: if you can only write what's in the provided text, you cannot leak anything from later in the book.

Other prompt elements:

- **Goal:** the wiki exists so a Q&A system can answer reader questions accurately. Optimise for that.
- **Each snapshot stands alone.** No "as of chapter X" qualifiers, no "previously revealed" / "later", no meta-commentary about what changed since the last version.
- **Page structure is your call.** Write what fits the subject. The only required section is `## Summary` (1-2 sentences).
- **Existence and richness encode importance.** Don't create a page for every named character — minor walk-ons get nothing. The pages that exist should feel substantial.
- **Restructure freely.** Rename when a character's identity shifts; rewrite when your understanding of a place sharpens; delete when a page turns out to be a dead end. The git history preserves prior snapshots.
- **Don't prune dormant lore.** A page that was relevant in chapter 5 but hasn't come up since may still be load-bearing (e.g., Mars mining mechanics in Red Rising matter again much later). Rewrite when facts change or sharpen, not to trim what's quiet.
- **Open questions are the reader's mind.** What is the author hinting at? What has been set up but not paid off? What is the reader being given just enough information to wonder about?

Full prompt text lives in `prompts.py`.

---

## Applier behavior

The applier's contract:
1. Receive the parsed JSON edit list.
2. For each op, apply to the working tree (file I/O only — no logic).
3. `git add -A && git commit -m "<msg>" && git tag seg-NNNN`.

Commit message: `seg-NNNN` plus a one-line summary the applier auto-generates from the edits (e.g., `seg-0030: 3 created, 1 renamed, 0 deleted`). The model does not author commit messages or log entries.

The applier does NOT:
- Maintain a `## Sources` block.
- Append to `log.md`.
- Try to be clever about merging edits.

Both `log.md` and per-page `## Sources` blocks go away in v2. The git log is the audit trail.

---

## Deterministic checks

Run on the parsed JSON before applying. If any fails, retry with issues.

1. **Schema.** Top-level `{"edits": [...]}`. Each op has a valid `op` field and the right shape.
2. **Path constraints.** Every `path`/`from`/`to` is one of:
   - `index.md`
   - `open-questions.md`
   - `(characters|concepts|places|factions|events)/[a-z0-9][a-z0-9-]*\.md`
3. **No deleting protected files.** `delete_file` may not target `index.md` or `open-questions.md`.
4. **`## Summary` mandatory.** Every `write_file` and `rename_file` content must contain `\n## Summary\n` (or start with `## Summary\n` after the title), with non-empty body text following. Exception: `index.md` and `open-questions.md` are exempt.
5. **Banned phrases.** Same scan as v1: no "as of chapter X", "later revealed", "will eventually", etc. Run on every written file's content.
6. **Self-consistent ops.** No two ops touching the same path in the same edit list (catch model confusion early).

What's *gone* from v1's checks:
- Evidence-quote substring matching (no q-NNNN ids → no evidence quotes).
- q-id hygiene.
- summary_card schema check.

---

## State / schema changes from v1

- `wiki_segment_summaries` table — drop. The full wiki + full prior text in context replaces what the summary card was carrying. (Migration: leave the table in place but stop writing to it; can be dropped in a later cleanup commit.)
- `wiki_segments` row format — unchanged. Still has `status`, `attempts`, `snapshot_tag`, `last_error`.
- `wiki_segments.last_error` — still receives the JSON-stringified issues list on retries and quarantines.

The status values stay: `pending`, `applied`, `quarantined`. The `applied_with_warnings` value is no longer reachable (no supervisor) but stays in the schema.

---

## Open design questions (to decide before implementing)

These are choices I'd defer to user review:

1. **Bootstrapping.** First segment has an empty wiki. Should the system prompt give the model an example of a "good" page structure to get it started, or should we trust it to figure out the form from cold? My instinct: cold. The Coppermind-style "narrative not bullets" is achievable from the constraint "write what fits the subject" alone.
2. **Quarantine recovery.** v1 left holes when supervisor blocked; we addressed it with apply-with-warnings. v2 has no supervisor, so quarantines should be rare. If one happens (3× JSON parse failures or 3× banned-phrase violations), do we want any fallback, or accept the gap?
3. **Reader UI for "what changed."** v1 had log.md. v2 drops it. The HTML viewer's "new/updated" badges work from snapshot diff and don't need log.md. But: should the snapshot dropdown show *anything* about a snapshot beyond `seg-NNNN`? Probably not, but flagging.
4. **Empty wiki dump on first segment.** Format question only — does `===== END WIKI =====` immediately follow `===== BEGIN WIKI =====` with nothing between, or do we say "(no wiki yet)"? Trivial, but worth deciding once.

---

## Phase 2b: compaction (deferred)

When raw text grows past ~600K tokens (somewhere mid-Malazan), we need a compaction strategy. Sketch for now, design later:

- Always keep the last 3 chapters raw.
- Older text gets folded into a running narrative summary the mini also edits each segment (so it's curated, not mechanical).
- Summary lives in a non-user-facing file.

Don't build until needed.

---

## Pilot plan

1. Build v2 incrementally: prompts → applier ops → worker → CLI flag (`--engine v2`?).
2. Re-init `data/wikis/red-rising-v3/` and run the full book.
3. Spot-check the result against the Mustang/Virginia case and a handful of others. Compare to `red-rising-v2` directly.
4. If the pilot is good: deprecate the v1 engine, fold v2 into the default, drop the engine flag.
5. If not: revise spec, iterate.

Time budget: pilot run on Red Rising at ~50 segments × ~30s/segment ≈ 25 minutes wall clock plus pacing. Worth doing in one sitting.
