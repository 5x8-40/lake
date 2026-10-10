#!/usr/bin/env bash
# Resolve worker rank -> local_seg mapping WITHOUT parsing any log.
#
# Data sources (no log involved):
#   seg names : mooncake master admin API  GET :<admin_port>/get_all_segments
#               (plain text, one "ip:port" per line; rpc_service.cpp)
#   rank->pid : pidfile written by start_worker.sh -> full process tree ->
#               /proc/<pid>/cmdline proctitle "VLLM::Worker_TP<N>" (TP>1)
#   pid->seg  : ss -ltnp listening ports INTERSECT admin segment list
#
# Any source disagreement fails loud — a silently wrong rank<->seg map means
# copying to the wrong DRAM segment (local-first is the whole point).
#
# Usage:
#   bash resolve_segments.sh                        # role B, TP=1, local pidfile
#   eval "$(bash resolve_segments.sh --export --role B --tp 4)"
#   eval "$(bash resolve_segments.sh --export --role B --tp 4 \
#             --ssh 'ssh root@7.242.105.217' \
#             --pidfile /root/va-precopy/logs/worker_B.pid)"
#
# Cross-machine: B-side probes (pidfile/ps/ss) run over --ssh; the master
# admin API is queried locally. --pidfile is the path ON THE B HOST.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOGDIR=${LOGDIR:-$SCRIPT_DIR/logs}
EXPORT=0
ROLE=${ROLE:-B}
TP=${TP:-1}
TARGET_IP=${TARGET_IP:-}
MC_PORT=${MC_PORT:-50088}
MC_MASTER=${MC_MASTER:-127.0.0.1:$MC_PORT}
MC_ADMIN_PORT=${MC_ADMIN_PORT:-9003}
MC_ADMIN=${MC_ADMIN:-}
RESOLVE_SSH=${RESOLVE_SSH:-}
PIDFILE=${PIDFILE:-}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --export) EXPORT=1; shift ;;
    --role) ROLE=$2; shift 2 ;;
    --tp) TP=$2; shift 2 ;;
    --target-ip) TARGET_IP=$2; shift 2 ;;
    --master) MC_MASTER=$2; shift 2 ;;
    --admin-port) MC_ADMIN_PORT=$2; shift 2 ;;
    --admin) MC_ADMIN=$2; shift 2 ;;
    --pidfile) PIDFILE=$2; shift 2 ;;
    --ssh) RESOLVE_SSH=$2; shift 2 ;;
    *)
      echo "[resolve_segments] ERROR: unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

# Env fallbacks (for e2e)
TARGET_IP=${TARGET_IP:-${RESOLVE_TARGET_IP:-}}
PIDFILE=${PIDFILE:-$LOGDIR/worker_${ROLE}.pid}
if [[ -z "$MC_ADMIN" ]]; then
  _host=${MC_MASTER%:*}
  MC_ADMIN="http://${_host}:${MC_ADMIN_PORT}"
fi

fail() { echo "[resolve_segments] ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Authoritative segment list from master admin API (text, one per line)
# ---------------------------------------------------------------------------
SEG_LIST=$(curl -sf --max-time 5 "$MC_ADMIN/get_all_segments") \
  || fail "GET $MC_ADMIN/get_all_segments failed (master admin down? metrics_port?)"
[[ -n "${SEG_LIST//[[:space:]]/}" ]] || fail "empty segment list from $MC_ADMIN"

# ---------------------------------------------------------------------------
# 2. Probe worker host: pidfile -> process tree -> (rank hint, listen ports)
#    Prints one line per tree pid that owns >=1 listening TCP port:
#      pid <TAB> rank-or-'-' <TAB> port1,port2,...
# ---------------------------------------------------------------------------
probe_host() {
  local pidfile=$1
  [[ -f $pidfile ]] || return 3
  local root
  root=$(cat "$pidfile" 2>/dev/null)
  [[ -n ${root:-} ]] || return 3
  kill -0 "$root" 2>/dev/null || return 4

  local -A seen=()
  local stack=("$root") p c
  while ((${#stack[@]})); do
    p=${stack[-1]}
    unset 'stack[-1]'
    [[ -n ${seen[$p]:-} ]] && continue
    seen[$p]=1
    while read -r c; do
      [[ -n $c ]] && stack+=("$c")
    done < <(ps -eo pid=,ppid= | awk -v pp="$p" '$2==pp{print $1}')
  done

  local ss_out
  ss_out=$(ss -ltnp 2>/dev/null) || return 5
  local cmd rank ports
  for p in "${!seen[@]}"; do
    cmd=$(tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null)
    rank="-"
    # vLLM v1 setproctitle: VLLM::EngineCore / VLLM::Worker_TP<N> / VLLM::APIServer
    if [[ $cmd =~ Worker_TP([0-9]+) ]]; then
      rank="${BASH_REMATCH[1]}"
    fi
    ports=$(awk -v pid="pid=${p}," 'index($0,pid){print $4}' <<<"$ss_out" \
      | sed -E 's/.*:([0-9]+)$/\1/' | sort -un | paste -sd, -)
    [[ -n ${ports:-} ]] && printf '%s\t%s\t%s\n' "$p" "$rank" "$ports"
  done
  return 0
}

run_probe() { # $1=pidfile -> PROBE_OUT, rc propagated
  if [[ -n $RESOLVE_SSH ]]; then
    # shellcheck disable=SC2086
    { declare -f probe_host; echo 'probe_host "$1"'; } | $RESOLVE_SSH bash -s -- "$1"
  else
    probe_host "$1"
  fi
}

PROBE_OUT=$(run_probe "$PIDFILE") && _rc=0 || _rc=$?
case $_rc in
  0) ;;
  3) fail "pidfile $PIDFILE missing/empty (worker-$ROLE not started via start_worker.sh on ${RESOLVE_SSH:-this host}? pass --pidfile)" ;;
  4) fail "pidfile $PIDFILE stale: root process dead (old worker gone? re-run start_worker.sh)" ;;
  5) fail "ss -ltnp unavailable on ${RESOLVE_SSH:-this host}" ;;
  *) fail "probe failed (rc=$_rc) on ${RESOLVE_SSH:-localhost}" ;;
esac
[[ -n $PROBE_OUT ]] || fail "no listening TCP port found under pidfile tree $PIDFILE"

# ---------------------------------------------------------------------------
# 3. Intersect listened ports with admin segment list -> rank -> seg
# ---------------------------------------------------------------------------
declare -A RANK_SEG=()
UNKNOWN_PID=()
while IFS=$'\t' read -r pid rank ports; do
  IFS=',' read -ra plist <<<"$ports"
  for port in "${plist[@]}"; do
    matches=()
    while IFS= read -r seg; do
      [[ $seg == *:"$port" ]] || continue
      if [[ -n $TARGET_IP && $seg != "$TARGET_IP":* ]]; then
        continue
      fi
      matches+=("$seg")
    done <<<"$SEG_LIST"
    if ((${#matches[@]} > 1)); then
      fail "port $port (pid $pid) matches multiple segments: ${matches[*]} (set --target-ip)"
    fi
    ((${#matches[@]} == 1)) || continue
    seg=${matches[0]}
    if [[ $rank == "-" ]]; then
      UNKNOWN_PID+=("$pid=$seg")
      continue
    fi
    if [[ -n ${RANK_SEG[$rank]:-} && ${RANK_SEG[$rank]} != "$seg" ]]; then
      fail "rank $rank claimed by two segments (${RANK_SEG[$rank]} vs $seg) — stale worker? pgrep -af 'VLLM::'"
    fi
    RANK_SEG[$rank]=$seg
  done
done <<<"$PROBE_OUT"

# TP=1: the single candidate is rank 0 by construction (no proctitle needed).
if ((TP == 1)); then
  if [[ -z ${RANK_SEG[0]:-} ]]; then
    ((${#UNKNOWN_PID[@]} == 1)) \
      || fail "cannot pin the single segment for TP=1 (candidates: ${UNKNOWN_PID[*]:-none}) — stale worker? pgrep -af 'VLLM::'"
    RANK_SEG[0]=${UNKNOWN_PID[0]#*=}
    UNKNOWN_PID=()
  fi
fi

# TP>1: every rank must come from a VLLM::Worker_TP<N> proctitle.
if ((TP > 1)) && ((${#UNKNOWN_PID[@]} > 0)); then
  fail "segment(s) without rank hint: ${UNKNOWN_PID[*]} — process name pattern 'VLLM::Worker_TP<N>' not seen (vLLM renamed proctitle?); check: ${RESOLVE_SSH:+$RESOLVE_SSH }pgrep -af 'VLLM::'"
fi

B_SEGS=()
for ((i = 0; i < TP; i++)); do
  [[ -n ${RANK_SEG[$i]:-} ]] \
    || fail "rank $i segment unresolved (resolved: $(for r in "${!RANK_SEG[@]}"; do printf 'rank%s=%s ' "$r" "${RANK_SEG[$r]}"; done)); want ranks 0..$((TP - 1))"
  B_SEGS+=("${RANK_SEG[$i]}")
done

TARGET_SEGMENTS=$(IFS=,; echo "${B_SEGS[*]}")
SEG_B=${B_SEGS[0]}
TARGET_SEGMENT=$SEG_B

# Best-effort SEG_A (display only): local role-A pidfile, rank 0.
SEG_A=""
if [[ -z $RESOLVE_SSH && $ROLE == "B" && -f $LOGDIR/worker_A.pid ]]; then
  if A_PROBE=$(probe_host "$LOGDIR/worker_A.pid" 2>/dev/null); then
    while IFS=$'\t' read -r pid rank ports; do
      [[ $rank == "0" || $rank == "-" ]] || continue
      IFS=',' read -ra plist <<<"$ports"
      for port in "${plist[@]}"; do
        while IFS= read -r seg; do
          if [[ $seg == *:"$port" && $seg != "$SEG_B" ]]; then
            SEG_A=$seg
            break 3
          fi
        done <<<"$SEG_LIST"
      done
    done <<<"$A_PROBE"
  fi
fi

if [[ $EXPORT == "1" ]]; then
  echo "SEG_A=$(printf '%q' "$SEG_A")"
  echo "SEG_B=$(printf '%q' "$SEG_B")"
  echo "TARGET_SEGMENT=$(printf '%q' "$TARGET_SEGMENT")"
  echo "TARGET_SEGMENTS=$(printf '%q' "$TARGET_SEGMENTS")"
else
  echo "admin:    $MC_ADMIN/get_all_segments (${RESOLVE_SSH:-local probe}, pidfile=$PIDFILE)"
  echo "SEG_A=$SEG_A"
  echo "SEG_B=$SEG_B"
  echo "TARGET_SEGMENT=$TARGET_SEGMENT"
  echo "TARGET_SEGMENTS=$TARGET_SEGMENTS"
  for ((i = 0; i < TP; i++)); do
    printf 'rank%d -> %s\n' "$i" "${B_SEGS[$i]}"
  done
fi
