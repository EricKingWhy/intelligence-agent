# W-04 · 摘要有界重试与 Artifact/Context 硬护栏
**目标仓库**：intelligence-agent-backend（Python ContextBuilder / Compactor）。

**类型/优先级**：P0 Recovery。**依赖**：W-01、W-03。**范围**：`context/compactor.py`、`context/builder.py`、`tooling/overflow.py` 与状态/错误投影；对齐 Spec 06 §5/§8。不得改变 ToolExecutor 的 retry 责任。

## 失败契约

自动压缩阈值取生效配置（规格默认 70%，硬护栏默认 85%）；调用摘要模型失败后**至多再试一次**，每次失败原因留诊断与任务可见状态。两次失败且旧投影仍低于硬护栏，可安全继续并在下一稳定边界再评估；达到硬护栏而仍无法形成可核验上下文时，以现有暂停生命周期停住，不发送越窗请求。Artifact save/read-back 失败不能伪造 ref；旧原文仍在窗口内时保留，窗口容不下时暂停。

## 工作指令

1. 先覆盖模型 timeout/异常/空摘要/结构缺字段、Artifact save 成功却读不回、磁盘满、估值越硬护栏。断言无空 `context/compacted`、无假 ref、无无限重试、无超限 model request。
2. 复用 W-01 的持久化校验与 W-03 的确定性裁剪；只在二者不足时调用摘要。压缩事件写入后重新从 Event 投影确认预算与保护事实；不合格则拒绝事件或走既有补偿边界。
3. UI/CLI 需要可解释的失败码、阈值、尝试次数、下一步（检查 Artifact 存储/换模型/缩短输入），不能把暂停写成完成/失败。

**验收**：确定性注入以上故障各一次；当预算足够时可继续，达到硬护栏时安全暂停；重启后状态一致、原 SessionEvent/Artifact 不丢。focused tests、失败注入真实本地 ArtifactStore、无无界日志。**不做**：新预算引擎、#305 pause 定义、Memory V2。

**成熟参考/复用**：[Pi compaction](https://pi.dev/docs/latest/compaction)的 reserve/保留近期对话、[DeepSeek pruner](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/compaction/compaction-tool-result-pruner/README.md)的先裁剪后摘要均 `PORT DESIGN`；暂停沿用 #305 和本仓 SessionEvent。
