#!/usr/bin/env bash
# Double-click this file to start the Western Blot Ladder Labeler (macOS).
# The first time, it installs what the app needs (internet required, a minute or two).
# Keep the window that opens; close it to quit the app.
cd "$(dirname "$0")" || exit 1
printf '\033]0;Western Blot Ladder Labeler\007'  # window title
clear 2>/dev/null
echo "================================================"
echo "        Western Blot Ladder Labeler"
echo "================================================"

pause_and_exit() {
  echo
  read -n 1 -s -r -p "Press any key to close this window."
  echo
  exit "${1:-1}"
}

VENV="${BLOT_LADDER_VENV:-.venv}"
export PYTHONUNBUFFERED=1  # show messages immediately
if ! "$VENV/bin/python" -c "import flask, numpy, scipy, PIL, tifffile" >/dev/null 2>&1; then
  echo
  echo "First-time setup: installing the app..."
  echo
  if ! ./setup.sh; then
    echo
    echo "Setup did not finish. Check your internet connection and try again."
    echo "If it keeps failing, send a screenshot of this window to whoever shared the app."
    pause_and_exit 1
  fi
fi

echo
echo "Starting... (this takes a few seconds)"
"$VENV/bin/python" launch.py || {
  echo
  echo "The app stopped with an error (see above)."
  pause_and_exit 1
}
