# kvcm-vs-oss 图源与许可

本目录供 [`../../kvcm-vs-oss.md`](../../kvcm-vs-oss.md) 使用。每张图的来源、许可与版本：

| 文件 | 类型 | 来源 | 许可 | 快照 / 版本 |
|---|---|---|---|---|
| `fig-ownership.svg` | 自绘 | `plot_kvcm_vs_oss.py`（本仓）。主线图：纵轴「数据依附于谁」，横轴「策略在哪」；kvcm 落在两轴交点 | 本仓 MIT | kvcm `4d9dd33`，上游 2026-11-13 |
| `fig-control-placement.svg` | 自绘 | 同上。同一组部件两种控制位置：策略在引擎内 vs 策略在池的 client 上。部件命名沿用 MemCache 上游架构图 | 本仓 MIT | 同上 |
| `fig-evidence.svg` | 自绘 | 同上。左栏控制权矩阵（谁决定 / 谁动字节 / 谁的记录算数），右栏 hint 落地时间线；正文 §5 引用 | 本仓 MIT | 同上 |
| `upstream-ucm-architecture.svg` | 上游原图 | unified-cache-management `docs/source/_static/architecture.svg`（[链接](https://github.com/ModelEngine-Group/unified-cache-management/blob/develop/docs/source/_static/architecture.svg)） | MIT（仓库 `LICENSE` 为华为 2025 标准 MIT 全文） | UCM `57ddbb26`（2026-11-13） |
| `upstream-lmcache-coordinator.png` | 上游原图 | [The LMCache Coordinator](https://blog.lmcache.ai/en/2026/11/11/the-lmcache-coordinator-a-control-plane-for-fleet-wide-kv-cache/) 的正文插图（`lmcache-coordinator-architecture@2x.png`） | 版权归 LMCache 作者；本文为技术对照引用并注明出处 | 2026-11-11 |
| `upstream-lmcache-mp-l2.png` | 上游原图 | [当开源遇见开源：LMCache 与 Mooncake 的一次双向奔赴](https://blog.lmcache.ai/zh/2026/05/26/%E5%BD%93%E5%BC%80%E6%BA%90%E9%81%87%E8%A7%81%E5%BC%80%E6%BA%90%EF%BC%9Almcache-%E4%B8%8E-mooncake-%E7%9A%84%E4%B8%80%E6%AC%A1%E5%8F%8C%E5%90%91%E5%A5%94%E8%B5%B4/) 的正文插图 | 版权归 LMCache 作者；技术对照引用并注明出处 | 2026-05-26 |
| `memcache-architecture.png` | 上游原图 | Ascend MemCache `doc/source/memcache_architecture.png`（[gitcode.com/Ascend/memcache](https://gitcode.com/Ascend/memcache)） | MulanPSL2 | MemCache `14b4e35e`（2026-06-02） |

## 约定

- 自绘图三张，都是本文自己的论点图，没有对应上游图：`fig-ownership`（数据依附于谁 × 策略在哪）、`fig-control-placement`（策略在引擎内 vs 在池的 client 上）、`fig-evidence`（控制权矩阵 + hint 落地时间线）。
- 图内文字用中文；项目名、API 名、字段名保留英文原样。
- 上游原图四张，一律保留原文件，不裁剪、不改色、不重绘；中文说明写在正文表格里，不贴在图上：
  - `memcache-architecture.png`：Ascend MemCache，MulanPSL2。
  - `upstream-ucm-architecture.svg`：UCM，MIT（仓库 `LICENSE` 是标准 MIT 全文）。
  - `upstream-lmcache-coordinator.png`、`upstream-lmcache-mp-l2.png`：LMCache 博文插图，版权归作者。
- 换原图的原因：重绘会把上游的图标语义与配色辨识度丢掉，而这几节要说明的正是「它的结构长什么样」；原图配逐段中文说明比中文重绘更准确。
- 唯一仍按图意重绘的情形是没有上游图可引用——也就是上面三张自绘图，它们表达的是本文的对照结论，不是对某个上游结构的复述。

## 字体要求（中文图内文字）

图内文字是中文，需要系统里有 CJK 字体；字体文件**不入仓**。脚本按顺序找这几个路径，命中即用：

```
/tmp/NotoSansCJKsc.ttc
/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc
/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc
~/.fonts/NotoSansCJKsc.ttc
~/.fonts/NotoSansCJK-Regular.ttc
~/Library/Fonts/NotoSansCJKsc.ttc
```

都没有时**不会报错**：脚本打印一行警告，图内文字自动退回英文，图仍能生成（便于在别的机器与 CI 上跑）。

装字体：

```bash
# Debian / Ubuntu
apt-get install fonts-noto-cjk
# macOS（Homebrew）
brew install --cask font-noto-sans-cjk-sc
# 或者把任意 NotoSansCJK*.ttc 放到 ~/.fonts/ 下，再 fc-cache -f
```

## 重跑

```bash
python3 docs/research/kvcm-vs-oss/figures/plot_kvcm_vs_oss.py
```

脚本会就地覆盖三张自绘图（`fig-ownership`、`fig-control-placement`、`fig-evidence`）；四张上游原图不由此脚本生成。
