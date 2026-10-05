#!/usr/bin/env bash
# Start the app (macOS / Linux). Run ./setup.sh once first.
cd "$(dirname "$0")"
VENV="${BLOT_LADDER_VENV:-.venv}"
if [ ! -x "$VENV/bin/python" ]; then
  echo "Environment not found at $VENV. Run ./setup.sh first."
  exit 1
fi
echo "Starting… (first launch can take a few seconds)"
exec "$VENV/bin/python" launch.py
