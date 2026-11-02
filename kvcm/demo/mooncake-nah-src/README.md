# Mooncake NAH 分配诊断

目标：`OffsetBufferAllocator::allocate` / `CopyStart` 失败时打出
`request / used / capacity / sum_free / largest_free`，区分**碎片** vs **总和不够** vs **并发预留见底**。

本环境 wheel：`mooncake-transfer-engine-npu==0.3.11.post1`。

## 已就位

| 产物 | 路径 |
|------|------|
| 统一补丁 | `patches/0001-nah-alloc-diag.patch` |
| 一键 apply | `./apply_nah_patch.sh /path/to/Mooncake` |
| allocate 失败块（patched） | `mooncake-store/src/offset_allocate_fail.patched.cpp` |
| CopyStart 日志增强（from ref） | `mooncake-store/src/master_service.cpp.patched-from-ref` |
| gdb 探针（需 gdb） | `gdb_alloc_fail.gdb` |
| -v=1 日志看守 | `./watch_alloc_fail.sh` |

## 运行时状态（2026-11-02）

- **master 已用 `-v=1` 拉起**（`:50088` / `:9003` health ok）→ 现成 `VLOG(1) allocation_failed size=… segment=… current_size=…`
- **完整 clone + cmake 换 .so：受阻**（本机出网 GitHub / jsDelivr / yum repo 均超时）
- **gdb：未装上**（yum repodata 超时；容器 apt 无 gdb 包）
- **真机 NAH 风暴：卡被占**
  - 45 / 225 均有 `Qwen3.5-122B…` TP8 启动 + placeholder 占满 HBM
  - Ascend Mooncake 挂 seg 需要 NPU → 暂不能复现

## 树齐后换二进制

```bash
cd Mooncake && git checkout v0.3.11.post1
/data/w84156052-104-45/lake/kvcm/demo/mooncake-nah-src/apply_nah_patch.sh "$PWD"
# cmake/ninja 编 store → 替换容器内
#   .../site-packages/mooncake/libmooncake_store.so
#   .../site-packages/mooncake/mooncake_master
# 重启 master（建议继续带 -v=1）
```

失败日志（patched）形态：

```text
action=offset_alloc_fail, segment=…, request=…, used=…, capacity=…, sum_free=…, largest_free=…, frag=0|1
action=copy_start_alloc_fail, key=…, tgt_segment=…, request=…, err=…
```

`frag=1` ⇒ `largest_free < request ≤ sum_free`（碎片石锤）。

## 不等重编（卡空后立刻可跑）

```bash
# 终端1：看守 VLOG
./watch_alloc_fail.sh

# 终端2：拉 A/B seg 后打 copy 风暴（既有 probe_*）
# 对照：allocation_failed 的 current_size + metrics capacity → sum_free
# sum_free≥request 仍 fail → 大概率碎片/洞；再用补丁或 gdb 钉 largest_free
```

## 参考实现

- Mooncake `mooncake-store/src/allocator.cpp::OffsetBufferAllocator::allocate` + `getLargestFreeRegion`
- `master_service.cpp::MasterService::CopyStart` → `AllocateFrom` → `NO_AVAILABLE_HANDLE (-200)`
- 与 lake 差异：我们要的是**失败现场 largest_free**；上游只有 VLOG(1) 的 size/segment/used，无 frag 判定
