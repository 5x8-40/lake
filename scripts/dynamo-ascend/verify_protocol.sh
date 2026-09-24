#!/usr/bin/env bash
# Verify MooncakeConnectorV1 is importable inside the container (does not install).
# Host-side path checks: prepare_src.sh; install-time assert: build_install.sh.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LAKE_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
NAME=${NAME:-vllm-ascend-lake-test}
SRC=${SRC:-$LAKE_ROOT/3rdparty/dynamo-ascend}

[[ -f "$SRC/components/src/dynamo/vllm/kv_connector_protocols.py" ]] || {
  echo "missing $SRC — run prepare_src.sh first" >&2
  exit 1
}

docker exec -e SRC="$SRC" "$NAME" bash -lc '
set -euo pipefail
export PATH=/usr/local/python3.12.13/bin:$PATH
python3 - <<PY
from dynamo.vllm import kv_connector_protocols as k
assert "MooncakeConnectorV1" in k.KV_CONNECTOR_PROTOCOLS, sorted(k.KV_CONNECTOR_PROTOCOLS)
print("ok", "MooncakeConnectorV1", k.KV_CONNECTOR_PROTOCOLS["MooncakeConnectorV1"])
print("proto", k.__file__)
PY
'
