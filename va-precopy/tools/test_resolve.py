#!/usr/bin/env python3
"""Unit + integration checks for precopy/resolve.py.

_intersect cases are pure (synthetic segs/rows). Integration cases use real
OS state: a child process owning a real listening socket + a real pidfile +
a real local HTTP admin endpoint — so pidfile/ps/ss//proc parsing is
exercised end to end. No third-party deps; runs anywhere.
"""

from __future__ import annotations

import http.server
import os
import subprocess
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRECOPY = os.path.join(os.path.dirname(_HERE), "precopy")
if _PRECOPY not in sys.path:
    sys.path.insert(0, _PRECOPY)

from resolve import (  # noqa: E402
    ResolveError,
    _intersect,
    probe_local,
    resolve_segments,
)


def _expect_err(fn, needle: str) -> None:
    try:
        fn()
    except ResolveError as e:
        assert needle in str(e), f"want {needle!r} in error: {e}"
        return
    raise AssertionError(f"expected ResolveError containing {needle!r}")


# ---------------------------------------------------------------------------
# _intersect (pure)
# ---------------------------------------------------------------------------


def test_tp1_happy() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, None, [5001])]
    assert _intersect(segs, rows, tp=1, target_ip="") == ["10.0.0.1:5001"]


def test_tp2_rank_order() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(101, 1, [5002]), (100, 0, [5001])]  # probe order != rank order
    assert _intersect(segs, rows, tp=2, target_ip="") == [
        "10.0.0.1:5001",
        "10.0.0.1:5002",
    ]


def test_tp1_ambiguous_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, None, [5001]), (101, None, [5002])]
    _expect_err(
        lambda: _intersect(segs, rows, tp=1, target_ip=""),
        "cannot pin the single segment",
    )


def test_tp2_unknown_rank_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, 0, [5001]), (101, None, [5002])]
    _expect_err(
        lambda: _intersect(segs, rows, tp=2, target_ip=""),
        "without rank hint",
    )


def test_multi_segment_port_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.2:5001"]  # same port, two hosts
    rows = [(100, None, [5001])]
    _expect_err(
        lambda: _intersect(segs, rows, tp=1, target_ip=""),
        "matches multiple segments",
    )


def test_target_ip_disambiguates() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.2:5001"]
    rows = [(100, None, [5001])]
    assert _intersect(segs, rows, tp=1, target_ip="10.0.0.2") == ["10.0.0.2:5001"]


def test_missing_rank_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, 1, [5002])]  # rank 0 never seen
    _expect_err(
        lambda: _intersect(segs, rows, tp=2, target_ip=""),
        "rank 0 segment unresolved",
    )


def test_rank_conflict_fails() -> None:
    segs = ["10.0.0.1:5001", "10.0.0.1:5002"]
    rows = [(100, 0, [5001]), (101, 0, [5002])]
    _expect_err(
        lambda: _intersect(segs, rows, tp=2, target_ip=""),
        "claimed by two segments",
    )


# ---------------------------------------------------------------------------
# Integration: real process tree + socket + pidfile + HTTP admin
# ---------------------------------------------------------------------------

_LISTENER = (
    "import socket,sys,time;"
    "s=socket.socket();s.bind(('127.0.0.1',0));s.listen(1);"
    "print(s.getsockname()[1],flush=True);time.sleep(120)"
)


class _Admin(http.server.BaseHTTPRequestHandler):
    body = ""

    def do_GET(self):  # noqa: N802
        if self.path == "/get_all_segments":
            data = self.body.encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):  # silence
        pass


def _start_admin(body: str) -> http.server.HTTPServer:
    _Admin.body = body
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Admin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_end_to_end_tp1() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", _LISTENER], stdout=subprocess.PIPE, text=True
    )
    try:
        port = int(proc.stdout.readline().strip())
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(str(proc.pid))
            pidfile = f.name
        srv = _start_admin(f"127.0.0.1:{port}\n127.0.0.1:1\n")  # noise seg too
        try:
            rows = probe_local(pidfile)
            assert any(p == proc.pid and port in ports for p, _, ports in rows), rows
            assert rows[0][1] is None  # no VLLM::Worker_TP proctitle here

            targets, info = resolve_segments(
                tp=1,
                admin=f"http://127.0.0.1:{srv.server_port}",
                pidfile=pidfile,
            )
            assert targets == [f"127.0.0.1:{port}"], targets
            assert info["rank_seg"] == {0: f"127.0.0.1:{port}"}
        finally:
            srv.shutdown()
            os.unlink(pidfile)
    finally:
        proc.kill()
        proc.wait()


def test_stale_pidfile_fails() -> None:
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("999999")  # almost certainly dead
        pidfile = f.name
    srv = _start_admin("127.0.0.1:5001\n")
    try:
        _expect_err(
            lambda: resolve_segments(
                tp=1, admin=f"http://127.0.0.1:{srv.server_port}", pidfile=pidfile
            ),
            "stale",
        )
    finally:
        srv.shutdown()
        os.unlink(pidfile)


def test_missing_pidfile_fails() -> None:
    srv = _start_admin("127.0.0.1:5001\n")
    try:
        _expect_err(
            lambda: resolve_segments(
                tp=1,
                admin=f"http://127.0.0.1:{srv.server_port}",
                pidfile="/nonexistent/worker_B.pid",
            ),
            "missing/empty",
        )
    finally:
        srv.shutdown()


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
]

if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"test_resolve: PASS ({len(TESTS)} cases)")
