# Sleep Mode 与 P/D/混部角色切换

> 场景：一台 8 卡机器，预置 P TP8、D DP8、混部 DP2TP4 三种形态，分时复用同一组 8 张卡。
> 约束：同一时间只有一个形态 active；另外两个 sleep。KV 留在本机 DRAM/Mooncake；sleeping 形态不保留权重；ACL 图和 HCCL 组保留；权重从远端同形态实例 P2P 加载。
> 范围：引擎侧可行性探索，不先写进 kvcm 设计。
> 源码：`3rdparty/vllm-ascend` @ `63800d26`（2026-11-28）。

## 0. 结论

1. 三种形态各自是独立部署/进程组，同一时间只有一个 active，另外两个睡眠。
2. 源形态把已完成的 KV block 保存到本机 Mooncake 池；decode 增量要 `save_decode_cache=true`，并在最后一个 step 后确认保存完成。
3. 目标形态 wake，从**另一台机器上的同形态实例**通过 Netloader/HCCL rank-to-rank 拉权重。
4. 目标形态再 wake KV，从本机 Mooncake 池读。
5. `level=2` + `sleep_mode_extra_cleanup=false`：大部分 weights 丢弃（非参数 weights 池分配仍 D2H 备份），ACL 图和 HCCL 组保留。

需要工程补齐：

- 本机三形态的 sleep/wake 编排；
- 把 `Netloader` 的 HCCL P2P 加载接到 level 2 wake 后触发；
- 在 sleep 前增加 sleep-safe 的 KV drain hook；
- 三种形态共享本机 Mooncake 的连接器配置；
- ACL 图/HCCL 组常驻带来的 host 内存预算。

不采用：

- 本机 engine→engine 权重 D2D；
- 任意 P/D 跨形态权重加载；
- 本地盘作为权重主路径。

## 1. sleep mode 的实际行为

参考：

- <https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/sleep_mode.html>
- `3rdparty/vllm-ascend/vllm_ascend/device_allocator/sleep_mode_backend.py::CaMemBackend`
- `3rdparty/vllm-ascend/vllm_ascend/device_allocator/camem.py::CaMemAllocator`
- `3rdparty/vllm-ascend/vllm_ascend/worker/worker.py::Worker.sleep` / `wake_up`

已核实：

- `CaMemBackend.suspend` 的 offload tags 是 `("weights", "runtime", "cudagraph")`；
- 只处理 NPU memory pool，不 unmount Mooncake connector/segment，不释放进程 host DRAM；
- 进程不退出时，Mooncake connector/segment 仍在进程内。

### 1.1 三种配置下的资源去向

| 内容 | `level=1` | `level=2` | `level=2` + `sleep_mode_extra_cleanup=true` |
|---|---|---|---|
| 模型权重 | 移到 host DRAM | 部分移到 host DRAM（仅非参数 weights 池分配）；其余丢弃 | 同 `level=2` |
| KV cache（NPU） | 丢弃 | 丢弃 | 丢弃 |
| runtime 池 | 移到 host DRAM | 移到 host DRAM | 移到 host DRAM |
| cudagraph 池 | 移到 host DRAM | 移到 host DRAM | 移到 host DRAM |
| ACL graph workspace | 保留在 NPU | 保留在 NPU | 丢弃 |
| ACL graph params | 保留在 NPU | 保留在 NPU | 丢弃 |
| HCCL 组 | 保留 | 保留 | 丢弃 |
| Mooncake segment（host DRAM） | 保留 | 保留 | 保留 |
| Mooncake client 与心跳 | 保留 | 保留 | 保留 |
| 显存 buffer 注册 | 丢弃 | 丢弃 | 丢弃 |

依据：`camem.py`、`sleep_mode_backend.py`、`sleep_mem_optimized.py`、`worker.py`。

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

同一时间只有一个形态 active；另外两个进程还在，但 NPU 资源已释放。

```text
P 引擎       (sleep)  ─┐
D 引擎       (sleep)  ─┼─ 同一台机器的 8 张卡
混部引擎     (active) ─┘
```

源形态必须先 sleep，目标形态才能 wake，因为二者不能同时在这 8 张卡上持有完整权重。

## 3. 权重：同形态跨机 P2P

### 3.1 原则

目标形态只找另一台机器上的同形态实例：

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

这就是同形态 P2P 传输。
P TP8、D DP8、混部 DP2TP4 都可以按相同 rank 直接收。

当前差距：

- `Netloader` 的入口是 `--load-format=netloader`，在引擎启动时加载；
- 本方案要在 level 2 sleep 后 `wake?tags=weights` 再触发；
- 需要把 Netloader 的 send/recv 接到 wake 后的加载路径，并补 donor 选择/控制面。

不需要重新造传输。

其他路径：

- `weight_transfer_config` 是 trainer → worker 的权重更新，不是 P/D 引擎之间的 donor 协议；
- `RFork` 是 seed 加载，sleep mode 下被 bypass。

结论：

> **Ascend 有同形态 P2P 权重加载，核心就是 Netloader；缺的是把它接到 level 2 wake，而不是缺传输。**

### 3.4 前提

- donor 和目标同模型、同 revision；
- 同 TP/DP/layout；
- donor 节点 alive，且不被 sleep；
- 目标先 `wake?tags=weights`；
- 所有 rank 拉完并 barrier；
- 再 `wake?tags=kv_cache`。

如果 donor 不可用，才降级。注意 level 2 不会在 host 保留完整权重：

- 默认从本地盘/3FS 加载，只做 fallback；
- 如果要 host 完整权重 + H2D，必须额外常驻一份 host 副本，这会抵消 level 2 省 host 的意义，需要单独算代价。

不要把本地盘当主路径，也不要把 host 副本当成 level 2 的免费 fallback。

## 4. KV：三个引擎连同一个本机 Mooncake 池

三个形态各带 Mooncake connector，但接的是**同一个本机 `mooncake_master`/逻辑池**：

- 同一个 master 地址、tenant/namespace；
- 同一个模型、revision、block size、hash seed；
- 每个引擎有自己的 client/segment，不是共享同一段物理 DRAM；
- sleeping 引擎的 segment 留在进程内；
- 形态间 TP/DP layout 不同，由 connector 做 rank 映射。

vLLM-Ascend 已有同类部署：

- [PD-Colocated with Mooncake Multi-Instance](https://github.com/vllm-project/vllm-ascend/blob/main/docs/source/tutorials/features/pd_colocated_mooncake_multi_instance.md)
- 两个 vLLM 实例共用同一个 `mooncake_master`；
- 都用 `MooncakeConnectorStoreV1`，`kv_role=kv_both`；
- 跨实例 KV 可复用。

本方案：

- 源形态在正常推理时就把完整 block 保存到 Mooncake；
- **decode 增量默认不保存**：需要 `save_decode_cache=true`；否则只有 prefill/前缀路径保存；
- 不是请求结束才落盘，也不是按 token；
- sleep 前等最后一批 block 的保存完成；
- 目标形态 wake KV 后从同一个池读；
- 形态间 TP/DP layout 不同时，由 connector 做 rank 映射。

术语上不使用 SGLang HiCache 的 `write_through`：`AscendStoreConnector` 没有这个配置项。这里准确说法是 **block 完成后 save/put 到池**。

## 5. 切换流程

```text
1. 控制面选择目标形态
2. 源形态摘流，等在途请求结束
3. 等在途 step 完成；正常路径下最后一个 step 的 `wait_for_save` 会 join 保存队列（无 timeout，不能当容错屏障）
4. 走 sleep-safe drain（引擎/connector 小改），确认队列清空
5. 源形态 sleep，释放 8 卡
6. 目标形态 wake?tags=weights
7. 目标 8 个 rank 从远端同形态 donor 做 Netloader/HCCL P2P
8. 所有 rank barrier
9. 目标形态 wake?tags=kv_cache
10. 目标形态确认 Mooncake KV 可读
11. 路由切到目标形态
```

关键点：

- `wait_for_save` 不是控制面 API；只能在 step 边界等它完成；
- `wait_for_save` 无 timeout；SDK 卡住会阻塞引擎，不能把它当可靠屏障；
- `drain_queued_saves` 当前只被 `disconnect_kv_transfer_peers()` 调用；后者不是 sleep 路径；
- 需要改引擎/connector：在 `Worker.sleep` 或 sleep RPC 里，释放 NPU 前调用 connector 的 drain，**不要复用 disconnect**；
- drain 失败必须 `raise`，不能继续 `unregister_buffer` / `suspend`；
- 这是一个小改动，不是阻塞方案：`drain_queued_saves` 已有超时和失败语义，只差 sleep 路径调用；
- 只有源形态 sleep 后，目标形态才能占满 8 卡；
- 目标权重拉完前不能接流量；
- 源形态的 Mooncake connector/segment 在 sleep 期间保留。

drain 失败/超时：切换失败，源形态不进入 sleep。
不承诺回切源形态——源保存链路已经可疑，不能把流量打回该实例；按故障恢复流程处理。

## 6. level 2、ACL 图与 HCCL

### 6.1 为什么 level 2

level 1 会把 weights offload 到 host。D DP8 每个 DP rank 持完整权重，所以 level 1 的 D 形态会在 host 放 **8 份完整权重**，不是一份；这是选 level 2 的主要量化理由。

level 2 只把 `_discardable_parameters` 返回的参数指针改为丢弃；**非参数的 weights 池分配（如 quantization scale）仍会 D2H 备份**。所以准确说法是“**大部分 weights 丢弃，非参数 weights 池分配仍备份到 host**”，不是“所有 weights 都不在 host”。

注意：level 1 也会 discard KV；选 level 2 不是因为“KV 卸载需要 level 2”，而是因为**避免 D DP8 在 host 放 8 份完整权重**。

### 6.2 ACL 图和 HCCL

- `sleep_mode_extra_cleanup=false`（默认）：不清 ACL graph workspace，不重置 graph params；
- `sleep_mode_extra_cleanup=true`：清 workspace，wake 后重新 capture；
- `cudagraph` / `runtime` 在 sleep 时 D2H 到 host，不占 NPU；
- HCCL group 在 `sleep_mode_extra_cleanup=false` 时保留，不等于占 NPU；
- sleeping 形态占的是 host 内存，不是 NPU。

保留图的前提：

- 权重回填到原参数地址/布局；
- 模型结构或 TP/DP 布局不变；
- 否则必须重新 capture。

## 7. 成立条件

1. 三个形态同模型、同 revision；同形态 donor 同 TP/DP/layout。
2. 三形态共用本机 8 卡、同一个 Mooncake master/逻辑池；同一时间一个 active。
3. 源形态先 sleep，目标才能 wake。
4. 远端同形态 donor 保持 alive。
5. 三个本地进程不退出；否则进程内 Mooncake segment 消失。
6. 控制面负责摘流、sleep-safe drain、sleep/wake、权重 P2P、KV 恢复、路由切换。
7. sleep 路径已经接上 drain；drain 失败时不释放 NPU。

## 8. 不做的事

- 不做本机 engine→engine 权重 D2D：三形态不能同时占 8 卡；
- 不做任意 P/D 跨形态权重加载：shard 布局不同；
- 不把本地盘作为权重主路径；
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
3. 源形态已保存的完整 block，目标 wake 后能命中；用 `batch_is_exist` / `batch_get_replica_desc` 核验，不只靠请求成功。
4. level 2 + `sleep_mode_extra_cleanup=false` 下 ACL 图和 HCCL 组保留。
5. 目标从远端同形态 donor 通过 Netloader/HCCL P2P 加载权重，所有 rank 一把完成。
6. 同形态 donor 的 TP/DP/layout 不匹配时拒绝加载，不静默错位。
7. sleeping 形态不保留完整权重；非参数 weights 池分配和 runtime/cudagraph 仍占 host。
8. sleep 前 drain 失败时，源形态不进入 sleep；路由不把流量打回该可疑源，按故障恢复处理。
9. 路由不会把请求发给 sleeping 形态。
