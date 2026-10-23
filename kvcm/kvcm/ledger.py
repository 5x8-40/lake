"""Ledger：状态层（sqlite）。

四类状态记录，按可丢性分级：
- 任务状态机（可丢）：重启后任务终态可从 master 重查
- 保活注册表（不可丢）：丢了 = 保活停止 = 副本被驱逐
- 副本台账（半可丢）：可从 master 副本查询重建
- 会话绑定（不可丢）：丢了 = 会话结束时不知道该释放哪些前缀

部署：kvcm 单实例跟随 master；上 HA 时再换共享存储。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass

from kvcm.ops.types import Prefix

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT NOT NULL,
    op          TEXT NOT NULL,
    prefix_key  TEXT NOT NULL,
    target      TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (task_id)
);
CREATE TABLE IF NOT EXISTS keepalive (
    prefix_key      TEXT PRIMARY KEY,
    model_id        TEXT NOT NULL,
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
    op: str
    prefix_key: str
    target: str
    status: str
    created_at: float
    updated_at: float


class Ledger:
    """sqlite 状态记录。单进程使用（kvcm 单实例）。"""

    _TASK_STATUSES = frozenset({"running", "ready", "failed", "abandoned"})

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # 任务状态机（可丢）
    # ------------------------------------------------------------------

    def upsert_task(
        self, task_id: str, *, op: str, prefix: Prefix, target: str, status: str
    ) -> None:
        self.upsert_tasks([task_id], op=op, prefix=prefix, target=target, status=status)

    def upsert_tasks(
        self, task_ids: list[str], *, op: str, prefix: Prefix, target: str, status: str
    ) -> None:
        """批量 upsert（单事务）——多任务同时迁移时不出现半新半旧。"""
        if status not in self._TASK_STATUSES:
            raise ValueError(f"unknown task status: {status!r}")
        now = time.time()
        with self._conn:
            self._conn.executemany(
                "INSERT INTO tasks (task_id, op, prefix_key, target, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
                [
                    (tid, op, prefix_key(prefix), target, status, now, now)
                    for tid in task_ids
                ],
            )

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute(
            "SELECT task_id, op, prefix_key, target, status, created_at, updated_at"
            " FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return TaskRecord(*row) if row else None

    # ------------------------------------------------------------------
    # 保活注册表（不可丢）
    # ------------------------------------------------------------------

    def register_keepalive(self, prefix: Prefix) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO keepalive (prefix_key, model_id, registered_at)"
            " VALUES (?, ?, ?)",
            (prefix_key(prefix), prefix.model_id, time.time()),
        )
        self._conn.commit()

    def unregister_keepalive(self, prefix: Prefix) -> None:
        self._conn.execute(
            "DELETE FROM keepalive WHERE prefix_key = ?", (prefix_key(prefix),)
        )
        self._conn.commit()

    def list_keepalive(self) -> list[str]:
        rows = self._conn.execute("SELECT prefix_key FROM keepalive").fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # 副本台账（半可丢）
    # ------------------------------------------------------------------

    def put_replica(self, key: str, instance: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO replicas (key, instance, verified_at) VALUES (?, ?, ?)",
            (key, instance, time.time()),
        )
        self._conn.commit()

    def list_replicas(self, key: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT instance FROM replicas WHERE key = ?", (key,)
        ).fetchall()
        return [r[0] for r in rows]

    def delete_replica(self, key: str, instance: str) -> None:
        self._conn.execute(
            "DELETE FROM replicas WHERE key = ? AND instance = ?", (key, instance)
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # 会话绑定（不可丢）
    # ------------------------------------------------------------------

    def bind_session(self, session_id: str, prefix: Prefix, instance: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, prefix_key, instance, bound_at)"
            " VALUES (?, ?, ?, ?)",
            (session_id, prefix_key(prefix), instance, time.time()),
        )
        self._conn.commit()

    def unbind_session(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self._conn.commit()

    def list_session(self, session_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT prefix_key FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchall()
        return [r[0] for r in rows]
