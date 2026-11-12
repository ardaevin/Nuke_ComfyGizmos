#!/bin/bash
# ComfyGizmos installer for macOS and Linux: opens the installer window with the Python inside Nuke.
# Double-click on macOS (or run ./install.command). To use another Python: ./install.command /path/to/python
cd "$(dirname "$0")"
PY="$1"
if [ -z "$PY" ]; then
  for cand in /Applications/Nuke*/Nuke*.app/Contents/MacOS/python* \
              /Applications/Nuke*/Nuke*.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 \
              /usr/local/Nuke*/python* /opt/Nuke*/python*; do
    case "$cand" in *Beta*|*beta*) continue;; esac
    [ -x "$cand" ] && PY="$cand"
  done
fi
if [ -z "$PY" ]; then
  echo "No Nuke found. Run:  ./install.command /path/to/Nuke/python"
  exit 1
fi
echo "Starting the ComfyGizmos installer with $PY"
"$PY" installer/run.py
