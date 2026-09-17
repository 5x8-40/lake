# D001: 总体路线——fork Dynamo 做昇腾适配,不自研框架

- 日期:2026-09-17
- 状态:已定

## 背景

昇腾生态已有计算引擎适配(vllm-ascend、SGLang Ascend),但没有 Dynamo 的昇腾支持。Dynamo 是 NVIDIA 的分布式推理运行时(Router / PD 分离 / KV 管理 / 传输 / 扩缩一体)。问题:做"dynamo-ascend"应该自研一套对标框架,还是 fork 适配?

本文蒸馏自 2026-09-17 的一次外部讨论(chat-export),其中的过时与错误点已对照上游源码与调研文档修正(见下"修正")。

## 选项

1. **自研对标框架**:重复造轮子。昇腾侧已有数据底座(memcache/UCM/Yuanrong),引擎侧已有 vllm-ascend;再写一套控制面既重复又无法与上游兼容。
2. **fork Dynamo 做硬件适配**:控制面复用上游,执行层复用 vllm-ascend,只替换存储接口与网络传输层。与 vllm-ascend 对 vLLM 的关系同构。

## 决策

选 2。已执行:fork `ai-dynamo/dynamo` → [`5x8-40/dynamo-ascend`](https://github.com/5x8-40/dynamo-ascend),以 submodule 挂在本仓 `3rdparty/dynamo-ascend`(`ascend-dev` 分支,当前为上游 main 快照,尚无适配代码)。

分层适配点:

| 层 | 上游组件 | 适配动作 |
|----|----------|----------|
| 控制面 | Router / Planner / KVCR 策略 | 尽量复用,不动 |
| 执行层 | vLLM/SGLang 后端 | 对接 vllm-ascend(引擎适配已存在) |
| 数据面传输 | NIXL | 换昇腾传输:UB/URMA 或 MemFabric(候选评估见 [../data-plane-options.md](../data-plane-options.md)) |
| KV 存储后端 | KVCR 二级存储后端 | 换昇腾数据底座:memcache / Yuanrong 候选(同上) |
| Worker 生命周期 | Worker Launcher / 镜像 / K8s 资源 | 需重写,差异清单见下 |

Worker 拉起差异清单(讨论中确认,实操时逐条核对):

- 环境变量:`ASCEND_RT_VISIBLE_DEVICES` 替代 `CUDA_VISIBLE_DEVICES`,另有 `HCCL_CONNECT_TIMEOUT` 等 CANN/HCCL 变量;
- 进程内必须显式 `import torch_npu` 完成设备初始化,启动参数带 `--device npu`;
- 容器镜像需基于 CANN 基础镜像重建(含 `torch_npu` / `vllm-ascend` / HCCL);
- K8s 资源申请从 `nvidia.com/gpu` 改为昇腾 NPU 资源标识(如 `huawei.com/Ascend910`);
- 存活/就绪探针超时与路径需按 `torch_npu` + HCCL 初始化耗时调整。

## 修正(讨论中的过时/错误点,已对照上游源码与调研文档核实)

1. **KVBM 已 sunset**:讨论中"控制面(Router/KVBM)直接复用"的说法过时——上游 KVBM 2026-07 被官方废弃(DEP #11673),继任者是 **KVCR**(引擎进程内二级存储 + router hint 驱动 P2P,分析见 [`../../research/kvcr/overview.md`](../../research/kvcr/overview.md))。存储/传输适配必须对标 KVCR 的接口与后端位,不是 KVBM。
2. **Dynamo Router 已有拓扑感知选路**(Topology-Aware KV Transfer,experimental;`docs/fern/pages/developer-guide/knowledge-base/modular-components/router/topology-aware-kv-transfer.md`):
   - 工作原理:
     - worker 在 `ModelRuntimeConfig` 发布 `topology_domains` 逻辑域标签,如 `{"zone": "us-east-1a", "rack": "rack-22"}`;
     - 每个域自动生成 worker 污点 `dynamo.topology/<domain>=<value>`;
     - prefill worker 选定后,Prefill Router 把它的传输域(`kv_transfer_domain`)转成 decode 选路约束;
     - K8s 侧经 DGD 的 `spec.experimental.kvTransferPolicy` 配置。
   - 约束强度两档:
     - `required`:硬约束,推不出约束就直接拒请求,不放行;
     - `preferred`:加权偏好。
   - 机制边界:域是**逻辑标签**,成员关系由部署侧标注,Router 不自动发现物理拓扑。
   - 对昇腾的含义:把 UB 超节点域映射成 topology domain 并标注到 worker,即可复用该机制;要新做的是"UB 域成员的发现与标注",不是 Router 本身。
3. **讨论里那份"在用 Dynamo 的公司名单"不作为选型依据**。名单里我们只核实了一条:上游仓库确实有 Kimi K3 的部署示例(`docs/fern/pages/recipes/model-recipes/kimi-k3.mdx`);其余条目未逐一核实,可信度不明。选 Dynamo 的理由是控制面复用价值本身,不需要靠用户名单背书。

## 后果

- 好处:站在上游控制面与生态上,工程量集中在数据面与 Worker 生命周期;上游持续演进可直接吸收。
- 代价:需维护 fork 与上游的同步策略(rebase/merge 节奏、冲突面)——待立 D003 决策。
- 后续:数据底座选型(memcache vs Yuanrong vs 自研薄层)待评估,见 [../data-plane-options.md](../data-plane-options.md);传输层 UB/URMA 能力需官方资料核实。
