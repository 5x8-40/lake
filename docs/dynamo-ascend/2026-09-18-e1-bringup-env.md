# 2026-09-18 E1 环境核实:镜像、版本对齐与安装方式

> E 线开工前的环境核实记录。结论均经官方文档 / PyPI / 上游源码核实,来源见末节。

## 目标环境

- 硬件:Atlas A2(`/dev/davinci[0-7]`)
- 系统:openEuler;CPU:Kunpeng 920(**aarch64**)

## 镜像

- 拉取:`quay.io/ascend/vllm-ascend:v0.26.0rc1`(**Ubuntu 变体,2026-09-18 实测可用**)。openEuler 变体(`v0.26.0rc1-openeuler`)在本机容器内 `npu-smi info` 卡住、Python import 报 "can't start new thread",判定与本机驱动用户态不兼容,**弃用**;宿主仍是 openEuler,容器 userland 换 Ubuntu 无冲突。国内加速:registry 前缀换 `m.daocloud.io/quay.io` 或 `quay.nju.edu.cn`,tag 原样保留。
- 镜像内容(v0.26.0rc1,2026-09-03 发布,对齐上游 vLLM 0.26.0):
  - CANN 9.1.0;PyTorch 2.10.0 / torch_npu 2.10.0.post4;Python ≥3.10 <3.13;Triton Ascend 3.2.2
  - **Mooncake 0.3.11.post1 已在镜像内**(K 线传输/存储的依赖预置)
  - vllm 与 vllm-ascend 代码在 `/vllm-workspace`,以 `pip install -e` 开发模式安装——改代码即时生效,不用重装
- **模型受限版**:官方完整验证仅 Kimi K3 / GLM-5.2 / DeepSeek V4 Flash 0731 / DeepSeek V4 Pro 0813。bring-up 阶段直接用已验证模型,不自加变量。
- 已知问题:triton-ascend 需 ≥3.2.0.dev20260322(官方镜像已含;手动装环境时才需要注意)。

## Dynamo 安装:版本对齐是关键

- **陷阱**:Dynamo main(ai-dynamo 1.5.0)的 `[vllm]` extra pin `vllm[flashinfer,runai,otel]==0.28.0`。在镜像里直接 `pip install ai-dynamo[vllm]` 会把 vLLM 0.26 顶掉,vllm-ascend 失效。
- **对齐版本**:1.4.x 全系列(1.4.0 / 1.4.1 / 1.4.2,PyPI 已逐一核实)的 [vllm] extra 都 pin `vllm==0.26.0`,与镜像一致。**用最新 patch:ai-dynamo==1.4.2**(2026-08-28 发布)。上游 pin 演进:0.26.0(2026-07-29,#12202)→ 0.27.1(08-18,#13059)→ 0.28.0(08-31,#13846)——1.4.2 是 0.26 对齐窗口内的最后一个 patch。
- **安装方式**:`pip install ai-dynamo==1.4.2`(**不带** [vllm] extra,用镜像自带 vLLM)。Rust 组件(frontend/router 等)在 `ai-dynamo-runtime` wheel 里,1.4.2 有 aarch64 wheel(`cp310-abi3-manylinux_2_28_aarch64`,一个 wheel 覆盖 Python 3.10–3.12),Kunpeng 920 可直接装,不需要 Rust 工具链;glibc ≥2.28(openEuler 满足)。
- **base 依赖零 CUDA**(2026-09-18 PyPI 核实):base 包仅 9 项依赖——`ai-dynamo-runtime` + aiohttp / kubernetes / msgspec / prometheus-client / pyzmq / transformers≥4.56 / typing-extensions / zstandard,无 torch/vllm/CUDA;CUDA 依赖(nixl[cu13]、vllm[flashinfer]、cupy-cuda12x、tensorrt-llm)全在 extra 里,不带 extra 不引入。**NPU 环境 pip 直接装 base 包是安全的**。
- **不源码装、不用 venv**:源码装需 Rust 工具链 + maturin 编 runtime wheel,bring-up 无收益(E1.3 结论:大概率零代码改动),待真要改 dynamo 代码再切;**禁用 uv/venv 隔离环境**——`dynamo.vllm` 必须 import 镜像内的 vllm/vllm-ascend/torch_npu,要装进镜像的系统 Python 环境。装后检查:`pip list | grep -Ei "torch|vllm|ai-dynamo"` 确认镜像原有包未被动。
- **fork 基线**:`5x8-40/dynamo-ascend` 的 `ascend-dev` 目前跟踪 main(1.5.0 / vllm 0.28)。bring-up 阶段应从上游 **v1.4.2** 切基线,与 vllm-ascend v0.26.0rc1 锁步;待 vllm-ascend 发布 0.28 对齐版(nightly 已到 0.27.1rc)再整体升。
- **为什么不能用最新 main**:main(1.5.0)要 `vllm==0.28.0`,而 vllm-ascend 版本号跟随 vLLM、最新正式版只有 v0.26.0rc1(对齐 0.26.0)——用 main 就没有可用的昇腾引擎。1.4.2 + vLLM 0.26 + vllm-ascend v0.26.0rc1 是对齐窗口内的最新组合。
- **KVBM sunset 与本线无关**:KVBM 已日落(DEP #11673),继任者 KVCR 是**独立仓独立包**(`nvidia-kvcr`,framework-neutral,不依赖 vllm/ai-dynamo),dynamo 侧对接面是 router hint(2026-08-07 进 main,#11695,**1.4.2 已含**)。KVCR 管 KV 卸载/P2P(K 线范畴);E 线聚合模式 bring-up 既不需要 KVBM 也不需要 KVCR。

## E1.3 摸底对象:胶水层的版本/硬件敏感点(初步清单)

`dynamo.vllm` 对 vLLM 的 import 面(上游 1.4.0 树)里,以下属版本敏感或 CUDA 专属,需逐个对照 vLLM 0.26 + vllm-ascend 核实:

- `vllm.config.CUDAGraphMode`——名字即 CUDA;vllm-ascend 的等价物/是否被绕过
- `vllm.v1.*` 内部接口:`AsyncScheduler`、`SchedulerOutput` / `CachedRequestData` / `NewRequestData`、`kv_cache_utils.get_request_block_hasher` / `init_none_hash`、`single_type_kv_cache_manager.CrossAttentionManager`
- `vllm.distributed.kv_events`(KVEventsConfig / ZmqEventPublisher)——KV 事件发布,Router 视图的来源
- `[vllm]` extra 的 `nixl[cu13]==1.3.2` 是 CUDA 版 NIXL,昇腾不能这么装——K1 时改源码编译 + Mooncake TE 后端
- flashinfer extra 同理为 CUDA 生态,昇腾不需要

## 容器启动挂载(A2,官方文档)

设备:`/dev/davinci[0-7]`、`/dev/davinci_manager`、`/dev/devmm_svm`、`/dev/hisi_hdc`;卷:`/usr/local/dcmi`、`/usr/local/bin/npu-smi`、`/usr/local/Ascend/driver/lib64`、`/usr/local/Ascend/driver/version.info`、`/etc/ascend_install.info`、`/root/.cache`(模型权重);另挂 `hccn_tool`。建议 `--net=host`、`--shm-size` 调大。

### 实测可用的启动命令(2026-09-18,8 卡全挂)

```bash
export IMAGE=quay.io/ascend/vllm-ascend:v0.26.0rc1
docker run --rm \
  --name vllm-ascend \
  --shm-size=1g \
  --device /dev/davinci0 \
  --device /dev/davinci1 \
  --device /dev/davinci2 \
  --device /dev/davinci3 \
  --device /dev/davinci4 \
  --device /dev/davinci5 \
  --device /dev/davinci6 \
  --device /dev/davinci7 \
  --device /dev/davinci_manager \
  --device /dev/devmm_svm \
  --device /dev/hisi_hdc \
  -v /usr/local/dcmi:/usr/local/dcmi \
  -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
  -v /usr/local/Ascend/driver/lib64/:/usr/local/Ascend/driver/lib64/ \
  -v /usr/local/Ascend/driver/version.info:/usr/local/Ascend/driver/version.info \
  -v /etc/ascend_install.info:/etc/ascend_install.info \
  -v /root/.cache:/root/.cache \
  -p 8000:8000 \
  -it $IMAGE bash
# 容器内(Ubuntu):apt-get update && apt-get install -y curl
```

容器内 apt 源(国内实测好用,jammy):

```
deb http://mirrors.tools.huawei.com/ubuntu/ jammy main restricted universe multiverse
deb http://mirrors.tools.huawei.com/ubuntu/ jammy-updates main restricted universe multiverse
deb http://mirrors.tools.huawei.com/ubuntu/ jammy-backports main restricted universe multiverse
deb http://mirrors.tools.huawei.com/ubuntu/ jammy-security main restricted universe multiverse
```

注意:jammy 起 Ubuntu 源已无 `etcd-server` 包,etcd 用静态二进制装(华为云镜像 `mirrors.huaweicloud.com/etcd`,命令见 e-line 执行清单 E1.4)。

注意:8 卡全挂(davinci0–7),2026-09-18 实测 npu-smi 正常。`--shm-size=1g` 对多卡 TP 偏小,起多卡 worker 若报 shm 相关错误,重起容器调大(如 16g)。

## 来源

- vllm-ascend v0.26.0rc1 release notes(github.com/vllm-project/vllm-ascend/releases)
- vllm-ascend 安装文档(docs.vllm.ai/projects/ascend → installation / quick_start)
- PyPI `ai-dynamo` 1.4.0 / `ai-dynamo-runtime` wheel 列表;NVIDIA pypi.nvidia.com
- 上游 dynamo 仓 `pyproject.toml` 及 pin 演进提交(#12202 / #13059 / #13846)
