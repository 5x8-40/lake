# Ascend 迁移约束审视

> 专题索引:[README.md](README.md) · 代码级排查,基于 `3rdparty/dynamo`

Dynamo 原生于 NVIDIA GPU 平台。迁移到 Ascend NPU 后，扩缩容和可观测性的关键模块存在大量 NVIDIA 硬依赖。以下是代码级排查结果。

## 总览

| 模块 | NVIDIA 依赖 | Ascend 可用性 | 改动工作量 |
|------|------------|--------------|-----------|
| FPM 指标采集 | 部分（设备身份识别走 CUDA） | 部分可用 | 小 |
| Prometheus 可观测栈 | 全部（DCGM exporter、NVML/DCGM actuator） | 不可用 | 中 |
| K8s Operator 扩缩容 | 全部（`nvidia.com/gpu` 资源名硬编码 110+ 处） | 不可用 | 中 |
| Worker 健康检查 | 无 | 可用 | 无 |
| 通信域 TP/EP 集体通信 | **无**（引擎层负责，Dynamo 仅透传配置） | **可用** | 无 |
| KV Cache 跨卡广播 | NCCL broadcast（Dynamo 自有，CollectiveOps trait 已抽象） | 需新增 HCCL 后端 | 中 |
| VirtualConnector (非 K8s) | 无 | 可用 | 无 |

## 1. FPM 指标采集

### 可用部分

FPM 的数据管线（Schema + 消息中继）是纯 Python/Rust，不涉及 GPU 硬件：

- Schema: `components/src/dynamo/common/forward_pass_metrics.py` — 纯 msgspec 结构体
- 发布器: `lib/llm/src/fpm_publisher.rs` — 纯 ZMQ 消息转发
- 采集: `InstrumentedScheduler` 从调度器读取 `sum_prefill_tokens`、`kv_utilization` 等调度层指标，不直接读 GPU 内存

### 不可用部分

- **设备身份识别**: `gpu_memory_service/v1/device.py` 使用 `cuda.bindings.driver` (cuda-python) 调用 `cuDeviceGet()` 和 `cuDeviceGetUuid()` 获取设备标识。Ascend 上没有 cuda-python，会直接报错。
- **CUDA Graph 字段**: `InstrumentedScheduler` 记录了 `cudagraph_mode`、`cudagraph_capture_sizes` 等配置值。这些来自 vLLM 编译配置对象而非硬件读取，字段名在 NPU 上有误导。

### 适配要点

```
需要做的事情:
1. gpu_memory_service 的设备识别层需要适配 Ascend（使用 CANN/npu-smi 替代 cuda.bindings.driver）
2. FPM Schema 中的 CUDA Graph 相关字段在 Ascend 上置 None 或重命名
3. FPM 的 ZMQ 传输层和 Planner 消费逻辑本身不需要改
```

## 2. Prometheus 可观测栈

### 问题

整个可观测性部署脚本 `deploy/observability/setup-monitoring.sh` 依赖 NVIDIA 生态：

- 部署 `nvidia-dcgm-exporter` 通过 NVIDIA GPU Operator Helm chart
- 自定义指标文件 `dcgm-metrics-with-nvlink.csv` 全部是 DCGM field ID（`DCGM_FI_DEV_SM_CLOCK`、`DCGM_FI_DEV_GPU_TEMP`、`DCGM_FI_DEV_POWER_USAGE` 等）
- 功耗代理 `deploy/power-agent/actuator.py` 两个实现 `NvmlActuator`（`pynvml`）和 `DcgmActuator`（`pydcgm`）全部是 NVIDIA 专有

### 适配要点

```
需要做的事情:
1. 替换 DCGM exporter → Ascend NPU 指标导出器（npu-smi 或 Ascend Manager）
2. 重写 Prometheus 抓取配置，指向 NPU 指标端点
3. 功耗管理: 用 Ascend 功耗 API 替换 pynvml/pydcgm
4. Grafana 仪表板需要重新映射指标名（dynamo_gpu_* → dynamo_npu_*）
```

## 3. K8s Operator 扩缩容

### 问题

这是约束最重的模块。`nvidia.com/gpu` 在 operator 代码中被硬编码了 110 次，分布在 27 个文件中。

关键硬编码点：

| 文件 | 硬编码内容 |
|------|-----------|
| `internal/consts/consts.go:149` | `KubeResourceGPUNvidia = "nvidia.com/gpu"` 常量 |
| `internal/consts/consts.go:68` | `dynamo.nvidia.com/gpu-power-limit` 注解 |
| `internal/gpu/discovery.go` | GPU 发现全走 DCGM exporter pod (`:9400/metrics`)，解析 DCGM 字段；GPU 型号推断只识别 NVIDIA 型号（GB200/H100/A100）和 AMD MI300，没有 Ascend 型号 |
| `internal/dynamo/failover.go:265` | failover toleration 硬编码 `Key: "nvidia.com/gpu"` |
| `internal/dra/dra.go` | GPU 节点 taint `nvidia.com/gpu=NoSchedule`，MIG 形状 `nvidia.com/mig-3g.20gb` |

### 适配要点

```
需要做的事情:
1. KubeResourceGPUNvidia 常量改为可配置（通过 Helm values 或环境变量注入），
   Ascend 对应资源名: "ascend.npu" 或 "huawei.com/ascend"（取决于设备插件）
2. GPU 发现层: 替换 DCGM exporter 查询 → Ascend NPU 发现机制
   （使用 Ascend device plugin 标注的节点标签）
3. GPU 型号推断表: 加入 Ascend 910B/910C 型号
4. failover toleration: 使用配置的资源名而非硬编码
5. 功耗注解: 将 dynamo.nvidia.com/ 改为通用命名空间
6. CRD 示例文件中 nvidia.com/gpu 全部替换
```

## 4. Worker 健康检查

**可直接使用，无需改动。** 健康检查 (`components/src/dynamo/vllm/health_check.py`) 通过发送测试 prompt 验证引擎响应，不依赖 GPU 特定 API。

## 5. 扩缩容控制面 — 逐层验证零 GPU 依赖

对 Dynamo 非 K8s（VirtualConnector）扩缩容完整链路做了逐文件审计。结论：**扩缩容控制面完全不依赖 CUDA/NCCL/GPU 硬件。**

### 5.1 扩缩容执行链路

扩容（加节点）：
```
Planner 读 FPM/Traffic 指标（纯数字运算，零 CUDA）
  → set_component_replicas(target) 写入 etcd
    → VirtualConnectorCoordinator 写 etcd:
        v1/{ns}/planner/num_prefill_workers
        v1/{ns}/planner/num_decode_workers
        v1/{ns}/planner/decision_id
    → 外部部署进程 VirtualConnectorClient 读 etcd
      → 启动 dynamo.vllm 进程
        → WorkerFactory.create()（纯控制流分发，零 GPU 调用）
          → AsyncLLM.from_vllm_config() ← 唯一 GPU 调用（引擎层，vLLM Ascend 负责）
            → register_model() 写 etcd: v1/mdc/{ns}/{comp}/{ep}/{instance_id}
              → serve_endpoint() 写 etcd: v1/instances/{ns}/{comp}/{ep}/{instance_id}
                → 前端 etcd watch 发现新 worker，开始路由
```

缩容（减节点）反向：停进程 → etcd 租约过期 → 前端发现下线 → 不再路由。

**逐层审计结果：**

| 层 | 文件 | GPU/CUDA 调用 | 结论 |
|----|------|-------------|------|
| Planner 决策 | `planner/core/load_scaling.py` | 无 — 纯 FpmObservation 数字运算 | 零依赖 |
| VirtualConnector | `planner/connectors/virtual.py` | 无 — etcd 读写 | 零依赖 |
| VirtualConnectorCoordinator | `lib/bindings/python/rust/planner.rs` | 无 — etcd 读写 | 零依赖 |
| WorkerFactory.create() | `vllm/worker_factory.py:723-783` | 无 — 纯控制流分发 | 零依赖 |
| register_model() | `lib/bindings/python/rust/lib.rs:610` | 无 — 序列化 + etcd 写入 | 零依赖 |
| serve_endpoint() | `lib/runtime/src/component/endpoint.rs` | 无 — NATS/TCP 监听注册 | 零依赖 |
| FPM 采集管线 | `lib/llm/src/fpm_publisher/` + `planner/environment/metrics_provider/` | 无 — ZMQ → 事件平面 → Planner 全链路 CPU 侧 | 零依赖 |
| 前端发现 | `lib/runtime/src/discovery/` | 无 — etcd watch | 零依赖 |
| **引擎初始化** | `vllm/main.py:750` `AsyncLLM.from_vllm_config()` | **有 — CUDA context + 权重加载 + KV 分配** | **引擎层，vLLM Ascend 适配** |

### 5.2 需要关注的边缘细节

| 项目 | 代码位置 | 影响程度 | 说明 |
|------|---------|---------|------|
| `DeviceType` 枚举只有 `Cpu/Cuda` | `component.rs:93-98` | 极低 | `endpoint_device_type()` 只读环境变量（`CUDA_VISIBLE_DEVICES`），不调用任何 CUDA API。Ascend worker 会被标为 `Cuda`。仅 `DeviceAwareWeighted` 路由模式使用此字段做 CPU/Accelerator 区分；标准路由（RoundRobin/LeastLoaded/KV）完全忽略 |
| NVTX 配置文件标注 | `lib/runtime/src/nvtx.rs` | 可忽略 | `#[cfg(feature = "nvtx")]` 门控，默认关闭 |
| Benchmark 模式 | `vllm/benchmark_worker.py:132` | 可忽略 | `torch.cuda.current_stream()` 仅在 `--benchmark-mode` 时执行 |

### 5.3 推理引擎的 TP/EP 集体通信 — 不是 Dynamo 的事

**Dynamo 完全不参与。** 模型推理的 TP/EP 集体通信（forward 时的 all-reduce 等）由推理引擎（vLLM Ascend / SGLang Ascend）内部处理。Dynamo 在 K8s Operator 层只做配置透传：

- 设置环境变量：`NCCL_DEBUG`、`NCCL_IB_DISABLE`、`NCCL_P2P_DISABLE`（operator 的 `backend_trtllm.go:239`）
- 注入启动参数：`--tensor-parallel-size`、`--distributed-executor-backend mp`、`--distributed-port`（`backend_vllm.go`）
- **不调用任何 NCCL API**，不创建 NCCL communicator

> **结论**: TP/EP 由 vLLM Ascend 适配 HCCL。Dynamo 侧只需把 NCCL 相关的环境变量名改为 HCCL 等价名。

### 5.4 Dynamo 自身的 KV Cache 跨卡广播 — 条件性触发

**触发条件：MLA 模型（DeepSeek v3/v4 等）+ KV 从持久化存储（G2/G3）恢复。** 基本扩缩容不触发此路径。

**背景：MLA 模型的 KV cache 是融合后的 latent vector，在 TP 组内不切分。** 每个 GPU 都需要完整 KV 数据。

Dynamo 的优化方案（replicated 模式）：
```
Rank 0:   G3(disk) ← G2(host) ← G1(GPU) ===broadcast==> 其他 rank 的 G1
Rank 1-N: [无需 G2/G3]               G1(GPU) <==================================
```

只有 rank 0 从存储加载数据，然后通过 NCCL broadcast 发给同 TP 组所有 rank。

**两层独立数据流：**
- vLLM Ascend 用 HCCL 做模型推理通信 → 引擎层已完成
- Dynamo 用 NCCL 做 KV cache broadcast → 需 Dynamo 层适配（但仅在 MLA + KV 恢复时触发）

**代码架构（v1 生产路径 + v2 开发中）：**

| 路径 | 关键文件 | NCCL 调用 |
|------|---------|----------|
| v1 生产 | `lib/llm/src/block_manager/block/transfer/nccl.rs` | `ncclBcast`, `ncclGroupStart/End` |
| v1 bootstrap | `lib/llm/src/block_manager/distributed/nccl_bootstrap.rs` | `ncclGetUniqueId`, `ncclCommInitRankConfig` |
| v2 抽象 | `lib/kvbm-engine/src/collectives/mod.rs` | `CollectiveOps` trait（后端无关） |
| v2 NCCL 实现 | `lib/kvbm-engine/src/collectives/nccl.rs` | `NcclCollectives` |

**所有 CUDA/NCCL 代码在 `lib/llm/src/block_manager/` 中都被 `#[cfg(feature = "block-manager")]` 门控。** 不使用 KV 传输功能时不编译。

### 5.4.1 Ascend 适配路径（如需 MLA + KV 恢复场景）

`CollectiveOps` trait 已后端无关（`lib/kvbm-engine/src/collectives/mod.rs`）：
```rust
pub trait CollectiveOps: Send + Sync {
    fn broadcast(&self, src: LogicalLayoutHandle, dst: LogicalLayoutHandle,
                 src_block_ids: &[BlockId], dst_block_ids: &[BlockId],
                 layer_range: Option<Range<usize>>) -> Result<TransferCompleteNotification>;
    fn rank(&self) -> usize;
    fn world_size(&self) -> usize;
}
```

新增 `HcclCollectives` 实现即可接入 v2 路径。

**9 个 NCCL C API 调用映射：**

| NCCL 函数 | 调用位置 | HCCL 等价值 |
|-----------|---------|-------------|
| `ncclGetUniqueId` | bootstrap.rs:105 (kvbm-engine), nccl_bootstrap.rs:85 (llm) | `hcclGetUniqueId` |
| `ncclCommInitRank` | bootstrap.rs:215 (kvbm-engine) | `hcclCommInitRank` |
| `ncclCommInitRankConfig` | nccl_bootstrap.rs:213 (llm) | `hcclCommInitRankConfig` |
| `ncclCommInitAll` | nccl.rs:519 (测试) | `hcclCommInitAll` |
| `ncclCommDestroy` | nccl.rs:477 (kvbm-engine), nccl_bootstrap.rs:272 (llm) | `hcclCommDestroy` |
| `ncclBcast` | nccl.rs:338 (kvbm-engine), nccl.rs:141/201 (llm) | `hcclBroadcast` |
| `ncclGroupStart` | nccl.rs:329 (kvbm-engine), nccl.rs:66 (llm) | `hcclGroupStart` |
| `ncclGroupEnd` | nccl.rs:351 (kvbm-engine), nccl.rs:85/99 (llm) | `hcclGroupEnd` |
| `ncclGetVersion` | nccl_bootstrap.rs:169 (llm) | `hcclGetVersion` |

**Cargo.toml 改动：**

| 文件 | 现有 feature | 新增 |
|------|-------------|------|
| `lib/kvbm-engine/Cargo.toml` | `nccl = ["dep:cudarc"]` | `hccl = ["dep:hccl-sys"]` |
| `lib/llm/Cargo.toml` | `nccl = ["dep:cudarc", "cudarc/nccl"]` | `hccl = ["dep:hccl-sys", "dep:cann-driver"]` |
| `lib/bindings/kvbm/Cargo.toml` | `nccl = ["block-manager", "dynamo-llm/nccl", "cudarc/nccl"]` | `hccl = ["block-manager", "dynamo-llm/hccl"]` |

**vLLM Ascend 必须提供的接口（Dynamo 借用）：**
1. HCCL Communicator (`hcclComm_t` raw pointer)
2. NPU Stream (`aclvtStream`)
3. 生命周期管理（vLLM Ascend 管理创建/销毁，Dynamo 仅借用）

## 6. VirtualConnector (非 K8s 部署)

**可直接使用，无需改动。** 完整扩缩容链路（§5.1）已逐层验证零 GPU 依赖。

`components/src/dynamo/planner/connectors/virtual.py` 是纯平台无关代码：
- `get_gpu_counts()` / `get_gpu_shapes()` 返回 `None` — 明确标注 "Virtual deployments do not expose GPU shape"
- `validate_deployment()` 是空实现
- 所有协调通过 etcd，不依赖 K8s GPU 资源 API

etcd 协调 key 结构：
```
v1/{ns}/planner/num_prefill_workers   — Planner 写入期望数量
v1/{ns}/planner/num_decode_workers    — 同上
v1/{ns}/planner/decision_id           — 单调递增决策 ID
v1/{ns}/planner/scaled_decision_id    — 部署进程完成后回写确认
v1/instances/{ns}/{comp}/{ep}/{id}    — worker 实例注册
v1/mdc/{ns}/{comp}/{ep}/{id}          — 模型部署卡片
```

## 7. 约束总览（代码级审计后）

| 模块 | 代码验证结论 | Ascend 影响 |
|------|-------------|-------------|
| **扩缩容控制面** | Planner → VirtualConnector → etcd → worker 注册 → 前端发现，全链路零 CUDA | **直接可用** |
| **FPM 指标采集** | vLLM 调度器 → ZMQ → FpmEventRelay → 事件平面 → Planner，全链路 CPU 侧 | **直接可用** |
| **Worker 注册/发现** | `register_model()` + `serve_endpoint()` 纯序列化 + etcd 写入 | **直接可用** |
| **健康检查** | 发送测试 prompt 验证引擎响应，不依赖 GPU API | **直接可用** |
| **Tracing/Request Trace** | OTLP 导出和请求审计，纯平台无关 | **直接可用** |
| **推理引擎 GPU 初始化** | `AsyncLLM.from_vllm_config()` — CUDA context + 权重加载 + KV 分配 | **引擎层适配**（vLLM Ascend） |
| **TP/EP 集体通信** | Dynamo 零 NCCL 调用，仅透传环境变量 | **引擎层适配**（vLLM Ascend HCCL） |
| **KV Cache 跨卡广播** | `#[cfg(feature = "block-manager")]` 门控，仅 MLA + KV 恢复触发 | **可选适配** |
| **K8s Operator** | `nvidia.com/gpu` 硬编码 110+ 处 | 需配置化（仅 K8s 部署） |
| **Prometheus 监控栈** | DCGM exporter 部署 | 需 npu-smi 指标导出器 |
| **DeviceType 元数据** | 只有 `Cpu/Cuda`，Ascend worker 标为 `Cuda` | 极低影响，仅 DeviceAwareWeighted 路由用到 |

### 近期可直接运行的能力

| 能力 | 状态 |
|------|------|
| 单卡推理 | 可用（vLLM Ascend 后端直接工作） |
| 多卡 TP 推理 | 可用（vLLM Ascend 已适配 HCCL，Dynamo 仅透传配置） |
| 扩缩容（多拉/少拉节点） | 可用（控制面全链路零 GPU 依赖） |
| 健康检查端点 | 可用（`/health`、`/live`） |
| FPM 数据管线 | 可用（Schema + ZMQ 传输纯平台无关） |
| Tracing/Request Trace | 可用（OTLP 导出和请求审计不依赖 GPU 硬件） |
| 非 K8s 部署框架 | 可用（VirtualConnector + etcd 协调） |

### 需要适配的部分

| 能力 | 工作量 | 说明 |
|------|--------|------|
| K8s 扩缩容 | 中 | `nvidia.com/gpu` 配置化 + NPU 节点发现（仅 K8s 部署） |
| Prometheus 监控 | 中 | DCGM → npu-smi 指标导出器 |
