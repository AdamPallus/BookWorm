#!/usr/bin/env python3
"""
Generate a static HTML viewer with chapter-state toggle for a Bookworm wiki.

Usage:
    python wiki_viewer_multi.py <wiki-repo-dir> --output viewer.html

Unlike wiki_viewer.py (single tag), this embeds ALL chapter snapshots
into one HTML file with a chapter selector dropdown. The user can toggle
between chapter states and see how the wiki grows over time.

The viewer also shows a diff summary when switching chapters — highlighting
which pages are new or updated.
"""

import argparse
import html as html_mod
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def run_git(args: list[str], cwd: str) -> str:
    result = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def get_chapter_tags(cwd: str) -> list[str]:
    raw = run_git(["tag", "-l"], cwd)
    if not raw:
        return []
    tags = [t for t in raw.split("\n") if re.match(r"(b\d+-)?ch-\d+", t)]

    def sort_key(tag):
        parts = re.match(r"(?:b(\d+)-)?ch-(\d+)", tag)
        if parts:
            return (int(parts.group(1) or 0), int(parts.group(2)))
        return (0, 0)

    return sorted(tags, key=sort_key)


def get_wiki_files_at_tag(tag: str, cwd: str) -> dict[str, str]:
    file_list = run_git(["ls-tree", "-r", "--name-only", tag, "--", "wiki/"], cwd)
    if not file_list:
        return {}
    pages = {}
    for filepath in file_list.split("\n"):
        if filepath.endswith(".md"):
            content = run_git(["show", f"{tag}:{filepath}"], cwd)
            pages[filepath] = content
    return pages


def get_commit_message(tag: str, cwd: str) -> str:
    return run_git(["log", "-1", "--format=%s", tag], cwd)


def strip_frontmatter(content: str) -> str:
    if content.startswith("---"):
        end = content.find("\n---\n", 3)
        if end != -1:
            return content[end + 5:].strip()
    return content.strip()


def extract_frontmatter(content: str) -> dict:
    if not content.startswith("---"):
        return {}
    end = content.find("\n---\n", 3)
    if end == -1:
        return {}
    fm = {}
    for line in content[4:end].split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.strip().strip("'\"")
    return fm


def md_to_html(md: str, page_id: str) -> str:
    """Convert markdown to HTML with cross-link support."""
    lines = md.split("\n")
    html_parts = []
    in_list = False
    in_code = False
    in_paragraph = False
    code_block = []

    def close_p():
        nonlocal in_paragraph
        if in_paragraph:
            html_parts.append("</p>")
            in_paragraph = False

    def close_ul():
        nonlocal in_list
        if in_list:
            html_parts.append("</ul>")
            in_list = False

    def inline(text: str) -> str:
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)

        def link_repl(m):
            lt, href = m.group(1), m.group(2)
            if href.endswith(".md"):
                parts = page_id.split("/")
                base = "/".join(parts[:-1])
                resolved = os.path.normpath(os.path.join(base, href))
                return f'<a href="#" onclick="navigateTo(\'{resolved}\')" class="wiki-link">{lt}</a>'
            return f'<a href="{href}" target="_blank">{lt}</a>'

        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link_repl, text)
        return text

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code:
                html_parts.append("<pre><code>" + html_mod.escape("\n".join(code_block)) + "</code></pre>")
                code_block = []
                in_code = False
            else:
                close_p()
                close_ul()
                in_code = True
            continue
        if in_code:
            code_block.append(line)
            continue

        if not stripped:
            close_p()
            close_ul()
            continue

        hm = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if hm:
            close_p()
            close_ul()
            lvl = len(hm.group(1))
            html_parts.append(f"<h{lvl}>{inline(html_mod.escape(hm.group(2)))}</h{lvl}>")
            continue

        if re.match(r"^[-*_]{3,}$", stripped):
            close_p()
            close_ul()
            html_parts.append("<hr>")
            continue

        # Blockquotes
        bq = re.match(r"^>\s*(.*)", stripped)
        if bq:
            close_p()
            close_ul()
            html_parts.append(f"<blockquote><p>{inline(html_mod.escape(bq.group(1)))}</p></blockquote>")
            continue

        lm = re.match(r"^[-*]\s+(.*)", stripped)
        if lm:
            close_p()
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{inline(html_mod.escape(lm.group(1)))}</li>")
            continue

        close_ul()
        t = inline(html_mod.escape(stripped))
        if not in_paragraph:
            html_parts.append("<p>")
            in_paragraph = True
        else:
            html_parts.append(" ")
        html_parts.append(t)

    close_p()
    close_ul()
    return "\n".join(html_parts)


def build_snapshot_data(pages: dict[str, str]) -> dict:
    """Build structured data for a single snapshot."""
    result = {}
    for path, content in sorted(pages.items()):
        parts = path.replace("\\", "/").split("/")
        fm = extract_frontmatter(content)
        body = strip_frontmatter(content)
        title_match = re.match(r"^#\s+(.*)", body)
        title = title_match.group(1) if title_match else parts[-1].replace(".md", "").replace("-", " ").title()

        # Categorize
        cat = "meta"
        if parts[-1] == "index.md":
            cat = "index"
        elif parts[-1] in ("open-questions.md", "log.md"):
            cat = "meta"
        elif len(parts) >= 3 and parts[1] in ("characters", "concepts", "places", "factions", "events"):
            cat = parts[1]

        page_html = md_to_html(body, path)
        result[path] = {
            "title": title,
            "html": page_html,
            "category": cat,
        }
    return result


def generate_multi_html(repo_dir: str, book_title: str = "Bookworm Wiki") -> str:
    """Generate HTML with all chapter snapshots embedded."""
    tags = get_chapter_tags(repo_dir)
    if not tags:
        print("No chapter tags found.", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(tags)} chapter tags for '{book_title}'...")

    all_snapshots = {}
    tag_metadata = []

    for i, tag in enumerate(tags):
        pages = get_wiki_files_at_tag(tag, repo_dir)
        snapshot = build_snapshot_data(pages)
        all_snapshots[tag] = snapshot

        commit_msg = get_commit_message(tag, repo_dir)
        page_count = len(snapshot)
        char_count = sum(1 for p in snapshot.values() if p["category"] == "characters")
        concept_count = sum(1 for p in snapshot.values() if p["category"] == "concepts")

        tag_metadata.append({
            "tag": tag,
            "label": f"{tag}: {commit_msg}" if commit_msg else tag,
            "pages": page_count,
            "chars": char_count,
            "concepts": concept_count,
        })

        print(f"  [{i+1}/{len(tags)}] {tag}: {page_count} pages")

    snapshots_json = json.dumps(all_snapshots, ensure_ascii=False)
    tags_json = json.dumps(tag_metadata, ensure_ascii=False)
    default_tag = tags[-1]

    category_config = json.dumps({
        "index":      {"label": "Overview",          "icon": "\U0001f4d6"},
        "characters": {"label": "Characters",        "icon": "\U0001f464"},
        "concepts":   {"label": "Concepts",          "icon": "\U0001f4a1"},
        "places":     {"label": "Places",            "icon": "\U0001f30d"},
        "factions":   {"label": "Factions & Groups",  "icon": "\u2694\ufe0f"},
        "events":     {"label": "Events",            "icon": "\u26a1"},
        "meta":       {"label": "Meta",              "icon": "\u2753"},
    }, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html_mod.escape(book_title)} — Spoiler-Safe Wiki</title>
<style>
:root {{
  --bg-deep: #1a1a1a;
  --bg-surface: #2a2725;
  --bg-elevated: #353230;
  --text-primary: #e8e0d4;
  --text-secondary: #a89e94;
  --text-muted: #78706a;
  --accent: #d4a048;
  --accent-dim: #a67c32;
  --accent-glow: rgba(212, 160, 72, 0.15);
  --border: #3d3835;
  --link: #d4a048;
  --link-hover: #e8b860;
  --new-badge: #5a9a6a;
  --updated-badge: #6a8ab0;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  font-family: 'Georgia', 'Times New Roman', serif;
  background: var(--bg-deep);
  color: var(--text-primary);
  display: flex;
  height: 100vh;
  overflow: hidden;
}}

/* Sidebar */
.sidebar {{
  width: 300px;
  min-width: 300px;
  background: var(--bg-surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}}

.sidebar-header {{
  padding: 20px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}}

.sidebar-header h1 {{
  font-size: 1.1rem;
  color: var(--accent);
  font-weight: 600;
  line-height: 1.3;
  margin-bottom: 4px;
}}

.sidebar-header .subtitle {{
  font-size: 0.8rem;
  color: var(--text-muted);
  margin-bottom: 12px;
}}

/* Chapter selector */
.chapter-selector {{
  width: 100%;
  padding: 8px 12px;
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text-primary);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 0.85rem;
  cursor: pointer;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23a89e94' d='M6 8L1 3h10z'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 10px center;
}}

.chapter-selector:focus {{
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-glow);
}}

.chapter-selector option {{
  background: var(--bg-surface);
  color: var(--text-primary);
}}

.snapshot-stats {{
  margin-top: 8px;
  font-size: 0.75rem;
  color: var(--text-muted);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}

/* Sidebar nav */
.sidebar-nav {{
  flex: 1;
  overflow-y: auto;
  padding: 12px 0;
}}

.nav-section {{
  margin-bottom: 8px;
}}

.nav-header {{
  padding: 6px 20px;
  font-size: 0.75rem;
  font-weight: 700;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
}}

.nav-item {{
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 20px 6px 28px;
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 0.9rem;
  transition: all 0.15s;
  border-left: 3px solid transparent;
  cursor: pointer;
}}

.nav-item:hover {{
  color: var(--text-primary);
  background: var(--bg-elevated);
}}

.nav-item.active {{
  color: var(--accent);
  border-left-color: var(--accent);
  background: var(--bg-elevated);
}}

.badge {{
  font-size: 0.65rem;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 3px;
  text-transform: uppercase;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  letter-spacing: 0.03em;
}}

.badge-new {{
  background: var(--new-badge);
  color: white;
}}

.badge-updated {{
  background: var(--updated-badge);
  color: white;
}}

/* Main content */
.main {{
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}}

.content-area {{
  flex: 1;
  overflow-y: auto;
  padding: 40px 60px;
  max-width: 860px;
}}

.content-area h1 {{
  font-size: 1.8rem;
  color: var(--accent);
  margin-bottom: 24px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 12px;
}}

.content-area h2 {{
  font-size: 1.3rem;
  color: var(--text-primary);
  margin-top: 32px;
  margin-bottom: 12px;
}}

.content-area h3 {{
  font-size: 1.1rem;
  color: var(--text-secondary);
  margin-top: 24px;
  margin-bottom: 8px;
}}

.content-area p {{
  line-height: 1.7;
  margin-bottom: 12px;
}}

.content-area ul {{
  margin: 8px 0 16px 24px;
  line-height: 1.7;
}}

.content-area li {{
  margin-bottom: 6px;
}}

.content-area hr {{
  border: none;
  border-top: 1px solid var(--border);
  margin: 24px 0;
}}

.content-area code {{
  background: var(--bg-elevated);
  padding: 2px 6px;
  border-radius: 3px;
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 0.85em;
}}

.content-area pre {{
  background: var(--bg-elevated);
  padding: 16px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 12px 0;
}}

.content-area pre code {{
  padding: 0;
  background: none;
}}

.content-area blockquote {{
  border-left: 3px solid var(--accent-dim);
  padding-left: 16px;
  margin: 16px 0;
  color: var(--text-secondary);
  font-style: italic;
}}

.wiki-link {{
  color: var(--link);
  text-decoration: none;
  border-bottom: 1px dotted var(--accent-dim);
  transition: all 0.15s;
}}

.wiki-link:hover {{
  color: var(--link-hover);
  border-bottom-style: solid;
}}

/* Welcome state */
.welcome {{
  padding: 60px;
  text-align: center;
  color: var(--text-muted);
}}

.welcome h2 {{
  color: var(--accent);
  margin-bottom: 12px;
}}

/* Scrollbar */
::-webkit-scrollbar {{ width: 8px; }}
::-webkit-scrollbar-track {{ background: var(--bg-deep); }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: var(--text-muted); }}

@media (max-width: 768px) {{
  .sidebar {{ width: 240px; min-width: 240px; }}
  .content-area {{ padding: 24px; }}
}}
</style>
</head>
<body>

<div class="sidebar">
  <div class="sidebar-header">
    <h1>{html_mod.escape(book_title)}</h1>
    <div class="subtitle">Spoiler-Safe Wiki</div>
    <select class="chapter-selector" id="chapterSelect" onchange="switchChapter(this.value)">
    </select>
    <div class="snapshot-stats" id="snapshotStats"></div>
  </div>
  <div class="sidebar-nav" id="sidebarNav">
  </div>
</div>

<div class="main">
  <div class="content-area" id="content">
    <div class="welcome">
      <h2>Select a chapter checkpoint</h2>
      <p>Use the dropdown above to choose how far you've read.</p>
    </div>
  </div>
</div>

<script>
const allSnapshots = {snapshots_json};
const tagMetadata = {tags_json};
const categoryConfig = {category_config};
const urlParams = new URLSearchParams(window.location.search);
const requestedTag = urlParams.get('tag');
const requestedPage = urlParams.get('page');

let currentTag = null;
let currentPage = null;
let previousSnapshot = null;
let pendingPage = requestedPage || 'wiki/index.md';

// Populate chapter selector
const select = document.getElementById('chapterSelect');
tagMetadata.forEach((tm, i) => {{
  const opt = document.createElement('option');
  opt.value = tm.tag;
  opt.textContent = tm.label;
  select.appendChild(opt);
}});

function switchChapter(tag) {{
  previousSnapshot = currentTag ? allSnapshots[currentTag] : null;
  currentTag = tag;
  const snapshot = allSnapshots[tag];
  if (!snapshot) return;
  select.value = tag;

  // Update stats
  const tm = tagMetadata.find(t => t.tag === tag);
  document.getElementById('snapshotStats').textContent =
    `${{Object.keys(snapshot).length}} pages | ${{tm?.chars || 0}} characters | ${{tm?.concepts || 0}} concepts`;

  // Build sidebar
  buildSidebar(snapshot);

  // Navigate to index or current page
  const nextPage = pendingPage && snapshot[pendingPage]
    ? pendingPage
    : (currentPage && snapshot[currentPage] ? currentPage : 'wiki/index.md');
  pendingPage = null;
  navigateTo(nextPage);
}}

function buildSidebar(snapshot) {{
  const nav = document.getElementById('sidebarNav');
  nav.innerHTML = '';

  // Group by category
  const grouped = {{}};
  for (const [path, page] of Object.entries(snapshot)) {{
    const cat = page.category;
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push({{ path, ...page }});
  }}

  // Render in order
  const order = ['index', 'characters', 'concepts', 'places', 'factions', 'events', 'meta'];
  for (const cat of order) {{
    const pages = grouped[cat];
    if (!pages || pages.length === 0) continue;

    const cfg = categoryConfig[cat] || {{ label: cat, icon: '' }};
    const section = document.createElement('div');
    section.className = 'nav-section';

    const header = document.createElement('div');
    header.className = 'nav-header';
    header.textContent = `${{cfg.icon}} ${{cfg.label}}`;
    section.appendChild(header);

    pages.sort((a, b) => a.title.localeCompare(b.title));
    for (const p of pages) {{
      const item = document.createElement('a');
      item.className = 'nav-item';
      item.href = '#';
      item.dataset.pagePath = p.path;
      item.onclick = (event) => {{
        event.preventDefault();
        navigateTo(p.path);
      }};

      let label = p.title;
      item.innerHTML = label;

      // Add new/updated badge
      if (previousSnapshot) {{
        if (!previousSnapshot[p.path]) {{
          item.innerHTML += ' <span class="badge badge-new">new</span>';
        }} else if (previousSnapshot[p.path].html !== p.html) {{
          item.innerHTML += ' <span class="badge badge-updated">updated</span>';
        }}
      }}

      section.appendChild(item);
    }}

    nav.appendChild(section);
  }}
}}

function navigateTo(pageId) {{
  if (!currentTag) return;
  const snapshot = allSnapshots[currentTag];
  const page = snapshot[pageId];

  if (!page) {{
    document.getElementById('content').innerHTML =
      `<p style="color: var(--text-muted);">Page not found: ${{pageId}}</p>`;
    return;
  }}

  currentPage = pageId;
  document.getElementById('content').innerHTML = page.html;
  document.getElementById('content').scrollTop = 0;
  const nextUrl = new URL(window.location.href);
  nextUrl.searchParams.set('tag', currentTag);
  nextUrl.searchParams.set('page', currentPage);
  history.replaceState(null, '', nextUrl.toString());

  // Update active state
  document.querySelectorAll('.nav-item').forEach(el => {{
    el.classList.remove('active');
  }});
  document.querySelectorAll('.nav-item').forEach(el => {{
    if (el.dataset && el.dataset.pagePath === pageId) {{
      el.classList.add('active');
    }}
  }});

  return false;
}}

// Auto-select the requested tag or the last tag
const initialTag = requestedTag && allSnapshots[requestedTag] ? requestedTag : '{default_tag}';
select.value = initialTag;
switchChapter(initialTag);
</script>

</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Generate multi-snapshot HTML wiki viewer")
    parser.add_argument("repo_dir", help="Path to the wiki git repository")
    parser.add_argument("--output", "-o", required=True, help="Output HTML file path")
    parser.add_argument("--title", default=None, help="Book title")
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)

    title = args.title
    if not title:
        meta_path = os.path.join(repo_dir, "raw", "metadata.json")
        if os.path.isfile(meta_path):
            with open(meta_path) as f:
                title = json.load(f).get("title", "Bookworm Wiki")
        else:
            title = "Bookworm Wiki"

    html_output = generate_multi_html(repo_dir, book_title=title)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html_output)

    print(f"Generated multi-snapshot viewer: {args.output}")


if __name__ == "__main__":
    main()
