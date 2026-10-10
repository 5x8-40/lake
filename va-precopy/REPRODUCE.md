# va-precopy 复现参考（客户版）

在 **vllm-ascend** 上做 Mooncake KV **预复制（pre-copy）**：真实请求打到目标实例之前，用 Mooncake `create_copy_task` 把已有前缀 KV 复制到该实例的本机 DRAM segment，使读路径 local-first、避免跨机拉 KV。

本文是**复现参考**：只保留已跑通的结论、必配项与必须注意的 bug/陷阱。完整设计说明见 [README.md](README.md)，实验史与踩坑记录见 [EXPERIMENTS.md](EXPERIMENTS.md)。

## 0. 通用环境与硬配置（所有场景必读）

| 项 | 必配值 | 说明 |
|----|--------|------|
| 容器镜像 | `quay.io/ascend/vllm-ascend:v0.26.0rc1` | 两端一致 |
| mooncake wheel | `mooncake_transfer_engine_npu-0.3.11.post1` | **勿升级 0.3.13.post1**：mount segment 后 EngineCore 挂起 15+ 分钟（疑与旧 master 兼容性），实测失败已回退 |
| master | 245 宿主机 `mooncake_master 0.0.0.0:50088` | 跨机时 B 侧配 `MC_MASTER=A_IP:50088`、`LOCAL_IP=B_IP`、`HCCL_IF_IP=B_IP` |
| A2 RoCE | `HCCL_INTRA_ROCE_ENABLE=1` + `env_ascend_a2.sh` 全套 | **缺则跨卡 copy 直接失败（`HcclBatchPut=4`）**。业务网卡 `NIC_NAME=enp67s0f5`（两台同名） |
| hash 对齐 | `PYTHONHASHSEED=0` | 控制面与引擎的 block hash 必须一致，否则 collect 出的 key 全 miss |
| 同机多 worker | `preferred_segment: true` | 否则写路径可能把 KV 分到同机另一 worker 的 segment，预复制场景退化 |
| 取证日志 | `VLLM_LOGGING_LEVEL=DEBUG` | 非 DEBUG 无分 rank `MooncakeBackend.get enter keys=` 证据 |

**已知上游 bug（异构 TP 场景必打补丁）**：v0.26.0rc1 的 `pool_worker.py::_start_kv_transfer_threads` 构造发送/接收线程时漏传 `worker=self`，导致 tp_mismatch 的 put/get 分支是**死代码**。必须先打 `patch_tp_mismatch_worker.py`（幂等、自动备份，容器内执行）。同构场景不受影响，无需补丁。上游已在 main 修复（[#15835](https://github.com/vllm-project/vllm-ascend/pull/15835)，2026-09-09 合入，`9f8773ea`），但 **rc1 / rc2 均不含**，0.26 rc 镜像仍须打本补丁；本补丁为 #15835 完整版的 rc1 backport（含同步 load 分发，自动升级旧子集补丁）。

**操作要点**：

- 残留 worker 清理：vLLM 进程经 `setproctitle` 后名为 `VLLM::EngineCore/Worker_TP/APIServer`，`ps | grep python` 搜不到；用 `pgrep -f "VLLM::[W]"`（括号防自匹配）+ 显式 kill，再用 `npu-smi info -t usages -i <id>` 确认 HBM 释放（<10%）。旧实例残留在目标端口会让新实例「假 READY」。
- `precopy.py` 客户端退出偶发 allocator abort（RC=134）或挂起：**拷贝工作已完成，以源侧 `replica_copy_success` 计数 / READY 打印为准**，可忽略。
- master 日志被轮转后 fd 仍在改名文件上：`ln -sf mooncake_master.log.bak.27b logs/mooncake_master.log` 修复 `resolve_segments.sh`。
- 两机**非共享存储**：各自 clone 本目录并同步。
- 跨机 ssh 高频连接触发对端限速（认证后断连/KEX 卡死）：控制操作合并成批执行，或改控制台人工执行。

## 1. 四类场景总览

| 场景 | 配置 | 现状 | external hit | 读本机副本（local-first） |
|------|------|------|--------------|--------------------------|
| ① 同构同机 | 245 单机，A=TP2(卡0,1)→B=TP2(卡2,3)，VL-8B | ✅ 通过 | **95.9%**（768/801），零 invalid | 同机场景：目标实例本机 seg，天然 local |
| ② 同构跨机 | 245→217，TP=1/2；TP=4（27B Mamba） | ✅ 通过 | ~95%+（27B ≈92.1%，满块） | 当时未单独取证；建议按 §3 自证（见下） |
| ③ 异构同机 | 245 单机，双向 TP2→TP4 / TP4→TP2 | ✅ 双向通过 | **95.9%**，零 invalid | 同机场景：天然 local |
| ④ 异构跨机 | 245(A=TP2)→217(B=TP4)，三轮 precopy | ✅ 通过 | **95.9%**（768/801），零 invalid | ✅ **杀源实例法铁证**（2026-10-09 晚，见 §3）：仅剩本机副本时外部 get 成功 |

命中数字均为 `kvpool hit tokens 768/801` 量级（前缀 801 token、768 满块 token；尾巴不满块不进 key 列表，属预期）。各场景 A/B 同 prompt 贪心输出**逐字一致**（16/64 token 均验过）。

## 2. 各场景复现要点

### ① 同构同机

A/B 同 `tp_size`，同机不同卡组。**无需补丁**。

```bash
# 一键（TP=1）：
bash run_e2e.sh
# 多 TP 分步：
PYTHONHASHSEED=0 python3 collect_prefix_keys.py --tp-size 2 --prefix-repeat 80 \
  --out prefix_keys.txt --check-master 127.0.0.1:50088
eval "$(bash resolve_segments.sh --export --target-ip $LOCAL_IP --tp 2)"
python3 precopy.py --master 127.0.0.1:50088 --protocol ascend \
  --targets "$TARGET_SEGMENTS" --keys-file prefix_keys.txt
```

注意：`--targets` 按 rank 序传（rank i 的 key 只 copy 到 `targets[i]`）；seg 名从 `worker_B.log` 的 `Transfer Engine RPC ... listening on IP:port` 取（按 mount 序 ≈ rank 序）。

### ② 同构跨机

master 在源机 `0.0.0.0:50088`；B 机 worker 配 `MC_MASTER`/`LOCAL_IP`/`HCCL_IF_IP` 指向本机。

- TP=1/2 与 TP=4 均已通过；27B Mamba 需 `--block-size 1536`（Mamba 状态按 align 块入池）。
- **须知**：本场景验证于 2026-10-08 的旧 key×N 广播路径；当前代码的新 rank↔seg 映射路径已在其子集（同构同机 TP=2）复测通过，同构跨机新路径未单独重跑。复现时请按 §3 方法验证读本机副本。

### ③ 异构同机（双向）

A/B 不同 `tp_size`，extra_config 两端均配 `prefill_tp_size=<A_TP>`、`decode_tp_size=<B_TP>`。**两端容器都必须打 `patch_tp_mismatch_worker.py`**。

方向规则（`effective_tp = max(A_TP, B_TP)`）：

| 角色 | put（生产） | get（消费） |
|------|-------------|-------------|
| TP = effective_tp 的大 TP 端 | plain put（无需 sub-key） | 同步 load 即可 |
| TP < effective_tp 的小 TP 端 | 需补丁后 sub-key put | sub-key get（实测 `LOAD_ASYNC=1` 异步路径；完整补丁后同步亦可，未复测） |

注：旧子集补丁（仅 `worker=self`，无同步 load 分发）下小 TP 消费者**必须** `LOAD_ASYNC=1`——同步 load 只按本机 rank 名取全本地切片 → 尺寸不匹配 → invalid → 全重算（假命中）。当前补丁已是 #15835 完整版 backport，同步分发已恢复，该硬约束解除；异步仍是推荐的 overlap 路径。本测试床异构实测均走异步路径，同步 mismatch 未单独复测。

**配置陷阱（反向方向最易错）**：`infer_tp_mismatch_info` 对 `kv_producer`/`kv_both` 读的是 **`decode_tp_size`** 作为 peer size（`kv_consumer` 才读 `prefill_tp_size`）。反向（A=TP4→B=TP2）时 B 侧必须配 `decode_tp_size=4`（对端 TP）；配成本机 TP=2 会被判「无不匹配」而退化为普通路径——**指标照样显示 ~95% hit，但实际是假命中**（见 §3）。

precopy 映射：eff rank i → B seg `i // num_sub_keys`。B=TP2（num_sub_keys=2）时传重复列表 `seg0,seg0,seg1,seg1`；B=TP4（=eff）时平铺 `seg0,seg1,seg2,seg3`。collect 用 `--tp-size <eff> --peer-tp-size <小 TP>`（24 keys = 6 满块 × 4 eff rank）。

限制：tp_mismatch 不支持 MLA、layerwise、sparse、hybrid（**Mamba/线性注意力结构被代码 gate 显式排除，异构不支持**；Mamba 同构可用）。

### ④ 异构跨机

流程与③完全一致，叠加②的跨机配置：master 在源机、B 机容器先打补丁、ssh 操作合并成批。

已验证配置：245(A=TP2，卡0,1，:8001) → 217(B=TP4，卡0-3，:8002)，三轮 precopy 各 24/24 `replica_copy_success`（A 侧 ~1 key/s），副本对号落 217 四个 seg（每 key 双副本：源 seg + 目标 seg，`check_exists` 逐 key 取证）。

## 3. 「读本机副本」判定方法（重点）

**external hit ≈95% 这个指标本身会骗人**：invalid block 会整体回退重算，指标照样显示高命中（假命中）。复现验收必须按下面三层递进判据确认，缺一不可：

| 层级 | 方法 | 判据 |
|------|------|------|
| 1. 指标 | B 侧 log / metrics | `External prefix cache hit rate` ~90%+，且**零 invalid block** |
| 2. 分 rank get | `VLLM_LOGGING_LEVEL=DEBUG` 启动 | 每个 TP rank 各打印 `MooncakeBackend.get enter keys=<每 rank key 数>`，get 成功 |
| 3. 杀源实例终极判定（金标准） | 确认外部 get 真实发生（`get enter` 行在该时刻存在）且输出与源实例逐字一致 | 源实例已死、其 segment 副本已从池注销（`batch_get_replica_desc` 仅剩目标机副本）后，目标实例**首次**命中该 prompt 仍成功 ⇒ 数据物理上只能来自本机副本 |

**杀源实例法的两个关键陷阱**：

- **必须用全新 prompt 且只打一次**。同一 prompt 第二次打目标实例时，vLLM 内部 prefix cache（HBM radix）会整体接住（外部 get 次数为 0，`Prefix cache hit rate` 上升）——此时命中与 pool 无关，实验作废。实测曾因此误判，靠 `MooncakeBackend.get enter` 行数为 0 揭穿。
- **必须等源实例 client TTL 过期**（master `client_ttl`，默认 120s），确认 `batch_get_replica_desc` 对全部 key 只剩目标机副本后再打。

**勿用 netdev 网卡计数器判定**：本环境（昇腾 A2 RoCE）的 `ip -s link` / sysfs 计数器**不统计 RDMA KV 流量**——阳性对照：24 keys（~43 MB）真实跨机传输期间，源/目标两侧计数器增量均 <2 MB。任何「传输了却看不到字节」或「没看到字节=读本机」的推断在此 NIC 上均不成立。

**各场景现状**：场景④已按第 3 层铁证确认 local-first（2026-10-09 晚：杀源 A 后 B 首中 768 token 全部外部 get 成功、零 invalid、输出与 A 逐字一致）；场景①③同机无跨机流量问题；场景②验证当时未做本机读判定，复现时建议按本节自证一次。

**副本选择策略（已从源码确证）**：双副本并存时 get 的 local-first 在本验证所用 0.3.11.post1 即已生效——`SelectBestReplica` 实现「prefer local MEMORY」（`real_client.cpp:283-286`，调用点 `:2527` / `:2834`）；0.3.12+ / upstream `78726c5c` 新增的 `SelectCompleteMemoryReplica` 只用于 session API 路径，与本文 get 路径无关。与实测一致：双副本首中未新建远端连接（`Connected to segment` 仅出现在 precopy copy-task 执行线程）。

## 4. 已知问题与边界

- **B 侧重复请求偶发 1-token 贪心翻转**：仅旧非 DEBUG 实例出现（同 prompt 偶数次输出在近平局 token 处偏 1 个字符）；A 稳定、B 冷 prompt 稳定、DEBUG 新实例未复现。疑外部 KV 加载路径与 local 路径交替的数值扰动，非字节错乱（外部读首请求与 A 逐字一致）。复现条件与根因待查。
- **副本无 pin**：READY 后仍可能被池驱逐；源 worker-A 必须在线，掉线则 copy 失败。
- copy 按 key 串行，未做并行；未做 layerwise、落盘（SSD）、编排层集成。
- **Mamba 异构不支持**（hybrid 模型被 tp_mismatch gate 排除）；Mamba 同构 TP=4 跨机已通过（`--block-size 1536`）。
- 源属主客户端与读共享带宽，无限速。
