# Code Review Follow-ups

This document records the concrete issues found during the April 2026 review of the wiki/OpenClaw work, plus the practical fixes and test coverage each one should get.

## 1. Preserve `wiki_segments.session_total_tokens` during migration

**Priority:** P1

**Files:** `app/db.py`

The migration adds `wiki_segments.session_total_tokens`, then later recreates `wiki_segments` to change the uniqueness constraint. The recreated table and copy statement omit `session_total_tokens`, so older databases can migrate into a schema where later SQL references a missing column.

This is not about clearing the live OpenClaw session token counter. The current session-level counter belongs on the wiki/session state and can be reset when starting a new OpenClaw session. The bug is that the physical database column can disappear during upgrade.

**Consequence:** upgraded databases can fail with `no such column: session_total_tokens` when worker or dashboard queries reference the column.

**Fix:**

- Include `session_total_tokens INTEGER` in the recreated `wiki_segments` table.
- Include it in the `INSERT INTO ... SELECT ...` copy list.
- Optionally call `_ensure_column(conn, "wiki_segments", "session_total_tokens", "INTEGER")` again after the recreate block as a belt-and-suspenders guard.

**Regression test:**

- Create a synthetic old-schema database with the old `wiki_segments` unique constraint and no `session_total_tokens`.
- Run `db.connect()` / migration.
- Assert `PRAGMA table_info(wiki_segments)` includes `session_total_tokens`.
- Assert existing segment rows survive.

## 2. Validate wiki slugs before deleting paths

**Priority:** P1

**Files:** `app/wiki_builder/dashboard.py`, `scripts/init_wiki.py`

The dashboard and CLI accept a slug and may delete `WIKI_ROOT / slug` when force recreation is requested. A slug with path components such as `../` can resolve outside `data/wikis`.

**Consequence:** a malformed or malicious slug can delete files outside the wiki root.

**Fix:**

- Validate slugs with a strict full-match pattern: `^[a-z0-9][a-z0-9-]*$`.
- Resolve the target path and verify it is inside `WIKI_ROOT` before any deletion.
- Share the validation helper between dashboard and CLI if possible.

**Regression test:**

- Assert valid slugs like `red-rising` and `book-2` pass.
- Assert `../outside`, `foo/bar`, `.hidden`, empty string, uppercase, and spaces fail.
- Assert force recreate refuses to delete paths outside `WIKI_ROOT`.

## 3. Strengthen deterministic wiki checks without over-constraining prose

**Priority:** P2

**Files:** `app/wiki_builder/checks.py`, `tests/test_checks.py`

The current checks enforce path shape, protected file deletion, `## Summary`, UTF-8, and banned phrases. They do not enforce several core prompt requirements: content pages should actually change for non-empty segments, links should point to real files, first mentions of existing subjects should usually be linked, and `open-questions.md` should have only one `Recently resolved` section.

**Consequence:** the agent can produce a formally valid but low-value snapshot, such as an `index.md`-only update or pages that mention known subjects without links.

**Fix approach:**

Keep the harness split into two layers:

- **Hard failures:** safety and structural integrity. These should block apply.
- **Soft diagnostics:** quality concerns. These should be shown to the agent or dashboard, but should not automatically cause repeated retries unless they are severe.

Good hard checks:

- All internal markdown links resolve to existing files after the edit set is applied.
- Exactly one `## Recently resolved` section in `open-questions.md`, if the section exists.
- No content page is missing `## Summary`.
- No protected files are deleted.
- No writes outside the allowed wiki path set.

Good soft diagnostics:

- Non-empty segment changed only `index.md` or only `open-questions.md`.
- A changed content page mentions a known page title or alias without linking the first mention.
- Central pages shrink materially.
- `open-questions.md` has too few active questions after a segment with new mysteries, new names, or ambiguous scenes.
- New pages are very short and may be walk-on pages rather than durable wiki subjects.

**Regression tests:**

- Index-only update on a non-empty segment produces at least a warning.
- Broken internal link fails.
- Duplicate `Recently resolved` sections fail.
- Existing page title mentioned without first-link coverage produces a warning.

## 4. Coordinate git commit/tag with database state

**Priority:** P2

**Files:** `app/wiki_builder/worker.py`, `app/wiki_builder/applier.py`

The worker commits and tags the wiki repository before updating `wiki_segments`. If the DB update fails after `commit_and_tag` succeeds, the repo contains `seg-NNNN` but the DB still thinks the segment is pending.

**Consequence:** reruns can duplicate work, fail on an existing tag, or leave dashboard state inconsistent with the repo.

**Fix options:**

1. Mark the segment `applying` before mutating the repo, then mark `applied` only after commit/tag succeeds.
2. If the DB update fails after commit/tag, roll back the tag and commit where safe.
3. Make reruns idempotent: if `seg-NNNN` already exists and the DB row is still pending, verify the tag and repair the DB row instead of applying again.

The idempotent repair path is the most forgiving for local tooling.

**Regression test:**

- Simulate a DB failure after commit/tag.
- Rerun the worker.
- Assert it does not create a second commit for the same segment and repairs or reports the inconsistent state clearly.

## 5. Do not silently fall back from a selected v2 wiki

**Priority:** P2

**Files:** `app/main.py`

When a user explicitly selects a segment-built wiki, Q&A catches exceptions from `load_selected_wiki_context` and falls back to the legacy wiki path.

**Consequence:** Q&A can answer using a different knowledge source and spoiler boundary than the user selected. Broken tags, missing repos, or migration problems can be hidden.

**Fix:**

- If no selected wiki exists, legacy fallback is fine.
- If a selected wiki exists and loading it fails, return a visible error or warning instead of silently using the legacy wiki.
- Include enough diagnostic detail for local debugging: wiki id, slug, selected segment/tag, and the failing operation.

**Regression test:**

- Create a selected v2 wiki with a broken repo/tag.
- Ask a Q&A query.
- Assert the response surfaces the selected-wiki failure and does not use legacy context.

## Suggested Order

1. Fix the DB migration and slug deletion issues first. They are correctness/safety bugs.
2. Fix selected-wiki fallback next, because it affects user trust in Q&A.
3. Add idempotent commit/tag recovery before running long wiki builds.
4. Expand checks in hard/soft layers so quality can improve without creating retry loops.
