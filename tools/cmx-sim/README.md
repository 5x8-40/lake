# NVIDIA CMX 分析计算器

三个页面提供字节、容量、offered load 和成本假设计算，不代表 CMX benchmark 或 SKU。技术依据见 [`docs/research/nvidia-cmx.md`](../../docs/research/nvidia-cmx.md)。

```bash
# 从仓库根目录启动
python3 -m http.server --directory .
# 打开 http://127.0.0.1:8000/tools/cmx-sim/

node tools/cmx-sim/verify.js
```

| 页面 | 内容 |
|---|---|
| [`index.html`](index.html) | CMX 与 P→D offered load；支持外部或 GPU-saturated `λ` |
| [`capacity.html`](capacity.html) | GPU、rack、POD 会话容量；支持分片、副本和 GB/GiB |
| [`economics.html`](economics.html) | 匿名 Cursor 用量、provider cache 留存和成本假设 |

## GitLab Pages

同一 private Pages 站点包含三个页面：

- <https://lake-13d9f7.gitlab.io/>
- `/capacity.html`
- `/economics.html`

只有已登录的项目成员可访问。MR !7 发布分支版本；合并后由 `main` 更新。

## 流量公式

```text
M = floor(C × hit / block) × block
U = C − M + E

prefix_cmx_read/request = q × selected_representation(M)

P→D local:    decode_cmx_read=0, direct=0
P→D direct:   decode_cmx_read=0, direct=selected_representation(C+E)
P→CMX→D:      decode_cmx_read=selected_representation(C+E), effective_a=1

cmx_write/request
  = effective_a × [selected_growing(C+E) − selected_growing(M) + final_state]

external λ      = user_supplied_req/s
GPU-saturated λ = effective_unique_tok/s/GPU × GPUs / U

cmx_read_offered  = λ × (prefix_cmx_read + decode_cmx_read)
cmx_write_offered = λ × cmx_write/request
direct_offered    = λ × direct/request
```

- Byte basis：logical payload、engine entry payload、engine page allocation、custom wire。未知 engine-page profile 返回 `N/A`。
- V4 base/speculative/online-C128 是不同 representation；V4/GLM MTP 是可选 component。
- `q` 表示命中字节中实际从 CMX 读取的比例；`a` 表示写入 CMX 的比例。
- `U=0` 时只能使用外部 `λ`。外部 `λ` 不随命中率变化。
- 链路预算默认为未知；填写后得到用户假设下的 ceiling，不是实测吞吐。
- 容量计算要求显式填写 growing/state 分片和 Pool representation 副本。

## 成本公式

```text
gpu_cost_saved
  = prefill_gpu_cost_share × avoidable_prefill_work_fraction × path_efficiency
net_saved
  = gpu_cost_saved − cache_total_cost_share
```

Cursor CSV 的 `92.2939%` 是 token 记账命中，不包含 GPU time 或 CMX 成本。默认 `25% × 80% × 100% = 20%` 仅为假设场景。

## 校验点

- V4-Pro BF16 @ 1,048,576：paged KV `9.6246 GiB`；含 compressor state `9.6421 GiB`；vLLM engine pages `9.6479 GiB`。
- V4-Pro FP8：entry payload `5.4039 GiB`；engine pages `5.5038 GiB`。
- V4-Flash BF16：paged KV `6.7240 GiB`；含 compressor state `6.7354 GiB`。
- GLM-5.2 FP8 logical：base `46.5820 GiB`；含 MTP `47.2734 GiB`。
- Kimi K3 FP8 MLA + KDA state：`13.9185 GiB`，其中 fixed state `428.5547 MiB`。

后续合同见 [#22](https://gitlab.com/BeeBreeze/lake/-/issues/22) 和 [#23](https://gitlab.com/BeeBreeze/lake/-/issues/23)。
