"""常驻循环（E11 进程化的支撑组件，L2）。

KeepaliveLoop 本期落地；ReleaseLoop / RetainLoop 只留骨架——依赖的
demote/retain/remove 在 L4 尚未接线（MooncakeBackend 均
NotImplementedError），能力接线完成前没有可执行内容。

并发纪律（E11 决策一）：Loop 线程与外部调用方（register/unregister）
只经 Ledger 交互（锁在 Ledger 内）；Loop 自身状态只有 stop event，
不加锁。
"""

from __future__ import annotations

import threading
import time

from kvcm.ledger import Ledger
from kvcm.ops.base import KvOps, QueryError
from kvcm.ops.types import Prefix


class KeepaliveLoop:
    """保活注册表驱动的周期续租循环。

    机制（D1 F15 / D2 F9）：`batch_is_exist` 顺带续租（`ProbeKey` 不
    续），lease TTL 是 master 配置（测试床实配 11s）——周期调
    `ops.exists(prefix)` 即续租，间隔必须显著小于 TTL。

    策略（D3 未定，E11 占位 = **固定窗口**）：注册后续 `window_s`
    （默认 10 min），到期停租注销。窗口按 Ledger 的 `registered_at`
    起算——重启恢复后窗口自然缩短、已过期的首扫即注销。保活必须有
    界：无限续租事实上等于 pin（D3 约束）。占位决策：exists 返回
    False（副本已不在池）即注销——续租没有对象，重新预热由 intent
    驱动并重新注册。D3 正式定型时替换策略实现，Loop 机制不变。
    """

    def __init__(
        self,
        ops: KvOps,
        ledger: Ledger,
        *,
        interval_s: float = 5.0,
        window_s: float = 600.0,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self._ops = ops
        self._ledger = ledger
        self._interval = interval_s
        self._window = window_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 注册（调用方 = TaskEngine READY 接线 / 手工）
    # ------------------------------------------------------------------

    def register(self, prefix: Prefix) -> None:
        """登记保活。幂等——重复注册刷新 registered_at（窗口重算）：
        同一前缀再次预热 READY 即续约，合理。"""
        self._ledger.register_keepalive(prefix)

    def unregister(self, prefix: Prefix) -> None:
        self._ledger.unregister_keepalive(prefix)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动后台循环（幂等）。重启恢复不需特判：注册行就在 Ledger
        里，第一轮 sweep 自然接管（窗口按 registered_at 已连续起算）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="kvcm-keepalive", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout_s: float = 10.0) -> None:
        """停循环（优雅退出第三步）。不撤销注册——保活行留在 Ledger，
        重启后续租或到期注销；lease 本身 TTL 到期自然失效，不残留。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.sweep_once()

    # ------------------------------------------------------------------
    # 一轮续租（独立成方法便于测试直调）
    # ------------------------------------------------------------------

    def sweep_once(self) -> None:
        now = time.time()
        for rec in self._ledger.list_keepalive_records():
            if now - rec.registered_at >= self._window:
                # 窗口到：停租注销（占位策略，D3 定型时替换）
                self._ledger.unregister_keepalive(rec.prefix)
                continue
            try:
                present = self._ops.exists(rec.prefix)
            except QueryError:
                # 查询失败跳过本轮——lease TTL 内有多个周期容错；连续
                # 失败说明后端/master 故障，归进程级监控，不在 Loop 内
                # 特判。
                continue
            if not present:
                # 副本已不在池：续租没有对象，注销（重新预热由 intent
                # 驱动并重新注册）
                self._ledger.unregister_keepalive(rec.prefix)


class ReleaseLoop:
    """软释放 / demote / 主动 Remove 三级（D1 §6）。骨架——依赖的
    demote/remove 在 L4 未接线，capability 具备并接线后再常驻化。"""


class RetainLoop:
    """retain 与计划下线编排（D2 §4）。骨架——依赖的 retain/move 在
    L4 未接线（retain 契约已定为返回 TaskHandle，主路径 retained
    client 注册待 mooncake 版本升级），能力接线后再常驻化。"""
