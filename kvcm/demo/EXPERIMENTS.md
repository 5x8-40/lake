# va-precopy 实验总表与踩坑记录

> **demo 记录**：precopy 预热验证（kvcm 八项操作中仅 **copy** 一项）的实验史。生产化设计见 [../design/](../design/)；kvcm 全貌见 [../README.md](../README.md)。

2026-10-08 ~ 11-02 全量实验史（含已撤回证据；总表至 #24），是 [README.md](README.md)「验证状态」的完整依据；复现操作见 [REPRODUCE.md](REPRODUCE.md)。

模型除注明外均为 `/data/models/Qwen3-VL-8B-w8a8c16`（GQA 8 KV head，block_size 128，`PYTHONHASHSEED=0`）；主机 245=7.242.105.245，217=7.242.105.217（**#17 起另用** 45=`7.242.104.45`，225=`7.242.110.225`）；容器镜像 `quay.io/ascend/vllm-ascend:v0.26.0rc1`；mooncake wheel `mooncake_transfer_engine_npu-0.3.11.post1`（除注明外）；master 在源机宿主机 `:50088`。

| # | 时间 | 环境 | 实验 | 结果 | 证据 |
|---|------|------|------|------|------|
| 1 | 10/08 | 单机 245，TP=1/2 | B2 首验（旧 key×N 广播路径） | 通过，hit ≈95%+ | 10/08 会话记录（细节见 git 历史） |
| 2 | 10/08 | 245↔217 跨机，TP=1/2 | 跨机 B2 | 通过，hit ≈95%+ | 同上 |
| 3 | 10/08 | 跨机，TP=4，27B Mamba | 满块大模型跨机 | 通过 ≈92.1%（`--block-size 1536`） | 同上 |
| 4 | 10/08 | 单机 TP=2 | 对照：不开 A2 RoCE | **未通过**（`HcclBatchPut=4`）——跨卡 copy 必须开 A2 RoCE | 同上 |
| 5 | 10/09 午 | 单机 245：A=TP2(卡0,1)→B=TP4(卡2-5)，`prefill_tp_size=2 decode_tp_size=4` | 异构正向 e2e | 首跑死于 collect（上游 put 死代码）→ **补丁后通过**：24/24、**95.9%**、零 invalid、输出逐字一致 | 见「实验实录 · 单机正向」 |
| 6 | 10/09 午 | 单机 245：A=TP4(卡0-3)→B=TP2(卡4,5)，B `LOAD_ASYNC=1` | 异构反向 e2e | 通过：24/24、**95.9%**、零 invalid、输出一致；踩坑：`decode_tp_size` 对 kv_both 是 **peer** size | 见「实验实录 · 反向」 |
| 7 | 10/09 | 容器内 `vllm_ascend` 源码 | 上游 bug 修复实验：`pool_worker.py::_start_kv_transfer_threads` 构造 `KVCacheStore{Sending,Recving}Thread` 漏传 `worker=self` → `kv_transfer.py` tp_mismatch put/get 分支双双死代码 | `patch_tp_mismatch_worker.py` 幂等补丁，put 通路可用（#5 的 24/24 即补丁效果）；get 通路靠 #6 异步路径 | `patch_tp_mismatch_worker.py`、`config_data.py::infer_tp_mismatch_info` |
| 8 | 10/09 晚 | 跨机 245→217：A=TP2(卡0,1)→B=TP4(卡0-3) | 异构跨机 e2e（三轮 precopy 验证稳定性） | **通过**：24/24 跨机 sub-key put、24/24 `replica_copy_success`、副本对号落 217 四 seg、B 首中 **95.9%** 零 invalid、输出逐字一致 | 见「实验实录 · 跨机」 |
| 9 | 10/09 晚 | 245 宿主机 RoCE 网卡打点（`ip -s link enp67s0f5` 前后差值） | **读本机副本判定**（单次 B 命中 16 token） | ~~TX **+0.47 MB** / RX +0.13 MB；若读远端应 ~44 MB ⇒ get 读的是 217 本机 seg 副本~~ **10/09 深夜撤回：阳性对照实测 netdev 计数器不统计昇腾 RoCE RDMA 流量（24 keys ~43 MB 真实跨机传输，两侧计数器增量均 <2 MB），本行证据方法论作废**。读本机副本改由 #12 杀源实例法确证。注：worker 日志里的 `Connected to segment: 245` 是 precopy copy-task 收尾连接（与 get 同秒重叠），非读取证据 | 本次会话打点记录；`/tmp/nic_before.txt`/`after.txt`（245） |
| 10 | 10/09 | B(217) 重复请求确定性观察 | 同 prompt 反复打 B（16/64 token × 多次）+ 冷 prompt 对照 | 旧非 DEBUG 实例：**奇数次输出与 A 一致、偶数次近平局 token 处 1-token 翻转**（"warmup. va" → "warmup.  va"）；A 3×64 token 稳定；B 冷 prompt 3× 稳定；0.3.11+DEBUG 新实例 3 连跑未复现。疑外部加载路径交替的数值扰动，非字节错乱（纯外部读首请求与 A 逐字一致） | 本会话输出记录；根因待复现 |
| 11 | 10/09 晚 | 217 容器 wheel 升级 `0.3.13.post1` | 尝试升级 mooncake（上游 1fc27b6 已有 `SelectCompleteMemoryReplica` 本机优先） | **失败**：mount 4 seg 后 EngineCore 挂起 15+ 分钟（APIServer `Waiting for 1 local core engine proc`），疑与旧 master 兼容性；回退 `0.3.11.post1` 后恢复正常（#8/#9 即回退后所测） | master 日志、`worker_B.log`（17:03-17:18） |
| 12 | 10/09 深夜 | 跨机 245→217 异构重验（全新 prompt ×2：961/801 tok）+ **杀源实例终判** | **通过** + **杀源实例法确证读本机副本**（仅剩 217 副本时 B 首中全部外部 get 成功、零 invalid、输出与 A 逐字一致）。两认知：判定须用**全新 prompt 首中**（第二次被内部 HBM cache 接住，外部 get=0）；netdev 不计 RDMA（#9 撤回依据） | 见「实验实录 · 跨机」；判定方法 [REPRODUCE.md](REPRODUCE.md) §3 |
| 13 | 10/12 上午 | 跨机 245→217 异构，**bc6b5be 单入口控制面 + 两端 #15835 完整补丁** | 正向回归（升级后首测）：warm A → 单入口 precopy（32 keys=8 满块×4 eff）READY → B 首中 external **88.2%**（从实例启动起累计，不是单请求）、零 invalid、16-tok ×6 + 64-tok ×2 **8/8 逐字一致**（1-token 翻转未复现）。B 机容器无 `ss`：用 admin `:9003/get_all_segments` + 显式 `--targets` | 本次会话记录；见「实验实录 · 10-12 回归 · 正向」 |
| 14 | 10/12 下午 | 跨机 **反向 217(TP4)→245(TP2)，A 同步 load（无 LOAD_ASYNC）** | **通过**：36/36 key 对号入 A 两 seg（18+18）；每 rank `get keys=18`（9 块×2 sub-key 拼回）、`load_async=False` 证 hunk3 同步分发生效；8/8 token 与 B(TP4) 参考逐字一致；external hit 74.6→79.7%、零 invalid。踩坑：首跑 A 侧 peer 误配成 2（=自身 TP）→ mismatch 静默禁用 → `-800`（指纹 `Failed to get 9 keys`=每块 1 key）；改回 `PEER=4` 即通——同步 load 的 mismatch 路径由此验证通过 | 见「实验实录 · 10-12 回归 · 反向（同步 load）」 |
| 15 | 10/20 | 跨机正向 keys（#13 的 32 keys）不动，**kill 写入者 A 全进程树 → 等 client TTL 过期 → 复验** | **「副本随属主消失」不成立（纠正踩坑 3 早期误判）**：基线 32/32 → kill（17:06:51）→ 17:08:57 master `client_expired` + unmount A 两 seg（**零驱逐零 remove**）→ `batch_is_exist` **32/32**、副本全在 B 四 seg → 裸 client 跨机 `get` **3/3 字节级读回**（各 4.5MB）→ A 复活后终态仍 32/32。结论：副本存亡绑定 seg 所在 client，key 的 client_id 下线只清其 seg 内副本，远端副本保留 | master 日志切片（本机 `va-precopy/logs/`，不入仓）；见踩坑 3 |
| 16 | 10/26 | 单机 245：源引擎 TP2 + 合成目标 seg + **kvcm daemon（E11/E12）** | PR70 进程化首验：**e2e 全绿**（warm → SubmitIntent → READY）；踩出 copy 连发打爆 adxl 建链（坑 2：14 连发是 `TRANSFER_FAIL`，窗口 4 节奏化 42/42）。与 #17 的 `NO_AVAILABLE_HANDLE` 不是同一次失败 | 见「实验实录 · 2026-10-26」 |
| 17 | 10/29 | 跨机 **45↔225**，Qwen3.8-27B（hybrid，`--block-size 1536`），**同构 TP2↔TP2**（异构 TP2↔TP4 被 hybrid gate 关掉），PR70 daemon | **主链路通过**（SubmitIntent ~11s READY，B External prefix hit 59.1%）；**并发度压力：14 `create_copy_task` 全成，42 约半挂**——窗口 4+0.2s 节奏在 42 规模仍扛不住；工程风险高，E15 限速为刚需 | 见「实验实录 · 2026-10-29」 |
| 18 | 10/30 | 先单机 put 填满；后清 225 占卡、拉回 B(TP2)，跨机 42-key 风暴 + `/metrics` 余量 | **驳回「B 物理空间装不下」单因说**：跨机 gate4×42 → 25 成 / **17 全是 NAH**，失败当下 **B 合计仍有 448–525MB free**；专打较空 seg 时 **free≈164MB 仍 NAH**（单对象 ~32MB 装得下）。put 填满对照给的是 `-800` 不是 `-200`。NAH 与**并发/分配压力**相关，不是「字节容量见底」 | 见「实验实录 · 2026-10-30 · #18」 |
| 19 | 11/02 | 同拓扑 **空闲 B segment** 上 42 key × **window=1 vs window=4**（分轮 warm，每轮前重启 A/B 防 spill） | **两边皆 42/42、0 NAH**（W1≈42s，W4≈11s）。B segment 空闲 + 闸=4 **复现不出** #17/#18 的 NAH →「纯 window=4 并发预留」不是充分条件；#18 半挂需叠加更高连发/已有占用/节奏等因素 | 见「实验实录 · 2026-11-02 · #19」 |
| 20 | 11/02 | 半占用 segment × window 矩阵 | **中止**（信息量低；与用户确认后停） | — |
| 21 | 11/02 | NAH 源码诊断：给 `OffsetBufferAllocator::allocate` / `CopyStart` 打 `largest_free` | **方案已定、真机未跑成**（需改 Mooncake 打 `largest_free`；出网无法编换 .so / 装 gdb；当时卡被占）——不阻塞 #18/#22 结论 | — |
| 22 | 11/02 | **seg 容量剂量**：同风暴半占用(~50%) + window=4 ×42，B **1GB vs 4GB** | **1GB：25/42、17 NAH**（失败时 B free≈449MB）；**4GB：42/42、0 NAH**。加大 segment 后 NAH 消失 → 运营上「segment 太小/有效空间不够」成立；但失败瞬间仍有数百 MB free，**不等于**字节已见底（与 #18 一致） | 见「实验实录 · 2026-11-02 · #22」 |
| 23 | 11/02 | 单机 45：A=TP2(:8001) + 本机合成目标 seg（卡6/7）+ **PR71 E14** （真 Mooncake `create_copy_task` + 真 `ZmqEventPublisher`） | **发件人侧通过**：warm→拷前 0→`create_copy_task=2`→拷后 2/2→`BlockStored(CPU, full_attention)`；warm 落 225 = A 近满 spill（非代码错）；真机脚本手工不进 Actions；**未验**索引/选路/gRPC/注册 | 见「实验实录 · 2026-11-02 · #23」 |
| 24 | 11/02 | 单机 45：A=TP2(:8001) + **16MiB 合成空段**（卡6/7）+ **PR72 段容量准入**（真 warm / 真副本大小 / 真 `/query_segment`） | **通过**：need≈50MiB > remaining → `stopped before submit`；假 admin `@pytest.mark.smoke`（CI 可跑）；真机 `@pytest.mark.npu`（`KVCM_NPU_SMOKE=1`，不进 Actions）；**未验**：腾出后再拷、TP>1 漏放、gRPC | 见「实验实录 · 2026-11-02 · #24」；[`tools/admission_real_smoke.py`](tools/admission_real_smoke.py) |

## 实验实录（异构 TP e2e，2026-10-09，A2 RoCE）

总表 #5-#8/#12 的操作细节与输出原文（2026-10-10 自 README 迁入；文中裸文件名按文末「勘误与演进」的映射解读）。

### 单机正向：A=TP2 → B=TP4（总表 #5）

- 配置：A=TP2（卡 0,1，:8001）、B=TP4（卡 2,3,4,5，:8002），均 `prefill_tp_size=2, decode_tp_size=4`；模型 VL-8B（GQA，8 KV head；`effective_tp=4`，`num_sub_keys=2`）。
- 流程：warm A → `collect_prefix_keys.py --tp-size 4 --peer-tp-size 2`（展开 eff rank 0-3，6 满块 × 4 = **24 keys**）→ 解析 B 的 4 seg（`worker_B.log` 的 `listening on`，pid 对号 rank）→ `precopy.py --targets seg0,seg1,seg2,seg3`（eff rank i → B seg i）→ 打 B。
- 结果：**24/24 key 存在**且副本对号（eff0/1 → A TP0 seg，eff2/3 → A TP1 seg）；precopy 后每个 eff-rank key 在 B 对应 seg 有本地副本；B 侧 `kvpool hit tokens: 768/801`（**95.9%**），TP0-3 各自 `backend get keys=6` 成功、零 invalid block；A(TP2)/B(TP4) 同 prompt 贪心 16 token 输出逐字一致。
- 证据：`logs/worker_A.log`（`tp_mismatch put keys=12` × 2 rank）、`logs/worker_B.log`（分 rank get）、`logs/e2e_hetero_2_4.log`（首次跑，死于 collect exit 2；补丁后手工复测通过）。

### 跨机：245(A=TP2) → 217(B=TP4)（总表 #8/#12）

配置：A=TP2（245，卡 0,1，:8001）、B=TP4（217，卡 0-3，:8002），master 在 245 宿主机 `:50088`；两端容器同镜像，217 容器需先打 `patch_tp_mismatch_worker.patch`。跨机操作用 `ssh root@7.242.105.217`（密码，expect 包装；sshpass 未装）。

- 流程与单机一致：warm A → collect 24 keys（`prefix_keys_xhost_het.txt`）→ `resolve_segments.sh --target-ip 7.242.105.217 --tp 4` → precopy（245 侧容器内跑，`--targets` 平铺 4 seg）→ 打 B。
- 结果：**24/24 key 存在**，A=TP2 的 sub-key put 跨机生效；**24/24 `replica_copy_success`**（A 侧 `client_service.cpp:2624`，~1 key/s），副本对号：rank0→`217:16471`、rank1→`217:15526`、rank2→`217:16596`、rank3→`217:15742`（每 key 双副本：245 源 seg + 217 目标 seg，`check_exists` 取证）；B 首次请求 `hit_tokens: 768/801`（**95.9%**，与单机正向一致）、零 invalid；B(DEBUG) 分 rank `MooncakeBackend.get enter keys=6` × TP0-3；A/B 同 prompt 贪心 16/64 token 输出逐字一致。
- **本机读确证（2026-10-09 深夜，杀源实例法，总表 #12）**：杀 worker-A 并等 client TTL 注销其 segment 副本（`batch_get_replica_desc` 仅剩 217 副本）后，B 用**全新 prompt 首次**命中：外部 get `enter keys=6` ×4 rank 同秒成功、external hit 累计 89.8%、零 invalid、输出与 A 逐字一致 ⇒ **get 读的是 217 本机 seg 副本**。早期用 RoCE 网卡打点取证的方法已撤回（netdev 计数器不统计 RDMA，总表 #9）；同 prompt 第二次命中会被 vLLM 内部 HBM prefix cache 接住（外部 get=0），判定必须用全新 prompt 首中。B 命中时刻 `Connected to segment: 7.242.105.245:*` 是 precopy copy-task 收尾传输（与 get 同秒重叠造成的误读，勿再当读取证据——本机 seg 读不留连接日志）。
- 坑（跨机新增）：217 容器残留旧 worker 致假 READY（清理方法现见 [README.md](README.md)「操作注意事项」）；master 日志轮转坑已失效（见文末「踩坑记录」）。

### 反向：A=TP4 → B=TP2（总表 #6）

反向暴露两个新约束（规则形式见 [REPRODUCE.md](REPRODUCE.md) §2③，此处只记发现过程）：

1. **配置语义**：首跑 B 侧 `decode_tp_size` 配成本机 TP=2，但 `infer_tp_mismatch_info`（`config_data.py:48`）对 kv_both 读的是 **peer** size → 判「无不匹配」退化为普通 TP2 行为；改配 4 才启用 mismatch。
2. **get 假命中**：同步 load 按本机 rank 名取全本地切片尺寸 → 尺寸不匹配 → invalid → 全部回退重算，而 external hit 指标照样 ~95%——靠分 rank 日志才揭穿（判定方法因此写进 REPRODUCE §3）；当时仅异步路径（`KVCacheStoreRecvingThread → _load_kv_tp_mismatch`）感知 mismatch，故 B 配 `LOAD_ASYNC=1`。

实测：A=TP4（卡 0-3，`decode_tp_size=4`→plain put 即 eff 命名）、B=TP2（卡 4,5，`LOAD_ASYNC=1`）。

- A 四 rank 各普通 put 6 key（24/24，命名=eff 0-3，内容=2 头/eff shard）；
- precopy 映射：eff rank i → B seg[i//2]，当时 `--targets` 传**重复列表** `seg0,seg0,seg1,seg1`（2026-10-10 起由 precopy 自动展开，见「勘误与演进」）；
- B：`External prefix cache hit rate: 95.9%`、零 invalid；TP0/TP1 各 `tp_mismatch get keys=12`（6 块 × 2 sub-key）成功；
- A(TP4)/B(TP2) 同 prompt 贪心 16 token 输出逐字一致。

**方向规则**（谁拆谁退化、`load_async` 约束演变的完整表述）：见 [README.md](README.md)「怎么运作 · 异构 TP」与 [REPRODUCE.md](REPRODUCE.md) §2③。

**勘误与演进（2026-10-10）**：

- **判定方法勘误**
  - #12「0.3.11 无显式本机优先」有误：0.3.11.post1 `SelectBestReplica` 已实现 prefer local MEMORY（`real_client.cpp:283-286`）；0.3.12+ `SelectCompleteMemoryReplica` 仅 session API 路径。结论以 [REPRODUCE.md](REPRODUCE.md) §3 为准。
  - #9 netdev 打点法撤回：计数器不统计昇腾 RoCE RDMA；本机读判定的终判改为杀源实例法（REPRODUCE §3）。
- **补丁演进**
  - `patch_tp_mismatch_worker.py`（python 子集补丁，仅 `worker=self`，#5/#7 行）→ `patch_tp_mismatch_worker.patch`（#15835 完整版 rc1 backport，含同步 load 分发）；`LOAD_ASYNC=1` 由硬约束降为推荐（同步 mismatch 路径 2026-10-12 真机复测通过，总表 #14）。
- **问题修复**
  - `precopy.py` 退出偶发 allocator abort/挂起：根因 = 退出期 teardown 竞态（GC/atexit 乱序析构 × 在途收尾；0.3.11.post1 缺上游 #3943 drain，precopy 是唯一不 close 的脚本）——已修复为 READY 后显式 `store.close()`；旧日志中出现时 READY 已打印即可忽略。
- **控制面重构（按调用链三次归位）**
  - import 上游权威：`keys.py` import vllm `kv_cache_utils` + vllm-ascend `PoolKey`；内置镜像降离线 fallback；哨兵 = `--check-master` + `test_keys.py::test_upstream_parity`。
  - 单入口合并：`precopy.py` = warm prompt → 进程内 resolve → 算 key → `batch_is_exist` 核对 → copy → READY，key 全程内存（`--dump-keys` 仅调试留档）。
  - 目录归位：产品控制面 → `precopy/`（precopy/keys/resolve/common），集群脚本 → `cluster/`，调试 → `tools/`；`resolve_segments.sh` Python 化为 `precopy/resolve.py`（admin API + pidfile/ss，**不再读日志**——「master 日志轮转」「旧 master 无 mount 行」两坑随之失效，保留下方作历史）；`check_exists.py` 并入 `keys.py --keys-file --check-master`；新增 `tools/test_resolve.py`。
  - 历史裸文件名映射：`collect_prefix_keys.py`→`precopy/keys.py`；`resolve_segments.sh`→`precopy/resolve.py`；`check_exists`→`keys.py --keys-file --check-master`。本文及 README/REPRODUCE 历史章节按此映射。
- **反向异构一键化（2026-10-10，外部 review 修正）**
  - `precopy.py` 对反向（peer>local）自动按 `i // num_sub_keys` 展开 targets——此前须手工传重复列表（见实录反向），run_e2e 反向会报「need targets[0..3]」走不通。
  - `run_e2e.sh` `DECODE_TP_SIZE` 默认从 `$TP_B` 改为 `max(TP_A,TP_B)`：kv_both 的 decode_tp_size 是 **peer** size，两个方向都必须恒为 effective_tp；原默认在反向让 B 判「无不匹配」→ 普通 get → 假命中。
  - `run_e2e.sh` worker 就绪轮询后 fail-fast（此前未就绪也继续 warm，且正好撞上假 READY 坑）。
  - `run_e2e.sh` 反向（TP_A>TP_B）自动给 B 注入 `LOAD_ASYNC=1`（走 #6 已验证的异步 sub-key get；同步 mismatch 路径补丁恢复后仍未复测，避免一键落在未验证路径）。

**踩坑记录（跨机操作）**：

仍现行的操作陷阱已并入操作文档：残留旧 worker 致「假 READY」与清理、跨机 SSH 限速、两机非共享存储 → [README.md](README.md)「操作注意事项」；判定环节的陷阱（假命中、全新 prompt、TTL、netdev、DEBUG 取证）→ [REPRODUCE.md](REPRODUCE.md) §3。以下只留历史记录：

- **证据与记录**
  - `worker_A.log` 被 10/09 深夜两次重启覆盖（`nohup >` 截断）：早期 72 次 `replica_copy_success` 以总表 #8 文字为准；重启 A 后需重 warm。
- **已修复 / 已失效**
  - `precopy.py` 退出偶发 allocator abort（RC=134）/挂起：已修复（见「勘误与演进」）。
  - master 日志被轮转后 fd 仍写改名文件（`.bak.27b`）→ `ln -sf` 修复：当时 `resolve_segments.sh` 读 master 日志 mount 行；resolve 改走 admin API + pidfile/ss 后此坑消失。

## 实验实录（2026-10-12 回归：bc6b5be 单入口控制面 + #15835 完整补丁）

环境：两端容器均 #15835 完整版补丁（`patch_tp_mismatch_worker.patch`）；A=245 TP2（卡 0,1，:8001）、B=217 TP4（卡 0-3，:8002）；master 245 宿主机 `:50088`；env 两台同 `DECODE_TP_SIZE=4`（**对端 TP 字段**，=max(2,4)，两台同值——不是本机 TP；当日另有 `PREFILL_TP_SIZE=2`，kv_both 不读、无作用。2026-10-12 晚起脚本删 `PREFILL_TP_SIZE`、`DECODE_TP_SIZE` 正名 `PEER_TP_SIZE`，语义不变；现行配置见 [README.md](README.md)「env 配置」）；`VLLM_LOGGING_LEVEL=DEBUG`（仅取证：分 rank `get enter` 日志非 DEBUG 不打印）。

### 正向：245(TP2)→217(TP4)（总表 #13）

- 流程：warm A（`"va-precopy v53 regression hetero cross-machine prompt. "×80` = 1041 tok，8 满块）→ 单入口 `precopy.py --role B --tp-size 4 --peer-tp-size 2`（32 keys READY；eff0→16051, 1→16258, 2→16276, 3→16787）→ 打 B。
- 结果：B 首中外部 get ×4 rank 成功（复命中走 vLLM 内部 HBM cache，预期）、external hit **88.2%**（从实例启动起累计所有请求，不是单请求的值；所以低于 95.9% 的单请求值）、零 invalid；16-tok ×6 + 64-tok ×2 **8/8 与 A 逐字一致，1-token 翻转未复现**（与 #10「新 DEBUG 实例未复现」一致）。
- B 机探测路径：容器无 `ss`、pidfile 亦 stale——改用 master admin API `GET :9003/get_all_segments`（TTL 已自动注销旧 seg，名单干净）+ 显式 `--targets`。

### 反向：217(TP4)→245(TP2)，同步 load（总表 #14）

- 首跑失败（配置坑）：把 A env 按方向误翻成 `PREFILL_TP_SIZE=4 DECODE_TP_SIZE=2` → kv_both 读 peer=`decode_tp_size`=2=自身 → mismatch **静默禁用** → plain get 按本地 4-head 尺寸读 2-head eff key → `mooncake -800`，日志形状 `Failed to get 9 keys out of 9`（=9 块×1 key/rank，即「每块 1 key」指纹；正确 mismatch 应为每 rank 18 keys）→ 500。**#6 的坑 1 在新单入口下复踩**，规则固化为：两台同配（README「env 配置」+ `start_worker.sh` 整除校验）。
- 修正后：A 重启（无 `LOAD_ASYNC`）；B 不动（plain put 名字与字节恒等于 eff key）→ warm B 全新前缀（`"...reverse sync prompt. "×80` = 1201 tok，9 满块）→ 单入口 `precopy.py --role A --tp-size 2 --peer-tp-size 4 --targets <A 两 seg>`（36/36 exist；反向自动展开 eff i → targets[i//2]；rank↔seg 经 `ss` 端口→pid + `npu-smi` 卡→pid 对号）→ 打 A。
- 结果：**每 rank `MooncakeBackend.get enter keys=18`**（TP0 读 @0…、TP1 读 @3…，9 块 × 2 sub-key 拼回本地）、`prepare get ... load_async=False` ×2 rank（hunk3 同步分发生效）、external hit 74.6→79.7%、零 invalid、16×6 + 64×2 **8/8 与 B(TP4) 参考逐字一致** → **`_load_kv_tp_mismatch` 拼回 + 同步分发两条空白路径首次在真机验证通过**。

### 10-12 踩坑（新）

1. **PYTHONHASHSEED 未设 → 控制面 key 全体漂移**：vllm `kv_cache_utils.init_none_hash`（rc1 容器版 `kv_cache_utils.py:111-114`；main 已重构为 `resolve_none_hash_seed`）在 seed 未设时 `NONE_HASH = os.urandom(32)` 折入**每个** block hash，且 sha256 不在 `_CBOR_HASH_FUNCTIONS` 警告名单——**静默**。实测同一 prompt 两次算出 `dc9fdff8…`/`948bbe7a…` 两个 key 集，`batch_is_exist` 0/N 假失败（引擎其实已 put）。引擎侧 `start_worker.sh` 已带 seed；控制面（keys.py/precopy/手工探测脚本）此前没带。已修：`keys.py::_hash_prompt` fail loud；顺带勘误 README 前置表「默认 sha256 不需要」的说法。
2. **孤儿 EngineCore 占卡**：重启 A 时只 kill 了 api_server（按 pid 段过滤漏掉 EngineCore/Worker——其 pid 与 api_server 相差悬殊），HBM 被孤儿占住，新实例 worker 起不来；segs 还是旧实例的。清理须按 `npu-smi`（卡→pid）+ `ss`（端口→pid）对号出的实际 pid 杀。
3. **「副本随属主消失」是误判，级联删除不存在**：10-12 观察到的 0/32 是踩坑 1 的假失败（控制面未带 PYTHONHASHSEED → key 名漂移 → `batch_is_exist` 假失败），并非 key 的 client_id 级联删除。10-20 受控复验：kill 写入者 A → client TTL 过期 unmount 其 seg（master 日志零驱逐零 remove）→ `batch_is_exist` 仍 32/32 且副本全在 B seg，跨机裸 client `get` 字节级读回 3/3。结论：**副本存亡绑定「副本所在的 seg 所在 client」，不绑定写入者；key 的 client_id 下线只清其自己 seg 上的副本，远端副本保留**（代码锚点：v0.3.11 `master_service.cpp` `CleanupStaleHandles` 的清理条件 = 副本所在 seg 是否失效；main `replica.h::isAffiliatedWith` 同义）。复验记录见总表 #15。
4. **ssh 限速下 expect 的正确姿势**：`timeout` 杀掉 expect 后孤儿 ssh 仍握住管道写端，`| tail` 等 EOF 会把整个外层命令拖挂（实测被拖 300s+）。姿势：输出重定向落文件 + expect timeout 分支显式 `close`。

### 10-12 对既有结论的影响

- 同步 load mismatch：**已验证**（#14，A 同步 load 36/36）。
- key 一致性：**已验证**（#13/#14 单入口 `batch_is_exist` 32/32、36/36 全中）。
- 多卡 rank↔seg 对号：**已验证**（`--targets` + admin API/`npu-smi` 手工对号）。
- 仍待：`run_e2e.sh` 一键真机重跑（同构 TP=2 + 异构双向；10-12 回归走单入口手工分步，一键编排本身未跑）、1-token 翻转根因、MLA（明确不支持）；`tools/test_keys.py` 容器内单跑；resolve.py proctitle 自动匹配的多卡真机验证。

## 实验实录（2026-10-26：kvcm daemon 真机冒烟，E11 进程化首验）

环境：245 本机。master 宿主机 `:50088`（`--client_ttl=120`）；源引擎 = 存活 vLLM TP2（卡 0,1，:8001，tp_mismatch peer=4）；目标 = 两个合成 seg helper（卡 6/7）；kvcm daemon（E11 进程壳）经 gRPC 收 intent 驱动 copy。容器 `vllm-ascend-lake-test`，控制面全程 `PYTHONHASHSEED=0`。

**状态：e2e 全绿（6/6 PASS）**。当日下午 daemon 链路已全通但末轮卡在引擎自身
put 路径被打伤（见坑 8）；**重启 :8001 引擎（原参数：`TP=2 PEER_TP_SIZE=4`
经 `cluster/start_worker.sh`，宿主 pid 2924707，~165s READY）后复跑全过**：
warm 4.3s 落池 → intent ack → 25 块副本 READY → daemon SIGTERM 退出码 0。
旧引擎 seg 在 TTL（120s）内自动从 master 过期，新引擎 seg 全新注册。
操作备忘：容器 PidMode 非 host，start_worker.sh 写的 pidfile 是容器命名
空间 pid，宿主侧操作须用 `pgrep -af vllm.entrypoints` 对照 cmdline 找宿主
pid。途中根出 8 个坑，全部对代码验证：

### 10-26 踩坑

1. **ascend 协议绑定必须 ACL context**：`mooncake_transfer_engine_npu` 无论 protocol 都装 AscendDirectTransport，无 `torch.npu.set_device()` 建的 ctx 时 setup rc=-600（ctx NULL）。已修：`client.py::connect` 加 `ascend_device` 前置仪式（与 precopy 同对策），daemon `--device` 透传。
2. **copy 连发打爆目标侧建链（本轮主根因）**：daemon 一次铺开 14 个 `create_copy_task` → 源 worker 向目标 adxl 消息端口的**并发建链失败**（`ConnectToPeer FAILED`，`channel_msg_handler.cc:515`）→ `TRANSFER_FAIL`（`client_service.cpp:2994` 只重试 NO_AVAILABLE_HANDLE）。对照：连发 42 任务 = 24 成 18 败；**窗口 4 节奏化 = 42/42 全成**；简单 0.2s 错峰提交 = 源侧 38 成 0 败。十月 precopy 从不踩是因为严格串行（`create_copy_and_wait`）。当时的临时措施是 `backend.py` 的 `submit_interval_s`（0.2 秒错峰）；**该参数已被并发窗口取代**——现在是进程级 `submit_concurrency`（默认 4，所有 copy 共用一把信号量），既压住建链风暴又不再把大前缀拖成 N 倍往返。按链路利用率推进的限速器仍归 E15。
3. **同物理卡多进程 HCCL 互踩**：两个 helper 同卡（都 `ASCEND_RT_VISIBLE_DEVICES=6`）→ 第二进程 `HcclCommPrepare` 必败（`comm_channel.cc:105`）。合成测试每进程必须独占物理卡。
4. **同名 seg 客户端更替 + client_ttl=120s 陈旧**：同名重起 helper，copy 解析到死进程握手端口 → `Connection refused` 秒败；更阴的是 `replica_copy_skipped, info=target_replicas_already_exist`（`client_service.cpp:2605`）按陈旧副本记录报**假 SUCCESS**。测试每轮身份必须全新。
5. **adxl 端口分桶复用 → 断信道缓存中毒**：端口段 = `base + 物理卡×100`（`utils.cpp:227 FindAdxlListenPort`）；新进程复用刚死掉的端口，继承源端 `Channel is unavailable due to a previous transport failure`（`comm_channel.cc:224`）→ 秒败。`ASCEND_BASE_PORT`（`ascend_direct_transport.cpp:202`）每进程分桶隔离。vLLM worker 用 20096/20184（卡 0/1 桶），我们客户端默认 20600 桶。
6. **seg 名与副本端点是两个命名空间**：P2PHANDSHAKE 下 transfer engine 把 `local_server_name` 重写成 `ip:随机RPC端口`（`transfer_engine_impl.cpp:151-185`）——store 层 seg 名仍是 setup 实参，副本描述 `transport_endpoint` 是重写后的。copy targets 认 seg 名、READY 核验认端点 → 合成 helper 两形态不一致时核验必炸（`VerifyError`）。**正确姿势（vllm-ascend `mooncake_backend.py:122-134` 同款）：先 `TransferEngine.initialize` → `get_rpc_port()` → 以 `ip:rpc_port` 为 local_hostname setup 并传 `engine=`**，两命名空间合一。引擎 worker 天然满足（seg 名 = 端点），产品 `_verify_missing` 契约不变；合成 helper 已照此改造。
7. **store 退出析构崩溃**：AscendDirectTransport 析构期 `corrupted size vs. prev_size` + ctx NULL unmount 报错，合成客户端 100% 复现；daemon 优雅退出路径目前不受影响（退出码 0）。
8. **引擎 put 路径可被实验副作用打伤（共享实例警示）**：一下午的僵尸任务重试 + 断信道缓存把存活 worker 的传输子系统拖入降级态——末轮 warm 时引擎**自身 put** 大批量失败（`Transfer failed for key ... TRANSFER_FAIL` + `Failed to revoke put ... OBJECT_NOT_FOUND`，`client_service.cpp:1685/1776`），KV 不落池。kvcm exists 预检正确拦截（`prefix not fully in pool`）。教训：对共享引擎做破坏性探测要控制节奏；引擎重启可复原。

## 实验实录（2026-10-29：PR70 跨机同构 TP2 + copy 并发度，总表 #17）

> **单位约定**：下文 **14 / 42 均指 `create_copy_task` 任务数**（= 待复制的 KV **key** 条数，每任务复制 1 key → 目标 1 个 seg）。**不是** token 数、不是 block 数、也不是配置项里的「连接上限」。TP2 下一个满块前缀会展开成多条 key（按 rank/group），「复制一个前缀」= 多条 `create_copy_task`。

环境：

| 项 | 值 |
|----|----|
| 主机 | A=`7.242.104.45`（卡 0,1，:8001），B=`7.242.110.225`（卡 0,1，:8002） |
| 模型 | `/data/models/Qwen3.8-27B`（qwen3_5 hybrid：linear + full attn），`--block-size 1536`，`MAX_MODEL_LEN=4096`，`PYTHONHASHSEED=0` |
| TP | **同构 TP2↔TP2**，`PEER_TP_SIZE=2`。原计划 TP2↔TP4：**hybrid 模型走 tp_mismatch gate 会被静默关掉**，无法做异构验证 |
| 镜像 / wheel | `vllm-ascend:v0.26.0rc1` + mooncake `0.3.11.post1`；A2 RoCE（`enp67s0f5`） |
| 控制面 | PR70 `kvcm.daemon`（`:9000` gRPC），`--max-concurrent 4`，`--device 2`（独占物理卡建 ACL ctx），`--segs` = B 两 seg；master `:50088` / admin `:9003` |
| 共享机干扰 | 同机 `mtsc_test`（带 `trace_mtsc_guarded.sh` 杀外来 vllm）与 `Qwen3.5-122B…` TP8 多次抢卡；测前需停干净，否则假失败 |

### 主链路（SubmitIntent e2e）

1. Warm A：prompt ≈2601 tok（≥1 满块）→ 池内出现对应 key。
2. `SubmitIntent({model_id=Qwen3.8-27B, token_ids}, target=inst-B)` → 立即返回 `intent_id`。
3. 轮询 `GetTask` → **READY ≈11–12s**（清环境后复测同样 ~11.1s READY）。
4. 打 B 同 prompt：External prefix cache hit rate **59.1%**（滚动累计口径；hybrid 满块对齐与 VL-8B 的 95%+ 不可直接比）。

结论：PR70「gRPC intent → TaskEngine 闸 → PrefetchFlow → `create_copy_task` → 目标可读」跨机主路径通。

### copy 并发度微基准（互不重叠的新 key；重启 A 清传输降级后）

方法：控制面旁路 StoreClient（`protocol=ascend`）对**尚未落在 B 的 fresh key** 发 `create_copy_task`；`asyncio.Semaphore(window)` 限制同时在飞任务数；可选提交间隔 `pace_s`。每任务 `query_task` 到 SUCCESS/READY 或 FAIL（含 `NO_AVAILABLE_HANDLE (max retries reached: 10)`）。

| 规模（key / `create_copy_task` 数） | 窗口 | 节奏 | 结果 | 耗时 |
|-----------------------------------|------|------|------|------|
| **14** | 4 | 0.2s | **14/14** | ~3.7s |
| **14** | 14（等同全开） | 0 | **14/14** | ~0.7s |
| **42** | 4 | 0.2s | **18/42**（~43%） | ~22s |
| **42** | 14 | 0 | **21/42**（~50%） | ~7.9s |

失败形态（42 规模主导）：

- **`NO_AVAILABLE_HANDLE (max retries reached: 10)`**：master 留内存失败，不是 #16 那种建链连不上（`ConnectToPeer FAILED` → `TRANSFER_FAIL`）。store 对这个错误有限次重试，**不是排队变慢，是硬失败终态**。
- 偶发 **`OBJECT_NOT_FOUND`**：master `--default_kv_lease_ttl 11000`（≈11s）；大批量跑久了 key 过期（与建链风暴叠加）。
- 方法陷阱：同一批 key 先跑窗口 4 再跑窗口 14 → 第二轮大量 `replica_copy_skipped` 假全成；**对比必须用互不重叠的 fresh key**。
- 风暴后遗症：即便改回串行，仍可能半挂（信道缓存中毒，同 #16 坑 5/8）；需**重启源引擎**才能复原。

对照 #16 坑 2（单机、合成目标 seg）：当时连不上是 `TRANSFER_FAIL`，窗口 4 节奏化 42/42 全成。**本次跨机是另一件事**：主错误是 `NO_AVAILABLE_HANDLE`（master 留内存失败）；同样 42 任务 + 窗口 4+0.2s 仍约半挂。「闸=4」限的是在飞建链，不看目标段剩余。E15 的网卡限速仍然不做。

### 工程含义（供 PR70 reviewer）

1. **异步编排 ≠ 无限并发安全**。`asyncio` 只让调用方不堵；Mooncake/Ascend 路径在句柄耗尽时直接 `FAILED`，不会自动排队到成功。
2. 本床 **14 任务全成、42 任务不可靠**。42 的主错误是 `NO_AVAILABLE_HANDLE`（master 留内存失败，最多重试 10 次），不是 #16 的建链 `TRANSFER_FAIL`。软闸默认 4 限的是在飞建链 RPC，不看段剩余。
3. **生产风险**：一个稍长前缀在 TP≥2 时很容易展开到数十 key；若 Router 突发多个 prefetch intent，或单 intent 内一次铺开全部 key，会稳定踩到本崖。PR70 现状（`max_concurrent=4` + 进程级 `submit_concurrency`）是止血，**不是**可承诺的 SLO 级吞吐。
4. **E15 为阻塞项**：需要按窗口推进的限速器 + 明确背压/重试语义；仅靠协程多开会把传输子系统打进降级态（连带打伤引擎自身 put，见 #16 坑 8）。
5. **异构 TP2↔TP4 本轮未测**：Qwen3.8-27B hybrid 被 gate；要复验异构需换非 hybrid 模型（或上游解除 gate）。

代码锚点：`kvcm/backends/mooncake/backend.py`（`submit_concurrency`）、`kvcm/task_engine.py`（`max_concurrent` 传输闸）、Mooncake `client_service` 对 `NO_AVAILABLE_HANDLE` 有限重试（错误文案 `max retries reached: 10`）。

### #18–#22 续测后的结论与生产启示（2026-11-02）

| 命题 | 判定 |
|------|------|
| NAH = 目标 segment「字节已见底」 | **驳回（充分单因）**：失败时 B 仍有数百 MB free（#18/#22） |
| NAH = 纯 window=4 | **驳回**：B segment 空闲时 W1/W4 皆 42/42（#19） |
| 半占用(~50%) + window=4 ×42 可复现 | **成立**（#22 的 1GB 轮：25/17 NAH） |
| 加大目标 segment 可压住同风暴 | **成立**（#22：B 4GB → 42/42、0 NAH） |

生产启示（与 lake 职责边界对齐）：

1. **copy/预热必须限速**——E15 + 现有提交窗/前缀闸是刚需；不能靠开闸多扔。
2. **目标侧 `global_segment_size` 按突发 copy 留余量**——别只按稳态存放量；半占用小 segment 是危险组合。
3. **监控看失败率 + 目标 free + 在飞 copy**，不要只看「总容量还没满」。
4. **能 D-direct / 本地命中就少跨节点 copy**；NAH 是执行失败 → 上报/F4 重路由，过载拒绝仍归 gateway。

## 实验实录 · 2026-10-30 · #18（NAH vs 目标空间）

动机：同事认为 #17 的 `NO_AVAILABLE_HANDLE`「不是建链太多，而是 A→B 同时任务太多、B 物理空间申请不下来」。官方错误码表确写 `-200 NO_AVAILABLE_HANDLE` = *Memory allocation failed due to insufficient space*。本轮用 `/metrics` 的 `segment_*` 余量打点做对照。

### 环境现状（测前）

| 项 | 值 |
|----|----|
| A (45) | Qwen3.8-27B TP2 仍在；两 seg 各 1GiB，占用约 **80%+**（`segment_allocated_bytes`） |
| B (225) | **已不在池**：`get_all_segments` 只剩 A；225 卡被 `qwen35-122b-mixed` TP8 占满 → **无法复跑 #17 跨机风暴并采 B 余量** |
| 工具 | `GET :9003/metrics`（`segment_allocated_bytes` / `segment_total_capacity_bytes` / `master_put_start_alloc_failures_total`） |

### 做了什么

1. **引擎 key → 合成小 tgt（污染，作废）**：池内 key 租约 ~11s，copy 大量 `-704 OBJECT_NOT_FOUND`；tgt 占用曾升至 ~76% 但失败类不是 NAH。脚本：`demo/logs/probe_nah_capacity.py`。
2. **合成 src→tgt copy（传输出错，作废）**：两 helper 之间 `HcclBatchPut`/`TRANSFER_FAIL`，tgt **始终 0%**——测不到分配崖。脚本：`demo/logs/probe_nah_capacity2.py`。
3. **主结论实验：本机 put 填满 64MiB seg**（8MiB×对象，preferred=该 seg）  
   - 0..7：全部 `rc=0`，`free` 从 64MiB 线性降到 **0（100%）**  
   - 第 8 次：`pre_free=0` → 返回 **`rc=-800 TRANSFER_FAIL`**（日志伴 `Connect timeout` / `HcclCommPrepare` 失败，像是满了之后改走别的远端 seg 再传）  
   - 全程 **`master_put_start_alloc_failures_total = 0`**，**从未出现 `-200 NO_AVAILABLE_HANDLE`**  
   - 脚本与结果：`demo/logs/probe_put_fill.py`、`probe_put_fill_result.json`

### 对同事结论的判定

| 命题 | 判定 |
|------|------|
| 官方 `-200` 语义含「空间不够」 | 成立（文档） |
| 「本拓扑 #17 失败 = B 物理空间放不下」 | **本轮不能坐实**：B 已卸，无失败瞬间 B 余量；可控「空间见底」路径给出的是 **-800** 而非 **-200**，且 alloc-fail 计数器为 0 |
| 「所以不是建链问题」 | **也不能据此排除**：满 seg 的 put 失败路径本身就在打 Connect/HCCL；#16 日志里确有 `ConnectToPeer FAILED` |

### 续测（清 225 占卡后，同日）

环境：停 `qwen35-122b-mixed` / `vllm-ascend-v0.30`；拉回 A/B TP2 + master；B seg=`225:16137`/`225:16855` 各 1GiB。脚本：`demo/logs/probe_b_space_storm.py`、`probe_emptier_seg.run.log`。

| 轮次 | 设置 | 结果 | 失败当下余量 |
|------|------|------|----------------|
| burst14 | window=14，14 key | **14/14 OK** | 开始前 B free 1.6GB |
| gate4_42 | window=4 + 0.2s，42 key | **25 OK / 17 FAIL**，失败类 **全是 `NO_AVAILABLE_HANDLE`** | 失败时 B **合计 free 448–525MB**（约 75–79% 占用）；`put_start_alloc_failures=0` |
| emptier-only | 只打较空 seg `225:16855`，window=4，12 key | **7 OK / 5 NAH** | NAH 时该 seg **free≈164MB / 113MB / 62MB**（单对象约 32MB，164MB 仍够装） |

终态不均：一轮风暴后 `16137`≈97% 满、`16855`≈70%——rank→seg 映射会让单边先满，但 **emptier-only 轮在仍有 164MB free 时照样 NAH**，说明不是「目标 seg 字节见底」这一条能解释完。

### 对同事结论（更新）

| 命题 | 判定 |
|------|------|
| 官方 `-200` 语义含 insufficient space | 成立（文档） |
| 「#17/#18 失败 = B 物理空间放不下」 | **驳回（作为充分单因）**：失败当下 B/目标 seg 仍有数百 MB free |
| NAH 与并发/分配压力有关 | **成立**：同拓扑 14 全成、42 半挂；半占用小 segment 上 window=4 可稳复现（#22） |

代码/结果：`demo/logs/probe_b_space_storm_result.json`、`probe_put_fill_result.json`。

## 实验实录 · 2026-11-02 · #19（window=1 vs 4，空闲 B segment）

动机：#18 显示 NAH 时 B 仍有数百 MB free，怀疑与**并发预留**有关。用同一 42-key 规模拆 **串行 window=1** vs **闸=4**，看是否「串行全成、并发 NAH」。

### 方法要点

- A/B 各 2×1GiB；一次 warm 两轮（~84 key）会把 A 打满并 **spill 到 B**，对照作废。改为 **PHASE 分轮**：每轮只 warm≈48 key（取 42），**每轮前重启 A/B** 保证 B≈空。
- 脚本：`demo/logs/probe_w1_vs_w4.py`（`PHASE=w1|w4`）、结果 JSON 同目录。

### 结果

| 轮 | window | 开跑 B | 结果 | 耗时 | 结束后 B |
|----|--------|--------|------|------|----------|
| W1 | 1（串行） | 0% / free 2147MB | **42/42 OK，0 NAH** | 41.6s | 64% / free 774MB |
| W4 | 4 | 0% / free 2147MB | **42/42 OK，0 NAH** | 10.6s | 64% / free 774MB |

判定：`inconclusive_both_ok` —— B segment 空闲时闸=4 **压不出** NAH，不能单独坐实「window=4 并发预留 ⇒ NAH」。

与 #17/#18 对齐：那边是 **已有占用 / 连发 14 或 42 风暴** 才半挂；B segment 空闲 + 温和闸=4 仍全成。E15 限速仍有价值（跨机高连发），但根因不是「闸=4 本身」。

## 实验实录 · 2026-11-02 · #22（B seg 1GB vs 4GB）

动机：用户假设「把 segment 挂大就不会 NAH ⇒ 就是放不下」。在可复现的半占用(~50%) 风暴上做剂量对照。

### 方法

- 拓扑同 #17/#18：A/B TP2，Qwen3.8-27B hybrid，`create_copy_task` window=4 × 42 key
- 预填 B 至 ~50% 占用，再风暴（`probe_half_full.py`）
- **1GB**：A/B `global_segment_size=1GB`（B 合计 cap≈2.1GB）
- **4GB**：A 仍 1GB；B `4GB`×2（合计≈8.6GB；`gpu-memory-utilization=0.70`，因 4GB+缓冲会挤爆 0.9）
- 日志：`probe_segsize_1GB_t50_w4_n42.log` / `probe_segsize_4GB_t50_w4_n42.log`

### 结果

| B seg | 风暴前 B | 结果 | 失败当下 |
|-------|----------|------|----------|
| 2×1GB | ~47% / free≈1.1GB 量级 | **25 OK / 17 NAH** | NAH 时 **B free≈449MB（79%）** |
| 2×4GB | ~47% / free≈4.5GB 量级 | **42/42、0 NAH** | — |

### 判定

- **加大 B segment 后 NAH 消失** → 运营结论「segment 太小 / 有效可分配空间不够」**成立**（剂量响应清楚）。
- **不等于**「失败 = 字节容量已经见底」：1GB 轮 NAH 时仍有 ~449MB free（单对象 ~32MB），与 #18 一致。
- 更贴切：半占用的小 segment + 并发 `CopyStart→AllocateFrom` 时，**连续可分配洞 / 并发预留**扛不住；放大 cap 提高余量后压住。

参考实现：Mooncake `MasterService::CopyStart` → `AllocateFrom` → `OffsetBufferAllocator::allocate` → `NO_AVAILABLE_HANDLE (-200)`。

## 实验实录 · 2026-11-02 · #23（PR71 E14：READY → BlockStored）

动机：确认 E14「预热 READY 后补发引擎格式 `BlockStored`」在**真 Mooncake + 真 vLLM ZmqEventPublisher** 下可通，而不是假发布器单测。

### 环境

- 容器 `vllm-ascend-lake-test`（`quay.io/ascend/vllm-ascend:v0.26.0rc1`），主机 45=`7.242.104.45`
- master `:50088`；A=Qwen3.8-27B hybrid TP2 卡0/1 `:8001`，`--block-size 1536`，`PYTHONHASHSEED=0`
- 目标：**本机合成 seg**（卡 6/7，TransferEngine `rpc_port` 作 seg 名，各 2GiB）——不依赖 225
- 控制面：进程内 `TaskEngine` + `MooncakeBackend`（非 gRPC daemon）；物理卡 2 给控制面 ACL
- 复现脚本：[`tools/e14_mooncake_smoke.py`](tools/e14_mooncake_smoke.py)；pytest 入口 [`../tests/test_e14_npu_smoke.py`](../tests/test_e14_npu_smoke.py)（`@pytest.mark.npu`，**不进 Actions**；本地 `KVCM_NPU_SMOKE=1 pytest -m npu`）
- 无 NPU 单测：`kvcm/tests/test_kv_events.py`（`@pytest.mark.smoke`，CI / 本地都可跑）
- 环境变量见脚本头（`E14_HOST` / `E14_MASTER` / `E14_MODEL_PATH` 等）；缺省仍为本床 45 机口径

### 判定口径（避免空转 READY）

须同时满足：

1. warm **前** plan key `is_exist=0`；warm **后** `is_exist=全真`
2. 拷 **前** 目标合成 seg 上副本数 = 0
3. 实际调用 `create_copy_task` 次数 ≥ 1（本轮 = 2，TP2×1 满块×group0）
4. 拷 **后** 目标合成 seg 上副本数 = plan key 数
5. ZMQ SUB 收到 payload：`type=BlockStored`，`medium=CPU`，`kv_cache_spec_kind=full_attention`，满块 hash 数正确

### 结果

| 步骤 | 结果 |
|------|------|
| 假发布器单测（宿主机 Py3.9） | 逐条 14/14 通过；整文件一次跑受 3.9 事件循环干扰（非功能逻辑） |
| 容器真 ZMQ（FakeOps READY） | SUB 收到 `BlockStored` |
| 真 Mooncake + 合成目标（本轮） | **PASS**：`create_copy_task=2`，READY≈0.5s（2 key 同机），`on_synth=2/2`，ZMQ 收到 |

### 踩坑（写进结论，避免误判）

1. **仅看 READY 耗时不够**：首轮打向 225 已有 seg 时 ~1s READY，疑 `already_exist` 跳过传输；必须以「拷前/拷后位置 + `create_copy_task` 计数」为准。
2. **tokenizer 必须与引擎同口径**：`add_special_tokens=True` 或 decode 再 encode 会导致 key 与 warm 漂移，误撞池内旧 key。
3. **warm → 225**：打 A(:8001) warm 后，group0 副本落到 **225** 的 B seg——当时 **A 本机 seg 已接近满载**，池把新写入 spill 到 B，属放置行为，**不是补发/copy 代码错误**。本轮目标改用本机合成空 seg，只验证「强制 `create_copy_task` + BlockStored」，不依赖「副本应在 A」。
4. **未覆盖**：索引 SUB/改表、Router 选路、gRPC `SubmitIntent`、实例注册交齐 endpoint+kind、hybrid 多 group 全量拷贝。

### 结论

**E14 发件人侧（READY → 真端口补发 `BlockStored`）在真 Mooncake 路径上通过。** 选路闭环与注册面仍归后续。
代码锚点：`kvcm/backends/mooncake/backend.py`（`submit_interval_s` 注释与连发止血）、`kvcm/task_engine.py`（`max_concurrent` 传输闸）、Mooncake `client_service` 对 `NO_AVAILABLE_HANDLE` 有限重试（错误文案 `max retries reached: 10`）。

## 实验实录 · 2026-11-02 · #24（PR72 段容量准入）

动机：PR72 在 `create_copy_task` 前用 master `GET /query_segment` 的 **Capacity−Used（段剩余）** 与本批真副本大小比较；放不下则闸外等待，不提交。验证「真剩余 + 真 need」能否拦住拷贝。


### 环境

- 容器 `vllm-ascend-lake-test`，主机 45=`7.242.104.45`；master `:50088` / admin `:9003`
- A=Qwen3.8-27B hybrid TP2 卡0/1 `:8001`，`--block-size 1536`，`PYTHONHASHSEED=0`
- 目标：本机合成空段卡 6/7，各 **16MiB**（master 段大小下限；远小于单 block）
- 控制面：进程内 `TaskEngine` + `MooncakeBackend(admin=AdminClient)`（非 gRPC daemon）；卡 2 给控制面 ACL
- 复现：[`tools/admission_real_smoke.py`](tools/admission_real_smoke.py)；pytest 入口 [`../tests/test_admission_npu_smoke.py`](../tests/test_admission_npu_smoke.py)（`@pytest.mark.npu`，**不进 Actions**；本地 `KVCM_NPU_SMOKE=1 pytest -m npu`）
- 无 NPU 单测：`test_backend.py` 中 `test_admit_*`（`@pytest.mark.smoke`，CI / 本地都可跑）
- 环境变量见脚本头（`ADMIT_HOST` / `ADMIT_MASTER` / `ADMIT_MODEL_PATH` 等）；缺省仍为本床 45 机口径

### 判定口径

1. `/query_segment` 读出合成段 `used=0, capacity=16MiB, remaining=16MiB`
2. 新鲜前缀 warm 后 plan key 全在池；`batch_memory_replica_sizes` 给出真大小（本轮 rank0 目标段 need≈**50MiB**）
3. `need > remaining`
4. `run_once` 在预算内以 `PrefetchError: … stopped before submit` 结束
5. 全程 **`create_copy_task=0`**，合成段上副本仍为 0

### 结果

| 步骤 | 结果 |
|------|------|
| 容器内 admission 单测（Py3.12） | 17/17 PASS（宿主机 Py3.9 的 `TaskEngine`/`asyncio.Queue` 构造受事件循环限制，非逻辑回归） |
| 挂 16MiB 合成段 | `remaining=16777216` |
| warm 后真 need | `need_bytes=50331648`（> remaining） |
| `run_once` timeout_s=3 | 日志反复「准入等待：需要 50331648，剩余 16777216」→ **`stopped before submit`** |
| 提交与落位 | **`create_copy_task=0`**，`replicas on synth=0/2` |

### 未覆盖

- 段剩余变大后继续等待并成功 `create_copy_task`（本轮只验「塞不下则不提交」）
- 只查计划第一条目标段时，其它 rank 段满导致的漏放/误放
- gRPC `SubmitIntent` / daemon 装配路径

### 结论

**段容量准入在真 Mooncake 路径上按设计拦住了必败拷贝**：真 `/query_segment` 剩余 + 真副本大小 → 闸外等待 → 超时停在提交前，零 `create_copy_task`。与 #18/#22「NAH 不等于字节见底」不矛盾——本特性只挡「客户端已能看见 need>remaining」的明显塞不下，挡不住分配洞/并发预留类 NAH。
 (docs(kvcm): 登记 #24 段容量准入真测——need>remaining 不提交)
