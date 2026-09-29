# W-02 · 建立带 SessionEvent 来源的任务保护事实
**目标仓库**：intelligence-agent-backend（Python Core / SessionEvent 投影）。

**类型/优先级**：P0 Contract。**依赖**：W-01。**范围**：SessionEvent schema/服务端投影与 `ContextBuilder` 注入点；候选 `session/event.py`、`session/derive.py`、`context/builder.py`。先核对 Engineering Spec 03/06 和 `CONTEXT.md`；不得建 Memory V2 平行事实库。

> ⚠️ **2026-09-27 修订**（来源：`docs/PRD_LONG_TASK_CONTEXT_MANAGEMENT.md` §5）：fact 类型新增「失败方案」，见正文 [增量] 标注。

## 明确契约

保护事实仅包含：用户原目标、明确约束/授权及其撤销、验收项与变更、精确标识、已确认的关键决策、已完成/未完成边界、未决 Operation、关键证据 ref，**[增量] 失败方案**。每项至少有 `fact_id`、类型、原文字面值或无损结构值、`source_event_id/seq`、状态（生效/被后续用户事件取代）和任务 Session ID。[增量] **失败方案结构**：`{fact_id, type: "failed_approach", 路径描述, 证伪依据, source_event_id/seq, 状态, session_id}`——证伪依据必须指向证伪事件（测试红/用户否决/运行时错误），不接受「模型觉得不行」。仅用户事件/已确认系统事实可改变授权；搜索结果、仓库文本、模型摘要、进度文件及 Memory 候选均不能升级成用户授权。投影须能从原 SessionEvent 前缀重建，Fork 在合法边界继承那一刻生效事实并留来源。

> **2026-09-29 用户批准的运行边界投影澄清**：append-only SessionEvent 与 `derive_protected_facts()` 保留/重建所有 run 边界；`ContextBuilder` 与压缩摘要只向模型提供按 `source_seq` 最新的一条 `work_boundary`，其余事实类别仍按原契约完整注入。这样重复 run 的边界不会累计耗尽独立事实预算。

## 工作指令

1. 先为同一约束被新用户指令明确撤销、仓库 README 冒充新指令、摘要改写订单号、Fork 前后事实隔离各写判据。[增量] 再为失败方案写判据：证伪依据缺失（无 source_event_id）的「失败方案」不得注册；已注册失败方案在压缩后仍逐项可追。
2. 设计最小追加事件或确定性投影，不把所有对话复制进新表；保存来源并处理重复事件/重放幂等。
3. 注入 Context 时对受保护事实设独立预算与硬护栏：不能因为普通近期窗口变化丢弃；放不下时安全暂停并指出缺口，不能静默截断精确 ID。

**验收**：先构建、压缩、重启、Fork 后，原目标/禁令/精确 ID/未完成项可逐项追到原 Event；恶意仓库文字不能改变授权；旧事件不被修改；Memory Provider 关闭或故障时仍成立。focused session/context 测试 + 真模型上下文快照检查（脱敏）。**不做**：记忆召回排序、#305 预算、用户任务 UI。

**成熟参考/复用**：[Anthropic 长任务实验](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)用显式进度与 feature list 防跨窗口遗忘；[Codex long-horizon 实践](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex)把目标、限制、完成条件外置。这里 `PORT DESIGN`；SessionEvent/ContextBuilder 直接 `REUSE` 本仓实现。
