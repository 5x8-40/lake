#!/usr/bin/env bash
# Ascend A2 (RoCE) env for Mooncake AscendDirectTransport / HCCL one-sided.
#
# Official baseline: vllm-ascend kv_pool.md (HARDWARE_SERIES=A2):
#   HCCL_INTRA_ROCE_ENABLE=1
#   HCCL_IF_IP / HCCL_SOCKET_IFNAME / GLOO_SOCKET_IFNAME / TP_SOCKET_IFNAME
#
# Usage (source, do not exec):
#   export LOCAL_IP=7.242.105.245 NIC_NAME=enp67s0f5
#   source ./env_ascend_a2.sh
#
# Or let the script guess the first non-loopback IPv4 + its iface:
#   source ./env_ascend_a2.sh
#
# Optional:
#   APPLY_HUGEPAGES=1  # write /proc/sys/vm/nr_hugepages (default: off; avoid DRY_RUN/host surprises)
#   NR_HUGEPAGES=200000
#
# NOTE: this file is SOURCED — do NOT `set -e/-u` here (they would leak into
# the calling shell, incl. interactive sessions). Guard variables explicitly.

_guess_ip_iface() {
  # Prefer a routable NIC with an IPv4 address (skip lo / docker0 / virbr*).
  local line iface ip
  while read -r line; do
    iface=${line%% *}
    case "$iface" in
      lo|docker0|virbr*|br-*|veth*) continue ;;
    esac
    ip=$(ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{print $4}' | head -1 | cut -d/ -f1)
    if [[ -n "${ip:-}" ]]; then
      echo "$ip $iface"
      return 0
    fi
  done < <(ip -br link 2>/dev/null | awk '$2 ~ /UP|UNKNOWN/ {print $1}')
  # Fallback: hostname -I first address (iface unknown).
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  if [[ -n "${ip:-}" ]]; then
    echo "$ip"
    return 0
  fi
  return 1
}

if [[ -z "${LOCAL_IP:-}" || -z "${NIC_NAME:-}" ]]; then
  guessed=$(_guess_ip_iface || true)
  if [[ -n "${guessed:-}" ]]; then
    LOCAL_IP=${LOCAL_IP:-${guessed%% *}}
    rest=${guessed#* }
    if [[ "$rest" != "$guessed" && -z "${NIC_NAME:-}" ]]; then
      NIC_NAME=$rest
    fi
  fi
fi

if [[ -z "${LOCAL_IP:-}" ]]; then
  echo "[env_ascend_a2] ERROR: set LOCAL_IP (host IPv4 used in Mooncake local_seg)" >&2
  return 1 2>/dev/null || exit 1
fi
if [[ -z "${NIC_NAME:-}" ]]; then
  echo "[env_ascend_a2] WARN: NIC_NAME unset; HCCL_SOCKET_IFNAME/GLOO/TP not set" >&2
else
  export GLOO_SOCKET_IFNAME=${GLOO_SOCKET_IFNAME:-$NIC_NAME}
  export TP_SOCKET_IFNAME=${TP_SOCKET_IFNAME:-$NIC_NAME}
  export HCCL_SOCKET_IFNAME=${HCCL_SOCKET_IFNAME:-$NIC_NAME}
fi

export HCCL_IF_IP=${HCCL_IF_IP:-$LOCAL_IP}
export HCCL_INTRA_ROCE_ENABLE=${HCCL_INTRA_ROCE_ENABLE:-1}
export PYTHONHASHSEED=${PYTHONHASHSEED:-0}

if [[ "${APPLY_HUGEPAGES:-0}" == "1" && -w /proc/sys/vm/nr_hugepages ]]; then
  echo "${NR_HUGEPAGES:-200000}" > /proc/sys/vm/nr_hugepages || true
fi

echo "[env_ascend_a2] HCCL_INTRA_ROCE_ENABLE=$HCCL_INTRA_ROCE_ENABLE"
echo "[env_ascend_a2] HCCL_IF_IP=$HCCL_IF_IP"
echo "[env_ascend_a2] HCCL_SOCKET_IFNAME=${HCCL_SOCKET_IFNAME:-<unset>} GLOO=${GLOO_SOCKET_IFNAME:-<unset>} TP=${TP_SOCKET_IFNAME:-<unset>}"
echo "[env_ascend_a2] PYTHONHASHSEED=$PYTHONHASHSEED"
echo "[env_ascend_a2] nr_hugepages=$(cat /proc/sys/vm/nr_hugepages 2>/dev/null || echo n/a)"
