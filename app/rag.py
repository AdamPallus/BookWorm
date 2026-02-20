import os
from pathlib import Path
from typing import Dict, Generator, List, Optional

from openai import OpenAI

_client = None
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

SYSTEM_PROMPT = """
You are a spoiler-free book companion. Answer ONLY using the provided excerpts.
Do NOT use any knowledge about this book from your training data.
If the excerpts don't contain the answer, say you don't have enough information.
Cite your sources using [c:CHUNK_ID] markers (example: [c:42]).
Never use numbered citations like [1], (1), or 【1】.
If you cite multiple chunks together, use one marker with comma-separated IDs, e.g. [c:42,57].
Keep answers concise and conversational.
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


def _build_messages(question: str, excerpts: List[Dict]) -> List[Dict[str, str]]:
  context_lines = []
  for ex in excerpts:
    context_lines.append(
      f"[c:{ex['id']}] [Chapter {ex['chapter_index'] + 1} | Pos {ex['position_index']}]\n{ex['text']}"
    )
  context = "\n\n".join(context_lines)

  return [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": f"Question: {question}\n\nExcerpts:\n{context}"},
  ]


def stream_answer(question: str, excerpts: List[Dict], model: str) -> Generator[str, None, None]:
  stream = _get_client().chat.completions.create(
    model=model,
    messages=_build_messages(question, excerpts),
    stream=True,
  )

  for event in stream:
    choices = getattr(event, "choices", None) or []
    if not choices:
      continue
    delta = getattr(choices[0], "delta", None)
    if not delta:
      continue
    content = getattr(delta, "content", None)
    if not content:
      continue
    if isinstance(content, str):
      yield content
    else:
      for part in content:
        text = getattr(part, "text", None)
        if text:
          yield text
