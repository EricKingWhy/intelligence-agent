# Issue #642 阶段二修复调研：来源身份信任（C-6）方案依据

- 日期：2026-10-06（读取日期）
- 分支：`codebuddy/642-summary-trust`（worktree `intelligence-agent-wt-642`）
- 依据：`docs/agents/642-evidence.md` 阶段一证据 + 用户四项裁决（裁决 1–4）
- 本文件 = 协议 §1.3 要求的「方案依据」块；豁免判断：C-6 是消费边界契约变更
  （设计类），不属纯缺陷修复豁免范围，故落本块。裁决 2/3/4 为特征锁定与小修
  （纯缺陷修复 / 纯测试），不重复落块。

## 方案依据（协议 §1.3 / AGENTS.md §6.1）

### 来源 ①：Anthropic compaction 的 API 层类型化载体

**来源**（两处互证，同一机制的两个独立发布面）：

1. Anthropic Claude Platform Docs — *Compaction on demand*（2026-09-04 更新）：
   `https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand`
2. AWS Bedrock 官方文档 — *Claude messages compaction*（2026-07-18 / 2026-08-28）：
   `https://docs.aws.amazon.com/us_en/bedrock/latest/userguide/claude-messages-compaction.md`
   （对 Anthropic `compact-2026-01-12` / `compact-2026-09-04` beta 机制的转述，
   与 platform docs 一致）

**机制摘要**：摘要身份由**类型化载体**携带，绝不靠正文前缀认：

- 摘要是响应 `content` 数组里的一个 `{"type": "compaction", "content": ...}`
  **content block**（不是靠 `"## 某标题\n"` 文本前缀识别）；
- `compact-2026-01-12`（自动触发，`context_management.edits` 策略
  `compact_20260112`）：后续请求把含 compaction block 的响应原样 append 回
  `messages`，服务端按 **block 的位置/类型** 丢弃其前的一切内容——"The API
  automatically drops all message blocks before the `compaction` block"；
- `compact-2026-09-04`（按需触发，顶层 `compaction` 参数
  `{"type": "summarize"}`）：block 增加 **opaque `signature` 字段**，采纳
  （adopt）时必须逐字节回传该 block，服务端无状态校验签名；篡改即
  400（`compaction block content does not match its signature`）。
  "The server keeps no state between requests" —— 无状态下信任完全由
  签名（密码学身份）承载；
- 摘要正文 `<summary></summary>` 标签只是默认 summarization prompt 的**约定**，
  不参与身份判定。

**对本票的含义**：合法摘要的身份来自「它由谁产生」（事件/签名），消费端按
载体身份信任；伪造者可以复制文本，但复制不了事件链身份。本仓的对应物不是
密码学签名（超出本票范围），而是**已有的结构化身份源**：`derive_messages_
with_source_ranges` 给 bracket 投影摘要分配的 `source_range`（指向
`CONTEXT_COMPACTED` bracket 区间的确定性投影产物）。

### 来源 ②：内容嗅探是已知可利用模式（修 pattern 不修个例）

**来源**（规范 + 权威参考）：

1. WHATWG Fetch 规范 — *X-Content-Type-Options header* /
   MIME Sniffing 算法：`https://fetch.spec.whatwg.org/#x-content-type-options-header`、
   `https://mimesniff.spec.whatwg.org/#mime-type-sniffing-algorithm`
2. MDN — *X-Content-Type-Options*（读取日期 2026-10-06）：
   `https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/X-Content-Type-Options`

**机制摘要**：浏览器历史上靠"看内容猜类型"（MIME sniffing）决定如何对待
一个响应——这与"看前缀认摘要"同构：**信任决策建立在攻击者可控的内容上**。
可利用性是实测成立的（用户上传内容 + 嗅探 → 被当 HTML 执行 → XSS），业界
的修正方向不是"把嗅探规则写得更准"（修个例 pattern），而是**给内容一个
不可伪造的类型/来源声明并要求消费端尊重它**（`X-Content-Type-Options:
nosniff`：声明缺失或不符时直接拒绝/降级，不再从内容推断）。MDN 原文要点：
"the browser will not interpret it as HTML, even if the content contains HTML
markup"——身份与内容彻底分离。

**对本票的含义**：`_programmatic_summary_sections` 靠
`content.startswith("## 原始目标与用户约束\n")` 嗅探"这是合法摘要"并给予
结构化继承特权 = 内容嗅探做信任决策。修复方向与 `nosniff` 同构：不再从
内容推断身份；身份只来自结构化载体（source_ranges/bracket 事件身份）；
**有身份 → 结构化继承；无身份 → 降级为普通文本开采（不拒绝，无特权的
默认通道）**。同时按"修 pattern 不修个例"纪律：不给字符串再加一个可伪造
标记（票面明令禁止按 marker name 收窄识别）。

### 契合点（与 AGENTS.md §7 不变量的兼容）

- **不变量 #3（Session append-only typed SessionEvent）**：信任源 =
  SessionEvent 里的 bracket 事件身份，事件流零改动；
- **不变量 #6/#7（Persistent History ≠ Runtime Context；完整保存 ≠ 完整注入）**：
  无来源摘要降级为普通文本开采，原文仍在持久历史与 LLM 转录里，不丢失；
- **不变量 #14（UNKNOWN 不盲信）与 §4.3 Security（Runtime 边界不靠 Prompt）**：
  结构化继承特权从"内容前缀"收窄到"事件身份"，是消费边界（Runtime 层）的
  收紧，与 T12h `source_range_unavailable` 的 fail-closed 方向一致；
- **兼容层四个消费点（642-evidence §3）不删除**：`_is_compaction_summary`
  的识别语义继续服务 early 窗口边界、turn 计数、清单锚块落点、dry-run 守卫
  ——本票只改 `_programmatic_summary_sections` 的**结构化继承**入口判据；
- **生产行为**：builder 恒传 `events`（`builder.py:741`），derive 投影的
  bracket 摘要带 source_range ⇒ 生产路径全部合法摘要继续结构化继承，行为
  逐字不变；受影响的只有直连调用面（tests/程序化调用方），按裁决 1
  「有来源才结构继承；无来源降级为普通文本开采」执行。

### 判定

**PORT DESIGN**（借鉴来源①的身份载体思想 + 来源②的 nosniff 原则，Python
自有实现）：不引入任何上游代码/License 义务（两来源均为文档/规范，零代码
复制）。复用本仓已有的 `derive_messages_with_source_ranges` 产物作为身份
通道，`compact()` 内新增 trust 判定 helper，改动面限于
`src/agent_harness/context/compactor.py`。

## 兼容策略（票面要求的"有来源/无来源"明确口径）

直连调用面核实结论（票面 AC：先核实所有直接调用 `compact()` 的现有
tests/API 调用方式）：

- **生产调用面**：唯一调用点 `builder.py:738`，恒传 `events=session.events`
  （+ pruner 路径传 `source_ranges`）⇒ 恒有来源 ⇒ 结构化继承语义不变。
- **直连测试调用面**（grep `\.compact\(` 全仓）：`test_compactor.py`（8 处）、
  `test_compaction_bracket.py`（3 处）、`test_compactor_identifiers.py`（1）、
  `test_compactor_section_parse.py`（2）、`test_compactor_no_early_turn.py`（2）、
  `test_compactor_source_rejection.py`（5，全部传 events）、
  `test_protected_facts.py`（1）、`test_concurrency_gate_scope.py`（3）。
  其中**没有任何一个直连测试**把先前八节摘要放进 messages 依赖结构化继承
  （该路径此前只被 builder→builder 的二次压缩真实走到，属生产路径）；
  `test_compaction_bracket.py::test_second_compaction_merges_previous_summary_
  after_reload` 走 builder（有 events）⇒ 不受影响。
- **兼容策略（用户已定，fail-safe 不 fail-closed）**：
  - 有来源（events 在场 + 消息 source_range 命中 `CONTEXT_COMPACTED` bracket
    区间 + `_is_compaction_summary` 认可）⇒ 结构化继承（[6]/[7]，裁决 4 后
    [1] 不再从消息继承）；
  - 无来源（`events=None` 直连、或区间不可用/不对齐）⇒ 该消息降级为普通
    文本开采（`visit()`），**不拒绝、不报错**；
  - 无来源摘要里的模式可开采内容（如 `R-042`）仍会经 `visit()` 进 [6]/[7]
    ——这是与所有普通会话文本同权的开采，不再享有特权通道；模式不可开采
    的条目（如无扩展名路径、无数字连字符 token）不再进入，即伪造与继承
    特权一并消失。
