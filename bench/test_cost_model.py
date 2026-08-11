"""P7.2 成本模型回归钉测试:把 docs/architecture/cost-model.md 的阈值声称
钉成 CI 门禁(pytest 纯函数,无硬件依赖)。

钉法先例 = Go/Rust HRW 跨语言镜像向量测试(go/router 的
TestFnv1a64CrossLanguageVectors / rust 侧 hrw_score_matches_go_mirror_vectors):
「钉绝对值」防止常量被无意改动后文档与代码悄悄漂移。

⚠ 改动 cost_model.py 的 ModelParams 常量 / TIER_LOAD_PROFILES 剖面会导致
本文件 §6 回归锚失败——此时必须同步更新 docs/architecture/cost-model.md
阈值表与 docs/features/slo.md 回填(锚上注释同样写明)。
"""

from __future__ import annotations

import pytest

from cost_model import (
    GB,
    QWEN3_06B_KV_BYTES_PER_TOKEN,
    SLO_PREFILL_TOK_PER_S,
    ModelParams,
    breakeven_hit_tokens,
    load_breakeven_tokens,
    mode_for,
    quantize_to_blocks,
)


def test_breakeven_none_when_transfer_not_cheaper() -> None:
    """传输 per-token ≥ 计算 per-token 时,传 KV 永不划算 → None(重算恒胜)。"""
    # 构造分界带宽:per_token_transfer == prefill_per_token 恰好相等。
    bw_at_parity = QWEN3_06B_KV_BYTES_PER_TOKEN * SLO_PREFILL_TOK_PER_S
    assert breakeven_hit_tokens(ModelParams(bw_bytes_per_s=bw_at_parity)) is None
    # 0.5 GB/s(低于分界带宽 ≈0.573 GB/s)默认参数下也是 None。
    assert breakeven_hit_tokens(ModelParams(bw_bytes_per_s=0.5 * GB)) is None


def test_breakeven_decreases_with_bandwidth() -> None:
    """单调性:带宽越高,传 KV 划算的最小命中量 H* 越小。"""
    h1 = breakeven_hit_tokens(ModelParams(bw_bytes_per_s=1 * GB))
    h10 = breakeven_hit_tokens(ModelParams(bw_bytes_per_s=10 * GB))
    h100 = breakeven_hit_tokens(ModelParams(bw_bytes_per_s=100 * GB))
    assert h1 is not None and h10 is not None and h100 is not None
    assert h1 > h10 > h100


def test_load_breakeven_tier_ordering() -> None:
    """层序:介质越快加载越划算 → T*(L1) < T*(L2) < T*(L3)。"""
    p = ModelParams()
    t1 = load_breakeven_tokens("L1", p)
    t2 = load_breakeven_tokens("L2", p)
    t3 = load_breakeven_tokens("L3", p)
    assert t1 is not None and t2 is not None and t3 is not None
    assert t1 < t2 < t3


def test_mode_for_three_boundaries() -> None:
    """三模式边界(features.md 决策表 + 本模型阈值):

    - hit=0 → COLOCATED(无命中重算,混部);
    - 本地命中 ≥1 块 → D_DIRECT(零传输直跳,不再看 H*);
    - 池命中 ≥ max(H*, 1 块) → PD_DISAGG(默认 10 GB/s 下 max(5.3,128)=128)。
    """
    p = ModelParams(bw_bytes_per_s=10 * GB)
    assert mode_for(0, 512, p, local_hit=False) == "COLOCATED"
    assert mode_for(0, 512, p, local_hit=True) == "COLOCATED"  # 本地但不足 1 块仍混部
    assert mode_for(128, 512, p, local_hit=True) == "D_DIRECT"
    assert mode_for(128, 512, p, local_hit=False) == "PD_DISAGG"


def test_quantize_to_blocks() -> None:
    """命中按 128 token 块向下取整:300 → 256。"""
    assert quantize_to_blocks(300, 128) == 256


# ---- §6 回归锚:钉绝对值(HRW 镜像向量同款钉法) ----
# 以下数值是 cost-model.md 阈值表 / slo.md 回填的来源;改动 ModelParams 常量
# 或 TIER_LOAD_PROFILES 剖面会打翻锚——届时必须同步更新这两份文档,不能只改测试。
# L3 T*≈222.7 tok → ≥2 块(256 token),与 SGLang 硬编码 prefetch_threshold=256
# (hiradix_cache.py::prefetch_from_storage)互相印证,见 cost-model.md §5。

REL = 1e-9  # 纯 IEEE 确定性数学,钉到浮点精度


def test_anchor_breakeven_at_10gbps() -> None:
    """H*@10GB/s = 5.304161857559915 token(带宽扫描表 10 GB/s 行)。"""
    h = breakeven_hit_tokens(ModelParams(bw_bytes_per_s=10 * GB))
    assert h == pytest.approx(5.304161857559915, rel=REL)


def test_anchor_l3_load_threshold() -> None:
    """L3 T* = 222.71192798199547 tok(→ ≥2 块才加载,decision_128t=recompute)。"""
    t = load_breakeven_tokens("L3", ModelParams())
    assert t == pytest.approx(222.71192798199547, rel=REL)


def test_anchor_l1_load_threshold_negative() -> None:
    """L1 T* = -2.5290046485128643 tok(<0 = 任意命中即加载)。"""
    t = load_breakeven_tokens("L1", ModelParams())
    assert t == pytest.approx(-2.5290046485128643, rel=REL)
