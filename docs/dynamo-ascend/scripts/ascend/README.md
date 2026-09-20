# Ascend bring-up 脚本

| 脚本 | 作用 |
|------|------|
| `start_etcd.sh` | etcd 容器创建 + 拉起（**仅跨机 discovery 需要**；单机用 file，免 etcd） |
| `start_va_dynamo.sh` | vllm-ascend 容器创建 + 容器内拉起 dynamo frontend/worker（`stop` 参数停进程） |
| `start_dynamo_va_native_mc.sh` | Mooncake Transfer Engine 集成版：容器内拉起 frontend/worker + `mooncake_master`，自动生成 `mooncake_config.json`，支持 `MODE=agg`（混部）/ `MODE=pd`（PD 分离） |

用法与参数见 [../e-line.md](../e-line.md)；Mooncake 部署细节见 [Mooncake_部署适配说明.md](Mooncake_部署适配说明.md)。
