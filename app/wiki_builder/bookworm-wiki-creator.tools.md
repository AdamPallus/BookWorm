# TOOLS.md — Bookworm Wiki Creator

Your cheat sheet for this setup. Add to it as you learn things worth remembering across sessions.

## Wiki layout

Wikis live at `data/wikis/{slug}/wiki/` inside the bookworm repo. Each wiki is a git repo at `data/wikis/{slug}/`; the working tree under `wiki/` is what you edit.

Page taxonomy:

- `characters/{slug}.md`
- `concepts/{slug}.md`
- `places/{slug}.md`
- `factions/{slug}.md`
- `events/{slug}.md`

Plus `wiki/index.md` and `wiki/open-questions.md` — never delete those.

Slugs: lowercase, hyphenated, matching `[a-z0-9][a-z0-9-]*`.

## Git

The harness handles all git operations — staging, commits, tags, reverts. You don't run `git` yourself. Edit files; the harness commits and tags after each accepted segment.

Git author for the wiki repos: `Bookworm Wiki Builder <bookworm@wiki-builder>`.

## Per-segment protocol

Each turn the harness sends you:
- The wiki slug (e.g. `red-rising-v3`) — confirms which wiki to edit.
- A segment of book text plus its identifier (e.g. `seg-0017`).

Respond by editing files in `data/wikis/{slug}/wiki/` then returning briefly. The harness checks and either accepts (next segment incoming) or rejects with a list of issues (fix only what was flagged, in the same session).

## Investigative pattern

Before writing, look. A useful default workflow:

1. `Glob` for files matching names that appear in the segment.
2. `Grep` for the proper nouns and key concepts in the segment to see if they're already covered under a different page.
3. `Read` the candidates.
4. *Then* decide: write a new page, rewrite an existing one, rename, or no-op.

This is how you avoid the failure mode where a name is already covered under a nickname and you create a duplicate.

---

Add notes below as you go. Things you've learned about this setup, conventions you've established, gotchas. This file is yours.
