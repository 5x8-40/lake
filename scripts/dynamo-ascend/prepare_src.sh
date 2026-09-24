#!/usr/bin/env bash
# Clone/checkout 5x8-40/dynamo-ascend into $SRC (default: lake/3rdparty/dynamo-ascend).
# Protocol registration for MooncakeConnectorV1 lives in dynamo-ascend (not a lake patch).
# Default REF=feat/ascend-1.4.2-protocol-kunpeng (PR → release/1.4.2).
# After merge: REF=release/1.4.2. Do not use ascend-dev (1.5.0).
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LAKE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
SRC=${SRC:-$LAKE_ROOT/3rdparty/dynamo-ascend}
REF=${REF:-feat/ascend-1.4.2-protocol-kunpeng}
REPO=${REPO:-https://github.com/5x8-40/dynamo-ascend.git}

if [[ ! -d "$SRC/.git" ]]; then
  git clone --branch "$REF" --single-branch "$REPO" "$SRC"
else
  git -C "$SRC" fetch --depth 1 origin "$REF"
  git -C "$SRC" checkout -B "$REF" "FETCH_HEAD"
fi

proto=$SRC/components/src/dynamo/vllm/kv_connector_protocols.py
if ! grep -q 'MooncakeConnectorV1' "$proto" 2>/dev/null; then
  echo "ERROR: $proto has no MooncakeConnectorV1 — need dynamo-ascend branch $REF." >&2
  exit 1
fi

echo "SRC=$SRC  $(git -C "$SRC" rev-parse --abbrev-ref HEAD)  $(git -C "$SRC" rev-parse --short HEAD)"
