#!/usr/bin/env bash
# 从 proto/ 重新生成 kvcm 的 Python gRPC stub（入仓 kvcm/api/pb/）。
# 形态对齐主仓 scripts/gen_stubs.sh：生成后把顶层 import 改成包内引用。
#
# 工具链：python3 -m grpc_tools.protoc。版本钉到生成当前入仓 stub 的
# 版本（见 kvcm/requirements-dev.txt）；他机重生成 diff 时先核对版本。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROTO_DIR="$ROOT/proto"
PY_OUT="$ROOT/kvcm/api/pb"

mkdir -p "$PY_OUT"
python3 -m grpc_tools.protoc \
  -I "$PROTO_DIR" \
  --python_out="$PY_OUT" \
  --grpc_python_out="$PY_OUT" \
  "$PROTO_DIR/kvcm_intent.proto"

# grpcio-tools 按 proto 文件名生成顶层 import；改成包内引用
# （幂等：只匹配行首裸 import，替换串不含被匹配子串）。
cd "$ROOT"
python3 - <<'PY'
from pathlib import Path

pb = Path("kvcm/api/pb")
grpc_file = pb / "kvcm_intent_pb2_grpc.py"
text = grpc_file.read_text()
text = text.replace(
    "\nimport kvcm_intent_pb2 as",
    "\nfrom kvcm.api.pb import kvcm_intent_pb2 as",
)
grpc_file.write_text(text)
(pb / "__init__.py").write_text('"""生成物包（stub 入仓，勿手改）。"""\n')
print("patched:", grpc_file)
PY

echo "OK → $PY_OUT"
