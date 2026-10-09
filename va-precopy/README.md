# va-precopy

在 **vllm-ascend** 上做 Mooncake KV **预复制（pre-copy）**：真实请求打到目标实例之前，用 Mooncake `create_copy_task` 把已有前缀 KV 复制到该实例的本机 DRAM segment，让读路径 local-first，避免跨机拉 KV。

本目录自包含，只依赖：

- 镜像 / 环境：`vllm-ascend`（建议 `v0.26.0rc1`）
- 池：`mooncake_master` + `AscendStoreConnector(backend=mooncake)`
- 控制面：本目录 Python（`create_copy_task` / `query_task` / `batch_get_replica_desc`）

引擎不参与复制；不引入额外控制面框架。

官方 Store 基线：[KV Pool（Ascend Store）](https://docs.vllm.ai/projects/ascend/en/v0.26.0rc1/user_guide/feature_guide/kv_pool.html)

## 验证状态

镜像：`quay.io/ascend/vllm-ascend:v0.26.0rc1`；`preferred_segment: true`；A2 RoCE。主机：`7.242.105.245` / `7.242.105.217`。

### 同构 rank↔seg（新路径，2026-10-09）

| 场景 | 结果 |
|------|------|
| **同机 TP=2**（VL-8B，`DEVICES_A=0,1` / `B=2,3`） | **通过**：12 keys（rank0/1×6 满块）；rank0→B:`15466`、rank1→B:`15594` READY；打 B 后 `hits=768` / `queries=801` ≈ **95.9%** |

说明：旧 master 日志无 `mount_segment` 时 `resolve_segments.sh` 会失败——可从 `worker_B.log` 的 `Transfer Engine RPC ... listening on IP:port` 取两段作 `--targets`（按 rank 序）。`precopy` 退出偶发 allocator abort，**已打印 READY 即可忽略**。

### 历史（2026-10-08，旧 key×N 广播路径）

| 场景 | 结果 |
|------|------|
| 同机/跨机 TP=1/2（VL-8B） | 通过；hit ≈95%+ |
| 同机/跨机 TP=4（27B Mamba） | 通过；≈92.1%（满块） |
| 未开 A2 RoCE | 未通过（`HcclBatchPut=4`） |

跨机要点：master `0.0.0.0:50088`；B 的 `MC_MASTER`/`LOCAL_IP`/`HCCL_IF_IP`。Mamba：`--block-size 1536`。

## 实验总表（2026-10-08 ~ 10-09 全量）

模型除注明外均为 `/data/models/Qwen3-VL-8B-w8a8c16`（GQA 8 KV head，block_size 128，`PYTHONHASHSEED=0`）；主机 245=7.242.105.245，217=7.242.105.217；容器镜像 `quay.io/ascend/vllm-ascend:v0.26.0rc1`；mooncake wheel `mooncake_transfer_engine_npu-0.3.11.post1`（除注明外）；master 在 245 宿主机 `:50088`。

| # | 时间 | 环境 | 实验 | 结果 | 证据 |
|---|------|------|------|------|------|
| 1 | 10/08 | 单机 245，TP=1/2 | B2 首验（旧 key×N 广播路径） | 通过，hit ≈95%+ | 见「历史」表 |
| 2 | 10/08 | 245↔217 跨机，TP=1/2 | 跨机 B2 | 通过，hit ≈95%+ | 同上 |
| 3 | 10/08 | 跨机，TP=4，27B Mamba | 满块大模型跨机 | 通过 ≈92.1%（`--block-size 1536`） | 同上 |
| 4 | 10/08 | 单机 TP=2 | 对照：不开 A2 RoCE | **未通过**（`HcclBatchPut=4`）——跨卡 copy 必须开 A2 RoCE | 同上 |
| 5 | 10/09 午 | 单机 245：A=TP2(卡0,1)→B=TP4(卡2-5)，`prefill_tp_size=2 decode_tp_size=4` | 异构正向 e2e：warm→collect(24 eff keys)→precopy→hit | 首跑死于 collect（上游 put 死代码）→**补丁后通过**：24/24 key、B external hit **95.9%**(768/801)、零 invalid、TP0-3 各 `backend get keys=6`、A/B 16 token 输出逐字一致 | `logs/worker_A.log`（`tp_mismatch put keys=12`×2 rank）、`logs/e2e_hetero_2_4.log` |
| 6 | 10/09 午 | 单机 245：A=TP4(卡0-3)→B=TP2(卡4,5)，B `LOAD_ASYNC=1` | 异构反向 e2e | 通过：24/24、B **95.9%** 零 invalid、TP0/TP1 各 `tp_mismatch get keys=12`（异步路径）、输出一致。踩坑：B 配 `decode_tp_size=2`(==local) 不启用 mismatch → 必须 `=4` | `logs/worker_A.log`/`worker_B.log` |
| 7 | 10/09 | 容器内 `vllm_ascend` 源码 | 上游 bug 修复实验：`pool_worker.py::_start_kv_transfer_threads` 构造 `KVCacheStore{Sending,Recving}Thread` 漏传 `worker=self` → `kv_transfer.py` tp_mismatch put/get 分支双双死代码 | `patch_tp_mismatch_worker.py` 幂等补丁，put 通路打通（#5 的 24/24 即补丁效果）；get 通路靠 #6 异步路径 | `patch_tp_mismatch_worker.py`、`config_data.py::infer_tp_mismatch_info` |
| 8 | 10/09 晚 | 跨机 245→217：A=TP2(卡0,1)→B=TP4(卡0-3) | 异构跨机 e2e（三轮 precopy 验证稳定性） | **通过**：24/24 跨机 sub-key put；24/24 `replica_copy_success`（A 侧 ~1 key/s）；副本对号落 217 四 seg（`check_exists` 逐 key 双副本端点取证）；B 首请求 **95.9%** 零 invalid；DEBUG 下 TP0-3 各 `MooncakeBackend.get enter keys=6`；A/B 16/64 token 输出逐字一致 | `logs/worker_A.log`（72 次 `replica_copy_success`）、master 日志 `mount_segment`（217:16300/15247/16469/15472 等） |
| 9 | 10/09 晚 | 245 宿主机 RoCE 网卡打点（`ip -s link enp67s0f5` 前后差值） | **读本机副本判定**（单次 B 命中 16 token） | ~~TX **+0.47 MB** / RX +0.13 MB；若读远端应 ~44 MB ⇒ get 读的是 217 本机 seg 副本~~ **10/09 深夜撤回：阳性对照实测 netdev 计数器不统计昇腾 RoCE RDMA 流量（24 keys ~43 MB 真实跨机传输，两侧计数器增量均 <2 MB），本行证据方法论作废**。读本机副本改由 #12 杀源实例法铁证。注：worker 日志里的 `Connected to segment: 245` 是 precopy copy-task 收尾连接（与 get 同秒重叠），非读取证据 | 本次会话打点记录；`/tmp/nic_before.txt`/`after.txt`（245） |
| 10 | 10/09 | B(217) 重复请求确定性观察 | 同 prompt 反复打 B（16/64 token × 多次）+ 冷 prompt 对照 | 旧非 DEBUG 实例：**奇数次输出与 A 一致、偶数次近平局 token 处 1-token 翻转**（"warmup. va" → "warmup.  va"）；A 3×64 token 稳定；B 冷 prompt 3× 稳定；0.3.11+DEBUG 新实例 3 连跑未复现。疑外部加载路径交替的数值扰动，非字节错乱（纯外部读首请求与 A 逐字一致） | 本会话输出记录；根因待复现 |
| 11 | 10/09 晚 | 217 容器 wheel 升级 `0.3.13.post1` | 尝试升级 mooncake（上游 1fc27b6 已有 `SelectCompleteMemoryReplica` 本机优先） | **失败**：mount 4 seg 后 EngineCore 挂起 15+ 分钟（APIServer `Waiting for 1 local core engine proc`），疑与旧 master 兼容性；回退 `0.3.11.post1` 后恢复正常（#8/#9 即回退后所测） | master 日志、`worker_B.log`（17:03-17:18） |
| 12 | 10/09 深夜 | 跨机 245→217 异构重验（全新 prompt ×2：961/801 tok）+ **杀源实例终极判定** | **通过**：28/24 keys 全对号入池（master 独立复核）；杀 A + client TTL 注销（`batch_get_replica_desc` 仅剩 217 副本）后 **B 首中**：外部 get `enter keys=6` ×4 rank、`get returned token_len=768` 同秒成功、external hit 累计 89.8%（与 768/897 增量精确自洽）、零 invalid、输出与 A 逐字一致 ⇒ **get 读本机 seg 副本铁证**。两个新认知：① 同 prompt 第二次命中被 vLLM 内部 HBM prefix cache 整体接住（外部 get=0；内部累计 24.9%=896/3620 精确自洽）——本机读判定必须用**全新 prompt 首中**；② netdev 计数器不计 RDMA（见 #9 撤回）。开放问题：双副本并存时 replica 选择策略（0.3.11 无显式本机优先，实测首中未新建远端连接） | `logs/worker_B.log`（get 11829-11841、metrics 12203 行）；`prefix_keys_reverify.txt`/`prefix_keys_final.txt` |

**踩坑记录（跨机操作）**：
- 217 容器残留 10/8 旧 worker：vLLM `setproctitle` 后进程名为 `VLLM::EngineCore/Worker_TP/APIServer`，`ps | grep python|vllm` **大小写躲过**；占 8002 端口与卡 0-3 显存，新 B 假 READY（`/v1/models` 由旧实例应答）。清理用 `pgrep -f "VLLM::[W]"`（括号防 pgrep 自匹配）+ `npu-smi info -t usages -i <id>` 验 HBM 释放。
- master 日志被轮转后 fd 仍在改名文件（`.bak.27b`）：`ln -sf mooncake_master.log.bak.27b logs/mooncake_master.log` 修复 `resolve_segments.sh`。
- `precopy.py` 客户端退出偶发 allocator abort（core dumped, RC=134）/挂起：**拷贝工作已完成**（以源侧 `replica_copy_success` 计数为准），可忽略。
- 非 DEBUG 启动无 `MooncakeBackend.get enter keys=` 分 rank 证据：需要 `VLLM_LOGGING_LEVEL=DEBUG`。
- `worker_A.log` 被 10/09 深夜两次重启覆盖（`nohup >` 截断），早期 72 次 `replica_copy_success` 计数以本表文字为准；重启 A 后需重 warm 再 collect。
- 跨机非交互 SSH：本机无 sshpass/密钥，`expect` + 密码可用；高频连接触发对端限速（认证后断连/KEX 卡死），需静置恢复或改控制台人工执行。

**进度决策（2026-10-09）**：同构 rank↔seg 已验证；**异构 TP 双向（TP2→TP4 / TP4→TP2）已验证**（见下节；需容器补丁 + 小 TP 消费者 `load_async=1`）；**跨机异构 TP2(245)→TP4(217) 已验证且读本机副本经杀源实例法铁证**（总表 #12）。未做：layerwise、编排层、落盘、copy 并行。

## 实现现状（同构多 TP / 多 key）

B2：请求前把前缀 KV 放到目标实例本机 DRAM。同构 TP（A/B 同 `tp_size`）下对齐 AscendStore 命名空间：

术语：

- **seg**：`IP:rpc_port`，一 rank 一个 `local_seg`。
- **key**：一个满 KV block 的 object key；**一请求多 key**（按 `block_size` 切满块；尾巴不满块一般不进列表 → hit ≈ 满块 tokens/总 prefix）。
- **head_or_tp_rank**：PoolKey 里的 shard 下标（通常 = `tp_rank`；`kv_heads < tp` 时有 `put_step` 折叠）。

| 项 | 现状 |
|----|------|
| **Key 集合** | `collect_prefix_keys.py --tp-size N` 展开 `head_or_tp_rank:0..N/put_step-1` × 满块（`keys.py::expand_store_keys`） |
| **多 seg** | `precopy.py --targets seg0,seg1,...`：**rank i 的 key 只 copy 到 `targets[i]`**（不再 key×N 广播） |
| **调度** | 仍 **按 key 串行** `create_copy_and_wait`；映射已对号入座 |
| **解析 seg** | `resolve_segments.sh --target-ip B_IP --tp N` → `TARGET_SEGMENTS`（按 mount 时间序 ≈ rank 0..N-1） |
| **未做** | 多 key/多 seg **并行**；跨机一份 + 同机扩散；异构 TP 见下节（已验证，需容器补丁） |

TP=4、K 个满块 → `4×K` 次 copy，但是 **各 rank 各一份 shard**（总字节 ≈ 一份逻辑 KV），不是同一 key 在 4 个 seg 上各留全量副本。

## 拓扑

```text
mooncake_master
     |
+----+----+
|         |
worker-A  worker-B     各跑 vllm OpenAI server + AscendStoreConnector
(源 segment) (目标 segment)
     |
precopy.py / run_e2e.sh    独立进程：create_copy_task → READY
```

时序：

1. 在 worker-A 上暖前缀（KV 写入池，落在 A 的 segment）
2. 控制面把 key 复制到 B 的 `local_seg`
3. 轮询任务完成 + `batch_get_replica_desc` 确认 B 上有内存副本 → `READY`
4. **之后**再把真实请求打到 worker-B（get session 建立时锁定本机副本）

复制晚于 get session 则本次请求仍可能读远端；下一个请求会重新选副本。

## 目录

| 文件 | 作用 |
|------|------|
| `README.md` | 本文：端到端说明 |
| `env_ascend_a2.sh` | A2 RoCE 环境（`HCCL_INTRA_ROCE_ENABLE` 等），供 source |
| `common.py` | Mooncake store 封装 |
| `precopy.py` | 控制面：`--targets` 时 rank→seg 映射后 `create_copy_task` |
| `collect_prefix_keys.py` | warm prompt → block hash → 全 rank PoolKey → `prefix_keys.txt` |
| `resolve_segments.sh` | master 日志解析 seg；`--target-ip`+`--tp` → `TARGET_SEGMENTS` |
| `stop_cluster.sh` | 停 worker（可选停 master） |
| `store_demo.py` | Store 半程（无 vLLM） |
| `keys.py` | 按 AscendStore `PoolKey` 格式枚举 object key（已知 hex） |
| `start_master.sh` | 拉起 `mooncake_master` |
| `start_worker.sh` | 拉起单实例 vllm-ascend + AscendStoreConnector |
| `run_store_demo.sh` | 跑 store 半程 |
| `run_e2e.sh` | 双 worker 编排 + 自动 warm/precopy/hit B |
| `conf/mooncake.json.example` | Mooncake 配置样例 |
| `test_keys.py` | `keys.py` 纯单测 |

## 快速开始

### A. 无 NPU：看计划 / 跑 store 半程

```bash
cd va-precopy

DRY_RUN=1 bash run_e2e.sh

# 通用/CPU Mooncake：
PROTOCOL=tcp bash run_store_demo.sh

# vllm-ascend 镜像内 store 半程（三张空闲卡）：
PROTOCOL=ascend SOURCE_DEVICE=4 TARGET_DEVICE=5 COORD_DEVICE=6 \
  bash run_store_demo.sh
```

### B. 有 NPU：端到端（推荐一键，TP=1）

在 **vllm-ascend** 容器内：

```bash
cd va-precopy

# 显式指定主机 IP + 业务网卡（官方 A2 要求；也可让 env_ascend_a2.sh 猜测）
export LOCAL_IP=7.242.105.245
export NIC_NAME=enp67s0f5

MODEL=/data/models/Qwen3-VL-8B-w8a8c16 \
  MODEL_NAME=Qwen3-VL-8B-w8a8c16 \
  DEVICES_A=0 DEVICES_B=1 TP=1 MAX_MODEL_LEN=4096 \
  bash run_e2e.sh
```

成功标志：

- 控制面打印 `[precopy] READY`
- `worker_B.log` 出现 `External prefix cache hit rate`（暖前缀足够长时可达 ~90%+）

停集群：

```bash
bash stop_cluster.sh           # 只停 A/B
STOP_MASTER=1 bash stop_cluster.sh
```

### C. 分步（调试用）

```bash
source ./env_ascend_a2.sh   # 需 LOCAL_IP / NIC_NAME

bash start_master.sh

ROLE=A PORT=8001 LOOKUP_ID=0 ASCEND_RT_VISIBLE_DEVICES=0 TP=1 \
  MODEL=/data/models/Qwen3-VL-8B-w8a8c16 MAX_MODEL_LEN=4096 \
  bash start_worker.sh

ROLE=B PORT=8002 LOOKUP_ID=1 ASCEND_RT_VISIBLE_DEVICES=1 TP=1 \
  MODEL=/data/models/Qwen3-VL-8B-w8a8c16 MAX_MODEL_LEN=4096 \
  bash start_worker.sh

# warm（前缀需够长以产生多个 full block；默认 *80 ≈ 801 tokens）
python3 - <<'PY' | curl -s http://127.0.0.1:8001/v1/completions \
  -H 'Content-Type: application/json' -d @-
import json
prefix = "va-precopy shared prefix for store warmup. " * 80
print(json.dumps({"model":"qwen","prompt":prefix,"max_tokens":1,"temperature":0}))
PY

PYTHONHASHSEED=0 python3 collect_prefix_keys.py \
  --model /data/models/Qwen3-VL-8B-w8a8c16 \
  --model-name Qwen3-VL-8B-w8a8c16 \
  --tp-size "${TP:-1}" \
  --prefix-repeat 80 \
  --out prefix_keys.txt \
  --check-master 127.0.0.1:50088

# TP=1: resolve 最近两次 mount；TP>1: --target-ip + --tp
eval "$(bash resolve_segments.sh --export)"
# eval "$(bash resolve_segments.sh --export --target-ip "$LOCAL_IP" --tp "$TP")"

python3 precopy.py \
  --master 127.0.0.1:50088 \
  --protocol ascend \
  --targets "${TARGET_SEGMENTS:-$TARGET_SEGMENT}" \
  --keys-file prefix_keys.txt \
  --dry-show-before
# 成功：READY（rank i → targets[i]）
```

## A2 环境变量（硬依赖）

与官方 `kv_pool.md` A2 分支一致，`start_worker.sh` 在 `ENABLE_ASCEND_A2=1`（默认）且 `protocol=ascend` 时自动 `source env_ascend_a2.sh`：

| 变量 | 作用 |
|------|------|
| `HCCL_INTRA_ROCE_ENABLE=1` | 卡间 RoCE 单边通信（缺则 `HcclBatchPut=4`） |
| `HCCL_IF_IP` | 主机业务 IP（与 segment 名中的 IP 一致） |
| `HCCL_SOCKET_IFNAME` / `GLOO_SOCKET_IFNAME` / `TP_SOCKET_IFNAME` | 业务网卡名 |
| `PYTHONHASHSEED=0` | 与控制面重算 block hash 对齐 |
| `HCCL_NPU_SOCKET_PORT_RANGE` | 同机多 worker 分端口段（脚本按 ROLE 默认 26000/26100） |

非 A2（如 A3 HCCS）设 `ENABLE_ASCEND_A2=0` 并按官方文档导出对应变量。

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

并设置 `MOONCAKE_CONFIG_PATH` → `conf/mooncake.<ROLE>.json`（NPU 上 `protocol=ascend`，`device_name=""`）。

**同机多 worker 必须 `preferred_segment: true`**：否则仅有 `prefer_alloc_in_same_node` 时，写路径可能把 KV 分到同机**另一个** worker 的 segment（B2 预复制场景退化成「KV 本来就在目标机」）。`start_worker.sh` 默认已开。

`lookup_rpc_port` 在 AscendStore 里是 **lookup id 后缀**（拼本地 IPC 路径），不是 TCP 端口；A/B 用不同 id（0/1）。

## Key 与 segment

- **Object key**：AscendStore 命名空间；**一请求多 key**（每满块 × 每 `head_or_tp_rank`）。`collect_prefix_keys.py --tp-size N`；离线用 `keys.py`。
- **Segment 名**：`local_seg` = `get_ip():rpc_port`。TP=1：`resolve_segments.sh` 取最近两次 mount；TP>1：`--target-ip B_IP --tp N`。
- **chunk hash**：`PYTHONHASHSEED` 与引擎一致（worker 默认 0）。

## 验收

| 步骤 | 判据 |
|------|------|
| `DRY_RUN=1 bash run_e2e.sh` | 打印 master / A / B / precopy 步骤 |
| `run_e2e.sh`（TP=1 + A2 env） | `[precopy] READY`；B 侧 External prefix hit |
| `precopy.py --targets` | 各 key AFTER 仅含其 mapped seg |
| api_server `/proc/<pid>/environ` | 含 `HCCL_INTRA_ROCE_ENABLE=1` |
| `python3 test_keys.py` | `PASS` |

## 已知限制

- 同构 **TP=2 rank↔seg** 已通过（见上）；TP=4 新路径未重跑。旧 key×N 历史结果仍有效作对照。
- copy 仍按 key **串行**；未做并行 / 本机扩散。
- `resolve_segments.sh` 依赖本目录 `mooncake_master.log` 的 `mount_segment`；master 若为旧进程/日志不在此文件则失败——改从 `worker_*.log` 取 `listening on IP:port` 填 `--targets`。
- `precopy.py` 退出时偶发 allocator abort；若已打印 `READY` 可忽略。
- 源属主客户端（worker-A）必须在线；副本 **无 pin**；与读共享带宽。
- 生成物已 `.gitignore`；本目录只验证 **非 layerwise** + **本机 DRAM**。
- **异构 TP（A/B 不同 tp_size）**：AscendStore **原生支持**（`prefill_tp_size`/`decode_tp_size` → tp_mismatch sub-key），但 v0.26.0rc1 put 路径有 bug 需补丁，见下节。

### 跨机手工步骤（摘要，同构 TP）

```bash
# 机 A：master + worker-A；机 B：worker-B（同 MC_MASTER；LOCAL_IP=B_IP）
PYTHONHASHSEED=0 python3 collect_prefix_keys.py \
  --model ... --model-name ... --tp-size 4 --block-size 1536 \
  --out prefix_keys.txt --check-master A_IP:50088
eval "$(bash resolve_segments.sh --export --target-ip B_IP --tp 4)"
python3 precopy.py --master A_IP:50088 --protocol ascend \
  --targets "$TARGET_SEGMENTS" --keys-file prefix_keys.txt --dry-show-before
# 再打 B:8002；看 external_prefix_cache_*
```

## 异构 TP（A=TP2 → B=TP4；2026-10-09 已验证）

问题：A=TP2、B=TP4 能否像「原生 Mooncake/connector」一样共享同一套 shard？

| 层 | 结论 |
|----|------|
| **Mooncake Store** | 不管 TP；只认 `key → segment 副本`。 |
| **AscendStore**（`v0.26.0rc1`） | **原生支持**异构：extra_config `prefill_tp_size` / `decode_tp_size` → `config_data.py::infer_tp_mismatch_info`。语义：`effective_tp = max(local, peer)`；条件：TP 不同、非 MLA、非 hybrid、`num_kv_heads % effective_tp == 0`；`num_sub_keys = local_heads_per_rank // effective_heads_per_rank`；key 的 `head_or_tp_rank` 改写为 `effective_rank = tp_rank * num_sub_keys + sub_idx`（`pool_worker.py::_build_tp_mismatch_keys_and_addrs`），KV 按 head 切片 strided 读写。限制：不支持 layerwise / sparse / hybrid(Mamba/DSV4)。 |
| **上游 vLLM `MooncakeStoreConnector`** | 有异构 Store-TP / LCM（[PR #53129](https://github.com/vllm-project/vllm/pull/53129)）。 |
| **SGLang HiCache** | `tp_lcm_size` + head split。 |

### 上游 bug（v0.26.0rc1）：tp_mismatch put/get 是死代码

`pool_worker.py::_start_kv_transfer_threads` 构造 `KVCacheStoreSendingThread` / `KVCacheStoreRecvingThread` 时**漏传 `worker=self`**，而 `kv_transfer.py` 的 tp_mismatch put/get 分支都 gate 在 `self.worker is not None`（`:681` / `:934`）。后果：put 走普通路径，key 用**本地 rank 名**、值是**全本地 head 切片**——消费方（更大 TP）按 eff-rank 名找 key，部分「同名碰撞」会读到错误字节，其余直接 miss。

补丁：`patch_tp_mismatch_worker.py`（两处构造补 `worker=self`，幂等，自动备份）。已在容器 `quay.io/ascend/vllm-ascend:v0.26.0rc1` 实测生效；建议反馈上游。

### 异构 e2e 实测（2026-10-09，单机 245，A2 RoCE）

- 配置：A=TP2（卡 0,1，:8001）、B=TP4（卡 2,3,4,5，:8002），均 `prefill_tp_size=2, decode_tp_size=4`；模型 VL-8B（GQA，8 KV head；`effective_tp=4`，`num_sub_keys=2`）。
- 流程：warm A → `collect_prefix_keys.py --tp-size 4 --peer-tp-size 2`（展开 eff rank 0-3，6 满块 × 4 = **24 keys**）→ 解析 B 的 4 seg（`worker_B.log` 的 `listening on`，pid 对号 rank）→ `precopy.py --targets seg0,seg1,seg2,seg3`（eff rank i → B seg i）→ 打 B。
- 结果：**24/24 key 存在**且副本对号（eff0/1 → A TP0 seg，eff2/3 → A TP1 seg）；precopy 后每个 eff-rank key 在 B 对应 seg 有本地副本；B 侧 `kvpool hit tokens: 768/801`（**95.9%**），TP0-3 各自 `backend get keys=6` 成功、零 invalid block；A(TP2)/B(TP4) 同 prompt 贪心 16 token 输出逐字一致。
- 证据：`logs/worker_A.log`（`tp_mismatch put keys=12` × 2 rank）、`logs/worker_B.log`（分 rank get）、`logs/e2e_hetero_2_4.log`（首次跑，死于 collect exit 2；补丁后手工复测通过）。
- **本机读已取证（2026-10-09 深夜，杀源实例法）**：杀 worker-A 并等 client TTL 注销其 segment 副本（`batch_get_replica_desc` 仅剩 217 副本）后，B 用**全新 prompt 首次**命中：外部 get `enter keys=6` ×4 rank 同秒成功、external hit 累计 89.8%、零 invalid、输出与 A 逐字一致 ⇒ **get 读的是 217 本机 seg 副本**。早期用 RoCE 网卡打点取证的方法已撤回（netdev 计数器不统计 RDMA，见总表 #9）；同 prompt 第二次命中会被 vLLM 内部 HBM prefix cache 接住（外部 get=0），判定必须用全新 prompt 首中（见总表 #12）。B 命中时刻 `Connected to segment: 7.242.105.245:*` 是 precopy copy-task 收尾传输（与 get 同秒重叠造成的误读，勿再当读取证据——本机 seg 读不留连接日志）。

### 跨机异构实测（2026-10-09，245 → 217，A=TP2 → B=TP4）

配置：A=TP2（245，卡 0,1，:8001）、B=TP4（217，卡 0-3，:8002），master 在 245 宿主机 `:50088`；两端容器同镜像，217 容器需先打 `patch_tp_mismatch_worker.py`。跨机操作用 `ssh root@7.242.105.217`（密码，expect 包装；sshpass 未装）。

- 流程与单机一致：warm A → collect 24 keys（`prefix_keys_xhost_het.txt`）→ `resolve_segments.sh --target-ip 7.242.105.217 --tp 4` → precopy（245 侧容器内跑，`--targets` 平铺 4 seg）→ 打 B。
- 结果：**24/24 key 存在**，A=TP2 的 sub-key put 跨机生效；**24/24 `replica_copy_success`**（A 侧 `client_service.cpp:2624`，~1 key/s），副本对号：rank0→`217:16471`、rank1→`217:15526`、rank2→`217:16596`、rank3→`217:15742`（每 key 双副本：245 源 seg + 217 目标 seg，`check_exists` 取证）；B 首次请求 `hit_tokens: 768/801`（**95.9%**，与单机正向一致）、零 invalid；B(DEBUG) 分 rank `MooncakeBackend.get enter keys=6` × TP0-3；A/B 同 prompt 贪心 16/64 token 输出逐字一致。
- 坑（跨机新增）：217 容器残留 10/8 的旧 worker-B —— vLLM `setproctitle` 后进程名是 `VLLM::EngineCore/Worker_TP/AAPIServer`，`ps | grep python|vllm` **搜不到**（大小写躲过），旧进程占着 8002 端口与卡 0-3 显存，新 B 起不来或假 READY（`/v1/models` 由旧实例应答）。清理用 `pgrep -f "VLLM::"` + `npu-smi info -t usages -i <id>` 核对 HBM 释放（注意 pgrep 模式含 "VLLM::" 时会匹配自身 ssh 命令行，用 `VLLM::[W]` 括号技巧）。
- master 日志：`logs/mooncake_master.log` 被轮转后 master 的 fd 仍写在改名文件上（`.bak.27b`），`resolve_segments.sh` 读不到 mount_segment 行 —— `ln -sf mooncake_master.log.bak.27b logs/mooncake_master.log` 即可。

### 反向 A=TP4 → B=TP2 实测（2026-10-09，单机 245）

反向暴露两个新约束，均已解决并验证：

1. **配置语义（易错）**：`infer_tp_mismatch_info`（`config_data.py:48`）对 kv_producer/**kv_both** 读 **`decode_tp_size`** 作为 peer（kv_consumer 才读 `prefill_tp_size`）。反向必须配 `decode_tp_size=4`（对端 TP，不是本机 TP）——配成 2 时 B(TP2) 判「无不匹配」而退化为普通 TP2 行为。
2. **get 侧只有异步路径感知 tp_mismatch**：同步 load（`start_load_kv` 内联 get）按本机 rank 名取**一个** key、全本地切片尺寸，对 B=TP2 会请求 4 头尺寸而 eff 对象只有 2 头 → get 失败 → invalid block → 全部回退重算（external hit 指标照样 ~95%，**假命中**）。小 TP 消费者必须 `load_async=1`（`start_worker.sh` 已支持 `LOAD_ASYNC` env 注入），走 `KVCacheStoreRecvingThread → _load_kv_tp_mismatch` sub-key strided get。

实测：A=TP4（卡 0-3，`decode_tp_size=4`→plain put 即 eff 命名）、B=TP2（卡 4,5，`LOAD_ASYNC=1`）。
- A 四 rank 各普通 put 6 key（24/24，命名=eff 0-3，内容=2 头/eff shard）；
- precopy 映射：eff rank i → B seg[i//2]，现有 `--targets` 传**重复列表** `seg0,seg0,seg1,seg1` 即可（无需改代码）；
- B：`External prefix cache hit rate: 95.9%`、零 invalid；TP0/TP1 各 `tp_mismatch get keys=12`（6 块 × 2 sub-key）成功；
- A(TP4)/B(TP2) 同 prompt 贪心 16 token 输出逐字一致。

**方向差异小结**：eff=4 时，A=TP2 生产者靠补丁后的 sub-key **put**；B=TP4 消费者同步 load 即可；A=TP4 生产者 plain put 即可；B=TP2 消费者**必须** `load_async=1`。即：**小 TP 端需要 sub-key 读写（put 已由补丁修通，get 需异步路径）**；大 TP（=effective_tp）端两条路径都退化为普通行为。

**决策（2026-10-09）**：异构 TP 单机双向（TP2→TP4、TP4→TP2）已验证；**跨机异构 TP2(245)→TP4(217) 已验证**（见上节）。tp_mismatch 与 layerwise/sparse 互斥，生产组合需评估。

参考：`pool_worker.py`（`put_step`/`head_or_tp_rank`/`_build_tp_mismatch_keys_and_addrs`）；`docs/research/sglang/storage-backends.md`。

## 开放问题（待讨论）

底层 B2 已实测；下列未在本目录闭环，生产前需选型。

### 1. Layerwise

当前 e2e：`use_layerwise=false`。池侧有两条 layerwise 线（vllm-ascend）：

| 路线 | 大致配置 | 含义 |
|------|----------|------|
| Mooncake block_key | `backend=mooncake` + `use_layerwise=true` | 逐层 range 读；HBM 仍全层；壳请求同步占槽 |
| MemCache gva | `backend=memcache` + `use_layerwise=true` | HBM 可减量；预热落点更偏本机 DRAM |

待定：生产默认走 MemCache，还是留 Mooncake 并补测 layerwise + B2（get session 是否仍选本机 DRAM）。不建议自研 Mooncake GVA。

### 2. 上层编排（现只有底层）

现状：手工 / `run_e2e.sh`：warm → keys → `create_copy_task` → READY → **再**打目标。缺「谁预取、何时放行、失败重试」。

待定：独立 **Prefetch Orchestrator**（旁路进程或并入 Router）：收 intent → 调 B2 → READY 栅栏 → gateway/router 放行。过载/准入仍归 gateway；引擎不参与策略。

### 3. 落盘（SSD / disk）

B2 目标是 worker 挂载的 **DRAM segment**。Mooncake 可有 DRAM→SSD 分层（`enable_ssd_offload`，本目录默认 `false`），那是池内冷热，不是「预复制直指定 disk」。

待定：产品语义是否 **READY = 本机 DRAM 必达**；SSD 仅作容量/恢复异步 demote。以 disk 为 READY 会吃掉预热收益。

### 4. 其它生产缺口（短清单）

- 副本无 pin：READY 后仍可能被池驱逐  
- copy 带宽限速 / 与在线读抢带宽；多 key **并行**  
- 异构 TP：单机双向 TP2→TP4 / TP4→TP2 已验证；跨机 TP2(245)→TP4(217) 已验证（2026-10-09）；**跨机读本机副本已用杀源实例法铁证**（2026-10-09 深夜，见总表 #12；早期网卡打点证据已撤回——netdev 计数器不统计 RDMA）
- **B 侧重复请求偶发 1-token 贪心翻转**（仅前置实验 B 非 DEBUG 实例出现：同 prompt 奇数次输出与 A 一致、偶数次在近平局 token 处偏移；A 稳定、B 冷 prompt 稳定、DEBUG 新实例 3 连跑未复现）——疑外部 KV 加载路径与 local 路径交替时的数值扰动，非字节错乱（纯外部读首请求输出与 A 逐字一致）；复现条件与根因待查
- ~~跨机 get 不读本机副本~~（**2026-10-09 晚撤回**：系把 precopy copy-task 收尾连接误读为 get 读；读本机副本由 #12 杀源实例法确证）
- **B 侧重复请求偶发 1-token 贪心翻转**（仅前置实验 B 非 DEBUG 实例出现：同 prompt 奇数次输出与 A 一致、偶数次在近平局 token 处偏移；A 稳定、B 冷 prompt 稳定、DEBUG 新实例 3 连跑未复现）——疑外部 KV 加载路径与 local 路径交替时的数值扰动，非字节错乱（纯外部读首请求输出与 A 逐字一致）；复现条件与根因待查
- EP：持 KV 的 rank/多机拓扑尚未测  
- master HA、监控（READY 延迟、copy 失败率、external hit）  
- 源 worker 掉线则 copy 失败，与故障重路由如何衔接  
- 方案 A（壳请求进 HBM）是否还要、与 B2 叠加顺序  

## 边界

- `protocol`：store 半程可用 `tcp`；接 vllm-ascend NPU 集群用 `ascend`，须与 `mooncake.json` 一致。
- 控制面发起 `create_copy_task`；`mooncake_master` 调度；引擎不调用该 API。
