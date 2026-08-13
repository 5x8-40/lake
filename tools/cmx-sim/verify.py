#!/usr/bin/env python3
"""钉住 KV 体积公式。V4-Pro BF16 @ 1M 必须是 9.62 GiB（vLLM 附录）。"""
from __future__ import annotations

GiB = 1024**3
SWA = 128
N = 1_048_576


def v4_layer_c4(n: int, kv_b: int, idx_b: int) -> float:
    return (SWA + n / 4) * kv_b + (n / 4) * idx_b


def v4_layer_c128(n: int, kv_b: int) -> float:
    return (SWA + n / 128) * kv_b


def v4_total(n: int, n_c4: int, n_c128: int, n_swa_only: int, kv_b: int, idx_b: int) -> float:
    return (
        n_c4 * v4_layer_c4(n, kv_b, idx_b)
        + n_c128 * v4_layer_c128(n, kv_b)
        + n_swa_only * SWA * kv_b
    )


def main() -> None:
    pro = v4_total(N, 30, 31, 0, 1024, 256)
    pro_gib = pro / GiB
    assert abs(pro_gib - 9.62) < 0.01, f"V4-Pro BF16 1M = {pro_gib:.4f} GiB, expected 9.62"
    c4 = v4_layer_c4(N, 1024, 256)
    c128 = v4_layer_c128(N, 1024)
    assert abs(c4 / (1024**2) - 320.1) < 0.1, c4 / (1024**2)
    assert abs(c128 / (1024**2) - 8.13) < 0.05, c128 / (1024**2)

    flash = v4_total(N, 21, 20, 2, 1024, 256)
    flash_gib = flash / GiB
    assert abs(flash_gib - 6.73) < 0.02, f"V4-Flash BF16 1M = {flash_gib:.4f} GiB"

    v32 = N * (1152 + 256) * 61
    v32_gib = v32 / GiB
    assert abs(v32_gib - 83.9) < 0.1, v32_gib

    glm_fp8_bpt = 79 * 576 + 22 * 132
    assert glm_fp8_bpt == 48408, glm_fp8_bpt
    glm_gib = N * glm_fp8_bpt / GiB
    assert abs(glm_gib - 47.27) < 0.05, glm_gib
    glm_packed = N * (79 * 656 + 22 * 132)
    assert abs(glm_packed / GiB - 53.45) < 0.05, glm_packed / GiB

    k3_mla_fp8 = 24 * 576 * N
    kda = 221.55 * 1024 * 1024
    k3_gib = (k3_mla_fp8 + kda) / GiB
    assert abs(k3_gib - 13.70) < 0.05, k3_gib

    inc = 30 * (1024 / 4 + 256 / 4) + 31 * (1024 / 128)
    assert abs(inc - 9848) < 1e-6, inc

    print("ok")
    print(f"  V4-Pro  BF16 1M = {pro_gib:.3f} GiB  ({pro:,.0f} B)")
    print(f"  V4-Flash BF16 1M = {flash_gib:.3f} GiB")
    print(f"  V3.2     BF16 1M = {v32_gib:.1f} GiB")
    print(f"  GLM-5.2  FP8  1M = {glm_gib:.2f} GiB")
    print(f"  K3       FP8  1M = {k3_gib:.2f} GiB (MLA + KDA)")
    print(f"  V4-Pro incremental = {inc:.0f} B/unique-token")


if __name__ == "__main__":
    main()
