# 调度流程（L0 / L1 / L2）

- 定位: [D8-joint-scheduling.md](../D8-joint-scheduling.md) 的流程视图——每张图对应一个决策环或联合场景
- 状态: 设计草案
- 读法: `->>` 调用/数据流；`-->>` 响应/异步回执；`--x` 失败/断线；`rect` 底色区分并列场景

---

## 1. L0 请求环：一次选路

```mermaid
sequenceDiagram
    participant C as Client/Agent
    participant G as Gateway
    participant R as Router
    participant KVT as KVT
    participant Q as 队列
    participant W as vLLM-Ascend
    participant K as kvcm

    C->>G: request + HookEnvelope
    G->>R: 透传（鉴权/限流已做）
    R->>R: 解析前缀 hash 链
    R->>KVT: Match(ns, hashes, 候选节点)
    KVT-->>R: 命中/tier/置信度/as-of
    R->>R: 过滤约束（D-direct/PD/混部）
    R->>R: 打分（命中/传输/队列/SLO/future/risk）
    R->>Q: 入队（优先级）
    R->>W: Dispatch(mode, node, hint)
    R->>KVT: 写 Speculative（短 TTL）
    R-->>K: 异步 KVIntent（prefetch/retain）
    W-->>R: 首 token/结果
    R-->>G: 流式返回
    G-->>C: 流式返回
```

要点：

- KVT 查询是纯内存，<5ms；miss 回退权威不在 L0 热路径（可选异步）。
- 预取 intent 不阻塞本次请求；READY 回写由 L1/摄入层处理。
- `Speculative` 只用于同批请求的亲和，不参与 D-direct。

---

## 2. L0：D-direct / PD / 混部分支

```mermaid
flowchart TB
    S["请求"] --> H["KVT.Match"]
    H --> D{"连续 HBM 命中 ≥ 阈值<br/>且置信度 ≥ 0.9?"}
    D -->|"是"| DD["D_DIRECT<br/>残差 prefill + decode"]
    D -->|"否"| P{"Pool 命中且 BW ≥ BW*?"}
    P -->|"是"| PD["PD_DISAGG<br/>P 产出 → 传输 → D"]
    P -->|"否"| CO["COLOCATED<br/>同节点完成"]
    DD --> O["下发"]
    PD --> O
    CO --> O
```

---

## 3. L1：预取 → READY → 回写

```mermaid
sequenceDiagram
    participant R as Router
    participant K as kvcm
    participant M as Mooncake
    participant W as 目标实例
    participant KVT as KVT

    R->>K: SubmitIntent(PREFETCH, P→B, deadline, budget)
    K->>K: 幂等/前缀闸/预算检查
    K->>M: create_copy_task(keys, B.seg)
    M->>W: 派任务给源 seg client
    W->>M: RDMA 复制
    M-->>K: CopyEnd
    K->>M: QueryTask + 核验副本（全 rank）
    M-->>K: Ready
    K-->>R: READY / 补发 BlockStored
    R->>KVT: 写 Confirmed(HBM, B)
    Note over R,K: 超时：abandon + 按最新 KVT 重算
```

---

## 4. L1：会话停顿 → 保留 → 恢复

```mermaid
sequenceDiagram
    participant A as Agent
    participant R as Router
    participant K as kvcm
    participant M as Mooncake

    A->>R: POST(pause, session S, eta)
    R->>R: 计算保留价值 Net(T)
    R->>K: RETAIN(S, TTL, class)
    K->>K: 注册 keepalive（有界）
    loop TTL/3
        K->>M: ExistKey/BatchExistKey（续租）
    end
    A->>R: 恢复请求
    R->>R: KVT.Match：S 的前缀仍在 B
    R->>A: 路由到 B / D-direct
    A->>R: Close
    R->>K: RELEASE(S)
    K->>K: 停 keepalive → lease 到期 → 池驱逐兜底
```

要点：

- 默认软释放（停 keepalive），不是立即 Remove；
- 保留有 TTL/字节上限；到期未用要能回收；
- 恢复时如果前缀不在，按普通请求走，不阻塞。

---

## 5. L2：扩容 + warm gate

```mermaid
sequenceDiagram
    participant L2 as Router L2
    participant O as 外部编排
    participant N as 新实例
    participant K as kvcm
    participant R as Router L0
    participant KVT as KVT

    L2->>L2: 预测需求 / 选池 / 预算约束
    L2->>O: ScaleOut(pool, +1)
    O->>N: provision + load weights
    N->>R: register(warming)
    L2->>K: 预热 hot/hook 前缀到 N
    K->>K: copy + verify
    K-->>KVT: READY / BlockStored
    KVT-->>L2: coverage
    alt coverage ≥ W_min 或 deadline
        L2->>R: ready，route_weight=1
    else 未达标
        L2->>R: 低权重/延迟加入
    end
```

---

## 6. L2：缩容 + drain

```mermaid
sequenceDiagram
    participant L2 as Router L2
    participant R as Router L0
    participant K as kvcm
    participant M as Mooncake
    participant O as 外部编排

    L2->>L2: victim = argmax(gain − loss)
    L2->>R: mark draining(victim)
    R->>R: 停止新请求；在途继续
    L2->>K: DrainPlan(victim, targets, deadline)
    K->>M: CreateDrainJob / move / retain
    loop 进度
        K->>K: 查询/重试/verify
    end
    alt 成功
        K-->>L2: DRAINED
        L2->>O: remove(victim)
    else 卡住/失败
        K-->>L2: 部分失败
        L2->>R: 保留 draining，下周期重试
    end
```

要点：

- victim 不是 LIFO；看 unique hot KV、会话风险、迁移成本；
- drain 卡住不硬删；
- 迁移失败按最新状态重算，不写 fallback 链。

---

## 7. L2：池压 → 降层/扩池

```mermaid
sequenceDiagram
    participant M as Mooncake
    participant K as kvcm
    participant L2 as Router L2
    participant R as Router L0
    participant KVT as KVT

    M-->>K: 水位/驱逐事件（或 admin 查询）
    K-->>L2: PoolCapacity
    L2->>L2: 判断：冷前缀多 / 容量真不够
    alt 冷前缀多
        L2->>K: DEMOTE/停保活（软路径）
        K->>K: 等驱逐/落盘
        K-->>KVT: 更新 tier
        R->>R: 不再 D-direct，改传输/重算
    else 容量真不够
        L2->>L2: 扩 KV 池 / 加 DRAM 节点
    end
```

---

## 8. 失败：ghost / miss 反馈

```mermaid
sequenceDiagram
    participant R as Router
    participant KVT as KVT
    participant W as 实例 B
    participant K as kvcm

    KVT-->>R: B 有前缀 P（Confirmed）
    R->>W: Dispatch(P)
    W-->>R: 本地 miss（副本不在了）
    R->>KVT: MISS_FEEDBACK(P, B)
    KVT->>KVT: B 的 loc → Stale/删除
    R->>K: VERIFY(P, B)（抽样/热条目）
    K-->>KVT: LOST / OK
    R->>R: 本次走远端读/重算；下次换节点
```

要点：

- ghost 只损性能；
- miss 反馈是最便宜的反熵；
- 不要把“查询失败”当成“副本丢失”。

---

## 9. 多 Router：L2 leader

```mermaid
sequenceDiagram
    participant R1 as Router-1
    participant R2 as Router-2
    participant E as etcd
    participant O as 外部编排
    participant K as kvcm

    R1->>E: 抢 lease /lake/scheduling/leader
    R2->>E: 抢 lease（失败，standby）
    loop leader 任期
        R1->>R1: L2 决策
        R1->>O: ScaleDecision
    end
    E--xR1: lease 过期
    R2->>E: 抢到 lease
    R2->>R2: 从最新 KVT/价格继续
    R1-->>K: 仍可发 KVIntent（幂等）
    R2-->>K: 仍可发 KVIntent（幂等）
```

---

## 10. 联合场景：Agent 多轮全链路

```mermaid
sequenceDiagram
    participant A as Agent
    participant G as Gateway
    participant R as Router
    participant K as kvcm
    participant W as vLLM-Ascend
    participant M as Mooncake

    A->>G: PRE_ANNOUNCE(S, P, B, eta 30s)
    G->>R: hook
    R->>R: KVT：P 在 A，B 无
    R->>K: PREFETCH(P→B) + RETAIN
    K->>M: create_copy_task
    M-->>K: CopyEnd
    K-->>R: READY
    R->>R: KVT：B.HBM Confirmed

    A->>G: 请求（P + 新内容）
    G->>R: request
    R->>R: Match：B 连续 HBM 命中
    R->>W: Generate(D_DIRECT, B)
    W-->>R: 首 token

    A->>G: POST(pause)
    G->>R: hook
    R->>K: RETAIN(TTL)
    Note over K: keepalive 续租

    A->>G: 恢复请求
    G->>R: request
    R->>W: 路由到 B（命中）
```

---

## 11. 读图要点

- **L0 不阻塞**：所有 KV intent 都是异步；请求走当前最优路径。
- **READY 是回写事件**：KVT 的 `Confirmed` 以 kvcm READY/引擎事件为准，不以 intent 提交为准。
- **L2 只出决策**：provision 在外部编排；kvcm 只执行 KV 搬迁。
- **失败不 fallback**：按最新 KVT/价格重算；读路径兜底不叫 fallback。
- **多 Router**：请求环可水平扩；L2 单 leader；intent 幂等。
