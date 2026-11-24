# Sleep Mode 与 KV 持久化

> 目标:让引擎在 sleep 时只释放 NPU/GPU 资源，KV 继续留在 host DRAM / Mooncake；wake 或角色重配后还能复用，而不是重算。
> 适用:vLLM-Ascend 的 `enable_sleep_mode` / `sleep` / `wake_up`；Mooncake Store 作为外部 KV 池。

## 0. 结论

1. **单个引擎的 sleep mode 不是 P/D/混部切换。** 它释放 NPU 内存并丢弃 NPU 侧 KV，不改变这个引擎的调度角色；预置多个角色引擎时，sleep/wake 可以做部署级切换，见 §5.1。
2. **要让 KV 活着，KV 的所有权必须独立于引擎进程生命周期。** 引擎可以 sleep、退出、换角色；Mooncake master 和 host segment 不能跟着退出。
3. **推荐结构是“引擎无状态 + Mooncake 持久池 + kvcm 管理”**：sleep 前把 KV 写进 Mooncake 并确认 READY；sleep 只释放 NPU；wake 后从 Mooncake 回填。
4. **P/D/混部切换有三种**：
   - **预置 P/D/混部引擎 + sleep/wake + 路由**：部署级切换，可行；不是同进程切角色；
   - **停旧引擎、起新角色**：同一条路的冷启动版本，KV 靠 Mooncake；
   - **同一进程内切角色**：sleep 不够，需要引擎侧对称调度。

## 1. 事实基线

### 1.1 vLLM-Ascend sleep mode 做什么

- `enable_sleep_mode=True` 后，`sleep(level)` 把模型权重 offload 出 NPU，并丢弃 KV cache；
- `wake_up()` 把权重加载回来；
- level 1 / level 2 的区别是释放范围；单个引擎 sleep 不改变角色，多引擎池的切换见 §5.1；
- sleep 期间引擎不处理请求，路由要把请求摘走。

参考:

- vLLM-Ascend Sleep Mode: https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/sleep_mode.html
- vLLM `sleep` / `wake_up` 入口: `3rdparty/vllm/vllm/v1/worker/gpu_worker.py::GPUWorker.sleep` / `wake_up`；`vllm/device_allocator/sleep_mode_backend.py::SleepModeBackend`

### 1.2 Evolving Infra 的切换不是 sleep

Evolving Infra 的 P/D 切换来自引擎侧对称调度:

- 一套调度循环同时支持 P 和 D；
- 路由器按请求选择 P 腿/D 腿；
- decode 侧在 prefill 过程中按 chunk 拉 KV；
- KV 在 host memory 池，切角色不丢。

代码事实:

- `evolving_infra/engine/decoupled_engine/srt/disaggregation/symmetric/`
- `evolving_infra/engine/CHANGES.md`：相对 SGLang，85 个新文件、140 个修改文件

所以不能用 `sleep` 代替对称引擎。

## 2. 约束

### 2.1 sleep 的边界

- 只释放 NPU/GPU 内存；
- 不保证 host DRAM 里的引擎私有 KV 缓存还可用；
- 不改变 vLLM 的 P/D 角色、scheduler、connector；
- 进程退出后，进程内 Mooncake client 挂载的 segment 也会消失。

### 2.2 KV 的边界

- Mooncake master 是副本权威；
- Mooncake host segment 是字节载体，不是元数据权威；
- 如果 segment 所在的进程退出，数据可能随 segment 消失；
- 只有 master 元数据没有用，必须有可读的 segment 副本。

### 2.3 角色切换的边界

- 同一进程内 P/D/混部切换：需要引擎侧调度改造；
- 停旧引擎、起新角色：sleep 只能减少停机时间，不能避免重配；
- 无论哪种，KV 必须在引擎生命周期之外有副本。

## 3. 推荐结构

```text
                 ┌────────────────────┐
                 │ Mooncake master    │  副本权威
                 └─────────┬──────────┘
                           │
        ┌──────────────────┴──────────────────┐
        │                                     │
┌───────▼────────┐                   ┌────────▼───────┐
│ KV Node /      │                   │ KV Node /      │
│ host segment   │                   │ host segment   │
│ (独立进程)      │                   │ (独立进程)      │
└───────▲────────┘                   └────────▲───────┘
        │                                     │
        │        ┌──────────────────┐         │
        └────────│ vLLM-Ascend 引擎 │─────────┘
                 │ sleep / wake     │
                 └──────────────────┘
```

关键点:

- Mooncake master 不随引擎 sleep；
- host segment 由独立进程/agent 持有，不放在引擎进程里；
- 引擎只是 Mooncake client，sleep 不影响 segment；
- 引擎退出/换角色，segment 仍然可读。

## 4. 生命周期

### 4.1 sleep 前

1. 路由摘流：不再给该引擎发新请求；
2. 等在途请求完成或迁移；
3. 把引擎侧 KV 写穿/卸载到 Mooncake；
4. 等 Mooncake READY / `FinishWriteCache`；
5. 对需要保留的 KV 加 pin/lease；
6. 记录 engine epoch / segment 列表；
7. 调 `sleep(level)`。

### 4.2 sleep 期间

- 引擎进程可以继续存在，也可以退出；
- Mooncake master 和 host segment 必须独立存活；
- KV 不能被 TTL/GC 清掉；
- 路由把该引擎标记为 sleeping/unavailable；
- kvcm/KVT 记录副本仍在哪、是否被 pin。

### 4.3 wake / 重配后

1. `wake_up()` 或启动新角色引擎；
2. 重新挂载/发现 Mooncake segment；
3. 用 master 查询副本，验证可读；
4. 回填 KVT/READY；
5. 路由恢复流量；
6. 解除不再需要的 pin/lease。

## 5. 角色切换

### 5.1 预置引擎池 + sleep/wake

这是“三个引擎、两个 sleep”的方案：P 引擎、D 引擎、混部引擎预先启动，同一时间只让一个角色醒着，路由器指向醒着的端点。

```text
P 引擎 (sleep)
D 引擎 (sleep)
混部引擎 (active)  ← 路由
```

切换到 P：

```text
混部引擎：摘流 → KV 写穿 Mooncake → sleep
P 引擎：wake → 挂 Mooncake segment → 验证 KV → 接流
```

成立条件：

- 三个引擎使用同一模型、同一 revision、同一套 key/hash 口径；
- 同一 NPU 上同一时间只允许一个引擎占满资源；sleep/wake 必须串行，不能先把目标引擎 wake 到一半再 sleep 源引擎；
- host 侧要能容纳 sleeping 引擎的权重 offload（level 1）或从磁盘重载（level 2）；
- Mooncake master 和 host segment 独立于引擎进程；否则引擎退出会丢 segment；
- 路由和 health check 必须知道 engine 的 sleeping/active 状态；
- 切换期间 KV 必须已经在 Mooncake 里 READY，不能只在本机 NPU/进程内。

这个方案能实现 P/D/混部切换，但属于**部署级切换**：

- 切的是“哪个引擎接流”，不是“同一个引擎换角色”；
- 有 sleep/wake 和 KV 回填延迟；
- 不承诺秒级，也不承诺在途请求不中断；
- 需要外部控制器编排，不是 vLLM 单机 sleep API 自动完成。

如果三个引擎分别占不同 NPU，那本来就可以直接路由切换；sleep 主要解决“同一节点/NPU 上并存多个角色引擎”的资源问题。

### 5.2 同进程内切换

要求引擎支持:

- 同一调度循环服务 P 和 D；
- 路由器按请求指定 P/D 腿；
- KV 在 prefill/decode 之间按 chunk 传输；
- 切换不重启进程。

sleep mode 不提供这些能力。

### 5.3 停旧引擎、起新角色

```text
旧 P/D/混部引擎
→ 摘流
→ KV 写穿 Mooncake
→ sleep / 退出
→ 起新角色引擎
→ 从 Mooncake 读 KV
→ 恢复流量
```

这条路可以保住 KV，但不是“秒级原地切换”；切换时间取决于引擎启动/权重加载/KV 回填。

## 6. 失败与边界

| 失败 | 后果 | 处理 |
|---|---|---|
| host segment 随引擎退出 | KV 丢失 | segment 放独立进程；或提前复制到其他 segment/SSD |
| master 还在、segment 不可读 | 元数据幽灵 | 删除/重建副本；KVT 标 stale |
| sleep 前没写穿 | wake 后无 KV | sleep 前强制 flush + READY barrier |
| pin/lease 过期 | KV 被 GC | sleep 期间续租或延长 TTL |
| 角色切换后 key 空间变化 | 命中错 | 用同一 model revision/block hash；校验 epoch |
| sleep 后请求仍打到该引擎 | 失败/超时 | 路由状态与 sleep 状态联动 |

## 7. 验收

1. sleep level 1/2 后，Mooncake master 和 host segment 仍可读；
2. sleep 前写入的 KV，wake 后仍能命中，不重算；
3. 引擎进程退出再起，segment 由独立进程持有，KV 仍可读；
4. P/D 角色重配后，KV 仍可读；
5. segment 故障时，KVT 能标记 stale，请求回退重算；
6. pin/lease 能覆盖 sleep 窗口，TTL 不误删。

## 8. 待确认

- vLLM-Ascend sleep 是否触碰 host DRAM，还是只释放 NPU；需要在目标版本实测。
- Mooncake client 是否能在独立进程挂 segment，还是必须与引擎同进程。
- vLLM-Ascend 是否支持同进程 P/D 角色切换；若不支持，5.1 不成立。
- sleep 期间 KV 的 lease/TTL 如何与 kvcm keepalive 对齐。

## 9. 参考

- vLLM-Ascend Sleep Mode: https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/sleep_mode.html
- vLLM `GPUWorker.sleep` / `wake_up`: `3rdparty/vllm/vllm/v1/worker/gpu_worker.py`
- vLLM `SleepModeBackend`: `3rdparty/vllm/vllm/device_allocator/sleep_mode_backend.py`
- Evolving Infra 对称引擎: `3rdparty` 外仓库 `mit-han-lab/EvolvingInfra` 的 `evolving_infra/engine/decoupled_engine/srt/disaggregation/symmetric/`
- Mooncake 副本/segment: `3rdparty/mooncake/mooncake-store/include/master_client.h::GetReplicaList`、`MountSegment`
