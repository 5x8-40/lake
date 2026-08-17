# NVIDIA CMX 分析计算器

技术分析在 [`docs/research/nvidia-cmx.md`](../../docs/research/nvidia-cmx.md)。页面只实现可审计的字节、容量、offered-load 和经济阈值公式；它不是 benchmark，也不包含 NVIDIA/VAST 未公开的 CMX 性能模型。VAST 当前 G3 的 20× 实验、`16 TB/GPU × 1152 GPU` 发布会算术和页面默认值都不是 CMX SKU/SLA。

后续决策见 [lake #22：可移交 Context 合同](https://gitlab.com/BeeBreeze/lake/-/issues/22) 与 [#23：Portable Context ABI 独立项目孵化](https://gitlab.com/BeeBreeze/lake/-/issues/23)。本工具不修改 lake proto/runtime。

```bash
# 从仓库根目录启动，文档链接才能正确解析
python3 -m http.server --directory .
# 打开 http://127.0.0.1:8000/tools/cmx-sim/

node tools/cmx-sim/verify.js
```

| 页 | 内容 |
|---|---|
| [`index.html`](index.html) | 外部 `λ` 或 GPU-saturated `λ` 下，local/direct/via-CMX handoff 的 CMX 与 P→D offered load |
| [`capacity.html`](capacity.html) | 按 byte basis、growing/state 分片、pool 副本和 GB/GiB 口径计算 GPU/rack/POD 会话数 |
| [`economics.html`](economics.html) | 匿名 Cursor trace、provider cache 留存证据，以及 token hit 与 avoided Prefill work 分离后的阈值模型 |

## GitLab Pages

三个 HTML 文件属于**同一个静态网站、同一个 Pages deployment**，不是三个独立部署：

- 站点入口：<https://lake-13d9f7.gitlab.io/>
- 路由：`/index.html`、`/capacity.html`、`/economics.html`
- 访问控制：沿用 GitLab 项目的 private Pages 设置，只有已登录的项目成员可查看
- MR !7 pipeline 先发布当前版本；合并后由 `main` pipeline 覆盖为主分支版本

核心口径：

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

- Byte basis 分四类：logical payload、engine entry payload、engine page allocation、custom wire。CMX 序列化未公开；不支持的 engine-page profile 返回 `N/A`。
- V4 engine-page estimate 按 vLLM block/alignment 和 settled sliding-window admission（`max_in_flight=0`）计算，不含全局 allocator fragmentation、packed-pool sharing 和 runtime reserve。
- V4 base/speculative/online-C128 compressor ring 是不同 representation；V4/GLM MTP 是显式可选 component。
- `q` 把“前缀命中”与“命中字节确实从 CMX 远程读取”分开。
- `a` 是长期 cache admission；via-CMX handoff 必须强制写出最终 continuation state。
- 有效 unique token/s 由用户输入，应来自实测或明确假设。当 `U=0` 时只能使用外部 `λ`；外部 `λ` 不随 hit 自动增长。
- CMX 和 P→D 链路预算默认是 `0`（未知）。填写后只得到用户假设下的 ceiling，不是 CMX 可达吞吐。
- 容量页不从 GPU 数猜 TP/CP：`growing shard ranks` 和 `fixed-state shard ranks` 分开，`state=1` 表示每 GPU 完整副本；G2/CMX 另乘 representation copies。未提供 HBM 容量/拓扑时不展示 HBM 会话数。
- 经济页把 trace token hit、可避免的 Prefill work、路径效率与 Cache 总成本分开：

  ```text
  gpu_cost_saved
    = prefill_gpu_cost_share × avoidable_prefill_work_fraction × path_efficiency
  net_saved
    = gpu_cost_saved − cache_total_cost_share
  ```

  Cursor CSV 的 `92.2939%` 只是 token 记账命中，没有 GPU time 或 CMX 成本，因此只给 what-if，不把“5% 成本换 20% 算力”写成 trace 结论。页面默认例子是 `25% × 80% × 100% = 20%` gross；若 Cache 成本在同一基线上为 `5%`，net 才是 `15%`。

校验锚点：

- V4-Pro BF16 @ 1,048,576：paged KV `9.6246 GiB`；加 vLLM FP32 compressor residual 后，可移交会话状态 `9.6421 GiB`。
- V4-Pro BF16 vLLM engine-page estimate：`9.6479 GiB`；FP8 entry profile payload `5.4039 GiB`，engine pages `5.5038 GiB`。
- V4-Flash BF16 @ 1,048,576：paged KV `6.7240 GiB`；加 compressor residual 后 `6.7354 GiB`。
- GLM-5.2 base FP8 logical：`46.5820 GiB`；计入 MTP：`47.2734 GiB`。
- Kimi K3 FP8 MLA + vLLM 默认 KDA state：`13.9185 GiB`，其中固定 state `428.5547 MiB`。
