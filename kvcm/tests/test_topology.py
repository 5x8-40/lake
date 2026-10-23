"""kvcm.engines.vllm.topology 单元 + 集成检查。

intersect_rank_seg 用例为纯函数（合成 segs/rows）。集成用例用真实 OS
状态：持有真实监听 socket 的子进程 + 真实 pidfile——pidfile/ps/ss//proc
解析端到端走一遍。无第三方依赖（pytest 除外），随处可跑。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

from kvcm.engines.vllm import topology as tp_mod
from kvcm.engines.vllm.topology import (
    ProbeError,
    ResolveError,
    _listen_ports_by_pid,
    _listen_ports_by_pid_proc,
    _process_tree,
    intersect_rank_seg,
    probe_local,
    resolve_segments,
)

_LISTENER = (
    "import socket,sys,time;"
    "s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1);"
    "print(s.getsockname()[1],flush=True);time.sleep(120)"
)

_has_proc_tcp = os.path.exists("/proc/net/tcp")


def _listener_proc():
    return subprocess.Popen(
        [sys.executable, "-c", _LISTENER], stdout=subprocess.PIPE, text=True
    )


# ---------------------------------------------------------------------------
# intersect_rank_seg（纯函数）
# ---------------------------------------------------------------------------


def test_tp1_happy() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, None, [5001])]
    assert intersect_rank_seg(segs, rows, tp=1, target_ip="") == ["10.0.0.1:5001"]


def test_tp2_rank_order() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(101, 1, [5002]), (100, 0, [5001])]  # 探测顺序 ≠ rank 顺序
    assert intersect_rank_seg(segs, rows, tp=2, target_ip="") == [
        "10.0.0.1:5001",
        "10.0.0.1:5002",
    ]


def test_tp1_ambiguous_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, None, [5001]), (101, None, [5002])]
    with pytest.raises(ResolveError, match="cannot pin the single segment"):
        intersect_rank_seg(segs, rows, tp=1, target_ip="")


def test_tp2_unknown_rank_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, 0, [5001]), (101, None, [5002])]
    with pytest.raises(ResolveError, match="without rank hint"):
        intersect_rank_seg(segs, rows, tp=2, target_ip="")


def test_multi_segment_port_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.2:5001"]  # 同端口两主机
    rows = [(100, None, [5001])]
    with pytest.raises(ResolveError, match="matches multiple segments"):
        intersect_rank_seg(segs, rows, tp=1, target_ip="")


def test_target_ip_disambiguates() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.2:5001"]
    rows = [(100, None, [5001])]
    assert intersect_rank_seg(segs, rows, tp=1, target_ip="10.0.0.2") == ["10.0.0.2:5001"]


def test_missing_rank_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, 1, [5002])]  # rank 0 从未出现
    with pytest.raises(ResolveError, match="rank 0 segment unresolved"):
        intersect_rank_seg(segs, rows, tp=2, target_ip="")


def test_rank_conflict_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, 0, [5001]), (101, 0, [5002])]
    with pytest.raises(ResolveError, match="claimed by two segments"):
        intersect_rank_seg(segs, rows, tp=2, target_ip="")


# ---------------------------------------------------------------------------
# 集成：真实进程树 + socket + pidfile
# ---------------------------------------------------------------------------


def test_end_to_end_tp1() -> None:
    proc = _listener_proc()
    try:
        port = int(proc.stdout.readline().strip())
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(str(proc.pid))
            pidfile = f.name
        try:
            rows = probe_local(pidfile)
            assert any(p == proc.pid and port in ports for p, _, ports in rows), rows
            assert rows[0][1] is None  # 此处无 VLLM::Worker_TP proctitle

            targets, info = resolve_segments(
                segs=[f"127.0.0.1:{port}", "127.0.0.1:1"],  # 带噪音 seg
                tp=1,
                pidfile=pidfile,
            )
            assert targets == [f"127.0.0.1:{port}"], targets
            assert info["rank_seg"] == {0: f"127.0.0.1:{port}"}
        finally:
            os.unlink(pidfile)
    finally:
        proc.kill()
        proc.wait()


def test_stale_pidfile_fails() -> None:
    # 起进程再杀掉收尸，拿一个确定已死的 pid（不写死 magic pid）
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    proc.kill()
    proc.wait()
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(str(proc.pid))
        pidfile = f.name
    try:
        with pytest.raises(ResolveError, match="stale"):
            resolve_segments(segs=["127.0.0.1:5001"], tp=1, pidfile=pidfile)
    finally:
        os.unlink(pidfile)


def test_missing_pidfile_fails() -> None:
    with pytest.raises(ResolveError, match="missing/empty"):
        resolve_segments(
            segs=["127.0.0.1:5001", "127.0.0.1:5002"],  # 歧义：无 TP=1 降级
            tp=1,
            pidfile="/nonexistent/worker_B.pid",
        )


def test_ssh_requires_explicit_pidfile() -> None:
    with pytest.raises(ResolveError, match="requires explicit pidfile"):
        resolve_segments(segs=["127.0.0.1:5001"], tp=1, pidfile="", ssh="ssh x")


def test_garbage_pidfile_fails() -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("not-a-pid")
        pidfile = f.name
    try:
        with pytest.raises(ProbeError, match="not a pid"):
            probe_local(pidfile)
    finally:
        os.unlink(pidfile)


# ---------------------------------------------------------------------------
# /proc 兜底（无 ss 主机——极简容器）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _has_proc_tcp, reason="no /proc on this host")
def test_proc_fallback_finds_listener() -> None:
    proc = _listener_proc()
    try:
        port = int(proc.stdout.readline().strip())
        ports = _listen_ports_by_pid_proc()
        assert port in ports.get(proc.pid, []), (proc.pid, port, ports.get(proc.pid))
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(not _has_proc_tcp, reason="no /proc on this host")
def test_dispatch_falls_back_without_ss(monkeypatch) -> None:
    orig_run = tp_mod.subprocess.run

    def no_ss(cmd, **kw):
        if list(cmd)[:2] == ["ss", "-ltnp"]:
            raise FileNotFoundError("ss")
        return orig_run(cmd, **kw)

    proc = _listener_proc()
    try:
        port = int(proc.stdout.readline().strip())
        monkeypatch.setattr(tp_mod.subprocess, "run", no_ss)
        ports = _listen_ports_by_pid()  # 不得抛 _PROBE_NO_SS
        assert port in ports.get(proc.pid, []), (proc.pid, port)
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# 进程树（/proc 首选，ps 兜底）+ TP=1 单 seg 降级
# ---------------------------------------------------------------------------


def test_process_tree_contains_child() -> None:
    proc = _listener_proc()
    try:
        proc.stdout.readline()  # 等到监听
        assert proc.pid in _process_tree(os.getpid())
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(not os.path.exists("/proc"), reason="no /proc on this host")
def test_process_tree_ps_fallback(monkeypatch) -> None:
    orig_listdir = tp_mod.os.listdir

    def no_proc(path, *a, **kw):
        if path == "/proc":
            raise OSError("hidepid")
        return orig_listdir(path, *a, **kw)

    proc = _listener_proc()
    try:
        proc.stdout.readline()
        monkeypatch.setattr(tp_mod.os, "listdir", no_proc)
        assert proc.pid in _process_tree(os.getpid())  # 经 ps
    finally:
        proc.kill()
        proc.wait()


def test_tp1_single_seg_degrade() -> None:
    targets, info = resolve_segments(
        segs=["127.0.0.1:5001"],  # 恰有一个 seg
        tp=1,
        pidfile="/nonexistent/worker_B.pid",  # 探测必失败
    )
    assert targets == ["127.0.0.1:5001"], targets
    assert info["probe"].startswith("single-seg degraded"), info


def test_tp1_single_seg_ambiguous_fails() -> None:
    with pytest.raises(ResolveError, match="missing/empty"):
        resolve_segments(
            segs=["127.0.0.1:5001", "127.0.0.1:5002"],  # 两个 seg，降级拒绝歧义
            tp=1,
            pidfile="/nonexistent/worker_B.pid",
        )


def test_rank_topology_class() -> None:
    rt = tp_mod.RankTopology(tp_size=1)
    targets = rt.resolve_segments(
        segs=["127.0.0.1:5001"], pidfile="/nonexistent/worker_B.pid"
    )
    assert targets == ["127.0.0.1:5001"]


def test_pid_children_map_ps_fallback_failure_is_probe_error(monkeypatch) -> None:
    """/proc 一个都读不到且 ps 缺失/失败 → ProbeError(_PROBE_NO_PS)，
    不得让 FileNotFoundError/CalledProcessError 裸逃。"""
    monkeypatch.setattr(tp_mod.os, "listdir", lambda _p: (_ for _ in ()).throw(OSError("no proc")))

    def _boom(*a, **k):
        raise FileNotFoundError("ps not found")

    monkeypatch.setattr(tp_mod.subprocess, "run", _boom)
    with pytest.raises(ProbeError) as ei:
        tp_mod._pid_children_map()
    assert ei.value.code == tp_mod._PROBE_NO_PS
