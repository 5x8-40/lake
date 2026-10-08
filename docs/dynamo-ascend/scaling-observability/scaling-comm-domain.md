# 通信域管理（TP/EP/PP/DP）

> 专题索引:[README.md](README.md) · 代码级剖析,基于 `3rdparty/dynamo`

大模型推理不是单个 worker 干活，而是一群 worker 组成了一个"通信域"。比如 TP（张量并行）把一个矩阵拆成几份、每个 GPU 算一份；EP（专家并行）让每个 GPU 负责不同的 MoE 专家。这些 worker 通过 NCCL 集体通信（collective）协同工作。关键问题是：一个 worker 挂了，整个通信域怎么办？

### 并行组的生命周期

TP 和 PP 组在 Pod 创建时就确定了，运行期间不能变更。只有 EP 组支持"弹性伸缩"（elastic EP）— 可以在运行时动态加入/移除 DP worker。Dynamo Operator 本身不创建 torch.distributed 的 process group，它只负责编排 Pod 和基础设施；真正的 NCCL/torch.distributed 组由后端框架（vLLM、SGLang）自己组建。

### Worker 故障的三种处理路径

### ① Inter-Pod 故障 — 全组级联删除

单个 Pod 进入 Failed 状态
→ FailoverCascadeController (failover\_cascade\_controller.go:85)
→ 识别 engine group（相同 Grove 标签的所有 Pod）
→ 全部 force delete（grace=0）
→ Grove 重建整个 cohort
→ 全新的 NCCL 集体通信

**原因：** NCCL 集体通信、torch.distributed TCPStore 成员、CUDA IPC 状态无法原地重启 — 部分销毁后会留下半 torn-down 的残留状态。

### ② Intra-Pod 故障 — 单 rank 恢复

Pod 内 active 容器崩溃
→ standby 容器获取 flock 锁
→ 单 rank 恢复
→ 其他 rank 不受影响
→ 共享 GPU（DRA ResourceClaims）

**原因：** active/standby 在同一 Pod 内共享 GPU，standby 已经准备好，只需要接管锁文件。无需 NCCL 重新初始化。


<details><summary>▶ 展开 FailoverCascadeController 代码</summary>

```
// failover_cascade_controller.go:104-114
// "the distributed inference group is already broken when we get here"
// leaving partial NCCL/CUDA IPC state would be worse than clean removal
func reconcileTerminalPhase(ctx, c, pod, ns) error {
  enginePods := groupByEngineLabels(getPodsByNamespace(ns))
  for _, group := range enginePods {
    for _, p := range group {
      grace := int64(0)
      c.Delete(ctx, p, &client.DeleteOptions{GracePeriodSeconds: &grace})
    }
  }
}
```

</details>


### 图解：Worker 故障恢复路径

<!-- SVG diagram: Worker 故障被检测到 — Worker 故障：三种恢复路径 · Worker 故障被检测到 · ▼ -->

