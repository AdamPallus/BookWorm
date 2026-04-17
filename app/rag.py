import json
import os
from pathlib import Path
from typing import Dict, Generator, List, Optional

import httpx
from openai import OpenAI

_client = None
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

OPENCLAW_BASE_URL = "http://127.0.0.1:18789"
OPENCLAW_AGENT = "openclaw/bookworm"
OPENCLAW_BACKEND_PREFIX = "openai-codex/"

SYSTEM_PROMPT = """
You are a spoiler-free book companion.
Use ONLY the provided wiki context and raw excerpts.
Do NOT use any knowledge about this book from your training data.

The wiki context is spoiler-safe background through the last completed chapter before the reader's current location.
The raw excerpts are the authoritative source text up to the reader's exact current position.

Rules:
- Prefer the raw excerpts whenever answering about the current chapter, a precise event, or anything that needs proof.
- Use the wiki context to ground background, identity, terminology, and prior understanding.
- If the wiki context and raw excerpts seem to disagree, trust the raw excerpts.
- If the provided context is insufficient, say you don't have enough information.
- Cite raw excerpts using [c:CHUNK_ID] markers (example: [c:42]).
- Cite wiki sections using [w:SECTION_ID] markers (example: [w:17]).
- If a point is supported by both, you may cite both.
- Never use numbered citations like [1], (1), or 【1】.
- If you cite multiple raw chunks together, use one marker with comma-separated IDs, e.g. [c:42,57].
- If you rely mainly on the wiki, say so plainly instead of pretending it came from raw text.
- Keep answers concise and conversational.
""".strip()

EMBEDDING_MODEL = "text-embedding-3-small"
EMBED_BATCH_MAX_ITEMS = 128
# Stay comfortably below the per-request total input token cap.
EMBED_BATCH_MAX_EST_TOKENS = 200_000
# Guardrail for per-item token limits on embedding requests.
EMBED_ITEM_MAX_EST_TOKENS = 7_000


def _load_env():
  if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
      line = line.strip()
      if line and not line.startswith("#") and "=" in line:
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _get_client() -> OpenAI:
  global _client
  if _client is None:
    _load_env()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
      raise RuntimeError("OpenAI API key not set. Go to Settings to enter your key.")
    _client = OpenAI(api_key=api_key)
  return _client


def reset_client():
  global _client
  _client = None


def get_api_key() -> Optional[str]:
  _load_env()
  key = os.environ.get("OPENAI_API_KEY")
  if key and len(key) > 8:
    return key[:4] + "..." + key[-4:]
  return key


def set_api_key(key: str):
  os.environ["OPENAI_API_KEY"] = key

  lines = []
  found = False
  if ENV_PATH.exists():
    for line in ENV_PATH.read_text().splitlines():
      if line.strip().startswith("OPENAI_API_KEY"):
        lines.append(f"OPENAI_API_KEY={key}")
        found = True
      else:
        lines.append(line)
  if not found:
    lines.append(f"OPENAI_API_KEY={key}")

  ENV_PATH.write_text("\n".join(lines) + "\n")
  reset_client()


def _estimate_tokens(text: str) -> int:
  # Lightweight estimate to keep batch requests under OpenAI token caps.
  return max(1, len(text or "") // 4)


def _embedding_batches(texts: List[str]):
  batch: List[str] = []
  token_total = 0
  for text in texts:
    est = _estimate_tokens(text)
    if batch and (len(batch) >= EMBED_BATCH_MAX_ITEMS or token_total + est > EMBED_BATCH_MAX_EST_TOKENS):
      yield batch
      batch = []
      token_total = 0
    batch.append(text)
    token_total += est
  if batch:
    yield batch


def _clamp_text_for_embedding(text: str) -> str:
  raw = text or ""
  est = _estimate_tokens(raw)
  if est <= EMBED_ITEM_MAX_EST_TOKENS:
    return raw

  target_chars = max(4000, int(len(raw) * (EMBED_ITEM_MAX_EST_TOKENS / max(1, est))))
  clamped = raw[:target_chars]
  # Prefer ending on whitespace for cleaner truncation.
  while clamped and not clamped[-1].isspace():
    clamped = clamped[:-1]
  return clamped.strip() or raw[:target_chars]


def embed_texts(texts: List[str]) -> List[List[float]]:
  if not texts:
    return []
  safe_texts = [_clamp_text_for_embedding(t) for t in texts]
  embeddings: List[List[float]] = []
  client = _get_client()
  for batch in _embedding_batches(safe_texts):
    resp = client.embeddings.create(
      model=EMBEDDING_MODEL,
      input=batch,
    )
    embeddings.extend([d.embedding for d in resp.data])
  return embeddings


def _build_messages(question: str, excerpts: List[Dict], wiki_context: Optional[str] = None) -> List[Dict[str, str]]:
  raw_context_lines = []
  for ex in excerpts:
    raw_context_lines.append(
      f"[c:{ex['id']}] [Chapter {ex['chapter_index'] + 1} | Pos {ex['position_index']}]\n{ex['text']}"
    )
  raw_context = "\n\n".join(raw_context_lines).strip() or "(none)"
  wiki_context = (wiki_context or "").strip() or "(none)"

  return [
    {"role": "system", "content": SYSTEM_PROMPT},
    {
      "role": "user",
      "content": (
        f"Question: {question}\n\n"
        f"Wiki Context:\n{wiki_context}\n\n"
        f"Raw Excerpts:\n{raw_context}"
      ),
    },
  ]


def stream_answer(
  question: str,
  excerpts: List[Dict],
  model: str,
  wiki_context: Optional[str] = None,
) -> Generator[str, None, None]:
  _load_env()
  token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
  if not token:
    raise RuntimeError("OPENCLAW_GATEWAY_TOKEN is not set. Add it to .env.")

  messages = _build_messages(question, excerpts, wiki_context=wiki_context)
  flat_input = f"{messages[0]['content']}\n\n{messages[1]['content']}"

  payload = {
    "model": OPENCLAW_AGENT,
    "input": flat_input,
    "user": "bookworm-qa",
    "stream": True,
  }
  headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "x-openclaw-model": f"{OPENCLAW_BACKEND_PREFIX}{model}",
  }

  with httpx.stream(
    "POST",
    f"{OPENCLAW_BASE_URL}/v1/responses",
    headers=headers,
    json=payload,
    timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
  ) as response:
    response.raise_for_status()
    for line in response.iter_lines():
      if not line or not line.startswith("data:"):
        continue
      data = line[5:].strip()
      if data == "[DONE]":
        break
      try:
        evt = json.loads(data)
      except json.JSONDecodeError:
        continue
      if evt.get("type") == "response.output_text.delta":
        delta = evt.get("delta")
        if delta:
          yield delta
