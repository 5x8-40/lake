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
import json
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

# 老库文件的列增补（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。
# kvcm 未正式部署，此处仅为 demo 期遗留 db 兜底。
_MIGRATIONS = (
    ("keepalive", "token_ids", "ALTER TABLE keepalive ADD COLUMN token_ids TEXT NOT NULL DEFAULT ''"),
    ("sessions", "token_ids", "ALTER TABLE sessions ADD COLUMN token_ids TEXT NOT NULL DEFAULT ''"),
    ("sessions", "model_id", "ALTER TABLE sessions ADD COLUMN model_id TEXT NOT NULL DEFAULT ''"),
)


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
    """sqlite 状态记录。单进程使用（kvcm 单实例）。

    并发约束：sqlite 连接默认 check_same_thread——本类只能在创建它的
    线程里用。当前全部调用方（MooncakeBackend._record 链路与流程层）
    同线程，无锁。E11 进程化把 TaskEngine 挪进独立线程时，要么在调用
    侧加锁，要么改每线程一连接——届时第一件事就是处理这条，否则第一
    次跨线程调用即 ProgrammingError。
    """

    _TASK_STATUSES = frozenset({"running", "ready", "failed", "abandoned"})

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """老库文件补列（缺列才 ALTER，已齐则零动作）。"""
        for table, column, ddl in _MIGRATIONS:
            cols = {
                row[1]
                for row in self._conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if column not in cols:
                self._conn.execute(ddl)
        self._conn.commit()

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

    def gc_tasks(self, *, max_age_s: float = 7 * 24 * 3600) -> int:
        """清理终态老任务（ready/failed/abandoned 且 updated_at 超龄），
        返回删除行数。running 行不动（在途或孤儿，处置归恢复策略）。

        调用归 E11 的周期循环；本表此前只增不删，长跑必胀。"""
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
        self._conn.execute(
            "DELETE FROM keepalive WHERE prefix_key = ?", (prefix_key(prefix),)
        )
        self._conn.commit()

    def list_keepalive(self) -> list[Prefix]:
        """全部在保前缀（还原为 Prefix——KeepaliveLoop 重启恢复的直接输入）。

        token_ids 必须随表持久化：prefix_key 是单向 digest，反推不出
        token 序列；没有本列则重启后保活无法恢复（「不可丢」名不符实）。
        老库迁移而来的行 token_ids 为 ''（无法还原），跳过并留给
        KeepaliveLoop 决定如何处理（重新注册或告警）。
        """
        rows = self._conn.execute(
            "SELECT model_id, token_ids FROM keepalive"
        ).fetchall()
        out: list[Prefix] = []
        for model_id, token_ids_json in rows:
            if not token_ids_json:
                continue
            out.append(
                Prefix(model_id=model_id, token_ids=tuple(json.loads(token_ids_json)))
            )
        return out

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
        self._conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        self._conn.commit()

    def list_session(self, session_id: str) -> list[Prefix]:
        """会话绑定的前缀（还原为 Prefix；token_ids 持久化的必要性同
        list_keepalive——会话结束要 demote 的前缀重启后必须能展开 key）。
        model_id 用独立列（prefix_key 首个冒号切割会被含冒号的 model_id
        截断）；老库行的空 token_ids 跳过（无法还原，唯一诚实做法）。"""
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
