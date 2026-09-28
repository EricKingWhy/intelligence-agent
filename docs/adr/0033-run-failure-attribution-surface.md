# ADR-0033 — 失败归因的产生与呈现（provider 故障分类 → `run_failed` 载荷 → 前端投影）

- **Status**: Accepted
- **Date**: 2026-09-17（§2.4 与 §3 的三条判定随 #222 同日追加）
- **Deciders**: 本 Agent（机制设计）+ 真机实测证据（见 §2.1）
- **Related**：Issue #218（后端分类）、#220（前端呈现）、#221（迟到响应丢消息，未覆盖）、**#222（任何失败路径都要有归因）**、**#239（分类表下沉 model 层）**；`src/agent_harness/model/failure.py`（分类表 / reason / 固定文案 / DSML 判定——本表唯一 owner）；`src/agent_harness/agent/runtime.py`（`failure_terminal` / 消费分类结果 / `UNCLASSIFIED_FAILURE_MESSAGE` 的使用点）；`src/agent_harness/session/session.py:end_run`；`web/src/lib/projection.ts`（`projectRunFailed` / `summarizeRunFailed`）；`web/src/components/StepDetail.tsx`（ChatTab「失败原因」行）；`docs/BACKEND_CONTRACT_STREAMING_UI.md` §4（跨仓取值登记）；ADR-0016 §2（投影纪律）；`docs/LIVE_BROWSER_TEST_20260917.md`（实测记录）；AGENTS §7 不变量 2/4、§16.1（机制叙述的唯一落点）

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

`model/failure.py` 维护一张有序标记表 `_PROVIDER_FAILURE_MARKERS`，把 `str(error).lower()` 里出现的供应商错误标记映射到**项目自有的 reason 常量**，并配一句固定的中文可读文案（T03/#239 起本表与 DSML wire marker 都在 model 层；`runtime.py` 只消费 reason / 文案 / 判定，不再持有 vendor 词汇）：

| reason | 文案语义 | 命中标记（示例） |
| --- | --- | --- |
| `provider_account_unavailable` | 欠费 / 配额耗尽 / 账户被冻结，请到供应商控制台检查计费与配额 | `billing`、`insufficient_quota`、`account_deactivated`… |
| `provider_auth_failed` | 鉴权失败，请检查供应商凭证配置 | `invalid_api_key`、`incorrect_api_key`… |
| `provider_model_not_found` | 模型不存在或无权访问 | `model_not_found` |
| `provider_content_moderation` | 内容审查拒绝输入（可能因检索到的网页文本） | `data_inspection_failed` |

- 分类命中 ⇒ `failure_terminal(reason=<分类>, message=<固定文案>)`；
- **未命中 ⇒ `reason = 异常类型名` + `message = UNCLASSIFIED_FAILURE_MESSAGE 模板`**
  （#222 起；旧口径是"两个键都不落"，见 §2.4 为什么必须改）；
- 供应商原文只进结构化日志（OBS-008）。进事件的只有**项目自己拼的字**：分类常量、
  以及**异常类名**（`type(error).__name__`——类名不是回显正文，与 `model/failed` 的
  `model call failed: {error_type}` 同源，早于本票就存在）。

命中标记表的顺序有意义（先匹配先赢），且**标记必须取自供应商错误体**：不能把项目自己的 reason 常量当标记，否则常量名会自我命中（#218 实现期踩过，见 `tests/agent/test_runtime_failure_paths.py` 里 `absent` 用例的注释）。

匹配面是整个 `str(error)`（供应商错误体的 `code` **与** `message` 自然语言都算），不是在解析错误码——标记是"从错误码里挑的词"，不是"只可能出现在错误码里"。顺序因此有实际后果：实测 `insufficient_quota` 那条载荷同时含 `billing details`，而 `billing` 在表里更靠前，于是按 `billing` 命中**同一** reason（结果一致，故不调整顺序；但改表顺序前先看这条）。

真机证据：冻结账户的真实失败产生 `run/failed` 且 `reason=provider_account_unavailable` + 固定中文文案——证明标记匹配的是**真实的** `str(error)`，不只是合成载荷。

### 2.2 前端：`run_failure` 投影 + 两处呈现（#220）

`run/failed.data` 的 `reason` / `message` 折叠进 `ConversationState.run_failure`，由**两处**呈现：

1. Inspector Overview（ChatTab）的「失败原因」行——带 key 的正式字段位；
2. Timeline 行摘要（`summarizeRunFailed`）——Timeline 是 Inspector 的**默认页签**，同一句话不该要求用户先切页签才看得到。

投影口径（每条都由 `projection.test.ts` 锁定）：

- `run/failed(reason='cancelled' | 'orphaned'?)`：**取消不是失败**，`run_failure` 为 `null`（延续 da394a9 的「取消 ≠ 错误」）；
- 两个键都没落 ⇒ 字段整体 `null`，**不是** `{reason:null,message:null}`——后者会让 `if (state.run_failure)` 为真却无内容可渲染。**这个状态在运行期已不可达**（§2.4 之后每条失败路径都落 reason），保留是因为历史会话里确实有这种 `run/failed`（#222 之前写下的），前端必须把它们渲染成「失败」而不是崩或铺空槽；
- 缺 `message` 有 `reason` ⇒ 保留 reason 并**在 UI 上以它兜底显示**（见 2.3）。#222 起运行期两条都齐，这一支留给历史载荷与直接调 `session.end_run` 的消费者；
- **绝不伪造文案**：后端没给的句子，前端不编（与 `model` / `usage_total` 的缺失即 null 同口径）。

失效规则：失败归因是「**最近一个** run 的结局」。`run/started`（新 run 开始）、`run/completed`、`run/interrupted` 都把它清掉——与 `run_cancelled` / `run_interrupted` 同一套「更晚的终态赢」，避免状态行说「已完成」而「失败原因」还挂在旁边。

### 2.3 已知载荷形状，以及对它们的诚实呈现

`session.end_run` 的两个键**互相独立**（`if reason:` / `if message:` 各自判断），因此下列形状都真实存在，前端必须都接住：

| 载荷 | 来源路径 | 呈现 |
| --- | --- | --- |
| `reason` + 固定中文 `message` | 已分类的供应商故障（2.1）；未分类异常（#222：`UNCLASSIFIED_FAILURE_MESSAGE`） | 文案 + reason 码 |
| `reason`，无 `message` | 工具连续失败保险丝（`identical_tool_failure_loop`）；**#222 之前**写下的历史 `run/failed` | 退到显示 reason 码——比空白更能说明这行为什么红 |
| 两者都无 | 只剩历史会话与直接调 `session.end_run` 的消费者（运行期已不可达，§2.4） | 字段为 `null`，界面就是「失败」（诚实的不显示，而不是编一句） |

⚠ **`message` 不保证是"给人读的中文句子"**：上下文超限路径（`ContextWindowExceededError`）直接 `append(RUN_FAILED, {"message": str(error)})`，那里的文本是项目内部英文串（如 `Orphan tool result in context`）；未分类兜底句里嵌的也是异常**类名**。它不违反 §1.2 的脱敏约束（是项目自己的常量/内部文本/类名，不是供应商回显），但任何**依赖 `message` 必然是中文可操作句**的消费方都是错的。

⚠ **`reason` 是开集，不是枚举**（#222 起）：除了 `cancelled` / `orphaned` 两个字面量，其余取值是「项目分类常量 ∪ 任意异常类名」。前端只对那两个做等值判断（`reason === 'cancelled'`），其余一律当不透明字符串；写白名单、正则或长度假设都是错的。

呈现细节：这行文案是完整句子（30-45 字），而 `.detail-val` 是 `nowrap + ellipsis`（默认 Inspector 宽 340px）⇒ 需要换行修饰类，否则正好切掉「请到供应商控制台检查计费与配额」这类可操作尾巴；Timeline 摘要**不**做 `slice(0,40)`（相邻 summary 的惯例），理由同上——截断切掉的是可操作指令，两个消费面都已有 CSS 省略兜底。

**两个消费面必须同规则**（#222 真机实测的教训）：Timeline 摘要（`summarizeRunFailed`）一直有"没文案就用码"的兜底，而 Overview 那一行的码只在 `message && reason` 同时成立时才缀出来 ⇒ 只给码的载荷渲染成**标签在、值为空**（`「失败原因」 ____`），比不显示这行更糟。两处现在都是"无文案 ⇒ 码就是值"，并由 `StepDetail.runFailure.test.tsx` 的 `rowValue()` 按**值域**断言——那条旧断言用的是整页 `toContain(码)`，而码还出现在**同一元素**的 `title` 属性里，所以它在值域为空的树上照样绿（假绿形态记在测试注释里，可执行复现：码在整页出现 2 次 = title + 值域）。

### 2.4 任何失败路径都要有归因（#222）

**缺口**（真机，2026-09-17）：失败的 run 在 durable 历史里说不出为什么。

```
3 run/failed {"trace_id":"11ebf194…","trace_url":null}   ← 无 reason、无 message
```

`reason` 的缺省语义（"缺省 = 模型/执行器异常"）是**把归因推给读事件的人**，而读事件的人（界面、事后排障）拿不到——异常正文只进进程日志，日志会滚。同一次失败连 `model/failed` 都没有：失败发生在**归因窗口之外**（tiktoken 取词表时网络不可达 ⇒ `ContextBuilder.build` 先炸，`model_call_open` 从未置位）。

**决定**：`run/failed` 的 **`reason` 在每条运行期失败路径上都落值**，`message` 落项目自己拼的可读句。逐路径（`grep` 穷举过：全仓只有这 5 处写 `run/failed`，都在 `runtime.py`）：

| 路径 | `reason` | `message` |
| --- | --- | --- |
| 上下文超限（`ContextWindowExceededError`，直接 append） | `context_window_exceeded` | `str(error)`（内部英文串，见 §2.3 的警告） |
| `max_steps` 保险丝 | `max_steps_exceeded` | 与 `agent_decision` 日志同一句（「连续 N 轮仍在请求工具，触发保险丝」） |
| （`#312` 起）`max_steps` / 预算到顶 | **不再是失败终态** | 改落**非终态** `run/paused`（`reason=budget_exhausted` + `trigger_dimension`），本表该行只对历史载荷成立，见下方边界 4 |
| 同错熔断硬保险丝 | `identical_tool_failure_loop` | 无（码即信息） |
| 异常臂（模型在途 / 工具 / 执行器 / **投影阶段**） | 分类常量，未分类则 `type(error).__name__` | 分类固定文案，未分类则 `UNCLASSIFIED_FAILURE_MESSAGE` 模板 |
| 取消臂 / 孤儿回收 | `cancelled` / `orphaned` | 无 |

三条边界，都写在这里而不是只写在代码里：

1. **兜底文案只代入类名，不代入 `str(error)`**。类名是类名，正文是正文——正文可能含 provider 回显（OBS-008 红线）。用例带反向断言（异常正文不得出现在任何持久化事件里）。
2. **`model/failed.message` 不跟着变**：它未分类时仍是 `model call failed: {error_type}`。两个面各有读者，`run/failed` 是给人看的终态，`model/failed` 是逐步归因（前端不投影，§3）。写代码时用一个中间变量显式分开，别把同一句喂给两边。
3. **`max_steps` 改走 `failure_terminal`**。这条路径原先自己拼 `session.end_run`，绕过了终态字段的唯一 owner —— 这正是它"一个键都没有"而长期没人发现的机制原因（字段集中供给被绕过，下次加字段还会漏它）。
4. **（`#312` 追加）预算到顶不是失败，故它离开了本表**。撞 `local.max_agent_turns` / `run.max_agent_turns_total` 时该次执行落**非终态** `run/paused`（`reason=budget_exhausted` + `trigger_dimension`），既不写 `run/failed` 也不写 `run/completed`；`max_steps_exceeded` 这个受控失败终态随之**在运行期不可达并删除**（`memory/v2/eligibility.py` 的白名单同步移除它）。本表上一条只对 `#312` 之前的历史载荷成立 ⇒ 读到旧载荷要照旧渲染成失败，但**不得**把它当作今天还可能出现的归因面。前端契约的对应处已同步（`docs/BACKEND_CONTRACT_STREAMING_UI.md` §4）。

**未覆盖（有据，非本票引入）**：失败发生在 `begin_run` **之前**（`USER_ACCEPTED` checkpoint / 写 user 消息阶段）时 `run_id` 为 `None`，此时**没有任何终态事件可写**（`failure_terminal` 返回 `None`），durable 历史停在 `run/started` 之前。这是"没有 run 可终结"的正当语义，不是本票要改的形状；真要覆盖得先定义"无 run 的失败"往哪写。

**红证与证据**：
- 后端：把 `reason=provider_reason or type(error).__name__` 回退 ⇒ 4 条用例红（含参数化的未分类载荷）；只回退 `max_steps` 的 `reason` ⇒ 恰好那 1 条红。新增的"投影阶段失败"用例（`_ExplodingContextBuilder` 替身，形状等价、异常类型不同于真机的 `ProxyError`）锁"没有 `model/failed`、仍要能说出原因"。
- 前端：回退 Overview 的值域兜底 ⇒ 2 条按值域断言的用例红，失败信息逐字复现真机空值（`expected '' to be 'RateLimitError'`）。
- 真机（代理已恢复，把 `MODEL_BASE_URL` 指向死端口制造真实失败；`--workers`/探针会话已按纪律回收，会话基线 32 复原）：事件流 `run/failed {"reason":"RateLimitError"}`，Overview 行 `「失败原因 RateLimitError」`，Timeline 摘要同码。附带观测：链路是 `model/fallback deepseek → glm · APIConnectionError` —— 终态 `reason` 指的是**最后一次尝试**的异常类型，主模型的类型在 `model/fallback` 事件里（见 §3）。

## 3. 未覆盖 / 后续

- **#221**：#219 的 steer→queue 回退只覆盖"响应在窗口内到达"；更晚到达的非 2xx 仍会丢消息。与本 ADR 无关但同批发现。
- **`model/failed.message` 仍未投影——这是决定，不是遗漏**（#222 复查后维持）：票面曾建议"把它投影出来当兜底"，判定**不做**。理由：(a) `run/failed` 现在是它所在信息面的唯一载体，"原因到达用户眼前"已经达成，再投影一条会造成同一事实两个来源（§16.1）；(b) 真机复核时 `model/fallback` 行已经在 Timeline 上显示了中间过程（`deepseek → glm · APIConnectionError`），兜底并不缺位；(c) 它未分类时的文本是开发者英文（`model call failed: X`），摆到界面上正是 §1.2 反对的形状。若将来真要在中途（run 还没终结时）提示失败，那是另一个需求，按 2.1 同法先在后端分类。
- **`reason` 开集与前端等值判断的耦合**（#222 复查发现，判定**不改**）：前端用 `reason === 'cancelled'` / `=== 'orphaned'` 当语义开关，而 `reason` 现在可能是任意异常类名。若某天出现一个**恰好叫 `cancelled` 的异常类**，一次真实失败会被渲染成中性「已取消」并把归因整行隐藏——这是本链上最坏的错法。实测（`grep` 全依赖树）不存在小写 `cancelled` / `orphaned` 的异常类（`asyncio.CancelledError` 走专用臂且大小写不同），且本仓没有任意第三方代码的 in-process 加载路径，故当前不可达；加命名空间前缀（`error_type:…`）能根治但要改赋值 + 3 条断言，且把用户可见字符串变丑，本票不换。**记在这里是因为它是潜伏耦合，不是因为它现在会炸。**
- **fallback 链上 `reason` 只指最后一次尝试**：`model/fallback` 与 `run/failed` 两处字符串长得一样（都是 `type(error).__name__`）却指不同的尝试——真机那次是 `deepseek → glm`，终态 `reason=RateLimitError` 说的是 **glm 那一次**，而主模型那一跳的类型（`APIConnectionError`）只在 `model/fallback` 事件里。信息没丢，但终态那一行单独看会让人以为"主模型被限流"。要修得让终态带上模型归属（改载荷形状），本票不做。
- **fork 继承**：fork 出的子会话若种子日志尾部带 `run/failed`，Overview 会显示父 run 的归因（字段语义是"最近一个 run"，子会话确实继承了这个事件，ADR-0017 决策 3）。判定为可接受，未额外处理。

## 4. 后果

- 失败不再只有「失败」两字：已分类故障给出原因 + 处置建议，未分类给出类名 + 兜底句，保险丝类给出机器码。
- **新不变量**：`run/failed` 在每条运行期失败路径上都带 `reason`（§2.4）。它靠**逐路径用例**守（5 条路径各有断言），不靠 `failure_terminal` 的签名——那里加 `assert` 会把"漏归因"换成"失败路径自己抛错、连终态一起丢"，是更坏的交易。新增终态路径时请在这张表里补一行。
- 前端不持有第二份失败语义：`run_failure` 是 `run/failed.data` 的纯函数，重放（刷新 / 历史加载 / fork 子会话）与实时流给出同一值。
- 新增 UI 契约：`ConversationState.run_failure` 的形状、失效规则与两处呈现的同规则要求（2.2 / 2.3）——改 `projectRunFailed` 或 `StepDetail` 那行的人都必须同步。
- 跨仓契约面：`run/failed.data.reason` 的取值登记在 `docs/BACKEND_CONTRACT_STREAMING_UI.md` §4（前端线按它实现），本 ADR 是口径的完整叙述。
