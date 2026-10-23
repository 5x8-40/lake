"""StoreClient：mooncake 存储客户端（双通道）。

- 控制类操作走 Python 绑定（store_py）：copy / exist / remove / 任务查询
- 查询/观测走 HTTP 管理端点（AdminClient，见 topology.py 的 seg 名单与
  后续零副作用核验）

在 master 眼里本进程必然是一个 client：绑定 setup 即注册 UUID 并起心
跳——任务校验「谁发起谁完结」、副本绑段所在 client 的存活、下线清理
都按这个身份走。是否持有存储由 global_segment_size 决定（控制面 = 0，
纯控制身份）。

引擎不发起、也不协调复制：copy 由本进程发起、master 协调；但数据面传
输的执行者是源/目标 seg 所在 client——源侧通常就是源引擎进程内的
store client（它必须在线，否则 master 派不了源任务）。
"""

from __future__ import annotations

import time
from typing import Iterable, Sequence

# mooncake.store 为 lazy import（connect 时）：无 mooncake 安装的环境
# 仍可 import 本模块（离线 key 计算、拓扑解析、单元测试）。


def entry_exists(e) -> bool:
    """batch_is_exist 返回项的_truthy 判定——唯一的判定定义，所有
    「key 是否在池」的调用共用。"""
    return e == 1 or e is True or (isinstance(e, int) and e > 0)


class StoreClientError(RuntimeError):
    """store 绑定调用失败（rc/err 非零、任务终态非成功）。"""


class StoreClient:
    """mooncake store 绑定的生命周期与方法封装。"""

    def __init__(self) -> None:
        self._store = None

    def connect(
        self,
        *,
        local_hostname: str,
        master_server_address: str,
        metadata_server: str = "P2PHANDSHAKE",
        global_segment_size: int = 0,
        local_buffer_size: int = 32 * 1024 * 1024,
        protocol: str = "tcp",
        device_name: str = "",
    ) -> None:
        """建立 client（注册 UUID + 起心跳）。global_segment_size=0 = 纯控
        制面身份（不挂载 seg、不持有存储）。"""
        if self._store is not None:
            raise StoreClientError("already connected")
        from mooncake.store import MooncakeDistributedStore

        store = MooncakeDistributedStore()
        rc = store.setup(
            local_hostname,
            metadata_server,
            global_segment_size,
            local_buffer_size,
            protocol,
            device_name,
            master_server_address,
        )
        if rc != 0:
            # setup 失败的对象仍可能持有已初始化的内部资源（注册线程等），
            # 显式 close 再抛，不靠 GC 碰运气。
            try:
                store.close()
            except Exception:
                pass
            raise StoreClientError(
                f"store setup failed rc={rc} "
                f"host={local_hostname} master={master_server_address}"
            )
        self._store = store

    def close(self) -> None:
        """有序收尾。不 close 则清理由退出期 GC/atexit 承担——旧版本上
        会与在途 RPC/传输线程竞态（间歇 abort/挂起）。"""
        if self._store is not None:
            self._store.close()
            self._store = None

    def _s(self):
        if self._store is None:
            raise StoreClientError("not connected")
        return self._store

    # ------------------------------------------------------------------
    # 存在性
    # ------------------------------------------------------------------

    def is_exist(self, keys: Sequence[str]) -> list[bool]:
        """batch_is_exist → bool 列表（读即续租——保活机制利用这一点）。"""
        return [entry_exists(e) for e in self._s().batch_is_exist(list(keys))]

    # ------------------------------------------------------------------
    # 复制任务
    # ------------------------------------------------------------------

    def create_copy_task(self, key: str, targets: Sequence[str]) -> object:
        """发起复制：为 key 在目标 seg 各建一个副本；返回任务 id。

        master 只校验 key 存在 + 目标 seg 已挂载且可分配——不查目标已有
        副本、不查在途任务（重复发起各拿任务 id，执行时先完成者赢、后者
        按幂等规则失败）。源由 master 选择，源 seg 所在 client 必须在线。
        """
        task_id, err = self._s().create_copy_task(key, list(targets))
        if err != 0:
            raise StoreClientError(
                f"create_copy_task failed err={err} key={key!r} targets={list(targets)}"
            )
        return task_id

    def query_task(self, task_id: object) -> object:
        """查询任务状态（QueryTaskResponse）。"""
        resp, err = self._s().query_task(task_id)
        if err != 0:
            raise StoreClientError(f"query_task failed err={err} task_id={task_id}")
        return resp

    # ------------------------------------------------------------------
    # 同步便利封装（demo 脚本用；MooncakeBackend 的异步 query_task 路径
    # 不走这里——以下方法当前无 L3/L2 调用方，删除前先确认 demo 不再用）
    # ------------------------------------------------------------------

    def wait_task(self, task_id: object, *, poll_s: float = 0.5, timeout_s: float = 120.0) -> object:
        """轮询到终态（SUCCESS/FAILED）或超时。"""
        from mooncake.store import TaskStatus

        deadline = time.monotonic() + timeout_s
        while True:
            resp = self.query_task(task_id)
            if resp.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
                return resp
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"task {task_id} not terminal after {timeout_s}s; last={resp}"
                )
            time.sleep(poll_s)

    def copy_and_wait(self, key: str, targets: Sequence[str], *, timeout_s: float = 120.0) -> None:
        """同步组合：发起 + 等到成功（失败/超时抛错）。同步调用方用。"""
        from mooncake.store import TaskStatus

        task_id = self.create_copy_task(key, targets)
        resp = self.wait_task(task_id, timeout_s=timeout_s)
        if resp.status != TaskStatus.SUCCESS:
            raise StoreClientError(
                f"copy task finished non-success: status={resp.status} "
                f"key={key!r} targets={list(targets)} resp={resp}"
            )

    # ------------------------------------------------------------------
    # 副本核验（绑定通道；注意：本通道有续租副作用。
    # 零副作用核验走 HTTP 管理端点，仅新版本有——随版本接线）
    #
    # 调用方现状：MooncakeBackend 只用 batch 版；单 key 版与
    # assert_targets_have_replicas 为 demo 脚本遗留的便利封装，L3/L2
    # 当前无调用方。
    # ------------------------------------------------------------------

    def memory_replica_endpoints(self, key: str) -> list[str]:
        """单个 key 的内存副本传输端点列表（demo 脚本用；L3 走 batch 版）。"""
        endpoints: list[str] = []
        for desc in self._s().get_replica_desc(key):
            if not desc.is_memory_replica:
                continue
            mem = desc.get_memory_descriptor()
            endpoints.append(mem.buffer_descriptor.transport_endpoint)
        return endpoints

    def batch_memory_replica_endpoints(self, keys: Sequence[str]) -> dict[str, list[str]]:
        """key → 内存副本端点映射（batch_get_replica_desc）。"""
        out: dict[str, list[str]] = {k: [] for k in keys}
        descs = self._s().batch_get_replica_desc(list(keys))
        items = descs.items() if isinstance(descs, dict) else zip(keys, descs)
        for key, replicas in items:
            eps: list[str] = []
            for desc in replicas or []:
                if getattr(desc, "is_memory_replica", False):
                    mem = desc.get_memory_descriptor()
                    eps.append(mem.buffer_descriptor.transport_endpoint)
            out[str(key)] = eps
        return out

    def assert_targets_have_replicas(
        self, keys: Iterable[str], targets: Sequence[str]
    ) -> dict[str, list[str]]:
        """核验：每个 key 在每个目标 seg 上都有内存副本，否则抛错。

        demo 脚本用；L3（MooncakeBackend.query_task 的 READY 核验）按
        「每个 key 在其映射目标上有副本」逐 key 判定，不走这里——本方法
        的语义是「每个 key 在每个目标上都有」，对 rank 映射计划过强。
        """
        key_list = list(keys)
        placement = self.batch_memory_replica_endpoints(key_list)
        missing: list[str] = []
        for key in key_list:
            have = set(placement.get(key, []))
            for t in targets:
                if t not in have:
                    missing.append(f"{key} missing on {t} (have={sorted(have)})")
        if missing:
            raise StoreClientError("verify failed:\n  " + "\n  ".join(missing))
        return placement
