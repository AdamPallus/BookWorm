# Bookworm MVP (Phase 1)

Local, spoiler-free ebook companion.

## Setup

```bash
cd ~/projects/bookworm
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY=...  # required

# Optional: better token counting during ingest
pip install tiktoken
```

## Run

```bash
./scripts/run_server.sh
```

Open: http://localhost:8000

## Notes
- MVP supports **.epub** only.
- Requires sqlite-vec extension (via `sqlite-vec` package). If vectors fail to load, reinstall deps.
- Manual reading position is chapter index (0-based) + percent (0-100).
