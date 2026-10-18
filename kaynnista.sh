#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -m pip show urheiluhallit-kone >/dev/null 2>&1; then
  .venv/bin/python -m pip install -e .
fi
if [ "$#" -eq 0 ]; then
  exec .venv/bin/python -u -m uhkone --help
fi
exec .venv/bin/python -u -m uhkone "$@"
