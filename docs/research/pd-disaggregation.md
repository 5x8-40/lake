# vLLM / SGLang — PD 分离的控制机制对比

> 源码:`3rdparty/vllm`、`3rdparty/sglang`(submodule)。本文聚焦两家**如何控制 prefill/decode 分离**——角色划分、请求编排、配对握手、KV 传输推进、失败处理,供 lake 的 PD 分离(三执行模式之一,见 [`../architecture/execution-modes.md`](../architecture/execution-modes.md))设计参考。
>
> 一句话结论:**vLLM = 通用 KV connector 插件 + 外部 proxy 编排;SGLang = 启动即固定角色 + 内建队列状态机**。两者的 KV 都归引擎/实例私有,PD 传输是"实例间搬运",且部署形态固定(非逐请求选模式)。

## 一、vLLM:KVConnector 插件 + 外部 proxy

vLLM 引擎本身**不内建 PD 编排**,而是把"KV 怎么在 P/D 间搬"抽象成 **connector 插件接口** `KVConnectorBase_V1`;"谁先 P 后 D"由**外部 proxy**串联。

### 双角色(`KVConnectorRole`)

| 角色 | 职责 |
|------|------|
| `SCHEDULER` 侧 | 请求级:查外部/远端 KV 命中、决定是否等待外部 KV 加载、产 connector metadata |
| `WORKER` 侧 | 执行级:把外部 KV 逐层 load 进本机 paged buffer、把产出 KV 逐层 save 回外部 |

- **SCHEDULER 侧钩子**:`get_num_new_matched_tokens`(查外部 KV 命中数、决定要不要等外部加载)→ `update_state_after_alloc`(分配后更新状态)→ `build_connector_meta`(产 worker 要用的传输元数据)→ `request_finished`(请求结束时调一次,返回**是否还有异步 save 未完成**,未完成则 block 延迟释放)。
- **WORKER 侧钩子**:`start_load_kv`(启动异步 load)/ `wait_for_layer_load`(等某层 load 完)+ `save_kv_layer`(逐层 save)/ `wait_for_save`(等全部 save 完)+ `get_finished`(返回已完成 load/save 的请求 id)。→ **layer-wise 异步流水线**:前向逐层 save KV、加载逐层 load KV,与计算重叠。

### 控制流

外部 proxy(`examples/disaggregated/disaggregated_serving/disagg_proxy_demo.py`)先把请求发 P 实例(配 `kv_producer`)触发 KV 产出,再发 D 实例(`kv_consumer`)续 decode;角色由 `kv_transfer_config` 配置。P/D 间握手用 `KVConnectorHandshakeMetadata`,传输元数据用 `KVConnectorMetadata`。

传输后端多样,均实现为 connector:**NIXL / Mooncake / P2pNccl / LMCache / SharedStorage / FlexKV** 等(`vllm/distributed/kv_transfer/kv_connector/v1/` 下各子目录)。FlexKV 本体见 [`flexkv/`](flexkv/)。

### 特点

- **抽象层次高**:PD 只是 connector 的一种用法(disaggregated prefill);connector 本身也服务于 offloading、跨实例复用等。引擎通用、传输可插拔。
- **编排在引擎外**:vLLM 不决定"哪些请求走 PD",proxy 决定;引擎只在 scheduler/worker 双侧执行 connector 协议。
- 前缀复用跨实例靠 `ExternalBlockHash`(`maybe_convert_block_hash`):把内部 block hash 转成跨实例可交换的形式,disaggregated worker 间不重算即可复用。

## 二、SGLang:固定角色 + 内建队列状态机

SGLang 用 `--disaggregation-mode prefill|decode` **启动即固定角色**,P/D 是两组独立 server;请求生命周期是**内建的多段队列状态机**(prefill.py / decode.py 顶部 docstring 即权威描述)。

### Prefill 侧生命周期(`prefill.py`)

1. **Bootstrap Queue**(`PrefillBootstrapQueue`):每请求建 `disagg_kv_sender`,做握手 + 预分配,poll bootstrap 状态;完成移入 Waiting。
2. **Waiting Queue**:`PrefillAdder` pop → 跑 prefill forward → 加入 Inflight。
3. **Inflight Queue**:非阻塞 poll sender,传输完成即返回释放。

### Decode 侧生命周期(`decode.py`)

1. **PreallocQueue**(`DecodePreallocQueue`):每请求建 receiver,先握手、**有空闲 KV 即预分配**,移入 Transfer。
2. **TransferQueue**(`DecodeTransferQueue`):poll receiver 传输态,完成移入 Waiting。
3. **WaitingQueue**:构造 **`PrebuiltExtendBatch`**——**跳过 prefill forward,只填 metadata**(KV 已由传输灌入)。
4. **RunningBatch**:把 resolved 的 PrebuiltExtendBatch 并入 running batch 跑 decode。

### 配对、握手与一致推进

- **`bootstrap_room`**:每请求一个配对 ID,P 的 sender 与 D 的 receiver 靠它配对。
- **bootstrap server 只在 prefill 侧起**(`disagg_service.py::start_disagg_service`:`only start bootstrap server on prefill tm`);D 侧 receiver 向 P 的 bootstrap server 注册握手。
- **`KVPoll` 状态机**:Bootstrapping / WaitingForInput / Transferring / Success / Failed。
- **rank 间一致推进**:`poll_and_all_reduce_attn_cp_tp_group` 对 TP/CP group 内所有 rank 的 poll 状态做 all-reduce——保证分布式下传输推进一致(防某 rank 单独卡住 → collective 盲等)。
- **`SchedulerDisaggregationPrefillMixin` / `SchedulerDisaggregationDecodeMixin`**:把上面队列逻辑挂进 scheduler event loop。

### 特点

- **专门子系统**:`disaggregation/` 是独立模块,角色/队列/传输后端(mooncake/nixl/mori/ascend)成体系。
- **decode 跳过 prefill forward**:PrebuiltExtendBatch 只填 metadata,省掉 D 侧重算。
- **draft KV 也传输**:投机时连 draft model KV 一起跨 P/D 搬(`prefill.py` `transfer_draft_cache` / `draft_token_to_kv_pool`,L164-216)——即 drafter KV 被当作一等 KV 跨 P/D 搬运。
- **LB/路由在外部**:`sgl-router`(此版本 python 内已无 mini_lb)做 P/D 配对与路由。

## 三、对比表

| 维度 | vLLM | SGLang |
|------|------|--------|
| 角色划分 | connector role(SCHEDULER/WORKER),引擎通用 | 启动固定 `--disaggregation-mode prefill/decode` |
| 编排 | **外部 proxy** 串 P→D | **内建队列状态机**(P 三段 / D 四段) |
| 配对握手 | `KVConnectorHandshakeMetadata` | `bootstrap_room` + P 侧 bootstrap server |
| 传输推进 | layer-wise save/load 异步流水 | `KVPoll` + rank all-reduce 一致推进 |
| 抽象层次 | 通用 KV 传输插件(PD 是其一用法) | 专门 disaggregation 子系统 |
| 后端 | NIXL/Mooncake/P2pNccl/LMCache/FlexKV/… | Mooncake/NIXL/Mori/ascend |
| 前缀复用键 | `ExternalBlockHash`(跨实例外部哈希) | radix + bootstrap |
| D 侧省算 | connector 加载 KV,scheduler 决定跳过已命中 | PrebuiltExtendBatch 跳过 prefill forward |
| draft KV | connector 决定(有 hidden states connector 变体) | `transfer_draft_cache` 显式随传 |

**共性**:两者 KV 均**归引擎/实例私有**,PD 传输是**实例间搬运**(P 的 HBM → D 的 HBM);部署形态**固定**(SGLang 启动定角色,vLLM proxy 定串),无逐请求模式选择。

## 四、与 lake 的关系(关键差异,我们更彻底)

1. **KV 所有权**:两家 KV 归引擎私有、PD 传输是实例间搬运;lake **KV 归存储池权威**,P/D 的 HBM 都是池的物理载体,传输是**池 agent 发起的 L0↔L0**,引擎降到 publish/pull+fence、不知地址(见 [`../architecture/kv-cache-pool.md`](../architecture/kv-cache-pool.md) "跨实例 KV 传输"、[`../architecture/data-flow.md`](../architecture/data-flow.md))。vLLM connector ≈ lake 常驻的存储池 client,但 lake 是**必经路径 + 集群级权威**(非可选插件)。
2. **握手/一致性**:SGLang 靠 P 侧 bootstrap server 点对点握手 + KVPoll all-reduce;lake 用 **etcd 强一致位置视图 + in-flight ref**替代——一跳定位源、传输源端冻结(见 [`../architecture/consistency.md`](../architecture/consistency.md) §3),不需每请求 bootstrap 配对。vLLM 的 layer-wise save/load 流水线思路可直接借鉴到 lake 池 agent 的逐层 publish(但归池驱动、引擎无感)。
3. **固定 PD vs 混合执行**:两家都是**固定 PD 分离形态**;lake 的核心区分是**请求不固定走 P→D**——Router 逐请求按 `f(请求,集群状态)` 在 **PD 分离 / 混部 / D-direct** 间选(见 [`../architecture/execution-modes.md`](../architecture/execution-modes.md))。PD 分离只是三模式之一。
4. **失败处理**:SGLang 有 `KVPoll.Failed` + drafter skip 等特殊路径;lake **不设降级链**,执行失败 → F4 重跑模式选择纯函数(见 [`../architecture/consistency.md`](../architecture/consistency.md) §5)。
5. **D→P 反向流**:lake 原生支持 decode→prefill 反向喂(服务下一轮 agent prefill),两家 PD 均单向 P→D(见 [`dualpath.md`](dualpath.md))。
6. **draft KV 跨 P/D**:SGLang `transfer_draft_cache` 印证 drafter KV 需随 target KV 一起搬——对齐 lake"drafter KV 归池、随迁"(见 [`../architecture/compute-layer.md`](../architecture/compute-layer.md) "drafter cache 与 seed hidden states")。

## 代码索引

> 沿代码回溯用。符号名稳定锚定,行号会漂移——找不到时 `grep -n "符号名" 3rdparty/<repo>/<文件路径>`。

### vLLM

| 机制 | 文件:符号 |
|------|-----------|
| connector 接口基类 | `vllm/distributed/kv_transfer/kv_connector/v1/base.py`::`KVConnectorBase_V1`(L171) |
| 角色枚举(scheduler/worker) | `base.py`::`KVConnectorRole`(L124) |
| scheduler 侧:查命中/决定等待 | `base.py`::`get_num_new_matched_tokens`(L454)/ `update_state_after_alloc`(L489)/ `build_connector_meta`(L510)/ `request_finished`(L542) |
| worker 侧:逐层 load/save | `base.py`::`start_load_kv`(L293)/ `wait_for_layer_load`(L311)/ `save_kv_layer`(L325)/ `wait_for_save`(L347)/ `get_finished`(L357) |
| 握手/传输元数据 | `base.py`::`KVConnectorHandshakeMetadata`(L132)/ `KVConnectorMetadata`(L141) |
| 跨实例外部哈希 | `vllm/v1/core/...`::`ExternalBlockHash` / `maybe_convert_block_hash`(见 [`vllm/compute.md`](vllm/compute.md)) |
| 外部 proxy 编排 | `examples/disaggregated/disaggregated_serving/disagg_proxy_demo.py` |
| 后端 connector | `kv_connector/v1/{nixl,mooncake,lmcache_connector.py,flexkv_connector.py,…}` |

### SGLang

| 机制 | 文件:符号 |
|------|-----------|
| prefill 生命周期(docstring) | `python/sglang/srt/disaggregation/prefill.py`(L1-18) |
| prefill bootstrap 队列 | `prefill.py`::`PrefillBootstrapQueue`(L106)/ `create_sender`(L252) |
| prefill scheduler 挂钩 | `prefill.py`::`SchedulerDisaggregationPrefillMixin`(L422) |
| draft KV 随传 | `prefill.py`::`transfer_draft_cache`(L164)/ `draft_token_to_kv_pool`(L186,L216) |
| decode 生命周期(docstring) | `python/sglang/srt/disaggregation/decode.py`(L1-19) |
| decode 预分配/传输队列 | `decode.py`::`DecodePreallocQueue`(L283)/ `DecodeTransferQueue`(L1590) |
| decode scheduler 挂钩 | `decode.py`::`SchedulerDisaggregationDecodeMixin`(L1918) |
| bootstrap server(仅 P 侧起) | `python/sglang/srt/managers/disagg_service.py`::`start_disagg_service`(L14,`KVClassType.BOOTSTRAP_SERVER`) |
| 配对 ID / poll 状态机 | `prefill.py`/`decode.py`::`bootstrap_room` / `KVPoll` / `poll_and_all_reduce_attn_cp_tp_group` |
| 传输后端 | `disaggregation/{mooncake,nixl,mori,ascend}/` |
