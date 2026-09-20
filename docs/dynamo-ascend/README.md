# dynamo-ascend 工作区

本目录是 **Dynamo → 昇腾(Ascend)适配**这一独立计划的讨论与决策记录区。

- 代码仓:[5x8-40/dynamo-ascend](https://github.com/5x8-40/dynamo-ascend)(fork 自 ai-dynamo/dynamo),以 submodule 挂在 `3rdparty/dynamo-ascend`。该仓是 fork,**仓内可以改代码**,不受 3rdparty 只读约定限制。
- 这是**独立计划**,有自己的路线图与决策记录;本目录文档自成体系,不依赖、不迁就其他计划的设计原则。调研类参考材料在 `../research/`(如 [`../research/dynamo/`](../research/dynamo/)),按需用相对链接引用。

## 文档约定

- **决策记录**放 `decisions/`,一个决策一个文件,命名 `NNN-短标题.md`,编号递增,格式照 [`decisions/000-template.md`](decisions/000-template.md)(背景 / 选项 / 决策 / 后果)。
- **讨论纪要**直接放本目录,命名 `YYYY-MM-DD-主题.md`。
- **专题文档**(选型盘点、调研笔记等长期有效的)直接放本目录,命名即主题。
- **计划主线**(阶段 / 任务 / 状态)写在 `00-plan.md`(待建);各线自己的任务追踪按线单独成文(如 [e-line.md](e-line.md))。
- 链接一律用相对路径。

## 现有内容

- 任务追踪:[E 引擎线](e-line.md)(任务表 + 状态 + 进展日志;**进行中**)
- 运行手册:[Ascend 原生 bring-up](native-bringup.md)(源码编译 + 常驻容器 + etcd + FE/worker 同容器,2026-09-18 验通;含启动脚本 [scripts/ascend/](scripts/ascend/))
- 决策记录:
  - [D001 总体路线:fork 适配而非自研框架](decisions/D001-overall-approach.md)(含 KVBM→KVCR 修正、Worker 拉起差异清单)
  - [D002 组件边界:Dynamo 全组件的适配动作划分](decisions/D002-component-boundary.md)(复用 / 适配 / 替换 / 不用,含引擎双路线)
- 专题:
  - [昇腾数据底座候选盘点](data-plane-options.md)(Mooncake / memcache 二选一倾向,UCM 备选,Yuanrong 暂缓,UB-URMA 待核实)
