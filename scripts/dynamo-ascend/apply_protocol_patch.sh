#!/usr/bin/env bash
# Apply Ascend MooncakeConnectorV1 protocol registration onto release/1.4.2 SRC.
# Idempotent: skips if MooncakeConnectorV1 is already registered.
set -euo pipefail
WM_ROOT=${WM_ROOT:-/data/wm}
SRC=${SRC:-$WM_ROOT/dynamo}
PATCH_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/patches" && pwd)
PATCH=$PATCH_DIR/0001-mooncake-connector-v1-protocol.patch

[[ -d "$SRC/.git" ]] || { echo "missing $SRC — run prepare_src.sh first" >&2; exit 1; }
[[ -f "$PATCH" ]] || { echo "missing $PATCH" >&2; exit 1; }

proto=$SRC/components/src/dynamo/vllm/kv_connector_protocols.py
if grep -q 'MooncakeConnectorV1' "$proto" 2>/dev/null; then
  echo "already patched: MooncakeConnectorV1 in $proto"
  exit 0
fi

# Prefer git apply (clean tree); fall back to patch -p1 for dirty trees.
if git -C "$SRC" apply --check "$PATCH" 2>/dev/null; then
  git -C "$SRC" apply "$PATCH"
else
  patch -d "$SRC" -p1 --forward <"$PATCH" || {
    echo "failed to apply $PATCH" >&2
    exit 1
  }
fi
grep -q 'MooncakeConnectorV1' "$proto"
echo "applied: $PATCH → $SRC"
