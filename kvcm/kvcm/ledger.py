"""Ledger：状态层（sqlite）。

四类状态记录，按可丢性分级：
- 任务状态机（可丢）：裸状态可按 task_id 从 master 重查；跟踪关系
  （handle → plan）由 backend.rebuild() 用本表存的可重算输入
  （token_ids + target + op，key/seg 不落库）现场重算 plan 重建
- 保活注册表（不可丢）：丢了 = 保活停止 = 副本被驱逐
- 副本台账（半可丢）：可从 master 副本查询重建
- 会话绑定（不可丢）：丢了 = 会话结束时不知道该释放哪些前缀

部署：kvcm 单实例跟随 master；上 HA 时再换共享存储。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from kvcm.ops.types import Prefix

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT NOT NULL,
    handle_id   TEXT NOT NULL,
    op          TEXT NOT NULL,
    prefix_key  TEXT NOT NULL,
    target      TEXT NOT NULL,
    status      TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    token_ids   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (task_id)
);
CREATE TABLE IF NOT EXISTS keepalive (
    prefix_key      TEXT PRIMARY KEY,
    model_id        TEXT NOT NULL,
    token_ids       TEXT NOT NULL,
    registered_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS replicas (
    key         TEXT NOT NULL,
    instance    TEXT NOT NULL,
    verified_at REAL NOT NULL,
    PRIMARY KEY (key, instance)
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT NOT NULL,
    prefix_key  TEXT NOT NULL,
    instance    TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    token_ids   TEXT NOT NULL,
    bound_at    REAL NOT NULL,
    PRIMARY KEY (session_id, prefix_key)
);
"""


def prefix_key(prefix: Prefix) -> str:
    """前缀的稳定标识（model + token 序列的内容 hash）。"""
    digest = hashlib.sha256(
        ",".join(map(str, prefix.token_ids)).encode()
    ).hexdigest()[:16]
    return f"{prefix.model_id}:{digest}"


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    handle_id: str
    op: str
    prefix_key: str
    target: str
    status: str
    model_id: str
    token_ids: tuple[int, ...]
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class KeepaliveRecord:
    """保活注册行（Prefix + 注册时刻）。KeepaliveLoop 的固定窗口按
    registered_at 起算——重启恢复后窗口自然缩短（或已过期首扫即注销），
    不需要额外的到期列。"""

    prefix: Prefix
    registered_at: float


class Ledger:
    """sqlite 状态记录。单进程使用（kvcm 单实例）。

    线程模型：**专用单线程 executor**——连接在该 executor 线程内创建，
    `check_same_thread` 保持默认 True（任何别的线程直接碰连接会立刻
    报错，误用无法静默）。全部公共方法都经 `_run` 投递到该线程串行
    执行，因此不需要锁，也不会有两线程同时进连接。

    两个方法互调时（如 `upsert_task` → `upsert_tasks`）在 executor 线
    程内**就地执行**，不再投递——单线程 executor 里嵌套提交会自死锁。
    """

    _TASK_STATUSES = frozenset({"running", "ready", "failed", "abandoned"})

    def __init__(self, path: str) -> None:
        # kvcm 未正式部署，只管新建库（CREATE TABLE IF NOT EXISTS 建全
        # 列）；不做老库迁移——遇到旧结构的库文件应直接删除重建。
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="kvcm-ledger"
        )
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        self._worker_ident: int | None = None
        # 建连接与建表在 executor 线程内完成（连接归属该线程）
        try:
            self._run(self._open)
        except BaseException:
            # 建库失败：不留悬空的 executor 线程
            self._executor.shutdown(wait=False)
            raise

    def _open(self) -> None:
        self._worker_ident = threading.get_ident()
        self._conn = sqlite3.connect(self._path)
        self._conn.executescript(_SCHEMA)

    def _run(self, fn, *args, **kwargs):
        """在 executor 线程内执行 fn；已在该线程时就地执行（防自死锁）。

        用线程 ident 判定（不是线程名前缀）：进程里可能有多个 Ledger，
        各自的 worker 线程名相同——按名字判定会让「A 的 worker 调 B」
        被误判成就地执行，把 B 的连接用在错误线程上。
        """
        if self._worker_ident is not None and threading.get_ident() == self._worker_ident:
            return fn(*args, **kwargs)
        return self._executor.submit(fn, *args, **kwargs).result()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        def _close() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        try:
            self._run(_close)
        finally:
            self._executor.shutdown(wait=True)

    # ------------------------------------------------------------------
    # 任务状态机（可丢）
    # ------------------------------------------------------------------

    def upsert_task(
        self,
        task_id: str,
        *,
        handle_id: str,
        op: str,
        prefix: Prefix,
        target: str,
        status: str,
    ) -> None:
        self.upsert_tasks(
            [task_id], handle_id=handle_id, op=op, prefix=prefix, target=target, status=status
        )

    def upsert_tasks(
        self,
        task_ids: list[str],
        *,
        handle_id: str,
        op: str,
        prefix: Prefix,
        target: str,
        status: str,
    ) -> None:
        """批量 upsert（单事务）——多任务同时迁移时不出现半新半旧。

        存的是**可重算输入**（token_ids + target + op），不存 key/seg—
        —重启后由 KeyComputer 现场重算 plan 重建跟踪。
        """
        if status not in self._TASK_STATUSES:
            raise ValueError(f"unknown task status: {status!r}")
        self._run(
            self._upsert_tasks_sync,
            task_ids,
            handle_id=handle_id,
            op=op,
            prefix=prefix,
            target=target,
            status=status,
        )

    def _upsert_tasks_sync(
        self,
        task_ids: list[str],
        *,
        handle_id: str,
        op: str,
        prefix: Prefix,
        target: str,
        status: str,
    ) -> None:
        now = time.time()
        token_json = json.dumps(list(prefix.token_ids))
        with self._conn:
            self._conn.executemany(
                "INSERT INTO tasks (task_id, handle_id, op, prefix_key, target, status, model_id, token_ids, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,"
                " updated_at=excluded.updated_at",
                [
                    (tid, handle_id, op, prefix_key(prefix), target, status,
                     prefix.model_id, token_json, now, now)
                    for tid in task_ids
                ],
            )

    def _row_to_record(self, row: tuple) -> TaskRecord:
        (task_id, handle_id, op, pk, target, status, model_id,
         token_json, created_at, updated_at) = row
        return TaskRecord(
            task_id=task_id,
            handle_id=handle_id,
            op=op,
            prefix_key=pk,
            target=target,
            status=status,
            model_id=model_id,
            token_ids=tuple(json.loads(token_json)) if token_json else (),
            created_at=created_at,
            updated_at=updated_at,
        )

    def list_running_tasks(self) -> list[TaskRecord]:
        """全部 running 任务（重启恢复跟踪的输入）。"""
        return self._run(self._list_running_tasks_sync)

    def _list_running_tasks_sync(self) -> list[TaskRecord]:
        rows = self._conn.execute(
            "SELECT task_id, handle_id, op, prefix_key, target, status, model_id,"
            " token_ids, created_at, updated_at FROM tasks"
            " WHERE status = 'running' ORDER BY rowid"
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_tasks_by_handle(self, handle_id: str) -> list[TaskRecord]:
        """同一 handle 分组的全部任务（含终态；观测态恢复要读出冲突
        failed 兄弟）。"""
        return self._run(self._list_tasks_by_handle_sync, handle_id)

    def _list_tasks_by_handle_sync(self, handle_id: str) -> list[TaskRecord]:
        rows = self._conn.execute(
            "SELECT task_id, handle_id, op, prefix_key, target, status, model_id,"
            " token_ids, created_at, updated_at FROM tasks"
            " WHERE handle_id = ? ORDER BY rowid",
            (handle_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self._run(self._get_task_sync, task_id)

    def _get_task_sync(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute(
            "SELECT task_id, handle_id, op, prefix_key, target, status, model_id,"
            " token_ids, created_at, updated_at FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def gc_tasks(self, *, max_age_s: float = 7 * 24 * 3600) -> int:
        """清理终态老任务（ready/failed/abandoned 且 updated_at 超龄），
        返回删除行数。running 行不动（在途或孤儿，处置归恢复策略）。

        调用归 E11 的周期循环；本表此前只增不删，长跑必胀。"""
        return self._run(self._gc_tasks_sync, max_age_s)

    def _gc_tasks_sync(self, max_age_s: float) -> int:
        cur = self._conn.execute(
            "DELETE FROM tasks WHERE status != 'running' AND updated_at < ?",
            (time.time() - max_age_s,),
        )
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # 保活注册表（不可丢）
    # ------------------------------------------------------------------

    def register_keepalive(self, prefix: Prefix) -> None:
        self._run(self._register_keepalive_sync, prefix)

    def _register_keepalive_sync(self, prefix: Prefix) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO keepalive (prefix_key, model_id, token_ids, registered_at)"
            " VALUES (?, ?, ?, ?)",
            (
                prefix_key(prefix),
                prefix.model_id,
                json.dumps(list(prefix.token_ids)),
                time.time(),
            ),
        )
        self._conn.commit()

    def unregister_keepalive(self, prefix: Prefix) -> None:
        self._run(self._unregister_keepalive_sync, prefix)

    def _unregister_keepalive_sync(self, prefix: Prefix) -> None:
        self._conn.execute(
            "DELETE FROM keepalive WHERE prefix_key = ?", (prefix_key(prefix),)
        )
        self._conn.commit()

    def list_keepalive_records(self) -> list[KeepaliveRecord]:
        """全部在保前缀（Prefix + registered_at）。KeepaliveLoop 的直接
        输入：固定窗口按 registered_at 起算。

        token_ids 必须随表持久化：prefix_key 是单向 digest，反推不出
        token 序列；没有本列则重启后保活无法恢复（「不可丢」名不符实）。
        数据残缺的行（token_ids 为 ''，无法还原），跳过并留给
        KeepaliveLoop 决定如何处理（重新注册或告警）。
        """
        return self._run(self._list_keepalive_records_sync)

    def _list_keepalive_records_sync(self) -> list[KeepaliveRecord]:
        rows = self._conn.execute(
            "SELECT model_id, token_ids, registered_at FROM keepalive"
        ).fetchall()
        out: list[KeepaliveRecord] = []
        for model_id, token_ids_json, registered_at in rows:
            if not token_ids_json:
                continue
            out.append(
                KeepaliveRecord(
                    prefix=Prefix(
                        model_id=model_id,
                        token_ids=tuple(json.loads(token_ids_json)),
                    ),
                    registered_at=registered_at,
                )
            )
        return out

    def list_keepalive(self) -> list[Prefix]:
        """全部在保前缀（只还原 Prefix；需要 registered_at 的调用方用
        list_keepalive_records）。"""
        return [r.prefix for r in self.list_keepalive_records()]

    # ------------------------------------------------------------------
    # 副本台账（半可丢）
    # ------------------------------------------------------------------

    def put_replica(self, key: str, instance: str) -> None:
        self._run(self._put_replica_sync, key, instance)

    def _put_replica_sync(self, key: str, instance: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO replicas (key, instance, verified_at) VALUES (?, ?, ?)",
            (key, instance, time.time()),
        )
        self._conn.commit()

    def list_replicas(self, key: str) -> list[str]:
        return self._run(self._list_replicas_sync, key)

    def _list_replicas_sync(self, key: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT instance FROM replicas WHERE key = ?", (key,)
        ).fetchall()
        return [r[0] for r in rows]

    def delete_replica(self, key: str, instance: str) -> None:
        self._run(self._delete_replica_sync, key, instance)

    def _delete_replica_sync(self, key: str, instance: str) -> None:
        self._conn.execute(
            "DELETE FROM replicas WHERE key = ? AND instance = ?", (key, instance)
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # 会话绑定（不可丢）
    # ------------------------------------------------------------------

    def bind_session(self, session_id: str, prefix: Prefix, instance: str) -> None:
        self._run(self._bind_session_sync, session_id, prefix, instance)

    def _bind_session_sync(
        self, session_id: str, prefix: Prefix, instance: str
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, prefix_key, instance, model_id, token_ids, bound_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                prefix_key(prefix),
                instance,
                prefix.model_id,
                json.dumps(list(prefix.token_ids)),
                time.time(),
            ),
        )
        self._conn.commit()

    def unbind_session(self, session_id: str) -> None:
        self._run(self._unbind_session_sync, session_id)

    def _unbind_session_sync(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self._conn.commit()

    def list_session(self, session_id: str) -> list[Prefix]:
        """会话绑定的前缀（还原为 Prefix；token_ids 持久化的必要性同
        list_keepalive——会话结束要 demote 的前缀重启后必须能展开 key）。
        model_id 用独立列（prefix_key 首个冒号切割会被含冒号的 model_id
        截断）；空 token_ids 的残缺行跳过（无法还原，唯一诚实做法）。"""
        return self._run(self._list_session_sync, session_id)

    def _list_session_sync(self, session_id: str) -> list[Prefix]:
        rows = self._conn.execute(
            "SELECT model_id, token_ids FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return [
            Prefix(
                model_id=model_id,
                token_ids=tuple(json.loads(token_ids_json)),
            )
            for model_id, token_ids_json in rows
            if token_ids_json
        ]

    # ------------------------------------------------------------------
    # 测试支持（受控改写，避免测试直接碰连接——连接归属 executor 线程）
    # ------------------------------------------------------------------

    def _backdate_tasks(self, *, days: float, exclude: tuple[str, ...] = ()) -> None:
        """把任务行 updated_at 往前拨（gc 测试用）。"""
        self._run(self._backdate_tasks_sync, days=days, exclude=exclude)

    def _backdate_tasks_sync(self, *, days: float, exclude: tuple[str, ...]) -> None:
        old = time.time() - days * 24 * 3600
        if exclude:
            placeholders = ",".join("?" for _ in exclude)
            self._conn.execute(
                f"UPDATE tasks SET updated_at = ? WHERE task_id NOT IN ({placeholders})",
                (old, *exclude),
            )
        else:
            self._conn.execute("UPDATE tasks SET updated_at = ?", (old,))
        self._conn.commit()

    def _backdate_keepalive(self, *, seconds: float) -> None:
        """把保活行 registered_at 往前拨（固定窗口测试用）。"""
        self._run(self._backdate_keepalive_sync, seconds=seconds)

    def _backdate_keepalive_sync(self, *, seconds: float) -> None:
        self._conn.execute(
            "UPDATE keepalive SET registered_at = ?", (time.time() - seconds,)
        )
        self._conn.commit()

    def _corrupt_task_row(self, task_id: str) -> None:
        """把任务行改造成数据残缺形态（handle_id/token_ids 置空；恢复
        测试用）。"""
        self._run(self._corrupt_task_row_sync, task_id)

    def _corrupt_task_row_sync(self, task_id: str) -> None:
        self._conn.execute(
            "UPDATE tasks SET handle_id = '', token_ids = '' WHERE task_id = ?",
            (task_id,),
        )
        self._conn.commit()
