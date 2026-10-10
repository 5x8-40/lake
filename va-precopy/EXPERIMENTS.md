# va-precopy 实验总表与踩坑记录

2026-10-08 ~ 10-09 全量实验史（含已撤回证据），是 [README.md](README.md)「验证状态」的完整依据；复现操作见 [REPRODUCE.md](REPRODUCE.md)。

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

**勘误（2026-10-10）**：#12 行「0.3.11 无显式本机优先」判断有误——0.3.11.post1 的 `SelectBestReplica` 已实现「prefer local MEMORY」（`real_client.cpp:283-286`）；0.3.12+ 新增的 `SelectCompleteMemoryReplica` 只用于 session API 路径。结论以 [REPRODUCE.md](REPRODUCE.md) §3 为准。另：#5/#7 行的 `patch_tp_mismatch_worker.py`（python 打补丁脚本，#15835 子集）已替换为 `patch_tp_mismatch_worker.patch`（标准 unified diff，#15835 完整版 backport，含同步 load 分发）。另：`precopy.py` 退出偶发 allocator abort 已定位——退出期 teardown 竞态（GC/atexit 乱序析构与在途收尾操作竞争；0.3.11.post1 缺上游 #3943 teardown drain，check_exists/collect/store_demo 均有 close、precopy 是唯一漏的），已修复为 READY 后显式 `store.close()`。另（2026-10-10 重构）：`resolve_segments.sh` 已重写为 master admin API（`:9003/get_all_segments`）+ pidfile/进程树 `ss` 对号，**不再解析任何日志**——下方「master 日志轮转」「旧 master 无 mount 行」两条踩坑随之失效（保留作历史记录）；同批 `keys.py`/`collect_prefix_keys.py` 改为 import 上游权威实现（vllm-ascend `PoolKey` / vllm `kv_cache_utils`），内置 key 镜像降为离线 fallback，`--check-master` 与 `test_keys.py::test_upstream_parity` 为格式漂移哨兵；`precopy.py` 随后合并为单入口（warm prompt → 进程内算 key → `batch_is_exist` 核对 → copy → READY，key 全程内存），`prefix_keys.txt` 降为 `--dump-keys` 调试留档，`collect_prefix_keys.py` 保留为库 + 独立 CLI。另（2026-10-10 目录重组）：产品控制面 py 归 `precopy/`（`collect_prefix_keys.py` 更名 `precopy/collect.py`）、集群脚本归 `cluster/`、调试工具归 `tools/`，`run_e2e.sh` 留根；本文及 README/REPRODUCE 历史章节中的裸文件名按此映射。

**踩坑记录（跨机操作）**：
- 217 容器残留 10/8 旧 worker：vLLM `setproctitle` 后进程名为 `VLLM::EngineCore/Worker_TP/APIServer`，`ps | grep python|vllm` **大小写躲过**；占 8002 端口与卡 0-3 显存，新 B 假 READY（`/v1/models` 由旧实例应答）。清理用 `pgrep -f "VLLM::[W]"`（括号防 pgrep 自匹配）+ `npu-smi info -t usages -i <id>` 验 HBM 释放。
- master 日志被轮转后 fd 仍在改名文件（`.bak.27b`）：`ln -sf mooncake_master.log.bak.27b logs/mooncake_master.log` 修复 `resolve_segments.sh`。
- `precopy.py` 客户端退出偶发 allocator abort（core dumped, RC=134）/挂起：**拷贝工作已完成**（以源侧 `replica_copy_success` 计数为准），可忽略。
- 非 DEBUG 启动无 `MooncakeBackend.get enter keys=` 分 rank 证据：需要 `VLLM_LOGGING_LEVEL=DEBUG`。
- `worker_A.log` 被 10/09 深夜两次重启覆盖（`nohup >` 截断），早期 72 次 `replica_copy_success` 计数以本表文字为准；重启 A 后需重 warm 再 collect。
- 跨机非交互 SSH：本机无 sshpass/密钥，`expect` + 密码可用；高频连接触发对端限速（认证后断连/KEX 卡死），需静置恢复或改控制台人工执行。
