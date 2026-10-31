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
    「key 是否在池」的调用共用（错误项 -1 不到这里，is_exist 先抛）。"""
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
        ascend_device: int = 0,
    ) -> None:
        """建立 client（注册 UUID + 起心跳）。global_segment_size=0 = 纯控
        制面身份（不挂载 seg、不持有存储）。

        protocol="ascend" 的前置仪式（真机冒烟实证）：NPU 版绑定无论
        protocol 都安装 AscendDirectTransport，而它需要 ACL context
        （不建则 setup 报 ctx NULL / rc=-600）——与 precopy 同对策，
        setup 前 `torch.npu.set_device(ascend_device)`。torch/torch_npu
        只在本分支 lazy import（重依赖，离线/纯 tcp 路径不背）。"""
        if self._store is not None:
            raise StoreClientError("already connected")
        if protocol == "ascend":
            import torch
            import torch_npu  # noqa: F401

            torch.npu.set_device(ascend_device)
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

    def binding(self):
        """已建好的官方 wheel store 对象。软钉用它，不再另建 client。"""
        return self._s()

    # ------------------------------------------------------------------
    # 存在性
    # ------------------------------------------------------------------

    def is_exist(self, keys: Sequence[str]) -> list[bool]:
        """batch_is_exist → bool 列表（读即续租——保活机制利用这一点）。

        单 key 查询失败（返回 -1）不吞成「不存在」——抛
        StoreClientError，由调用方按可重试错误处理（吞掉会让
        「查询失败」被当成「缓存不存在」，误触发重算/预热）。
        """
        keys = list(keys)
        raw = self._s().batch_is_exist(keys)
        failed = [k for k, e in zip(keys, raw) if e == -1]
        if failed:
            raise StoreClientError(
                f"batch_is_exist per-key query failed ({len(failed)}/{len(keys)}): "
                f"{failed[:4]}{'...' if len(failed) > 4 else ''}"
            )
        return [entry_exists(e) for e in raw]

    # ------------------------------------------------------------------
    # 复制任务
    # ------------------------------------------------------------------

    def create_copy_task(self, key: str, targets: Sequence[str]) -> object:
        """发起复制：为 key 在目标 seg 各建一个副本；返回任务 id。

        master 只校验 key 存在 + 目标 seg 已挂载且可分配。重复发起的
        两种归宿（两版本同）：
        - 目标已有 COMPLETE 副本：CopyStart 跳过已占 seg（0.3.11
          `master_service.cpp:1899` / main `:6497`），targets 全占则
          返回空列表 → client 侧 `replica_copy_skipped` 直接成功
          （0.3.11 `client_service.cpp:2604`）——**完成重复 = 幂等成功**；
        - 同 key 有任务在途：CopyStart 报 `OBJECT_HAS_REPLICATION_TASK`
          （0.3.11 `master_service.cpp:1875` / main `:6488`）——
          **在途重复 = 任务失败**，query_task 的 message 带该错误码名
          （`types.cpp:32`）。按 D1 §4 此情形是「别人在干同一件事」，
          不当真失败（MooncakeBackend.query_task 已按此处理）。
        源由 master 选择，源 seg 所在 client 必须在线。
        """
        task_id, err = self._s().create_copy_task(key, list(targets))
        if err != 0:
            raise StoreClientError(
                f"create_copy_task failed err={err} key={key!r} targets={list(targets)}"
            )
        return task_id

    def create_move_task(self, key: str, source: str, target: str) -> object:
        """发起移动：把 key 从源 seg 挪到目标 seg，完成后源副本删除。返回任务 id。

        绑定 `create_move_task(key, source, target) -> (UUID, err)`。一次一个
        key、一个源段、一个目标段——没有 targets 列表，也没有「省略 source
        让 master 挑源」的重载（那是 copy 的行为）。
        """
        task_id, err = self._s().create_move_task(key, source, target)
        if err != 0:
            raise StoreClientError(
                f"create_move_task failed err={err} key={key!r} "
                f"source={source!r} target={target!r}"
            )
        return task_id

    def batch_remove(self, keys: Sequence[str], force: bool = False) -> list[int]:
        """按 key 批量删除，每个 key 的全部副本一起删（全局生效）。

        绑定 `batch_remove(keys, force=False) -> list[int]`：0 成功，负数是
        错误码。长度不符或任一非 0 都当失败抛出，不把「删了一半」报成成功。
        force 默认 false：lease 未过期或复制任务在途时 master 拒删。
        删除不占新内存，调用方不要为此做容量判断。
        """
        keys = list(keys)
        raw = list(self._s().batch_remove(keys, force))
        if len(raw) != len(keys):
            raise StoreClientError(
                f"batch_remove size mismatch keys={len(keys)} results={len(raw)}"
            )
        failed = [(k, rc) for k, rc in zip(keys, raw) if rc != 0]
        if failed:
            shown = failed[:4]
            tail = "..." if len(failed) > 4 else ""
            raise StoreClientError(
                f"batch_remove failed {len(failed)}/{len(keys)}: {shown}{tail}"
            )
        return raw

    def _memory_replica_sizes(self, keys: Sequence[str]) -> list[int]:
        """每个 key 的内存副本字节数。空写把这个数交给 UpsertStart，不新写数据。"""
        keys = list(keys)
        descs = self._s().batch_get_replica_desc(keys)
        items = descs.items() if isinstance(descs, dict) else zip(keys, descs)
        found: dict[str, int] = {}
        for key, replicas in items:
            sizes: list[int] = []
            for desc in replicas or []:
                if not getattr(desc, "is_memory_replica", False):
                    continue
                mem = desc.get_memory_descriptor()
                sizes.append(int(mem.buffer_descriptor.size))
            uniq = set(sizes)
            if len(uniq) != 1:
                raise StoreClientError(
                    f"软钉读不到唯一的原尺寸，没有钉上: {key!r} sizes={sorted(uniq)}"
                )
            found[str(key)] = uniq.pop()
        missing = [k for k in keys if k not in found]
        if missing:
            raise StoreClientError(
                "软钉缺少内存副本尺寸，没有钉上: " + ", ".join(missing[:4])
            )
        return [found[k] for k in keys]

    def soft_pin(self, keys: Sequence[str], *, enable: bool = True) -> None:
        """空写软钉。用官方包已经建好的 client，不第二次 create。

        ``_get_pyclient_capsule`` 拿回 setup 时放进 ``store_`` 的 PyClient。
        扩展在这个对象上调 ``MasterClient::UpsertStart`` / ``UpsertEnd``，
        中间不 ``TransferWrite``。不用 ``upsert_tensor`` 重写数据。
        """
        store = self._s()
        keys = list(keys)
        sizes = self._memory_replica_sizes(keys)
        capsule_fn = getattr(store, "_get_pyclient_capsule", None)
        if not callable(capsule_fn):
            raise StoreClientError(
                "官方包没有 _get_pyclient_capsule，拿不回已建 client，没有钉上"
            )
        capsule = capsule_fn()
        if capsule is None:
            raise StoreClientError("已建 client 的 capsule 是空的，没有钉上")
        from kvcm.native.empty_pin import empty_soft_pin

        try:
            empty_soft_pin(capsule, keys, sizes, enable)
        except ImportError as e:
            raise StoreClientError(
                "空写扩展还没加载，没有钉上"
            ) from e

    def register_retained(self) -> None:
        """保留身份这次不登记。1fc27b6 没有 AddReplicaForRetainedClient。
        这里不接 kRetained。绑定上若没有 register_retained_client，调用会失败。
        """
        store = self._store
        if store is None:
            raise StoreClientError("store 未连接，保留身份没有注册")
        import mooncake.store as store_mod

        fn = getattr(store, "register_retained_client", None)
        if not callable(fn):
            names = [
                name
                for name in dir(store_mod)
                if "retain" in name.lower()
            ]
            raise StoreClientError(
                "mooncake wheel 没有保留身份注册，没有注册。"
                f" store 模块里相关名字: {names or '无'}"
            )
        fn()

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
