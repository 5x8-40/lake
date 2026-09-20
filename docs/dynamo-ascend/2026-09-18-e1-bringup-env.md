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

## Dynamo 安装:源码编译路线(2026-09-20 定稿)

- **定稿路线**:宿主机源码编译 fork `ascend-dev`(跟踪上游 main)+ `.pth` 注入容器 site-packages,完整步骤见 [native-bringup.md](native-bringup.md) §2。不用 venv——`dynamo.vllm` 必须 import 镜像内的 vllm/vllm-ascend/torch_npu,注入目标是容器系统 Python。
- **PyPI wheel 不可用**,两个原因:
  - aarch64 wheel(`ai-dynamo-runtime`)在部分鲲鹏主机 `import dynamo._core` 报 **Illegal instruction**(wheel target-cpu 基线不兼容),需 `.cargo` 配 `target-cpu=generic` 源码重编;
  - `ai-dynamo` 的 [vllm] extra pin `vllm==0.28.0`(main)且拉 CUDA 生态(nixl[cu13]、flashinfer),会顶掉镜像里的 vLLM 0.26——任何场景都**禁止**带 [vllm] extra 安装。
- **版本对齐的实质**:包依赖里的 vllm 版本 pin 只是解析约束,源码装与之无关;main 胶水在 agg 路径对 vllm 0.26 运行时兼容(2026-09-18 实测)。vllm-ascend 升级后需重验 import 面,方法见 [2026-09-18-e13-glue-survey.md](2026-09-18-e13-glue-survey.md)。
- **fork 基线**:`5x8-40/dynamo-ascend` 的 `ascend-dev`,跟踪上游 main。
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

### 容器启动(定稿:常驻脚本)

用 [scripts/ascend/start_docker_va.sh](scripts/ascend/start_docker_va.sh) 起**常驻**容器(`sleep infinity`;不要 `docker run --rm -it`,Ctrl+D 容器即销毁):8 卡全挂 + `--net=host` + `-v /data:/data` + hccn_tool + 华为 PyPI 源注入,设备与挂载清单即上节所列。`--shm-size=1g` 对多卡 TP 偏小,起多卡 worker 若报 shm 相关错误,调大(如 16g)。

容器内 apt 源(国内实测好用,jammy):

```
deb http://mirrors.tools.huawei.com/ubuntu/ jammy main restricted universe multiverse
deb http://mirrors.tools.huawei.com/ubuntu/ jammy-updates main restricted universe multiverse
deb http://mirrors.tools.huawei.com/ubuntu/ jammy-backports main restricted universe multiverse
deb http://mirrors.tools.huawei.com/ubuntu/ jammy-security main restricted universe multiverse
```

etcd 用 [scripts/ascend/start_etcd.sh](scripts/ascend/start_etcd.sh) 起 Docker 常驻单节点(`quay.io/coreos/etcd:v3.5.16`,`--net=host`;跨机把 `ETCD_ENDPOINTS` 指到宿主 IP)。jammy 起 Ubuntu 源已无 `etcd-server` 包;容器内静态二进制装法(华为云镜像 `mirrors.huaweicloud.com/etcd`)仅作备选。

## 来源

- vllm-ascend v0.26.0rc1 release notes(github.com/vllm-project/vllm-ascend/releases)
- vllm-ascend 安装文档(docs.vllm.ai/projects/ascend → installation / quick_start)
- PyPI `ai-dynamo` 1.4.0 / `ai-dynamo-runtime` wheel 列表;NVIDIA pypi.nvidia.com
- 上游 dynamo 仓 `pyproject.toml` 及 pin 演进提交(#12202 / #13059 / #13846)
