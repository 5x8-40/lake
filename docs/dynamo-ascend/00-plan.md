# 00-plan: Dynamo-Ascend 可交付交付物（lake 文档 + 脚本）

## 边界

| 仓 | 放什么 |
|----|--------|
| [5x8-40/dynamo-ascend](https://github.com/5x8-40/dynamo-ascend) **`ascend-dev-1.4.2`** | 1.4.2 交付线代码（`MooncakeConnectorV1` 协议 + Kunpeng `generic` aarch64） |
| **本目录 + `scripts/dynamo-ascend/`** | 文档与拉起脚本；**不**再维护 protocol patch |

> 不用 `ascend-dev`（当前 1.5.0）。本机验证与版本锁定：**dynamo 1.4.2 ↔ vllm-ascend 0.26.0rc1**。

## 交付内容

1. **Bring-up**：容器内编译安装 dynamo-ascend，聚合 FE + worker — [bringup.md](bringup.md)
2. **PD + Mooncake + KV router + 跨机 + 卸载**：MultiConnector 拉起与核实 — [pd-mooncake.md](pd-mooncake.md)

## 依赖

- 镜像：`quay.io/ascend/vllm-ascend:v0.26.0rc1`（需 NPU）
- 源码：`5x8-40/dynamo-ascend` @ **`ascend-dev-1.4.2`**
- 协议：在该分支内，**lake 无 patch**
