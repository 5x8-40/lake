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

## 4. 成立条件

1. 三个引擎使用同一模型、同一 revision、同一套 key/hash 口径。
2. 同一 NPU 上同一时间只允许一个引擎 active。
3. host 侧能容纳两个 sleeping 引擎的 weights（level 1）或能从磁盘重载（level 2）。
4. 三个引擎进程都不退出；否则进程内的 Mooncake segment 会随进程消失。
5. 路由/控制器负责：
   - 摘流；
   - 等在途请求结束；
   - 写穿 Mooncake + READY；
   - sleep/wake 顺序；
   - 健康检查与回切。

## 5. 角色切换的边界

- 切的是“哪个引擎接流”，不是“同一个进程换角色”；
- 同进程内切 P/D/混部需要引擎侧对称调度，sleep 不提供；
- 有 sleep/wake 延迟和 KV 回填延迟，不承诺零中断；
- vLLM 只提供单引擎 sleep/wake，三引擎的编排要外部控制器做；
- 如果进程退出/重启，KV 必须提前复制到别的 segment 或外部存储。

## 6. 失败场景

| 失败 | 后果 | 处理 |
|---|---|---|
| 目标引擎 wake 前源引擎未释放 NPU | 显存不足 | 串行 sleep/wake |
| sleep 前未写穿 Mooncake | wake 后 NPU KV 为空 | 强制 flush + READY barrier |
| host 侧 weights 放不下 | 无法 sleep/wake | level 2 重载或增加 host 内存 |
| 进程退出 | Mooncake segment 消失 | 提前复制到其他 segment/SSD |
| 路由仍打 sleeping 引擎 | 请求失败/超时 | 路由状态跟随 sleep/wake |
| key/模型版本不一致 | 命中错误 | 固定 model revision 和 hash 口径 |

## 7. 探索通过后，kvcm 加什么

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

## 8. 验收

1. 同一 NPU 上三个引擎可以逐个初始化并 sleep，只有一个 active。
2. 当前 active 引擎 sleep 后，进程内 Mooncake connector/segment 仍可读。
3. sleep 前写穿的 KV，wake 后能命中，不重算。
4. 混部 → P → D → 混部的切换只需 sleep/wake + 路由，不重启进程。
5. 两个 sleeping 引擎的 host 内存/权重 offload 在预算内。
6. 路由不会把请求发给 sleeping 引擎。
