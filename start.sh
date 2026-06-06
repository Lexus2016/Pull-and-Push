#!/usr/bin/env bash
# Pull-and-Push — one-command install & run.
#
# Creates a CPython virtualenv (.venv), installs the package, and launches the
# dashboard. Safe to re-run: steps already done are skipped. Any extra arguments
# are passed straight to `pull-and-push web`.
#
#   ./start.sh                 # install if needed, then open http://127.0.0.1:8765
#   ./start.sh --port 8080     # run on another port
#   ./start.sh --update        # reinstall dependencies (e.g. after `git pull`)
#
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"

# split off our own --update flag; everything else goes to the dashboard
UPDATE=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--update" ]; then UPDATE=1; else ARGS+=("$a"); fi
done

# Find a usable CPython 3.10+ interpreter (NOT PyPy — its sqlite driver breaks the
# dashboard's database layer). Only needed when creating the venv the first time.
pick_python() {
  for cand in python3.12 python3.11 python3.13 python3.10 python3 python; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" - <<'PY' >/dev/null 2>&1
import sys, platform
sys.exit(0 if platform.python_implementation() == "CPython"
         and sys.version_info[:2] >= (3, 10) else 1)
PY
    then echo "$cand"; return 0; fi
  done
  return 1
}

# 1. virtualenv
if [ ! -d "$VENV" ]; then
  if ! PY="$(pick_python)"; then
    echo "✖ Need CPython 3.10+ (not PyPy). Install one (macOS: 'brew install python@3.12';" >&2
    echo "  or from https://www.python.org/downloads/) and run ./start.sh again." >&2
    exit 1
  fi
  echo "▶ Creating virtualenv with $("$PY" --version 2>&1) …"
  "$PY" -m venv "$VENV"
  UPDATE=1   # fresh venv → must install
fi

# resolve the venv interpreter (handle Git-Bash on Windows too)
PYBIN="$VENV/bin/python";            [ -x "$PYBIN" ] || PYBIN="$VENV/Scripts/python.exe"

# 2. install (first run, or --update, or if the package somehow isn't importable)
if [ "$UPDATE" = "1" ] || ! "$PYBIN" -c "import tyani_tolkai" >/dev/null 2>&1; then
  echo "▶ Installing dependencies (.[web]) …"
  "$PYBIN" -m pip install -q --upgrade pip
  "$PYBIN" -m pip install -q -e ".[web]"
fi

# 3. launch the dashboard — prefer the console script, fall back to `python -m`.
#    Extra args (e.g. --port 8080) pass through.
echo "▶ Starting the dashboard — open the URL printed below (Ctrl-C to stop)."
if   [ -x "$VENV/bin/pull-and-push" ];         then exec "$VENV/bin/pull-and-push" web ${ARGS[@]+"${ARGS[@]}"}
elif [ -x "$VENV/Scripts/pull-and-push.exe" ]; then exec "$VENV/Scripts/pull-and-push.exe" web ${ARGS[@]+"${ARGS[@]}"}
else exec "$PYBIN" -m tyani_tolkai.cli web ${ARGS[@]+"${ARGS[@]}"}
fi
