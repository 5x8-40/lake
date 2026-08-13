# NVIDIA CMX 分析 + 仿真

只分析 NVIDIA CMX / Vera Rubin，不对照其它系统。

打开方式：本目录 `python3 -m http.server` 后访问。

| 页 | 内容 |
|---|---|
| [`index.html`](index.html) | **CMX 分析**：G3.5、STX、数据路径、六个技术点、5× 怎么读、公开数字 vs 缺口 |
| [`prefill.html`](prefill.html) | **仿真 1**：PD 分离、Prefill GPU 算力打满时的 CMX 读/写 |
| [`capacity.html`](capacity.html) | **仿真 2**：1M 会话在 G1/G2/CMX 的驻留条数；读写仍按 Prefill 跑满 |

```bash
python3 tools/cmx-sim/verify.py
```

## Prefill 跑满

```
unique = C × (1 − hit) + extra
FLOPs  = 2 × N_active + attn(C)
tok/s  = (peak_FLOPS × util) / FLOPs
write  = tok/s × ΔB/token
read   = req/s × KV(hit × C)
```

99% @ 1M 时 `read/write ≈ 99`。NIC 默认 CX-9 1.6 Tb/s 单向 ≈ 200 GB/s/GPU。

## 容量驻留

```
sessions_G1 = floor( HBM × (1 − weight) / KV )
sessions_G2 = floor( 54 TB / KV )
sessions_CMX = floor( PB_slider / KV )   # NVIDIA 只说「数 PB / POD」
```

Decode 的 `22 TB/s / KV` 只作对照，不是 CMX 模型。

## KV

- V4-Pro BF16 @ 1,048,576 = **9.62 GiB**（vLLM 附录 30×c4a + 31×c128a）
- dtype 独立可选；GLM FP8 还可选逻辑 576 vs 引擎 `fp8_ds_mla` 656

## 来源

- [CMX 博客](https://developer.nvidia.com/blog/introducing-nvidia-bluefield-4-powered-inference-context-memory-storage-platform-for-the-next-frontier-of-ai/)
- [Vera Rubin POD](https://developer.nvidia.com/blog/nvidia-vera-rubin-pod-seven-chips-five-rack-scale-systems-one-ai-supercomputer/)
- [Inside Rubin](https://developer.nvidia.com/blog/inside-the-nvidia-rubin-platform-six-new-chips-one-ai-supercomputer/)
- [vLLM V4 附录](https://vllm-project.github.io/2026/04/24/deepseek-v4.html)
