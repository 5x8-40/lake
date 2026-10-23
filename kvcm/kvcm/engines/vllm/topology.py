"""RankTopology：vLLM worker 的 rank ↔ seg 对号（不解析任何日志）。

数据源（无日志参与）：
- seg 名单：由调用方注入（存储侧 SegTopology 提供）
- rank ↔ pid：pidfile（启动脚本写）→ 进程树（/proc/*/stat ppid 链；
  ``ps`` 兜底）→ /proc/<pid>/cmdline 的 proctitle ``VLLM::Worker_TP<N>``
- pid ↔ seg：监听 TCP 端口 ∩ seg 名单；端口来自 ``ss -ltnp``，
  兜底 /proc/net/tcp{,6} × /proc/*/fd（纯 stdlib，适配极简容器）
- TP=1 降级：探测基础设施缺口（无 pidfile / 无端口来源）→ 仅凭 seg 名
  单单 seg 匹配（大声 WARN、无交叉校验；pidfile 过期 = worker 已死，
  保持报错不降级）

任何数据源之间不一致都直接报错——静默拿错 rank↔seg 映射意味着把 KV
复制到错误的 DRAM 段（本机优先是整个机制的意义）。

跨机：对端探测（pidfile + /proc）经 ssh 把**本文件源码** pipe 到目标机
``python3 - --local-probe <pidfile>`` 执行——不假设远端有任何检出路径。
探测退出码经 ssh 透传（保持稳定：3=pidfile 缺失 4=pidfile 过期
5=无端口来源）。

这是 demo 环境的实例发现机制；生产由实例注册（接口层 InstanceWatcher）
取代 pidfile/proctitle 探测，对号算法不变。
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys

_WORKER_RANK_RE = re.compile(r"Worker_TP(\d+)")
_SOCKET_RE = re.compile(r"socket:\[(\d+)\]")

# 探测退出码（经 ssh 透传；保持稳定）
_PROBE_MISSING = 3  # pidfile 缺失/为空
_PROBE_STALE = 4  # pidfile 根进程已死
_PROBE_NO_SS = 5  # 无端口列举来源（ss 缺失且 /proc 兜底失败）


class ResolveError(RuntimeError):
    """致命的、用户可行动的解析失败（fail loud）。"""


class ProbeError(Exception):
    def __init__(self, code: int, msg: str) -> None:
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------------------
# 探测：pidfile → 进程树 →（rank 提示，监听端口）
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_children_map() -> dict[int, list[int]]:
    """ppid → pids 映射。首选 /proc/*/stat（stdlib——极简容器可能无 ps）；
    兜底 ``ps -eo pid=,ppid=``（如 hidepid 场景）。"""
    children: dict[int, list[int]] = {}
    n_stat = 0
    try:
        pid_names = os.listdir("/proc")
    except OSError:
        pid_names = []
    for pid_s in pid_names:
        if not pid_s.isdigit():
            continue
        try:
            with open(f"/proc/{pid_s}/stat", "rb") as f:
                stat = f.read().decode(errors="replace")
        except OSError:
            continue
        # comm（字段 2）可能含空格/括号——取最后一个 ')' 之后的字段：
        # state ppid pgrp ...
        rparen = stat.rfind(")")
        if rparen < 0:
            continue
        fields = stat[rparen + 1 :].split()
        if len(fields) < 2:
            continue
        children.setdefault(int(fields[1]), []).append(int(pid_s))
        n_stat += 1
    if n_stat:
        return children
    out = subprocess.run(
        ["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=True
    ).stdout
    for ln in out.splitlines():
        parts = ln.split()
        if len(parts) != 2:
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        children.setdefault(ppid, []).append(pid)
    return children


def _process_tree(root_pid: int) -> set[int]:
    children = _pid_children_map()
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
    """pid → 排序后的监听 TCP 端口，解析自 ``ss -ltnp``。

    行布局：[netid?] state recv-q send-q LOCAL PEER process。process 字段
    恒在最后（users:(("name",pid=N,fd=M))），LOCAL 相对它取——对可选的
    netid 列健壮。

    ``ss`` 缺失（极简容器）时兜底 /proc 解析。
    """
    try:
        proc = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return _listen_ports_by_pid_proc()
    except subprocess.TimeoutExpired as e:
        raise ProbeError(_PROBE_NO_SS, f"ss -ltnp timed out after 10s: {e}") from e
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


def _listen_ports_by_pid_proc() -> dict[int, list[int]]:
    """无 ss 主机的兜底：LISTEN 端口来自 /proc/net/tcp{,6}（socket inode
    → 端口），经 /proc/<pid>/fd 的 socket 链接映射到 pid。

    纯 stdlib。需要读其他进程 fd 目录的权限（root 或同 uid；hidepid 挂载
    的 /proc 会破坏它 → 报错）。跨机时本函数在目标机上经 ssh 运行，宿主
    /proc 能看到容器进程（宿主侧共享 pid 命名空间视图）。
    """
    inode_port: dict[int, int] = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for ln in lines[1:]:
            fields = ln.split()
            # sl local rem st tx:rx tr:tm retrnsmt uid timeout inode ...
            if len(fields) < 10 or fields[3] != "0A":  # 0A = LISTEN
                continue
            try:
                port = int(fields[1].rsplit(":", 1)[1], 16)
                inode = int(fields[9])
            except ValueError:
                continue
            inode_port[inode] = port
    if not inode_port:
        raise ProbeError(
            _PROBE_NO_SS,
            "ss missing and /proc/net/tcp{,6} unreadable or has no LISTEN rows",
        )
    out: dict[int, set[int]] = {}
    for pid_s in os.listdir("/proc"):
        if not pid_s.isdigit():
            continue
        fd_dir = f"/proc/{pid_s}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            m = _SOCKET_RE.fullmatch(target)
            if not m:
                continue
            port = inode_port.get(int(m.group(1)))
            if port is not None:
                out.setdefault(int(pid_s), set()).add(port)
    return {pid: sorted(ports) for pid, ports in out.items()}


def _rank_of_pid(pid: int) -> int | None:
    """从 vLLM v1 proctitle ``VLLM::Worker_TP<N>`` 取 rank；无则 None。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return None
    m = _WORKER_RANK_RE.search(cmd)
    return int(m.group(1)) if m else None


def probe_local(pidfile: str) -> list[tuple[int, int | None, list[int]]]:
    """本机探测。返回进程树中持有 ≥1 监听 TCP 端口的行
    (pid, rank|None, ports)。"""
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
    ports_by_pid = _listen_ports_by_pid()  # 可能抛 ProbeError(_PROBE_NO_SS)
    rows: list[tuple[int, int | None, list[int]]] = []
    for pid in sorted(tree):
        ports = ports_by_pid.get(pid)
        if ports:
            rows.append((pid, _rank_of_pid(pid), ports))
    return rows


def probe_ssh(ssh_cmd: str, pidfile: str) -> list[tuple[int, int | None, list[int]]]:
    """在目标机上运行**本文件**作探针：把自身源码 pipe 到
    ``ssh ... python3 - --local-probe <pidfile>``。不假设远端路径；
    探测退出码经 ssh 透传。"""
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        src = f.read()
    cmd = shlex.split(ssh_cmd) + ["python3", "-", "--local-probe", pidfile]
    try:
        proc = subprocess.run(cmd, input=src, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        raise ProbeError(-1, f"ssh probe timed out after 30s: {e}") from e
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
# 交叉：监听端口 ∩ seg 名单 → rank → seg
# ---------------------------------------------------------------------------


def intersect_rank_seg(
    segs: list[str],
    rows: list[tuple[int, int | None, list[int]]],
    *,
    tp: int,
    target_ip: str,
) -> list[str]:
    """按端口把探测行与 seg 名单交叉，输出 rank 有序的 seg 列表。"""
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
                    "(restrict target_ip)"
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

    # TP=1：唯一候选即 rank 0（构造上无需 proctitle）。
    if tp == 1 and 0 not in rank_seg:
        if len(unknown) != 1:
            raise ResolveError(
                f"cannot pin the single segment for TP=1 "
                f"(candidates: {unknown or 'none'}) — stale worker? pgrep -af 'VLLM::'"
            )
        rank_seg[0] = unknown.pop().split("=", 1)[1]

    # TP>1：每个 rank 必须来自 VLLM::Worker_TP<N> proctitle。
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
# 顶层
# ---------------------------------------------------------------------------


def resolve_segments(
    *,
    segs: list[str],
    tp: int,
    pidfile: str,
    ssh: str = "",
    target_ip: str = "",
) -> tuple[list[str], dict]:
    """解析目标 worker 的 rank 有序 seg 列表。

    segs 由调用方注入（存储侧 SegTopology.segments()）；pidfile 在目标机
    上（ssh 模式必须显式给——本地检出推导的默认路径在远端无意义）。
    返回 (targets, info)：targets[i] = rank i 的 seg。"""
    if ssh and not pidfile:
        raise ResolveError(
            "cross-machine probe requires explicit pidfile (path ON the target host)"
        )

    try:
        rows = probe_ssh(ssh, pidfile) if ssh else probe_local(pidfile)
    except ProbeError as e:
        where = ssh or "this host"
        # TP=1 最后手段：目标 ip 上恰有一个 seg + TP=1 ⇒ 构造上即 rank 0。
        # 仅限基础设施缺口（无 pidfile / 无端口来源）——pidfile 过期意味着
        # worker 已死，此时降级会把 KV 复制到即将消失的 seg，保持报错。
        # 两种情形都大声 WARN；任何歧义保持报错。
        if tp == 1 and e.code != _PROBE_STALE:
            solo = [s for s in segs if not target_ip or s.startswith(f"{target_ip}:")]
            if len(solo) == 1:
                print(
                    f"[resolve] WARN: probe on {where} failed ({e}); TP=1 degraded "
                    f"to single-seg match {solo[0]} (no pid cross-check)",
                    file=sys.stderr,
                )
                return solo, {
                    "pidfile": pidfile,
                    "probe": f"single-seg degraded (probe failed: {e})",
                    "rank_seg": {0: solo[0]},
                }
        if e.code == _PROBE_MISSING:
            raise ResolveError(
                f"pidfile {pidfile} missing/empty (worker not started via "
                f"start script on {where}? pass pidfile explicitly)"
            ) from e
        if e.code == _PROBE_STALE:
            raise ResolveError(
                f"pidfile {pidfile} stale: root process dead (old worker gone? "
                "restart worker)"
            ) from e
        if e.code == _PROBE_NO_SS:
            raise ResolveError(
                f"no port-listing source on {where} (ss missing and /proc "
                "fallback failed — hidepid / no read access to other pids' fd?) — "
                "alternatives: pass segments explicitly, or resolve rank<->seg "
                "by hand (npu-smi card->pid)"
            ) from e
        raise ResolveError(f"probe failed on {where}: {e}") from e
    if not rows:
        raise ResolveError(f"no listening TCP port found under pidfile tree {pidfile}")

    targets = intersect_rank_seg(segs, rows, tp=tp, target_ip=target_ip)
    info = {
        "pidfile": pidfile,
        "probe": ssh or "local",
        "rank_seg": {i: targets[i] for i in range(len(targets))},
    }
    return targets, info


class RankTopology:
    """rank ↔ KV shard 形状与 rank ↔ seg 对号（引擎适配子件）。

    demo 环境发现机制 = pidfile/proctitle/端口交叉（本模块函数）；生产由
    实例注册取代发现机制，对号算法复用。
    """

    def __init__(self, tp_size: int) -> None:
        if tp_size < 1:
            raise ValueError(f"tp_size must be >= 1: {tp_size}")
        self.tp_size = tp_size

    def resolve_segments(
        self,
        *,
        segs: list[str],
        pidfile: str,
        ssh: str = "",
        target_ip: str = "",
    ) -> list[str]:
        """rank 有序的 seg 列表（targets[i] = rank i 的本机 seg）。"""
        targets, _ = resolve_segments(
            segs=segs, tp=self.tp_size, pidfile=pidfile, ssh=ssh, target_ip=target_ip
        )
        return targets


def _local_probe_main(argv: list[str]) -> int:
    """``--local-probe <pidfile>``：ssh 模式的远端入口（打印 TSV 行）。"""
    if len(argv) != 2 or argv[0] != "--local-probe":
        print("usage: topology.py --local-probe <pidfile>", file=sys.stderr)
        return 2
    try:
        rows = probe_local(argv[1])
    except ProbeError as e:
        print(f"[resolve] local-probe: {e}", file=sys.stderr)
        return e.code
    for pid, rank, ports in rows:
        print(f"{pid}\t{rank if rank is not None else '-'}\t{','.join(map(str, ports))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_local_probe_main(sys.argv[1:]))
