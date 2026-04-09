# Wiki Page Templates

These are the templates for different entity types in the wiki. Each page uses YAML frontmatter for machine-readable metadata and markdown body for human-readable content.

The key design principle: **write from the reader's perspective at this point in the book.** Preserve uncertainty. Don't use omniscient voice. If the text says "a tall, dark-skinned man" and we suspect it's a specific character but it hasn't been confirmed, say so. The wiki should reflect the reader's evolving understanding, not a spoiler-filled encyclopedia.

## Table of Contents

1. [Character Page](#character-page)
2. [Concept Page](#concept-page)
3. [Place Page](#place-page)
4. [Faction/Group Page](#factiongroup-page)
5. [Event Page](#event-page)
6. [Index Page](#index-page)
7. [Open Questions Page](#open-questions-page)

---

## Character Page

File location: `wiki/characters/<slug>.md`

```markdown
---
entity_id: <slug>
entity_type: character
aliases: [<list of names, nicknames, titles used so far>]
introduced_at: <chapter-tag where first mentioned>
last_updated_at: <chapter-tag of most recent update>
status: <alive | dead | unknown | presumed-dead>
affiliations: [<faction slugs>]
---

# <Character Name>

## Summary
<2-4 sentence overview of who this character is as understood so far. Written in present tense for living characters, past tense for dead ones. Include their role, primary relationships, and significance.>

## What We Know So Far
<Detailed account of everything established about this character, organized chronologically by when information was revealed. Use bullet points for discrete facts. Cite chapter references in parentheses.>

- First appeared as... (ch. N)
- Revealed to be... (ch. N)
- Key action: ... (ch. N)

## Relationships
<List of known relationships to other characters. Link to their wiki pages.>

- **[Other Character](../characters/other-character.md)**: <nature of relationship as currently understood>

## Open Questions
<Things about this character that are unresolved, ambiguous, or hinted at but not confirmed.>

- Is their stated motivation genuine, or is there something else going on?
- Connection to [concept/event] is unclear.

## Chapter Appearances
<List of chapters where this character appears or is meaningfully referenced.>

- Ch. N: <brief note on what happens>
```

---

## Concept Page

File location: `wiki/concepts/<slug>.md`

```markdown
---
entity_id: <slug>
entity_type: concept
aliases: [<alternate names or terms>]
introduced_at: <chapter-tag>
last_updated_at: <chapter-tag>
status: <partial | established | evolving>
---

# <Concept Name>

## Summary
<2-3 sentence overview of the concept as currently understood.>

## Current Understanding
<What the reader knows about this concept so far. Be explicit about what's confirmed vs. implied vs. speculated.>

### Confirmed
<Facts that are clearly established in the text.>

### Implied / Suggested
<Things that seem likely based on context but haven't been explicitly stated.>

## How It Works
<Mechanics or rules of the concept as revealed so far, if applicable.>

## Connections
<Links to related concepts, characters, places.>

- Related to [other concept](../concepts/other.md)
- Used by [character](../characters/character.md)

## Open Questions
<What remains unclear or contradictory about this concept.>

## References
<Chapter citations where this concept is discussed or demonstrated.>
```

---

## Place Page

File location: `wiki/places/<slug>.md`

```markdown
---
entity_id: <slug>
entity_type: place
aliases: []
introduced_at: <chapter-tag>
last_updated_at: <chapter-tag>
---

# <Place Name>

## Summary
<Brief description of the place and its significance.>

## Description
<Physical description, atmosphere, notable features as described in the text.>

## Significance
<Why this place matters to the story. What happens here.>

## Notable Inhabitants / Visitors
<Characters associated with this place. Link to character pages.>

## Events Here
<Key events that take place at this location. Link to event pages.>

## References
<Chapter citations.>
```

---

## Faction/Group Page

File location: `wiki/factions/<slug>.md`

```markdown
---
entity_id: <slug>
entity_type: faction
aliases: []
introduced_at: <chapter-tag>
last_updated_at: <chapter-tag>
status: <active | disbanded | unknown>
---

# <Faction Name>

## Summary
<Overview of the group, its purpose, and its role in the story so far.>

## Known Members
<List members with links to character pages. Note roles within the group if known.>

- **[Character](../characters/character.md)** — <role, if known>

## Goals / Purpose
<What this group is trying to accomplish, as understood so far.>

## Structure / Hierarchy
<How the group is organized, if known.>

## Key Actions
<Important things this group has done in the story so far, cited by chapter.>

## Relationships with Other Groups
<Alliances, rivalries, conflicts with other factions.>

## Open Questions
<Unresolved questions about this group.>
```

---

## Event Page

File location: `wiki/events/<slug>.md`

```markdown
---
entity_id: <slug>
entity_type: event
occurred_at: <chapter-tag>
last_updated_at: <chapter-tag>
participants: [<character slugs>]
locations: [<place slugs>]
---

# <Event Name>

## Summary
<Brief description of what happened.>

## What Happened
<Detailed account of the event as described in the text. Chronological order.>

## Participants
<Who was involved. Link to character pages.>

## Consequences
<What resulted from this event, as known so far.>

## Open Questions
<Unresolved aspects of this event.>

## References
<Chapter citations.>
```

---

## Index Page

File location: `wiki/index.md`

```markdown
---
last_updated_at: <chapter-tag>
---

# <Book Title> — Wiki Index

> This wiki reflects the reader's knowledge through **<current chapter tag>**.
> It is spoiler-safe up to this checkpoint.

## Characters
<Alphabetical list with one-line descriptions and links.>

- [Character Name](characters/slug.md) — <one-line description>

## Concepts
<List with links.>

## Places
<List with links.>

## Factions & Groups
<List with links.>

## Key Events
<Chronological list with links.>

## [Open Questions](open-questions.md)
<Link to the running open-questions page.>
```

---

## Open Questions Page

File location: `wiki/open-questions.md`

This is a special running document that tracks mysteries, unresolved threads, and things the reader might be wondering about.

```markdown
---
last_updated_at: <chapter-tag>
---

# Open Questions

Questions and mysteries as of **<current chapter tag>**.

## Active Questions
<Questions raised by the text that haven't been answered yet.>

- **<Question>** — Raised in ch. N. <Brief context.>

## Recently Resolved
<Questions that were answered in recent chapters. Keep the last ~5 resolved questions here for continuity, then archive older ones.>

- **<Question>** — Raised ch. N, resolved ch. M. <Brief answer.>
```

---

## Guidelines for All Pages

1. **Chapter references**: Always cite which chapter(s) information comes from, using the format `(ch. N)` or `(chs. N-M)`.

2. **Cross-links**: Use relative markdown links between pages. Characters should link to their factions, concepts, and events. Events should link to participants and locations. This web of connections is one of the wiki's core values.

3. **Uncertainty language**: Use phrases like "appears to be," "seems to," "it's implied that," "not yet confirmed" when information is ambiguous. This is critical for spoiler safety.

4. **No future knowledge**: Never include information from chapters that haven't been processed yet. If you know something from training data about the book, do NOT include it. Only use information from the chapter text provided.

5. **Evolving pages**: Pages should grow and change over time. Early pages will be sparse. That's fine. Don't pad them with speculation — let them fill in naturally as chapters reveal more.

6. **Slug conventions**: Use lowercase kebab-case for all slugs: `quick-ben`, `deck-of-dragons`, `warrens`, `bridgeburners`.

7. **Chapter Appearances are mandatory for character pages.** Every character page must have a "Chapter Appearances" section listing each chapter where the character appears or is meaningfully referenced. This is one of the most useful features for readers trying to trace a character's involvement. Update it every time you update a character page. Format: `- Ch. N: <brief note on what happens with this character>`.

8. **Open Questions require active maintenance.** When updating `open-questions.md`, don't just add new questions — actively review every existing Active Question against the current chapter. If a question is answered (even partially), move it to "Recently Resolved" with a one-line explanation. If a question is partially answered but still open, update its context text. Stale open questions that have actually been answered are one of the most common quality issues.
