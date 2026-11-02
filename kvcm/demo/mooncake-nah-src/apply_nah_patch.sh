#!/usr/bin/env bash
# Apply 0001-nah-alloc-diag.patch onto a Mooncake checkout (v0.3.11.post1).
set -euo pipefail
ROOT=${1:?usage: apply_nah_patch.sh /path/to/Mooncake}
HERE=$(cd "$(dirname "$0")" && pwd)
test -f "$ROOT/mooncake-store/src/allocator.cpp"
patch -p1 -d "$ROOT" < "$HERE/patches/0001-nah-alloc-diag.patch"
echo "patched OK — rebuild mooncake-store, replace libmooncake_store.so + mooncake_master"
