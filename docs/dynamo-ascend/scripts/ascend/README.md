# Ascend bring-up 脚本

| 脚本 | 作用 |
|------|------|
| `start_etcd.sh` | etcd 容器创建 + 拉起（**仅跨机 discovery 需要**；单机用 file，免 etcd） |
| `start_va_dynamo.sh` | vllm-ascend 容器创建 + 容器内拉起 dynamo frontend/worker（`stop` 参数停进程） |

用法与参数见 [../e-line.md](../e-line.md)。
