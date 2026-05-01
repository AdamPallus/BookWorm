"""Thin OpenClaw client wrapper for the wiki builder.

Reuses the same SSE-streaming endpoint as `app/rag.py`, but accumulates the
full response and parses it as JSON. Each call may use a fresh session id
(mini per-segment) or reuse one (mini retries within a segment).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import httpx

OPENCLAW_BASE_URL = "http://127.0.0.1:18789"
OPENCLAW_AGENT = "openclaw/bookworm"
OPENCLAW_BACKEND_PREFIX = "openai-codex/"

ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"

DEFAULT_MINI_MODEL = "gpt-5.4-mini"

JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class OpenClawError(RuntimeError):
  pass


class JsonParseError(OpenClawError):
  def __init__(self, raw_text: str, error: str):
    super().__init__(f"failed to parse JSON from OpenClaw response: {error}")
    self.raw_text = raw_text
    self.parse_error = error


@dataclass
class OpenClawResponse:
  raw_text: str
  parsed: Optional[dict]
  parse_error: Optional[str]


def _load_env() -> None:
  if not ENV_PATH.exists():
    return
  for line in ENV_PATH.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, _, val = line.partition("=")
    os.environ.setdefault(key.strip(), val.strip())


def new_session_id(label: str = "wiki-builder") -> str:
  return f"bookworm-{label}-{uuid.uuid4()}"


def _post_stream(payload: dict, headers: dict, *, read_timeout: float) -> tuple[str, Optional[dict]]:
  """Return (text, usage). `usage` is the {input_tokens, output_tokens,
  total_tokens} dict from the terminal `response.completed` event, or None
  if the stream ended without one."""
  buf: List[str] = []
  usage: Optional[dict] = None
  with httpx.stream(
    "POST",
    f"{OPENCLAW_BASE_URL}/v1/responses",
    headers=headers,
    json=payload,
    timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=30.0, pool=10.0),
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
      etype = evt.get("type")
      if etype == "response.output_text.delta":
        delta = evt.get("delta")
        if delta:
          buf.append(delta)
      elif etype == "response.completed":
        reported = evt.get("response", {}).get("usage")
        if isinstance(reported, dict):
          usage = reported
  return "".join(buf), usage


def call(
  input_text: str,
  *,
  model: str,
  session_id: str,
  read_timeout: float = 600.0,
  max_attempts: int = 2,
  backoff_seconds: float = 5.0,
) -> tuple[str, Optional[dict]]:
  """Call OpenClaw with the given input and return (text, usage).

  `usage` mirrors the `response.usage` object from the terminal
  `response.completed` event — typically {input_tokens, output_tokens,
  total_tokens}. May be None if the stream ended without one.

  Retries transient httpx errors. Does NOT retry JSON parse failures — the
  caller decides whether to issue a corrective follow-up message.
  """
  _load_env()
  token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
  if not token:
    raise OpenClawError("OPENCLAW_GATEWAY_TOKEN is not set in .env")

  payload = {
    "model": OPENCLAW_AGENT,
    "input": input_text,
    "user": session_id,
    "stream": True,
  }
  headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "x-openclaw-model": f"{OPENCLAW_BACKEND_PREFIX}{model}",
  }

  last_error: Optional[Exception] = None
  for attempt in range(1, max_attempts + 1):
    try:
      return _post_stream(payload, headers, read_timeout=read_timeout)
    except (httpx.HTTPError, httpx.StreamError) as e:
      last_error = e
      if attempt >= max_attempts:
        break
      time.sleep(backoff_seconds * attempt)
  raise OpenClawError(f"openclaw call failed after {max_attempts} attempts: {last_error}") from last_error


def _strip_code_fence(text: str) -> str:
  match = JSON_FENCE_RE.search(text)
  if match:
    return match.group(1).strip()
  return text.strip()


def _slice_first_object(text: str) -> Optional[str]:
  start = text.find("{")
  if start < 0:
    return None
  depth = 0
  in_str = False
  esc = False
  for i in range(start, len(text)):
    ch = text[i]
    if in_str:
      if esc:
        esc = False
      elif ch == "\\":
        esc = True
      elif ch == '"':
        in_str = False
      continue
    if ch == '"':
      in_str = True
      continue
    if ch == "{":
      depth += 1
    elif ch == "}":
      depth -= 1
      if depth == 0:
        return text[start : i + 1]
  return None


def parse_json_response(raw_text: str) -> OpenClawResponse:
  """Best-effort JSON extraction. The model is asked for raw JSON, but we
  accept fenced ```json``` blocks too."""
  candidate = _strip_code_fence(raw_text)
  try:
    return OpenClawResponse(raw_text=raw_text, parsed=json.loads(candidate), parse_error=None)
  except json.JSONDecodeError as e:
    sliced = _slice_first_object(candidate)
    if sliced:
      try:
        return OpenClawResponse(raw_text=raw_text, parsed=json.loads(sliced), parse_error=None)
      except json.JSONDecodeError as e2:
        return OpenClawResponse(raw_text=raw_text, parsed=None, parse_error=str(e2))
    return OpenClawResponse(raw_text=raw_text, parsed=None, parse_error=str(e))


def call_json(
  input_text: str,
  *,
  model: str,
  session_id: str,
  read_timeout: float = 600.0,
) -> OpenClawResponse:
  raw, _usage = call(input_text, model=model, session_id=session_id, read_timeout=read_timeout)
  return parse_json_response(raw)
