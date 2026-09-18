#!/bin/bash
# Start the EM QC API + dashboard (used by .claude/launch.json "emqc").
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p var/tmp var/cache/torch var/cache/cuda var/cache/triton
export TMPDIR="$ROOT/var/tmp" TORCH_HOME="$ROOT/var/cache/torch"
export CUDA_CACHE_PATH="$ROOT/var/cache/cuda" TRITON_CACHE_DIR="$ROOT/var/cache/triton"
export PYTHONDONTWRITEBYTECODE=1
exec "$ROOT/.venv/bin/python" -u scripts/serve.py "${1:-8765}"
