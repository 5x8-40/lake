# kvcm-vs-oss 图源与许可

本目录供 [`../../kvcm-vs-oss.md`](../../kvcm-vs-oss.md) 使用。每张图的来源、许可与版本：

| 文件 | 类型 | 来源 | 许可 | 快照 / 版本 |
|---|---|---|---|---|
| `fig-overview.svg` | 自绘 | `plot_kvcm_vs_oss.py`（本仓），左栏分层 + 右栏三种控制形态 | 本仓 MIT | kvcm `4d9dd33`，上游 2026-11-13 |
| `fig-evidence.svg` | 自绘 | 同上，左栏控制权矩阵 + 右栏 hint 落地时间线 | 本仓 MIT | 同上 |
| `fig-ucm-schematic.svg` | 自绘（redrawn） | redrawn from unified-cache-management `docs/source/_static/architecture.svg` | MIT with additional conditions | UCM `57ddbb26`（2026-11-13） |
| `memcache-architecture.png` | 上游原图 | Ascend MemCache `doc/source/memcache_architecture.png`（[gitcode.com/Ascend/memcache](https://gitcode.com/Ascend/memcache)） | MulanPSL2 | MemCache `14b4e35e`（2026-06-02） |

约定：

- 图数刻意压到四张。两张对照工件各承载一组问题：`fig-overview` 回答「在哪层、谁说了算」，`fig-evidence` 回答「谁持有权威、各家落地到哪一步」。拆成多张小图会让同一组问题散落在正文各处，评审成本反而更高。
- 上游原图保留原文件，不做裁剪或重绘；出处与许可写在正文图注里，本文档同步登记。
- 自绘图只用于对照工件（分层 / 控制流 / 能力矩阵 / 时间线）。`fig-ucm-schematic.svg` 是重绘，图注与脚注都写明「redrawn from」，不声称是上游原图。
- UCM 的 `architecture.svg` 与 `pd_disaggregation.jpg` 仓内只放链接，不复制：`LICENSE` 文件是标准 MIT，但 README 声明「MIT license with additional conditions」且未写明附加条款，边界不清，按保守处理。
- 重跑：`python3 docs/research/kvcm-vs-oss/figures/plot_kvcm_vs_oss.py`（无 CJK 字体，图内文字用英文）。
