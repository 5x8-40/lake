# CMX Prefill 跑满仿真

静态页：打开 [`index.html`](index.html)（或 `python3 -m http.server` 后访问该目录）。

钉公式：

```bash
python3 tools/cmx-sim/verify.py
```

## 这页算什么

Vera Rubin 上、PD 分离、**Prefill GPU 算力打满**。每人一条 ~1M token 会话，前缀命中可调。

不是 Decode 每步用 HBM 扫完整 KV。那个口径会把吞吐写成 `22 TB/s / KV_size`，和 Prefill 跑满不是一回事。

## KV dtype

侧栏独立可选，**切模型不会改精度**。

| 选项 | 含义 |
|---|---|
| BF16 | 对上 V4-Pro 附录 9.62 GiB |
| FP8 | attn KV 1 byte/elem；GLM indexer 132 B（含 scale） |
| FP8 + FP4 indexer | V4 serving 常用，约再小一半 |
| GLM 逻辑 576 / 引擎 656 | 仅 GLM + FP8：架构 latent vs vLLM `fp8_ds_mla` 页 |

图里可选「dtype → KV / 读写」。

## V4-Pro 为什么是 9.62 GiB

vLLM 2026-04-24 附录（BF16，`N = 1,048,576`）：

- 共享 KV entry：`512 × 2 = 1024` B
- c4a indexer entry：`128 × 2 = 256` B
- c4a 层：`(128 + N/4) × 1024 + (N/4) × 256` ≈ 320.1 MiB
- c128a 层：`(128 + N/128) × 1024` ≈ 8.1 MiB
- **30 c4a + 31 c128a ≈ 9.62 GiB**

论文 §4.2.1：Pro 61 层，前 2 层 HCA，其后 CSA/HCA 交错 → 30+31。Flash 43 层，前 2 层纯 SWA，其后交错 → 21 c4a + 20 c128a ≈ 6.73 GiB。

「V3.2 的 10%」是论文相对口径（V3.2 @ 1M ≈ 83.9 GiB 的 10% ≈ 8.4 GiB），不能代替上面这条逐层公式；更不能再除一次平均 token 得到 ~5 GB。

服务端常用 FP8 attn + FP4 indexer，体积大约再小一半。页上默认 BF16，为了对上 9.62 GiB。

## Prefill 跑满

```
unique = C × (1 − hit) + extra
FLOPs  = 2 × N_active + attn(C)          # V4 indexer 只扫 C/4
tok/s  = (peak_FLOPS × util) / FLOPs
req/s  = tok/s / unique
write  = tok/s × ΔB/token                # 新 KV
read   = req/s × KV(hit × C)             # 前缀预取到 Prefill GPU
```

99% @ 1M 时 `read/write ≈ 99`。NIC 默认按 ConnectX-9 1.6 Tb/s 单向 ≈ 200 GB/s/GPU，可改。

## 参考

- [vLLM DeepSeek V4](https://vllm-project.github.io/2026/04/24/deepseek-v4.html) 附录算术
- DeepSeek-V4 论文 arXiv:2606.19348 §4.2.1 层配置；SGLang `deepseek_v4.py`（Flash 默认 43 层、`index_head_dim=128`）
- SGLang `deepseek_v4_memory_pool.py::DeepSeekV4SingleKVPool.get_bytes_per_token` 是未压缩 FP8 页布局（584 B/token），**不是** 9.62 GiB 那条压缩公式
- GLM-5.2：78+MTP 层 MLA 576-d + 22 indexer 层（FP8 48408 B/token）
- Kimi K3：24 MLA × 576 + 69 层 KDA 固定 ~221.55 MiB/会话
