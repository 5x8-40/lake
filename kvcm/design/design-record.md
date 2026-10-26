# E11 进程化设计记录（临时文档）

> **性质**：E11 开工的阶段 0 决策底稿。决策经用户拍板（2026-10-26）即生效；
> 正式表述由文档负责人回填进 [code-architecture.md](code-architecture.md) 后，
> **本文件删除**。
>
> 依据：code-architecture.md（#69 回填版）「与 E10/E11/E15 的对位」节只给了方向
> （进程壳 + TaskEngine + 三 Loop 常驻化），以下六项是方向之内、代码之前的决策。

## 决策一：线程模型

主线程管信号与退出 + **任务工作线程池**执行流程。

- PrefetchFlow 的同步 `run()` 直接进池，**不拆** submit/poll/收口三段——「流程
  本体不变」的最低成本实现；轮询仍在流程内，TaskEngine 不管单任务推进
- Ledger 并发：**调用侧加锁**（一把锁包住全部 Ledger 调用）。不选每线程一连接：
  sqlite 写入本就串行，多连接只徒增一致性死角
- backend 共享态（`_plans`/`_watch`）：**不加锁**，靠纪律约束——只在工作线程内
  访问，Ledger 是跨线程权威。约束写进代码注释，违反即 bug
- 现状注意：backend/client 零锁（单线程设计），本决策是 E11 的并发边界定义，
  不是给现有类补锁

## 决策二：TaskEngine 形态（轻量）

intent 队列 + 并发上限（默认 4，参数化）+ 工作线程池执行 `PrefetchFlow.run` +
终态记账。任务状态机（排队/进行/READY/失败）= 队列位置 + Ledger `running` 迁移
的组合，不另建状态存储。限速器**只留接口不实现**（E15 接入点：提交前询问）。
失败态策略三选（重发 / 弃置清副本 / 源掉线衔接）本期仍只留接口——超时收口
（abandon）已有，重发策略随 E12 intent 语义一起定。

## 决策三：保活占位策略（D3 未定项的临时默认）

**固定窗口**：副本 READY 后 KeepaliveLoop 续租一个可配置窗口（默认 10 分钟），
窗口到即停租注销。选它的理由：三候选（会话期/固定窗口/首流量后停租）里最保
守、最不容易写成事实 pin、后改成本最低。D3 正式定型时替换策略实现，Loop 机
制（注册表驱动的周期 `batch_is_exist`）不变。

## 决策四：优雅退出语义

SIGTERM/SIGINT → ① 停收 intent（占位入口关闭）→ ② 在途任务 **abandon**
（Ledger 记 abandoned，不等待、不阻塞退出——后端无任务取消能力 P9，副本清理
归 remove；重启后 orphan 由 rebuild 兜底）→ ③ KeepaliveLoop 停止 → ④
`StoreClient.close()`（demo 已知退出期析构竞态，显式 close 是既有结论）。

## 决策五：IntentReceiver 占位形态

进程内函数调用 + 一个最小 HTTP 端点（`POST /prefetch {model_id, token_ids,
target}`），便于手工与脚本测试。协议字段、通道（gRPC 候选）随 E12 定型后替
换——占位入口只调 TaskEngine 公开方法，替换不动引擎。

## 决策六：范围切分

- **本期做**：进程壳（启动/退出/rebuild 接线）+ TaskEngine + **KeepaliveLoop**
  （唯一常驻循环）+ IntentReceiver 占位
- **只留类骨架**：ReleaseLoop、RetainLoop——依赖的 `demote`/`retain`/`remove`
  在 L4 全是 NotImplementedError，能力接线（E10 后续/backlog）前无内容可跑
- **不做**：限速器（E15）、重发策略（随 E12）、多模型归属判定（多模型接入前
  置，已登记）、E12/E13/E14 协议

## 阶段计划

1. **阶段 1 进程壳 + 状态恢复**：Ledger 加锁 → 启动时 `rebuild()` 接线 →
   KeepaliveLoop（固定窗口策略）→ 优雅退出四步
2. **阶段 2 TaskEngine**：intent 占位入口 → 并发执行 → 终态记账 → 超时收口
   （复用现有 abandon）
3. **阶段 3 测试**：并发多 intent 单测；重启 e2e（杀进程 → rebuild → READY）；
   优雅退出回归（退出不崩、账本会收口）；真机冒烟

## 参考实现

- **SGLang HiCache** `hiradix_cache.py::can_terminate_prefetch` / prefetch 线程：
  prefetch 三策略（best_effort/wait_complete/timeout）与 timeout 预算公式
  （`base + per_ki_token * tokens/1024`）→ 我们 TaskEngine 失败态策略三选与
  PrefetchFlow 超时收口的语义对照；差异：HiCache 是引擎内 L1/L2 私有、追求
  prefill 衔接，kvcm 是池控制面、以副本核验为 READY 依据
- **TensorCast** Global Store / Store Daemon 分离 + 「CLI 管服务、worker 只
  connect、不持有基础设施进程」（`docs/research/tensorcast/overview.md`）→
  kvcm 进程壳形态的工业佐证：常驻服务生命周期显式化，与计算进程解耦
- **mooncake 任务无取消能力**（capability `has_task_cancel=NO`，P9）→ 决策四
  abandon 而非 cancel 的直接依据
