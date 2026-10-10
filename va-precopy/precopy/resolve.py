#!/usr/bin/env python3
"""Resolve worker rank -> local_seg mapping WITHOUT parsing any log.

Part of the precopy control plane: precopy.py calls resolve_segments()
in-process when --targets is not given. This module is also runnable
standalone (debug) and as its own remote probe (see below).

Data sources (no log involved):
  seg names : mooncake master admin API  GET :<admin_port>/get_all_segments
              (plain text, one "ip:port" per line; rpc_service.cpp)
  rank->pid : pidfile written by cluster/start_worker.sh -> full process
              tree -> /proc/<pid>/cmdline proctitle "VLLM::Worker_TP<N>"
  pid->seg  : ss -ltnp listening ports INTERSECT admin segment list

Any source disagreement fails loud — a silently wrong rank<->seg map means
copying to the wrong DRAM segment (local-first is the whole point).

Cross-machine: B-side probes (pidfile/ps/ss) run over ssh by piping THIS
FILE's source to `python3 - --local-probe <pidfile>` on the target host —
no remote checkout path is assumed. That is why this module is stdlib-only.

Usage:
  python3 resolve.py --role B --tp 4                      # standalone debug
  python3 resolve.py --role B --tp 4 --ssh 'ssh root@B' \
      --pidfile /root/va-precopy/logs/worker_B.pid        # cross-machine
  python3 resolve.py --local-probe logs/worker_B.pid      # probe only (ssh target)
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import urllib.request

_WORKER_RANK_RE = re.compile(r"Worker_TP(\d+)")

# Probe exit codes (propagated through ssh; keep stable).
_PROBE_MISSING = 3  # pidfile missing/empty
_PROBE_STALE = 4  # pidfile root process dead
_PROBE_NO_SS = 5  # ss -ltnp unavailable


class ResolveError(RuntimeError):
    """Fatal, user-actionable resolve failure (fail loud)."""


class ProbeError(Exception):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------------------
# Authoritative segment list (master admin API)
# ---------------------------------------------------------------------------


def fetch_segments(admin: str) -> list[str]:
    """GET <admin>/get_all_segments — plain text, one "ip:port" per line."""
    url = f"{admin}/get_all_segments"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = resp.read().decode()
    except Exception as e:
        raise ResolveError(
            f"GET {url} failed ({type(e).__name__}: {e}) — master admin down? metrics_port?"
        ) from e
    segs = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not segs:
        raise ResolveError(f"empty segment list from {url}")
    return segs


# ---------------------------------------------------------------------------
# Probe: pidfile -> process tree -> (rank hint, listen ports)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_tree(root_pid: int) -> set[int]:
    out = subprocess.run(
        ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=True
    ).stdout
    children: dict[int, list[int]] = {}
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) != 2:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(children.get(p, ()))
    return seen


def _listen_ports_by_pid() -> dict[int, list[int]]:
    """pid -> sorted listening TCP ports, parsed from `ss -ltnp`.

    Row layout: [netid?] state recv-q send-q LOCAL PEER process. The process
    field is always last (users:(("name",pid=N,fd=M))), so LOCAL is taken
    relative to it — robust to the optional netid column.
    """
    try:
        proc = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ProbeError(_PROBE_NO_SS, "ss -ltnp unavailable (ss not found)") from e
    out: dict[int, set[int]] = {}
    for ln in proc.stdout.splitlines():
        m = re.search(r"pid=(\d+)", ln)
        if not m:
            continue
        pid = int(m.group(1))
        fields = ln.split()
        try:
            proc_idx = next(i for i, f in enumerate(fields) if f.startswith("users:"))
        except StopIteration:
            continue
        if proc_idx < 2:
            continue
        local = fields[proc_idx - 2]
        try:
            port = int(local.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
        out.setdefault(pid, set()).add(port)
    return {pid: sorted(ports) for pid, ports in out.items()}


def _rank_of_pid(pid: int) -> int | None:
    """Rank from vLLM v1 setproctitle 'VLLM::Worker_TP<N>'; None if absent."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return None
    m = _WORKER_RANK_RE.search(cmd)
    return int(m.group(1)) if m else None


def probe_local(pidfile: str) -> list[tuple[int, int | None, list[int]]]:
    """Local probe. Returns rows (pid, rank|None, listen-ports) for tree pids
    that own >=1 listening TCP port."""
    try:
        with open(pidfile, encoding="utf-8") as f:
            root = f.read().strip()
    except OSError as e:
        raise ProbeError(_PROBE_MISSING, f"pidfile {pidfile} unreadable ({e})") from e
    if not root:
        raise ProbeError(_PROBE_MISSING, f"pidfile {pidfile} empty")
    try:
        root_pid = int(root)
    except ValueError as e:
        raise ProbeError(_PROBE_MISSING, f"pidfile {pidfile} not a pid: {root!r}") from e
    if not _pid_alive(root_pid):
        raise ProbeError(_PROBE_STALE, f"pidfile {pidfile} stale: pid {root_pid} dead")
    tree = _process_tree(root_pid)
    ports_by_pid = _listen_ports_by_pid()  # may raise ProbeError(_PROBE_NO_SS)
    rows: list[tuple[int, int | None, list[int]]] = []
    for pid in sorted(tree):
        ports = ports_by_pid.get(pid)
        if ports:
            rows.append((pid, _rank_of_pid(pid), ports))
    return rows


def probe_ssh(ssh_cmd: str, pidfile: str) -> list[tuple[int, int | None, list[int]]]:
    """Run THIS FILE as the probe on the target host: pipe own source to
    `ssh ... python3 - --local-probe <pidfile>`. No remote path assumptions;
    probe exit codes propagate through ssh."""
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        src = f.read()
    cmd = shlex.split(ssh_cmd) + ["python3", "-", "--local-probe", pidfile]
    proc = subprocess.run(cmd, input=src, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProbeError(
            proc.returncode,
            proc.stderr.strip() or f"ssh probe exited rc={proc.returncode}",
        )
    rows: list[tuple[int, int | None, list[int]]] = []
    for ln in proc.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) != 3:
            continue
        pid_s, rank_s, ports_s = parts
        rows.append(
            (
                int(pid_s),
                None if rank_s == "-" else int(rank_s),
                [int(x) for x in ports_s.split(",") if x],
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Intersect: listened ports ∩ admin segment list -> rank -> seg
# ---------------------------------------------------------------------------


def _intersect(
    segs: list[str],
    rows: list[tuple[int, int | None, list[int]]],
    *,
    tp: int,
    target_ip: str,
) -> list[str]:
    def port_of(seg: str) -> str:
        return seg.rsplit(":", 1)[1]

    rank_seg: dict[int, str] = {}
    unknown: list[str] = []
    for pid, rank, ports in rows:
        for port in ports:
            matches = [
                s
                for s in segs
                if port_of(s) == str(port) and (not target_ip or s.startswith(f"{target_ip}:"))
            ]
            if len(matches) > 1:
                raise ResolveError(
                    f"port {port} (pid {pid}) matches multiple segments: {matches} "
                    "(set --target-ip)"
                )
            if not matches:
                continue
            seg = matches[0]
            if rank is None:
                unknown.append(f"{pid}={seg}")
                continue
            if rank in rank_seg and rank_seg[rank] != seg:
                raise ResolveError(
                    f"rank {rank} claimed by two segments ({rank_seg[rank]} vs {seg}) — "
                    "stale worker? pgrep -af 'VLLM::'"
                )
            rank_seg[rank] = seg

    # TP=1: the single candidate is rank 0 by construction (no proctitle needed).
    if tp == 1 and 0 not in rank_seg:
        if len(unknown) != 1:
            raise ResolveError(
                f"cannot pin the single segment for TP=1 "
                f"(candidates: {unknown or 'none'}) — stale worker? pgrep -af 'VLLM::'"
            )
        rank_seg[0] = unknown.pop().split("=", 1)[1]

    # TP>1: every rank must come from a VLLM::Worker_TP<N> proctitle.
    if tp > 1 and unknown:
        raise ResolveError(
            f"segment(s) without rank hint: {unknown} — process name pattern "
            "'VLLM::Worker_TP<N>' not seen (vLLM renamed proctitle?); "
            "check: pgrep -af 'VLLM::'"
        )

    out: list[str] = []
    for i in range(tp):
        if i not in rank_seg:
            raise ResolveError(
                f"rank {i} segment unresolved (resolved: {rank_seg}); want ranks 0..{tp - 1}"
            )
        out.append(rank_seg[i])
    return out


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def resolve_segments(
    *,
    role: str = "B",
    tp: int = 1,
    target_ip: str = "",
    master: str = "127.0.0.1:50088",
    admin: str = "",
    admin_port: int = 9003,
    pidfile: str = "",
    ssh: str = "",
    logdir: str = "",
) -> tuple[list[str], dict]:
    """Resolve target worker's rank-ordered local_seg list.

    Returns (targets, info): targets[i] is rank i's segment (feed precopy
    --targets / per-rank copy plan)."""
    if not admin:
        host = master.rsplit(":", 1)[0] if ":" in master else master
        admin = f"http://{host}:{admin_port}"
    if not pidfile:
        if ssh:
            # The default pidfile path is derived from THIS checkout — it is
            # meaningless on the remote host. Fail loud instead of probing a
            # silently wrong path.
            raise ResolveError(
                "cross-machine probe requires --pidfile (path ON the target host)"
            )
        if not logdir:
            logdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
        pidfile = os.path.join(logdir, f"worker_{role}.pid")

    segs = fetch_segments(admin)
    try:
        rows = probe_ssh(ssh, pidfile) if ssh else probe_local(pidfile)
    except ProbeError as e:
        where = ssh or "this host"
        if e.code == _PROBE_MISSING:
            raise ResolveError(
                f"pidfile {pidfile} missing/empty (worker-{role} not started via "
                f"start_worker.sh on {where}? pass --pidfile)"
            ) from e
        if e.code == _PROBE_STALE:
            raise ResolveError(
                f"pidfile {pidfile} stale: root process dead (old worker gone? "
                "re-run start_worker.sh)"
            ) from e
        if e.code == _PROBE_NO_SS:
            raise ResolveError(f"ss -ltnp unavailable on {where}") from e
        raise ResolveError(f"probe failed on {where}: {e}") from e
    if not rows:
        raise ResolveError(f"no listening TCP port found under pidfile tree {pidfile}")

    targets = _intersect(segs, rows, tp=tp, target_ip=target_ip)
    info = {
        "admin": admin,
        "pidfile": pidfile,
        "probe": ssh or "local",
        "rank_seg": {i: targets[i] for i in range(len(targets))},
    }
    return targets, info


def main() -> int:
    p = argparse.ArgumentParser(
        description="Resolve worker rank -> mooncake segment (no log parsing)"
    )
    p.add_argument("--role", default=os.environ.get("ROLE", "B"))
    p.add_argument("--tp", type=int, default=int(os.environ.get("TP", "1")))
    p.add_argument("--target-ip", default=os.environ.get("RESOLVE_TARGET_IP", ""))
    p.add_argument("--master", default=os.environ.get("MC_MASTER", "127.0.0.1:50088"))
    p.add_argument("--admin-port", type=int, default=int(os.environ.get("MC_ADMIN_PORT", "9003")))
    p.add_argument("--admin", default=os.environ.get("MC_ADMIN", ""))
    p.add_argument("--pidfile", default=os.environ.get("RESOLVE_PIDFILE", ""))
    p.add_argument("--ssh", default=os.environ.get("RESOLVE_SSH", ""))
    p.add_argument(
        "--local-probe",
        default="",
        metavar="PIDFILE",
        help="internal: run probe only, print TSV rows (used over ssh)",
    )
    args = p.parse_args()

    if args.local_probe:
        try:
            rows = probe_local(args.local_probe)
        except ProbeError as e:
            print(f"[resolve] local-probe: {e}", file=sys.stderr)
            return e.code
        for pid, rank, ports in rows:
            print(f"{pid}\t{rank if rank is not None else '-'}\t{','.join(map(str, ports))}")
        return 0

    try:
        targets, info = resolve_segments(
            role=args.role,
            tp=args.tp,
            target_ip=args.target_ip,
            master=args.master,
            admin=args.admin,
            admin_port=args.admin_port,
            pidfile=args.pidfile,
            ssh=args.ssh,
        )
    except ResolveError as e:
        print(f"[resolve] ERROR: {e}", file=sys.stderr)
        return 1

    print(f"admin:    {info['admin']}/get_all_segments ({info['probe']} probe, pidfile={info['pidfile']})")
    for rank, seg in info["rank_seg"].items():
        print(f"rank{rank} -> {seg}")
    print("TARGET_SEGMENTS=" + ",".join(targets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
