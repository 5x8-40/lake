# va-precopy

在 **vllm-ascend** 上做 Mooncake KV **预复制（pre-copy）**：真实请求打到目标实例之前，用 Mooncake `create_copy_task` 把已有前缀 KV 复制到该实例的本机 DRAM segment，让读路径 local-first，避免跨机拉 KV。

本目录自包含，只依赖：

- 镜像 / 环境：`vllm-ascend`（建议 `v0.26.0rc1`）
- 池：`mooncake_master` + `AscendStoreConnector(backend=mooncake)`
- 控制面：本目录 Python（`create_copy_task` / `query_task` / `batch_get_replica_desc`）

引擎不参与复制；不引入额外控制面框架。

官方 Store 基线：[KV Pool（Ascend Store）](https://docs.vllm.ai/projects/ascend/en/v0.26.0rc1/user_guide/feature_guide/kv_pool.html)

## 拓扑

```text
mooncake_master
     |
+----+----+
|         |
worker-A  worker-B     各跑 vllm OpenAI server + AscendStoreConnector
(源 segment) (目标 segment)
     |
precopy.py             独立进程：create_copy_task → READY
```

时序：

1. 在 worker-A 上暖前缀（KV 写入池，落在 A 的 segment）
2. `precopy.py` 把 key 复制到 B 的 `local_seg`
3. 轮询任务完成 + `batch_get_replica_desc` 确认 B 上有内存副本 → `READY`
4. **之后**再把真实请求打到 worker-B（get session 建立时锁定本机副本）

复制晚于 get session 则本次请求仍可能读远端；下一个请求会重新选副本。

## 目录

| 文件 | 作用 |
|------|------|
| `README.md` | 本文：端到端说明 |
| `common.py` | Mooncake store 封装 |
| `precopy.py` | 控制面：对现网 key 做 `create_copy_task` |
| `store_demo.py` | Store 半程（无 vLLM / 无 NPU，`protocol=tcp`） |
| `keys.py` | 按 AscendStore `PoolKey` 格式枚举 object key |
| `start_master.sh` | 拉起 `mooncake_master` |
| `start_worker.sh` | 拉起单实例 vllm-ascend + AscendStoreConnector |
| `run_store_demo.sh` | 跑 store 半程 |
| `run_e2e.sh` | 双 worker 编排；`DRY_RUN=1` 只打印计划 |
| `conf/mooncake.json.example` | Mooncake 配置样例 |
| `test_keys.py` | `keys.py` 纯单测 |

## 快速开始

### A. 无 NPU：看计划 / 跑 store 半程

```bash
cd va-precopy

# 打印 e2e 步骤（不启进程）
DRY_RUN=1 bash run_e2e.sh

# 有 Mooncake Python wheel + mooncake_master 时：
PROTOCOL=tcp bash run_store_demo.sh
# 成功：PASS - create_copy_task -> local DRAM replica on target
```

### B. 有 NPU：端到端

```bash
cd va-precopy

# 1) master（默认 :50088）
bash start_master.sh

# 2) 两个 Store 实例（各一张卡）
ROLE=A PORT=8001 LOOKUP_ID=0 ASCEND_RT_VISIBLE_DEVICES=0 \
  MODEL=/data/models/Qwen2.5-7B-Instruct \
  bash start_worker.sh

ROLE=B PORT=8002 LOOKUP_ID=1 ASCEND_RT_VISIBLE_DEVICES=1 \
  MODEL=/data/models/Qwen2.5-7B-Instruct \
  bash start_worker.sh

# 3) 在 A 上暖前缀
curl -s http://127.0.0.1:8001/v1/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen","prompt":"va-precopy shared prefix for store warmup. ","max_tokens":1,"temperature":0}'

# 4) 准备 key 名单（chunk hash = 引擎 block hash hex）
python3 keys.py --model-name qwen --chunk-hashes <hex1>,<hex2> --out prefix_keys.txt

# 5) 目标 segment = worker-B 的 local_seg（见 worker-B 日志 MooncakeBackend）
python3 precopy.py \
  --master 127.0.0.1:50088 \
  --protocol ascend \
  --target '<B-local_seg>' \
  --keys-file prefix_keys.txt \
  --dry-show-before
# 成功：READY

# 6) READY 后再打 B
curl -s http://127.0.0.1:8002/v1/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen","prompt":"va-precopy shared prefix for store warmup. ","max_tokens":16,"temperature":0}'
```

或填好环境变量后一键：

```bash
TARGET_SEGMENT='<B-local_seg>' KEYS_FILE=prefix_keys.txt \
  MODEL=/data/models/Qwen2.5-7B-Instruct \
  bash run_e2e.sh
```

## Worker 配置要点

`start_worker.sh` 只挂 **AscendStoreConnector**：

```json
{
  "kv_connector": "AscendStoreConnector",
  "kv_role": "kv_both",
  "kv_connector_extra_config": {
    "backend": "mooncake",
    "lookup_rpc_port": "<LOOKUP_ID>",
    "use_layerwise": false
  }
}
```

并设置 `MOONCAKE_CONFIG_PATH` 指向本目录生成的 `conf/mooncake.<ROLE>.json`（`protocol` 在 NPU 上为 `ascend`）。

`lookup_rpc_port` 在 AscendStore 里是 **lookup id 后缀**（拼本地 IPC 路径），不是 TCP 端口；A/B 用不同 id（0/1）避免冲突。

## Key 与 segment

- **Object key**：与 AscendStore put/get 同一命名空间。`keys.py` 镜像 vllm-ascend 0.26 `PoolKey.to_string` / `LayerPoolKey.to_string`。容器内可加 `--prefer-upstream` 直接调安装树里的类型。
- **Segment 名**：worker 侧 `MooncakeBackend.local_seg`，一般为 `hostname:rpc_port`。从 worker-B 启动日志确认后传给 `precopy.py --target`。
- **chunk hash**：当前需从引擎 block hash / 日志取得；`keys.py` 接受已知 hex，不从 prompt 自动推导。

## 验收

| 步骤 | 判据 |
|------|------|
| `DRY_RUN=1 bash run_e2e.sh` | 打印 master / A / B / precopy 步骤 |
| `PROTOCOL=tcp bash run_store_demo.sh` | `PASS`；AFTER 列表含 source+target |
| `precopy.py` | 打印 `READY`；各 key 的 endpoint 含 `--target` |
| 打 worker-B | 日志可见本机副本 / 跨机读减少（视日志级别） |

## 边界

- 源属主客户端（贡献源 segment 的 worker-A）必须在线，复制才由源端后台线程执行。
- 复制与正常读共享带宽；批量预复制需自行限速。
- 池副本受 Mooncake 驱逐策略管理，无 pin。
- `protocol`：store 半程用 `tcp`；接 vllm-ascend NPU 集群用 `ascend`，须与 `mooncake.json` 一致。
