#!/bin/bash
# 软钉单独动态库。只链官方 store.so，不包含 Mooncake 头文件。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SRC="$(cd "$(dirname "$0")" && pwd)"
PY="${ROOT}/kvcm/.venv/bin/python"
STORE="${ROOT}/kvcm/.venv/lib/python3.12/site-packages/mooncake/store.so"
"${PY}" -m pip install -q 'pybind11==2.13.6'
PYBIND="$("${PY}" -m pybind11 --includes)"
SUFFIX="$("${PY}" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
# shellcheck disable=SC2086
c++ -std=c++20 -O2 -fPIC -shared ${PYBIND} \
  "${SRC}/empty_soft_pin.cpp" \
  "${STORE}" \
  -Wl,--allow-shlib-undefined \
  -Wl,-rpath,"${ROOT}/kvcm/.venv/lib/python3.12/site-packages/mooncake" \
  -o "${SRC}/_empty_pin${SUFFIX}"
echo "built ${SRC}/_empty_pin${SUFFIX}"
