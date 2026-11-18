# kvcm-vs-oss 图源与许可

本目录供 [`../../kvcm-vs-oss.md`](../../kvcm-vs-oss.md) 使用。每张图的来源、许可与版本：

| 文件 | 类型 | 来源 | 许可 | 快照 / 版本 |
|---|---|---|---|---|
| `fig-ownership.svg` | 自绘 | `plot_kvcm_vs_oss.py`（本仓）。主线图：纵轴「数据依附于谁」，横轴「策略在哪」；kvcm 落在两轴交点 | 本仓 MIT | kvcm `4d9dd33`，上游 2026-11-13 |
| `fig-control-placement.svg` | 自绘 | 同上。同一组部件两种控制位置：策略在引擎内 vs 策略在池的 client 上。部件命名沿用 MemCache 上游架构图 | 本仓 MIT | 同上 |
| `fig-evidence.svg` | 自绘 | 同上。左栏控制权矩阵（谁决定 / 谁动字节 / 谁的记录算数），右栏 hint 落地时间线 | 本仓 MIT | 同上 |
| `fig-ucm-schematic.svg` | 自绘（redrawn） | redrawn from unified-cache-management `docs/source/_static/architecture.svg` | MIT with additional conditions | UCM `57ddbb26`（2026-11-13） |
| `fig-lmcache-coordinator.svg` | 自绘（redrawn） | redrawn from The LMCache Coordinator（[blog.lmcache.ai，2026-11-11](https://blog.lmcache.ai/en/2026/11/11/the-lmcache-coordinator-a-control-plane-for-fleet-wide-kv-cache/)）与 [docs.lmcache.ai/mp/coordinator.html](https://docs.lmcache.ai/mp/coordinator.html) | 上游代码 Apache-2.0；**博文插图未声明可复用许可**，故只按图意重绘、不复制原图 | LMCache `58c11e38`（2026-11-13） |
| `fig-lmcache-mp-l2.svg` | 自绘（redrawn） | redrawn from《当开源遇见开源：LMCache 与 Mooncake 的一次双向奔赴》（[blog.lmcache.ai，2026-05-26](https://blog.lmcache.ai/zh/2026/05/26/%E5%BD%93%E5%BC%80%E6%BA%90%E9%81%87%E8%A7%81%E5%BC%80%E6%BA%90%EF%BC%9Almcache-%E4%B8%8E-mooncake-%E7%9A%84%E4%B8%80%E6%AC%A1%E5%8F%8C%E5%90%91%E5%A5%94%E8%B5%B4/)）与 [docs.lmcache.ai/kv_cache/mooncake.html](https://docs.lmcache.ai/kv_cache/mooncake.html) | 同上：只重绘、不复制 | 同上 |
| `memcache-architecture.png` | 上游原图 | Ascend MemCache `doc/source/memcache_architecture.png`（[gitcode.com/Ascend/memcache](https://gitcode.com/Ascend/memcache)） | MulanPSL2 | MemCache `14b4e35e`（2026-06-02） |

## 约定

- 自绘图六张，各管一件事：`fig-ownership` 是正文主线（数据依附于谁 × 策略在哪），`fig-control-placement` 是部件级对照，`fig-evidence` 是控制权与落地状态，`fig-ucm-schematic` 是 UCM 接线的重绘，两张 `fig-lmcache-*` 是 LMCache 的控制面与 MP + L2 数据流的重绘。
- 图内文字用中文；项目名、API 名、字段名保留英文原样。
- 上游原图只收一张（`memcache-architecture.png`），许可明确（MulanPSL2 允许复制与再分发）；保留原文件，不做裁剪或重绘，出处与许可写在正文图注里。
- 其余上游图一律**不复制**，只重绘为中文示意并在图注里写明「重绘自」：
  - UCM 的 `architecture.svg`：`LICENSE` 是标准 MIT，但 README 声明「MIT license with additional conditions」且未写明附加条款，边界不清。
  - LMCache 的两张博文插图：仓库代码是 Apache-2.0，但图在 WordPress 博文里，页面未声明配图许可；`docs.lmcache.ai` 的图同样未标许可。按保守处理，只按图意重绘，不搬运图片。
- 重绘件不声称是上游原图：图注与脚注都写「重绘自」，脚注里同时写明「未复制原图，原图版权归原作者」。

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
