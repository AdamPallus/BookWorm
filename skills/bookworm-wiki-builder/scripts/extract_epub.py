#!/usr/bin/env python3
"""
Extract chapters from an EPUB file into individual markdown files.

Usage:
    python extract_epub.py <epub_path> <output_dir> [--min-chars 500]

Produces:
    <output_dir>/
        metadata.json          # Book title, author, chapter count
        chapters/
            0000-front-matter.md
            0001-chapter-01-helldiver.md
            0002-chapter-02-the-township.md
            ...

Each chapter file has YAML frontmatter with index, title, spine_href,
and character count. The body is the raw chapter text.

This script handles common EPUB quirks:
- Filters out very short sections (< min_chars) as front matter noise
- Groups tiny adjacent sections (part dividers, epigraphs) into the next chapter
- Detects chapter titles from headings
- Preserves original spine order
"""

import argparse
import json
import os
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path


class _TextExtractor(HTMLParser):
    """Simple HTML-to-text extractor that preserves paragraph breaks."""

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = False
        self._block_tags = {
            "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
            "li", "blockquote", "tr", "section", "article",
        }

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        if tag in self._block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        if tag in self._block_tags:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self.parts)
        # Normalize whitespace: collapse runs of 3+ newlines to 2
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        # Strip leading/trailing whitespace per line, then overall
        lines = [line.strip() for line in raw.split("\n")]
        return "\n".join(lines).strip()


def _extract_title_from_html(html: str) -> str | None:
    """Pull the first heading (h1-h3) text from raw HTML."""
    match = re.search(r"<h[123][^>]*>(.*?)</h[123]>", html, re.DOTALL | re.IGNORECASE)
    if match:
        # Strip inner HTML tags
        inner = re.sub(r"<[^>]+>", "", match.group(1))
        title = inner.strip()
        if title:
            return title
    return None


def _slugify(text: str, max_len: int = 50) -> str:
    """Turn a title into a filename-safe slug."""
    s = text.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s-]+", "-", s).strip("-")
    return s[:max_len]


def extract_epub(epub_path: str, output_dir: str, min_chars: int = 500) -> dict:
    """
    Extract chapters from an EPUB into markdown files.

    Returns metadata dict with title, author, chapters list.
    """
    epub_path = os.path.abspath(epub_path)
    output_dir = os.path.abspath(output_dir)

    if not os.path.isfile(epub_path):
        print(f"Error: EPUB not found: {epub_path}", file=sys.stderr)
        sys.exit(1)

    chapters_dir = os.path.join(output_dir, "chapters")
    os.makedirs(chapters_dir, exist_ok=True)

    with zipfile.ZipFile(epub_path) as zf:
        # Parse container.xml to find OPF
        container = zf.read("META-INF/container.xml").decode("utf-8")
        opf_match = re.search(r'full-path="([^"]+)"', container)
        if not opf_match:
            print("Error: Could not find OPF path in container.xml", file=sys.stderr)
            sys.exit(1)

        opf_path = opf_match.group(1)
        opf = zf.read(opf_path).decode("utf-8")
        opf_dir = "/".join(opf_path.split("/")[:-1])
        if opf_dir:
            opf_dir += "/"

        # Extract metadata
        title_match = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.DOTALL)
        author_match = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", opf, re.DOTALL)
        book_title = title_match.group(1).strip() if title_match else "Untitled"
        book_author = author_match.group(1).strip() if author_match else "Unknown"

        # Build manifest: id -> (href, media-type)
        manifest: dict[str, tuple[str, str]] = {}
        for m in re.finditer(
            r'<item\s+[^>]*id="([^"]+)"[^>]*href="([^"]+)"[^>]*media-type="([^"]+)"',
            opf,
        ):
            manifest[m.group(1)] = (m.group(2), m.group(3))
        # Also match reversed attribute order (href before id)
        for m in re.finditer(
            r'<item\s+[^>]*href="([^"]+)"[^>]*id="([^"]+)"[^>]*media-type="([^"]+)"',
            opf,
        ):
            if m.group(2) not in manifest:
                manifest[m.group(2)] = (m.group(1), m.group(3))

        # Get spine order
        spine_ids = re.findall(r'<itemref\s+idref="([^"]+)"', opf)

        # Extract all sections in spine order
        raw_sections: list[dict] = []
        for sid in spine_ids:
            if sid not in manifest:
                continue
            href, mtype = manifest[sid]
            if "html" not in mtype and "xhtml" not in mtype:
                continue

            full_path = opf_dir + href
            try:
                html = zf.read(full_path).decode("utf-8")
            except (KeyError, UnicodeDecodeError):
                continue

            extractor = _TextExtractor()
            extractor.feed(html)
            text = extractor.get_text()

            if not text or len(text) < 20:
                continue

            title = _extract_title_from_html(html)
            raw_sections.append({
                "spine_id": sid,
                "spine_href": href,
                "title": title,
                "text": text,
                "char_count": len(text),
            })

    # Now merge small sections into the next substantial chapter.
    # This handles: part dividers, epigraphs, short prologues, etc.
    # Strategy: walk forward, accumulate small sections, attach them
    # as preamble to the next large section.
    chapters: list[dict] = []
    pending_preamble: list[dict] = []

    for section in raw_sections:
        if section["char_count"] < min_chars:
            pending_preamble.append(section)
        else:
            # This is a real chapter. Attach any pending preamble.
            preamble_text = ""
            preamble_titles = []
            for p in pending_preamble:
                if p["title"]:
                    preamble_titles.append(p["title"])
                if p["text"]:
                    preamble_text += p["text"] + "\n\n---\n\n"

            chapter_title = section["title"]
            # If no chapter title but preamble had one (like "Part I"),
            # prepend it
            if preamble_titles and chapter_title:
                # Check if preamble is a Part divider
                for pt in preamble_titles:
                    if re.match(r"^Part\s+", pt, re.IGNORECASE):
                        chapter_title = f"{pt} — {chapter_title}"
                        break

            full_text = preamble_text + section["text"] if preamble_text else section["text"]

            chapters.append({
                "index": len(chapters),
                "title": chapter_title or f"Chapter {len(chapters) + 1}",
                "spine_href": section["spine_href"],
                "text": full_text,
                "char_count": len(full_text),
            })
            pending_preamble = []

    # If there's trailing preamble with no following chapter, add it
    if pending_preamble:
        combined_text = "\n\n---\n\n".join(p["text"] for p in pending_preamble if p["text"])
        combined_title = next((p["title"] for p in pending_preamble if p["title"]), "Epilogue")
        if combined_text and len(combined_text) >= 100:
            chapters.append({
                "index": len(chapters),
                "title": combined_title,
                "spine_href": pending_preamble[0]["spine_href"],
                "text": combined_text,
                "char_count": len(combined_text),
            })

    # Write chapter files
    chapter_manifest = []
    for ch in chapters:
        slug = _slugify(ch["title"])
        filename = f"{ch['index']:04d}-{slug}.md"
        filepath = os.path.join(chapters_dir, filename)

        frontmatter = (
            f"---\n"
            f"chapter_index: {ch['index']}\n"
            f"title: \"{ch['title']}\"\n"
            f"spine_href: \"{ch['spine_href']}\"\n"
            f"char_count: {ch['char_count']}\n"
            f"---\n\n"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(frontmatter)
            f.write(ch["text"])

        chapter_manifest.append({
            "index": ch["index"],
            "title": ch["title"],
            "filename": filename,
            "char_count": ch["char_count"],
        })
        print(f"  [{ch['index']:3d}] {ch['title'][:60]:60s} ({ch['char_count']:,} chars)")

    # Write metadata
    metadata = {
        "title": book_title,
        "author": book_author,
        "source_epub": os.path.basename(epub_path),
        "total_chapters": len(chapters),
        "total_chars": sum(ch["char_count"] for ch in chapters),
        "chapters": chapter_manifest,
    }

    meta_path = os.path.join(output_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    print(f"\nExtracted {len(chapters)} chapters ({metadata['total_chars']:,} total chars)")
    print(f"Metadata: {meta_path}")
    return metadata


def main():
    parser = argparse.ArgumentParser(description="Extract EPUB chapters to markdown")
    parser.add_argument("epub_path", help="Path to the EPUB file")
    parser.add_argument("output_dir", help="Output directory for extracted chapters")
    parser.add_argument("--min-chars", type=int, default=500,
                        help="Minimum char count for a section to be its own chapter (default: 500)")
    args = parser.parse_args()

    extract_epub(args.epub_path, args.output_dir, args.min_chars)


if __name__ == "__main__":
    main()
