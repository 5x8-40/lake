# dynamo-ascend 工作区

本目录是 **Dynamo → 昇腾(Ascend)适配**这一独立计划的讨论与决策记录区。

- 代码仓:[5x8-40/dynamo-ascend](https://github.com/5x8-40/dynamo-ascend)(fork 自 ai-dynamo/dynamo),以 submodule 挂在 `3rdparty/dynamo-ascend`。该仓是 fork,**仓内可以改代码**,不受 3rdparty 只读约定限制。
- 与 lake 的关系:这是**独立计划**,不在 lake 的 P 阶段路线图([`../00-plan.md`](../00-plan.md))里;lake 侧的 Dynamo 调研见 [`../research/dynamo/`](../research/dynamo/)。两边的交集(传输、PD 编排、KV 管理等结论)按需用相对链接互引。

## 文档约定

- **决策记录**放 `decisions/`,一个决策一个文件,命名 `NNN-短标题.md`,编号递增,格式照 [`decisions/000-template.md`](decisions/000-template.md)(背景 / 选项 / 决策 / 后果)。
- **讨论纪要**直接放本目录,命名 `YYYY-MM-DD-主题.md`。
- **计划主线**(阶段 / 任务 / 状态)写在 `00-plan.md`(待建,风格对齐 [`../00-plan.md`](../00-plan.md))。
- 链接一律用相对路径;引用 lake 侧文档示例:`../research/dynamo/overview.md`。
