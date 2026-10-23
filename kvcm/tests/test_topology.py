#!/usr/bin/env python3
"""kvcm.engines.vllm.topology 单元 + 集成检查。

intersect_rank_seg 用例为纯函数（合成 segs/rows）。集成用例用真实 OS
状态：持有真实监听 socket 的子进程 + 真实 pidfile——pidfile/ps/ss//proc
解析端到端走一遍。无第三方依赖，随处可跑。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

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


def _expect_err(fn, needle: str, exc=ResolveError) -> None:
    try:
        fn()
    except exc as e:
        assert needle in str(e), f"want {needle!r} in error: {e}"
        return
    raise AssertionError(f"expected {exc.__name__} containing {needle!r}")


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
    _expect_err(
        lambda: intersect_rank_seg(segs, rows, tp=1, target_ip=""),
        "cannot pin the single segment",
    )


def test_tp2_unknown_rank_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, 0, [5001]), (101, None, [5002])]
    _expect_err(
        lambda: intersect_rank_seg(segs, rows, tp=2, target_ip=""),
        "without rank hint",
    )


def test_multi_segment_port_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.2:5001"]  # 同端口两主机
    rows = [(100, None, [5001])]
    _expect_err(
        lambda: intersect_rank_seg(segs, rows, tp=1, target_ip=""),
        "matches multiple segments",
    )


def test_target_ip_disambiguates() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.2:5001"]
    rows = [(100, None, [5001])]
    assert intersect_rank_seg(segs, rows, tp=1, target_ip="10.0.0.2") == ["10.0.0.2:5001"]


def test_missing_rank_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, 1, [5002])]  # rank 0 从未出现
    _expect_err(
        lambda: intersect_rank_seg(segs, rows, tp=2, target_ip=""),
        "rank 0 segment unresolved",
    )


def test_rank_conflict_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, 0, [5001]), (101, 0, [5002])]
    _expect_err(
        lambda: intersect_rank_seg(segs, rows, tp=2, target_ip=""),
        "claimed by two segments",
    )


# ---------------------------------------------------------------------------
# 集成：真实进程树 + socket + pidfile
# ---------------------------------------------------------------------------

_LISTENER = (
    "import socket,sys,time;"
    "s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1);"
    "print(s.getsockname()[1],flush=True);time.sleep(120)"
)


def test_end_to_end_tp1() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _LISTENER], stdout=subprocess.PIPE, text=True
    )
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
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("999999")  # 几乎必然已死
        pidfile = f.name
    try:
        _expect_err(
            lambda: resolve_segments(
                segs=["127.0.0.1:5001"], tp=1, pidfile=pidfile
            ),
            "stale",
        )
    finally:
        os.unlink(pidfile)


def test_missing_pidfile_fails() -> None:
    _expect_err(
        lambda: resolve_segments(
            segs=["127.0.0.1:5001", "127.0.0.1:5002"],  # 歧义：无 TP=1 降级
            tp=1,
            pidfile="/nonexistent/worker_B.pid",
        ),
        "missing/empty",
    )


def test_ssh_requires_explicit_pidfile() -> None:
    _expect_err(
        lambda: resolve_segments(segs=["127.0.0.1:5001"], tp=1, pidfile="", ssh="ssh x"),
        "requires explicit pidfile",
    )


def test_garbage_pidfile_fails() -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("not-a-pid")
        pidfile = f.name
    try:
        _expect_err(lambda: probe_local(pidfile), "not a pid", exc=ProbeError)
    finally:
        os.unlink(pidfile)


# ---------------------------------------------------------------------------
# /proc 兜底（无 ss 主机——极简容器）
# ---------------------------------------------------------------------------


def test_proc_fallback_finds_listener() -> None:
    if not os.path.exists("/proc/net/tcp"):
        return  # 本机无 /proc（如 macOS）——兜底在此不可测
    proc = subprocess.Popen(
        [sys.executable, "-c", _LISTENER], stdout=subprocess.PIPE, text=True
    )
    try:
        port = int(proc.stdout.readline().strip())
        ports = _listen_ports_by_pid_proc()
        assert port in ports.get(proc.pid, []), (proc.pid, port, ports.get(proc.pid))
    finally:
        proc.kill()
        proc.wait()


def test_dispatch_falls_back_without_ss() -> None:
    if not os.path.exists("/proc/net/tcp"):
        return
    orig_run = tp_mod.subprocess.run

    def no_ss(cmd, **kw):
        if list(cmd)[:2] == ["ss", "-ltnp"]:
            raise FileNotFoundError("ss")
        return orig_run(cmd, **kw)

    proc = subprocess.Popen(
        [sys.executable, "-c", _LISTENER], stdout=subprocess.PIPE, text=True
    )
    try:
        port = int(proc.stdout.readline().strip())
        tp_mod.subprocess.run = no_ss
        try:
            ports = _listen_ports_by_pid()  # 不得抛 _PROBE_NO_SS
        finally:
            tp_mod.subprocess.run = orig_run
        assert port in ports.get(proc.pid, []), (proc.pid, port)
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# 进程树（/proc 首选，ps 兜底）+ TP=1 单 seg 降级
# ---------------------------------------------------------------------------


def test_process_tree_contains_child() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _LISTENER], stdout=subprocess.PIPE, text=True
    )
    try:
        proc.stdout.readline()  # 等到监听
        assert proc.pid in _process_tree(os.getpid())
    finally:
        proc.kill()
        proc.wait()


def test_process_tree_ps_fallback() -> None:
    if not os.path.exists("/proc"):
        return
    orig_listdir = tp_mod.os.listdir

    def no_proc(path, *a, **kw):
        if path == "/proc":
            raise OSError("hidepid")
        return orig_listdir(path, *a, **kw)

    proc = subprocess.Popen(
        [sys.executable, "-c", _LISTENER], stdout=subprocess.PIPE, text=True
    )
    try:
        proc.stdout.readline()
        tp_mod.os.listdir = no_proc
        try:
            assert proc.pid in _process_tree(os.getpid())  # 经 ps
        finally:
            tp_mod.os.listdir = orig_listdir
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
    _expect_err(
        lambda: resolve_segments(
            segs=["127.0.0.1:5001", "127.0.0.1:5002"],  # 两个 seg，降级拒绝歧义
            tp=1,
            pidfile="/nonexistent/worker_B.pid",
        ),
        "missing/empty",
    )


def test_rank_topology_class() -> None:
    rt = tp_mod.RankTopology(tp_size=1)
    targets = rt.resolve_segments(
        segs=["127.0.0.1:5001"], pidfile="/nonexistent/worker_B.pid"
    )
    assert targets == ["127.0.0.1:5001"]


TESTS = [
    test_tp1_happy,
    test_tp2_rank_order,
    test_tp1_ambiguous_fails,
    test_tp2_unknown_rank_fails,
    test_multi_segment_port_fails,
    test_target_ip_disambiguates,
    test_missing_rank_fails,
    test_rank_conflict_fails,
    test_end_to_end_tp1,
    test_stale_pidfile_fails,
    test_missing_pidfile_fails,
    test_ssh_requires_explicit_pidfile,
    test_garbage_pidfile_fails,
    test_proc_fallback_finds_listener,
    test_dispatch_falls_back_without_ss,
    test_process_tree_contains_child,
    test_process_tree_ps_fallback,
    test_tp1_single_seg_degrade,
    test_tp1_single_seg_ambiguous_fails,
    test_rank_topology_class,
]

if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"test_topology: PASS ({len(TESTS)} cases)")
