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

from typing import Sequence

# mooncake.store 为 lazy import（connect 时）：无 mooncake 安装的环境
# 仍可 import 本模块（离线 key 计算、拓扑解析、单元测试）。


def is_memory_replica(desc: object) -> bool:
    """内存副本谓词。上游绑定是方法，测试对象可以是布尔属性。

    方法对象本身为真。不能把 ``getattr`` 的结果直接当布尔，否则盘副本
    也会去调 ``get_memory_descriptor()``。
    """
    if not hasattr(desc, "is_memory_replica"):
        raise StoreClientError(
            "副本描述没有 is_memory_replica，绑定面不对"
        )
    pred = desc.is_memory_replica
    if callable(pred):
        return bool(pred())
    return bool(pred)


def entry_exists(e) -> bool:
    """batch_is_exist 的成功码：0 = 不存在，1 或 True = 存在。

    其它整数是 ErrorCode（``RPC_FAIL=-900`` 等），不是「存在」。
    调用方应先拒绝这些码；这里也不把任意正数当成存在。
    """
    if e is True or e == 1:
        return True
    if e is False or e == 0:
        return False
    raise StoreClientError(f"batch_is_exist unexpected code {e!r}")


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

    # ------------------------------------------------------------------
    # 存在性
    # ------------------------------------------------------------------

    def is_exist(self, keys: Sequence[str]) -> list[bool]:
        """batch_is_exist → bool 列表（读即续租——保活机制利用这一点）。

        成功只认 0（不存在）、1 或 True（存在）。其它整数是 ErrorCode
        原值（``INTERNAL_ERROR=-1``、``RPC_FAIL=-900``、``RPC_TIMEOUT=-901``），
        抛 StoreClientError，信息里带实际返回值。吞成 False 会让
        「查失败」变成「不在池」：预热终态失败，保活还会把行注销。
        """
        keys = list(keys)
        raw = list(self._s().batch_is_exist(keys))
        failed = [
            (k, e)
            for k, e in zip(keys, raw)
            if e is not True and e is not False and e not in (0, 1)
        ]
        if failed:
            shown = failed[:4]
            tail = "..." if len(failed) > 4 else ""
            raise StoreClientError(
                f"batch_is_exist per-key query failed ({len(failed)}/{len(keys)}): "
                f"{shown}{tail}"
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

    def soft_pin(
        self, keys: Sequence[str], sizes: Sequence[int], *, enable: bool = True
    ) -> None:
        """空写软钉。用官方包已经建好的 client，不第二次 create。

        ``sizes`` 由调用方从 ``GET /batch_query_keys`` 的 ``size_`` 传入。
        这里不调 ``batch_get_replica_desc`` / ``GetReplicaList``，那会
        ``GrantReadLease``。``has_read_lease`` 且存在非内存副本时，main 的
        ``UpsertStart`` 直接 ``OBJECT_HAS_LEASE``，空写失败。Case C 只在
        有租约且副本全是内存时：丢掉旧副本再按 ``replica_num=1`` 重分配，
        空写中间不写数据。这次只去掉 kvcm 自己取尺寸时授上的那次租约。
        已有租约且副本全是内存的情况仍未解决。有盘副本的 key 在调用前拒绝。

        ``_get_pyclient_capsule`` 拿回 setup 时放进 ``store_`` 的 PyClient。
        扩展在这个对象上调 ``MasterClient::UpsertStart`` / ``UpsertEnd``，
        中间不 ``TransferWrite``。不用 ``upsert_tensor`` 重写数据。
        """
        store = self._s()
        keys = list(keys)
        sizes = [int(s) for s in sizes]
        if len(keys) != len(sizes) or not keys or any(s <= 0 for s in sizes):
            raise StoreClientError("软钉读不到唯一的原尺寸，没有钉上")
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

    def query_task(self, task_id: object) -> object:
        """查询任务状态（QueryTaskResponse）。"""
        resp, err = self._s().query_task(task_id)
        if err != 0:
            raise StoreClientError(f"query_task failed err={err} task_id={task_id}")
        return resp

    # ------------------------------------------------------------------
    # 副本核验（绑定通道；注意：本通道有续租副作用。
    # 零副作用核验走 HTTP 管理端点。提回仍用这一条累计命中）
    # ------------------------------------------------------------------

    def batch_memory_replica_endpoints(self, keys: Sequence[str]) -> dict[str, list[str]]:
        """key → 内存副本端点映射（batch_get_replica_desc）。"""
        out: dict[str, list[str]] = {k: [] for k in keys}
        descs = self._s().batch_get_replica_desc(list(keys))
        items = descs.items() if isinstance(descs, dict) else zip(keys, descs)
        for key, replicas in items:
            eps: list[str] = []
            for desc in replicas or []:
                if is_memory_replica(desc):
                    mem = desc.get_memory_descriptor()
                    eps.append(mem.buffer_descriptor.transport_endpoint)
            out[str(key)] = eps
        return out
