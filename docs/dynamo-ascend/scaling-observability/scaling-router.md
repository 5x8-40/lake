# KV Cache 路由层与 Lake Router autoscaler

> 专题索引:[README.md](README.md) · 代码级剖析,基于 `3rdparty/dynamo` 与本仓 `go/router/`

KV Cache 是 GPU 上存储的前缀缓存。每个 block 属于某个 worker 所有。当新节点加入时，Hash 环重新划分，原来由旧节点管理的 block 需要迁移到新节点。扩容时新节点"立即可用"（不等迁移完成），缩容时先停止新请求（markDraining），迁移完再删节点。防抖机制确保不会因为一瞬间的流量波动就频繁扩缩容。

## 一致性 Hash 环 — 节点加入时的数据迁移

<!-- SVG diagram: Before: 2 节点 — 一致性 Hash 环 (xxh3_64, 64 vnode/节点) · W0 · W1 -->

## Lake Router 自动扩缩（独立于 Planner）

除了 Planner，Lake 路由器还自带了一个轻量级的自动扩缩器。它不依赖 Planner，只看路由器的"队列深度"来决定加不加节点。这个 autoscaler 默认关闭，设置 `LAKE_AUTOSCALE=1` 开启。扩容时通过 CP RPC 通知控制面加入新节点，缩容时先标记 draining 再移除。

### 决策流程 (lake/go/router/autoscale.go)

```
LAKE_AUTOSCALE=1
→ autoscaleTick() 每 2s (server.go:145, autoscale.go:418)
→ reapDraining() — 清理已完成 drain 的节点
→ flushHotHits() — 上报 KV hit 到控制面
→ sched.LoadSnapshot() — 取调度器快照: QueueLen, InFlight
→ scaler.Evaluate() — 决策
→ applyScale() — 执行
```

### 防抖动逻辑 (Evaluate(), line 101)

就像一个温度传感器：不是说"超过 4 个请求就扩容"，而是"连续 3 次检查都超过 4，且距离上次操作超过 10 秒"才触发。如果中间恢复正常，计数器立即清零。这防止了"抖动" — 流量一会儿高一会儿低导致反复扩缩容。

| 参数 | 默认 | 大白话 |
| --- | --- | --- |
| `MinNodes` | 1 | 至少 1 个（worker-0 永不移除） |
| `MaxNodes` | 8 | 最多 8 个 |
| `ScaleOutQueueLen` | 4 | 队列 ≥ 4 持续 3 tick → 扩容 |
| `ScaleInQueueLen` | 0 | 队列为空且 inflight ≤ 1 持续 3 tick → 缩容 |
| `SustainPeriods` | 3 | 连续 tick 数 |
| `Cooldown` | 10s | 两次操作间最少间隔 |

<details><summary>▶ Router autoscale 完整代码</summary>

```
// lake/go/router/autoscale.go
type AutoscaleConfig struct {
    MinNodes         int           // default 1
    MaxNodes         int           // default 8
    ScaleOutQueueLen int           // default 4
    ScaleInQueueLen  int           // default 0
    SustainPeriods   int           // default 3 (连续 3 tick 确认)
    Cooldown         time.Duration // default 10s
}

// Evaluate: 防抖决策逻辑 (line 99-129)
func (a *Autoscaler) Evaluate(now time.Time, m MetricsSnapshot, nodeCount int) ScaleDecision {
    over := m.QueueLen >= a.cfg.ScaleOutQueueLen
    under := m.QueueLen <= a.cfg.ScaleInQueueLen && m.InFlight <= 1

    // streak counting: 连续 N 次满足条件才触发
    if over { a.overStreak++; a.underStreak = 0 }
    if under { a.underStreak++; a.overStreak = 0 }

    // cooldown: 距离上次操作至少 Cooldown 时间
    if now.Sub(a.lastAction) < a.cfg.Cooldown { return DecideNone }

    if a.overStreak >= a.cfg.SustainPeriods && nodeCount < a.cfg.MaxNodes {
        return DecideScaleOut
    }
    if a.underStreak >= a.cfg.SustainPeriods && nodeCount > a.cfg.MinNodes {
        return DecideScaleIn
    }
    return DecideNone
}

// applyScale: 执行扩容/缩容 (line 339-390)
func (s *Server) applyScale(ctx context.Context, d ScaleDecision) {
    switch d {
    case DecideScaleOut:
        id := s.nodes.nextID()
        migrations := s.cp.JoinShardNode(ctx, id) // RPC → CP
        s.nodes.add(id)  // 立即可路由
        s.syncCapacity() // 总并发 *= nodeCount
    case DecideScaleIn:
        victim := s.nodes.lastReady()  // LIFO: 最新的先进来最后走
        s.nodes.markDraining(victim)    // 停止接收新请求
        if !s.cp.DrainShardNode(ctx, victim) {
            s.nodes.setReady(victim) // 失败则回滚
            return
        }
        // reapDraining 会等待迁移完成后 RemoveShardNode
    }
}
```

</details>

### 扩容执行步骤

1. nodes.nextID() → "worker-2"
2. cp.JoinShardNode(RPC) → 迁移列表
3. nodes.add() → 立即可路由
4. syncCapacity() → 总并发 × nodeCount
5. warmup\_plan() → 热数据预热

### 缩容执行步骤

1. victim = lastReady() → LIFO 选最新的
2. markDraining() → 停止新请求
3. cp.DrainShardNode(RPC) → 迁移计划
   失败 → 回滚
4. reapDraining() → 等待迁移完成
