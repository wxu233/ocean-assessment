#!/usr/bin/env bash
# One-command startup: install, seed if empty, serve on :8000.
#   ./start.sh          keep existing data (seed only creates it if missing)
#   ./start.sh --reset  recreate the demo data first
# Never adds the 100k rows; use backend.scripts.gen_large for that.
set -euo pipefail
cd "$(dirname "$0")"

RESET=""
for arg in "$@"; do
  case "$arg" in
    --reset) RESET="--reset" ;;
    *) echo "usage: ./start.sh [--reset]" >&2; exit 2 ;;
  esac
done

# --- Python 3.12 ---
PYTHON=""
for cand in python3.12 python3; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))' 2>/dev/null; then
    PYTHON="$cand"; break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "error: Python 3.12 is required (python3.12 not found on PATH)." >&2
  exit 1
fi

# --- Backend install ---
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv with $PYTHON"
  "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r backend/requirements.txt

# --- Frontend build: added with the UI slice (S6) ---

# --- Seed (idempotent; --reset recreates) ---
.venv/bin/python -m backend.scripts.seed $RESET

# --- Serve ---
echo "Serving on http://localhost:8000"
exec .venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
