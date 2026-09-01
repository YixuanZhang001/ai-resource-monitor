#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
  .venv/bin/python scripts/start.py "$@"
else
  python3 scripts/start.py "$@"
fi
