"""kvcm 进程壳（E11）：常驻服务入口。

执行形态（code-architecture.md「运行时结构」定稿）：**单事件循环 + 协程**。
阻塞调用（mooncake 同步 RPC、sqlite）交给 executor，等待期间协程挂起。

启动序列（事件循环内）：
1. Ledger → StoreClient.connect → MooncakeBackend 组装
2. `backend.rebuild()`：重启恢复 running 任务的跟踪登记，恢复的 handle
   交 TaskEngine.track 收编（只轮询到终态，不重跑流程）
3. KeepaliveLoop 启动（协程定时器，固定窗口占位策略）；TaskEngine 就绪
   （READY 后经 KeepaliveLoop.register 接线保活）
4. IntentReceiver gRPC 服务（--listen，E12 协议见 proto/）启动
   —— gRPC 用普通版（自己的线程池接请求），servicer 只做「入队即返」，
   不阻塞；任务执行全在事件循环里
5. 等 SIGTERM / SIGINT

优雅退出五步：停收 intent → 取消在途协程 → 有界等待（5 秒）→
KeepaliveLoop 停 → StoreClient.close()。

locate 本期静态注入（`--segs`，单实例冒烟形态）；生产 locate =
L1 InstanceWatcher 注册，归 E13。

用法：
    python -m kvcm.daemon --master 127.0.0.1:50088 \
        --local-hostname 127.0.0.1:13021 \
        --model-name qwen --tp-size 2 --segs seg0,seg1 \
        --ledger /var/lib/kvcm/ledger.sqlite \
        --listen 127.0.0.1:9000
"""

from __future__ import annotations

import argparse
import asyncio
import signal

from kvcm.api.grpc_server import IntentGrpcServer
from kvcm.backends.mooncake.backend import MooncakeBackend
from kvcm.backends.mooncake.capability import mooncake_main, mooncake_v0311
from kvcm.backends.mooncake.client import StoreClient
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.ledger import Ledger
from kvcm.loops import KeepaliveLoop
from kvcm.task_engine import TaskEngine


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


async def _serve(args: argparse.Namespace, stop: asyncio.Event) -> int:
    """事件循环内的主流程。"""
    ledger = Ledger(args.ledger)
    client = StoreClient()
    loop: KeepaliveLoop | None = None
    engine: TaskEngine | None = None
    backend: MooncakeBackend | None = None
    grpc_server: IntentGrpcServer | None = None
    try:
        client.connect(
            local_hostname=args.local_hostname,
            master_server_address=args.master,
            protocol=args.protocol,
            ascend_device=args.device,
        )
        backend = build_backend(args, client, ledger)
        loop = KeepaliveLoop(
            backend,
            ledger,
            interval_s=args.keepalive_interval_s,
            window_s=args.keepalive_window_s,
        )
        engine = TaskEngine(backend, keepalive=loop, max_concurrent=args.max_concurrent)
        # 2. 重启恢复：running 任务重建跟踪，收编进 TaskEngine
        for handle in backend.rebuild():
            asyncio.create_task(engine.track(handle), name="kvcm-recover")
        # 3. 常驻组件启动（协程）
        loop.start()
        engine.start()
        if args.listen:
            host, _, port = args.listen.rpartition(":")
            grpc_server = IntentGrpcServer((host or "127.0.0.1", int(port)), engine)
            grpc_server.start()

        # 4. 等退出信号
        await stop.wait()

        # 优雅退出（五步）
        # ① 停收入口
        if grpc_server is not None:
            grpc_server.stop()
        # ② 取消在途协程 + ③ 有界等待（TaskEngine.shutdown 内部做）
        await engine.shutdown(timeout_s=args.shutdown_timeout_s)
        # ④ Loop 停
        await loop.stop()
        return 0
    finally:
        # ⑤ client close（有序收尾，不靠退出期 GC/atexit）
        client.close()
        if backend is not None:
            backend.close()
        if loop is not None:
            loop.close()
        ledger.close()


def run(args: argparse.Namespace, *, stop: asyncio.Event | None = None) -> int:
    """进程主流程（stop 注入便于测试；None = 自己装 signal handler）。"""

    async def _main() -> int:
        ev = stop or asyncio.Event()
        if stop is None:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, ev.set)
                except NotImplementedError:
                    # 非 POSIX 主线程：退化为 signal.signal（测试/CI 通常
                    # 走注入的 stop，不进这里）
                    signal.signal(sig, lambda *_: ev.set())
        return await _serve(args, ev)

    return asyncio.run(_main())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kvcm.daemon", description="kvcm 进程壳（E11）")
    p.add_argument("--master", required=True, help="mooncake master 地址 host:port")
    p.add_argument("--local-hostname", required=True, help="本进程 client 身份 host:port")
    p.add_argument("--protocol", default="tcp", help="传输协议（tcp/ascend/rdma…）")
    p.add_argument(
        "--device",
        type=int,
        default=0,
        help="ascend 协议的 torch.npu.set_device 卡号（NPU 版绑定前置仪式；tcp 忽略）",
    )
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
        "--max-concurrent",
        type=int,
        default=4,
        help="传输闸名额数（同时在后端传输的任务数）",
    )
    p.add_argument(
        "--listen",
        default="127.0.0.1:9000",
        help="IntentReceiver gRPC 监听 host:port（空 = 不启用）",
    )
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
    p.add_argument(
        "--shutdown-timeout-s",
        type=float,
        default=5.0,
        help="优雅退出时等待在途协程的上限（等满不再等）",
    )
    return run(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
