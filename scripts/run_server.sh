#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

resolve_bin() {
  local name="$1"
  if command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
    return 0
  fi
  if [[ -x "${REPO_ROOT}/.venv/bin/${name}" ]]; then
    echo "${REPO_ROOT}/.venv/bin/${name}"
    return 0
  fi
  return 1
}

# Load simple KEY=VALUE entries from .env into process env if not already set.
if [[ -f ".env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" == *=* ]]; then
      key="${line%%=*}"
      val="${line#*=}"
      if [[ -z "${!key:-}" ]]; then
        export "$key=$val"
      fi
    fi
  done < ".env"
fi

UVICORN_BIN="$(resolve_bin uvicorn || true)"
if [[ -z "${UVICORN_BIN}" ]]; then
  echo "Could not find uvicorn. Install dependencies first: pip install -r requirements.txt" >&2
  exit 1
fi

DEFAULT_CMD=("${UVICORN_BIN}" app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --reload)
CMD=("$@")
if [[ ${#CMD[@]} -eq 0 ]]; then
  CMD=("${DEFAULT_CMD[@]}")
fi

exec "${CMD[@]}"
