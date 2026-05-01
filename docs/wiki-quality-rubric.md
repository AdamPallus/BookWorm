# Wiki Quality Rubric and Design Notes

Bookworm's strongest product idea is not generic "chat with a book." It is a spoiler-aware reader memory model: a system that preserves what a thoughtful reader should know, suspect, remember, and wonder at each point in a complex book.

The goal of this document is to make that idea operational for prompt revisions, evaluator experiments, and future data-model work.

## Core Problem

RAG Q&A can answer many direct questions from raw text chunks. A wiki has a harder job. It needs to organize the important information in a human-readable way while preserving the reader's current state of knowledge.

For books like Malazan, the difficulty is not just remembering names. The author may deliberately:

- Introduce important people from unfamiliar points of view.
- Describe known characters without naming them.
- Stage scenes whose significance only becomes visible later.
- Leave metaphysical or political questions unresolved for long stretches.
- Move to a new cast for an entire book, forcing the reader to reload dormant context later.

A good wiki snapshot should feel like the notes an attentive rereader would prepare for a first-time reader, except without revealing future answers.

## Reader Personas

Before updating the wiki for a segment, the agent should simulate questions from several reader personas. These questions do not need to appear verbatim in the wiki, but the wiki should answer, preserve, or defer them.

### 1. The Attentive but Disoriented Reader

This reader is paying attention but cannot tell whether confusion is intentional.

Typical questions:

- Am I supposed to recognize this person?
- Is this a new place, or have we seen it before under another name?
- Did I miss an explanation, or is this still unresolved?
- Why did the scene avoid naming someone directly?

### 2. The Character Tracker

This reader wants to understand people, aliases, loyalties, and relationships.

Typical questions:

- Who is this character connected to?
- Has this person appeared before?
- What does this character want right now?
- Has their allegiance or self-presentation changed?
- Is this likely the same person as someone previously described differently?

### 3. The Lore and Worldbuilding Reader

This reader tracks institutions, magic, history, geography, and metaphysics.

Typical questions:

- What do we actually know about this faction, god, title, magic, race, or institution?
- Which parts are fact, rumor, myth, or interpretation?
- What new rule or limitation did this scene imply?
- Does this concept connect to earlier unexplained terms?

### 4. The Mystery and Theory Reader

This reader tracks setup, ambiguity, foreshadowing, and unresolved contradictions.

Typical questions:

- What is the author inviting me to wonder about?
- What detail seems overemphasized without a payoff yet?
- What explanations are possible without using future knowledge?
- Did this segment answer an old question, sharpen it, or make it stranger?

### 5. The Returning Reader

This reader read earlier chapters days or weeks ago and needs continuity.

Typical questions:

- What should I remember before continuing?
- Which dormant characters, places, or concepts just became relevant again?
- What changed since the last time this subject mattered?
- What open questions are still worth carrying forward?

### 6. The Q&A Retrieval User

This reader may never open the wiki directly. They rely on the wiki to improve answers.

Typical questions:

- What compact page would help answer likely questions about this subject?
- Does the page use names and aliases a user might ask about?
- Are related subjects linked so retrieval can travel across the wiki graph?
- Is the summary a useful retrieval tagline rather than a flat definition?

## Prompt Block: Persona Pass

This block can be inserted before the file-editing instruction.

```text
Before editing the wiki, do a private reader-question pass.

Think through the newest segment from these reader perspectives:

1. Attentive but disoriented reader: what might feel intentionally confusing?
2. Character tracker: what changed about people, aliases, loyalties, or relationships?
3. Lore/worldbuilding reader: what institutions, powers, places, histories, or rules were clarified or complicated?
4. Mystery/theory reader: what open questions, hints, or unresolved tensions should a thoughtful reader carry forward?
5. Returning reader: what dormant information became relevant again?
6. Q&A retrieval user: what page updates would help answer future reader questions accurately?

Do not output this analysis. Use it to decide which pages to create, rewrite, link, rename, delete, or update in open-questions.md.
```

## Quality Rubric

Use this rubric for offline evaluation, prompt iteration, or an optional supervisor. It is better as a scoring tool than as a hard runtime gate.

Score each category from 0 to 3:

- **0:** missing or actively wrong
- **1:** present but shallow, brittle, or incomplete
- **2:** useful and mostly reliable
- **3:** excellent for this point in the book

### 1. Grounding and Spoiler Safety

The wiki contains only what the text shown so far supports. It does not use future knowledge, training-data knowledge, or meta-spoiler language.

High score signs:

- Claims are specific but bounded by current evidence.
- Rumor, myth, uncertainty, and direct observation are not collapsed into one certainty.
- The page can say "the text frames this as uncertain" without saying "later this will matter."

Failure signs:

- Future identity, fate, or explanation leaks into an earlier snapshot.
- A page overstates speculation as fact.
- The prose uses meta language like "later revealed" or "will become important."

### 2. Reader Usefulness

The wiki helps a reader understand what they have read and continue reading with less friction.

High score signs:

- Pages explain why a subject matters, not just what facts have accumulated.
- A returning reader can reload the situation quickly.
- Important dormant information is preserved when it becomes relevant again.

Failure signs:

- Pages read like segment summaries.
- The wiki creates pages for walk-ons but misses central subjects.
- Summaries are generic definitions instead of useful taglines.

### 3. Open Question Quality

`open-questions.md` captures what a thoughtful reader would actually be wondering.

High score signs:

- Questions are specific, story-aware, and nontrivial.
- Old questions are sharpened, resolved, merged, or pruned.
- Resolved questions move into one short `Recently resolved` section when useful.

Failure signs:

- Questions are generic: "What will happen next?"
- Answered questions vanish without being absorbed into relevant pages.
- The file accumulates stale questions that no longer reflect the reader's mental state.

### 4. Entity and Alias Tracking

The wiki tracks people, groups, places, titles, disguises, and aliases without prematurely flattening ambiguity.

High score signs:

- Alias reveals trigger renames or rewrites when the text supports them.
- Possible identity connections are phrased carefully when unresolved.
- Pages preserve how different characters perceive the same subject.

Failure signs:

- The same subject gets duplicate pages under different names after the text makes the identity clear.
- A mysterious description is identified too early.
- Characters with one brief function get overbuilt pages.

### 5. Narrative Synthesis

Pages synthesize the subject's current role in the story rather than appending facts.

High score signs:

- Central pages grow richer over time.
- New information reshapes old sections where appropriate.
- Sections fit the subject instead of following a rigid template.

Failure signs:

- Append-only bullet lists.
- Earlier characterization is lost during rewrites.
- Every character has the same page structure regardless of narrative role.

### 6. Link Graph and Retrieval Value

The wiki links related subjects so both humans and retrieval can move through the knowledge graph.

High score signs:

- First mentions of existing subjects are linked.
- Internal links resolve.
- Central pages link to relevant characters, factions, places, events, and concepts.

Failure signs:

- Rich prose mentions known pages without links.
- Broken links are introduced.
- Pages are isolated even when relationships are central.

### 7. Importance Calibration

The wiki distinguishes central subjects from walk-ons and transient details.

High score signs:

- `index.md` reflects what matters now.
- Major pages are substantial.
- Minor subjects are handled inside other pages unless they have durable importance.

Failure signs:

- The wiki creates a page for every named person.
- Major and minor pages all have the same depth.
- `index.md` is a directory rather than a curated front door.

### 8. Style and Readability

The prose is clear, direct, and wiki-like.

High score signs:

- Matter-of-fact tone.
- Rich prose where warranted.
- No chatty commentary, apologizing, or process notes.

Failure signs:

- The page talks about the wiki process.
- The prose hedges constantly.
- Bullet lists replace synthesis on major pages.

## Evaluator Prompt Shape

A supervisor model should not simply approve or reject. That often wastes tokens and produces empty approval. Use a forced structured audit instead.

```text
Evaluate this wiki snapshot update against the rubric.

Return JSON only:
{
  "scores": {
    "grounding_spoiler_safety": 0-3,
    "reader_usefulness": 0-3,
    "open_questions": 0-3,
    "entity_alias_tracking": 0-3,
    "narrative_synthesis": 0-3,
    "link_graph": 0-3,
    "importance_calibration": 0-3,
    "style_readability": 0-3
  },
  "top_issues": [
    {
      "severity": "blocker|major|minor",
      "file": "relative/path.md",
      "problem": "specific problem",
      "suggested_fix": "specific fix"
    }
  ],
  "best_page": "relative/path.md",
  "weakest_page": "relative/path.md",
  "should_retry": true|false
}

Rules:
- Always identify the weakest page.
- If should_retry is true, top_issues must include at least one blocker or major issue.
- Do not approve just because the wiki is plausible.
- Do not request a retry for taste-only prose differences.
```

Use the evaluator mainly for offline prompt experiments or sampled dashboard diagnostics. A runtime supervisor should only force retries for concrete blockers: spoiler leaks, clear contradictions, broken links, missing required files, or severe no-op updates.

## Mini vs Full Model Experiment

Run this as a small bakeoff before changing the production loop.

1. Pick 3 to 5 hard segments from a complex book.
2. Save the exact input bundle for each segment: current wiki, text context, prompt, and expected segment id.
3. Have `gpt-5.4-mini` write the update with the current prompt.
4. Have the stronger model write the same update.
5. Have the stronger model score both outputs with the rubric.
6. Manually review disagreements.
7. Convert repeated stronger-model advantages into prompt examples, checks, or data structures.

Useful measurements:

- Did the stronger model identify more important open questions?
- Did it preserve ambiguity better?
- Did it rename or merge pages more appropriately?
- Did it create fewer walk-on pages?
- Did it write summaries that would improve retrieval?
- Did it link more consistently?

The likely win is not "better prose." The likely win is better salience judgment: knowing what deserves durable memory.

## Structured Data Without Rigid Character Templates

Structured data does not need to mean every character gets a full biographical template. Fiction does not work that way. Some characters appear once, say something important, and vanish. Some are intentionally partial. Some are known first as a description, title, or rumor.

The useful structure is sparse and permissive: record what is known, where it came from, and how important it seems, without forcing a complete life story.

### Possible Tables or JSON Objects

**Entity**

```json
{
  "id": "entity_123",
  "canonical_name": "The unnamed cloaked man",
  "type": "character|faction|place|concept|event|object|unknown",
  "wiki_path": "characters/unnamed-cloaked-man.md",
  "importance": "central|recurring|local|walk_on|unknown",
  "status": "active|dead|destroyed|resolved|dormant|unknown",
  "first_seen_segment": 42,
  "last_seen_segment": 57
}
```

**Alias**

```json
{
  "entity_id": "entity_123",
  "name": "the dark-skinned man",
  "confidence": "confirmed|suspected|rejected",
  "first_seen_segment": 42,
  "notes": "Description used before the text gives a name."
}
```

**Appearance**

```json
{
  "entity_id": "entity_123",
  "segment_id": 42,
  "role": "appears|mentioned|described|rumored|implied",
  "summary": "Appears from a new point of view and is not named.",
  "evidence_chunk_ids": [1234, 1235]
}
```

**Relationship**

```json
{
  "source_entity_id": "entity_123",
  "target_entity_id": "entity_456",
  "relationship": "serves|opposes|travels_with|may_be_same_as|knows|killed_by",
  "confidence": "confirmed|suspected|ambiguous",
  "first_seen_segment": 44,
  "last_updated_segment": 60
}
```

**Open Question**

```json
{
  "id": "q_0182",
  "question": "Who is the cloaked man seen from the new point of view?",
  "status": "open|sharpened|resolved|absorbed|dropped",
  "introduced_segment": 42,
  "last_updated_segment": 57,
  "related_entity_ids": ["entity_123"],
  "spoiler_safe_answer": null
}
```

**Evidence Span**

```json
{
  "id": "ev_9021",
  "segment_id": 42,
  "chapter": "Chapter title if known",
  "chunk_id": 1234,
  "short_label": "first cloaked-man description"
}
```

### Why This Helps

- The agent can avoid duplicate pages for aliases.
- Q&A can retrieve by alias even when the wiki page uses a later canonical name.
- Open questions can persist across segments without relying only on prose.
- Walk-on characters can be recorded as appearances without getting full pages.
- Important dormant subjects can be revived when they reappear.

### How to Introduce It Gradually

Do not replace Markdown first. Add structured data as a sidecar.

Phase 1:

- Keep Markdown as the human-readable artifact.
- Add optional `wiki/entities.json` and `wiki/questions.json`.
- Let the agent update them after it updates Markdown.
- Use deterministic checks only for valid JSON, path references, duplicate IDs, and link existence.

Phase 2:

- Use sidecar data to warn about duplicate aliases, stale open questions, and missing links.
- Feed compact sidecar summaries into Q&A retrieval.

Phase 3:

- Render some wiki sections from structured data if that proves useful.
- Keep prose pages editable, because prose synthesis is still the main value.

## Oracle Map Idea

The oracle approach means letting a stronger model read the whole book or series to identify what early details eventually matter, then using that knowledge to improve spoiler-safe snapshots.

This is powerful but risky. It can create meta-spoilers even without explicit facts. A note like "preserve this minor detail carefully" can reveal that the detail matters later.

The safest version is a private salience map, not a user-visible annotation.

### Possible Workflow

1. Strong model reads the full book or series.
2. It creates a private map of early details that should be preserved.
3. For each segment, it emits only spoiler-safe salience hints:
   - "Preserve the existence of this encounter."
   - "Do not collapse this rumor into fact."
   - "Track this unnamed description as a possible entity."
   - "Keep this open question active."
4. The mini uses those hints while writing the snapshot.
5. A spoiler-leak check reviews the output against the actual read-so-far text.

### Risks

- The hint itself can bias the wiki toward "this is important" in a way a first-time reader would feel as a spoiler.
- The agent may accidentally include future framing.
- The wiki may become too clever, emphasizing details before the book itself has earned that emphasis.

### Practical Recommendation

Use oracle maps only as an offline experiment at first.

Compare three outputs:

- Mini with current prompt.
- Mini with persona pass and rubric-informed prompt.
- Mini with private oracle salience hints.

If oracle hints mainly improve preservation without causing suspicious emphasis, they may be useful. If they make pages feel like a rereader nudging the reader, skip them.

## Practical Next Prompt Changes

The highest-value prompt changes are likely:

1. Add the private persona pass before editing.
2. Strengthen `open-questions.md` instructions around ambiguity, setup, and resolved-question absorption.
3. Add one excellent example of a page that tracks partial knowledge without overclaiming.
4. Add one excellent example of an open question that gets sharpened over several segments before resolution.
5. Tell the agent explicitly that not every mentioned person deserves a page; some belong as appearances inside another page or open question.

The highest-value harness changes are likely:

1. Hard check broken internal links.
2. Hard check duplicate `Recently resolved` sections.
3. Soft warn on index-only updates.
4. Soft warn on missing first-link coverage.
5. Add optional structured sidecar files for aliases and open questions once the Markdown loop is stable.
