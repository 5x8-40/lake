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

最后一步有前提：RPC 线程等满仍未结束（RPC 卡死）时，**立即 `os._exit(1)`**，
不走任何清理。

- 不能只「跳过 close 再返回」：`client` 是局部变量，函数返回会丢掉 Python
  包装的最后一份引用，`~RealClient` 立刻 `tearDownAll_internal()`——此时
  RPC 线程还在绑定里，是 demo 实测过的 teardown 竞态（abort RC=134 / 挂住）
- 也不能主动 `client.close()`：那等于在在途 RPC 上 teardown
- `os._exit` 跳过 GC / `__del__` / atexit：已提交的 ledger 行还在（正在写的
  那条丢掉，靠 rebuild 重建 running 任务）；mooncake 侧靠 client TTL 回收

locate 读进程里的一份实例目录，不读命令。SubmitIntent 只有
prefix{model_id, token_ids} 和 target 实例名；copy / move / remove
也不带段。``--instance-segs`` 只在进程拉起时做初始登记
（``实例=seg0,seg1``，可重复），不是每次操作换一套。进程跑着时用
``register`` 加实例。``locate(实例)`` 只返回该实例的段；没登记就报错。
实例下线不在这里。

用法：
    python -m kvcm.daemon --master 127.0.0.1:50088 \
        --local-hostname 127.0.0.1:13021 \
        --model-name qwen --tp-size 2 \
        --instance-segs inst-a=seg0,seg1 \
        --instance-segs inst-b=seg2,seg3 \
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
from kvcm.backends.mooncake.admin_main import MainlineAdmin
from kvcm.backends.mooncake.backend import MooncakeBackend
from kvcm.backends.mooncake.capability import mooncake_main, mooncake_v0311
from kvcm.backends.mooncake.client import StoreClient
from kvcm.engines.vllm.keys import KeyComputer
from kvcm.instances import directory_from_specs
from kvcm.ledger import Ledger
from kvcm.loops import KeepaliveLoop
from kvcm.task_engine import TaskEngine


def build_backend(
    args: argparse.Namespace,
    client: StoreClient,
    ledger: Ledger,
    executor: ThreadPoolExecutor | None = None,
) -> MooncakeBackend:
    """按参数组装 backend。独立成函数便于测试注入（fake client 即可起
    进程壳）。实例目录在这里装一次，locate 走这份目录。"""
    keys = KeyComputer(
        model_name=args.model_name,
        block_size=args.block_size,
        tp_size=args.tp_size,
        peer_tp_size=args.peer_tp_size,
        put_step=args.put_step,
        hash_algo=args.hash_algo,
    )
    directory = directory_from_specs(args.instance_segs)
    capability = mooncake_main() if args.capability == "main" else mooncake_v0311()
    admin_url = getattr(args, "admin", "") or ""
    admin = MainlineAdmin(admin_url) if admin_url else None
    return MooncakeBackend(
        client=client,
        key_computer=keys,
        locate=directory.locate,
        capability=capability,
        admin=admin,
        ledger=ledger,
        submit_concurrency=args.copy_submit_concurrency,
        rpc_timeout_s=args.rpc_timeout_s,
        executor=executor,
    )


def _wait_executor(executor: ThreadPoolExecutor, timeout_s: float) -> bool:
    """有界等线程池全部 worker 结束；**返回是否真的等完了**。

    为什么要等：`run_in_executor` 取消时不等线程函数结束——协程结束了，
    mooncake 绑定可能还在跑。关 client 前必须等线程。

    **超时（返回 False）意味着什么**：还有调用停在 mooncake 绑定里。
    调用方**必须立即 `os._exit(1)`**，不能只跳过 close 然后返回——
    返回会丢掉 Python 包装对 client 的引用，`~RealClient` 随即在在途
    RPC 上 teardown（demo 实测的 abort/挂住形状）。
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


def _hard_exit() -> None:
    """进程立即消失，不跑 finally / GC / __del__ / atexit。

    用于「RPC 卡住、teardown 会与在途线程冲突」的退出分支。
    单独成函数是为了测试能替换它（测试进程不能被真的杀掉）。
    """
    os._exit(1)


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
    engine_shutdown_done = False
    backend: MooncakeBackend | None = None
    grpc_server: IntentGrpcServer | None = None
    try:
        loop = asyncio.get_running_loop()
        # connect / rebuild 都是同步阻塞调用（connect 含 TCP 握手+注册，
        # rebuild 内含 Ledger 的 submit().result()）——不能占事件循环线程，
        # 否则启动期信号处理都要等它们返回。
        #
        # 必须带超时：0.3.11 的绑定没有连接超时，master 不可达时可能挂很久。
        # 卡在这里时 `finally` 进不去（await 不返回），整个退出逻辑都不会跑。
        # 退出信号只在启动期等一次，connect 与 rebuild 共用同一个任务。
        stop_task = asyncio.ensure_future(stop.wait())
        try:
            # 等待 connect 的同时也监听退出信号：SIGTERM 打在 connect 期间时
            # 不该硬等满 connect_timeout_s（默认 30 秒）。
            connect_task = asyncio.ensure_future(
                loop.run_in_executor(
                    executor,
                    lambda: client.connect(
                        local_hostname=args.local_hostname,
                        master_server_address=args.master,
                        protocol=args.protocol,
                        ascend_device=args.device,
                    ),
                )
            )
            done, _ = await asyncio.wait(
                {connect_task, stop_task},
                timeout=args.connect_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                # 启动期收到退出信号：connect 可能还在跑，做与退出超时相同的
                # 处理（不 close、等线程会卡住 → 直接消失）
                print(
                    "[kvcm] startup: got shutdown signal during connect — "
                    "exiting immediately",
                    file=sys.stderr,
                )
                sys.stderr.flush()
                _hard_exit()
                return 1
            if connect_task not in done:
                raise asyncio.TimeoutError
            # connect 完成：让异常按原路径传播
            connect_task.result()
        except (asyncio.TimeoutError, TimeoutError) as e:
            # 连接线程还在跑（取消不会中断 C++ 调用）——与退出超时同一处理：
            # 立即 `os._exit(1)`，不跑析构、不走 atexit。
            print(
                f"[kvcm] startup: connect did not return in "
                f"{args.connect_timeout_s}s — exiting immediately",
                file=sys.stderr,
            )
            sys.stderr.flush()
            _hard_exit()
            return 1
        except Exception as e:
            print(f"[kvcm] startup: connect failed: {e}", file=sys.stderr)
            raise
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
        #    和 connect 一样带超时并监听信号：裸 await 会让 sqlite 卡住时
        #    finally 进不去，退出逻辑整个不跑。
        rebuild_task = asyncio.ensure_future(
            loop.run_in_executor(executor, backend.rebuild)
        )
        done, _ = await asyncio.wait(
            {rebuild_task, stop_task},
            timeout=args.connect_timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            print(
                "[kvcm] startup: got shutdown signal during rebuild — "
                "exiting immediately",
                file=sys.stderr,
            )
            sys.stderr.flush()
            _hard_exit()
            return 1
        if rebuild_task not in done:
            print(
                f"[kvcm] startup: rebuild did not return in "
                f"{args.connect_timeout_s}s — exiting immediately",
                file=sys.stderr,
            )
            sys.stderr.flush()
            _hard_exit()
            return 1
        restored = rebuild_task.result()
        for handle in restored:
            engine.track_in_background(handle)
        # 清一次超龄终态行（tasks 表只增不删）。放启动期：不加周期逻辑，
        # 而 kvcm 是 daemon，重启是常态。
        gone = await loop.run_in_executor(executor, ledger.gc_tasks)
        if gone:
            print(f"[kvcm] startup: gc removed {gone} stale task rows", file=sys.stderr)
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
        engine_shutdown_done = True
        # ④ Loop 停（可能还有一轮 sweep 停在 exists 里）。
        #    超时用同一个旋钮：`stop()` 自己的默认是 10 秒，比
        #    `--shutdown-timeout-s` 长，会让总退出时间翻倍。
        await keepalive.stop(timeout_s=args.shutdown_timeout_s)
        return 0
    finally:
        # ⑥ 兜底取消协程：正常路径已在上面 `engine.shutdown` 过（幂等，
        #    列表已清空）。这条兜的是**启动中途失败**——`track_in_background`
        #    在 `rebuild` 之后就起了恢复轮询，若之后 `gc_tasks`、`keepalive.start`
        #    或 gRPC 绑定抛错，正常路径根本没走到，协程会一直挂着到
        #    `asyncio.run` 收尾；那时 client 已经关了，`abandon` 会撞上去。
        if engine is not None and not engine_shutdown_done:
            await engine.shutdown(timeout_s=args.shutdown_timeout_s)
            engine_shutdown_done = True
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
        if keepalive is not None:
            keepalive.close()
        if not drained:
            # 有线程卡在 mooncake 绑定或 Ledger 的 submit().result() 里。
            #
            # **为什么这里不调 client.close()**：
            # 「必须 close」这条经验来自 demo，指的是「能正常收尾时」——
            # 不 close 会让清理推迟到退出期的 GC/atexit，而那条路径会与
            # 还在跑的传输线程撞车（demo 实测：abort RC=134 或挂住，
            # 上游 #3909/#3943）。也就是说 close 的作用是**避开 atexit**，
            # 不是「不 close 就漏了资源」。
            #
            # 但在本分支里，close 的前提不成立：RPC 还停在绑定里，
            # 无论主动 close 还是让它走 GC，都是在在途调用上做 teardown
            # ——正是那个会崩的形状。
            #
            # **为什么 os._exit 反而安全**：它连 atexit 都不跑，进程直接
            # 消失，没有任何 teardown 发生在在途线程上。三种选择里只有它
            # 不会崩：
            #   - 跳过 close 后正常返回 → ~RealClient 在在途 RPC 上 teardown
            #     （发生在 Py_FinalizeEx 之前，不经过 std::atexit）
            #   - 主动 client.close()      → 同一形状
            #   - os._exit(1)              → 什么都不跑，不崩
            #
            # 代价（都可接受）：
            #   - kvcm 是主侧身份（global_segment_size=0，不挂 seg、不持存储），
            #     名下没有副本会因它消失而丢；master 靠 client TTL 回收这个 UUID
            #   - 已提交的 ledger 行还在，正在写的那条丢掉；重启后 rebuild
            #     从 status='running' 重建跟踪
            print(
                f"[kvcm] shutdown: worker threads still running after "
                f"{args.shutdown_timeout_s}s — exiting immediately "
                f"(skip teardown to avoid the known race)",
                file=sys.stderr,
            )
            sys.stderr.flush()
            _hard_exit()
            return 1  # 走到这里说明 _hard_exit 被替换（测试），按失败返回
        # 安全：所有 RPC 线程已结束，可以拆 client、收 sqlite。
        # `backend.close()` 也放这里（不是硬退出分支）——今天对注入的池是
        # 空操作，但万一以后让它去碰 store，超时路径会在 os._exit 之前拆
        # client，正是要避免的形状。
        # backend.close() 对**注入的**池无事可等（daemon 自建并注入，
        # `_owns_executor` 为假），排空由前面的 `_wait_executor` 负责。
        # 这里只调用、不看返回值——那支 `if` 在生产路径永远进不去。
        if backend is not None:
            backend.close()
        client.close()
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
        "--admin",
        default="",
        help="master HTTP 管理面根 URL。副本核验和降层查盘走 GET /batch_query_keys",
    )
    p.add_argument(
        "--instance-segs",
        action="append",
        required=True,
        dest="instance_segs",
        metavar="INSTANCE=SEG0,SEG1",
        help="进程拉起时的初始登记：实例=rank 有序 seg 列表，可重复。"
        "不是 SubmitIntent / copy / move / remove 的参数；"
        "locate 只返回这一实例的段，没登记就报错",
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
        help="传输闸名额（同时没做完的预热前缀数，不按 key 计）",
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
        "--connect-timeout-s",
        type=float,
        default=30.0,
        help="启动时 connect master 的超时秒数（0.3.11 绑定无连接超时）",
    )
    p.add_argument(
        "--copy-submit-concurrency",
        type=int,
        default=4,
        help="进程内同时在飞的 create_copy_task 上限（所有 copy 共用；0.3.11 实测窗口 4 全成）",
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
