# Sleep Mode 与 P/D/混部角色切换

> 场景：一台 8 卡机器，预置 P TP8、D DP8、混部 DP2TP4 三种形态，分时复用同一组 8 张卡。
> 目标：控制面发起 sleep 当前形态、wake 目标形态；目标从另一台同形态实例 RD2D 拉权重，KV 继续用本机 Mooncake 池。
> 范围：引擎侧可行性探索，不先写进 kvcm 设计。
> 源码：`3rdparty/vllm-ascend` @ `63800d26`（2026-11-28）。

## 0. 结论

方案成立，分三块：

1. **本机 8 卡分时复用**：三种形态各自是独立部署/进程组，同一时间只有一个形态占卡、接流量；另外两个 sleep。
2. **KV**：三种形态的 Mooncake connector 接同一个本机 `mooncake_master`/池；源形态 sleep 前写穿，目标 wake 后读同一个池。
3. **权重**：目标形态从另一台**同形态**实例做 rank-to-rank RD2D，一把并行加载；不需要 reshard，不需要本地盘。

需要工程补齐的部分：

- 本机三形态的 sleep/wake 编排；
- 把 `Netloader` 的 HCCL P2P 加载接到 L2 wake 后触发；
- 共享 Mooncake 的连接器配置；
- ACL 图常驻带来的显存预算。

## 1. sleep mode 的行为

参考：

- <https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/sleep_mode.html>
- `3rdparty/vllm-ascend/vllm_ascend/device_allocator/sleep_mode_backend.py::CaMemBackend`
- `3rdparty/vllm-ascend/vllm_ascend/device_allocator/camem.py::CaMemAllocator`
- `3rdparty/vllm-ascend/vllm_ascend/worker/worker.py::Worker.sleep` / `wake_up`

核实结果：

- `level=1`：offload `weights` 到 host，其余 tag（含 KV cache）discard；
- `level=2`：weights 和 KV cache 全部 discard；
- 只处理 NPU memory pool，不 unmount Mooncake connector/segment，不释放进程 host DRAM；
- level 2 只保留 `sleep_preserve_parameter_names` 指定的参数和 buffers，不保留整套权重；
- 进程不退出时，Mooncake connector/segment 仍在进程内。

因此 level 2 下：

- ACL 图可以保留（`sleep_mode_extra_cleanup=false`）；
- 权重必须重新加载/传输；
- KV 必须提前写穿 Mooncake。

## 2. 本机三形态

| 形态 | 并行 | 每 rank 权重 | 占卡 |
|---|---|---|---|
| P | TP8 | 1/8 | 8 |
| D | DP8，每 DP 副本 TP1 | 完整模型 | 8 |
| 混部 | DP2 × TP4 | 1/4 | 8 |

三个形态各自是独立部署/进程组：

- P TP8：8 个 rank；
- D DP8：8 个 DP rank；
- 混部 DP2TP4：2 个 DP 组 × 4 个 TP rank。

同一时间只有一个形态占卡、接流量。

```text
P 引擎       (sleep)  ─┐
D 引擎       (sleep)  ─┼─ 同一台机器的 8 张卡
混部引擎     (active) ─┘
```

源形态必须先 sleep，目标形态才能 wake，因为二者不能同时在这 8 张卡上持有完整权重。

## 3. 权重：同形态跨机 RD2D

### 3.1 原则

目标形态不找任意 P/D，只找**另一台机器上的同形态实例**：

- P TP8 ← 远端 P TP8；
- D DP8 ← 远端 D DP8；
- 混部 DP2TP4 ← 远端混部 DP2TP4。

同形态意味着同 TP/DP/layout，权重 shard 一一对应，不需要 reshard。

### 3.2 传输方式

P TP8 → P TP8：

- donor 8 个 rank，各持 1/8；
- 目标 8 个 rank，各从对应 donor rank 拉 1/8；
- rank-to-rank 并行加载。

D DP8 TP1 → D DP8 TP1：

- 每个 donor DP rank 有完整权重；
- 每个目标 DP rank 从对应 donor rank 拉完整权重。

混部 DP2TP4 → 混部 DP2TP4：

- 每个目标 TP rank 拉同 rank 的 1/4；
- DP 组内映射一致。

结果：

> 目标所有 rank 并行拉，一把加载完；不经过本地盘，不做跨形态 reshard。

### 3.3 Ascend 对应实现：Netloader

vLLM-Ascend 已经有同形态 HCCL P2P 权重加载：

- `3rdparty/vllm-ascend/vllm_ascend/model_loader/netloader/netloader.py::ModelNetLoaderElastic`
- `3rdparty/vllm-ascend/vllm_ascend/model_loader/netloader/executor/elastic_load.py::P2PSend`
- server 预加载模型；
- client 校验模型/partition 一致；
- client 通过 HCCL 按相同 partition 直接接收权重；
- 不读本地盘。

这就是我们需要的同形态 RD2D 传输。
P TP8 → P TP8、D DP8 → D DP8、混部 DP2TP4 → 混部 DP2TP4，都可以按相同 rank 直接收。

当前差距只有一个：

- `Netloader` 的入口是 `--load-format=netloader`，在**引擎启动时**加载；
- 我们的场景是 level 2 sleep 后 `wake?tags=weights` 再触发加载；
- 需要把 Netloader 的 send/recv 接到 wake 后的加载路径，并补 donor 选择/控制面。

不需要重新造传输。

其他路径：

- `weight_transfer_config` 是 trainer → worker 的权重更新，不是 P/D 引擎之间的 donor 协议；
- `RFork` 是 seed 加载，sleep mode 下被 bypass。

结论：

> **Ascend 有同形态 P2P 权重加载，核心就是 Netloader；缺的是把它接到 L2 wake，而不是缺传输。**

### 3.4 前提

- donor 和目标同模型、同 revision；
- 同 TP/DP/layout；
- donor 节点 alive，且不被 sleep；
- 目标先 `wake?tags=weights`；
- 所有 rank 拉完并 barrier；
- 再 `wake?tags=kv_cache`；
- 本地源形态在目标 ready 之前保持不接流量，完成后 sleep。

### 3.5 fallback

- donor 不可用：host 完整权重 + H2D；
- host 也没有：本地盘/3FS，只做 fallback。

不要用本地盘做主路径。

## 4. KV：本机 Mooncake 池

三个形态各带 Mooncake connector，接同一个本机 `mooncake_master`/池。

vLLM-Ascend 已有同类部署：

- [PD-Colocated with Mooncake Multi-Instance](https://github.com/vllm-project/vllm-ascend/blob/main/docs/source/tutorials/features/pd_colocated_mooncake_multi_instance.md)
- 两个 vLLM 实例共用同一个 `mooncake_master`；
- 都用 `MooncakeConnectorStoreV1`，`kv_role=kv_both`；
- 跨实例 KV 可复用。

本方案沿用这个模型：

- 源形态 sleep 前把 KV 写穿 Mooncake，等 READY；
- 目标形态 wake KV 后从同一个池读；
- 形态间 TP/DP layout 不同时，由 connector 做 rank 映射；
- 不把 KV 和权重 donor 绑在一起，KV 走 Mooncake，权重走 RD2D。

## 5. 切换流程

```text
1. 控制面选择目标形态
2. 源形态摘流，等在途请求结束
3. 源形态 KV 写穿 Mooncake，等 READY
4. 源形态 sleep，释放 8 卡
5. 目标形态 wake?tags=weights
6. 目标 8 个 rank 从远端同形态 donor RD2D 拉权重
7. 所有 rank barrier
8. 目标形态 wake?tags=kv_cache
9. 目标形态确认 Mooncake KV 可读
10. 路由切到目标形态
```

失败时回到源形态；不设 mode-to-mode 的降级链，按控制面重新选择。

## 6. ACL 图

- `sleep_mode_extra_cleanup=false`（默认）：不清 ACL graph workspace，不重置 graph params；
- `sleep_mode_extra_cleanup=true`：sleep 时清 workspace，wake 后重新 capture；
- 权重回到原参数地址/布局，图可以直接复用；
- 模型结构或 TP/DP 改变，必须重新 capture。

保留 ACL 图的代价：

- 两个 sleeping 形态的 weights/KV 已释放；
- graph workspace/HCCL context 仍占 NPU；
- active 形态还要放完整权重 + KV。

放不下时只能 `sleep_mode_extra_cleanup=true`，或减少常驻形态数。

## 7. 成立条件

1. 三个形态同模型、同 revision；同形态 donor 同 TP/DP/layout。
2. 三形态共用本机 8 卡；同一时间一个 active。
3. 源形态先 sleep，目标才能 wake。
4. 远端同形态 donor 保持 alive。
5. 三个本地进程不退出；否则进程内 Mooncake segment 消失。
6. 控制面负责摘流、KV 写穿、sleep/wake、权重 RD2D、路由切换、失败回切。

## 8. 不做的事

- 不做本机 engine→engine 权重 D2D：三形态不能同时占 8 卡；
- 不做任意 P/D 跨形态权重 D2D：shard 布局不同，需要 reshard；
- 不把本地盘作为主路径；
- 不把 `weight_transfer_config` 当成 P/D donor 协议；
- 不让 kvcm 实现引擎内部 sleep/wake；
- 不重新造 P2P 权重传输，复用 Netloader。

## 9. 探索通过后，kvcm 加什么

如果验证通过，kvcm 加的是**形态转换**，不是新的 KV 操作：

- D8 决定 P/D/混部角色下发给哪些引擎；
- D8 维护 active/sleeping 状态，触发 sleep/wake，和路由联动；
- KVT 增加 engine 角色状态；
- D9 记录切换审计、耗时、失败回切、KV 回填命中率；
- KV 复制/保留仍走现有 D2 操作。

边界：

- kvcm 只做“选角色、发切换、看结果”；
- 执行在引擎/编排层；
- 形态转换先由 vLLM-Ascend 侧验证，通过后再进 D8。

## 10. 验收

1. 同一台 8 卡机器上三形态可以逐个初始化并 sleep，只有一个 active。
2. 源形态 sleep 后，进程内 Mooncake connector/segment 仍在。
3. 源形态写穿的 KV，目标 wake 后能命中。
4. level 2 + `sleep_mode_extra_cleanup=false` 下 ACL 图保留。
5. 目标从远端同形态 donor RD2D 加载权重，所有 rank 一把完成。
6. 同形态 donor 的 TP/DP/layout 不匹配时拒绝加载，不静默错位。
7. 路由不会把请求发给 sleeping 形态。
