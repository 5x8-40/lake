## Summary

- 落地 D003 **B2** 验证脚手架 `va-precopy/`：Mooncake `create_copy_task` 在真实请求前把前缀 KV 复制到目标 worker 本机 DRAM segment（引擎不参与复制）。
- A2 RoCE 环境脚本、`preferred_segment: true`、自动 warm / collect keys / resolve segment / precopy / hit B。
- **实测通过**（`v0.26.0rc1` + `Qwen3-VL-8B-w8a8c16`）：
  - 同机 TP=1、同机 TP=2：External prefix hit ≈95%+
  - **跨机 TP=2**（245→217，共用 master `:50088`）：两段 B seg READY；B metrics `768/801` ≈ **95.9%**

## 关键配置

- `HCCL_INTRA_ROCE_ENABLE=1` + `HCCL_IF_IP` / socket ifname（缺则同机跨卡 `HcclBatchPut=4`）
- `preferred_segment: true`（否则同机写可能落到「假 B」segment，B2 场景失真）
- 同构 TP>1：`collect --tp-size` + `precopy --targets`（rank↔seg）；`resolve_segments.sh --target-ip --tp`
- 异构 TP：AscendStore v0.26 **无** store_tp/LCM（见 README「异构 TP」）

## Test plan

- [x] 同机 TP=1 e2e READY + ~95% external hit
- [x] 同机 TP=2 真 A→B（placement 仅 A）+ READY + ~95.9% hit
- [x] 跨机 TP=2（A@245 / B@217）READY + 768/801 hit
- [ ] （可选）RoCE + preferred_segment 下重跑 27B TP4
- [ ] DRY_RUN / store_demo 无 NPU 冒烟

## 未改

- 不改 `docs/dynamo-ascend/decisions/D003-kv-prefetch.md`（决策文保持原状；验证记在本目录 README）
