#!/usr/bin/env bash
# Install official Meta SAM 2.1 Large inside this project. No system CUDA changes.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p var/models var/vendor var/cache/pip var/tmp
export PIP_CACHE_DIR="$ROOT/var/cache/pip" TMPDIR="$ROOT/var/tmp"
export PYTHONDONTWRITEBYTECODE=1
SAM_REV=2b90b9f5ceec907a1c18123530e92e794ad901a4
SAM_SHA=2647878d5dfa5098f2f8649825738a9345572bae2d4350a2468587ece47dd318
.venv/bin/python -m pip install torch==2.10.0 torchvision==0.25.0 --index-url https://download.pytorch.org/whl/cu128
if [ ! -d var/vendor/sam2 ]; then
  git clone https://github.com/facebookresearch/sam2.git var/vendor/sam2
  git -C var/vendor/sam2 checkout "$SAM_REV"
fi
if [ "$(git -C var/vendor/sam2 rev-parse HEAD)" != "$SAM_REV" ]; then
  echo 'SAM source revision differs; inspect var/vendor/sam2 before installing.' >&2
  exit 1
fi
SAM2_BUILD_CUDA=0 .venv/bin/python -m pip install --no-build-isolation -e var/vendor/sam2
WEIGHT=var/models/sam2.1_hiera_large.pt
if [ ! -f "$WEIGHT" ]; then
  curl -fL --retry 3 -o "$WEIGHT.part" https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_large.pt
  echo "$SAM_SHA  $WEIGHT.part" | sha256sum -c -
  mv "$WEIGHT.part" "$WEIGHT"
fi
echo "$SAM_SHA  $WEIGHT" | sha256sum -c -
.venv/bin/python -m pip check
