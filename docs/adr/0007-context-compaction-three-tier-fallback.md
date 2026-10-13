# ADR-0007: Context Compaction 三层降级 + tiktoken 精确计数

**Status**: Accepted（子决策 2 已于 #348 部分修订，见下方修订注）
**Date**: 2026-09-04  
**Phase**: 5 (Artifact + MinIO + Context Compaction)

## Context

规格 06 §5 要求 Compaction 在 `auto_compact_threshold` (0.70) 触发、`hard_guard_threshold` (0.85) 硬限，且"Summary LLM 失败时应有 deterministic fallback；hard guard 下 compaction 仍失败则阻止继续"。

两个子决策需要冻结：

### 子决策 1：Token 计数方式

四个候选：(a) tiktoken 精确、(b) 模型自带 tokenizer、(c) chars/4 粗估、(d) 绝对字符阈值。

### 子决策 2：摘要生成的降级策略

LLM 摘要可能失败（模型超时、格式不对、拒绝生成）。失败后怎么办？

## Decision

### 子决策 1：tiktoken cl100k_base 精确计数

`estimate_tokens(text: str) -> int` 用 `tiktoken` 的 `cl100k_base` 编码。

- `max_context_tokens` 默认 **200000**（覆盖 Claude 200K / GPT-4o 128K 的上界）
- auto_compact_threshold = 0.70 × 200000 = 140000 tokens
- hard_guard_threshold = 0.85 × 200000 = 170000 tokens

### 子决策 2：三层降级链

```
1. LLM 结构化摘要
   ↓ (LLM 失败/超时/格式错)
2. Deterministic 机械提取
   ↓ (机械提取后仍超 hard guard)
3. 抛 ContextWindowExceededError → Runtime 停止 loop
```

> **2026-09-29 修订（#348 落地失败契约）**：**第 2 层的"机械提取"从未按上图形状实现**——
> 实际降级是**保留原投影**（不伪造一份截断拼贴，摘要失败 ≠ 压缩发生）。#348 把这一
> 实际语义冻结为契约并补齐失败面：① 摘要生成至多**两次尝试**（预检不算尝试），
> 每次失败落一条有界 `CompactionFailure`（`error_class` 十类词表 + `message` 只装
> 本项目文案或异常类型名，绝不透传 provider 回显）⇒ 调用方（builder）落成任务可见
> 状态 `context/compaction_failed`（规格 06 §8"每次失败原因留诊断与任务可见状态"）；
> ② 两次都失败时旧投影仍在硬护栏内 ⇒ **安全继续**（原投影 + 失败记录，下一稳定边界
> 再评估）；已超硬护栏 ⇒ 抛 `ContextWindowExceededError`（携带失败记录）。③ 第 3 层
> 的"Runtime 停止 loop"随 #348 改为**非终态暂停**（`run/paused`，
> `trigger_dimension=max_context_tokens`，ADR-0033 边界 5）——"停止"不再意味着终结。
> 便宜摘要模型接缝（`summary_model`）同票就位，缺省 = 主模型，装配留后续票。

> **修订注（2026-09-29，W-29 / #383：进度清单重注入锚点）**：PRD §4.6 的
> 「压缩后确定性重注入」落地。机制决议（实现细节以 `context/builder.py` /
> `context/compactor.py` 为准，此处只写**为什么**）：
>
> 1. **重注入是 build 层的 ephemeral 块，不是持久化事件**——清单已经是
>    `task/plan_updated` 事件流（W-26），`derive_plan` 纯投影天然穿透压缩
>    （plan 事件不进 shadow 区间判定的投影集合）；缺的只是把投影结果放回
>    模型可见上下文。每次 build 重新决策、重新渲染、随用随弃，与运行时快照
>    同一哲学（不写 JSONL、不进 derive_messages、不进记忆抽取）。
> 2. **决策 = 纯事件流函数**（durable seq，票面硬约束）：无清单不注入；
>    会话发生过压缩（存在 `COMPACTION_END`）**恒注入**——清单是重注入包里
>    排在摘要之前的一环，"接班后必须持续在场"是判据①/W-30 判据 1 的耐久
>    读法；未压缩过走「事件驱动（距最近变更 ≤1 条投影消息）+ 周期兜底
>    （≥N 条后恒注入，N 缺省 6 = Cline Focus Chain 默认值，可配置）」，
>    中间静默窗是刻意的 token 经济。计数不读 wall-clock、不持实例状态——
>    重放/换实例决策一致。
> 3. **落点顺序**：清单块（SystemMessage，标题含 `context/plan` 标记）插在
>    第一条压缩摘要之前；尚无摘要时插在开头系统块区。标题刻意避开
>    `_is_compaction_summary` 的识别前缀与八节标题——注入块与持久化摘要绝不同形。
> 4. **成本口径**：清单 token 在阈值判定**之前**计入估算（不计数 = 系统性
>    低估），压缩路径计入 provider 预算补回项；`_last_plan_tokens` 独立记账
>    （"独立预算"的落点），看板折进"其他"桶（实测值非残差，不破坏六桶求和）。
> 5. **摘要第 5 节联动校验**（PRD §6.1 表行 5，W-04 留下的空接缝）：会话有
>    清单时，每个 in_progress 项的 `content`/`activeForm` 必须逐字出现在
>    第 5 节；零 in_progress ⇒ 第 5 节必须是 `(none)`；无清单不启用（既有
>    行为逐字节等价）。判据用逐字子串而非语义比对——PRD 执行约束要求确定性
>    机制兜底。拒绝走 `_SummaryRejected("plan_section_mismatch")`，进 W-04
>    既有失败通道（重试 ×1 → 双失败安全继续）。同票补上：`compact()` 成功
>    路径此前漏传 `failures`，首试被拒、重试成功时失败记录被静默丢弃——
>    与 `CompactionResult.failures` 自述契约及 PRD §4.5"每次失败留任务可见
>    状态"相悖，判据测试暴露后补齐。


**第 1 层 — LLM 摘要**：取早期完整 turns，送给同一个 ModelProvider 的 `ainvoke()`，prompt 要求产出结构化 summary（至少保留 facts / decisions / constraints / failed_attempts / unresolved / artifact_refs / citations / important tool outcomes——spec §5 列举）。产出为一条 `HumanMessage` 注入 messages 头部。摘要来自历史用户、模型和工具内容，属于不可信上下文，不得提升为系统指令。

**第 2 层 — Deterministic fallback**：不用 LLM。机械提取：
- HumanMessage → 原文截断保留（前 200 字符）
- AIMessage → 保留 tool_calls 列表（丢 content）
- ToolMessage → 保留 `tool_call_id` + content 截断（前 100 字符）
拼成一条 `HumanMessage` 注入头部。信息损失大但零失败面。

> **2026-09-29 用户批准的修订（#346 信任边界与边界预算）**：压缩摘要以及 ProtectedFact 的值都作为 `HumanMessage` 上下文注入；只有静态运行时策略和授权规则使用 `SystemMessage`。摘要即使经过校验，也不能获得系统指令权限。append-only 事件流仍保留所有系统 run 边界，但模型上下文（包括压缩摘要的事实表）只投影 `source_seq` 最新的一条 `work_boundary`；其余保护事实按 #346 注册表完整投影，避免重复 run 状态累积耗尽独立预算。此处为机制和决议的完整记录；PRD 给出简洁契约，W-02 只保留本 ADR 指针。

**第 3 层 — Hard guard 拒绝**：两层降级后 token 估算仍超 `hard_guard_threshold` → 抛 `ContextWindowExceededError`。Runtime 捕获后终止当前 run（spec §8："必须停止或要求用户处理"）。不继续发送超窗口请求。

## Rationale

### 为什么 tiktoken 而非 chars/4

- **200K 窗口下粗估不可接受**——chars/4 在大窗口下误差可能达 4 万 token。要么过早压缩浪费窗口，要么过晚压缩直接超限报错。生产级 coding agent（Claude Code / Codex）都用精确 tokenizer。
- **对非 OpenAI 模型是 ~10% 近似**——cl100k_base 对 Claude / DeepSeek 的 token 数是合理近似（偏保守方向）。未来换 Claude 原生 tokenizer 是 `estimate_tokens` 一行改动。
- **无厂商锁定**——`tiktoken` 是 OpenAI 开源的小包，不依赖任何 API。

### 为什么三层降级而非两层

- spec §5 同时要求"deterministic fallback"（第 2 层）和"hard guard 阻止"（第 3 层）。两层降级链只满足其中一个。
- 第 3 层是安全底线——不能让一个 LLM 摘要失败就把超大 context 发给模型（会直接 API 报错或截断）。

### 为什么 Compaction 以 AIMessage 为原子边界

- spec §5 硬约束："不能拆断 AI tool_call 与对应 ToolResult"。AIMessage(tool_calls=[...]) + 紧跟的连续 ToolMessage 块是一个不可分割单元——要么全压、要么全留。
- derive_messages 已经把 events 投影成配对好的 message 序列，Compaction 遍历这个序列按 AIMessage 边界切分即可，不需要重新做配对逻辑。

## Consequences

- `ContextBuilder` 构造参数：`model_provider` / `max_context_tokens=200_000` / `auto_compact_threshold=0.70` / `hard_guard_threshold=0.85`。
- 新增 `estimate_tokens()` 函数（tiktoken 封装）。
- 新增 `ContextWindowExceededError` 异常。
- 新增 `context/compacted` SessionEvent（记录 compaction 发生 + `fallback_used: bool`）。
- `tiktoken` 加入 `requirements`（核心依赖，非可选——Compaction 是 Core 能力）。
- Runtime loop 第 1 步从 `session.derive_messages()` 改为 `context_builder.build(session)`。

## 修订注（2026-10-12，用户裁决 A）

#1026 修复引入的**确定性截断降级**（`_deterministic_truncation`，摘要双失败且有效
用量超硬护栏时按 AIMessage 原子边界截断最早轮次、压回 auto 预算内，fallback 标记
可观测）**部分回调**本 ADR 第 2 层修订与 #348 冻结的「摘要失败 ≠ 压缩发生（不伪造
截断拼贴）」语义。回调范围严格受限，两条前提缺一不可：

1. **仅限分块/缩墙钟之后仍彻底失败**：摘要请求已经过规模缩放墙钟、分块递进归并
   与两次 attempt，全部失败且调用方有效用量已超 `hard_guard_threshold`——即第 3 层
   hard guard 的既有出口（`ContextWindowExceededError` → run/paused）是唯一剩余路径
   时，才允许确定性截断作为降级产物介入；
2. **截断后不发送超窗请求**：截断预算把 messages 压回 `auto_limit` 内（非 messages
   开销单扣），spec 06 §8 的 hard guard 冻结条款（"必须停止或要求用户处理，不能
   继续发送超窗口请求"）不受影响——截断不是"压缩发生"（无摘要、无 bracket、
   `compacted_turn_count=0`），而是失败语义的第三条出口：既不伪造摘要、也不必然
   停止会话。

三家成熟产品的同构设计背书（"按轮次/原子边界确定性截断最早历史"是业界正式的
history reduction 策略，非权宜 hack）：

- **Microsoft Semantic Kernel `ChatHistoryTruncationReducer`**：历史超限时按 safe
  boundary 消除最早消息（不拆 function call/result 配对），truncation 是与
  Summarization 并列的正式 History Reduction 策略；
- **MemGPT**（arXiv 2310.08560）：主上下文按 FIFO 队列管理，eviction 触发时截断
  队首消息（或递归摘要），被逐出内容仍可检索；
- **LangChain `trim_messages`**（strategy="last"）：保留最近消息、丢弃最早；
  `allow_partial=False` 不拆分单条消息/轮次，trim 后仍超窗才显式报错。

B 路线（保留原文继续发送超窗请求）违反 spec 06 §8 冻结条款且无任何一家产品背书，
未采纳。实现落点：`src/agent_harness/context/compactor.py`（#1026 分支
`codebuddy/1026-compaction-timeout`）；失败面经既有 #348 有界失败通道
（`deterministic_truncation_fallback` 诊断，attempt=0）落任务可见状态。

## 修订注（2026-10-12，用户裁决 R1，#1041）

spec 06 §8 原文「Summary 失败：保存 raw artifact，走 fallback summary」对**失败出口
的落盘面**存在字面歧义（"走 fallback summary"与 #1026 的确定性截断不产生摘要之间
错配）。用户 **2026-10-12 17:48 裁决读法 = R1**：**摘要失败的三条出口中，"抛错停止"
与"截断降级"两条落盘原始早期窗口全文的独立快照；"安全继续"出口不落盘。** 判据是
"早期原文是否离开模型视野，或会话是否因失败受损"，**且存在可存的 early 原文**——
early 为空的出口（如 `no_compactable_early_turn`：无可压缩完整早期轮、无原文可存）
即便会话因失败受损也不落盘：

1. **落盘（§8/R1 的独立快照前提成立）**：
   - ① 双失败且不可截断 ⇒ `ContextWindowExceededError`（hard guard，run/paused）；
   - ② 双失败后走确定性截断降级（`fallback_used=True`，早期原文被压出模型视野）；
   - ③ 预检拒绝 + 无法缩小 + 有效用量仍超 hard ⇒ `ContextWindowExceededError`
     （会话受损、早期原文离开模型视野）——同为"抛错停止"出口的变体。
   三者都把**原始 early 窗口 `messages[prefix_end:cut]` 的完整转录文本**（含"截断
   保留 + 逐出"两部分，是"逐出集"的超集、非仅被逐出集）落成 raw artifact，ref 回填
   进 `context/compaction_failed.artifact_ref`。
2. **不落盘（独立快照前提不成立）**：
   - 双失败但有效用量 ≤ hard 的**安全继续**出口——旧投影逐字保留、早期原文仍在模型
     视野内、且仍在 append-only 事件流里可原样回放，会话未受损 ⇒ 无需独立快照，落盘
     只是每轮触发的无界冗余写。
   - 会话虽因失败受损、但 **early 为空、无原文可存**的出口（`no_compactable_early_turn`：
     无可压缩完整早期轮）——"存在可存 early 原文"不成立 ⇒ 无原文可落盘。

**背书**：Aider（raw 对话历史无条件追加落盘，独立于摘要）与 LangGraph checkpointer
（完整状态历史持久化，摘要替换当前状态后原文仍可经 checkpoint 回溯）同证"raw 原文
恒久留存"原则；与 ADR-0007 第 2 层修订、#348「摘要失败 ≠ 压缩发生」一致。

**发现入口**：raw artifact 无 `artifact/created` / `artifact/externalized` 事件，其唯一
发现入口是 `context/compaction_failed.data["artifact_ref"]`——该载体已登记进 artifact
可达引用注册表（`session/service.py::_artifact_refs_in_event`，标签
`compaction_failed:<seq>`），故该快照进入 retention 可达集、在清理预览里判 `referenced`
（阻断删除），不被当作"未引用孤儿"误删。

**生命周期（有意设计）**：该 raw 快照经引用注册表登记为 `referenced`，同时阻断
`preview` / `execute` 两条清理路径，**既无 GC、也无显式人工覆盖/回收入口**——每个未被
内容哈希去重的新失败窗口都会新增一个随 session 命名空间存续的不可清对象。这是有意的
产品取舍（与上述发现入口同源）：快照供**事后审计与人工恢复**，宁可牺牲长期 session 的
空间回收，也不让审计原件被自动清理误删。

实现落点：`context/compactor.py`（失败分支带出原始早期窗口全文）、`context/builder.py`
（builder-owned 落盘 + ref 回填，fail-open、绝不伪造）、`session/service.py`（引用注册表
登记）。
