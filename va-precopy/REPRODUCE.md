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
| hash 对齐 | precopy/keys `--hash-algo sha256`（默认） | 与引擎 `prefix_caching_hash_algo` 一致即可；`PYTHONHASHSEED=0` 仅 algo=builtin 时需要（legacy 防御） |
| 同机多 worker | `preferred_segment: true` | 否则写路径可能把 KV 分到同机另一 worker 的 segment，预复制场景退化 |
| 取证日志 | `VLLM_LOGGING_LEVEL=DEBUG` | 非 DEBUG 无分 rank `MooncakeBackend.get enter keys=` 证据 |

**已知上游 bug（异构 TP 场景必打补丁）**：v0.26.0rc1 的 tp_mismatch put/get 分支因线程构造漏传 `worker=self` 而是**死代码**（根因与机制见 [README.md](README.md)「怎么运作 · 异构 TP」）。**rc1 / rc2 均不含上游修复**（main 已修，[#15835](https://github.com/vllm-project/vllm-ascend/pull/15835)），0.26 rc 镜像必打本补丁（#15835 完整版 rc1 backport）；同构场景无需补丁。容器内：`cd /vllm-workspace/vllm-ascend && git apply --check patch_tp_mismatch_worker.patch && git apply -v patch_tp_mismatch_worker.patch`（或 `patch -p1`；`git apply -R --check` 探测是否已打）。若容器打过旧 `.py` 子集补丁，先恢复其 `.bak.<时间戳>` 备份（或重建容器）再打，否则上下文不匹配。

**操作陷阱**：残留实例假 READY 与清理、跨机 SSH 限速、两机非共享存储等**现行坑**见 [README.md](README.md)「操作注意事项」；判定环节的陷阱（假命中、全新 prompt、TTL、netdev）见 §3；已修复问题（退出 abort 等）见 [EXPERIMENTS.md](EXPERIMENTS.md)「勘误与演进」。

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

A/B 同 `tp_size`，同机不同卡组。**无需补丁**。流程见 [README.md](README.md)「快速开始 B/C」（TP=1 一键 `run_e2e.sh`；多 TP 分步 `precopy.py --role B --tp-size 2`，rank↔seg 对号内嵌完成）。

验收：`External prefix cache hit rate` ≈95.9%（768/801）、零 invalid（分层判据见 §3）。

### ② 同构跨机

master 在源机 `0.0.0.0:50088`；B 机 worker 配 `MC_MASTER`/`LOCAL_IP`/`HCCL_IF_IP` 指向本机。

- TP=1/2 与 TP=4 均已通过；27B Mamba 需 `--block-size 1536`（Mamba 状态按 align 块入池）。
- **须知**：本场景验证于 2026-10-08 的旧 key×N 广播路径；当前代码的新 rank↔seg 映射路径已在其子集（同构同机 TP=2）复测通过，同构跨机新路径未单独重跑。复现时请按 §3 方法验证读本机副本。

### ③ 异构同机（双向）

A/B 不同 `tp_size`，extra_config 两端均配 `prefill_tp_size=<A_TP>`、`decode_tp_size=<B_TP>`。**两端容器都必须打 `patch_tp_mismatch_worker.patch`**。

方向规则（`effective_tp = max(A_TP, B_TP)`）：

| 角色 | put（生产） | get（消费） |
|------|-------------|-------------|
| TP = effective_tp 的大 TP 端 | plain put（无需 sub-key） | 同步 load 即可 |
| TP < effective_tp 的小 TP 端 | 需补丁后 sub-key put | sub-key get（`LOAD_ASYNC=1` 异步路径） |

注：小 TP 消费者走异步的原因——同步 load 只按本机 rank 名取全本地切片 → 尺寸不匹配 → invalid → 全重算（假命中，见 §3）；异步路径才走 `_load_kv_tp_mismatch`。当前补丁（#15835 完整版 backport）已恢复同步 load 的 mismatch 分发，硬约束解除，`LOAD_ASYNC=1` 降为推荐；本测试床异构实测均走异步，同步 mismatch 未单独复测。`run_e2e.sh` 反向（TP_A>TP_B）已自动给 B 注入 `LOAD_ASYNC=1`；手动分步（README 快速开始 C）需自行 export。

**配置陷阱（反向方向最易错）**：`infer_tp_mismatch_info` 对 `kv_producer`/`kv_both` 读的是 **`decode_tp_size`** 作为 peer size（`kv_consumer` 才读 `prefill_tp_size`）。反向（A=TP4→B=TP2）时 B 侧必须配 `decode_tp_size=4`（对端 TP）；配成本机 TP=2 会被判「无不匹配」而退化为普通路径——**指标照样显示 ~95% hit，但实际是假命中**（见 §3）。

precopy 映射：eff rank i → B seg `i // num_sub_keys`，**由 precopy 自动展开**（`--tp-size <B 的 TP> --peer-tp-size <A 的 TP>`；反向 B=TP2 时 resolve 出的 2 个 seg 自动展开成 `seg0,seg0,seg1,seg1`，无需手工重复列表）；显式 `--targets` 时按 eff rank 序平铺给出。key 计算：eff = max(TP_A, TP_B)（24 keys = 6 满块 × 4 eff rank）。

限制：tp_mismatch 不支持 MLA、layerwise、sparse、hybrid（**Mamba/线性注意力结构被代码 gate 显式排除，异构不支持**；Mamba 同构可用）。

### ④ 异构跨机

流程 = ③ 的异构配置 + ② 的跨机配置（master 在源机、B 机容器先打补丁、ssh 操作成批）；precopy 跨机探测参数（`--ssh`/`--pidfile`）与命令样例见 [README.md](README.md)「快速开始 D」。

已验证配置：245(A=TP2，卡0,1，:8001) → 217(B=TP4，卡0-3，:8002)，三轮 precopy 各 24/24 `replica_copy_success`（A 侧 ~1 key/s），副本对号落 217 四个 seg（每 key 双副本：源 seg + 目标 seg，逐 key 取证——当时用 `check_exists`，现并入 `keys.py --keys-file <留档> --check-master`）。

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

- **B 侧重复请求偶发 1-token 贪心翻转**：仅 10/09 旧非 DEBUG 实例出现（同 prompt 偶数次输出在近平局 token 处偏 1 个字符），新 DEBUG 实例未复现；非字节错乱，复现条件与根因待查（实验记录见 [EXPERIMENTS.md](EXPERIMENTS.md) #10）。
- **Mamba 异构不支持**（hybrid 模型被 tp_mismatch gate 排除，见 §2③）；Mamba 同构 TP=4 跨机已通过（`--block-size 1536`）。
- 通用边界（副本无 pin、串行 copy、源属主须在线、带宽共享、验证覆盖面）见 [README.md](README.md)「边界与已知限制」。
