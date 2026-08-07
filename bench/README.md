# bench — P7 测量基座

统一指标出口：三语言探针各自产出 JSONL，`scripts/bench.sh` 合并为带环境块的 summary JSON。

## JSONL schema（每行一条记录）

```json
{
  "name": "router_e2e_latency",        // 探针名(蛇形,语言内唯一)
  "lang": "go",                        // go | rust | python
  "phase": "P7.1",                     // 产出该探针的阶段
  "env": {"transport": "...", "compute": "...", "note": "..."},
  "latency_ms": {"p50": 0.0, "p99": 0.0},  // 时延类探针;分段类用自定义键(如 serve_gate)
  "counters": {"requests": 96}         // 计数/分布(模式分布、HitStats 等)
}
```

- `latency_ms` / `counters` 至少其一非空。
- `env` 必须标注测量环境（mock/loopback/介质），**原型相对值不得与真硬件绝对值混用**——真 GPU/RDMA 校准 defer（issue #61 defer 项）。

## 探针清单

| 探针 | 语言 | 位置 | 内容 |
|------|------|------|------|
| `router_e2e_latency` / `router_queue_wait` / `router_mode_distribution` | Go | `go/router/bench_p71_test.go` | bufconn 驱动真 handler+PQ：e2e / 队列等待 p50/p99 / 选路模式分布 |
| `tier_promote_l0` / `tier_put_durable` / `tier_hit_stats` | Rust | `rust/tiered-store/src/bin/p71_probe.rs` | 分层 promote/put 时延 + HitStats 分层命中 |
| `hit_curve` / `promote_calibration` / `block_granularity` / `writeback_scan` / `gc_proxy` | Rust | `rust/tiered-store/src/bin/p73_curves.rs` | P7.3:zipf workload 命中率-容量曲线、promote hops 校准、粒度/写回扫描、迁移放大 |
| `coldstart_sequential` / `coldstart_layer_async` / `engine_e2e_mock` | Python | `python/lake/runtime/bench.py` | 冷启动分段（P6.6 harness）+ mock 引擎 e2e |

## 跑法

```bash
bash scripts/bench.sh          # 产出 bench/results/summary-<utc>.json
```

探针单独跑：各文件头注释有一行命令（`LAKE_BENCH_OUT=<file>` 控制写盘，不设则只跑断言/打印）。

## 后续阶段挂点

- P7.2 成本模型：参数从 summary 的 `router_*`/`tier_*` 实测回填。
- P7.3 分层曲线：workload 生成器驱动 `tier_hit_stats` 同类计数，扫容量/粒度。
- P7.4 冷启动分解：扩 `coldstart_*` 分段为瀑布表。
