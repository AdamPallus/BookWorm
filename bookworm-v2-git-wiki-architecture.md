# Bookworm v2 — Git-Versioned Wiki Architecture

**Date:** 2026-04-05  
**Status:** architecture note / decision capture  
**Context:** follow-up to earlier Bookworm spoiler-safe companion work; this doc reconciles the prior “rolling chapter-state compiler” idea with a simpler git-first implementation.

## TL;DR

Bookworm v2 should use a **git-versioned markdown wiki** as its canonical knowledge base for each uploaded book/series.

- Ingest the book **chapter by chapter**
- Update the wiki incrementally after each chapter
- Save a **checkpoint tag/commit per chapter**
- When a reader is at chapter N, answer questions against the **repo at checkpoint N**
- Keep user questions from directly mutating canonical pages; instead, let them generate **research artifacts / proposed improvements** that can be folded back in later

This preserves spoiler safety by making the knowledge base itself historically versioned.

---

## Problem We Are Solving

For fiction, especially mystery-heavy series like **Malazan**, the reader does not just accumulate facts — they accumulate a changing and incomplete **understanding** of systems, characters, relationships, cosmology, and open questions.

Examples:
- the **Warren system**
- the **Deck of Dragons**
- **elder gods / ascendants / houses / holds**
- characters whose identity or significance unfolds gradually

A normal “final truth” wiki is spoiler-prone. Even links, terminology, and page structure can leak future understanding.

We want a system where:
- the agent only sees what the reader should know **up to this point**
- the knowledge base can span **many files and concepts**, not just one summary per chapter
- historical states remain **coherent and inspectable**
- the system can get better over time without corrupting earlier checkpoints

---

## Core Decision

### Canonical knowledge base = git repo of markdown files

For each uploaded book or series, maintain a repo containing:
- wiki pages (`characters/`, `concepts/`, `places/`, `events/`, etc.)
- index pages
- optional lightweight metadata/frontmatter

After each chapter is processed:
- update the wiki pages
- commit the changes
- create a tag or checkpoint name for that chapter

Example tags:
- `book1-ch01`
- `book1-ch02`
- `book1-ch03`

For series-wide continuity:
- continue the same repo across books
- keep chapter tags globally unique or use book-prefixed tags

### Why git fits this well

Git is a strong fit because this is a **living document set** whose historical states matter.

We need:
- many files changing together
- coherent snapshots over time
- easy rollback
- diffs showing what changed at each checkpoint
- a natural filesystem representation for both agents and humans

This is unusual, but not alien to software workflows. It is close in spirit to versioned docs, config-as-truth repos, research knowledge repos, and audit-style “show me what the world looked like then” systems.

---

## Reconciliation With Earlier Bookworm Design

Earlier Bookworm design direction:
- spoiler-safe chapter-versioned wiki companion
- not a graph database first
- conceptually a **rolling chapter-state compiler**
- output should be a chapter-scoped wiki with entity pages and a chapter selector

This doc does **not** reject that direction.

Instead, it simplifies the implementation:

### Previous framing
- chapter-state compiler may maintain hidden structured state
- wiki rendered from that state

### Current framing
- **the repo at commit/tag N is the chapter state**
- the wiki itself is the primary artifact
- git provides the historical versioning layer

So the new view is:

> the older design was conceptually right; git provides a simpler substrate for the same core behavior

We are trading some rigor for much lower complexity in v1.

---

## Architecture Overview

## 1) Ingestion layer

User uploads an EPUB/book file.

System responsibilities:
- extract chapter structure
- normalize chapter IDs/order
- preserve raw chapter text/chunks for grounding

Suggested internal assets:
- `raw/chapters/book1-ch01.md`
- `raw/chapters/book1-ch02.md`
- etc.

Raw chapter text is not the wiki. It is source material and evidence.

---

## 2) Wiki compilation layer

After processing each chapter, the agent updates the wiki.

Possible folders:

```text
wiki/
  index.md
  characters/
  concepts/
  gods/
  places/
  factions/
  events/
  open-questions.md
```

Examples:
- `wiki/concepts/warrens.md`
- `wiki/concepts/deck-of-dragons.md`
- `wiki/gods/shadowthrone.md`
- `wiki/characters/fiddler.md`

The wiki should reflect only what is valid **as of the current chapter checkpoint**.

Important principle:
- pages should preserve uncertainty where uncertainty exists
- do not rewrite early understanding with late-book hindsight unless the current checkpoint justifies it

In other words: avoid omniscient wiki voice.

---

## 3) Checkpointing layer

After each chapter update:
- create a git commit
- assign a tag/checkpoint for retrieval

Recommendation:
- use **tags** for standard chapter checkpoints
- reserve **branches** for experiments, alternate model runs, or alternate compilation strategies

Examples:
- normal checkpoints: `book1-ch14`, `book2-ch03`
- experimental branches: `gemini-pass`, `strict-spoiler-mode`, `ontology-cleanup`

Why tags over branches:
- simpler mental model
- chapter checkpoints are fixed states, not ongoing lines of development

---

## 4) Reader runtime / question answering

When the user is reading and asks a question:
1. determine the reader’s current checkpoint
2. load the wiki state at the matching tag/commit
3. optionally load relevant raw text chunks up to that checkpoint for grounding/citations
4. answer using only that state

This preserves the original Bookworm anti-spoiler principle:
- knowledge available to the model is constrained by reading progress

### Important runtime note

Do **not** require copying the full repo into a user folder by default.

Better default:
- maintain one canonical repo per uploaded book/series on the server
- query the relevant checkpoint directly

Use a **user overlay** only when needed.

---

## 5) User overlay layer

Some user data should not live in the canonical wiki.

Examples:
- private notes
- highlights
- bookmarks
- personal theories
- saved questions
- reading-progress metadata

Suggested split:
- **base repo** = canonical spoiler-safe wiki
- **user overlay** = per-user notes and personal artifacts

This avoids cloning/copying repos unnecessarily while still supporting personalization.

---

## 6) Query-improves-wiki loop

This is the missing piece relative to a pure static checkpoint system, and the main place where Karpathy’s knowledge-base idea becomes relevant.

We want user questions to improve the system over time.

### But:
User questions should **not directly edit canonical wiki pages**.

If questions write straight into the canon, the wiki will gradually accumulate:
- redundant clarifications
- overfitted wording
- accidental contamination
- sludge

### Better pattern

Questions produce **research artifacts** or **proposed improvements**.

Possible folders:

```text
research/
  queries/
  proposals/
  unresolved/
```

Examples:
- `research/queries/2026-04-05-deck-of-dragons-literal-vs-symbolic.md`
- `research/proposals/clarify-warrens-page-book1-ch07.md`
- `research/unresolved/open-questions.md`

Then a later maintenance pass can decide whether to fold those improvements into the canonical wiki, using only evidence valid at the relevant checkpoint.

This gives us the Karpathy-style compounding effect without letting every user question graffiti the source of truth.

---

## Page design guidance

Pages should be useful for both browsing and answering questions.

Recommended sections:
- **What is known so far**
- **Open questions / ambiguities**
- **Connections currently visible**
- **Recent changes**
- **Evidence / chapter references**

This is better than writing pages as final-form encyclopedia entries.

For fiction, uncertainty is part of the product.

---

## Minimal metadata recommendation

Even in a git-first system, add light frontmatter to pages.

Example:

```yaml
---
entity_id: warrens
entity_type: concept
introduced_at: book1-ch04
last_updated_at: book2-ch07
status: partial
aliases: [Warrens]
spoiler_scope: book2-ch07
---
```

This is optional but strongly recommended.

Why:
- stable identity across page renames
- easier indexing/search
- easier future migration to structured state if needed
- better validation and consistency checks

This is the main non-git concession that buys a lot without overengineering the system.

---

## Why not start with a graph DB or hidden compiler state?

Because v1 should optimize for:
- simplicity
- inspectability
- trust
- human-legible artifacts
- easy debugging

Git + markdown already gives us:
- coherent snapshots
- diffs
- rollback
- easy browsing in Obsidian / filesystem / custom UI

A graph or structured hidden state may become useful later for advanced querying, but it is not required to prove the core product.

---

## Known limitations of the git-first design

Git does **not** automatically solve:
- ontology discipline
- semantic identity beyond filenames unless we add metadata
- perfect handling of uncertain or contradictory interpretations
- advanced cross-checkpoint querying

Examples of future features that may want a sidecar index or structured state:
- “show when this concept first appeared”
- “trace how understanding of Warrens changed over 3 books”
- “build a graph of relationships visible by checkpoint”

These are good future problems, not blockers for v1.

---

## Proposed MVP

### Inputs
- EPUB upload
- chapter extraction
- per-user reading position

### Canonical artifacts
- git repo with markdown wiki
- one checkpoint tag per chapter
- raw chapter text/chunks for evidence

### Runtime
- map reading position to checkpoint tag
- answer using only wiki + raw text up to that checkpoint

### Improvement loop
- save user-question artifacts separately
- periodically fold justified improvements into canonical pages at the relevant checkpoint or later checkpoints

### Explicit non-goals for MVP
- graph DB
- fully autonomous ontology engine
- page-level or paragraph-level checkpoints
- global cross-series cosmology engine
- direct user-edit-driven mutation of canon

---

## Bottom-line decision

Bookworm v2 should be built as a **git-versioned markdown wiki with chapter checkpoints**.

That captures the strongest part of the earlier spoiler-safe Bookworm idea while staying much simpler than a full compiler/database architecture.

If this later proves too loose, we can add structure incrementally:
1. light frontmatter
2. validation/indexing tools
3. optional structured sidecar state
4. graph/query features

But the core should stay simple until the product proves itself.
