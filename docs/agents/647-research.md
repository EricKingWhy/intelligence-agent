# #647 T12h 调研：成熟系统如何对外暴露"压缩被拒绝/降级"的原因

> 日期：2026-10-06。对应票：#647（T12h source-range 拒绝可观测；T11f 排除）。
> 依据：`AGENTS.md` §6.1、`docs/SDD_WORKFLOW_PROTOCOL.md` §1.3。

## 1. 来源（≥2 个独立来源，均已核实）

### 来源 1：Pi（上游代码，已对照上游核实）

- **第三方笔记线索**：dg-ai-notes 本地克隆（`/home/hatch/workspace/dg-ai-notes`，
  commit `5f4abd34d3d4d756e016b78bfbfe90d97cd9c7cf`）
  `skills/dg-piagent/references/sdk_doc/18-compaction.md`：subscribe 层 `compaction_end`
  事件带 `reason` / `result?` / `aborted` / `willRetry` / `errorMessage?`；
  `summarization_retry_scheduled` 带 `attempt` / `maxAttempts` / `errorMessage`。
- **上游核实（2026-10-06，GitHub `earendil-works/pi` main
  `packages/coding-agent/src/core/extensions/types.ts`）**：扩展层
  `SessionCompactFailedEvent`（type `session_compact_failed`）字段
  `reason: "manual"|"threshold"|"overflow"`、`errorMessage?: string`、
  `aborted: boolean`、`willRetry: boolean`、`fromExtension: boolean`；
  成功路径 `SessionCompactEvent` 同样携带 `reason` + `willRetry`；
  `CompactOptions.onError?: (error: Error) => void` 回调。笔记主张与上游一致。

### 来源 2：Cline（上游代码，已核实）

- Cline tag `v3.20.0` `src/core/context/context-management/ContextManager.ts`
  （读取 2026-10-06）：token 触顶触发 half/quarter 截断（降级）时，
  `applyStandardContextTruncationNoticeChange` 把**第一条 assistant 消息**的文本块
  改写为 `formatResponse.contextTruncationNotice()`——降级事实以一条任务可见通知
  进入模型可见上下文（对模型与用户同时可见），不是只留在日志里。

## 2. 机制摘要

| 系统 | 拒绝/降级信号 | 载荷形状 |
| --- | --- | --- |
| Pi | 独立 typed 事件 `session_compact_failed`（成功是另一个事件 `session_compact`，二者不同形） | 有界：reason 枚举 + errorMessage 文本 + aborted/willRetry 布尔；摘要重试有 attempt/maxAttempts 计数 |
| Cline | 降级发生时向会话历史注入可见通知（第一条 assistant 消息文本被替换为截断通知） | 通知文本进入模型可见上下文，调用方无需读日志即可知"发生过降级" |

共同点：**"压缩被拒绝/降级"是一个任务可见、有界载荷的显式信号**，与成功路径
可区分；调用方/用户不需要比对消息内容或翻诊断日志就能知道发生了什么、为什么。

## 3. 契合点

- 本仓已有同构机制（W-04 #348 冻结形状）：`CompactionFailure`（有界
  `error_class` 词表 + `message` 只装自家文案/类型名的脱敏载荷）→ builder
  `_record_compaction_failures` → `context/compaction_failed` SessionEvent
  （任务可见状态）。与 Pi 的"typed 事件 + 有界失败载荷"同构。
- 规格依据：`06_CONTEXT_ARTIFACT_MEMORY.md` §8「每次失败原因留诊断与任务可见
  状态」（ADR-0007 #348 修订注）；AGENTS.md §7 不变量 4（Event ≠ Diagnostic Log）。
- 冲突检查：不新增事件类型/第二通道（违反 Reuse First / 懒惰阶梯第 2 级——仓库
  已有）；不伪造第 3 次摘要尝试（不违反 #348「至多两次尝试」契约）；不改 hard/
  target 比较符与预算契约；不新增 deterministic fallback；不删原始历史。

## 4. 判定

**ADAPT**——在既有有界失败通道上补一个有界 `error_class`
（`source_range_unavailable`），来源拒绝记录以 `attempt=0` 标记"非摘要尝试"，
经既有 `CompactionResult.failures` → builder `_record_compaction_failures` →
`context/compaction_failed` 落任务可见状态；拒绝写成功 bracket、保留原投影
（与 ADR-0007 #348"摘要失败 ≠ 压缩发生，保留原投影"的语义一致）。
Cline 的"把降级通知注入上下文"形态**不采纳**：会把诊断信息混入模型输入，
违反本仓"Event ≠ Log"与有界注入纪律。

## 5. License

无实质复制 / Port 上游代码（仅借鉴"typed 失败事件 + 有界载荷"的设计形态，
且本仓 #348 已先于本调研建立同构机制）⇒ License 无涉。
