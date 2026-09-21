#!/usr/bin/env bash
# Build+install the ONE Dynamo tree into the container Python (no host .pth).
set -euo pipefail
NAME=${NAME:-vllm-ascend-lake-test}
SRC=${SRC:-${WM_ROOT:-/data/wm}/dynamo}
PROXY=${PROXY:-${https_proxy:-${HTTPS_PROXY:-}}}

[[ -d "$SRC/.git" ]] || { echo "missing $SRC — run prepare_src.sh first" >&2; exit 1; }

CA=${SSL_CERT_FILE:-/data/wm/ca-bundle-with-huawei.crt}
docker exec -e PROXY="$PROXY" -e SRC="$SRC" -e SSL_CERT_FILE="$CA" -e CURL_CA_BUNDLE="$CA" \
  -e REQUESTS_CA_BUNDLE="$CA" "$NAME" bash -lc '
set -euo pipefail
export PATH="$HOME/.cargo/bin:/usr/local/python3.12.13/bin:$PATH"
[[ -n "${PROXY:-}" ]] && export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
[[ -f "${SSL_CERT_FILE:-}" ]] && export SSL_CERT_FILE CURL_CA_BUNDLE="$SSL_CERT_FILE" REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
rm -f /usr/local/python3.12.13/lib/python3.12/site-packages/dynamo_ascend_*.pth

if ! command -v rustc >/dev/null; then
  curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
fi
source "$HOME/.cargo/env"
command -v maturin >/dev/null || cargo install maturin --locked --version 1.8.3 || cargo install maturin --locked
# skip pip patchelf (huawei mirror timeouts); optional for this build

if ! /usr/local/bin/protoc --version >/dev/null 2>&1; then
  ver=28.3
  curl -fsSL -o /tmp/protoc.zip "https://github.com/protocolbuffers/protobuf/releases/download/v${ver}/protoc-${ver}-linux-aarch_64.zip"
  mkdir -p /usr/local/protoc
  python3 -c "import zipfile; zipfile.ZipFile(\"/tmp/protoc.zip\").extractall(\"/usr/local/protoc\")"
  ln -sfn /usr/local/protoc/bin/protoc /usr/local/bin/protoc
fi
export PATH="/usr/local/bin:$PATH" PROTOC="$(command -v protoc)"

mkdir -p "$HOME/.cargo"
cat > "$HOME/.cargo/config.toml" <<EOF
[target.aarch64-unknown-linux-gnu]
rustflags = ["-C", "target-cpu=generic", "-C", "force-frame-pointers=yes", "--cfg", "tokio_unstable"]
[source.crates-io]
replace-with = "rsproxy-sparse"
[source.rsproxy-sparse]
registry = "sparse+https://rsproxy.cn/index/"
[net]
git-fetch-with-cli = true
EOF

cd "$SRC"
# Kunpeng: repo ships neoverse-n1 → SIGILL; force generic for this build
if [[ -f .cargo/config.toml ]]; then
  sed -i 's/target-cpu=neoverse-n1/target-cpu=generic/g' .cargo/config.toml
fi
export PYO3_PYTHON="$(command -v python3)"
# system Python in the image (no .venv): build wheel then pip install
maturin build --release -m lib/bindings/python/Cargo.toml -o /tmp/dynamo-wheels
pip3 install --force-reinstall --no-deps /tmp/dynamo-wheels/ai_dynamo_runtime-*.whl
# huawei pypi+proxy often times out; pull build backend from pypi.org
pip3 install -q hatchling editables pathspec pluggy trove-classifiers packaging \
  --index-url https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org
pip3 install -e ".[mocker]" --no-deps --no-build-isolation
pip3 install -q pydantic uvloop aiohttp prometheus_client msgspec pyzmq tqdm typing_extensions \
  --index-url https://pypi.org/simple --trusted-host pypi.org --trusted-host files.pythonhosted.org || true
python3 -c "import dynamo, dynamo._core as c; print(\"ok\", getattr(dynamo,\"__version__\",\"?\"), c.__file__)"
'
