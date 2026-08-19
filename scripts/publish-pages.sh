#!/usr/bin/env bash
# 把 tools/cmx-sim 组装成 GitHub Pages 站点并推到 gh-pages。
# 用法（仓库根目录）: bash scripts/publish-pages.sh
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
REMOTE="${PAGES_REMOTE:-github}"
BRANCH="${PAGES_BRANCH:-gh-pages}"
DEST="$(mktemp -d)"
trap 'rm -rf "$DEST"' EXIT

mkdir -p "$DEST/docs/research"
cp "$ROOT/tools/cmx-sim/index.html" "$DEST/index.html"
cp "$ROOT/tools/cmx-sim/capacity.html" "$DEST/capacity.html"
cp "$ROOT/tools/cmx-sim/economics.html" "$DEST/economics.html"
cp "$ROOT/tools/cmx-sim/measured.html" "$DEST/measured.html"
cp "$ROOT/tools/cmx-sim/app.css" "$DEST/app.css"
cp "$ROOT/tools/cmx-sim/sim.js" "$DEST/sim.js"
cp "$ROOT/tools/cmx-sim/measured.js" "$DEST/measured.js"
cp "$ROOT/tools/cmx-sim/README.md" "$DEST/README.md"
cp "$ROOT/docs/research/nvidia-cmx.md" "$DEST/docs/research/nvidia-cmx.md"
cp "$ROOT/docs/research/agentic-cache-workload.md" "$DEST/docs/research/agentic-cache-workload.md"
cp "$ROOT/docs/research/references.md" "$DEST/docs/research/references.md"
# 仓库内 ../../docs/research 在 /tools/cmx-sim/ 与 GitLab 根域名都能解析；
# GitHub 项目站在 /lake/ 下，多一层 ../ 会跳出站点，部署时改成站点相对路径。
sed -i 's|href="../../docs/research/|href="docs/research/|g' "$DEST"/*.html
sed -i 's|](../../docs/research/|](docs/research/|g' "$DEST/README.md"
touch "$DEST/.nojekyll"

git -C "$DEST" init -q
git -C "$DEST" checkout -q -b "$BRANCH"
git -C "$DEST" config user.name "$(git -C "$ROOT" config user.name)"
git -C "$DEST" config user.email "$(git -C "$ROOT" config user.email)"
git -C "$DEST" add -A
git -C "$DEST" -c commit.gpgsign=false commit -q -m "docs: 发布 CMX 计算器静态页"
git -C "$DEST" remote add origin "$(git -C "$ROOT" remote get-url "$REMOTE")"
git -C "$DEST" push -f origin "$BRANCH"

echo "pushed $BRANCH -> $(git -C "$ROOT" remote get-url "$REMOTE")"
