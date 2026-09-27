# W-01 · 修复摘要失败后的持久化重载丢约束
**目标仓库**：intelligence-agent-backend（Python Core）。

**类型/优先级**：P0 Bug。**依赖**：现有 Phase 5；不依赖新桌面。**范围**：`src/agent_harness/context/compactor.py`、`context/builder.py`、`session/derive.py` 及对应 context/session 测试。实施前重读 Engineering Spec 06 §5/§8、03 的 SessionEvent 投影，核对主线现状。

> ⚠️ **2026-09-27 修订**（来源：`docs/PRD_LONG_TASK_CONTEXT_MANAGEMENT.md` §4.4/§6，三轮 grill-me 确认）：新增工作指令 4（摘要校验闸门）与 5（切点规则），见正文 [增量] 标注。

## 可复现症状

当前 `Compactor` 在摘要模型失败时会生成 `_mechanical_summary` 供**当轮**使用，但返回结果的 `summary` 可为 `None`；`ContextBuilder` 随后追加 `context/compacted.summary=""`。进程重载后 `derive_messages` 遮蔽早期事件，却因摘要为空不注入旧约束。不要只验证“当轮继续”，要测试**压缩→事件落盘→新进程加载→再次构建模型输入**。

## 工作指令

1. 先写回归：早期用户消息含精确约束 `不得删除 old_rows`、精确 ID `R-042`，中间有成对 Tool call/result，触发摘要模型失败。断言新 SessionStore 实例重载后模型输入仍含两项精确信息、Tool pair 完整，且原始 SessionEvent 未删除。
2. 查清当前机械 fallback 的返回契约与持久化点；只修“成功压缩事件与下次投影不一致”的最小原因。不能提交空 summary 后遮蔽原事件；若 fallback 不满足安全摘要条件，拒绝该 compaction 并保持旧投影。
3. 再覆盖摘要模型超时、空字符串、异常及持久化失败；证明不产生半个压缩事件、不触发新的 Tool 执行。
4. [增量] **摘要校验闸门**：压缩事件落盘前逐条校验——8 节结构齐全（空节写 `(none)`）、程序化节（1/2/7/8）与 harness 重新提取值**逐字一致**、summary 非空且体积小于被压缩前缀（dsh「摘要未变小则拒绝」）。任一不过 → 拒绝该次 compaction、保留原投影。8 节契约全文见 W-04 增量与 PRD §6。
5. [增量] **切点规则**：压缩边界永不在 tool result 处切断——同一 `tool_call_id` 的 call/result 原子保留在切点同侧（PORT DESIGN 自 Pi，调研 R R11），保证剩余上下文自洽。

**验收**：红证能在修前复现；修后重载的模型输入与当前轮对约束、ID 的判断一致；summary 事件非空且有效，或无压缩事件并保留原上下文；历史可 replay。运行 focused context/session 测试、ruff，并按 V3.1-lite 记录冻结树与 review。**不做**：新保护事实模型、Memory V2、全局压缩重写。

**成熟参考/复用**：[Pi 的 compaction](https://pi.dev/docs/latest/compaction)保留原会话并持久化摘要；借其“原历史可回查、摘要可续用”的设计，`PORT DESIGN` 到本仓 Python Event 投影。Pi TS Runtime 不直接嵌入 Core；上游 MIT 许可须在实质复制时保留。
