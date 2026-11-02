#!/usr/bin/env bash
# Tail master log for allocation_failed / copy_start_alloc_fail; snapshot metrics.
set -euo pipefail
LOG=${1:-/data/w84156052-104-45/lake/kvcm/demo/logs/mooncake_master.log}
OUT=${2:-/data/w84156052-104-45/lake/kvcm/demo/logs/nah_alloc_fail_watch.log}
: > "$OUT"
echo "watching $LOG -> $OUT"
tail -n0 -F "$LOG" | while IFS= read -r line; do
  if [[ "$line" == *allocation_failed* || "$line" == *offset_alloc_fail* || "$line" == *copy_start_alloc_fail* || "$line" == *NO_AVAILABLE_HANDLE* ]]; then
    ts=$(date -Iseconds)
    echo "==== $ts ====" | tee -a "$OUT"
    echo "$line" | tee -a "$OUT"
    curl -sf --max-time 1 http://127.0.0.1:9003/metrics 2>/dev/null \
      | grep -E 'master_allocated_bytes|master_total_capacity_bytes|master_key_count|put_start_alloc|segment' \
      | tee -a "$OUT" || true
  fi
done
