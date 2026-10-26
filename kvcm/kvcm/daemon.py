"""kvcm 进程壳（E11）：常驻服务入口。

启动序列：
1. 参数解析 → Ledger → StoreClient.connect → MooncakeBackend 组装
2. `backend.rebuild()`：重启恢复 running 任务的跟踪登记（D-4）
3. 恢复轮询线程：把 rebuild 恢复的 handle 跑到终态（极简过渡——
   单线程顺序轮询，阶段 2 由 TaskEngine 收编）
4. KeepaliveLoop 启动（固定窗口占位策略）
5. 主线程等 SIGTERM/SIGINT

优雅退出四步（E11 决策四）：停收入口（本期无对外入口，占位注释即
TaskEngine 接入点）→ 在途任务 abandon（Ledger 记 abandoned，不等待、
不阻塞退出——后端无任务取消能力，副本清理归 remove）→ Loop 停 →
StoreClient.close()（demo 已知退出期析构竞态，显式 close 是既有结论）。

locate 本期静态注入（`--segs`，单实例冒烟形态）；生产 locate =
L1 InstanceWatcher 注册，归 E13。

用法：
    python -m kvcm.daemon --master 127.0.0.1:50088 \
        --local-hostname 127.0.0.1:13021 \
        --model-name qwen --tp-size 2 --segs seg0,seg1 \
        --ledger /var/lib/kvcm/ledger.sqlite
"""

from __future__ import annotations

import argparse
import signal
import threading
import time

from kvcm.backends.mooncake.backend import MooncakeBackend
from kvcm.backends.mooncake.capability import mooncake_main, mooncake_v0311
from kvcm.backends.mooncake.client import StoreClient
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.ledger import Ledger
from kvcm.loops import KeepaliveLoop
from kvcm.ops.base import QueryError, TaskHandle, TaskStatus, VerifyError

# 恢复轮询参数（极简过渡版，阶段 2 归 TaskEngine 统一）
_RECOVER_POLL_S = 1.0
_RECOVER_TIMEOUT_S = 300.0


def build_backend(
    args: argparse.Namespace, client: StoreClient, ledger: Ledger
) -> MooncakeBackend:
    """按参数组装 backend。独立成函数便于测试注入（fake client +
    静态 locate 即可起进程壳）。"""
    keys = KeyComputer(
        model_name=args.model_name,
        block_size=args.block_size,
        tp_size=args.tp_size,
        peer_tp_size=args.peer_tp_size,
        put_step=args.put_step,
        hash_algo=args.hash_algo,
    )
    segs = [s for s in args.segs.split(",") if s]
    capability = mooncake_main() if args.capability == "main" else mooncake_v0311()
    return MooncakeBackend(
        client=client,
        key_computer=keys,
        locate=lambda _inst: list(segs),  # 静态注入；生产 = InstanceWatcher（E13）
        capability=capability,
        ledger=ledger,
    )


def recover_poll(
    backend: MooncakeBackend,
    handles: list[TaskHandle],
    stop: threading.Event,
    *,
    poll_s: float = _RECOVER_POLL_S,
    timeout_s: float = _RECOVER_TIMEOUT_S,
) -> None:
    """把 rebuild 恢复的 handle 顺序跑到终态（极简过渡，阶段 2 归
    TaskEngine）。查询失败（QueryError）跳过本轮；超时 abandon 收口
    （与 PrefetchFlow 同一语义）。"""
    deadline = time.monotonic() + timeout_s
    pending = list(handles)
    while pending and not stop.is_set():
        handle = pending.pop(0)
        try:
            status = backend.query_task(handle)
        except QueryError:
            pending.append(handle)  # 可重试：挪到队尾下轮再查
            time.sleep(poll_s)
            continue
        except VerifyError:
            continue  # 终态（核验失败已记账），不再跟踪
        if status in (TaskStatus.READY, TaskStatus.FAILED):
            continue  # 终态已记账
        if time.monotonic() >= deadline:
            backend.abandon(handle)
            continue
        pending.append(handle)
        time.sleep(poll_s)


def run(args: argparse.Namespace, *, stop: threading.Event | None = None) -> int:
    """进程主流程（stop 注入便于测试不发信号；None = 自己装 signal handler）。"""
    ledger = Ledger(args.ledger)
    client = StoreClient()
    backend: MooncakeBackend | None = None
    loop: KeepaliveLoop | None = None
    recover_thread: threading.Thread | None = None
    in_flight: list[TaskHandle] = []  # 在途 handle（退出时 abandon）
    try:
        client.connect(
            local_hostname=args.local_hostname,
            master_server_address=args.master,
            protocol=args.protocol,
        )
        backend = build_backend(args, client, ledger)
        # 2. 重启恢复：running 任务重建跟踪 + 恢复轮询
        restored = backend.rebuild()
        in_flight.extend(restored)
        if restored:
            recover_stop = threading.Event()
            recover_thread = threading.Thread(
                target=recover_poll,
                args=(backend, restored, recover_stop),
                name="kvcm-recover",
                daemon=True,
            )
            recover_thread.start()
        else:
            recover_stop = threading.Event()
        # 3. KeepaliveLoop（重启恢复免特判：注册行在 Ledger，首扫接管）
        loop = KeepaliveLoop(
            backend,
            ledger,
            interval_s=args.keepalive_interval_s,
            window_s=args.keepalive_window_s,
        )
        loop.start()

        # 4. 等退出信号
        if stop is None:
            stop = threading.Event()

            def _handler(_sig, _frame):
                stop.set()

            signal.signal(signal.SIGTERM, _handler)
            signal.signal(signal.SIGINT, _handler)
        stop.wait()

        # 优雅退出四步（决策四）
        # ① 停收入口：本期无对外入口（TaskEngine/HTTP 占位归阶段 2）
        # ② 在途 abandon（记账不等待；后端无取消能力，副本清理归 remove）
        recover_stop.set()
        for handle in in_flight:
            try:
                backend.abandon(handle)
            except KeyError:
                pass  # 已到终态被移除登记，正常
        # ③ Loop 停
        loop.stop()
        if recover_thread is not None:
            recover_thread.join(timeout=5.0)
        return 0
    finally:
        # ④ client close（有序收尾，不靠退出期 GC/atexit）
        client.close()
        ledger.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kvcm.daemon", description="kvcm 进程壳（E11）")
    p.add_argument("--master", required=True, help="mooncake master 地址 host:port")
    p.add_argument("--local-hostname", required=True, help="本进程 client 身份 host:port")
    p.add_argument("--protocol", default="tcp", help="传输协议（tcp/ascend/rdma…）")
    p.add_argument("--model-name", required=True)
    p.add_argument("--block-size", type=int, default=128)
    p.add_argument("--tp-size", type=int, default=1)
    p.add_argument("--peer-tp-size", type=int, default=None)
    p.add_argument("--put-step", type=int, default=1)
    p.add_argument("--hash-algo", default="sha256")
    p.add_argument(
        "--segs",
        required=True,
        help="静态 locate：rank 有序 seg 列表，逗号分隔（生产 = InstanceWatcher，E13）",
    )
    p.add_argument(
        "--capability",
        choices=["v0311", "main"],
        default="v0311",
        help="后端版本能力集（填错 = 编排选错路径）",
    )
    p.add_argument("--ledger", required=True, help="Ledger sqlite 路径")
    p.add_argument(
        "--keepalive-interval-s",
        type=float,
        default=5.0,
        help="续租周期（必须显著小于 master lease TTL，实配 11s）",
    )
    p.add_argument(
        "--keepalive-window-s",
        type=float,
        default=600.0,
        help="保活固定窗口（D3 占位策略：READY 后续租这么久即停租）",
    )
    return run(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
