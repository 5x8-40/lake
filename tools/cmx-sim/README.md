# NVIDIA CMX 分析计算器

技术分析在 [`docs/research/nvidia-cmx.md`](../../docs/research/nvidia-cmx.md)。页面只实现可审计的字节与 offered-load 公式；它不是 benchmark，也不包含 NVIDIA/VAST 未公开的 CMX 性能模型。

```bash
# 从仓库根目录启动，文档链接才能正确解析
python3 -m http.server --directory .
# 打开 http://127.0.0.1:8000/tools/cmx-sim/

node tools/cmx-sim/verify.js
```

| 页 | 内容 |
|---|---|
| [`index.html`](index.html) | PD 分离下的 compute-offered 请求率，以及它要求的 CMX 读/写 |
| [`capacity.html`](capacity.html) | 用户给定可用容量后，按 GPU / rack / POD 分别计算会话数 |
| [`economics.html`](economics.html) | 匿名 Cursor trace、provider cache 留存证据，以及 Cache 成本换算力的阈值模型 |

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

read/request  = q × [growing_KV(M) + prefix_state]
write/request = a × [growing_KV(C+E) − growing_KV(M) + final_state]

offered_req/s = effective_unique_tok/s/GPU × GPUs / U
required_read = offered_req/s × read/request
required_write = offered_req/s × write/request
```

- `q` 把“前缀命中”与“命中字节确实从 CMX 远程读取”分开。
- `a` 是新状态写入 CMX 的比例。
- 有效 unique token/s 由用户输入，应来自实测或明确假设；页面不再用不完整的模型 FLOPs 公式推导它。
- 当 `U=0`（100% 块对齐命中且无新增 token）时，unique token/s 不能推出请求到达率；页面把 offered req/s 和非零所需流量标成 `N/A`，而不是伪造 `Infinity`。
- 链路预算默认是 `0`（未知）。填写后只得到该用户假设下的传输上限，不是 CMX 可达吞吐。
- 容量页是原始字节除法；布局、分片/复制、页取整、冗余、reserve 和 usable/raw 必须在生产 sizing 前另行确认。
- 经济页把 `cache hit`、Prefill 算力占比、命中避免效率与 Cache 总成本分开：

  ```text
  compute_saved = prefill_compute_share × cache_hit × avoid_efficiency
  net_saved     = compute_saved − cache_total_cost_share
  ```

  Cursor CSV 没有 GPU 时间或 CMX 成本，因此只给阈值，不把“5% 成本换 20% 算力”写成实测结论。匿名 trace 的 input/output 是每个 Cursor usage event 的聚合 token，不是单次底层模型调用长度。

校验锚点：

- V4-Pro BF16 @ 1,048,576：paged KV `9.6246 GiB`；加 vLLM FP32 compressor residual 后，可移交会话状态 `9.6421 GiB`。
- V4-Flash BF16 @ 1,048,576：paged KV `6.7240 GiB`；加 compressor residual 后 `6.7354 GiB`。
- GLM-5.2 base FP8 logical：`46.5820 GiB`；计入 MTP：`47.2734 GiB`。
- Kimi K3 FP8 MLA + vLLM 默认 KDA state：`13.9185 GiB`，其中固定 state `428.5547 MiB`。
