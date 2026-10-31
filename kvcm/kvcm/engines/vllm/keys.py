"""vLLM / vllm-ascend 的 key 计算：token 序列 → 块 hash 链 → PoolKey 字符串。

key 格式与 hash 算法全部 import 上游、不重实现：

- 块 hash 链：vllm ``kv_cache_utils.hash_block_tokens`` +
  ``get_hash_fn_by_name``（仅容器内可用）
- PoolKey 字符串格式：vllm-ascend ``PoolKey.to_string()``
  （rc1 布局 ``.../ascend_store/config_data.py``；main 布局
  ``.../ascend_store/metadata.py``）

容器内按字段名构造上游 ``KeyMetadata``（``dataclasses.fields`` 过滤），
上游格式变化（如 rc1 的 ``pcp_rank`` 字段与 ``@pcp:`` 串在 main 被移除）
自动跟随而不是静默漂移。

容器外（vllm_ascend 不可导入）回退内置 rc1 镜像 ``KeySpec`` 并 stderr 警
告；镜像与上游的一致性由漂移哨兵
``tests/test_vllm_keys.py::test_upstream_parity`` 在容器内把关。
"""

from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, fields

_RANK_RE = re.compile(r"@head_or_tp_rank:(\d+)@")


def parse_key_rank(key: str) -> int:
    """从 PoolKey 字符串提取 head_or_tp_rank。"""
    m = _RANK_RE.search(key)
    if not m:
        raise ValueError(f"key missing @head_or_tp_rank:<n>@: {key!r}")
    return int(m.group(1))


def group_keys_by_rank(keys: list[str]) -> dict[int, list[str]]:
    """按 head_or_tp_rank 分组（组内保持稳定顺序）。"""
    out: dict[int, list[str]] = defaultdict(list)
    for k in keys:
        out[parse_key_rank(k)].append(k)
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# 上游 import（首选路径）
# ---------------------------------------------------------------------------

_UPSTREAM: tuple | bool | None = None  # (KeyMetadata, PoolKey) | False


def upstream_key_classes() -> tuple | None:
    """lazy-import vllm-ascend (KeyMetadata, PoolKey)；不可导入时返回 None。"""
    global _UPSTREAM
    if _UPSTREAM is not None:
        return _UPSTREAM or None
    try:
        try:  # v0.26 rc1/rc2 布局
            from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.config_data import (
                KeyMetadata,
                PoolKey,
            )
        except ImportError:  # main 布局（config_data.py 改名 metadata.py）
            from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.metadata import (
                KeyMetadata,
                PoolKey,
            )
        _UPSTREAM = (KeyMetadata, PoolKey)
    except Exception:
        _UPSTREAM = False
    return _UPSTREAM or None


def _make_keys_upstream(
    model_name: str,
    chunk_hash: str,
    *,
    head_or_tp_rank: int = 0,
    pcp_rank: int = 0,
    dcp_rank: int = 0,
    pp_rank: int = 0,
    kv_cache_group_id: int = 0,
    cache_role: str = "kv",
    cache_family: str = "default",
    num_layers: int = 0,
) -> list[str]:
    """经上游 PoolKey 构造，只传该版本具有的字段（fields 过滤）。"""
    KeyMetadata, PoolKey = upstream_key_classes()  # type: ignore[misc]
    known = {f.name for f in fields(KeyMetadata)}
    wanted = {
        "model_name": model_name,
        "head_or_tp_rank": head_or_tp_rank,
        "pcp_rank": pcp_rank,
        "dcp_rank": dcp_rank,
        "pp_rank": pp_rank,
        "kv_cache_group_id": kv_cache_group_id,
        "cache_role": cache_role,
        "cache_family": cache_family,
    }
    km = KeyMetadata(**{k: v for k, v in wanted.items() if k in known})
    pk = PoolKey(km, chunk_hash)
    if num_layers > 0:
        return [lk.to_string() for lk in pk.split_layers(num_layers)]
    return [pk.to_string()]


# ---------------------------------------------------------------------------
# 离线镜像（仅回退；rc1 格式）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeySpec:
    """纯 Python 的 rc1 格式镜像。仅离线回退——容器路径走上游 PoolKey。
    上游改格式则镜像漂移，test_upstream_parity 变红。"""

    model_name: str
    chunk_hash: str
    head_or_tp_rank: int = 0
    pcp_rank: int = 0
    dcp_rank: int = 0
    pp_rank: int = 0
    kv_cache_group_id: int = 0
    cache_role: str = "kv"
    cache_family: str = "default"
    layer_id: int | None = None

    def to_string(self) -> str:
        if self.layer_id is None:
            return (
                f"{self.model_name}"
                f"@pcp:{self.pcp_rank}@dcp:{self.dcp_rank}"
                f"@head_or_tp_rank:{self.head_or_tp_rank}"
                f"@pp_rank:{self.pp_rank}"
                f"@group:{self.kv_cache_group_id}"
                f"@cache_role:{self.cache_role}"
                f"@cache_family:{self.cache_family}"
                f"@{self.chunk_hash}"
            )
        return (
            f"{self.model_name}"
            f"@pcp:{self.pcp_rank}@dcp:{self.dcp_rank}"
            f"@head_or_tp_rank:{self.head_or_tp_rank}"
            f"@group:{self.kv_cache_group_id}"
            f"@cache_role:{self.cache_role}"
            f"@cache_family:{self.cache_family}"
            f"@layer_id:{self.layer_id}"
            f"@{self.chunk_hash}"
        )


_WARNED_MIRROR = False


def expand_store_keys(
    *,
    model_name: str,
    chunk_hashes: list[str],
    tp_size: int = 1,
    put_step: int = 1,
    pcp_size: int = 1,
    dcp_size: int = 1,
    pp_size: int = 1,
    include_layers: bool = False,
    num_layers: int = 0,
    kv_cache_group_id: int = 0,
    cache_family: str = "default",
    prefer_upstream: bool = True,
) -> list[str]:
    """块 hash 列表 → 全 rank PoolKey 展开（与引擎调度侧查询 key 的枚举
    口径一致）。key 字符串默认来自上游 PoolKey；内置镜像仅离线回退。"""
    global _WARNED_MIRROR
    if include_layers and num_layers <= 0:
        raise ValueError("include_layers requires num_layers > 0")
    use_upstream = prefer_upstream and upstream_key_classes() is not None
    if prefer_upstream and not use_upstream and not _WARNED_MIRROR:
        print(
            "WARN: vllm_ascend not importable; using built-in rc1 key mirror "
            "(OFFLINE fallback — format may drift from engine)",
            file=sys.stderr,
        )
        _WARNED_MIRROR = True
    head_or_tp_ranks = max(tp_size // max(put_step, 1), 1)
    keys: list[str] = []
    for chunk_hash in chunk_hashes:
        for pcp_rank in range(pcp_size):
            for dcp_rank in range(dcp_size):
                for head_or_tp_rank in range(head_or_tp_ranks):
                    for pp_rank in range(pp_size):
                        if use_upstream:
                            keys.extend(
                                _make_keys_upstream(
                                    model_name,
                                    chunk_hash,
                                    head_or_tp_rank=head_or_tp_rank,
                                    pcp_rank=pcp_rank,
                                    dcp_rank=dcp_rank,
                                    pp_rank=pp_rank,
                                    kv_cache_group_id=kv_cache_group_id,
                                    cache_family=cache_family,
                                    num_layers=num_layers if include_layers else 0,
                                )
                            )
                        elif include_layers:
                            for layer_id in range(num_layers):
                                # LayerPoolKey.to_string 不含 pp_rank
                                keys.append(
                                    KeySpec(
                                        model_name=model_name,
                                        chunk_hash=chunk_hash,
                                        head_or_tp_rank=head_or_tp_rank,
                                        pcp_rank=pcp_rank,
                                        dcp_rank=dcp_rank,
                                        pp_rank=0,
                                        kv_cache_group_id=kv_cache_group_id,
                                        cache_family=cache_family,
                                        layer_id=layer_id,
                                    ).to_string()
                                )
                        else:
                            keys.append(
                                KeySpec(
                                    model_name=model_name,
                                    chunk_hash=chunk_hash,
                                    head_or_tp_rank=head_or_tp_rank,
                                    pcp_rank=pcp_rank,
                                    dcp_rank=dcp_rank,
                                    pp_rank=pp_rank,
                                    kv_cache_group_id=kv_cache_group_id,
                                    cache_family=cache_family,
                                ).to_string()
                            )
    return keys


# ---------------------------------------------------------------------------
# 块 hash 链（vllm import，仅容器内）
# ---------------------------------------------------------------------------


def hash_token_blocks(token_ids: tuple[int, ...], block_size: int, hash_algo: str) -> list[str]:
    """token 序列 → 块 hash 链（与引擎同口径）。

    守卫前置：上游把 NONE_HASH 折入整条 hash 链，PYTHONHASHSEED 未设时
    rc1 容器的 vllm 用 os.urandom(32) 充盐（对所有算法；vllm main 已改
    为 sha256 给固定 DEFAULT_NONE_HASH_SEED）。引擎侧固定 PYTHONHASHSEED=0
    并把 NONE_HASH=hash_fn("0") 折入每个块；控制面不带盐则每跑一次算出
    不同的链，batch_is_exist 全 miss。实测（2026-10-12）：同 prompt 两次
    运行两套 key。此处直接报错。
    """
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise RuntimeError(
            "PYTHONHASHSEED must be '0' to reproduce the engine's block-hash "
            "chain (vllm init_none_hash salts with os.urandom when unset). "
            "Export PYTHONHASHSEED=0 and rerun."
        )

    from vllm.utils.hashing import get_hash_fn_by_name
    from vllm.v1.core import kv_cache_utils as ku

    try:  # v0.26 rc1/rc2 布局
        from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.config_data import (
            block_hash_to_str,
        )
    except ImportError:  # main 布局
        from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.metadata import (
            block_hash_to_str,
        )

    hash_fn = get_hash_fn_by_name(hash_algo)
    ku.init_none_hash(hash_fn)

    hashes: list = []
    parent = None
    n_full = (len(token_ids) // block_size) * block_size
    for i in range(0, n_full, block_size):
        chunk = token_ids[i : i + block_size]
        h = ku.hash_block_tokens(hash_fn, parent, chunk, None)
        hashes.append(h)
        parent = h
    return [block_hash_to_str(h) for h in hashes]


# ---------------------------------------------------------------------------
# KeyComputer（引擎适配的 key 计算子件）
# ---------------------------------------------------------------------------


class KeyComputer:
    """前缀 → PoolKey 列表的计算器，持引擎侧展开配置。

    异构 TP：源与目标 TP 不同时按 effective_tp = max(local, peer) 展开
    （key 按较大 rank 数生成，缺侧由复制计划映射回较少 seg）。

    **一个实例 = 一种 (tp_size, peer_tp_size, put_step) 组合**：异构
    展开的 key 口径在构造期定死，换目标 TP / put_step 必须新建实例
    （因此持它的后端实例同样是一对源-目标组合的）。编排层持多组合
    时按组合各建一个后端实例，不要复用。

    异构前提链（缺一环则 effective 空间 key 全 miss）：
    1. 小 TP 当源时，源引擎打 tp_mismatch 补丁（vllm-ascend #15835
       的 rc1 backport；引擎树已含 9f8773ea 则不用），按 effective_tp
       命名写 sub-key。小 TP 当消费者时，坏在 get；
    2. 两端引擎的 PEER_TP_SIZE 都填 max(本机 TP, 对端 TP)，两方向同一
       组值，不要按方向翻成对端。大 TP 端等于自身、mismatch 关掉是对的；
    3. 本类 tp_size 填一侧 TP，peer_tp_size 填另一侧 TP。max 必须等于
       上面的 max。不要把 peer_tp_size 填成引擎环境变量那个 max。
    """

    def __init__(
        self,
        *,
        model_name: str,
        block_size: int = 128,
        tp_size: int = 1,
        peer_tp_size: int | None = None,
        put_step: int = 1,
        hash_algo: str = "sha256",
    ) -> None:
        self.model_name = model_name
        self.block_size = block_size
        self.tp_size = tp_size
        self.peer_tp_size = peer_tp_size
        self.put_step = put_step
        self.hash_algo = hash_algo

    @property
    def effective_tp(self) -> int:
        if self.peer_tp_size and self.peer_tp_size != self.tp_size:
            return max(self.tp_size, self.peer_tp_size)
        return self.tp_size

    def keys_for_tokens(self, token_ids: tuple[int, ...]) -> list[str]:
        """token 序列 → PoolKey 列表（hash 链仅容器内可用）。"""
        hexes = hash_token_blocks(token_ids, self.block_size, self.hash_algo)
        return expand_store_keys(
            model_name=self.model_name,
            chunk_hashes=hexes,
            tp_size=self.effective_tp,
            put_step=self.put_step,
        )

    def expand(self, chunk_hashes: list[str]) -> list[str]:
        """已知块 hash → PoolKey 展开。容器内走上游 PoolKey；不可导入时
        回退内置镜像（offline，stderr 警告）。"""
        return expand_store_keys(
            model_name=self.model_name,
            chunk_hashes=chunk_hashes,
            tp_size=self.effective_tp,
            put_step=self.put_step,
        )

    def build_plan(
        self, keys: list[str], targets: list[str]
    ) -> list[tuple[str, str]]:
        """key → 目标 seg 的映射计划：rank i 的 key → targets[i]。

        异构反向（目标 rank 数 < key 展开的 effective_tp）：eff rank i 归
        targets[i // n_sub]（seg 列表按倍数展开）；其余超界组合直接报错。
        rank 知识（key 格式、TP 配置）全部归本类——存储侧只消费计划，
        不解释 key。

        单 rank（全部 key 都是 rank 0）时 targets 必须恰为一个 seg——
        多余的 seg 被静默忽略意味着 locate 返回了脏列表（同机另一实例的
        seg 混进来），按 fail-loud 原则直接报错，不替调用方猜。
        """
        by_rank = group_keys_by_rank(keys)
        max_rank = max(by_rank) if by_rank else -1
        if max_rank <= 0:
            if not by_rank:
                return []
            if len(targets) != 1:
                raise ValueError(
                    f"all keys are rank 0 but {len(targets)} targets given "
                    f"(targets={list(targets)}) — single-rank target should "
                    "resolve to exactly one segment; dirty locate result?"
                )
            return [(k, targets[0]) for k in keys]
        if max_rank >= len(targets):
            local_tp = self.tp_size
            peer_tp = self.peer_tp_size
            eff = max(local_tp, peer_tp or 0)
            n_sub = eff // local_tp if local_tp else 1
            if (
                peer_tp
                and peer_tp > local_tp
                and local_tp > 0
                and eff % local_tp == 0
                and len(targets) == local_tp
                and max_rank == eff - 1
            ):
                targets = [targets[i // n_sub] for i in range(eff)]
            else:
                raise ValueError(
                    f"key rank max={max_rank} but only {len(targets)} targets "
                    f"(need targets[0..{max_rank}])"
                )
        unknown = [r for r in by_rank if r < 0 or r >= len(targets)]
        if unknown:
            raise ValueError(f"ranks out of range for targets: {unknown}")
        return [(k, targets[parse_key_rank(k)]) for k in keys]


def load_keys_file(path: str) -> list[str]:
    """从 dump 文件读 PoolKey（一行一个；# 起注释行）。调试用。"""
    keys: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                keys.append(line)
    return keys
