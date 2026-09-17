# ADR-0034 — 请求侧模型标识落 durable 事件（`run/started.data.model`）

- **Status**: Accepted
- **Date**: 2026-09-17
- **Deciders**: 本 Agent（机制设计）；开票与验收见 Issue **#226**（来源：DSH 设计对照审计 `docs/RESEARCH_DSH_PATTERN_GAP_AUDIT.md` §3 T1）
- **Related**：Issue #226；`src/agent_harness/session/session.py:begin_run`；`src/agent_harness/agent/runtime.py:_drive`（`begin_run` 调用点）；`src/agent_harness/model/config.py`（`ModelConfig.model_name` 的来源）；`src/agent_harness/model/provider.py:107`（`model` 字段真正进请求体处）；`src/agent_harness/session/model_switch.py`（会话级模型选择：`session/started` / `model/changed`）；`web/src/lib/projection.ts`（`projectRunStarted`）；`web/src/components/StepDetail.tsx`（ChatTab 的 MODEL 一节）；`docs/BACKEND_CONTRACT_STREAMING_UI.md` §2；ADR-0014（Model Fallback 决策 14-16）；ADR-0016 §5（会话级模型选择）；ADR-0032（自定义模型供应商）；ADR-0033（失败归因，同属"事件里必须能读出发生了什么"）；AGENTS §16.1

> 本文档是「请求侧模型标识」这条机制的**唯一完整叙述**。代码注释只写各自那一段代码自己看不出来的操作约束 + 指向本文件的一句指针。

---

## 1. Context

### 1.1 事件流里有两个"模型名"，此前只有一个能查

durable 历史里与模型有关的记录有三处，但**没有一处记着"这一轮实际请求的是哪个模型"**：

- `session/started.data.{provider, model_id}` 与 `model/changed.data.{from_*, to_*}`（`session/model_switch.py`）——
  记的是**会话级选择**（picker 的 catalog 名，`model_id=None` = 走默认链）。它是**别名级**事实，
  不是发出去的那个 `model` 值：catalog 条目名与上游 `model_name` 可以不同（ADR-0016 §5 / #203）。
- `model/completed.data.model`（`runtime.py:_model_name_from_response`）—— 记的是 provider 在**响应**里
  **回显**的模型名。**对方不回显就没有这个键**：网关 / 自建端点（ADR-0032）下等于什么都没有。
- `model/started.data`（`runtime.py`）—— **流式专属（`STREAM_ONLY_TYPES`），永不落盘**
  （`session/event.py:164-166` 明确写"never appended to the durable log"，`Session.append`
  还会拒绝它）。它**帮不上 durable 可审计性**：刷新之后什么都不剩。
- （同值但不算"记录"：每 run 的 `run_config` **结构化诊断日志**里已有 `model_id`
  = `self._primary_model_name`。它不进会话事件流、不参与重放、界面读不到——按不变量 #4
  的分工，诊断日志不是可重放的会话历史。§4 登记这处并存。）

真实症状：一次 run 失败（甚至成功）之后，只读事件流**说不出这一轮请求的是哪个模型**——
在 provider 不回显的部署里，连"用了哪个模型"都说不出来；回退发生时只能靠 `model/fallback`
旁证（ADR-0033 §3 已记过相邻问题：终态 `reason` 只指最后一次尝试）。

### 1.2 DSH 的做法（本票来源）

DeepSeek Harness 把模型选择放进**已记录的请求信封**：`request/header` 事件的载荷是
`EpochHeader`＝"call configuration (**provider, model, reasoning effort**, and sampling scalars)"
（现场复核 `docs/subsystems/session.md:169-204`），按 `initial`/`resume`/`change`/`series`
追加快照，`foldRequestHeader(events)` 取最新快照重建；后续 turn/step/重试**继承最新快照**。
即：请求与回答两侧**都在日志里**，回放/审计两边都能看。

---

## 2. Decision

### 2.1 记在 `run/started`，值是"发出去的那个 model"

`run/started.data.model` = 本轮**请求侧**模型标识，由 Runtime 在 `session.begin_run(...)`
时传入 `self._primary_model_name`——装配层给的就是 `ModelConfig.model_name`
（`assembly.py`），而它正是进请求体的那个 `model` 字段（`model/provider.py:107`）。

措辞边界（两轴审查 P3）：它是 **run 级"配置侧"事实**——本轮若一次模型调用都没发生
（上下文超限在调用前就炸、或用户在建流前取消），这个值仍是"打算用谁"，**不代表它真被
发出去了**。所以本文与跨仓契约都写"请求侧标识"，不写"实际用过的模型"（后者只有
`model/completed` 的回显/`model/fallback` 能证明）。

选 `run/started` 而不是 `model/started` 的理由：

1. `model/started` **不持久化**（§1.1），往里加字段解决不了 durable 可审计性——这正是本票
   初稿写错的地方（见 §3 的记录）；
2. run 是**调用配置的天然变更点**（每个 run 一份 runtime，`_drive` 里 `begin_run` 一次），
   与 DSH"按 change 追加快照、后续继承"同构，而且不需要新事件类型
   （新增事件类型会牵动跨仓生成的词汇表产物）；
3. **失败路径也有**：`model/fallback` 记录了 run **内**的切换（from → to），
   `run/started.model` 记录了起点 ⇒ 两者合起来，任何一次模型调用的请求侧模型都可重建——
   连"run 失败在第一次调用"的情形也能说出请求的是谁（与 #222 的失败归因配合）。

### 2.2 「键缺席」与「键在场但为空」是两种事实

`Session.begin_run(model=None)`（未给）⇒ **不落该键**；给了就落。
本方法只写调用方给的事实，**不替调用方编默认值**。

- 装配路径（`assembly.build_runtime`）**总传真值**：`_single_from` 保证 `model_name` 非空
  （显式配置优先，否则厂商预设；两者皆无则装配期响亮失败）。
- 直接构造 `AgentRuntime`（既有单测、极少数嵌入用法）的缺省名是占位串 `"primary"`——
  它会被如实记下来，读的人看到 `"primary"` 应理解为"调用方没给真名"，而不是某个真实模型。

### 2.3 前端两侧不互相冒充

`ConversationState` 新增两个字段：

- `requested_model`（来自 `run/started`）：它是「**最近一个 run**」的镜像——新 run 开始即
  重置；本 run 没带该键就归 `null`（**不保留上一轮的值**：旧版后端/降级部署下"字段缺席"
  必须读作"未知"）。
- `model_run_id`（回显侧的**归属**）：`model` 这个值由哪个 run 写入的。**没有它就无法判断
  两个值能否并排比较**——`model` 的写者有四个（`model/completed` 回显、`model/fallback`
  切换、`model/changed` 会话级切换、重放重建），**谁都不随新 run 失效**，而
  `requested_model` 是每 run 重置的。写成 `null` = 该值来自不带 `run_id` 的事件
  （会话级 `model/changed`）。

ChatTab 的 MODEL 一节据此渲染：

| 情形 | `模型` 行 | `请求模型` 行 |
|---|---|---|
| 本轮有回显、与请求一致 | 回显值 | **不渲染**（重复一行无信息量） |
| 本轮有回显、与请求不同 | 回显值 | 渲染（**同一 run 的真对照**：请求 ≠ 回显） |
| 本轮无回显、`model` 是**别的 run** 写的 | 回显值 + 后缀「（非本轮）」 | 渲染 |
| 本轮无回显、`model` 为空 | 「—」 | 渲染 |

「（非本轮）」这个后缀是两轴审查共识的一条 P2 修复：没有它，"模型 A / 请求模型 B" 会被
读成"请求了 B 却回显 A（provider 无视请求）"，而 A 其实只是**上一轮**的值（run 1 回显后，
run 2 在首次模型调用前失败/取消，或换成了不回显的 provider——后者正是本票要服务的场景，
于是这个误导会**长期**留在界面上）。取值比对用的是 `model_run_id !== run_id`
（`run_id` = `projection.ts` 在 `applyEvent` 里统一维护的"最近一条带 run_id 的事件"）。

该节"是否空槽"的判据把 `requested_model` 也算作真值：不回显的部署里它是这一节唯一的
模型事实，若还显示空槽提示，用户会以为连请求都没发出去。

**未做（登记）**：`model` 本身仍是**会话级**的（不随 run 重置，`usage_total` 与之同层）。
把 `模型` 行做成 run 级会改动既有语义（`types.ts` 已冻结的描述）且牵连 `model_fallback`
的失效规则 ⇒ 本票不碰，只把差异在界面上标出来。

---

## 3. 本票初稿写错的地方（记下来防重犯）

初稿的标题与修法都是"给 `model/started` 加字段"。**动工前核对代码才发现该事件是
`STREAM_ONLY_TYPES` 成员、永不落盘**（`session/event.py:164-166`；跨仓契约
`docs/BACKEND_CONTRACT_STREAMING_UI.md` §2 的表格**早就写着** `❌（stream-only）`）。
按初稿实现会得到一个"看起来改了、刷新后什么都没有"的假修复。

教训与 `docs/RESEARCH_DSH_PATTERN_GAP_AUDIT.md` 文末那条同源：**缺口结论必须先核对该事件
是不是 durable**，再谈往哪儿加字段。本票已按实测改写（选择 `run/started`），票面 comment
记录了这次订正。

---

## 4. 未覆盖 / 按据不改

1. **provider / base_url 不落事件**：网关场景下两个不同 provider 的同名 model 无法从事件流
   区分，`model/fallback` 的 from/to 也只有名字。要修得动装配层与事件载荷两处，收益待评估 ⇒
   本票不做，登记在这里。
2. **`model/started` 保持原样**（只有 `step`）：它是活跃信号，给 live 消费者加请求侧模型名
   没有 durable 收益，且会多一处"同一事实两个落点"（§16.1）⇒ 不改。
3. **run 中途 `model/changed`**（T7 #137，会话级切换）：运行时按 run 装配，切换在下一个 run 生效
   ⇒ `run/started.model` 天然跟着变，无需额外处理。
4. **旧数据**：无该键的历史事件照旧可读（前端按"未知"降级，不报错、不补写）。
5. **`run_config` 诊断日志与新事件同值并存**（登记）：`runtime.py` 每 run 都往结构化日志写
   `model_id`（#198 起），与 `run/started.data.model` 是同一个值。它们服务于不同消费者
   （日志面向本机排障、事件流面向重放与界面），按不变量 #4 的分工不算"同一事实两个落点"；
   真要合并就是废掉日志那一处，本票不做。
6. **`model` 行的 run 归属只用于显示标注**：`model` 本身仍是会话级事实（见 §2.3 末）。
   把整节的粒度改成 run 级需要另一票（会动 `types.ts` 的冻结语义与 `model_fallback` 的
   失效规则）。
