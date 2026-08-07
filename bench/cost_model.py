#!/usr/bin/env python3
"""P7.2 成本模型 v1:KV 传输 vs 计算,三模式量化分界。

形态对齐 SGLang HiCache prefetch 预算(base + per_token 线性;
3rdparty/sglang `hiradix_cache.py::prefetch_from_storage` 的
`timeout = min(max, base + per_ki_token * num_tokens/1024)`,
见 docs/research/sglang/hicache.md)。

参数三分源(每条标注):
- P7.1 原型实测(mock/loopback,只作相对校验)
- SLO draft 锚定(docs/features/slo.md)
- 待真机占位(标 TODO-P7-hw)

跑:`python3 bench/cost_model.py [--out bench/results/cost_model.json]`
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

# ---- 参数表(来源见 docs/architecture/cost-model.md §参数) ----

# Qwen3-0.6B(对齐 docs/research/transformers/overview.md 的 HF config):
# 28 layers × 2(K,V) × 8 kv_heads × 128 head_dim × 2B(fp16) = 114,688 B/token
QWEN3_06B_KV_BYTES_PER_TOKEN = 28 * 2 * 8 * 128 * 2

# SLO draft 锚定:prefill > 5000 tok/s/GPU、decode > 2000 tok/s/GPU(slo.md 吞吐表)
SLO_PREFILL_TOK_PER_S = 5000.0
SLO_DECODE_TOK_PER_S = 2000.0

# 传输固定开销:RTT + 建立/收尾。SGLang prefetch base=2.0s 是整树超时语义,
# 单次块传输的固定开销取 ms 级(待真机校准,TODO-P7-hw)。
TRANSFER_BASE_S = 2e-3
PREFILL_BASE_S = 1e-3  # kernel launch / 调度固定开销(待真机校准)

GB = 1e9

# ---- 分层加载剖面(决策 A:加载 vs 重算;来源 cost-model.md §参数) ----
# 形态对齐 SGLang `prefetch_threshold`(默认 256 token,L3 命中段短于阈值则放弃预取
# 直接重算,`hiradix_cache.py::prefetch_from_storage`);差异:SGLang 拍硬阈值,
# 我们按层介质成本从模型推出阈值。
TIER_LOAD_PROFILES = {
    # 层: (有效带宽 B/s, 单次取数固定开销 s)。均为待真机占位 TODO-P7-hw。
    "L1": (50 * GB, 0.5e-3),   # 池化 DRAM,RDMA 读
    "L2": (5 * GB, 2e-3),      # 池化 NVMe
    "L3": (1 * GB, 20e-3),     # 对象存储,RTT 主导固定开销
}


@dataclass
class ModelParams:
    kv_bytes_per_token: int = QWEN3_06B_KV_BYTES_PER_TOKEN
    block_tokens: int = 128
    bw_bytes_per_s: float = 10 * GB
    transfer_base_s: float = TRANSFER_BASE_S
    prefill_base_s: float = PREFILL_BASE_S
    prefill_per_token_s: float = 1.0 / SLO_PREFILL_TOK_PER_S
    decode_per_step_s: float = 1.0 / SLO_DECODE_TOK_PER_S


def t_transfer_s(hit_tokens: float, p: ModelParams) -> float:
    """传 hit_tokens 的 KV:base + 字节数/带宽(SGLang 线性形态)。"""
    return p.transfer_base_s + hit_tokens * p.kv_bytes_per_token / p.bw_bytes_per_s


def t_prefill_s(tokens: float, p: ModelParams) -> float:
    """重算 tokens 的 prefill:base + per_token 线性。"""
    return p.prefill_base_s + tokens * p.prefill_per_token_s


def breakeven_hit_tokens(p: ModelParams) -> float | None:
    """传 KV 与重算 prefill 的分界命中量 H*(token)。

    base_t + H·b/BW = base_p + H·t_c  →  H* = (base_t − base_p) / (t_c − b/BW)
    t_c ≤ b/BW 时传输永不划算(返回 None);H* ≤ 0 时任意命中都该传。
    """
    per_token_transfer = p.kv_bytes_per_token / p.bw_bytes_per_s
    denom = p.prefill_per_token_s - per_token_transfer
    if denom <= 0:
        return None  # 传输比计算还慢/一样慢:重算恒胜
    return (p.transfer_base_s - p.prefill_base_s) / denom


def quantize_to_blocks(tokens: float, block_tokens: int) -> int:
    """命中按 block 粒度向下取整(128 token 块 = 复用最小单位)。"""
    return int(tokens // block_tokens) * block_tokens


def t_load_s(tier: str, hit_tokens: float, p: ModelParams) -> float:
    """从 tier 加载 hit_tokens 的 KV:tier_base + 字节数/tier_bw。"""
    bw, base = TIER_LOAD_PROFILES[tier]
    return base + hit_tokens * p.kv_bytes_per_token / bw


def load_breakeven_tokens(tier: str, p: ModelParams) -> float | None:
    """该层「加载 vs 重算」分界 T*(token):命中段 ≥ T* 才值得加载,否则截断重算。

    tier_base + T·b/BW_t = base_p + T·t_c  →  T* = (tier_base − base_p)/(t_c − b/BW_t)
    t_c ≤ b/BW_t 时该层加载永不划算(返回 None);T* ≤ 0 时任意命中都该加载。
    """
    bw, base = TIER_LOAD_PROFILES[tier]
    denom = p.prefill_per_token_s - p.kv_bytes_per_token / bw
    if denom <= 0:
        return None
    return (base - p.prefill_base_s) / denom


def load_or_recompute(tier: str, hit_tokens: float, p: ModelParams) -> str:
    """决策 A(本次请求):命中段从 tier 加载,还是截断重算。

    语义 = SGLang prefetch_threshold 的派生版:命中段短于该层分界 → 放弃加载,
    截短复用前缀、尾部重算(前缀链式,只能截尾不能跳块)。
    """
    t_star = load_breakeven_tokens(tier, p)
    if t_star is None:
        return "recompute"
    threshold = max(t_star, 0)
    return "load" if hit_tokens >= threshold else "recompute"


def mode_for(hit_tokens: float, prompt_tokens: int, p: ModelParams,
             local_hit: bool = False) -> str:
    """三模式选择(features.md 决策表 + 本模型阈值)。

    - 本地命中(前缀 KV 已在执行节点 HBM)→ D-direct:零传输,残差 prefill 就地
    - 池命中且传 KV 划算(H ≥ H*)→ PD 分离
    - 否则重算 → 混部

    注意(口径,review #64):本函数的 PD 判定是**带宽连续函数**(H ≥ H*(BW),
    BW > ~0.6 GB/s 时 1 块命中即 PD);features.md / cost-model.md 阈值表里的
    「≥ 1 GB/s → PD」是**有意的保守简化**(H*≈12 ≪ 128,留足余量),
    真实分界由 breakeven_hit_tokens 连续给出。读 code/doc 对照时以本注释为准。
    """
    if local_hit and quantize_to_blocks(hit_tokens, p.block_tokens) > 0:
        return "D_DIRECT"
    h_star = breakeven_hit_tokens(p)
    worth = h_star is not None and hit_tokens >= max(h_star, p.block_tokens)
    return "PD_DISAGG" if worth else "COLOCATED"


def sweep_bandwidth() -> list[dict]:
    """带宽扫描:H* 随有效带宽变化(含分界消失点)。"""
    rows = []
    for gb in [0.1, 0.5, 1, 5, 10, 25, 100]:
        p = ModelParams(bw_bytes_per_s=gb * GB)
        h_star = breakeven_hit_tokens(p)
        rows.append({
            "bw_gb_s": gb,
            "per_token_transfer_us": p.kv_bytes_per_token / p.bw_bytes_per_s * 1e6,
            "breakeven_hit_tokens": None if h_star is None else round(max(h_star, 0), 1),
            "transfer_wins": h_star is not None,
        })
    return rows


def sweep_prompt_matrix() -> list[dict]:
    """(prompt 长度 × 命中率) 模式矩阵 @10GB/s,block=128。"""
    p = ModelParams(bw_bytes_per_s=10 * GB)
    rows = []
    for prompt in [512, 2048, 8192]:
        for hit_ratio in [0.0, 0.25, 0.5, 0.75, 1.0]:
            hit = quantize_to_blocks(prompt * hit_ratio, p.block_tokens)
            rows.append({
                "prompt_tokens": prompt,
                "hit_ratio": hit_ratio,
                "hit_tokens_quantized": hit,
                "mode_pool_path": mode_for(hit, prompt, p, local_hit=False),
                "mode_local_path": mode_for(hit, prompt, p, local_hit=True),
                "t_transfer_ms": round(t_transfer_s(hit, p) * 1000, 2),
                "t_prefill_ms": round(t_prefill_s(hit, p) * 1000, 2),
            })
    return rows


def sweep_block_granularity() -> list[dict]:
    """block 粒度 {64,128,256}:量化损失(命中率向下取整)vs 传输最小单元。"""
    p = ModelParams(bw_bytes_per_s=10 * GB)
    rows = []
    for blk in [64, 128, 256]:
        # 命中 300 token 的量化损失示例
        lost = 300 - quantize_to_blocks(300, blk)
        rows.append({
            "block_tokens": blk,
            "hit300_quantized": quantize_to_blocks(300, blk),
            "quantization_loss_tokens": lost,
            "min_transfer_ms": round(t_transfer_s(blk, p) * 1000, 3),
        })
    return rows


def sweep_tier_load() -> list[dict]:
    """分层加载 vs 重算:各层分界 T* 与块数阈值 + 典型命中段的决策。"""
    p = ModelParams()
    rows = []
    for tier in ["L1", "L2", "L3"]:
        bw, base = TIER_LOAD_PROFILES[tier]
        t_star = load_breakeven_tokens(tier, p)
        threshold_tokens = None if t_star is None else max(t_star, 0)
        threshold_blocks = (
            None if threshold_tokens is None
            else -(-int(threshold_tokens) // p.block_tokens)  # 向上取整到块
        )
        rows.append({
            "tier": tier,
            "bw_gb_s": bw / GB,
            "base_ms": base * 1000,
            "per_token_load_us": p.kv_bytes_per_token / bw * 1e6,
            "breakeven_tokens": None if t_star is None else round(t_star, 1),
            "load_threshold_blocks": threshold_blocks,
            "decision_128t": load_or_recompute(tier, 128, p),
            "decision_256t": load_or_recompute(tier, 256, p),
            "decision_1024t": load_or_recompute(tier, 1024, p),
        })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="结果 JSON 输出路径(可选)")
    args = ap.parse_args()

    p = ModelParams()
    result = {
        "phase": "P7.2",
        "params": asdict(p),
        "param_sources": {
            "kv_bytes_per_token": "Qwen3-0.6B HF config(transformers 参考树)",
            "prefill/decode_per_token": "SLO draft 锚定(slo.md 吞吐表)",
            "transfer_base_s": "待真机占位 TODO-P7-hw(形态借 SGLang prefetch base)",
            "bw_bytes_per_s": "扫描参数(0.1–100 GB/s 覆盖 loopback→RDMA)",
        },
        "bandwidth_sweep": sweep_bandwidth(),
        "prompt_matrix": sweep_prompt_matrix(),
        "block_granularity": sweep_block_granularity(),
        "tier_load": sweep_tier_load(),
    }

    print("== 带宽扫描(H* = 传 KV 划算的最小命中 token)==")
    for r in result["bandwidth_sweep"]:
        hs = r["breakeven_hit_tokens"]
        hs_str = str(hs) if hs is not None else "N/A(重算恒胜)"
        print(f"  {r['bw_gb_s']:>6} GB/s: per-token {r['per_token_transfer_us']:>8.1f}µs  "
              f"H*={hs_str}")
    print("== 模式矩阵 @10GB/s(pool 路径 / 本地路径)==")
    for r in result["prompt_matrix"]:
        print(f"  prompt={r['prompt_tokens']:>5} hit={r['hit_ratio']:>4.0%} → "
              f"{r['mode_pool_path']:<10}/{r['mode_local_path']:<10} "
              f"传 {r['t_transfer_ms']:>7.2f}ms vs 算 {r['t_prefill_ms']:>7.2f}ms")
    print("== block 粒度 ==")
    for r in result["block_granularity"]:
        print(f"  {r['block_tokens']:>3} tok: 300→{r['hit300_quantized']:>3} "
              f"(损 {r['quantization_loss_tokens']:>2}) 最小传输 {r['min_transfer_ms']}ms")
    print("== 分层加载 vs 重算(决策 A;SGLang prefetch_threshold 的派生版)==")
    for r in result["tier_load"]:
        ts = r["breakeven_tokens"]
        ts_str = f"{ts} tok → ≥{r['load_threshold_blocks']} 块才加载" if ts is not None else "N/A(重算恒胜)"
        print(f"  {r['tier']}: base {r['base_ms']:>5.1f}ms per-token {r['per_token_load_us']:>7.1f}µs  "
              f"T*={ts_str}  [128t→{r['decision_128t']}, 256t→{r['decision_256t']}, 1024t→{r['decision_1024t']}]")
    print("  注:L1 T*<0 = 任意命中即加载;L3 派生阈值 ≈2 块(256 token),与 SGLang 硬编码 "
          "prefetch_threshold=256 互相印证。")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
