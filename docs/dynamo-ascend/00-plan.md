# 00-plan: Dynamo-Ascend 可交付交付物（lake 文档 + 脚本）

## 边界

| 仓 | 放什么 |
|----|--------|
| [5x8-40/dynamo-ascend](https://github.com/5x8-40/dynamo-ascend) | 代码改动（含 `MooncakeConnectorV1` 协议注册，见 [#2](https://github.com/5x8-40/dynamo-ascend/pull/2)） |
| **本目录 + `scripts/dynamo-ascend/`** | 文档与拉起脚本；**不**再维护 protocol patch |

## 交付内容

1. **Bring-up**：容器内编译安装 dynamo-ascend，聚合 FE + worker — [bringup.md](bringup.md)
2. **PD + Mooncake + KV router + 跨机 + 卸载**：MultiConnector 拉起与核实 — [pd-mooncake.md](pd-mooncake.md)

## 依赖

- 镜像：`quay.io/ascend/vllm-ascend:v0.26.0rc1`（需 NPU）
- 源码：`5x8-40/dynamo-ascend`（`ascend-dev`，含协议 PR）
- 协议：合入 dynamo-ascend，**lake 无 patch**
