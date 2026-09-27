# W-04 · 摘要有界重试与 Artifact/Context 硬护栏
**目标仓库**：intelligence-agent-backend（Python ContextBuilder / Compactor）。

**类型/优先级**：P0 Recovery。**依赖**：W-01、W-03。**范围**：`context/compactor.py`、`context/builder.py`、`tooling/overflow.py` 与状态/错误投影；对齐 Spec 06 §5/§8。不得改变 ToolExecutor 的 retry 责任。

> ⚠️ **2026-09-27 修订**（来源：`docs/PRD_LONG_TASK_CONTEXT_MANAGEMENT.md` §4.3/§4.5/§6，三轮 grill-me 确认）：新增「摘要内容契约（8 节 + 混合式生成）」「接近护栏 warning」「用户消息逐字保留」三项增量，见下方专节。

## 失败契约

自动压缩阈值取生效配置（规格默认 70%，硬护栏默认 85%）；调用摘要模型失败后**至多再试一次**，每次失败原因留诊断与任务可见状态。两次失败且旧投影仍低于硬护栏，可安全继续并在下一稳定边界再评估；达到硬护栏而仍无法形成可核验上下文时，以现有暂停生命周期停住，不发送越窗请求。Artifact save/read-back 失败不能伪造 ref；旧原文仍在窗口内时保留，窗口容不下时暂停。

## 工作指令

1. 先覆盖模型 timeout/异常/空摘要/结构缺字段、Artifact save 成功却读不回、磁盘满、估值越硬护栏。断言无空 `context/compacted`、无假 ref、无无限重试、无超限 model request。
2. 复用 W-01 的持久化校验与 W-03 的确定性裁剪；只在二者不足时调用摘要。压缩事件写入后重新从 Event 投影确认预算与保护事实；不合格则拒绝事件或走既有补偿边界。
3. UI/CLI 需要可解释的失败码、阈值、尝试次数、下一步（检查 Artifact 存储/换模型/缩短输入），不能把暂停写成完成/失败。

**验收**：确定性注入以上故障各一次；当预算足够时可继续，达到硬护栏时安全暂停；重启后状态一致、原 SessionEvent/Artifact 不丢。focused tests、失败注入真实本地 ArtifactStore、无无界日志。**不做**：新预算引擎、#305 pause 定义、Memory V2。

## [增量] 摘要内容契约（8 节 + 混合式生成）

8 节结构（节标题逐字）：`## 原始目标与用户约束` / `## 保护事实表` / `## 已完成工作与关键决策` / `## 失败方案` / `## 当前进行中状态` / `## Next Step` / `## 精确标识清单` / `## 文件清单`。

**混合式生成（弱模型兜底，调研 B2 自建项，本票主要工作量）**：第 1/2/7/8 节由 harness 程序化生成——第 1 节从 SessionEvent 用户事件**逐字引用**（禁止改写）；第 2 节整表复制 W-02 注册表（含失败方案）；第 7 节从 SessionEvent 逐字提取文件路径/命令/ID/错误串（dsh "Preserve exact file paths, commands, error strings, identifiers, numeric values"）；第 8 节读/改文件清单**跨压缩累积**（Pi 机制）。**模型只写第 3/4/5/6 节**（决策含理由、失败方案、进行中状态、Next Step+解除条件），摘要模型默认主 provider 便宜档。空节写 `(none)`；滚动合并四规则：不许删节、空节写 (none)、逐字保留精确标识、旧摘要保真去陈（dsh R3）。

**校验闸门**（落盘前，任一不过即拒绝该次压缩并保留原投影）：8 节齐全；程序化节与 harness 重新提取值**逐字一致**（harness 自生成即可自验）；summary 非空且小于被压缩前缀；第 5 节与进度清单 `in_progress` 项一致（W-29 联动）。

## [增量] 接近护栏 warning 与用户消息逐字

- **接近硬护栏 warning**（Anthropic `memory_20250818` 式）：达到 `hard_guard_threshold` 前向模型发系统 warning「即将到达上下文上限，请把关键信息显式落盘（保护事实/进度清单）」，再触发压缩。
- **用户消息逐字保留**（调研 B6，Codex 仅二手来源，按最保守语义定稿）：用户消息永不被裁剪/摘要改写；因此超限的走硬护栏暂停，不牺牲用户原话。

**成熟参考/复用**：[Pi compaction](https://pi.dev/docs/latest/compaction)的 reserve/保留近期对话、[DeepSeek pruner](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/README.md)的先裁剪后摘要均 `PORT DESIGN`；暂停沿用 #305 和本仓 SessionEvent。
