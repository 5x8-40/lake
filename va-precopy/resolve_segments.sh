#!/usr/bin/env bash
# Resolve worker local_seg names from mooncake_master mount logs.
#
# MooncakeBackend sets local_seg = get_ip() + ":" + rpc_port; master logs:
#   action=mount_segment, segment_name=<ip>:<port>
#
# Usage:
#   bash resolve_segments.sh
#   eval "$(bash resolve_segments.sh --export)"
#   eval "$(bash resolve_segments.sh --export --target-ip 7.242.105.217 --tp 4)"
#
# With --target-ip + --tp N: export TARGET_SEGMENTS as N segs for that IP
# (chronological mount order ≈ rank 0..N-1). Without them: last two mounts = A/B (TP=1).
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOGDIR=${LOGDIR:-$SCRIPT_DIR/logs}
MASTER_LOG=${MASTER_LOG:-$LOGDIR/mooncake_master.log}
EXPORT=0
TARGET_IP=${TARGET_IP:-}
TP=${TP:-}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --export) EXPORT=1; shift ;;
    --target-ip) TARGET_IP=$2; shift 2 ;;
    --tp) TP=$2; shift 2 ;;
    *)
      echo "[resolve_segments] ERROR: unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

# Env fallbacks (for e2e)
TARGET_IP=${TARGET_IP:-${RESOLVE_TARGET_IP:-}}
TP=${TP:-${RESOLVE_TP:-}}

if [[ ! -f "$MASTER_LOG" ]]; then
  echo "[resolve_segments] ERROR: missing $MASTER_LOG" >&2
  exit 1
fi

mapfile -t SEGS < <(
  grep -E 'action=mount_segment, segment_name=' "$MASTER_LOG" \
    | sed -E 's/.*segment_name=//' \
    | awk 'NF' \
    | tac \
    | awk '!seen[$0]++' \
    | head -40 \
    | tac
)

filter_demo() {
  local s
  for s in "$@"; do
    case "$s" in
      *:10001|*:10002) continue ;;
      *) echo "$s" ;;
    esac
  done
}

mapfile -t FILTERED < <(filter_demo "${SEGS[@]}")
if [[ ${#FILTERED[@]} -lt 1 ]]; then
  echo "[resolve_segments] ERROR: no mount_segment lines in $MASTER_LOG" >&2
  exit 1
fi

if [[ -n "$TARGET_IP" && -n "$TP" ]]; then
  mapfile -t B_SEGS < <(
    for s in "${FILTERED[@]}"; do
      case "$s" in
        "${TARGET_IP}:"*) echo "$s" ;;
      esac
    done | tail -n "$TP"
  )
  if [[ ${#B_SEGS[@]} -ne "$TP" ]]; then
    echo "[resolve_segments] ERROR: want $TP segs for IP=$TARGET_IP, got ${#B_SEGS[@]}" >&2
    printf '  all: %s\n' "${FILTERED[@]}" >&2
    printf '  matched: %s\n' "${B_SEGS[@]:-}" >&2
    exit 1
  fi
  # Join with comma for precopy.py --targets
  TARGET_SEGMENTS=$(IFS=,; echo "${B_SEGS[*]}")
  SEG_B=${B_SEGS[0]}
  TARGET_SEGMENT=$SEG_B
  # Best-effort SEG_A: other IP mounts, first of last wave
  mapfile -t A_SEGS < <(
    for s in "${FILTERED[@]}"; do
      case "$s" in
        "${TARGET_IP}:"*) ;;
        *) echo "$s" ;;
      esac
    done | tail -n "$TP"
  )
  SEG_A=${A_SEGS[0]:-}
  if [[ "$EXPORT" == "1" ]]; then
    echo "SEG_A=$(printf '%q' "$SEG_A")"
    echo "SEG_B=$(printf '%q' "$SEG_B")"
    echo "TARGET_SEGMENT=$(printf '%q' "$TARGET_SEGMENT")"
    echo "TARGET_SEGMENTS=$(printf '%q' "$TARGET_SEGMENTS")"
  else
    echo "SEG_A=$SEG_A"
    echo "SEG_B=$SEG_B"
    echo "TARGET_SEGMENT=$TARGET_SEGMENT"
    echo "TARGET_SEGMENTS=$TARGET_SEGMENTS"
    printf 'B_SEGS(%d): %s\n' "${#B_SEGS[@]}" "${B_SEGS[*]}"
  fi
  exit 0
fi

if [[ ${#FILTERED[@]} -lt 2 ]]; then
  echo "[resolve_segments] ERROR: need >=2 mounts for TP=1 A/B (got ${#FILTERED[@]})" >&2
  printf '  seen: %s\n' "${SEGS[@]:-}" >&2
  exit 1
fi

SEG_A=${FILTERED[-2]}
SEG_B=${FILTERED[-1]}
TARGET_SEGMENT=$SEG_B
TARGET_SEGMENTS=$SEG_B

if [[ "$EXPORT" == "1" ]]; then
  echo "SEG_A=$(printf '%q' "$SEG_A")"
  echo "SEG_B=$(printf '%q' "$SEG_B")"
  echo "TARGET_SEGMENT=$(printf '%q' "$TARGET_SEGMENT")"
  echo "TARGET_SEGMENTS=$(printf '%q' "$TARGET_SEGMENTS")"
else
  echo "SEG_A=$SEG_A"
  echo "SEG_B=$SEG_B"
  echo "TARGET_SEGMENT=$SEG_B  # worker-B local_seg for precopy.py --target"
  echo "TARGET_SEGMENTS=$TARGET_SEGMENTS"
fi
