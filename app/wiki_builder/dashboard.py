"""Human-in-the-loop wiki builder dashboard.

Mounted into the main FastAPI app. Provides:

- A single-page HTML UI at `/wiki-builder` for managing wiki builds.
- API endpoints under `/api/wiki-builder/*` that wrap the existing init /
  build / view scripts.

Run lifecycle: one active subprocess at a time (across all wikis). The
subprocess is the existing `python -m scripts.build_wiki ...` command. We
capture its stdout into an in-memory ring of log lines that the browser
polls. Stop/cancel sends SIGTERM.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import markdown
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .. import db
from .worker import COMPACT_AT_TOTAL_TOKENS

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WIKI_ROOT = REPO_ROOT / "data" / "wikis"
STATIC_DIR = REPO_ROOT / "static"

PAGE_DIRS = ("characters", "concepts", "places", "factions", "events")

router = APIRouter()


# -------------------------------------------------------------------- runs --


@dataclass
class ActiveRun:
  slug: str
  proc: subprocess.Popen
  started_at: float
  max_segments: Optional[int]
  mini_model: Optional[str] = None
  log: List[str] = field(default_factory=list)
  reader: Optional[threading.Thread] = None
  finished_at: Optional[float] = None
  exit_code: Optional[int] = None


class RunRegistry:
  """One active build at a time, plus the most recently finished build per
  wiki for log inspection."""

  def __init__(self) -> None:
    self._lock = threading.Lock()
    self._active: Optional[ActiveRun] = None
    self._history: dict[str, ActiveRun] = {}

  def active(self) -> Optional[ActiveRun]:
    with self._lock:
      return self._active

  def history_for(self, slug: str) -> Optional[ActiveRun]:
    with self._lock:
      return self._history.get(slug)

  def start(
    self,
    slug: str,
    max_segments: Optional[int],
    *,
    reset_session: bool = False,
    mini_model: Optional[str] = None,
  ) -> ActiveRun:
    with self._lock:
      if self._active is not None and self._active.proc.poll() is None:
        raise HTTPException(409, f"a build is already running for '{self._active.slug}'")

      args = [
        sys.executable,
        "-m",
        "scripts.build_wiki",
        "--wiki-slug",
        slug,
        "--segment-interval-seconds",
        "5",
      ]
      if max_segments is not None:
        args += ["--max-segments", str(max_segments)]
      if reset_session:
        args += ["--reset-session"]
      if mini_model:
        args += ["--mini-model", mini_model]

      env = os.environ.copy()
      env.setdefault("PYTHONUNBUFFERED", "1")

      proc = subprocess.Popen(
        args,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
      )

      run = ActiveRun(
        slug=slug,
        proc=proc,
        started_at=time.time(),
        max_segments=max_segments,
        mini_model=mini_model,
        log=[f"[harness] starting: {' '.join(shlex.quote(a) for a in args)}"],
      )
      self._active = run
      self._history[slug] = run

    reader = threading.Thread(target=self._drain, args=(run,), daemon=True)
    reader.start()
    run.reader = reader
    return run

  def stop(self, slug: str) -> bool:
    with self._lock:
      run = self._active
      if run is None or run.slug != slug:
        return False
      if run.proc.poll() is None:
        try:
          run.proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
          pass
        run.log.append("[harness] SIGTERM sent; waiting for process to exit")
        return True
      return False

  def _drain(self, run: ActiveRun) -> None:
    assert run.proc.stdout is not None
    for line in run.proc.stdout:
      with self._lock:
        run.log.append(line.rstrip("\n"))
    run.proc.wait()
    with self._lock:
      run.finished_at = time.time()
      run.exit_code = run.proc.returncode
      run.log.append(f"[harness] process exited code={run.exit_code}")
      if self._active is run:
        self._active = None


REGISTRY = RunRegistry()


# ------------------------------------------------------------ wiki helpers --


def _wiki_status_counts(conn, wiki_id: int) -> dict:
  rows = conn.execute(
    "SELECT status, COUNT(*) FROM wiki_segments WHERE wiki_id = ? GROUP BY status",
    (wiki_id,),
  ).fetchall()
  counts = {"pending": 0, "applied": 0, "absorbed": 0, "quarantined": 0}
  for status, count in rows:
    counts[status] = count
  total = sum(counts.values())
  return {**counts, "total": total}


def _list_wikis(conn) -> List[dict]:
  rows = conn.execute(
    """
    SELECT id, slug, name, repo_path,
           openclaw_session_id, openclaw_session_total_tokens,
           compact_at_total_tokens
    FROM wikis ORDER BY id ASC
    """
  ).fetchall()
  out = []
  for row in rows:
    counts = _wiki_status_counts(conn, row["id"])
    repo = Path(row["repo_path"])
    page_count = 0
    if (repo / "wiki").exists():
      for sub in PAGE_DIRS:
        sub_dir = repo / "wiki" / sub
        if sub_dir.exists():
          page_count += sum(1 for _ in sub_dir.glob("*.md"))
    out.append({
      "id": row["id"],
      "slug": row["slug"],
      "name": row["name"],
      "repo_path": str(repo),
      "repo_exists": (repo / ".git").exists(),
      "page_count": page_count,
      "segments": counts,
      "openclaw_session_id": row["openclaw_session_id"],
      "openclaw_session_total_tokens": row["openclaw_session_total_tokens"],
      "compact_at_total_tokens": row["compact_at_total_tokens"],
    })
  return out


def _list_books(conn) -> List[dict]:
  rows = conn.execute(
    """
    SELECT b.id, b.title, b.author, b.embedding_status, b.total_chunks,
           (SELECT COUNT(*) FROM wiki_books wb WHERE wb.book_id = b.id) AS wiki_count
    FROM books b
    ORDER BY b.id ASC
    """
  ).fetchall()
  return [
    {
      "id": r["id"],
      "title": r["title"],
      "author": r["author"],
      "embedding_status": r["embedding_status"],
      "total_chunks": r["total_chunks"],
      "wiki_count": r["wiki_count"],
    }
    for r in rows
  ]


SLUG_RE = re.compile(r"[^a-z0-9]+")


def _suggest_slug(name: str) -> str:
  base = SLUG_RE.sub("-", name.lower()).strip("-")
  return base or "untitled-wiki"


# -------------------------------------------------------------- API models --


class InitWikiRequest(BaseModel):
  book_id: int
  slug: Optional[str] = None
  name: Optional[str] = None
  target_tokens: int = 7000
  min_tokens: int = 3000
  max_tokens: int = 12000
  absorb_below_tokens: int = 1000
  force_recreate: bool = False


class RunRequest(BaseModel):
  max_segments: Optional[int] = None
  reset_session: bool = False
  # Backend model the agent runs on. None defaults to openclaw_client's
  # DEFAULT_MINI_MODEL (gpt-5.4-mini).
  mini_model: Optional[str] = None


class CompactionThresholdRequest(BaseModel):
  # null clears the override and falls back to the worker's default.
  compact_at_total_tokens: Optional[int] = None


class AddBookRequest(BaseModel):
  wiki_slug: str
  book_id: int
  target_tokens: int = 7000
  min_tokens: int = 3000
  max_tokens: int = 12000
  absorb_below_tokens: int = 1000
  skip_front_matter_chapters: Optional[str] = None  # e.g. "0,1"


# ------------------------------------------------------------ HTML / shell --


@router.get("/wiki-builder", response_class=HTMLResponse)
def serve_dashboard():
  html = (STATIC_DIR / "wiki-builder.html").read_text()
  return HTMLResponse(html)


# ---------------------------------------------------------------- API: list -


@router.get("/api/wiki-builder/wikis")
def list_wikis_endpoint():
  conn = db.connect()
  try:
    return {
      "wikis": _list_wikis(conn),
      # The default threshold used when a wiki has no per-row override.
      "compact_at_total_tokens": COMPACT_AT_TOTAL_TOKENS,
    }
  finally:
    conn.close()


@router.put("/api/wiki-builder/wiki/{slug}/compaction-threshold")
def set_compaction_threshold_endpoint(slug: str, req: CompactionThresholdRequest):
  """Override the wiki's session-rotation threshold.

  Pass `compact_at_total_tokens: null` to clear the override and fall back
  to the worker's default. Must be a positive integer otherwise — the
  threshold is checked against the OpenClaw session's cumulative
  total_tokens, so values below ~50k are nonsense.
  """
  value = req.compact_at_total_tokens
  if value is not None and value <= 0:
    raise HTTPException(400, "compact_at_total_tokens must be positive or null")
  conn = db.connect()
  try:
    row = conn.execute("SELECT id FROM wikis WHERE slug = ?", (slug,)).fetchone()
    if not row:
      raise HTTPException(404, f"no wiki with slug {slug!r}")
    conn.execute(
      "UPDATE wikis SET compact_at_total_tokens = ? WHERE id = ?",
      (value, row["id"]),
    )
    conn.commit()
    return {
      "slug": slug,
      "compact_at_total_tokens": value,
      "default_compact_at_total_tokens": COMPACT_AT_TOTAL_TOKENS,
    }
  finally:
    conn.close()


@router.get("/api/wiki-builder/wiki/{slug}/session-history")
def session_history_endpoint(slug: str):
  """Per-segment session_total_tokens readings — the curve behind the
  session-size indicator. Ordered by story_order."""
  conn = db.connect()
  try:
    row = conn.execute("SELECT id FROM wikis WHERE slug = ?", (slug,)).fetchone()
    if not row:
      raise HTTPException(404, f"no wiki with slug {slug!r}")
    segments = conn.execute(
      """
      SELECT story_order, snapshot_tag, status, session_total_tokens
      FROM wiki_segments
      WHERE wiki_id = ? AND session_total_tokens IS NOT NULL
      ORDER BY story_order ASC
      """,
      (row["id"],),
    ).fetchall()
    return {
      "slug": slug,
      "compact_at_total_tokens": COMPACT_AT_TOTAL_TOKENS,
      "points": [
        {
          "story_order": r["story_order"],
          "snapshot_tag": r["snapshot_tag"],
          "status": r["status"],
          "total_tokens": r["session_total_tokens"],
        }
        for r in segments
      ],
    }
  finally:
    conn.close()


@router.get("/api/wiki-builder/books")
def list_books_endpoint():
  conn = db.connect()
  try:
    return {"books": _list_books(conn)}
  finally:
    conn.close()


# ----------------------------------------------------------- API: lifecycle -


@router.post("/api/wiki-builder/init")
def init_wiki_endpoint(req: InitWikiRequest):
  conn = db.connect()
  try:
    book = conn.execute("SELECT id, title FROM books WHERE id = ?", (req.book_id,)).fetchone()
    if not book:
      raise HTTPException(404, f"book id {req.book_id} not found")
    name = req.name or book["title"]
    slug = req.slug or _suggest_slug(name)
  finally:
    conn.close()

  args = [
    sys.executable, "-m", "scripts.init_wiki",
    "--slug", slug,
    "--name", name,
    "--book-id", str(req.book_id),
    "--target-tokens", str(req.target_tokens),
    "--min-tokens", str(req.min_tokens),
    "--max-tokens", str(req.max_tokens),
    "--absorb-below-tokens", str(req.absorb_below_tokens),
  ]
  if req.force_recreate:
    args.append("--force-recreate")
    repo_path = WIKI_ROOT / slug
    if repo_path.exists():
      import shutil
      shutil.rmtree(repo_path)

  result = subprocess.run(
    args, cwd=str(REPO_ROOT),
    capture_output=True, text=True,
  )
  if result.returncode != 0:
    raise HTTPException(
      500,
      detail={"stdout": result.stdout, "stderr": result.stderr},
    )
  return {
    "slug": slug,
    "name": name,
    "stdout": result.stdout.splitlines(),
  }


@router.post("/api/wiki-builder/add-book")
def add_book_endpoint(req: AddBookRequest):
  """Append a book to an existing wiki — the next book in a series.

  Does not touch the on-disk wiki repo. Just inserts wiki_books +
  wiki_segments rows so the worker picks the new book's segments up after
  the existing book's segments are done. The worker auto-detects the
  book transition and starts a fresh OpenClaw session for it.
  """
  conn = db.connect()
  try:
    wiki = conn.execute(
      "SELECT id, name FROM wikis WHERE slug = ?", (req.wiki_slug,),
    ).fetchone()
    if not wiki:
      raise HTTPException(404, f"no wiki with slug {req.wiki_slug!r}")
    book = conn.execute(
      "SELECT id, title FROM books WHERE id = ?", (req.book_id,),
    ).fetchone()
    if not book:
      raise HTTPException(404, f"book id {req.book_id} not found")
    already = conn.execute(
      "SELECT 1 FROM wiki_books WHERE wiki_id = ? AND book_id = ?",
      (wiki["id"], req.book_id),
    ).fetchone()
    if already:
      raise HTTPException(
        409,
        f"book id {req.book_id} ({book['title']!r}) is already linked to wiki "
        f"{req.wiki_slug!r}",
      )
  finally:
    conn.close()

  args = [
    sys.executable, "-m", "scripts.add_book_to_wiki",
    "--slug", req.wiki_slug,
    "--book-id", str(req.book_id),
    "--target-tokens", str(req.target_tokens),
    "--min-tokens", str(req.min_tokens),
    "--max-tokens", str(req.max_tokens),
    "--absorb-below-tokens", str(req.absorb_below_tokens),
  ]
  if req.skip_front_matter_chapters:
    args += ["--skip-front-matter-chapters", req.skip_front_matter_chapters]

  result = subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True, text=True)
  if result.returncode != 0:
    raise HTTPException(
      500, detail={"stdout": result.stdout, "stderr": result.stderr},
    )
  return {
    "wiki_slug": req.wiki_slug,
    "book_id": req.book_id,
    "stdout": result.stdout.splitlines(),
  }


@router.post("/api/wiki-builder/run/{slug}")
def start_run_endpoint(slug: str, req: RunRequest):
  conn = db.connect()
  try:
    row = conn.execute("SELECT id FROM wikis WHERE slug = ?", (slug,)).fetchone()
    if not row:
      raise HTTPException(404, f"no wiki with slug {slug!r}")
  finally:
    conn.close()

  run = REGISTRY.start(
    slug, req.max_segments,
    reset_session=req.reset_session,
    mini_model=req.mini_model,
  )
  return {
    "slug": slug,
    "started_at": run.started_at,
    "pid": run.proc.pid,
    "max_segments": run.max_segments,
    "reset_session": req.reset_session,
    "mini_model": run.mini_model,
  }


@router.post("/api/wiki-builder/run/{slug}/stop")
def stop_run_endpoint(slug: str):
  ok = REGISTRY.stop(slug)
  return {"stopped": ok}


@router.get("/api/wiki-builder/run/{slug}/log")
def get_run_log_endpoint(slug: str, since: int = Query(0, ge=0)):
  active = REGISTRY.active()
  if active and active.slug == slug:
    run = active
  else:
    run = REGISTRY.history_for(slug)
  if run is None:
    return {"slug": slug, "running": False, "lines": [], "next_since": since, "exit_code": None}

  with REGISTRY._lock:
    total = len(run.log)
    lines = run.log[since:]
    running = run.proc.poll() is None
    exit_code = run.exit_code if not running else None
  return {
    "slug": slug,
    "running": running,
    "lines": lines,
    "next_since": total,
    "exit_code": exit_code,
    "started_at": run.started_at,
    "finished_at": run.finished_at,
  }


# ---------------------------------------------------------------- API: view -

WORKING_REF = "WORKING"  # sentinel meaning "files on disk, not a git ref"


def _repo_for_slug(slug: str) -> Path:
  conn = db.connect()
  try:
    row = conn.execute("SELECT repo_path FROM wikis WHERE slug = ?", (slug,)).fetchone()
    if not row:
      raise HTTPException(404, f"no wiki with slug {slug!r}")
    return Path(row["repo_path"])
  finally:
    conn.close()


def _run_git(repo: Path, *args: str) -> tuple[int, str]:
  result = subprocess.run(
    ["git", *args],
    cwd=str(repo),
    capture_output=True, text=True,
  )
  return result.returncode, result.stdout


def _resolve_relative(current_path: str, link_target: str) -> Optional[str]:
  """Given a markdown link target like `../characters/foo.md` from a page
  at `concepts/cruciform.md`, return the normalized wiki-relative path
  (`characters/foo.md`) — or None if the link goes outside the wiki tree."""
  if "://" in link_target or link_target.startswith(("http:", "https:", "mailto:", "#")):
    return None
  base_dir = os.path.dirname(current_path)
  combined = os.path.normpath(os.path.join(base_dir, link_target))
  if combined.startswith(".."):
    return None
  return combined.replace("\\", "/")


def _safe_page_path(repo: Path, path: str) -> Path:
  wiki_dir = (repo / "wiki").resolve()
  candidate = (wiki_dir / path).resolve()
  if not str(candidate).startswith(str(wiki_dir)):
    raise HTTPException(400, "path escapes wiki dir")
  return candidate


def _list_pages_at_ref(repo: Path, ref: str) -> List[str]:
  """Return wiki-relative page paths (e.g. 'characters/darrow.md',
  'index.md') present at the given ref. WORKING_REF reads the disk."""
  if ref == WORKING_REF:
    wiki_dir = repo / "wiki"
    if not wiki_dir.exists():
      return []
    paths: List[str] = []
    for special in ("index.md", "open-questions.md"):
      if (wiki_dir / special).exists():
        paths.append(special)
    for sub in PAGE_DIRS:
      sub_dir = wiki_dir / sub
      if sub_dir.exists():
        for md in sorted(sub_dir.glob("*.md")):
          paths.append(f"{sub}/{md.name}")
    return paths

  rc, out = _run_git(repo, "ls-tree", "-r", "--name-only", ref, "wiki/")
  if rc != 0:
    raise HTTPException(404, f"unknown ref: {ref}")
  paths = []
  for line in out.splitlines():
    if not line.startswith("wiki/") or not line.endswith(".md"):
      continue
    rel = line[len("wiki/"):]
    if "/" in rel:
      sub = rel.split("/", 1)[0]
      if sub not in PAGE_DIRS:
        continue
    elif rel not in ("index.md", "open-questions.md"):
      continue
    paths.append(rel)
  return sorted(paths)


def _read_page_at_ref(repo: Path, ref: str, path: str) -> Optional[str]:
  if ref == WORKING_REF:
    page_path = _safe_page_path(repo, path)
    if not page_path.exists() or not page_path.is_file():
      return None
    return page_path.read_text(encoding="utf-8")
  rc, out = _run_git(repo, "show", f"{ref}:wiki/{path}")
  if rc != 0:
    return None
  return out


def _render_markdown(text: str, page_path: str) -> str:
  md = markdown.Markdown(extensions=["extra", "sane_lists"])
  body_html = md.convert(text)

  def rewrite_link(match: re.Match) -> str:
    href = match.group(1)
    if not href.endswith(".md"):
      return match.group(0)
    resolved = _resolve_relative(page_path, href)
    if resolved is None:
      return match.group(0)
    return f'href="#" data-page="{resolved}"'

  return re.sub(r'href="([^"#]+\.md)"', rewrite_link, body_html)


@router.get("/api/wiki-builder/wiki/{slug}/checkpoints")
def list_checkpoints_endpoint(slug: str):
  repo = _repo_for_slug(slug)
  rc, out = _run_git(repo, "for-each-ref", "--format=%(refname:short)\t%(*objectname)\t%(objectname)\t%(subject)", "refs/tags/")
  if rc != 0:
    return {"slug": slug, "checkpoints": []}

  checkpoints = []
  for line in out.splitlines():
    parts = line.split("\t", 3)
    if len(parts) < 4:
      continue
    tag, _peeled, _obj, subject = parts
    # Only seg-NNNN style tags belong to the segment-based pipeline.
    m = re.match(r"^seg-(\d+)$", tag)
    if not m:
      continue
    checkpoints.append({
      "ref": tag,
      "story_order": int(m.group(1)),
      "subject": subject or tag,
    })
  checkpoints.sort(key=lambda c: c["story_order"])
  return {"slug": slug, "checkpoints": checkpoints}


@router.get("/api/wiki-builder/wiki/{slug}/tree")
def get_wiki_tree_endpoint(slug: str, ref: str = Query(WORKING_REF), compare_to: Optional[str] = Query(None)):
  repo = _repo_for_slug(slug)
  paths = _list_pages_at_ref(repo, ref)

  diff: dict[str, str] = {}
  if compare_to:
    other_paths = set(_list_pages_at_ref(repo, compare_to))
    cur_paths = set(paths)
    for p in cur_paths | other_paths:
      if p in cur_paths and p not in other_paths:
        diff[p] = "added"
      elif p in other_paths and p not in cur_paths:
        diff[p] = "deleted"
      else:
        a = _read_page_at_ref(repo, ref, p) or ""
        b = _read_page_at_ref(repo, compare_to, p) or ""
        diff[p] = "modified" if a != b else "unchanged"
    # Make sure deleted-only pages still appear in the tree so the user
    # can pick them.
    for p in other_paths - cur_paths:
      paths.append(p)
    paths = sorted(set(paths))

  sections: dict[str, list] = {sub: [] for sub in PAGE_DIRS}
  specials = []
  for path in paths:
    entry = {"path": path, "name": Path(path).stem, "diff": diff.get(path)}
    if "/" in path:
      sub = path.split("/", 1)[0]
      if sub in sections:
        sections[sub].append(entry)
    else:
      specials.append(entry)

  out_sections = [{"name": sub, "pages": pages} for sub, pages in sections.items() if pages]
  return {"slug": slug, "ref": ref, "compare_to": compare_to, "sections": out_sections, "specials": specials}


@router.get("/api/wiki-builder/wiki/{slug}/page", response_class=HTMLResponse)
def get_wiki_page_endpoint(slug: str, path: str = Query(...), ref: str = Query(WORKING_REF)):
  repo = _repo_for_slug(slug)
  text = _read_page_at_ref(repo, ref, path)
  if text is None:
    return HTMLResponse(
      f'<div class="empty">page <code>{path}</code> does not exist at ref <code>{ref}</code></div>',
      status_code=200,
    )
  return HTMLResponse(_render_markdown(text, path))
