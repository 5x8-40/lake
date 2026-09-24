# dynamo-ascend 工作区

本目录是 **Dynamo → 昇腾(Ascend)适配**的讨论与可交付文档区。

- 代码仓:[5x8-40/dynamo-ascend](https://github.com/5x8-40/dynamo-ascend)（fork 自 ai-dynamo/dynamo）。**代码改动进该仓**；lake 只保留文档与脚本。
- 独立计划：本目录自成体系。调研参考在 `../research/`。

## 文档约定

- **决策记录**放 `decisions/`，格式照 [`decisions/000-template.md`](decisions/000-template.md)。
- **讨论纪要**命名 `YYYY-MM-DD-主题.md`。
- **计划主线**：[00-plan.md](00-plan.md)；各线任务追踪按线单独成文（如 [e-line.md](e-line.md)）。
- 链接用相对路径。

## 现有内容

- 任务追踪：[E 引擎线](e-line.md)（任务表 + 本机实测补充 + 进展日志）
- 决策：
  - [D001 总体路线](decisions/D001-overall-approach.md)
  - [D002 组件边界](decisions/D002-component-boundary.md)
- 专题：[昇腾数据底座候选盘点](data-plane-options.md)
- 可交付：
  - [bringup.md](bringup.md) — 容器内安装 + 聚合拉起
  - [pd-mooncake.md](pd-mooncake.md) — PD / Store / 卸载 / KV router / 跨机
  - 脚本：[`../../scripts/dynamo-ascend/`](../../scripts/dynamo-ascend/)
