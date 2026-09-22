#!/usr/bin/env bash
# Ensure patched 1.4.2 (MooncakeConnectorV1) is importable inside the container.
# Prefers already-installed / .pth-wired SRC; falls back to editable pip install.
set -euo pipefail
NAME=${NAME:-vllm-ascend-lake-test}
SRC=${SRC:-${WM_ROOT:-/data/wm}/dynamo}
SP=/usr/local/python3.12.13/lib/python3.12/site-packages

[[ -f "$SRC/components/src/dynamo/vllm/kv_connector_protocols.py" ]] || {
  echo "missing $SRC (kv_connector_protocols.py)" >&2
  exit 1
}
grep -q MooncakeConnectorV1 "$SRC/components/src/dynamo/vllm/kv_connector_protocols.py" || {
  echo "SRC missing MooncakeConnectorV1 — run apply_protocol_patch.sh" >&2
  exit 1
}

docker exec -e SRC="$SRC" -e SP="$SP" "$NAME" bash -lc '
set -euo pipefail
export PATH=/usr/local/python3.12.13/bin:$PATH

# Ensure SRC is on the import path (no host .pth required for end users who
# pip-install inside the container; this helps when runtime was built separately).
if [[ ! -f "$SP/dynamo_lake_runtime.pth" ]]; then
  echo "$SRC/lib/bindings/python/src" > "$SP/dynamo_lake_runtime.pth"
fi
if [[ ! -f "$SP/dynamo_lake_components.pth" ]]; then
  echo "$SRC/components/src" > "$SP/dynamo_lake_components.pth"
fi
# Prefer SRC over any stale site-packages dynamo
rm -f "$SP"/dynamo_ascend_*.pth

python3 - <<PY
from dynamo.vllm import kv_connector_protocols as k
assert "MooncakeConnectorV1" in k.KV_CONNECTOR_PROTOCOLS, sorted(k.KV_CONNECTOR_PROTOCOLS)
print("ok", "MooncakeConnectorV1", k.KV_CONNECTOR_PROTOCOLS["MooncakeConnectorV1"])
print("proto", k.__file__)
PY
'
