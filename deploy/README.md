# P3 本地全栈

跨语言最小路径（无 Bifrost、无 RDMA）：

```
curl → Go Router (:8080)
         ├─ AgentService.Dispatch + TransferService (:50054)  # Dispatch + 节点间传输
         └─ WorkerService.Generate (:50053)
                ├─ ControlPlane Lookup/Register (:50051)
                └─ TcpDataService Put/Get (:50052)           # TCP 退化数据面
```

## 依赖

- Rust toolchain（`cargo`）
- Go 1.22+
- Python 3 + `grpcio` / `protobuf`（与 `python/lake/pb` 一致）

## 起栈

```bash
# 仓库根
./deploy/run-local.sh
```

另开终端冒烟（共享前缀 ≥3 blocks，第二次 `reused_blocks>=3`）：

```bash
./deploy/smoke.sh
```

编译门禁（不起进程）：

```bash
./scripts/verify-p3.sh
```

## 端口

| 服务 | 默认 | 环境变量 |
|------|------|----------|
| ControlPlane | `50051` | `LAKE_CP_ADDR` |
| TcpDataService | `50052` | `LAKE_KV_ADDR` |
| Python Worker | `50053` | `LAKE_WORKER_BIND` / `LAKE_WORKER_ADDR` |
| Storage Agent | `50054` | `LAKE_AGENT_ADDR` |
| Router HTTP | `8080` | `LAKE_HTTP_ADDR` |

端口占用时 `run-local.sh` 直接失败，避免连到残留进程造成假复用。

## 手动 curl

```bash
curl -sS http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mock-llm","messages":[{"role":"user","content":"hi"}],"max_tokens":4}'
```

响应 JSON 含 `lake.reused_blocks` / `lake.prefill_blocks` / `lake.mode`（P3 固定 `COLOCATED`）。
