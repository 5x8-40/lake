# Dynamo + vLLM-Ascend + Mooncake 部署适配说明

## 环境

- 硬件：昇腾 910B（单机 8 卡）
- 模型：Qwen3.8-27B
- 软件：vLLM-Ascend、Mooncake（Ascend 协议，观察到通信使用RDMA）、Dynamo、etcd

## 部署步骤

### 1. 启动服务（Mooncake 配置自动生成）

mooncake_config.json 由 `start_dynamo_va_native_mc.sh` 在容器内自动生成，路径为
`$WM_ROOT/mooncake_conf/mooncake_config.json`，无需手工准备。内容如下：

```json
{
  "protocol": "ascend",
  "use_ascend_direct": true,
  "master_server_address": "127.0.0.1:50058",
  "global_segment_size": 4294967296,
  "local_buffer_size": 4294967296,
  "metadata_server": ""
}
```

`metadata_server` 必须显式给空串：单 mooncake_master 直连模式下该字段用不到
（它语义是 master HA 的 etcd 选主地址），但 vllm-ascend 的
`mooncake_backend.py::_setup_store` 会把它原样传给
`MooncakeDistributedStore.setup()`，缺省时取到 `None`，而 mooncake Python
binding（pybind11）要求该参数为 `str`，传 `None` 直接 TypeError 崩 worker。

可通过环境变量调整：`MOONCAKE_RPC_PORT`、`MC_MASTER_ADDRESS`、
`MC_GLOBAL_SEGMENT_SIZE`、`MC_LOCAL_BUFFER_SIZE`、`MOONCAKE_CONFIG_PATH`。
PD 模式下另有：`KV_PORT_PREFILL`/`KV_PORT_DECODE`（MooncakeConnectorV1 传输端口，
默认 20001/20002）、`MC_LIB_DIR`（mooncake 动态库目录，会注入容器内
`LD_LIBRARY_PATH`）。脚本同时导出 `PYTHONHASHSEED=0`（官方 kv_pool 样例要求）。

如需自定义配置，可提前创建文件并导出其路径：

```bash
export MOONCAKE_CONFIG_PATH=/path/to/mooncake_config.json
```

注意：脚本每次启动都会覆盖生成该配置文件。

### 2. 启动服务

脚本只支持 PD 分离（混部模式已移除）：

```bash
export WM_ROOT=/data/wm
bash start_docker_va.sh
bash start_etcd.sh
bash start_dynamo_va_native_mc.sh      # PD 分离（prefill 4卡 + decode 4卡）
```

| 形态 | NPU 布局 | 可调变量 |
|------|---------|---------|
| PD 分离，两组 worker | prefill 0,1,2,3 / decode 4,5,6,7（各 TP4×DP1） | `P_NPU`/`P_TP`/`P_DP`、`D_NPU`/`D_TP`/`D_DP` |

约束：`P_NPU` 数量必须等于 `P_TP*P_DP`（decode 同理），脚本启动时会校验。

其中 start_dynamo_va_native_mc.sh 中新增的 Mooncake 集成逻辑如下：

```bash
# --- Mooncake Integration Start ---
export MOONCAKE_CONFIG_PATH='$MOONCAKE_CONFIG_PATH'

if ! ss -tlnp | grep -q ":$MOONCAKE_RPC_PORT "; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting mooncake_master on port $MOONCAKE_RPC_PORT..." >> '$LOGDIR/mooncake.log'
  nohup mooncake_master \
    --rpc_port $MOONCAKE_RPC_PORT \
    --eviction_high_watermark_ratio 0.9 \
    --rpc_thread_num 32 \
    >> '$LOGDIR/mooncake.log' 2>&1 &
  MC_PID=$!
  echo $MC_PID > '$LOGDIR/mooncake.pid'
  sleep 2
  if kill -0 $MC_PID 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] mooncake_master started successfully (PID: $MC_PID)" >> '$LOGDIR/mooncake.log'
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: mooncake_master failed to start, check log above" >> '$LOGDIR/mooncake.log'
    exit 1
  fi
else
  EXISTING_PID=$(ss -tlnp | grep ":$MOONCAKE_RPC_PORT " | grep -oP 'pid=\K[0-9]+' | head -1)
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] mooncake_master already running on port $MOONCAKE_RPC_PORT (PID: $EXISTING_PID), skipping startup" >> '$LOGDIR/mooncake.log'
fi
# --- Mooncake Integration End ---
```

### 3. 拉起 vLLM 服务

脚本拉起 prefill / decode 两组 worker：prefill worker 先起（注册为 `prefill`
组件），decode worker 后起（注册为 `backend` 组件），frontend 依次路由
prefill → decode；两侧 NPU 通过 `ASCEND_RT_VISIBLE_DEVICES` 隔离。

使用 `MultiConnector` 组合两个子 connector（配置格式来自 vLLM-Ascend
kv_pool 官方样例）：

- **MooncakeConnectorV1**：负责 P/D 间 KV transfer（走 Mooncake Transfer Engine），
  prefill 侧 `kv_role=kv_producer` + `kv_port=20001`，decode 侧
  `kv_role=kv_consumer` + `kv_port=20002`；
- **AscendStoreConnector**：作为前缀缓存节点（`backend: mooncake`，对接本脚本
  拉起的 `mooncake_master`），prefill 侧 `lookup_rpc_port=0`（不启 lookup RPC），
  decode 侧 `lookup_rpc_port=1`（启 lookup RPC 查前缀）。

```bash
# prefill worker（NPU 0-3）
ASCEND_RT_VISIBLE_DEVICES=0,1,2,3 python3 -m dynamo.vllm \
  --model /data/models/Qwen3.8-27B --served-model-name qwen \
  --tensor-parallel-size 4 --data-parallel-size 1 \
  --discovery-backend etcd --request-plane tcp --response-plane tcp \
  --disaggregation-mode prefill --trust-remote-code \
  --gpu-memory-utilization 0.9 --max-model-len 32768 \
  --max-num-seqs 64 \
  --enable-prefix-caching \
  --dyn-tool-call-parser qwen3_coder \
  --kv-transfer-config '{
    "kv_connector": "MultiConnector",
    "kv_role": "kv_producer",
    "kv_connector_extra_config": {
      "connectors": [
        {
          "kv_connector": "MooncakeConnectorV1",
          "kv_role": "kv_producer",
          "kv_port": "20001",
          "kv_connector_extra_config": {
            "prefill": {"dp_size": 1, "tp_size": 4},
            "decode": {"dp_size": 1, "tp_size": 4}
          }
        },
        {
          "kv_connector": "AscendStoreConnector",
          "kv_role": "kv_producer",
          "kv_connector_extra_config": {
            "backend": "mooncake",
            "lookup_rpc_port": "0"
          }
        }
      ]
    }
  }'

# decode worker（NPU 4-7）
ASCEND_RT_VISIBLE_DEVICES=4,5,6,7 python3 -m dynamo.vllm \
  --model /data/models/Qwen3.8-27B --served-model-name qwen \
  --tensor-parallel-size 4 --data-parallel-size 1 \
  --discovery-backend etcd --request-plane tcp --response-plane tcp \
  --disaggregation-mode decode --trust-remote-code \
  --gpu-memory-utilization 0.9 --max-model-len 32768 \
  --max-num-seqs 64 \
  --enable-prefix-caching \
  --dyn-tool-call-parser qwen3_coder \
  --kv-transfer-config '{
    "kv_connector": "MultiConnector",
    "kv_role": "kv_consumer",
    "kv_connector_extra_config": {
      "connectors": [
        {
          "kv_connector": "MooncakeConnectorV1",
          "kv_role": "kv_consumer",
          "kv_port": "20002",
          "kv_connector_extra_config": {
            "prefill": {"dp_size": 1, "tp_size": 4},
            "decode": {"dp_size": 1, "tp_size": 4}
          }
        },
        {
          "kv_connector": "AscendStoreConnector",
          "kv_role": "kv_consumer",
          "kv_connector_extra_config": {
            "backend": "mooncake",
            "lookup_rpc_port": "1"
          }
        }
      ]
    }
  }'
```

关于 `kv_role`：PD 分离下用 `kv_producer`/`kv_consumer` 单向锁定（官方样例
写法），方向与 prefill/decode 角色一致——prefill 侧只发，decode 侧只收。

### 4. 验证

已在昇腾 910B（单机 8 卡，Qwen3.8-27B）真机验证通过：

```bash
curl -s localhost:8000/v1/models
curl -s localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"hi"}],"max_tokens":32}'
```

### 脚本参考

https://docs.vllm.ai/projects/vllm-ascend-cn/zh-cn/latest/user_guide/feature_guide/kv_pool.html
