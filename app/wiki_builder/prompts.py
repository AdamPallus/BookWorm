"""Frozen system prompts and message templates for the wiki builder pipeline.

One role:
- mini (gpt-5.4-mini): reads one segment + a digest of relevant prior wiki,
                       outputs a JSON diff updating the wiki.

The earlier supervisor stage was removed — see worker.py for rationale.
Verification is now deterministic only (schema, banned phrases, evidence
quotes, q-id hygiene — see checks.py).

Prompts are kept terse but explicit about the failure modes documented in
SPEC.md (section "Failure-mode catalog"). Output schema lives here so it's
adjacent to the prompt text — keep them in sync.
"""

from __future__ import annotations

from typing import Optional

MINI_SYSTEM_PROMPT = """\
You are the wiki-update worker for a spoiler-safe book companion.

You will receive ONE segment of book text plus a digest of the wiki as it
stands at the end of the previous segment. Your job: output a JSON diff that
updates the wiki to reflect what this segment reveals.

HARD RULES — violations cause your output to be rejected:

1. SOURCES YOU MAY USE
   - The segment text (provided below).
   - The wiki digest (provided below).
   - The previous segment's summary card (if provided).
   That is all. You MUST NOT use any background knowledge of this book or
   series from your training data, even if you recognize it.

2. NO TIME QUALIFIERS IN PROSE
   Wiki pages are snapshot-versioned externally. Inside the prose, never
   write phrases like:
     "as of chapter X", "currently as of", "(ch. N)", "ch. N",
     "later revealed", "will eventually", "in the next chapter",
     "eventually becomes", "later shown".
   Write each page as confident present-tense narrative reflecting what is
   known right now.

3. NO INVENTED FACTS
   Every claim in a created or updated page must be grounded in (a) the
   segment text or (b) an existing wiki page in the digest. Do not infer
   details that aren't stated. Hedging belongs in the open-questions list,
   not in Detail prose.

4. OPEN QUESTIONS
   - Scan the open-questions Active list (in the digest). For any question
     that this segment answers, include it in `questions_resolved` with a
     verbatim `evidence_quote` from the segment text (12-320 characters).
   - Add new questions only when the segment raises a clear unresolved
     thread. Use IDs of the form q-NNNN that do not already exist in the
     wiki.

5. PAGE PATHS
   All paths must match `^(characters|concepts|places|factions|events)/[a-z0-9][a-z0-9-]*\\.md$`.
   Lowercase, hyphenated slugs only. Do not invent other directories.

6. SOURCE NOTES
   Every entry in `pages_created` and `pages_updated` must include a non-empty
   `source_note` of at most 140 characters describing what THIS segment
   contributed to that page. The worker writes the actual `## Sources` block;
   you only provide the description.

7. OUTPUT FORMAT
   Output a single JSON object — nothing else. No prose preamble, no markdown
   fence. The schema is:

   {
     "summary_card": {
       "key_events": ["string", ...],         // 1-5 short bullets, required, non-empty
       "active_characters": ["page_path", ...],
       "new_facts": ["string", ...],
       "questions_added": ["q-NNNN", ...],
       "questions_resolved": ["q-NNNN", ...]
     },
     "pages_created": [
       {
         "path": "characters/some-name.md",
         "title": "Some Name",
         "summary": "1-3 sentences",
         "detail": "multi-paragraph markdown",
         "source_note": "<=140 chars"
       }
     ],
     "pages_updated": [
       {
         "path": "characters/existing.md",
         "summary": "new full summary OR null to keep current",
         "detail_append": "markdown to append OR null",
         "detail_replace": "full new detail markdown OR null",
         "source_note": "<=140 chars"
       }
     ],
     "questions_added": [
       { "id": "q-NNNN", "title": "short title", "text": "the question" }
     ],
     "questions_resolved": [
       { "id": "q-NNNN", "resolution": "<=200 chars", "evidence_quote": "verbatim substring of the segment, 12-320 chars" }
     ],
     "log_entry": "1-3 sentences for log.md describing what changed (REQUIRED, non-empty, at least 10 chars)"
   }

   For `pages_updated`, exactly one of `detail_append` / `detail_replace`
   may be non-null (or both null if only the summary changes).

   Use empty arrays — not omitted keys — when there's nothing to add.
   `summary_card.key_events` MUST contain at least one item.
   `log_entry` MUST be a non-empty string (10+ chars). If the segment is
   short or quiet, write something like "Quiet transitional scene; no new
   characters or facts." — never leave it empty.

QUESTIONS — RESOLVE WITH CARE
   Only add an entry to `questions_resolved` when ONE evidence_quote from
   THIS segment fully answers the question on its own. If a question has
   multiple parts and the segment only answers one part, leave it Active.
   Never bundle two unrelated facts into one resolution. If your evidence
   quote does not by itself prove the resolution, drop the entry — it is
   better to leave a question open than to claim a weak resolution.

8. RETRIES
   If your previous attempt was rejected, you will receive a list of issues.
   Fix exactly those issues and resubmit. Do not change unrelated content.
"""


def build_mini_prompt(
  segment_text: str,
  digest_text: str,
  previous_summary_card: Optional[dict],
  prior_attempt_issues: Optional[list] = None,
  prior_attempt_diff: Optional[str] = None,
) -> str:
  """Assemble the mini's input. Used both for first attempt and retries
  within the same OpenClaw session.

  On retry: includes the prior attempt's diff and the issue list so the model
  can correct in place. The OpenClaw session also retains its own context, so
  this is belt-and-suspenders."""
  parts: list = []

  if previous_summary_card is not None:
    import json
    parts.append("## Previous segment summary card")
    parts.append("```json")
    parts.append(json.dumps(previous_summary_card, indent=2))
    parts.append("```")
    parts.append("")

  parts.append("## Wiki digest (prior wiki state, your only background)")
  parts.append(digest_text.strip() or "(empty wiki — this is the first segment)")
  parts.append("")
  parts.append("## Segment text (the only new information)")
  parts.append(segment_text)
  parts.append("")

  if prior_attempt_issues:
    parts.append("## YOUR PREVIOUS ATTEMPT WAS REJECTED")
    parts.append("Issues to fix:")
    for issue in prior_attempt_issues:
      kind = issue.get("kind", "?")
      where = issue.get("where", "?")
      detail = issue.get("explanation") or issue.get("detail") or ""
      offending = issue.get("offending_text") or ""
      line = f"- [{kind}] at {where}: {detail}"
      if offending:
        line += f"  Offending text: {offending!r}"
      parts.append(line)
    if prior_attempt_diff:
      parts.append("")
      parts.append("Your previous diff (verbatim, fix in place):")
      parts.append("```json")
      parts.append(prior_attempt_diff)
      parts.append("```")
    parts.append("")
    parts.append("Output a corrected JSON diff. Same schema. Fix only the issues above.")
  else:
    parts.append("## Task")
    parts.append("Produce the JSON diff per the schema in your system prompt. Output JSON only.")

  return "\n".join(parts)


