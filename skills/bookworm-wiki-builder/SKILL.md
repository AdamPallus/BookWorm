---
name: bookworm-wiki-builder
description: >
  Build a git-versioned, spoiler-safe markdown wiki from an EPUB book or series.
  The wiki is compiled chapter-by-chapter by an LLM, with a git commit and tag
  at each chapter checkpoint. This enables a reader companion app to show the wiki
  as it exists at any point in the book — answering questions without spoilers.
  Use this skill whenever asked to: generate a book wiki, build a Bookworm knowledge
  base, compile a chapter-by-chapter wiki from an EPUB, create a spoiler-safe
  companion wiki, or process a book for the Bookworm reader app.
---

# Bookworm Wiki Builder

You are building a **spoiler-safe, chapter-versioned wiki** from a book (EPUB file). The wiki is a collection of interlinked markdown files — character pages, concept pages, place pages, faction pages, event pages — that grow chapter by chapter. After processing each chapter, you commit the wiki state and tag it, so the Bookworm reader app can later show the wiki as it existed at any reading position.

This is inspired by the "LLM Knowledge Base" / "LLM Wiki" pattern (Karpathy, 2026): instead of doing RAG retrieval at query time, we pre-compile raw source material into a structured, interlinked wiki. The wiki itself becomes the knowledge base. But unlike the general pattern where the wiki reflects cumulative knowledge, here the wiki is historically versioned — each chapter checkpoint is a coherent snapshot of the reader's knowledge at that point.

## Why this matters

For complex fiction (Malazan, Wheel of Time, Stormlight Archive, Red Rising, etc.), readers accumulate a *changing and incomplete understanding* of characters, systems, relationships, and mysteries. A normal wiki spoils things. This wiki preserves the reader's journey by only containing what's been revealed so far.

---

## Prerequisites

- An EPUB file to process
- A working directory to build the wiki in
- Git available on the system
- The extraction script at `scripts/extract_epub.py` (bundled with this skill)

Before starting, configure git in the work directory:
```bash
cd <work-dir>
git config user.email "bookworm@wiki-builder"
git config user.name "Bookworm Wiki Builder"
```

---

## The Build Process

### Phase 1: Extract Chapters

Run the bundled extraction script to split the EPUB into individual chapter files:

```bash
python <skill-path>/scripts/extract_epub.py <epub-path> <work-dir>/raw --min-chars 500
```

This produces:
- `<work-dir>/raw/metadata.json` — book title, author, chapter manifest
- `<work-dir>/raw/chapters/0000-chapter-title.md` — one file per chapter

After extraction, **review the chapter list** in metadata.json. The first few "chapters" may be front matter (copyright, dedication, table of contents). Note which chapter index is the actual start of the story — you'll skip front matter during wiki compilation. Read the first few chapter files to confirm.

Common patterns to watch for:
- **Front matter to skip**: Copyright pages, dedication, acknowledgments, table of contents. These contain no story content. Identify them and exclude from wiki processing.
- **Prologues**: Often merged with Part dividers by the extraction script. The Prologue is real story content — process it.
- **Part dividers**: "Part I: Slave" etc. may be merged into the following chapter. That's fine — the Part context is useful when processing that chapter.
- **Epilogues / back matter**: The last extracted "chapter" may be an author bio or back matter. Check and skip if so.
- **Chapter titles**: The extraction script gets titles from HTML headings, which aren't always present. If titles show as generic "Chapter N", read the content to identify the actual chapter name from the text body (often printed as a heading within the chapter). Use the real chapter name in your wiki references — e.g., "Ch. 1: Helldiver" not "Chapter 4" (which may be the extraction index).

Build a **chapter mapping** before starting the compilation loop:

```
Extraction Index → Story Chapter → Tag Name
0                → (front matter, skip)
1                → (front matter, skip)  
2                → Prologue              → ch-00
3                → Ch. 1: Helldiver      → ch-01
4                → Ch. 2: The Township   → ch-02
...
```

This mapping ensures your git tags correspond to the reader's actual chapter numbers, not the extraction indices.

### Phase 2: Initialize the Wiki Repository

Create the wiki directory structure and initialize git:

```bash
cd <work-dir>
mkdir -p wiki/characters wiki/concepts wiki/places wiki/factions wiki/events
git init
```

Create the initial `wiki/index.md` with the book title and empty section headers (read `references/page-templates.md` for the index template). Also create `wiki/open-questions.md` with an empty Active Questions section. And create `wiki/log.md` — this is an append-only chronological record of wiki changes (see below).

Make the initial commit:

```bash
git add -A
git commit -m "Initialize wiki for <Book Title>"
git tag init
```

### Phase 3: Compile the Wiki Chapter by Chapter

This is the core loop. For each chapter (skipping front matter):

#### Step 1: Read the chapter text

Read the chapter file from `raw/chapters/`. Understand what happens in this chapter: new characters introduced, existing characters doing new things, new information about the world, events, reveals, mysteries raised or answered.

#### Step 2: Read the current wiki state

Before making changes, read the current wiki pages that are relevant. At minimum, read `wiki/index.md`, `wiki/open-questions.md`, and the last few entries of `wiki/log.md` (to remember what happened recently). For the first few chapters, the wiki is small enough to read entirely. As it grows, focus on pages that the chapter content touches — characters who appear, concepts that are referenced, places where action happens.

#### Step 3: Decide what to create or update

Based on the chapter content, determine:

- **New pages to create**: Characters appearing for the first time, new concepts introduced, new places visited, new factions revealed, significant events.
- **Existing pages to update**: Characters who do something new, concepts that get more explanation, relationships that change, questions that get answered.
- **Open questions to resolve**: Read through the Active Questions list carefully. For each one, ask: "Does this chapter answer or partially answer this question?" If yes, move it to "Recently Resolved" with a brief explanation. This is easy to forget but important — readers rely on the open questions page to know what's still mysterious.
- **New open questions to add**: What new mysteries, ambiguities, or unresolved threads does this chapter introduce?

Not every chapter will touch every category. Some chapters might only update a few character pages. Others might introduce a whole new faction or concept. Match the effort to the content.

**Judgment calls on page creation**: Not every named character needs a page immediately. A character mentioned once in passing can wait. But if a character has a name, a role, and interacts with the story, they deserve a page. Use your judgment — when in doubt, create the page. It's easier to merge sparse pages later than to go back and create them retroactively.

#### Step 4: Write/update the wiki pages

Read `references/page-templates.md` for the templates and formatting guidelines. Key principles:

1. **Write from the reader's current knowledge.** You are writing for someone who has read up to and including this chapter. Do not include ANY information from later chapters, even if you know it from your training data. This is the single most important rule.

2. **Preserve uncertainty.** If the text is ambiguous, say so. "Darrow appears to be motivated by revenge, though his inner monologue suggests something deeper" is better than "Darrow is motivated by justice" if that's not yet clear.

3. **Cite chapters.** Every factual claim should reference where it was established: `(ch. 3)` or `(chs. 5-7)`.

4. **Cross-link aggressively.** Characters should link to their factions, to concepts they use, to places they visit. This web of links is one of the wiki's primary values. Use relative markdown links: `[Darrow](../characters/darrow.md)`.

5. **Update the index.** After creating new pages, add them to `wiki/index.md`.

6. **Update open questions.** This is a critical step that's easy to rush. For each Active Question, explicitly check whether this chapter resolves it. Move resolved questions to "Recently Resolved" with a one-line answer and chapter citation. Add new questions for mysteries or ambiguities this chapter introduces.

7. **Update chapter appearances.** Every character page has a "Chapter Appearances" section at the bottom. Add an entry for this chapter for every character who appears or is meaningfully referenced. This is a small thing but readers use it to trace a character's involvement across the book. Format: `- Ch. N: <brief note on role in this chapter>`

#### Step 5: Update the log

Append an entry to `wiki/log.md` summarizing what changed. Use a consistent format so the log is parseable and scannable:

```markdown
## [ch-07] Chapter 7: Other Things

**New pages:** mickey (character), carving (concept)
**Updated:** darrow, eo, the-society, colors, open-questions
**Key developments:** Darrow is taken from the mines by Dancer and the Sons of Ares. He meets Mickey, a Violet Carver. The carving process is introduced — Darrow will be physically transformed into a Gold.
**Questions raised:** How far does the physical transformation go? What is Dancer's real agenda?
**Questions resolved:** How Darrow goes from miner to Gold (ch. 1 question) — he's being surgically carved.
```

The log serves two purposes: (1) it helps you maintain context across chapters without re-reading the full wiki — a quick scan of recent entries reminds you what's been happening; (2) it gives humans a readable timeline of the wiki's evolution.

#### Step 6: Commit and tag

After updating all relevant pages and the log for this chapter:

```bash
git add -A
git commit -m "ch-<NN>: <Brief description of key developments>"
git tag ch-<NN>
```

Use zero-padded chapter numbers for sorting: `ch-01`, `ch-02`, ... `ch-48`.

For multi-book series, prefix with the book: `b01-ch-01`, `b01-ch-02`, etc.

The commit message should summarize the narrative developments, not just list file changes. Example: `"ch-07: Darrow undergoes the carving; introduced to Gold society hierarchy"` — not `"Updated 5 files"`.

#### Step 7: Update build status

After each successful commit, update `build-status.json` in the repo root. This file tells any agent (including a future session of yourself) exactly where the build left off:

```json
{
  "book_title": "Red Rising",
  "book_author": "Pierce Brown",
  "last_processed": {
    "extraction_index": 4,
    "story_chapter": "Chapter 2: The Township",
    "tag": "ch-02"
  },
  "next_to_process": {
    "extraction_index": 5,
    "story_chapter": "Chapter 3: The Laurel",
    "tag": "ch-03"
  },
  "chapters_processed": 3,
  "chapters_remaining": 43
}
```

Commit this file as part of each chapter's commit (it's already staged with `git add -A`).

#### Step 8: Move to the next chapter

Repeat steps 1-7 for each chapter until the book is complete.

---

### Resuming an Interrupted Build

If you are picking up a wiki build that was started by another agent or a previous session:

1. Read `build-status.json` to see what's been processed and what's next.
2. Check `git tag -l` to confirm the tags match the status file.
3. Read the last few entries of `wiki/log.md` to get context on recent developments.
4. Read `wiki/open-questions.md` to know what mysteries are active.
5. Continue from the `next_to_process` chapter.

This is the primary handoff mechanism between agents. The status file, the log, and the open questions together give a new agent enough context to continue without re-reading the entire wiki.

---

### Phase 4: Lint Pass (Post-Build Quality Check)

After processing all chapters, do a health check of the complete wiki. Read through the index, the log, and a sampling of pages. Look for:

- **Orphan pages**: Pages with no inbound links from other pages. Every character, concept, and place should be linked from at least one other page.
- **Missing cross-links**: Characters who interact but don't link to each other. Concepts referenced in character pages but not linked.
- **Stale open questions**: Questions in open-questions.md that were actually resolved in later chapters but not moved to "Recently Resolved."
- **Inconsistencies**: Facts that contradict each other across pages (e.g., a character's affiliation listed differently in two places).
- **Thin pages**: Pages that were created early and never fleshed out despite having more information available by the final chapter.
- **Missing pages**: Important characters, concepts, or events referenced in multiple pages but lacking their own dedicated page.

Fix any issues you find, commit as a `lint` pass, and tag as `lint-final` (or `b01-lint` for a multi-book series). This doesn't create a new chapter tag — it's a quality improvement to the final state.

```bash
git add -A
git commit -m "lint: fix cross-links, resolve stale questions, flesh out thin pages"
git tag lint-final
```

---

## Processing Strategy

### Batching for efficiency

You don't need to process every chapter one at a time if consecutive chapters form a natural group. For very short chapters (< 3,000 chars), it's fine to read 2-3 together and do a single wiki update + commit for each. But always create **one tag per chapter** even if you batch the reading — the Bookworm app needs per-chapter checkpoints.

If you batch-read chapters 5-7 together, still create separate commits/tags:
```bash
# After updating wiki for chapters 5-7 together:
git add -A && git commit -m "ch-05: ..." && git tag ch-05
# If 06 and 07 had additional changes worth noting:
git add -A && git commit --allow-empty -m "ch-06: ..." && git tag ch-06
git add -A && git commit --allow-empty -m "ch-07: ..." && git tag ch-07
```

Actually — the cleaner approach is: after reading the batch, write wiki updates, and commit once per chapter tag even if some commits are empty or small. The tag is the contract with the runtime.

### Context management

For a long book (40+ chapters), the wiki will grow large. You don't need to re-read every wiki page before every chapter. Develop a sense of which pages are relevant:

- Always read `wiki/index.md` (it's your map)
- Always read `wiki/open-questions.md` (it tells you what to watch for)
- Read character pages for characters who appear in the chapter
- Read concept pages for concepts that are referenced
- Skim place/faction pages if the chapter involves them

### Quality over speed

It's better to produce a thoughtful, well-linked wiki for each chapter than to rush through. The wiki is the product. Take time to:
- Write clear, useful summaries
- Create meaningful cross-links
- Note ambiguities and open questions
- Use good judgment about what deserves a page

---

## Multi-Book Series

For a series contained in a single EPUB (or when extending an existing wiki with a new book):

- Continue the same git repository
- Use book-prefixed tags: `b01-ch-01`, `b02-ch-01`
- At the start of a new book, consider adding a "book boundary" commit that notes the transition
- Characters, concepts, etc. carry forward — their pages just keep growing
- The index should note which book introduced each entity

When extending a wiki with a new book:
1. The wiki repo already exists with all previous book tags
2. Extract chapters from the new EPUB into `raw/chapters/` (with appropriate indexing)
3. Continue the compile loop from where the previous book left off

---

## Output Structure

When complete, the work directory should look like:

```
<work-dir>/
├── build-status.json           ← tracks build progress for resumability
├── raw/
│   ├── metadata.json
│   └── chapters/
│       ├── 0000-front-matter.md
│       ├── 0001-chapter-1-helldiver.md
│       └── ...
├── wiki/
│   ├── index.md
│   ├── open-questions.md
│   ├── log.md                  ← append-only chronological build log
│   ├── characters/
│   │   ├── darrow.md
│   │   ├── eo.md
│   │   └── ...
│   ├── concepts/
│   │   ├── the-society.md
│   │   ├── colors.md
│   │   └── ...
│   ├── places/
│   │   ├── lykos.md
│   │   └── ...
│   ├── factions/
│   │   └── ...
│   └── events/
│       └── ...
└── .git/
    └── (tags: init, ch-01, ch-02, ..., ch-48)
```

---

## Multi-Model Strategy

Processing a full book (or a 10-book series like Malazan) consumes a lot of tokens. Here's how to split the work across model tiers:

**Tier 1 — Compilation (bulk work): Use a capable mid-tier model**
Sonnet, GPT-5.4-mini, or similar. This model does the chapter-by-chapter wiki compilation: reading chapters, updating pages, maintaining cross-links. The task is mostly "read and synthesize" — it doesn't require deep reasoning, but it does require careful attention to detail and good writing. A strong mid-tier model handles this well.

**Tier 2 — Quality review (periodic): Use the strongest available model**
Opus, GPT-5, or similar. This model doesn't need to read the raw book text at all. Instead, it reviews the wiki output: reads the wiki pages, the log, the open questions, and assesses quality. It looks for:
- Internal contradictions between pages
- Pages that seem thin relative to how often a character/concept appears
- Awkward writing or unclear explanations
- Missing cross-links that should exist
- Open questions that seem already answered elsewhere in the wiki
- Spoiler leaks (information that seems too advanced for the stated chapter)

Run this review every ~10 chapters, or once after the full build. The reviewer produces a list of issues; the Tier 1 model (or a human) fixes them.

**Tier 3 — Lint and polish (final pass): Either tier**
The Phase 4 lint pass can be done by either tier. It's primarily mechanical — checking for orphan pages, missing links, stale questions — which a mid-tier model handles fine. But if you want the final wiki to read beautifully, have the Tier 2 model do a final editorial pass on key pages (the index, major character pages, core concept pages).

This tiered approach means Opus/GPT-5 usage is minimal — maybe 5-10% of total tokens — while still getting its judgment on the output quality.

---

## Integration with Bookworm

After the wiki is built, the Bookworm runtime needs to:

1. Map a reader's position to a chapter tag
2. Read the wiki state at that tag (using `git show <tag>:<path>` or by exporting snapshots)
3. Feed relevant wiki pages + raw text chunks to the LLM for Q&A

The bundled `scripts/snapshot_export.py` script can extract wiki state at each tag into a JSON format suitable for loading into the Bookworm database. Run it after the wiki is fully built:

```bash
python <skill-path>/scripts/snapshot_export.py <work-dir> --output <output-path>/snapshots.json
```

### Human-Readable Viewer

To generate a browsable HTML viewer with a chapter-state dropdown:

```bash
python <skill-path>/scripts/wiki_viewer_multi.py <work-dir> --output wiki-viewer.html
```

This produces a self-contained HTML file with all chapter snapshots embedded. The reader can toggle between chapters and see how the wiki grows, with "new" and "updated" badges showing what changed at each checkpoint. This is useful both for reviewing the build output and as a standalone wiki browser.

For a single-tag snapshot:

```bash
python <skill-path>/scripts/wiki_viewer.py <work-dir> --tag ch-25 --output wiki-at-ch25.html
```

---

## Tips for Smaller / Mid-Tier Models

If you are a smaller model (Haiku, GPT-5.4-mini, etc.) running this skill, pay extra attention to these common failure modes:

1. **Don't forget Chapter Appearances.** The "Chapter Appearances" section at the bottom of each character page is easy to skip when you're focused on updating the main content. After writing your updates, do a quick check: for every character who appeared in this chapter, did I add a chapter appearances entry?

2. **Explicitly scan open questions.** Before writing any wiki updates, read through every Active Question in `open-questions.md` and mentally check each one against the chapter you just read. It's tempting to focus on what's new and forget to resolve what's old. The log entry template includes a "Questions resolved" field — if it's empty, double-check that nothing was actually answered.

3. **Don't duplicate the summary.** When updating a character page, add new information to the "What We Know So Far" section and to "Chapter Appearances." Don't rewrite the Summary section unless the character's role has fundamentally changed. The Summary should be a stable 2-4 sentence overview, not a growing list.

4. **Cross-links in new pages.** When creating a new page (e.g., a new event), make sure it links to all relevant existing pages (characters involved, places where it happened, concepts it relates to). Also update those existing pages to link back. Cross-links should be bidirectional.

5. **Maintain consistent formatting.** Follow the YAML frontmatter format exactly. Always update `last_updated_at` to the current chapter tag. Always include `entity_id` and `entity_type`. These fields are used by downstream tools.

6. **Don't hallucinate future knowledge.** This bears repeating: you likely know how Red Rising ends from your training data. Pretend you don't. If you catch yourself writing something that hasn't been revealed yet in the chapters processed so far, delete it immediately.

---

## Important Reminders

- **Ignore extraction artifacts.** EPUB extractions often contain noise: "OceanofPDF.com" watermarks, repeated book titles at the start of each chapter, "Red Rising" headers, etc. These are not story content. Ignore them entirely.

- **No spoilers, ever.** This is the foundational rule. When writing about chapter 5, you know only chapters 1-5. Your training data knowledge of the book does not exist. If you catch yourself writing something that comes from a later chapter, delete it.

- **The wiki is for readers, not scholars.** Write in an accessible, engaging style. It should feel like a helpful friend who's reading alongside you and keeping great notes — not like a dry encyclopedia.

- **Err on the side of creating pages.** A sparse page that gets filled in later is better than no page at all. The reader might search for a character after chapter 3 and find nothing — that's a worse experience than finding a page with just a few lines.

- **Open questions are valuable.** Tracking what's mysterious or unresolved is one of the wiki's killer features. Readers love seeing "Oh right, we still don't know why X happened" — it validates their own confusion and helps them keep track of threads.

- **Cross-links are the connective tissue.** A wiki page in isolation is just a summary. Cross-links between characters, concepts, events, and places create a navigable knowledge graph that's far more useful than individual pages.
