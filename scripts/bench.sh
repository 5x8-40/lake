#!/usr/bin/env bash
# P7.1 测量基座:一键跑三语言探针,合并为 bench/results/summary-<utc>.json。
# 探针只测不写断言失败即非零退出;CI 不跑(原型相对值,不门禁)。
set -euo pipefail
cd "$(dirname "$0")/.."

OUT_DIR=bench/results
TS=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT_DIR"
GO_OUT="$OUT_DIR/go-$TS.jsonl"
RS_OUT="$OUT_DIR/rust-$TS.jsonl"
PY_OUT="$OUT_DIR/python-$TS.jsonl"
SUMMARY="$OUT_DIR/summary-$TS.json"

echo "== go probe =="
(cd go && LAKE_BENCH_OUT="$PWD/../$GO_OUT" go test ./router/ -run TestBenchP71 -count=1)

echo "== rust probe =="
(cd rust && LAKE_BENCH_OUT="$PWD/../$RS_OUT" cargo run -q -p lake-tiered-store --bin p71_probe)

echo "== python probe =="
LAKE_BENCH_OUT="$PWD/$PY_OUT" PYTHONPATH=python python3 -m lake.runtime.bench

echo "== merge =="
python3 - "$SUMMARY" "$GO_OUT" "$RS_OUT" "$PY_OUT" <<'EOF'
import json, platform, subprocess, sys, datetime

summary_path, *inputs = sys.argv[1:]
records = []
for p in inputs:
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

def git_sha():
    return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()

summary = {
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "env": {
        "git": git_sha(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "note": "原型相对值(mock 计算/loopback 传输);真硬件校准 defer(issue #61)",
    },
    "records": records,
}
with open(summary_path, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
print(f"wrote {summary_path} ({len(records)} records)")
EOF
