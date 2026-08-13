# NVIDIA CMX 仿真

技术分析在 [`docs/research/nvidia-cmx.md`](../../docs/research/nvidia-cmx.md)。本目录 HTML 只做可调公式和图表。

```bash
python3 -m http.server --directory tools/cmx-sim
# 然后打开 http://127.0.0.1:8000/
python3 tools/cmx-sim/verify.py
```

| 页 | 内容 |
|---|---|
| [`index.html`](index.html) | PD 分离、Prefill GPU 算力打满时的 CMX 读/写 |
| [`capacity.html`](capacity.html) | 1M 会话在 G1/G2/CMX 的驻留条数；读写仍按 Prefill 跑满 |

Prefill 跑满：

```
unique = C × (1 − hit) + extra
FLOPs  = 2 × N_active + attn(C)
tok/s  = (peak_FLOPS × util) / FLOPs
write  = tok/s × ΔB/token
read   = req/s × KV(hit × C)
```

99% @ 1M 时 `read/write ≈ 99`。NIC 默认 CX-9 1.6 Tb/s 单向 ≈ 200 GB/s/GPU。

容量驻留：

```
sessions_G1  = floor( HBM × (1 − weight) / KV )
sessions_G2  = floor( 54 TB / KV )
sessions_CMX = floor( PB_slider / KV )   # NVIDIA 只说「数 PB / POD」
```

Decode 的 `22 TB/s / KV` 只作对照，不是 CMX 模型。

KV：V4-Pro BF16 @ 1,048,576 = **9.62 GiB**（vLLM 附录 30×c4a + 31×c128a）。dtype 独立可选；GLM FP8 还可选逻辑 576 vs 引擎 `fp8_ds_mla` 656。
