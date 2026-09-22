# 00-plan: Dynamo-Ascend → lake 文档/脚本 PR 序列

对齐标准（与 lake#1 相同）：
1. 产物只进 `docs/dynamo-ascend/` + `scripts/dynamo-ascend/`
2. 单仓源码 + **容器内安装**（无 host `.pth`）
3. **验证通过后再开 PR**
4. 代码基线：**`release/1.4.2` + 最小协议补丁**（不切 dynamo-ascend 1.5 整树）

| Lake PR | 内容 | 验证门槛 |
|---------|------|----------|
| #1 | 容器内编装 1.4.2 + 聚合 FE/worker | ✅ models + chat |
| #2 | PD Mooncake：`apply_protocol_patch` + `start_pd` + docs | ✅ models + chat（1.4.2+协议补丁） |
| #3 | KV router：`start_pd` 默认 `--router-mode kv` + kv-events | ✅ FE/worker metrics 可见 |
| #4 | multi-host：`start_pd_multi` ROLE=p\|d | ✅ 跨机 1P1D chat（P=106.153 D=108.153） |

协议补丁：[`../scripts/dynamo-ascend/patches/0001-mooncake-connector-v1-protocol.patch`](../../scripts/dynamo-ascend/patches/0001-mooncake-connector-v1-protocol.patch)。说明见 [pd-mooncake.md](pd-mooncake.md)。
