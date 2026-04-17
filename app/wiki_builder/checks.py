import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set


PAGE_PATH_RE = re.compile(r"^(?:characters|concepts|places|factions|events)/[a-z0-9][a-z0-9-]*\.md$")
QUESTION_ID_RE = re.compile(r"^q-\d{4}$")

BANNED_PATTERNS = [
  ("as-of-chapter", re.compile(r"\bas\s+of\s+(?:ch(?:apter)?\.?\s*\d+)\b", re.IGNORECASE)),
  ("currently-as-of", re.compile(r"\bcurrent(?:ly)?\s+as\s+of\b", re.IGNORECASE)),
  ("inline-ch-citation", re.compile(r"\(\s*ch\.\s*\d+\s*\)", re.IGNORECASE)),
  ("bare-ch-citation", re.compile(r"(?<![A-Za-z])ch\.\s*\d+\b", re.IGNORECASE)),
  ("later-revealed", re.compile(r"\blater\s+(?:revealed|shown|discovered|learned)\b", re.IGNORECASE)),
  ("will-eventually", re.compile(
    r"\bwill\s+(?:later|eventually|soon)\s+(?:be\s+)?(?:revealed|reveal|become|turn\s+out|happen)\b",
    re.IGNORECASE,
  )),
  ("next-chapter", re.compile(r"\bin\s+the\s+next\s+chapter\b", re.IGNORECASE)),
  ("eventually-becomes", re.compile(r"\beventually\s+(?:becomes|reveals|turns\s+out)\b", re.IGNORECASE)),
]

PROSE_FIELDS_PAGE = ("summary", "detail", "detail_append", "detail_replace", "source_note")
PROSE_FIELDS_QUESTION_RESOLVE = ("resolution",)

MAX_SOURCE_NOTE_CHARS = 140
MIN_EVIDENCE_CHARS = 12
MAX_EVIDENCE_CHARS = 320


@dataclass
class CheckIssue:
  kind: str
  where: str
  detail: str

  def as_dict(self) -> Dict[str, str]:
    return {"kind": self.kind, "where": self.where, "detail": self.detail}


@dataclass
class CheckResult:
  ok: bool
  issues: List[CheckIssue]

  def as_dict(self) -> Dict[str, Any]:
    return {"ok": self.ok, "issues": [i.as_dict() for i in self.issues]}


def _add(issues: List[CheckIssue], kind: str, where: str, detail: str):
  issues.append(CheckIssue(kind=kind, where=where, detail=detail))


def _is_str(value: Any) -> bool:
  return isinstance(value, str)


def _is_str_list(value: Any) -> bool:
  return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _normalize_for_substring(text: str) -> str:
  return re.sub(r"\s+", " ", (text or "").lower()).strip()


def check_diff_schema(diff: Any) -> List[CheckIssue]:
  issues: List[CheckIssue] = []
  if not isinstance(diff, dict):
    _add(issues, "schema", "<root>", "diff must be an object")
    return issues

  card = diff.get("summary_card")
  if not isinstance(card, dict):
    _add(issues, "schema", "summary_card", "missing or not an object")
  else:
    key_events = card.get("key_events")
    if not _is_str_list(key_events) or len(key_events) == 0:
      _add(issues, "schema", "summary_card.key_events", "must be a non-empty list of strings")
    for field in ("active_characters", "new_facts", "questions_added", "questions_resolved"):
      val = card.get(field)
      if not _is_str_list(val):
        _add(issues, "schema", f"summary_card.{field}", "must be a list of strings")

  for field in ("pages_created", "pages_updated", "questions_added", "questions_resolved"):
    val = diff.get(field)
    if not isinstance(val, list):
      _add(issues, "schema", field, "must be a list")

  log_entry = diff.get("log_entry")
  if not _is_str(log_entry) or not log_entry.strip():
    _add(issues, "schema", "log_entry", "must be a non-empty string")

  for i, page in enumerate(diff.get("pages_created") or []):
    where = f"pages_created[{i}]"
    if not isinstance(page, dict):
      _add(issues, "schema", where, "must be an object")
      continue
    for field in ("path", "title", "summary", "detail", "source_note"):
      v = page.get(field)
      if not _is_str(v) or not v.strip():
        _add(issues, "schema", f"{where}.{field}", "missing or empty")
    path = page.get("path", "")
    if isinstance(path, str) and not PAGE_PATH_RE.match(path):
      _add(issues, "schema", f"{where}.path", f"path does not match expected shape: {path!r}")

  for i, page in enumerate(diff.get("pages_updated") or []):
    where = f"pages_updated[{i}]"
    if not isinstance(page, dict):
      _add(issues, "schema", where, "must be an object")
      continue
    path = page.get("path", "")
    if not _is_str(path) or not PAGE_PATH_RE.match(path):
      _add(issues, "schema", f"{where}.path", f"path does not match expected shape: {path!r}")
    summary = page.get("summary")
    if summary is not None and not _is_str(summary):
      _add(issues, "schema", f"{where}.summary", "must be string or null")
    append = page.get("detail_append")
    replace = page.get("detail_replace")
    if append is not None and not _is_str(append):
      _add(issues, "schema", f"{where}.detail_append", "must be string or null")
    if replace is not None and not _is_str(replace):
      _add(issues, "schema", f"{where}.detail_replace", "must be string or null")
    if append and replace:
      _add(issues, "schema", where, "detail_append and detail_replace are mutually exclusive")
    note = page.get("source_note")
    if not _is_str(note) or not note.strip():
      _add(issues, "schema", f"{where}.source_note", "missing or empty")

  for i, q in enumerate(diff.get("questions_added") or []):
    where = f"questions_added[{i}]"
    if not isinstance(q, dict):
      _add(issues, "schema", where, "must be an object")
      continue
    qid = q.get("id")
    if not _is_str(qid) or not QUESTION_ID_RE.match(qid):
      _add(issues, "schema", f"{where}.id", f"id does not match q-NNNN: {qid!r}")
    for field in ("title", "text"):
      v = q.get(field)
      if not _is_str(v) or not v.strip():
        _add(issues, "schema", f"{where}.{field}", "missing or empty")

  for i, q in enumerate(diff.get("questions_resolved") or []):
    where = f"questions_resolved[{i}]"
    if not isinstance(q, dict):
      _add(issues, "schema", where, "must be an object")
      continue
    qid = q.get("id")
    if not _is_str(qid) or not QUESTION_ID_RE.match(qid):
      _add(issues, "schema", f"{where}.id", f"id does not match q-NNNN: {qid!r}")
    for field in ("resolution", "evidence_quote"):
      v = q.get(field)
      if not _is_str(v) or not v.strip():
        _add(issues, "schema", f"{where}.{field}", "missing or empty")

  return issues


def _scan_banned(text: str, where: str, issues: List[CheckIssue]):
  if not text:
    return
  for label, pattern in BANNED_PATTERNS:
    m = pattern.search(text)
    if m:
      _add(
        issues,
        "banned-phrase",
        where,
        f"{label}: {m.group(0)!r} (offset {m.start()})",
      )


def check_banned_phrases(diff: Any) -> List[CheckIssue]:
  issues: List[CheckIssue] = []
  if not isinstance(diff, dict):
    return issues

  for i, page in enumerate(diff.get("pages_created") or []):
    if not isinstance(page, dict):
      continue
    for field in PROSE_FIELDS_PAGE:
      _scan_banned(page.get(field) or "", f"pages_created[{i}].{field}", issues)

  for i, page in enumerate(diff.get("pages_updated") or []):
    if not isinstance(page, dict):
      continue
    for field in PROSE_FIELDS_PAGE:
      _scan_banned(page.get(field) or "", f"pages_updated[{i}].{field}", issues)

  for i, q in enumerate(diff.get("questions_resolved") or []):
    if not isinstance(q, dict):
      continue
    for field in PROSE_FIELDS_QUESTION_RESOLVE:
      _scan_banned(q.get(field) or "", f"questions_resolved[{i}].{field}", issues)

  log_entry = diff.get("log_entry") if isinstance(diff, dict) else None
  if isinstance(log_entry, str):
    _scan_banned(log_entry, "log_entry", issues)

  return issues


def check_source_attribution(diff: Any) -> List[CheckIssue]:
  issues: List[CheckIssue] = []
  if not isinstance(diff, dict):
    return issues

  for group in ("pages_created", "pages_updated"):
    for i, page in enumerate(diff.get(group) or []):
      if not isinstance(page, dict):
        continue
      note = page.get("source_note")
      where = f"{group}[{i}].source_note"
      if not _is_str(note) or not note.strip():
        _add(issues, "missing-source", where, "source_note is empty")
        continue
      if len(note) > MAX_SOURCE_NOTE_CHARS:
        _add(
          issues,
          "missing-source",
          where,
          f"source_note longer than {MAX_SOURCE_NOTE_CHARS} chars (got {len(note)})",
        )
  return issues


def check_question_evidence(diff: Any, segment_text: str) -> List[CheckIssue]:
  issues: List[CheckIssue] = []
  if not isinstance(diff, dict):
    return issues
  haystack = _normalize_for_substring(segment_text)
  for i, q in enumerate(diff.get("questions_resolved") or []):
    if not isinstance(q, dict):
      continue
    quote = q.get("evidence_quote")
    where = f"questions_resolved[{i}].evidence_quote"
    if not _is_str(quote) or not quote.strip():
      _add(issues, "missing-evidence", where, "evidence_quote is empty")
      continue
    qlen = len(quote)
    if qlen < MIN_EVIDENCE_CHARS:
      _add(issues, "missing-evidence", where, f"evidence_quote shorter than {MIN_EVIDENCE_CHARS} chars")
      continue
    if qlen > MAX_EVIDENCE_CHARS:
      _add(issues, "missing-evidence", where, f"evidence_quote longer than {MAX_EVIDENCE_CHARS} chars")
      continue
    needle = _normalize_for_substring(quote)
    if needle not in haystack:
      _add(
        issues,
        "missing-evidence",
        where,
        f"evidence_quote not found in segment text: {quote[:80]!r}",
      )
  return issues


def check_question_id_hygiene(
  diff: Any,
  existing_question_ids: Iterable[str],
  active_question_ids: Iterable[str],
) -> List[CheckIssue]:
  issues: List[CheckIssue] = []
  if not isinstance(diff, dict):
    return issues
  existing: Set[str] = set(existing_question_ids)
  active: Set[str] = set(active_question_ids)

  added_ids: List[str] = []
  for i, q in enumerate(diff.get("questions_added") or []):
    if not isinstance(q, dict):
      continue
    qid = q.get("id")
    where = f"questions_added[{i}].id"
    if not _is_str(qid) or not QUESTION_ID_RE.match(qid):
      continue
    if qid in existing:
      _add(issues, "duplicate-question-id", where, f"{qid} is already in the wiki")
    if qid in added_ids:
      _add(issues, "duplicate-question-id", where, f"{qid} is added more than once in this diff")
    added_ids.append(qid)

  resolved_ids: List[str] = []
  for i, q in enumerate(diff.get("questions_resolved") or []):
    if not isinstance(q, dict):
      continue
    qid = q.get("id")
    where = f"questions_resolved[{i}].id"
    if not _is_str(qid) or not QUESTION_ID_RE.match(qid):
      continue
    if qid not in active and qid not in added_ids:
      _add(issues, "unknown-question-id", where, f"{qid} is not in the active questions")
    if qid in resolved_ids:
      _add(issues, "duplicate-question-id", where, f"{qid} is resolved more than once in this diff")
    resolved_ids.append(qid)

  return issues


def run_all_checks(
  diff: Any,
  segment_text: str,
  existing_question_ids: Optional[Iterable[str]] = None,
  active_question_ids: Optional[Iterable[str]] = None,
) -> CheckResult:
  issues: List[CheckIssue] = []
  schema_issues = check_diff_schema(diff)
  issues.extend(schema_issues)
  if schema_issues:
    return CheckResult(ok=False, issues=issues)

  issues.extend(check_banned_phrases(diff))
  issues.extend(check_source_attribution(diff))
  issues.extend(check_question_evidence(diff, segment_text))
  issues.extend(
    check_question_id_hygiene(
      diff,
      existing_question_ids or [],
      active_question_ids or [],
    )
  )
  return CheckResult(ok=not issues, issues=issues)
