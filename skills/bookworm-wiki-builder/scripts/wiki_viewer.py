#!/usr/bin/env python3
"""
Generate a static HTML viewer for a Bookworm wiki.

Usage:
    python wiki_viewer.py <wiki-dir> --output viewer.html [--tag <tag>]

If --tag is given and the wiki-dir is a git repo, reads wiki state at that tag.
Otherwise reads the current working tree.

Produces a self-contained HTML file with:
- Sidebar navigation organized by category
- Rendered markdown pages with working cross-links
- Open questions dashboard
- Log timeline (if log.md exists)
- Dark theme matching the Bookworm reader aesthetic
"""

import argparse
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def run_git(args: list[str], cwd: str) -> str:
    result = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def get_wiki_files(wiki_dir: str, tag: str | None = None) -> dict[str, str]:
    """Get all wiki .md files and their contents."""
    pages = {}

    if tag:
        # Read from git at tag
        file_list = run_git(["ls-tree", "-r", "--name-only", tag, "--", "wiki/"], wiki_dir)
        if not file_list:
            print(f"No wiki files found at tag '{tag}'", file=sys.stderr)
            return {}
        for filepath in file_list.split("\n"):
            if filepath.endswith(".md"):
                content = run_git(["show", f"{tag}:{filepath}"], wiki_dir)
                pages[filepath] = content
    else:
        # Read from working tree
        wiki_path = os.path.join(wiki_dir, "wiki")
        if not os.path.isdir(wiki_path):
            print(f"No wiki/ directory found in {wiki_dir}", file=sys.stderr)
            return {}
        for root, dirs, files in os.walk(wiki_path):
            for f in files:
                if f.endswith(".md"):
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, wiki_dir)
                    with open(full, encoding="utf-8") as fh:
                        pages[rel] = fh.read()

    return pages


def strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from markdown."""
    if content.startswith("---"):
        end = content.find("\n---\n", 3)
        if end != -1:
            return content[end + 5:].strip()
    return content.strip()


def extract_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter as dict."""
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


def md_to_html_simple(md: str, page_id: str) -> str:
    """Convert markdown to HTML with basic formatting.

    This is intentionally simple — no external dependencies.
    Handles: headings, bold, italic, links, lists, horizontal rules, code blocks, paragraphs.
    """
    lines = md.split("\n")
    html_parts = []
    in_list = False
    in_code = False
    in_paragraph = False
    code_block = []

    def close_paragraph():
        nonlocal in_paragraph
        if in_paragraph:
            html_parts.append("</p>")
            in_paragraph = False

    def close_list():
        nonlocal in_list
        if in_list:
            html_parts.append("</ul>")
            in_list = False

    def process_inline(text: str) -> str:
        # Code spans
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        # Bold
        text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
        # Italic
        text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
        # Wiki-style cross-links: [Text](../category/slug.md) → internal nav
        def replace_link(m):
            link_text = m.group(1)
            href = m.group(2)
            if href.endswith(".md"):
                # Convert to page ID for internal navigation
                # ../characters/darrow.md → wiki/characters/darrow.md
                # Resolve relative to current page
                parts = page_id.split("/")
                base = "/".join(parts[:-1])
                resolved = os.path.normpath(os.path.join(base, href))
                return f'<a href="#" onclick="navigateTo(\'{resolved}\')" class="wiki-link">{link_text}</a>'
            return f'<a href="{href}" target="_blank">{link_text}</a>'
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace_link, text)
        return text

    for line in lines:
        stripped = line.strip()

        # Code blocks
        if stripped.startswith("```"):
            if in_code:
                html_parts.append("<pre><code>" + html.escape("\n".join(code_block)) + "</code></pre>")
                code_block = []
                in_code = False
            else:
                close_paragraph()
                close_list()
                in_code = True
            continue
        if in_code:
            code_block.append(line)
            continue

        # Empty line
        if not stripped:
            close_paragraph()
            close_list()
            continue

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if heading_match:
            close_paragraph()
            close_list()
            level = len(heading_match.group(1))
            text = process_inline(html.escape(heading_match.group(2)))
            html_parts.append(f"<h{level}>{text}</h{level}>")
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}$", stripped):
            close_paragraph()
            close_list()
            html_parts.append("<hr>")
            continue

        # List items
        list_match = re.match(r"^[-*]\s+(.*)", stripped)
        if list_match:
            close_paragraph()
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            text = process_inline(html.escape(list_match.group(1)))
            html_parts.append(f"<li>{text}</li>")
            continue

        # Regular text
        close_list()
        text = process_inline(html.escape(stripped))
        if not in_paragraph:
            html_parts.append("<p>")
            in_paragraph = True
        else:
            html_parts.append(" ")
        html_parts.append(text)

    close_paragraph()
    close_list()

    return "\n".join(html_parts)


def categorize_pages(pages: dict[str, str]) -> dict:
    """Organize pages by category for the sidebar."""
    categories = {
        "index": [],
        "characters": [],
        "concepts": [],
        "places": [],
        "factions": [],
        "events": [],
        "meta": [],  # open-questions, log
    }

    for path, content in sorted(pages.items()):
        parts = path.replace("\\", "/").split("/")
        if len(parts) < 2:
            continue

        fm = extract_frontmatter(content)
        body = strip_frontmatter(content)
        # Extract first heading as title
        title_match = re.match(r"^#\s+(.*)", body)
        title = title_match.group(1) if title_match else parts[-1].replace(".md", "").replace("-", " ").title()

        page_info = {
            "path": path,
            "title": title,
            "frontmatter": fm,
            "body": body,
        }

        if parts[-1] == "index.md":
            categories["index"].append(page_info)
        elif parts[-1] in ("open-questions.md", "log.md"):
            categories["meta"].append(page_info)
        elif len(parts) >= 3:
            cat = parts[1]
            if cat in categories:
                categories[cat].append(page_info)
            else:
                categories["meta"].append(page_info)
        else:
            categories["meta"].append(page_info)

    return categories


def generate_html(pages: dict[str, str], book_title: str = "Bookworm Wiki",
                  tag: str | None = None) -> str:
    """Generate the full self-contained HTML viewer."""
    categories = categorize_pages(pages)

    # Build page data for JS
    page_data = {}
    for cat_pages in categories.values():
        for p in cat_pages:
            page_html = md_to_html_simple(p["body"], p["path"])
            page_data[p["path"]] = {
                "title": p["title"],
                "html": page_html,
                "category": next(
                    (k for k, v in categories.items() if p in v), "meta"
                ),
            }

    page_data_json = json.dumps(page_data, ensure_ascii=False)

    # Build sidebar HTML
    sidebar_sections = []

    category_labels = {
        "index": "Overview",
        "characters": "Characters",
        "concepts": "Concepts",
        "places": "Places",
        "factions": "Factions & Groups",
        "events": "Events",
        "meta": "Meta",
    }

    category_icons = {
        "index": "&#x1f4d6;",
        "characters": "&#x1f464;",
        "concepts": "&#x1f4a1;",
        "places": "&#x1f30d;",
        "factions": "&#x2694;",
        "events": "&#x26a1;",
        "meta": "&#x2753;",
    }

    for cat_key, label in category_labels.items():
        cat_pages = categories.get(cat_key, [])
        if not cat_pages:
            continue
        icon = category_icons.get(cat_key, "")
        items = []
        for p in sorted(cat_pages, key=lambda x: x["title"]):
            items.append(
                f'<a class="nav-item" href="#" onclick="navigateTo(\'{p["path"]}\')">'
                f'{html.escape(p["title"])}</a>'
            )
        sidebar_sections.append(
            f'<div class="nav-section">'
            f'<div class="nav-header">{icon} {label}</div>'
            f'{"".join(items)}'
            f'</div>'
        )

    sidebar_html = "\n".join(sidebar_sections)

    # Default page
    default_page = "wiki/index.md" if "wiki/index.md" in page_data else next(iter(page_data), "")

    tag_display = f" — as of {html.escape(tag)}" if tag else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(book_title)} Wiki</title>
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
  --border: #3d3835;
  --link: #d4a048;
  --link-hover: #e8b860;
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
  width: 280px;
  min-width: 280px;
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
}}

.sidebar-header .tag-label {{
  font-size: 0.8rem;
  color: var(--text-muted);
  margin-top: 4px;
}}

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
  display: block;
  padding: 6px 20px 6px 28px;
  color: var(--text-secondary);
  text-decoration: none;
  font-size: 0.9rem;
  transition: all 0.15s;
  border-left: 3px solid transparent;
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

/* Main content */
.main {{
  flex: 1;
  overflow-y: auto;
  padding: 40px 60px;
  max-width: 800px;
}}

.main h1 {{
  font-size: 1.8rem;
  color: var(--accent);
  margin-bottom: 24px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 12px;
}}

.main h2 {{
  font-size: 1.3rem;
  color: var(--text-primary);
  margin-top: 32px;
  margin-bottom: 12px;
}}

.main h3 {{
  font-size: 1.1rem;
  color: var(--text-secondary);
  margin-top: 24px;
  margin-bottom: 8px;
}}

.main p {{
  line-height: 1.7;
  margin-bottom: 12px;
  color: var(--text-primary);
}}

.main ul {{
  margin: 8px 0 16px 24px;
  line-height: 1.7;
}}

.main li {{
  margin-bottom: 6px;
}}

.main hr {{
  border: none;
  border-top: 1px solid var(--border);
  margin: 24px 0;
}}

.main code {{
  background: var(--bg-elevated);
  padding: 2px 6px;
  border-radius: 3px;
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 0.85em;
}}

.main pre {{
  background: var(--bg-elevated);
  padding: 16px;
  border-radius: 6px;
  overflow-x: auto;
  margin: 12px 0;
}}

.main pre code {{
  padding: 0;
  background: none;
}}

.main strong {{
  color: var(--text-primary);
  font-weight: 700;
}}

.main em {{
  color: var(--text-secondary);
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

/* Blockquote */
.main blockquote {{
  border-left: 3px solid var(--accent-dim);
  padding-left: 16px;
  margin: 16px 0;
  color: var(--text-secondary);
  font-style: italic;
}}

/* Scrollbar */
::-webkit-scrollbar {{
  width: 8px;
}}
::-webkit-scrollbar-track {{
  background: var(--bg-deep);
}}
::-webkit-scrollbar-thumb {{
  background: var(--border);
  border-radius: 4px;
}}
::-webkit-scrollbar-thumb:hover {{
  background: var(--text-muted);
}}

/* Responsive */
@media (max-width: 768px) {{
  .sidebar {{ width: 220px; min-width: 220px; }}
  .main {{ padding: 24px; }}
}}
</style>
</head>
<body>

<div class="sidebar">
  <div class="sidebar-header">
    <h1>{html.escape(book_title)}</h1>
    <div class="tag-label">Spoiler-Safe Wiki{tag_display}</div>
  </div>
  <div class="sidebar-nav">
    {sidebar_html}
  </div>
</div>

<div class="main" id="content">
  <p style="color: var(--text-muted);">Select a page from the sidebar.</p>
</div>

<script>
const pages = {page_data_json};

function navigateTo(pageId) {{
  const page = pages[pageId];
  if (!page) {{
    document.getElementById('content').innerHTML =
      '<p style="color: var(--text-muted);">Page not found: ' + pageId + '</p>';
    return;
  }}

  document.getElementById('content').innerHTML = page.html;

  // Update active state in sidebar
  document.querySelectorAll('.nav-item').forEach(el => {{
    el.classList.remove('active');
    if (el.getAttribute('onclick')?.includes(pageId)) {{
      el.classList.add('active');
    }}
  }});

  // Scroll to top
  document.getElementById('content').scrollTop = 0;

  return false;
}}

// Load default page
navigateTo('{default_page}');
</script>

</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Generate static HTML viewer for Bookworm wiki")
    parser.add_argument("wiki_dir", help="Path to the wiki repository")
    parser.add_argument("--output", "-o", required=True, help="Output HTML file path")
    parser.add_argument("--tag", "-t", help="Git tag to read wiki state from (optional)")
    parser.add_argument("--title", default=None, help="Book title (auto-detected from metadata.json if not given)")
    args = parser.parse_args()

    wiki_dir = os.path.abspath(args.wiki_dir)

    # Try to auto-detect title
    title = args.title
    if not title:
        meta_path = os.path.join(wiki_dir, "raw", "metadata.json")
        if os.path.isfile(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
                title = meta.get("title", "Bookworm Wiki")
        else:
            title = "Bookworm Wiki"

    pages = get_wiki_files(wiki_dir, args.tag)
    if not pages:
        print("No wiki pages found.", file=sys.stderr)
        sys.exit(1)

    html_output = generate_html(pages, book_title=title, tag=args.tag)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html_output)

    print(f"Generated viewer: {args.output} ({len(pages)} pages)")


if __name__ == "__main__":
    main()
