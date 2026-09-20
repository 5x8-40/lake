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
  "local_buffer_size": 4294967296
}
```

可通过环境变量调整：`MOONCAKE_RPC_PORT`、`MC_MASTER_ADDRESS`、
`MC_GLOBAL_SEGMENT_SIZE`、`MC_LOCAL_BUFFER_SIZE`、`MOONCAKE_CONFIG_PATH`。

如需自定义配置，可提前创建文件并导出其路径：

```bash
export MOONCAKE_CONFIG_PATH=/path/to/mooncake_config.json
```

注意：脚本每次启动都会覆盖生成该配置文件。

### 2. 启动服务

```bash
export WM_ROOT=/data/wm
bash start_docker_va.sh
bash start_etcd.sh
bash start_dynamo_va_native_mc.sh              # 默认 MODE=agg（混部）
MODE=pd bash start_dynamo_va_native_mc.sh      # PD 分离（prefill 4卡 + decode 4卡）
```

脚本通过 `MODE` 环境变量选择部署形态：

| MODE | 形态 | NPU 布局 | 可调变量 |
|------|------|---------|---------|
| `agg`（默认） | prefill+decode 混部单 worker | 0-7, TP4×DP2 | `TP` / `DP` |
| `pd` | PD 分离，两组 worker | prefill 0,1,2,3 / decode 4,5,6,7（各 TP4×DP1） | `P_NPU`/`P_TP`/`P_DP`、`D_NPU`/`D_TP`/`D_DP` |

PD 模式约束：`P_NPU` 数量必须等于 `P_TP*P_DP`（decode 同理），脚本启动时会校验。

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

脚本内实际拉起的 worker 命令如下。`--disaggregation-mode agg` 为混部模式
（aggregated）：prefill 与 decode 在同一个 worker 内执行，不是 PD 分离；
PD 分离使用 `prefill` / `decode` 模式分别拉起两组 worker。

混部（MODE=agg）：

```bash
python3 -m dynamo.vllm \
  --model /data/models/Qwen3.8-27B --served-model-name qwen \
  --tensor-parallel-size 4 --data-parallel-size 2 \
  --discovery-backend etcd --request-plane tcp --response-plane tcp \
  --disaggregation-mode agg --trust-remote-code \
  --gpu-memory-utilization 0.9 --max-model-len 32768 \
  --max-num-seqs 64 \
  --enable-prefix-caching \
  --dyn-tool-call-parser qwen3_coder \
  --kv-transfer-config '{
    "kv_connector": "MooncakeConnector",
    "kv_role": "kv_both"
  }'
```

PD 分离（MODE=pd）：prefill worker 先起（注册为 `prefill` 组件），decode worker
后起（注册为 `backend` 组件），frontend 依次路由 prefill → decode；两侧 NPU
通过 `ASCEND_RT_VISIBLE_DEVICES` 隔离。

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
    "kv_connector": "MooncakeConnector",
    "kv_role": "kv_both"
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
    "kv_connector": "MooncakeConnector",
    "kv_role": "kv_both"
  }'
```

关于 `kv_role`：`kv_both` 指该进程的 KV 传输通道双向使能（能发能收），与
prefill/decode 服务角色无关。PD 分离下两侧仍用 `kv_both`（与上游
`recipes/deepseek-r1/vllm/disagg` 等 recipe 一致），方向由实际数据流决定；
`kv_producer`/`kv_consumer` 为单向锁定的严格写法，仅部分 recipe 使用。

### 4. 验证

已在昇腾 910B（单机 8 卡，Qwen3.8-27B）真机验证通过：

```bash
curl -s localhost:8000/v1/models
curl -s localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"hi"}],"max_tokens":32}'
```

---

## 踩坑记录：Mooncake 适配 Qwen3.8-27B 的 KV 格式问题

### 问题

Qwen3.8-27B 是混合注意力架构：64 层中 16 层是 Full Attention，48 层是 Gated DeltaNet（GDN）线性注意力。

在 mooncake_connector.py 的 register_kv_caches 方法中，get_transfer_cache_regions 返回的 cache_list 对某些层（如 GDN 层）可能返回的是嵌套的 tuple/list 结构（例如 (k_cache, v_cache) 或 (state_tensor,)），而不是扁平的 tensor 列表。

原代码直接遍历 cache_list，当遇到 tuple/list 时，会把整个 tuple 当作一个 item 处理，导致：

1. cache.data_ptr() 报错（tuple 没有 data_ptr）
2. 或者 stride 计算错误，注册失败

### 解决方案

在 register_kv_caches 中，遍历 cache_list 之前，加一段展平逻辑，将嵌套的 tuple/list 展开为扁平的 tensor 列表：

```python
# ===== 修正：展平 cache_list，但保留原始数量用于日志 =====
original_cache_count = len(cache_list)
flat_cache_list = []
for item in cache_list:
    if isinstance(item, (tuple, list)):
        for sub_item in item:
            if isinstance(sub_item, torch.Tensor):
                flat_cache_list.append(sub_item)
    elif isinstance(item, torch.Tensor):
        flat_cache_list.append(item)
cache_list = flat_cache_list
# ===== 修正结束 =====
```

修改文件路径：/vllm-workspace/vllm/vllm/distributed/kv_transfer/kv_connector/v1/mooncake/mooncake_connector.py，约 1693 行。

