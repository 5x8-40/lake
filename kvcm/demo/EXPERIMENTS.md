# va-precopy 实验总表与踩坑记录

> **demo 记录**：precopy 预热验证（kvcm 八项操作中仅 **copy** 一项）的实验史。生产化设计见 [../design/](../design/)；kvcm 全貌见 [../README.md](../README.md)。

2026-10-08 ~ 10-12 全量实验史（含已撤回证据），是 [README.md](README.md)「验证状态」的完整依据；复现操作见 [REPRODUCE.md](REPRODUCE.md)。

模型除注明外均为 `/data/models/Qwen3-VL-8B-w8a8c16`（GQA 8 KV head，block_size 128，`PYTHONHASHSEED=0`）；主机 245=7.242.105.245，217=7.242.105.217；容器镜像 `quay.io/ascend/vllm-ascend:v0.26.0rc1`；mooncake wheel `mooncake_transfer_engine_npu-0.3.11.post1`（除注明外）；master 在 245 宿主机 `:50088`。

| # | 时间 | 环境 | 实验 | 结果 | 证据 |
|---|------|------|------|------|------|
| 1 | 10/08 | 单机 245，TP=1/2 | B2 首验（旧 key×N 广播路径） | 通过，hit ≈95%+ | 10/08 会话记录（细节见 git 历史） |
| 2 | 10/08 | 245↔217 跨机，TP=1/2 | 跨机 B2 | 通过，hit ≈95%+ | 同上 |
| 3 | 10/08 | 跨机，TP=4，27B Mamba | 满块大模型跨机 | 通过 ≈92.1%（`--block-size 1536`） | 同上 |
| 4 | 10/08 | 单机 TP=2 | 对照：不开 A2 RoCE | **未通过**（`HcclBatchPut=4`）——跨卡 copy 必须开 A2 RoCE | 同上 |
| 5 | 10/09 午 | 单机 245：A=TP2(卡0,1)→B=TP4(卡2-5)，`prefill_tp_size=2 decode_tp_size=4` | 异构正向 e2e | 首跑死于 collect（上游 put 死代码）→ **补丁后通过**：24/24、**95.9%**、零 invalid、输出逐字一致 | 见「实验实录 · 单机正向」 |
| 6 | 10/09 午 | 单机 245：A=TP4(卡0-3)→B=TP2(卡4,5)，B `LOAD_ASYNC=1` | 异构反向 e2e | 通过：24/24、**95.9%**、零 invalid、输出一致；踩坑：`decode_tp_size` 对 kv_both 是 **peer** size | 见「实验实录 · 反向」 |
| 7 | 10/09 | 容器内 `vllm_ascend` 源码 | 上游 bug 修复实验：`pool_worker.py::_start_kv_transfer_threads` 构造 `KVCacheStore{Sending,Recving}Thread` 漏传 `worker=self` → `kv_transfer.py` tp_mismatch put/get 分支双双死代码 | `patch_tp_mismatch_worker.py` 幂等补丁，put 通路打通（#5 的 24/24 即补丁效果）；get 通路靠 #6 异步路径 | `patch_tp_mismatch_worker.py`、`config_data.py::infer_tp_mismatch_info` |
| 8 | 10/09 晚 | 跨机 245→217：A=TP2(卡0,1)→B=TP4(卡0-3) | 异构跨机 e2e（三轮 precopy 验证稳定性） | **通过**：24/24 跨机 sub-key put、24/24 `replica_copy_success`、副本对号落 217 四 seg、B 首中 **95.9%** 零 invalid、输出逐字一致 | 见「实验实录 · 跨机」 |
| 9 | 10/09 晚 | 245 宿主机 RoCE 网卡打点（`ip -s link enp67s0f5` 前后差值） | **读本机副本判定**（单次 B 命中 16 token） | ~~TX **+0.47 MB** / RX +0.13 MB；若读远端应 ~44 MB ⇒ get 读的是 217 本机 seg 副本~~ **10/09 深夜撤回：阳性对照实测 netdev 计数器不统计昇腾 RoCE RDMA 流量（24 keys ~43 MB 真实跨机传输，两侧计数器增量均 <2 MB），本行证据方法论作废**。读本机副本改由 #12 杀源实例法确证。注：worker 日志里的 `Connected to segment: 245` 是 precopy copy-task 收尾连接（与 get 同秒重叠），非读取证据 | 本次会话打点记录；`/tmp/nic_before.txt`/`after.txt`（245） |
| 10 | 10/09 | B(217) 重复请求确定性观察 | 同 prompt 反复打 B（16/64 token × 多次）+ 冷 prompt 对照 | 旧非 DEBUG 实例：**奇数次输出与 A 一致、偶数次近平局 token 处 1-token 翻转**（"warmup. va" → "warmup.  va"）；A 3×64 token 稳定；B 冷 prompt 3× 稳定；0.3.11+DEBUG 新实例 3 连跑未复现。疑外部加载路径交替的数值扰动，非字节错乱（纯外部读首请求与 A 逐字一致） | 本会话输出记录；根因待复现 |
| 11 | 10/09 晚 | 217 容器 wheel 升级 `0.3.13.post1` | 尝试升级 mooncake（上游 1fc27b6 已有 `SelectCompleteMemoryReplica` 本机优先） | **失败**：mount 4 seg 后 EngineCore 挂起 15+ 分钟（APIServer `Waiting for 1 local core engine proc`），疑与旧 master 兼容性；回退 `0.3.11.post1` 后恢复正常（#8/#9 即回退后所测） | master 日志、`worker_B.log`（17:03-17:18） |
| 12 | 10/09 深夜 | 跨机 245→217 异构重验（全新 prompt ×2：961/801 tok）+ **杀源实例终判** | **通过** + **杀源实例法确证读本机副本**（仅剩 217 副本时 B 首中全部外部 get 成功、零 invalid、输出与 A 逐字一致）。两认知：判定须用**全新 prompt 首中**（第二次被内部 HBM cache 接住，外部 get=0）；netdev 不计 RDMA（#9 撤回依据） | 见「实验实录 · 跨机」；判定方法 [REPRODUCE.md](REPRODUCE.md) §3 |
| 13 | 10/12 上午 | 跨机 245→217 异构，**bc6b5be 单入口控制面 + 两端 #15835 完整补丁** | 正向回归（升级后首测）：warm A → 单入口 precopy（32 keys=8 满块×4 eff）READY → B 首中 external **88.2%**（滚动累计口径，非单请求）、零 invalid、16-tok ×6 + 64-tok ×2 **8/8 逐字一致**（1-token 翻转未复现）。B 机容器无 `ss`：用 admin `:9003/get_all_segments` + 显式 `--targets` | 本次会话记录；见「实验实录 · 10-12 回归 · 正向」 |
| 14 | 10/12 下午 | 跨机 **反向 217(TP4)→245(TP2)，A 同步 load（无 LOAD_ASYNC）** | **通过**：36/36 key 对号入 A 两 seg（18+18）；每 rank `get keys=18`（9 块×2 sub-key 拼回）、`load_async=False` 证 hunk3 同步分发生效；8/8 token 与 B(TP4) 参考逐字一致；external hit 74.6→79.7%、零 invalid。踩坑：首跑 A 侧 peer 误配成 2（=自身 TP）→ mismatch 静默禁用 → `-800`（指纹 `Failed to get 9 keys`=每块 1 key）；改回 `PEER=4` 即通——同步 load mismatch 由此闭环 | 见「实验实录 · 10-12 回归 · 反向（同步 load）」 |
| 15 | 10/20 | 跨机正向 keys（#13 的 32 keys）不动，**kill 写入者 A 全进程树 → 等 client TTL 过期 → 复验** | **「副本随属主消失」不成立（纠正踩坑 3 早期误判）**：基线 32/32 → kill（17:06:51）→ 17:08:57 master `client_expired` + unmount A 两 seg（**零驱逐零 remove**）→ `batch_is_exist` **32/32**、副本全在 B 四 seg → 裸 client 跨机 `get` **3/3 字节级读回**（各 4.5MB）→ A 复活后终态仍 32/32。结论：副本存亡绑定 seg 所在 client，key 的 client_id 下线只清其 seg 内副本，远端副本保留 | master 日志切片（本机 `va-precopy/logs/`，不入仓）；见踩坑 3 |

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
- 结果：B 首中外部 get ×4 rank 成功（复命中走 vLLM 内部 HBM cache，预期）、external hit **88.2%**（滚动累计口径：实例启动以来所有请求，非本请求单值；故低于 95.9% 单请求口径）、零 invalid；16-tok ×6 + 64-tok ×2 **8/8 与 A 逐字一致，1-token 翻转未复现**（与 #10「新 DEBUG 实例未复现」一致）。
- B 机探测路径：容器无 `ss`、pidfile 亦 stale——改用 master admin API `GET :9003/get_all_segments`（TTL 已自动注销旧 seg，名单干净）+ 显式 `--targets`。

### 反向：217(TP4)→245(TP2)，同步 load（总表 #14）

- 首跑失败（配置坑）：把 A env 按方向误翻成 `PREFILL_TP_SIZE=4 DECODE_TP_SIZE=2` → kv_both 读 peer=`decode_tp_size`=2=自身 → mismatch **静默禁用** → plain get 按本地 4-head 尺寸读 2-head eff key → `mooncake -800`，日志形状 `Failed to get 9 keys out of 9`（=9 块×1 key/rank，即「每块 1 key」指纹；正确 mismatch 应为每 rank 18 keys）→ 500。**#6 的坑 1 在新单入口下复踩**，规则固化为：两台同配（README「env 配置」+ `start_worker.sh` 整除校验）。
- 修正后：A 重启（无 `LOAD_ASYNC`）；B 不动（plain put 名字与字节恒等于 eff key）→ warm B 全新前缀（`"...reverse sync prompt. "×80` = 1201 tok，9 满块）→ 单入口 `precopy.py --role A --tp-size 2 --peer-tp-size 4 --targets <A 两 seg>`（36/36 exist；反向自动展开 eff i → targets[i//2]；rank↔seg 经 `ss` 端口→pid + `npu-smi` 卡→pid 对号）→ 打 A。
- 结果：**每 rank `MooncakeBackend.get enter keys=18`**（TP0 读 @0…、TP1 读 @3…，9 块 × 2 sub-key 拼回本地）、`prepare get ... load_async=False` ×2 rank（hunk3 同步分发生效）、external hit 74.6→79.7%、零 invalid、16×6 + 64×2 **8/8 与 B(TP4) 参考逐字一致** → **`_load_kv_tp_mismatch` 拼回 + 同步分发两条空白路径首次真机闭环**。

### 10-12 踩坑（新）

1. **PYTHONHASHSEED 未设 → 控制面 key 全体漂移**：vllm `kv_cache_utils.init_none_hash`（rc1 容器版 `kv_cache_utils.py:111-114`；main 已重构为 `resolve_none_hash_seed`）在 seed 未设时 `NONE_HASH = os.urandom(32)` 折入**每个** block hash，且 sha256 不在 `_CBOR_HASH_FUNCTIONS` 警告名单——**静默**。实测同一 prompt 两次算出 `dc9fdff8…`/`948bbe7a…` 两个 key 集，`batch_is_exist` 0/N 假失败（引擎其实已 put）。引擎侧 `start_worker.sh` 已带 seed；控制面（keys.py/precopy/手工探测脚本）此前没带。已修：`keys.py::_hash_prompt` fail loud；顺带勘误 README 前置表「默认 sha256 不需要」的说法。
2. **孤儿 EngineCore 占卡**：重启 A 时只 kill 了 api_server（按 pid 段过滤漏掉 EngineCore/Worker——其 pid 与 api_server 相差悬殊），HBM 被孤儿占住，新实例 worker 起不来；segs 还是旧实例的。清理须按 `npu-smi`（卡→pid）+ `ss`（端口→pid）对号出的实际 pid 杀。
3. **「副本随属主消失」是误判，级联删除不存在**：10-12 观察到的 0/32 是踩坑 1 的假失败（控制面未带 PYTHONHASHSEED → key 名漂移 → `batch_is_exist` 假失败），并非 key 的 client_id 级联删除。10-20 受控复验：kill 写入者 A → client TTL 过期 unmount 其 seg（master 日志零驱逐零 remove）→ `batch_is_exist` 仍 32/32 且副本全在 B seg，跨机裸 client `get` 字节级读回 3/3。结论：**副本存亡绑定「副本所在的 seg 所在 client」，不绑定写入者；key 的 client_id 下线只清其自己 seg 上的副本，远端副本保留**（代码锚点：v0.3.11 `master_service.cpp` `CleanupStaleHandles` 的清理条件 = 副本所在 seg 是否失效；main `replica.h::isAffiliatedWith` 同义）。复验记录见总表 #15。
4. **ssh 限速下 expect 的正确姿势**：`timeout` 杀掉 expect 后孤儿 ssh 仍握住管道写端，`| tail` 等 EOF 会把整个外层命令拖挂（实测被拖 300s+）。姿势：输出重定向落文件 + expect timeout 分支显式 `close`。

### 10-12 对既有结论的影响

- 同步 load mismatch：**已闭环**（#14，A 同步 load 36/36）。
- key 一致性：**已闭环**（#13/#14 单入口 `batch_is_exist` 32/32、36/36 全中）。
- 多卡 rank↔seg 对号：**已闭环**（`--targets` + admin API/`npu-smi` 手工对号）。
- 仍待：`run_e2e.sh` 一键真机重跑（同构 TP=2 + 异构双向；10-12 回归走单入口手工分步，一键编排本身未跑）、1-token 翻转根因、MLA（明确不支持）；`tools/test_keys.py` 容器内单跑；resolve.py proctitle 自动匹配的多卡真机验证。
