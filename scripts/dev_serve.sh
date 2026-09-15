#!/bin/bash
# Start the EM QC API + dashboard (used by .claude/launch.json "emqc").
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -u scripts/serve.py "${1:-8765}"
