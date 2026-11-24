# Sleep Mode 与 P/D/混部角色切换

> 目标：同节点同一个 NPU 上预置 P、D、混部三个 vLLM-Ascend 引擎，两个 sleep、一个 active；切换只切 active 引擎，不重启进程。
> 范围：只验证引擎侧可行性，不先写进 kvcm 设计。

## 0. 可行性结论

| 能力 | 结论 | 依据 |
|---|---|---|
| 三形态（P TP8 / D DP8 / 混部 DP2TP4）共用 8 卡，两个 sleep | 可行，但必须串行 | sleep 释放权重/KV；同一时间只能一个 active |
| 三个引擎共用本机 Mooncake master/池 | 可行 | vLLM-Ascend `pd_colocated_mooncake_multi_instance`：两实例共用 master、`MooncakeConnectorStoreV1`、`kv_both`，跨实例 KV 复用 |
| level 2 + `sleep_mode_extra_cleanup=false` 保留 ACL 图 | 可行 | `sleep_mode_extra_cleanup=true` 才清 graph workspace/重新 capture |
| sleep 前写穿 Mooncake，wake 后读 KV | 可行 | Mooncake connector 在引擎进程内；sleep 只释放 NPU |
| 权重用 host 一份 / 磁盘回填 | 可行，需控制器 | level 2 丢弃 weights；wake 后 H2D 或加载 |
| 同一 NPU 上 engine→engine 权重 D2D | **不可行** | 源和目标不能同时持有完整权重 |
| 跨 NPU/跨机权重 D2D | **无现成功能** | `weight_transfer` 是 trainer→worker；`Netloader` 是启动加载；`RFork` 在 sleep 下 bypass |
| 不同 TP/DP 直接 D2D 权重 | **不可行** | 源和目标 shard 布局不同；需要 reshard 或完整权重副本 |

结论不是“整套方案不行”，而是：

- 引擎切换、KV 共用、ACL 图保留：成立；
- 权重恢复：host/磁盘路径成立；
- engine→engine 权重 D2D：需要自研，且不同 TP/DP 不能只靠传输。

## 1. sleep mode 的实际行为

参考：

- <https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/sleep_mode.html>
- `vllm_ascend/device_allocator/sleep_mode_backend.py::CaMemBackend`
- `vllm_ascend/device_allocator/camem.py::CaMemAllocator`
- `vllm_ascend/worker/worker.py::Worker.sleep` / `wake_up`

已核实：

- `level=1`：offload `weights` 到 host，其余 tag（含 KV cache）discard；
- `level=2`：weights 和 KV cache 全部 discard；
- 只处理 NPU memory pool，不 unmount Mooncake connector/segment，不释放进程 host DRAM；
- level 2 只保留 `sleep_preserve_parameter_names` 指定的参数和 buffers，不保留整套权重；
- 进程不退出时，Mooncake connector/segment 仍在进程内。

## 2. 三形态分时复用（8 卡示例）

一台 8 卡机器上部署三个独立形态：

| 形态 | 并行 | 占卡 |
|---|---|---|
| P | TP8 | 8 |
| D | DP8（每 DP 副本 TP1） | 8 |
| 混部 | DP2 × TP4 | 8 |

三个形态各自是独立部署/进程组，不是一个进程：

- P TP8：8 个 rank；
- D DP8：8 个 DP rank；
- 混部 DP2TP4：2 个 DP 组 × 4 个 TP rank。

同一时间只有一个形态占卡、接流量；另外两个 sleep。

```text
P 引擎       (sleep)  ─┐
D 引擎       (sleep)  ─┼─ 同一台机器的 8 张卡
混部引擎     (active) ─┘
```

- 目标形态要 wake，源形态必须先 sleep 释放 8 张卡；
- 三个形态各带 Mooncake connector，接同一个本机 Mooncake master/池；
- sleep 只释放 NPU，不动 host 侧 connector/segment。

### 2.1 权重不是同一套 shard

| 形态 | 每 rank 权重 |
|---|---|
| P TP8 | 1/8 模型 |
| D DP8 TP1 | 完整模型（8 个副本各一份） |
| 混部 DP2TP4 | 1/4 模型 |

所以同机三形态不能靠“把一份 NPU 权重拷过去”解决；需要一份**完整权重**（host/磁盘），唤醒时按目标形态切 shard。

### 2.2 KV 也不是同一套 layout

| 形态 | 每请求 KV |
|---|---|
| P TP8 | 8 份 TP shard |
| D DP8 TP1 | 每个 DP rank 一份完整 KV |
| 混部 DP2TP4 | 每 DP 组 4 份 TP shard |

共用 Mooncake 池可以；跨形态读要 connector 做 TP/DP rank 映射，不能假设同一个 Store 对象直接可读。

### 2.3 保留 ACL 图的内存代价

`sleep_mode_extra_cleanup=false` 保留 ACL 图，但也保留 graph workspace/HCCL 相关状态。

三个形态各占 8 卡、两个 sleeping 时：

- 两个 sleeping 形态的 weights/KV 已释放；
- graph workspace/HCCL context 仍占 NPU 内存；
- active 形态还要放完整权重 + KV。

所以能不能同时驻留三个形态，取决于 sleeping 形态保留的 graph/HCCL 内存是否放得下。放不下就只能：

- `sleep_mode_extra_cleanup=true`，wake 后重新 capture；
- 或减少常驻形态数。

这是要实测的容量约束，不是配置开关本身能保证的。

## 3. 切换流程

同 NPU 上不能并存两个完整引擎，切换必须串行：

```text
1. 源引擎摘流，等在途请求结束
2. 源引擎 KV 写穿 Mooncake，等 READY
3. 源引擎 sleep，释放 NPU
4. 目标引擎 wake?tags=weights
5. 目标引擎从 host 权重副本 H2D 回填
6. 目标引擎 wake?tags=kv_cache
7. 目标引擎确认 Mooncake KV 可读
8. 路由切到目标引擎
```

同 NPU 上无法做 engine→engine 权重 D2D：源引擎 sleep 前目标不能持有完整权重，源 sleep 后权重已经丢弃。D2D 只能来自另一个 NPU/另一台机器的 seed。

## 4. 多引擎共用 Mooncake 池

vLLM-Ascend 已有同类部署：

- [PD-Colocated with Mooncake Multi-Instance](https://github.com/vllm-project/vllm-ascend/blob/main/docs/source/tutorials/features/pd_colocated_mooncake_multi_instance.md)
- 两个 vLLM 实例，各占 4 张 NPU；
- 共用同一个 `mooncake_master`；
- 两边都用 `MooncakeConnectorStoreV1`，`kv_role=kv_both`；
- 跨节点、跨实例 KV 可复用。

所以：

- **共用池**：成立，和部署形态无关；
- **Store 直读**：TP/DP/layout 一致时，可读同一个 KV 对象；
- **Connector 拉取**：TP/DP/layout 不同时，由 connector 做 rank 映射/搬运。

这解决的是 KV，不解决权重。

## 5. 成立条件

1. 三个引擎同模型、同 revision；Store 直读要求同 TP/DP/layout，不同 layout 由 connector 映射。
2. 三个引擎共用同一个 NPU；同一时间只有一个占 NPU、接流量。
3. 源引擎必须先 sleep，目标引擎才能 wake；同 NPU 不能同时持有两个完整引擎。
4. 三个引擎进程都不退出；否则进程内 Mooncake segment 消失。
5. 控制器负责摘流、KV 写穿/READY、权重回填、sleep/wake 顺序、路由切换。

## 6. 权重与 ACL 图

### 6.1 level 2 权重

- level 2 丢弃全部 weights 和 KV cache；
- wake 顺序：`wake_up?tags=weights` → 权重回填 → `wake_up?tags=kv_cache`；
- 权重来源必须提前准备，不能等 wake 后再从本地盘慢慢读。

### 6.2 ACL 图

关闭 `sleep_mode_extra_cleanup` 就是保留 ACL 图。

- `false`（默认）：不清 graph workspace，不重置 graph params；
- `true`：sleep 时清 workspace，wake 后重新 capture；
- 权重必须回填到原参数地址/布局，图才能直接复用；
- 模型结构、TP/DP 布局变化，必须重新 capture。

### 6.3 权重来源对比

| 来源 | 现状 | 适用 | 限制 |
|---|---|---|---|
| host 完整权重一份 | 可行，主路径 | P TP8 / D DP8 / 混部 DP2TP4 | host 内存；按目标形态 H2D/shard |
| 磁盘/3FS | 可行 | fallback/冷启动 | 慢 |
| 同 NPU engine→engine D2D | **不可行** | — | 源和目标不能同时驻留 |
| 跨 NPU/跨机 D2D | **无现成功能** | 自研 | 需要另一个 seed；不同 TP/DP 要 reshard |
| `weight_transfer_config hccl/ipc` | 有，但 trainer→worker | RL 权重更新 | 不是 engine→engine |
| `Netloader` | 有，启动加载 | 新实例启动 | 不是 level-2 wake 后路径 |
| `RFork` | 有，seed 拉权重 | 冷启动 | sleep mode 下 bypass |

### 6.4 为什么不同 TP/DP 不能直接 D2D

权重按 TP/DP 分片：

- 源引擎 TP=8，每个 rank 持有 1/8；
- 目标引擎 TP=4，需要不同的 1/4 分片；
- 直接把源 rank 的字节传给目标 rank，布局对不上；
- 需要 gather/merge/reshard，或保留一份完整（未分片）权重再按目标布局加载。

KV 有 connector 做 rank 映射；权重没有等价的现成 connector。

## 7. 角色切换的边界

- 切的是“哪个引擎接流”，不是“同一个进程换角色”；
- 三个引擎共用一个 NPU，同一时间只能一个占 NPU；
- 同进程内切 P/D/混部需要引擎侧对称调度，sleep 不提供；
- 有 sleep/wake、权重回填、KV 回填延迟，不承诺零中断；
- vLLM 只提供单引擎 sleep/wake，三引擎编排要外部控制器；
- 进程退出/重启会丢进程内 Mooncake segment。

## 8. 失败场景

| 失败 | 后果 | 处理 |
|---|---|---|
| 源引擎未 sleep 就 wake 目标 | NPU 不足 | 先 sleep 源，再 wake 目标 |
| 不同 TP/DP 直接拷权重 | 布局错/结果错 | reshard 或完整权重副本 |
| sleep 前未写穿 Mooncake | wake 后 KV 为空 | 强制 flush + READY |
| 目标引擎 wake 时 NPU 资源未就绪 | 启动失败 | 预分配/串行切换 |
| 进程退出 | Mooncake segment 消失 | 复制到其他 segment/SSD |
| 路由仍打 sleeping 引擎 | 请求失败 | 路由状态跟随 sleep/wake |

## 9. 探索通过后，kvcm 加什么

如果验证通过，kvcm 加的是**形态转换**，不是新的 KV 操作：

- D8 决定 P/D/混部角色下发给哪些引擎；
- D8 维护 active/sleeping 状态，触发 sleep/wake，和路由联动；
- KVT 增加 engine 角色状态；
- D9 记录切换审计、耗时、失败回切、KV 回填命中率；
- KV 复制/保留仍走现有 D2 操作。

边界：

- kvcm 不实现引擎内部调度，也不实现 sleep/wake；
- kvcm 只做“选角色、发切换、看结果”；
- 形态转换先由 vLLM-Ascend 侧验证，通过后再进 D8。

## 10. 验收

1. 同一个 NPU 上三个引擎可以逐个初始化并 sleep。
2. 同一时间只有一个引擎占 NPU、接流量。
3. sleep 前写穿的 KV，wake 后能命中。
4. level 2 + `sleep_mode_extra_cleanup=false` 下 ACL 图保留，权重回填后可直接复用。
5. host 权重副本回填路径可用；H2D 时间在切换预算内。
6. 同 NPU 场景不依赖 engine D2D；跨 NPU/跨机 D2D 只作为额外优化。
7. 路由不会把请求发给 sleeping 引擎。
