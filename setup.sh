#!/usr/bin/env bash
# One-time setup (macOS / Linux). Creates the Python environment in .venv and
# installs everything the app needs. The double-click launcher runs this for
# you the first time, so you normally never need to run it yourself.
#
# .venv is ignored by git. In a cloud-synced folder (OneDrive, iCloud, Dropbox),
# keep the project folder pinned ("Always Keep on This Device"): if the sync
# client moves .venv files to the cloud, Python freezes while they download.
# To put the environment elsewhere: BLOT_LADDER_VENV=/some/path ./setup.sh
set -e
cd "$(dirname "$0")"
VENV="${BLOT_LADDER_VENV:-.venv}"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo
  echo "Python 3.9 or newer was not found."
  echo "  - On a Mac: if a window asks to install the \"command line developer tools\","
  echo "    click Install, wait for it to finish, then start the app again."
  echo "  - Otherwise install Python from https://www.python.org/downloads/ and start the app again."
  exit 1
fi
echo "Using $PY ($("$PY" --version 2>&1))"
echo "Installing into $VENV (needs an internet connection; takes a minute or two)..."

"$PY" -m venv --clear "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -e ".[dev]"

echo
echo "Setup complete."
