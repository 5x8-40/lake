"""把原始 trace 变成按会话组织的需求数组（不改 trace 自带时间）。

每个请求只保留流体模型要的量：
- `t`：会话内相对到达时间（trace 原值）；
- `p`：prefill 需求，A3 NPU·s = `Params.npu_seconds(n_in, hit)`；
- `kv`：decode 期间平均占用的 KV 字节 = (n_in + n_out/2) × bytes/token；
- `out`：输出 token 数（decode 时长 = out × 每步耗时）。

命中率取无限容量上限 h*（CC 按会话内 64-token block、Qwen 按全局 16-token block 首次出现计），
与 PR #90 回放的实际命中相差 < 1 个点（`results/summary.md`），PD 实验里各配置共用同一命中。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import numpy as np

from kvcm_sim.params import DEFAULT as BASE

TRACE_PATHS = {
    "cc256k": os.environ.get("CC256K", "/tmp/traces/cc256k.jsonl"),
    "qwen_coder": os.environ.get("QWEN_CODER", "/tmp/traces/qwen_coder.jsonl"),
}
CACHE = os.environ.get("PD_CACHE", "/tmp/pd_cache")


@dataclass
class Sessions:
    name: str
    off: np.ndarray      # 会话 i 的请求区间 [off[i], off[i+1])
    t: np.ndarray
    p: np.ndarray
    n_in: np.ndarray
    out: np.ndarray

    @property
    def n(self) -> int:
        return len(self.off) - 1

    def kv_bytes(self, bpt: float) -> np.ndarray:
        return (self.n_in + self.out / 2.0) * bpt


def _hit(n_in: int, new_blocks: int, bs: int) -> float:
    return 1.0 - min(n_in, new_blocks * bs) / n_in if n_in > 0 else 0.0


def _cc(path):
    sess = []
    for line in open(path):
        d = json.loads(line)
        flat = []
        for r in d["requests"]:
            flat.extend(r["requests"] if r["type"] == "subagent" else [r])
        flat.sort(key=lambda r: r["t"])
        seen: set = set()
        rows = []
        for r in flat:
            h = r["hash_ids"]
            new = sum(1 for x in h if x not in seen)
            seen.update(h)
            rows.append((float(r["t"]), int(r["in"]), int(r.get("out") or 0), _hit(int(r["in"]), new, 64)))
        sess.append(rows)
    return sess


def _qwen(path):
    recs = [json.loads(l) for l in open(path)]
    recs.sort(key=lambda r: r["timestamp"])
    seen: set = set()
    hit = {}
    for r in recs:
        h = r["hash_ids"]
        new = sum(1 for x in h if x not in seen)
        seen.update(h)
        hit[r["chat_id"]] = _hit(int(r["input_length"]), new, 16)
    byid = {r["chat_id"]: r for r in recs}
    root_of: dict = {}

    def root(c):
        path_ = []
        while c not in root_of:
            p = byid[c]["parent_chat_id"]
            if p == -1 or p not in byid:
                root_of[c] = c
                break
            path_.append(c)
            c = p
        r = root_of[c]
        for x in path_:
            root_of[x] = r
        return r

    groups: dict = {}
    for r in recs:
        groups.setdefault(root(r["chat_id"]), []).append(r)
    sess = []
    for _, rs in sorted(groups.items(), key=lambda kv: byid[kv[0]]["timestamp"]):
        t0 = rs[0]["timestamp"]
        sess.append([(r["timestamp"] - t0, int(r["input_length"]), int(r["output_length"]), hit[r["chat_id"]])
                     for r in rs])
    return sess


def load(name: str) -> Sessions:
    os.makedirs(CACHE, exist_ok=True)
    fn = os.path.join(CACHE, f"{name}.npz")
    if not os.path.exists(fn):
        raw = (_cc if name == "cc256k" else _qwen)(TRACE_PATHS[name])
        off = np.cumsum([0] + [len(s) for s in raw])
        a = np.array([row for s in raw for row in s], dtype=float)
        np.savez(fn, off=off, t=a[:, 0], n_in=a[:, 1], out=a[:, 2], hit=a[:, 3])
    z = np.load(fn)
    p = BASE.npu_seconds(z["n_in"], z["hit"])
    return Sessions(name, z["off"], z["t"], p, z["n_in"], z["out"])
