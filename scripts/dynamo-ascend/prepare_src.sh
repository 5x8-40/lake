#!/usr/bin/env bash
# Ensure ONE Dynamo source tree at $SRC on $REF (default release/1.4.2; no master).
set -euo pipefail
WM_ROOT=${WM_ROOT:-/data/wm}
SRC=${SRC:-$WM_ROOT/dynamo}
REF=${REF:-release/1.4.2}
REPO=${REPO:-https://github.com/ai-dynamo/dynamo.git}

if [[ ! -d "$SRC/.git" ]]; then
  git clone --branch "$REF" --single-branch "$REPO" "$SRC"
else
  git -C "$SRC" fetch --depth 1 origin "$REF"
  git -C "$SRC" checkout -B "$REF" "FETCH_HEAD"
fi
echo "SRC=$SRC  $(git -C "$SRC" rev-parse --abbrev-ref HEAD)  $(git -C "$SRC" rev-parse --short HEAD)"
