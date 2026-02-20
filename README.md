# Bookworm

Bookworm is a local, spoiler-safe EPUB reader with Q&A grounded in book chunks up to your current reading position.

## Features

- EPUB library import and cover display
- Reader with page navigation, keyboard arrows, and adjustable font/layout
- Citation links that jump to source context with highlight
- In-reader search with next/previous match navigation
- Table of contents and bookmarks
- Spoiler-safe Q&A panel with model selection
- Mobile-friendly reader controls

## Requirements

- Python 3
- OpenAI API key

## Setup

```bash
cd ~/projects/bookworm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env`:

```bash
OPENAI_API_KEY=your_key_here
```

## Run

```bash
./scripts/run_server.sh
```

Open:

- Local: `http://localhost:8000`
- Same network (phone/tablet): `http://<your-mac-ip>:8000`

The server binds to `0.0.0.0` by default.

## Usage

1. Import an `.epub` from the library view.
2. Open the book and read with `Prev`/`Next` or left/right arrow keys.
3. Use `TOC`, `Bookmarks`, and `Find` in the reader controls.
4. Open the chat panel to ask spoiler-safe questions.
5. Click citation links to jump to supporting source text.

## Troubleshooting

- `tiktoken` build failures on Python 3.13 are optional for this app; Bookworm works without it.
- If vector search errors appear, reinstall dependencies in `.venv`.

## Repo Notes

- Main branch: `main`
- Do not commit local secrets (`.env`) or local book data (`data/`).
