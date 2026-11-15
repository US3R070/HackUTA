#!/usr/bin/env bash
# macOS / Linux wrapper → demo/run_hands_off.py
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" "$ROOT/demo/run_hands_off.py" "$@"
fi
exec python3 "$ROOT/demo/run_hands_off.py" "$@"
