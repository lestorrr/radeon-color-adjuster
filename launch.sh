#!/bin/bash
# Start Radeon Color Adjuster with ./venv if it exists, otherwise the system Python.
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
PY="$DIR/venv/bin/python"
[ -x "$PY" ] || PY=python3
"$PY" "$DIR/main.py" "$@"
