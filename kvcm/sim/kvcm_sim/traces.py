"""公开 trace → 会话模板（页级前缀键 + 流内时间线）。

三类输入统一成 `Session`：
- 每个请求给出到达时间、所属流（main / subagent / 一次性辅助）、输入 token 数、
  页键序列（链式前缀哈希，页 = `page_tokens` 个 token）、服务时长、流内下一请求时间。
- 页键链式：key_i = hash((key_{i-1}, block_ids_of_page_i))，与 vLLM/SGLang 的链式
  block hash 同构；同一前缀在不同请求中得到同一键，非前缀位置的同内容块不会误命中。
- `shared` 标记跨会话共享的页（Codex/Qwen 是全局内容哈希）；复制会话做负载放大时，
  非共享页加盐隔离、共享页保留，避免副本之间产生 trace 里不存在的复用。
"""

from __future__ import annotations

import json
import math
import os
import pickle
from dataclasses import dataclass, field

import numpy as np

CACHE_DIR = os.environ.get("KVCM_SIM_CACHE", "/tmp/kvcm-sim-cache")


@dataclass
class Req:
    t: float                 # 会话内相对到达时间（s）
    stream: int              # 会话内流 id
    n_in: int                # 输入 token 数
    pages: np.ndarray        # int64 页键（完整页）
    svc: float               # 服务时长（s）：占用实例的时间，用于负载与 in-flight
    next_t: float = math.inf  # 同一流下一请求的相对到达时间
    shared: np.ndarray | None = None  # bool，页是否跨会话共享（None=全部私有）


@dataclass
class Session:
    sid: int
    reqs: list[Req] = field(default_factory=list)


@dataclass
class Workload:
    name: str
    page_tokens: int
    sessions: list[Session]
    meta: dict = field(default_factory=dict)


def _chain(seed: int, ids, per_page: int) -> np.ndarray:
    n = len(ids) // per_page
    out = np.empty(n, dtype=np.int64)
    prev = seed
    for i in range(n):
        prev = hash((prev, tuple(ids[i * per_page:(i + 1) * per_page])))
        out[i] = prev
    return out


def _chain_cached(seed: int, ids, per_page: int, memo: dict) -> np.ndarray:
    """同一流内的请求前缀高度重合：按（seed, 前缀元组）分段复用已算的页键。"""
    n = len(ids) // per_page
    out = np.empty(n, dtype=np.int64)
    prev = seed
    for i in range(n):
        blk = tuple(ids[i * per_page:(i + 1) * per_page])
        k = (prev, blk)
        v = memo.get(k)
        if v is None:
            v = hash(k)
            memo[k] = v
        out[i] = v
        prev = v
    return out


def _fill_next(sess: Session) -> None:
    last: dict[int, Req] = {}
    for r in sorted(sess.reqs, key=lambda r: r.t):
        p = last.get(r.stream)
        if p is not None:
            p.next_t = r.t
        last[r.stream] = r


def _mark_shared(sessions: list[Session]) -> None:
    owners: dict[int, int] = {}
    for s in sessions:
        seen = set()
        for r in s.reqs:
            seen.update(r.pages.tolist())
        for k in seen:
            owners[k] = owners.get(k, 0) + 1
    multi = {k for k, c in owners.items() if c >= 2}
    for s in sessions:
        for r in s.reqs:
            if len(r.pages):
                r.shared = np.fromiter((k in multi for k in r.pages.tolist()), dtype=bool, count=len(r.pages))


def _cached(name: str, builder):
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, name + ".pkl")
    if os.path.exists(p):
        with open(p, "rb") as f:
            return pickle.load(f)
    w = builder()
    with open(p, "wb") as f:
        pickle.dump(w, f, protocol=pickle.HIGHEST_PROTOCOL)
    return w


# ---------------------------------------------------------------- AgentX / CC

def load_cc(path: str, page_tokens: int = 512, tag: str = "cc256k") -> Workload:
    """semianalysisai/cc-traces-weka-062126(-256k)：64-token block，hash 作用域为会话内。

    流划分：main 链（type=s）为流 0；每个 subagent group 一条流；main 层的一次性
    辅助请求（type=n）各自独立成流（无 join edge，主链不等它）。
    服务时长用 trace 自带 `api_time`（provider 侧实测，作为占用时长代理）。
    """
    def build():
        sessions = []
        per_page = page_tokens // 64
        with open(path) as f:
            for si, line in enumerate(f):
                d = json.loads(line)
                assert d.get("block_size") == 64
                sess = Session(si)
                memo: dict = {}
                seed = hash((0xCC, si))
                next_stream = 1

                def add(r, stream):
                    ids = r["hash_ids"]
                    sess.reqs.append(Req(
                        t=float(r["t"]), stream=stream, n_in=int(r["in"]),
                        pages=_chain_cached(seed, ids, per_page, memo),
                        svc=float(r.get("api_time") or 1.0)))

                for r in d["requests"]:
                    if r["type"] == "subagent":
                        for q in r["requests"]:
                            add(q, next_stream)
                        next_stream += 1
                    elif r["type"] == "s":
                        add(r, 0)
                    else:
                        add(r, next_stream)
                        next_stream += 1
                sess.reqs.sort(key=lambda r: r.t)
                _fill_next(sess)
                sessions.append(sess)
        return Workload(tag, page_tokens, sessions,
                        meta={"source": "huggingface.co/datasets/semianalysisai/" +
                              ("cc-traces-weka-062126-256k" if "256" in tag else "cc-traces-weka-062126"),
                              "hash_scope": "session-local"})
    return _cached(f"{tag}_p{page_tokens}", build)


# ---------------------------------------------------------------- Codex × SWE-bench Pro

def load_codex(path: str, page_tokens: int = 512, seed: int = 7) -> Workload:
    """Inferact/codex_swebenchpro_traces：只有对话文本，没有时间戳与 token 数。

    - token：tiktoken o200k_base 逐消息编码后拼接（不含 chat template，近似）。
    - 第 k 次 LLM 调用的输入 = 第 k 条 gpt 消息之前的全部消息。
    - 时间线是**合成的**：调用间隔 ~ lognormal(P50=5.2s, P90=23.0s)，按数据集 README
      §5 拟合（σ=ln(23/5.2)/1.2816）；服务时长 = 2s + output/40 tok/s（假设）。
    - 页键用全局内容哈希，跨 trial 的公共 system prompt 前缀天然共享。
    """
    def build():
        import tiktoken
        enc = tiktoken.get_encoding("o200k_base")
        data = json.load(open(path))
        rng = np.random.default_rng(seed)
        sigma = math.log(23.0 / 5.2) / 1.2816
        sessions = []
        memo: dict = {}
        gseed = 0xC0DE
        for si, trial in enumerate(data):
            msgs = trial["conversations"]
            toks: list[int] = []
            sess = Session(si)
            t = 0.0
            for m in msgs:
                if m["from"] == "gpt":
                    n_in = len(toks)
                    out_toks = enc.encode(m["value"], disallowed_special=())
                    svc = 2.0 + len(out_toks) / 40.0
                    sess.reqs.append(Req(t=t, stream=0, n_in=n_in,
                                         pages=_chain_cached(gseed, toks, page_tokens, memo),
                                         svc=svc))
                    toks.extend(out_toks)
                    t += svc + float(rng.lognormal(math.log(5.2), sigma))
                else:
                    toks.extend(enc.encode(m["value"], disallowed_special=()))
            memo.clear()
            _fill_next(sess)
            if sess.reqs:
                sessions.append(sess)
        _mark_shared(sessions)
        return Workload("codex", page_tokens, sessions,
                        meta={"source": "huggingface.co/datasets/Inferact/codex_swebenchpro_traces",
                              "hash_scope": "global", "timeline": "synthetic lognormal gaps"})
    return _cached(f"codex_p{page_tokens}", build)


# ---------------------------------------------------------------- Qwen-Bailian coder

def load_qwen(path: str, page_tokens: int = 512, tag: str = "qwen_coder") -> Workload:
    """alibaba-edu/qwen-bailian-usagetraces-anon：16-token block，全局 hash，真实时间戳。

    会话 = parent_chat_id 链；每链一条流。trace 没有服务时长：
    svc = 2s + output/40 tok/s（假设），并截断到不晚于下一轮到达。
    """
    def build():
        per_page = page_tokens // 16
        recs = [json.loads(l) for l in open(path)]
        byid = {r["chat_id"]: r for r in recs}
        root_of: dict[int, int] = {}

        def root(cid):
            path_ = []
            while True:
                if cid in root_of:
                    r = root_of[cid]
                    break
                p = byid[cid]["parent_chat_id"]
                if p == -1 or p not in byid:
                    r = cid
                    break
                path_.append(cid)
                cid = p
            for c in path_:
                root_of[c] = r
            root_of[cid] = r
            return r

        groups: dict[int, list] = {}
        for r in recs:
            groups.setdefault(root(r["chat_id"]), []).append(r)
        gseed = 0x9E4
        memo: dict = {}
        sessions = []
        for si, (rid, rs) in enumerate(sorted(groups.items(), key=lambda kv: byid[kv[0]]["timestamp"])):
            rs.sort(key=lambda r: r["timestamp"])
            t0 = rs[0]["timestamp"]
            sess = Session(si)
            for r in rs:
                sess.reqs.append(Req(t=r["timestamp"] - t0, stream=0, n_in=int(r["input_length"]),
                                     pages=_chain_cached(gseed, r["hash_ids"], per_page, memo),
                                     svc=2.0 + r["output_length"] / 40.0))
            _fill_next(sess)
            for q in sess.reqs:
                if q.next_t < math.inf:
                    q.svc = min(q.svc, max(0.05, q.next_t - q.t))
            sess.meta_t0 = t0  # type: ignore[attr-defined]
            sessions.append(sess)
        _mark_shared(sessions)
        return Workload(tag, page_tokens, sessions,
                        meta={"source": "github.com/alibaba-edu/qwen-bailian-usagetraces-anon (qwen_coder_blksz_16)",
                              "hash_scope": "global", "timeline": "trace timestamps (2h)"})
    return _cached(f"{tag}_p{page_tokens}", build)
