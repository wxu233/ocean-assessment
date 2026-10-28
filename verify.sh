#!/usr/bin/env bash
# Run the acceptance tests (docs/verify.md) on the project's .venv Python.
# Each test seeds its own SQLite file; APP_DB_PATH is also pointed at a
# throwaway file so nothing can touch data/app.db.
# Extra arguments go to pytest, e.g. `./verify.sh -m slow` for the 100k A6 test.
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "error: $PY not found. Run ./start.sh once (or: python3.12 -m venv .venv && $PY -m pip install -r backend/requirements.txt)." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
export APP_DB_PATH="$TMP_DIR/verify.db"

exec "$PY" -m pytest "$@"
