# 2026-09-18 E1 环境核实:镜像与挂载

> 环境事实记录。安装与启动的定稿流程见 [e-line.md](e-line.md)。

## 目标环境

- 硬件:Atlas A2(`/dev/davinci[0-7]`)
- 系统:openEuler;CPU:Kunpeng 920(**aarch64**)

## 镜像

- 拉取:`quay.io/ascend/vllm-ascend:v0.26.0rc1`(**Ubuntu 变体**;openEuler 变体 `v0.26.0rc1-openeuler` 容器内 `npu-smi info` 卡死,不要用)。国内加速:registry 前缀换 `m.daocloud.io/quay.io` 或 `quay.nju.edu.cn`,tag 原样保留。
- 镜像内容(v0.26.0rc1,2026-09-03 发布,对齐上游 vLLM 0.26.0):
  - CANN 9.1.0;PyTorch 2.10.0 / torch_npu 2.10.0.post4;Python ≥3.10 <3.13;Triton Ascend 3.2.2
  - **Mooncake 0.3.11.post1 已在镜像内**(K 线传输/存储的依赖预置)
  - vllm 与 vllm-ascend 代码在 `/vllm-workspace`,以开发模式安装——改代码即时生效,不用重装
- **模型受限版**:官方完整验证仅 Kimi K3 / GLM-5.2 / DeepSeek V4 系列;实测 Qwen3.8-27B 亦可(2026-09-18,DP2×TP4)。

## 容器启动挂载(A2,官方文档)

设备:`/dev/davinci[0-7]`、`/dev/davinci_manager`、`/dev/devmm_svm`、`/dev/hisi_hdc`;卷:`/usr/local/dcmi`、`/usr/local/bin/npu-smi`、`/usr/local/Ascend/driver/lib64`、`/usr/local/Ascend/driver/version.info`、`/etc/ascend_install.info`、`/root/.cache`(模型权重);另挂 `hccn_tool`。

启动用 [scripts/ascend/start_va_dynamo.sh](scripts/ascend/start_va_dynamo.sh)(常驻容器,8 卡全挂 + `--net=host` + `-v /data:/data` + 华为 PyPI 源注入)。`--shm-size=1g` 对多卡 TP 偏小,起多卡 worker 若报 shm 相关错误,调大(如 16g)。

容器内 apt 源(国内实测好用,jammy):

```
deb http://mirrors.tools.huawei.com/ubuntu/ jammy main restricted universe multiverse
deb http://mirrors.tools.huawei.com/ubuntu/ jammy-updates main restricted universe multiverse
deb http://mirrors.tools.huawei.com/ubuntu/ jammy-backports main restricted universe multiverse
deb http://mirrors.tools.huawei.com/ubuntu/ jammy-security main restricted universe multiverse
```

etcd 用 [scripts/ascend/start_etcd.sh](scripts/ascend/start_etcd.sh) 起 Docker 常驻单节点(跨机把 `ETCD_ENDPOINTS` 指到宿主 IP)。
