# ADR-0033 — 失败归因的产生与呈现（provider 故障分类 → `run_failed` 载荷 → 前端投影）

- **Status**: Accepted
- **Date**: 2026-09-17
- **Deciders**: 本 Agent（机制设计）+ 真机实测证据（见 §2.1）
- **Related**：Issue #218（后端分类）、#220（前端呈现）、#221（迟到响应丢消息，未覆盖）；`src/agent_harness/agent/runtime.py`（分类表 / `failure_terminal`）；`src/agent_harness/session/session.py:end_run`；`web/src/lib/projection.ts`（`projectRunFailed` / `summarizeRunFailed`）；`web/src/components/StepDetail.tsx`（ChatTab「失败原因」行）；ADR-0016 §2（投影纪律）；`docs/LIVE_BROWSER_TEST_20260917.md`（实测记录）；AGENTS §7 不变量 2/4、§16.1（机制叙述的唯一落点）

> 本文档是「失败归因」这条机制的**唯一完整叙述**。代码注释只写各自那一段代码自己看不出来的操作约束 + 指向本文件的一句指针。

---

## 1. Context

### 1.1 问题：失败在两侧都不可读

真机实测（2026-09-17，详情见 `docs/LIVE_BROWSER_TEST_20260917.md`）暴露两个方向的不可读：

- **后端侧**：模型调用失败时，事件里只有一个异常类名。真实故障是供应商账户冻结（`senseaudio` 返回 HTTP 400 `{'code':'billing','message':'计费账户已被冻结','ref_code':400901}`），但 `run/failed.data` 里能读到的是 `BadRequestError`——用户与排障者都看不出"去供应商控制台看一眼计费"。
- **前端侧**：`run_failed` 事件此前在 UI 上**完全没有投影**。Inspector 的 RUN 摘要只显示「失败」两个字，Timeline 那一行的摘要为空。后端即使算出了原因，也到不了用户眼前。

### 1.2 约束：不能靠透传原文来"可读"

供应商错误体里可能回显请求内容（prompt 片段、key 前缀），且形状随供应商变化。把它原样接进事件/UI 会让 OBS-008 的脱敏不变量（事件里只允许固定文案，全文只进结构化日志）失效。

## 2. Decision

### 2.1 后端：标记表分类 → 固定 reason + 固定文案（#218）

`runtime.py` 维护一张有序标记表 `_PROVIDER_FAILURE_MARKERS`，把 `str(error).lower()` 里出现的供应商错误标记映射到**项目自有的 reason 常量**，并配一句固定的中文可读文案：

| reason | 文案语义 | 命中标记（示例） |
| --- | --- | --- |
| `provider_account_unavailable` | 欠费 / 配额耗尽 / 账户被冻结，请到供应商控制台检查计费与配额 | `billing`、`insufficient_quota`、`account_deactivated`… |
| `provider_auth_failed` | 鉴权失败，请检查供应商凭证配置 | `invalid_api_key`、`incorrect_api_key`… |
| `provider_model_not_found` | 模型不存在或无权访问 | `model_not_found` |
| `provider_content_moderation` | 内容审查拒绝输入（可能因检索到的网页文本） | `data_inspection_failed` |

- 分类命中 ⇒ `failure_terminal(reason=<分类>, message=<固定文案>)`；
- **未命中 ⇒ 两个键都不落**（不猜、不回显原文）；
- 供应商原文只进结构化日志（OBS-008）。

命中标记表的顺序有意义（先匹配先赢），且**标记必须取自供应商错误体**：不能把项目自己的 reason 常量当标记，否则常量名会自我命中（#218 实现期踩过，见 `tests/agent/test_runtime_failure_paths.py` 里 `absent` 用例的注释）。

真机证据：冻结账户的真实失败产生 `run/failed` 且 `reason=provider_account_unavailable` + 固定中文文案——证明标记匹配的是**真实的** `str(error)`，不只是合成载荷。

### 2.2 前端：`run_failure` 投影 + 两处呈现（#220）

`run/failed.data` 的 `reason` / `message` 折叠进 `ConversationState.run_failure`，由**两处**呈现：

1. Inspector Overview（ChatTab）的「失败原因」行——带 key 的正式字段位；
2. Timeline 行摘要（`summarizeRunFailed`）——Timeline 是 Inspector 的**默认页签**，同一句话不该要求用户先切页签才看得到。

投影口径（每条都由 `projection.test.ts` 锁定）：

- `run/failed(reason='cancelled' | 'orphaned'?)`：**取消不是失败**，`run_failure` 为 `null`（延续 da394a9 的「取消 ≠ 错误」）；
- 两个键都没落（未分类异常、`max_steps_exceeded` 路径）⇒ 字段整体 `null`，**不是** `{reason:null,message:null}`——后者会让 `if (state.run_failure)` 为真却无内容可渲染；
- 缺 `message` 有 `reason` ⇒ 保留 reason 并**在 UI 上以它兜底显示**（见 2.3）；
- **绝不伪造文案**：后端没给的句子，前端不编（与 `model` / `usage_total` 的缺失即 null 同口径）。

失效规则：失败归因是「**最近一个** run 的结局」。`run/started`（新 run 开始）、`run/completed`、`run/interrupted` 都把它清掉——与 `run_cancelled` / `run_interrupted` 同一套「更晚的终态赢」，避免状态行说「已完成」而「失败原因」还挂在旁边。

### 2.3 已知载荷形状，以及对它们的诚实呈现

`session.end_run` 的两个键**互相独立**（`if reason:` / `if message:` 各自判断），因此三态都真实存在，前端必须都接住：

| 载荷 | 来源路径 | 呈现 |
| --- | --- | --- |
| `reason` + 固定中文 `message` | 已分类的供应商故障（2.1） | 文案 + reason 码 |
| `reason`，无 `message` | 工具连续失败保险丝（`identical_tool_failure_loop`，`runtime.py` 工具阶段收尾） | 退到显示 reason 码——比空白更能说明这行为什么红 |
| 两者都无 | 未分类异常；`max_steps_exceeded`（只进 tracer，从不落 `run/failed.data`） | 字段为 `null`，界面就是「失败」（诚实的不显示，而不是编一句） |

⚠ **`message` 不保证是"给人读的中文句子"**：上下文超限路径（`ContextWindowExceededError`）直接 `append(RUN_FAILED, {"message": str(error)})`，那里的文本是项目内部英文串（如 `Orphan tool result in context`）。它不违反 §1.2 的脱敏约束（是项目自己的常量/内部文本，不是供应商回显），但任何**依赖 `message` 必然是中文可操作句**的消费方都是错的。

呈现细节：这行文案是完整句子（30-45 字），而 `.detail-val` 是 `nowrap + ellipsis`（默认 Inspector 宽 340px）⇒ 需要换行修饰类，否则正好切掉「请到供应商控制台检查计费与配额」这类可操作尾巴；Timeline 摘要**不**做 `slice(0,40)`（相邻 summary 的惯例），理由同上——截断切掉的是可操作指令，两个消费面都已有 CSS 省略兜底。

## 3. 未覆盖 / 后续

- **#221**：#219 的 steer→queue 回退只覆盖"响应在窗口内到达"；更晚到达的非 2xx 仍会丢消息。与本 ADR 无关但同批发现。
- **`model/failed.message` 未投影**：未分类失败时 `model/failed.data.message`（如 `model call failed: BadRequestError`）已经存在却仍是 `noopProjection`。本 ADR 不投影它——那句话对用户没有可操作性，逐字显示等于把开发者语言摆到界面上；真要呈现应先在**后端**把它分类（与 2.1 同法），而不是让前端展示异常类名。
- **`max_steps_exceeded` 进不了 UI**：它只进 tracer（`runtime.py`），`run/failed` 里没有任何键。要让用户看到"步数用尽"，必须先改后端载荷。
- **fork 继承**：fork 出的子会话若种子日志尾部带 `run/failed`，Overview 会显示父 run 的归因（字段语义是"最近一个 run"，子会话确实继承了这个事件，ADR-0017 决策 3）。判定为可接受，未额外处理。

## 4. 后果

- 失败不再只有「失败」两字：已分类故障给出原因 + 处置建议，未分类至少给出 reason 码。
- 前端不持有第二份失败语义：`run_failure` 是 `run/failed.data` 的纯函数，重放（刷新 / 历史加载 / fork 子会话）与实时流给出同一值。
- 新增 UI 契约：`ConversationState.run_failure` 的三态与失效规则（2.2）——改 `projectRunFailed` 的任何人都必须同步这三条。
