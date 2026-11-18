# kvcm-vs-oss 图源与许可

本目录供 [`../../kvcm-vs-oss.md`](../../kvcm-vs-oss.md) 使用。每张图的来源、许可与版本：

| 文件 | 类型 | 来源 | 许可 | 快照 / 版本 |
|---|---|---|---|---|
| `fig-ownership.svg` | 自绘 | `plot_kvcm_vs_oss.py`（本仓）。主线图：纵轴「数据依附于谁」，横轴「策略在哪」；kvcm 落在两轴交点 | 本仓 MIT | kvcm `4d9dd33`，上游 2026-11-13 |
| `fig-control-placement.svg` | 自绘 | 同上。同一组部件两种控制位置：策略在引擎内 vs 策略在池的 client 上。部件命名沿用 MemCache 上游架构图 | 本仓 MIT | 同上 |
| `fig-evidence.svg` | 自绘 | 同上。左栏控制权矩阵（谁决定 / 谁动字节 / 谁的记录算数），右栏 hint 落地时间线 | 本仓 MIT | 同上 |
| `fig-ucm-schematic.svg` | 自绘（redrawn） | redrawn from unified-cache-management `docs/source/_static/architecture.svg` | MIT with additional conditions | UCM `57ddbb26`（2026-11-13） |
| `upstream-lmcache-coordinator.png` | 上游原图 | [The LMCache Coordinator](https://blog.lmcache.ai/en/2026/11/11/the-lmcache-coordinator-a-control-plane-for-fleet-wide-kv-cache/) 的正文插图（`lmcache-coordinator-architecture@2x.png`） | 版权归 LMCache 作者；本文为技术对照引用并注明出处 | 2026-11-11 |
| `upstream-lmcache-mp-l2.png` | 上游原图 | [当开源遇见开源：LMCache 与 Mooncake 的一次双向奔赴](https://blog.lmcache.ai/zh/2026/05/26/%E5%BD%93%E5%BC%80%E6%BA%90%E9%81%87%E8%A7%81%E5%BC%80%E6%BA%90%EF%BC%9Almcache-%E4%B8%8E-mooncake-%E7%9A%84%E4%B8%80%E6%AC%A1%E5%8F%8C%E5%90%91%E5%A5%94%E8%B5%B4/) 的正文插图 | 版权归 LMCache 作者；技术对照引用并注明出处 | 2026-05-26 |
| `memcache-architecture.png` | 上游原图 | Ascend MemCache `doc/source/memcache_architecture.png`（[gitcode.com/Ascend/memcache](https://gitcode.com/Ascend/memcache)） | MulanPSL2 | MemCache `14b4e35e`（2026-06-02） |

## 约定

- 自绘图四张，各管一件事：`fig-ownership` 是正文主线（数据依附于谁 × 策略在哪），`fig-control-placement` 是部件级对照，`fig-evidence` 是控制权与落地状态，`fig-ucm-schematic` 是 UCM 接线的重绘。
- 图内文字用中文；项目名、API 名、字段名保留英文原样。
- 上游原图收三张：`memcache-architecture.png`（MulanPSL2，明确允许复制与再分发）、`upstream-lmcache-coordinator.png` 与 `upstream-lmcache-mp-l2.png`（LMCache 博文插图，版权归作者）。
- 上游图一律保留原文件，不裁剪、不改色、不重绘；中文说明写在正文图注里，不贴在图上。
- LMCache 两张的取舍：先试过按图意重绘为中文示意，但重绘版会丢掉上游图的图标语义与配色辨识度，而本文要说明的正是「它的结构长什么样」，所以改回原图 + 逐段中文说明。引用时在正文标明来源与版权归属。
- UCM 的 `architecture.svg` 仍然只链接不复制：`LICENSE` 是标准 MIT，但 README 声明「MIT license with additional conditions」且未写明附加条款，边界不清，因此按图意重绘（`fig-ucm-schematic.svg`）。

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

六个 SVG 会就地覆盖生成。
