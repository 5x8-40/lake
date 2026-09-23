#!/usr/bin/env bash
# Clone/checkout 5x8-40/dynamo-ascend into $SRC (default: lake/3rdparty/dynamo-ascend).
# Protocol registration for MooncakeConnectorV1 lives in dynamo-ascend (not a lake patch).
# Requires MooncakeConnectorV1 on $REF (default: protocol branch until
# https://github.com/5x8-40/dynamo-ascend/pull/2 merges into ascend-dev).
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LAKE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
SRC=${SRC:-$LAKE_ROOT/3rdparty/dynamo-ascend}
REF=${REF:-feat/mooncake-connector-v1-protocol}
REPO=${REPO:-https://github.com/5x8-40/dynamo-ascend.git}

if [[ ! -d "$SRC/.git" ]]; then
  git clone --branch "$REF" --single-branch "$REPO" "$SRC"
else
  git -C "$SRC" fetch --depth 1 origin "$REF"
  git -C "$SRC" checkout -B "$REF" "FETCH_HEAD"
fi

proto=$SRC/components/src/dynamo/vllm/kv_connector_protocols.py
if ! grep -q 'MooncakeConnectorV1' "$proto" 2>/dev/null; then
  echo "ERROR: $proto has no MooncakeConnectorV1 — merge dynamo-ascend#2 (or set REF to that branch)." >&2
  exit 1
fi

echo "SRC=$SRC  $(git -C "$SRC" rev-parse --abbrev-ref HEAD)  $(git -C "$SRC" rev-parse --short HEAD)"
