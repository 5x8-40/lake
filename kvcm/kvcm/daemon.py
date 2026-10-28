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

优雅退出：停收 intent → 取消在途协程 → 有界等待协程（5 秒）→
KeepaliveLoop 停 → **等 RPC 线程结束** → StoreClient.close() + ledger.close()。

最后一步有前提：RPC 线程等满仍未结束（RPC 卡死）时，**client 和 ledger
都不关**——worker 是非 daemon 线程，解释器退出会 join 它们，进程本来就
要靠 SIGKILL；此时拆资源只会多制造一次并发崩溃。

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
import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

from kvcm.api.grpc_server import IntentGrpcServer
from kvcm.backends.mooncake.backend import MooncakeBackend
from kvcm.backends.mooncake.capability import mooncake_main, mooncake_v0311
from kvcm.backends.mooncake.client import StoreClient
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.ledger import Ledger
from kvcm.loops import KeepaliveLoop
from kvcm.task_engine import TaskEngine


def build_backend(
    args: argparse.Namespace,
    client: StoreClient,
    ledger: Ledger,
    executor: ThreadPoolExecutor | None = None,
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
        rpc_timeout_s=args.rpc_timeout_s,
        executor=executor,
    )


def _wait_executor(executor: ThreadPoolExecutor, timeout_s: float) -> bool:
    """有界等线程池全部 worker 结束；**返回是否真的等完了**。

    为什么要等：`run_in_executor` 取消时不等线程函数结束——协程结束了，
    mooncake 绑定可能还在跑。关 client 前必须等线程。

    **超时意味着什么**（必须说清，别写成「线程随进程消失」）：
    `ThreadPoolExecutor` 的 worker 是**非 daemon** 线程（CPython 硬编码），
    解释器退出时会 `join` 它们。所以 RPC 卡死时：
      - 本函数超时返回 False，但进程仍然退不出去（挂在解释器退出）
      - 只能靠外部 SIGKILL
    调用方据此**跳过 client.close()**：反正要强杀，不必再去拆一个正被
    使用的 client（那只会多制造一次崩溃）。
    """
    import threading

    done = threading.Event()

    def _shutdown() -> None:
        try:
            executor.shutdown(wait=True)
        finally:
            done.set()

    threading.Thread(target=_shutdown, name="kvcm-rpc-wait", daemon=True).start()
    return done.wait(timeout=timeout_s)


async def _serve(args: argparse.Namespace, stop: asyncio.Event) -> int:
    """事件循环内的主流程。"""
    ledger = Ledger(args.ledger)
    client = StoreClient()
    # RPC 池在 connect 之前建：connect 也是同步阻塞调用，用它才符合
    # 「不使用默认池」的定稿（connect 借一个线程、用完即还；启动期没有
    # 其他任务争用）。池大小 = max(4, min(8, cpu))，与 backend 内部默认一致。
    executor = ThreadPoolExecutor(
        max_workers=max(4, min(8, (os.cpu_count() or 4))),
        thread_name_prefix="kvcm-rpc",
    )
    keepalive: KeepaliveLoop | None = None
    engine: TaskEngine | None = None
    backend: MooncakeBackend | None = None
    grpc_server: IntentGrpcServer | None = None
    try:
        loop = asyncio.get_running_loop()
        # connect / rebuild 都是同步阻塞调用（connect 含 TCP 握手+注册，
        # rebuild 内含 Ledger 的 submit().result()）——不能占事件循环线程，
        # 否则启动期信号处理都要等它们返回
        await loop.run_in_executor(
            executor,
            lambda: client.connect(
                local_hostname=args.local_hostname,
                master_server_address=args.master,
                protocol=args.protocol,
                ascend_device=args.device,
            ),
        )
        backend = build_backend(args, client, ledger, executor=executor)
        keepalive = KeepaliveLoop(
            backend,
            ledger,
            interval_s=args.keepalive_interval_s,
            window_s=args.keepalive_window_s,
            executor=backend.executor,
        )
        engine = TaskEngine(backend, keepalive=keepalive, max_concurrent=args.max_concurrent)
        # 2. 重启恢复：running 任务重建跟踪，收编进 TaskEngine
        #    （rebuild 内部有 Ledger 同步阻塞调用，同样走 executor）
        restored = await loop.run_in_executor(backend.executor, backend.rebuild)
        for handle in restored:
            engine.track_in_background(handle)
        # 3. 常驻组件启动（协程）
        keepalive.start()
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
        # ④ Loop 停（可能还有一轮 sweep 停在 exists 里）
        await keepalive.stop()
        return 0
    finally:
        # ⑤ 收尾顺序：**先等 RPC 线程结束，再关 client**（反了就会 close 与
        #    在途 RPC 并发——demo 踩过的退出期崩溃正是这个形状）。
        #
        #    executor 是本进程建的（connect 时就在），所以由这里负责等：
        #    `shutdown(wait=True)` 会 join 全部 worker；它没有超时参数，用
        #    「另起线程 + Event.wait(有界)」包住。
        #
        #    不把这个等待放进 backend.close()：池是注入的，backend 不拥有它
        #    （`_owns_executor` 为假时会直接 return，等待会被静默跳过）。
        drained = _wait_executor(executor, args.shutdown_timeout_s)
        if backend is not None:
            backend.close()
        if keepalive is not None:
            keepalive.close()
        if drained:
            # 安全：所有 RPC 线程已结束，可以拆 client、收 sqlite
            client.close()
            ledger.close()
        else:
            # 有线程卡在 mooncake 绑定或 Ledger 的 submit().result() 里：
            # 进程退不出去（worker 非 daemon，解释器退出会 join），只能
            # 靠外部 SIGKILL。此时**什么都不拆**：
            #   - 拆 client 会与在途 RPC 并发崩溃（demo 踩过的形状）
            #   - 关 ledger 的 shutdown(wait=True) 无上界，会再撑开超时
            print(
                f"[kvcm] shutdown: worker threads still running after "
                f"{args.shutdown_timeout_s}s — skipping client/ledger close "
                f"(process will not exit until they return; expect SIGKILL)",
                file=sys.stderr,
            )


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
        "--rpc-timeout-s",
        type=float,
        default=30.0,
        help="协程侧 RPC 超时秒数（0.3.11 绑定无超时配置；<=0 关闭）",
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
