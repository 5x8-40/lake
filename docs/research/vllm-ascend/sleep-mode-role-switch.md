# Sleep Mode 与 P/D/混部角色切换

> 目标：同一节点/NPU 上预置 P、D、混部三个引擎，两个 sleep、一个 active；切换只切 active 引擎，不重启进程，KV 继续留在 Mooncake。
> 适用：vLLM-Ascend `enable_sleep_mode` / `sleep` / `wake_up`；Mooncake connector 在引擎进程内挂载。

## 0. 结论

1. 三个引擎预先启动，P、D、混部分别一套；同一时间只让一个 active。
2. 切换 = 当前 active 引擎摘流并 sleep，目标引擎 wake 并接流；**不需要重启进程**。
3. sleep 只释放 NPU memory pool，不 unmount Mooncake connector/segment；只要引擎进程不退出，host DRAM 上的 Mooncake segment 继续活着。
4. sleep 会丢弃 NPU 侧 KV，所以 sleep 前必须把 KV 写穿 Mooncake；wake 后从 Mooncake 读回。
5. 这不是同进程换角色，也不是零中断；切换有 sleep/wake 和 KV 回填延迟。

## 1. sleep mode 的实际行为

参考：

- vLLM-Ascend Sleep Mode: https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/sleep_mode.html
- `vllm_ascend/device_allocator/sleep_mode_backend.py::CaMemBackend.suspend`
- `vllm_ascend/device_allocator/camem.py::CaMemAllocator.sleep`
- `3rdparty/vllm/vllm/v1/worker/gpu_worker.py::GPUWorker.sleep` / `wake_up`

已核实：

- `level=1`：offload `weights`，其余 tag（包括 KV cache）discard；
- `level=2`：weights 和 KV cache 全部 discard；
- 只处理 NPU memory pool 里的 allocation，不 unmount Mooncake segment，不释放进程的 host DRAM；
- weights 会备份到 host pinned CPU；KV cache 没有 host backup；
- 进程不退出时，Mooncake connector/segment 仍在进程内。

## 2. 三个引擎的部署

```text
P 引擎       (sleep)
D 引擎       (sleep)
混部引擎     (active)  ← 路由
```

三个引擎可以放在同一节点/NPU 上：

- 启动时逐个初始化并 sleep，避免同时占满 NPU；
- 同一时间只允许一个 active；
- 两个 sleeping 引擎的 weights 留在 host（level 1），或从磁盘重载（level 2）；
- 三个引擎的 Mooncake connector 都在各自进程内，sleep 不会 unmount。

## 3. 切换流程

以“混部 → P”为例：

```text
1. 路由停止向混部引擎发新请求
2. 等在途请求结束
3. 混部引擎把 KV 写穿 Mooncake，等 READY
4. 混部引擎 sleep（释放 NPU）
5. P 引擎 wake
6. P 引擎确认 Mooncake segment/KV 可读
7. 路由切到 P 引擎
```

切换到 D 或切回混部同理。

关键点：

- 顺序必须串行：先 sleep 当前 active，再 wake 目标；不能两个引擎同时占满 NPU；
- 切换期间 KV 必须已经在 Mooncake 里 READY，不能只留在 NPU；
- 路由和 health check 必须知道 engine 的 active/sleeping 状态；
- 进程不重启，Mooncake connector/segment 不会因为 sleep 消失。

## 4. 多引擎共用 Mooncake 池

这不是新假设，vLLM-Ascend 已有同类部署：

- `docs/source/tutorials/features/pd_colocated_mooncake_multi_instance.md`
- 两个 vLLM 实例，各占 4 张 NPU；
- 共用同一个 `mooncake_master`；
- 两边都用 `MooncakeConnectorStoreV1`，`kv_role=kv_both`；
- 跨节点、跨实例 KV 可复用。

这说明：**共用 Mooncake 池和部署形态无关**。P、D、混部、多个实例都可以连同一个池。

要区分两件事：

- **Store 直读**：TP/DP/layout 一致时，可以直接读同一个 KV 对象；
- **Connector 拉取**：TP/DP/layout 不同时，由 `MooncakeConnector` 按两边配置做 rank 映射/搬运；P/D 就是这么读的。

所以“共用池”成立；“所有 layout 都直接同对象读”不成立。

## 5. 成立条件

1. 三个引擎使用同一模型、同一 revision；Store 直读还要求同一 TP/DP/key 布局，不同布局由 connector 映射。
2. 同一 NPU 上同一时间只允许一个引擎 active。
3. host 侧能容纳两个 sleeping 引擎的 weights（level 1）或能从磁盘重载（level 2）。
4. 三个引擎进程都不退出；否则进程内的 Mooncake segment 会随进程消失。
5. 路由/控制器负责：
   - 摘流；
   - 等在途请求结束；
   - 写穿 Mooncake + READY；
   - sleep/wake 顺序；
   - 健康检查与回切。

## 6. 权重与 ACL 图

### 6.1 level 2 的事实

- level 2 会 discard 全部 weights 和 KV cache；
- `sleep_preserve_parameter_names` 只保留指定参数，默认不保留整套权重；
- `_sleep_saved_buffers` 只保存 buffer，不保存全部模型参数；
- wake 顺序是 `wake_up?tags=weights` → 重新加载/传输权重 → `wake_up?tags=kv_cache`。

所以 level 2 下“权重存一份”不是可选项，是必须做：要么 host/磁盘留一份，要么从别的引擎 D2D 拉。

### 6.2 ACL 图

- `sleep_mode_extra_cleanup=false`（默认）不会清 ACL graph workspace，也不会重置 graph params；
- `sleep_mode_extra_cleanup=true` 会在 sleep 时清 workspace、重置 graph，wake 后重新 capture；
- 保留图的前提是：权重回填到同一批参数地址、同一布局；`CaMemAllocator.wake_up` 用原 handle 重新 map，VA 不变，所以在原参数上加载/传输权重可以复用图；
- 如果模型结构、TP/DP 布局或权重地址变了，必须重新 capture，不能硬复用。

### 6.3 权重来源

| 方案 | 做法 | 适用 | 代价 |
|---|---|---|---|
| host 一份 | 权重放 host DRAM，wake 后 H2D 到各引擎 | 同机多引擎 | host 内存；H2D 带宽 |
| 磁盘/3FS 一份 | wake 后从共享存储加载 | 跨机、冷启动 | 加载慢 |
| 同机 D2D | `weight_transfer_config` 用 `ipc`，从一个 seed 引擎拉 | 同物理 NPU/同机 | IPC 配置 |
| 跨机 D2D | `weight_transfer_config` 用 `hccl`，从 seed 引擎拉 | 跨节点 | HCCL 带宽 |
| RFork | seed 实例 + YuanRong TransferEngine | 冷启动 | **sleep mode 下被 bypass**，不能用 |
| Netloader | `--load-format=netloader` 从 server 拉 | 启动加载 | 不是 level-2 wake 后的自动路径 |

结论：

- 想保留 ACL 图：level 2 + `sleep_mode_extra_cleanup=false` + 权重回填到原参数地址；
- 想少占 host：level 2 + 一份共享权重 + HCCL/IPC 传输；
- 想简单：level 1，把整套 weights offload 到 host，但 host 内存要按模型大小乘 sleeping 引擎数算；
- RFork 不能和 sleep mode 一起用，vLLM-Ascend 明确在 sleep 模式下 bypass。

参考：

- `vllm_ascend/worker/worker.py::sleep` / `wake_up`
- `vllm_ascend/device_allocator/camem.py::CaMemAllocator.sleep` / `wake_up`
- `docs/source/user_guide/feature_guide/rl.md`：level 2 staged wake 与 `ipc`/`hccl` weight transfer
- `docs/source/user_guide/feature_guide/rfork.md`：sleep mode 下 RFork bypass

## 7. 角色切换的边界

- 切的是“哪个引擎接流”，不是“同一个进程换角色”；
- 同进程内切 P/D/混部需要引擎侧对称调度，sleep 不提供；
- 有 sleep/wake 延迟和 KV 回填延迟，不承诺零中断；
- vLLM 只提供单引擎 sleep/wake，三引擎的编排要外部控制器做；
- 如果进程退出/重启，KV 必须提前复制到别的 segment 或外部存储。

## 8. 失败场景

| 失败 | 后果 | 处理 |
|---|---|---|
| 目标引擎 wake 前源引擎未释放 NPU | 显存不足 | 串行 sleep/wake |
| sleep 前未写穿 Mooncake | wake 后 NPU KV 为空 | 强制 flush + READY barrier |
| host 侧 weights 放不下 | 无法 sleep/wake | level 2 重载或增加 host 内存 |
| 进程退出 | Mooncake segment 消失 | 提前复制到其他 segment/SSD |
| 路由仍打 sleeping 引擎 | 请求失败/超时 | 路由状态跟随 sleep/wake |
| key/模型版本不一致 | 命中错误 | 固定 model revision 和 hash 口径 |

## 9. 探索通过后，kvcm 加什么

这里只探索引擎侧是否可行，不先把方案写进 kvcm 设计。

如果验证通过，kvcm 侧对应的是**形态转换**，不是新的 KV 操作：

- D8 管理面增加“角色/形态”决策：P、D、混部三种角色下发给哪些引擎；
- D8 维护 engine 的 active/sleeping 状态，触发 sleep/wake，并和路由联动；
- KVT 增加 engine 角色字段，避免把请求发给 sleeping 或角色不匹配的引擎；
- D9 增加切换审计、切换耗时、失败回切、KV 回填命中率；
- KV 的复制/保留仍走现有 D2 操作，不因为形态转换新增一套 KV 语义。

边界：

- kvcm 不实现引擎内部调度，也不实现 sleep/wake；
- kvcm 只做“选角色、发切换、看结果”，执行在引擎/编排层；
- 形态转换的可行性先由 vLLM-Ascend 侧验证，验证通过后再进 D8。

## 10. 验收

1. 同一 NPU 上三个引擎可以逐个初始化并 sleep，只有一个 active。
2. 当前 active 引擎 sleep 后，进程内 Mooncake connector/segment 仍可读。
3. sleep 前写穿的 KV，wake 后能命中，不重算。
4. 混部 → P → D → 混部的切换只需 sleep/wake + 路由，不重启进程。
5. 两个 sleeping 引擎的 host 内存/权重 offload 在预算内。
6. 路由不会把请求发给 sleeping 引擎。
