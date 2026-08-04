#!/usr/bin/env bash
# P3 编译门禁(不含起全栈冒烟)。仓库根执行:./scripts/verify-p3.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> rust: bins + controlplane unit tests"
(cd rust && cargo build -q \
  -p lake-controlplane --bin lake-controlplane \
  -p lake-kv-pool --bin lake-kv-pool \
  -p lake-storage-agent --bin lake-storage-agent)
(cd rust && cargo test -q -p lake-controlplane -p lake-transfer)

echo "==> go: module"
(cd go && go build ./...)

echo "==> python: stub + runtime/engine import"
PYTHONPATH=python python3 -c "
from lake.pb import lake_pb2, lake_pb2_grpc, schema_pb2
import lake.runtime, lake.prefill, lake.decode, lake.engine
from lake.runtime.worker import chain_block_hashes, mock_kv_bytes
from lake.runtime.node_scheduler import NodeScheduler, build_req_from_generate, mock_decode_tokens
from lake.engine.model_runner import ModelRunner
assert hasattr(lake_pb2_grpc, 'WorkerServiceStub')
assert hasattr(lake_pb2_grpc, 'TcpDataServiceStub')
assert hasattr(lake_pb2_grpc, 'TransferServiceStub')
assert hasattr(lake_pb2_grpc, 'AgentServiceStub')
h = chain_block_hashes(list(range(24)))
assert len(h) == 3, h
assert mock_kv_bytes(h[0]).startswith(b'KV:')
assert mock_decode_tokens([7], 2) == [1008, 1009]
print('py OK')
"

echo "OK: P3 compile gate"
