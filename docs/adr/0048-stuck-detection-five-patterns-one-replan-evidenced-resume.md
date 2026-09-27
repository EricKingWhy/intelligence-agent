# ADR-0048 — Stuck 检测：五模式、恰好一次 replan、无依据不恢复

- **Status**: Accepted（实现随 T9/#317 落地）
- **Date**: 2026-09-27
- **Deciders**: 用户（#305 的产品与架构裁决，2026-09-23 冻结）+ 本 Agent（票面实现口径）
- **Related**:
  - 父 PRD **#305**、本票 **#317**（T9/12：「Detect stuck loops and require evidence to resume」）、
    前置 **#312**（T4：非终态 `run/paused`）、**#316**（T8：quiescence 完成闸门）、
    上游 **#313**（T5：四维 run ceiling）、**#315**（T7：绝对 deadline 与对账耦合）
  - **本 ADR 展开的正式规格**：`02_AGENT_RUNTIME.md` **§5.3**（五模式阈值表与五条规则——阈值的
    唯一权威，本 ADR **不复制**其数字）、`02 §6`（Repeated Tool Guard = 同一责任域）、
    `02 §5.2`（暂停是 durable 非终态、恢复不重置 counter 与 stuck 指纹）、
    `03_SESSION_EVENT_MODEL.md` **§3.4**（`run/paused` / `run/resumed` 字段与不变量）、
    **§5**（run 状态集合；「stuck 暂停缺少所需的变更依据 ⇒ 拒绝，且不启动任何 model / tool / child 工作」）
  - **ADR-0044 D5**（stuck 的规则与理由：唯一责任域 / 一次 replan / 指纹规范化脱敏 / 三类恢复前置 /
    只复位受影响模式）、**D9/D10**（409 在开工前判定；Live Gate 场景③）、**ADR-0014**（同错熔断护栏：
    本票**扩展**它）、**ADR-0047**（完成闸门：stuck 判定不得越过它）
  - 代码落点：`agent/guards.py`（扩展）、`agent/resume_evidence.py`（新）、`agent/run_budget.py`、
    `agent/runtime.py`、`session/event.py`、`session/service.py`、`prompt/builtin.py`、
    `prompt/section.py`、`assembly.py`

---

## 1. Context

### 1.1 票面要解决的问题

ADR-0014 的护栏只覆盖一个模式：**同一工具 + 同一参数连续失败**（3 次软 → 注入纠正消息；6 次硬 →
`run/failed(reason=identical_tool_failure_loop)` **终态**）。§5.3 把它扩成五个模式，并改掉两件事：

1. **硬熔断的收口不是失败，是暂停**——`02 §5.2` 明文：命中预算 / deadline / **stuck** 时 MUST 落
   `run/paused`（非终态），MUST NOT 落 `run/completed` / `run/failed` 顶替它。今天的硬臂落的正是
   `run/failed`，与冻结契约直接冲突；
2. **恢复要有依据**——stuck 不是一个"再试一次"就能解开的暂停。缺依据的 plain continue 与无关变更
   MUST 被 409 挡在开工前（`03 §5`、ADR-0044 D5/D9），否则"暂停"退化成"无依据一键重试"。

### 1.2 现状实测（本 ADR 写作时的读数）

| 事实 | 证据 |
| --- | --- |
| 护栏责任域只有一个，且已在 loop 内单点接入 | `agent/guards.py::RepeatedToolFailureGuard`；`runtime.py` 的护栏臂（工具回填后、下一轮模型调用前） |
| 硬臂今天落终态 | `runtime.py`：`_terminal_failed_run(..., reason=STATUS_IDENTICAL_TOOL_FAILURE_LOOP)`；`_terminal_failed_run` 的 docstring 自陈「`#312` 起只剩同错熔断硬触发一条路径」 |
| 暂停 / 恢复的账本、字段与 CAS 已齐备 | `agent/run_budget.py`（`PausedRun` / `build_pause_data` / `build_resume_data` / `validate_resume`）、`session/service.py`（`_plan_paused_resume` / `_commit_paused_resume`，两阶段同锁） |
| `run/paused` 已有 `resume_requirements` 字段，且**只有 stuck 用** | `build_pause_data(resume_requirements=...)`；`_terminal_paused` 恒传 `()`（`03 §3.4`：预算与 deadline 暂停为空） |
| `validate_resume` 今天**主动拒绝** stuck | `run_budget.py`：「暂停原因 reason=… 的恢复前置条件本票未实现（#317 stuck 负责），拒绝启动工作」 |
| 四类 `resume_basis` 常量已冻结，但只有 `budget_increase` 有证据判定 | `RESUME_BASIS_*`；`validate_resume` 对另外三值直接 409「本票不假装校验过它」 |
| 仓库里**没有**任何"环境 revision / policy 版本"概念 | `workspace_revision` / `env_revision` / `policy_version` 全仓无命中；`AgentSpec` 无 version 字段 |
| 决策级状态可纯派生 | `model/completed` 带 `content` / `tool_calls`；`tool/call` 带 `name`/`args`；`tool/result` 带 `{tool_call_id, content(ToolResult JSON), budget_delta}` —— 五个模式的输入全在事件里（T6 的 `consumed_from_events` 是同一做法的先例） |
| 一个 turn 的事件顺序固定 | `model/completed`（延迟落盘时）先于 `tool/result`，`tool/call` 预持久化在最前 |

### 1.3 本 ADR 的范围

**在**：五模式的判定与阈值读数；指纹的规范化与脱敏；进展证据的定义；replan 与暂停的**动作面**
（事件 + 注入消息 + `run/paused` 载荷）；三类恢复依据的观测口径与 409；重启 / 同 run 恢复的状态重建。

**不在**：SessionBudget 与委派树共享额度（`#318`）；`needs_reconcile` 与暂停的耦合（`#315` 已落地，
本票只复用）；客户端对 `resume_requirements` 的渲染（客户端票）；`max_steps` alias 的删除（`#320`）。

---

## 2. Decision

### D1 — 责任域唯一：扩展 `agent/guards.py`，检测状态**全部**由事件派生

- 五个模式同住一个检测器（`agent/guards.py`），loop 内**仍然只有一个接入点**。① 直接用既有的
  `RepeatedToolFailureGuard` 作引擎（其类名、`observe()` 形状与 `GuardLevel.SOFT` 语义不变），
  ②–⑤ 是同一检测器的其余四个子状态机。**不**新增第二个 loop guard、**不**新增存储。
- 检测器状态（各模式的连续计数、决策序列、进展水位）由 **SessionEvent 纯函数重放**得到：
  `StuckDetector.from_events(events, run_id)` 在每次执行开始时重建，随后 `advance(window)`
  只吃"本执行新落盘的那些事件"。⇒ 重启 / 同 run 恢复**天然**得到同样的指纹与计数
  （`03 §3.4` 不变量：恢复 MUST NOT 重置 stuck 指纹），无需任何"把内存态写进磁盘再读回来"的路径。
  重放时**丢弃**信号（当时那次 replan / 暂停已经落过盘），只有 `advance` 的新事件才产生动作。
- **唯一例外：委派树的持久护栏**（`10` / `#88`）。它的计数跨**后代会话**、跨**进程**，账本
  （`SqliteDelegationTreeLedger`）自己就是 durable 的——本 run 的事件流里只有"这一条 delegate
  失败了"，看不到子孙的失败。所以那一个来源不由重放重建，而是**照旧**由调用方在读 `runtime_signal`
  时翻译成本 run 的同一种信号（`guards.external_failure_signal`），并把它点名的 `tool_call_id`
  报给检测器**别在 ① 上重数**（`advance(externally_counted_call_ids=…)`）——同一批失败记两遍会让
  阈值提前到顶。**这不是"回到读进程内信号"**：那个信号后面站着一个真账本，重启后仍在（这正是
  persistent 的含义，killer 用例 `test_process_kill_and_recovery_…` 钉着它）；本 run 自己的
  `tool/failure-guard` / `guard/stuck` 事件仍由检测器按事件重放给出。

### D2 — 五模式与"第二次达到阈值"

`02 §5.3` 的表固定**首达**阈值 T。本 ADR 固定**暂停点**的读法：

| 模式（事件源的连续量） | 首达 T ⇒ replan | 再达 T（累计 2T）且无相关进展 ⇒ 暂停 |
| --- | --- | --- |
| ① 同动作 + 同错误/结果失败（**连续**同 (name,args) 的失败，中间不夹别的指纹） | 3 | 6 |
| ② 同动作 + 同观察（**仅成功结果**；失败被 ① 更严格地覆盖） | 4 | 8 |
| ③ 无进展独白（**无工具调用**的模型决策，连续同一决策签名） | 3 | 6 |
| ④ 两模式交替（连续 6 个决策在**恰好两个**决策签名间严格交替） | 6 | 12 |
| ⑤ 项目级无进展窗口（连续 N 个决策内**没有任何**进展证据） | 4 | 8 |

**为什么"再达 T"而不是"T+1"**（这是本 ADR 唯一的读数选择，另一种读法同样能引用 §5.3）：

1. §5.3 的规则句是「阈值首达 ⇒ 恰好一次 replan；replan 后同一模式仍持续且无进展证据 ⇒ 暂停」。
   "仍持续"没有独立数字，只能从阈值语言里取——**再次达到同一个阈值**是最小假设；
2. ②–⑤ 的"持续"若按 T+1 实现，等价于把表里的阈值当成"一次容忍"而不是"一个门槛"，模型在
   replan 后只要重复一次就被停住，纠正性 replan 事实上没有执行余地；
3. ① 因此与 ADR-0014 既有的软/硬两级（3 / 3+3）**逐字对齐**：软级就是 replan，硬级从
   `run/failed` 改成 `run/paused(reason=stuck)`——这正是 `02 §5.3`「扩展既有护栏」的字面执行。

### D3 — 指纹规范化与脱敏

- **动作指纹** `action_fp = digest(tool_name, canonical(args))`；`canonical(args)` = key 排序的 JSON，
  字符串值走**与观察同一套**规范化（见下条：trim + 换行统一 + 空白折叠 + 整数浮点归一 `1.0`→`1`）。
  ⇒ call id、key 顺序、等价参数抖动（含参数内部的多余空白）不构成新指纹。
  **参数不做"保守"处理**（只 trim 两端）：那会让"`ls  -la` / `ls -la` 交替重试"这类抖动被算成
  *换了个动作*，① 的计数被反复清零——`02 §5.3` 的"等价参数抖动 MUST NOT 构成进展"同时管住这两个
  方向（该折叠折叠下来，别让它既不算进展、又能逃出模式）。
- **错误指纹** = `error_code` + （规范化后的）失败 message。
- **观察指纹** = 规范化后的成功结果：`message` + `data`（**排除 `metadata`**——`duration_ms` /
  `attempt` 这类每次必变的字段一旦进指纹，"同观察"永远不成立，"等价观察"成了假命题）。
  规范化含空白折叠、ANSI 转义剥离、**文本若是 JSON 则重排为紧凑形式**（pretty / compact 同一指纹）。
- **脱敏**：任何要指纹化的文本先过 `agent_harness/redaction.py` 的 `redact_secret_values()`
  （**新增的公开函数**，此前这套正则在 `transport/contract.py` 内部；该模块仍经它做命令脱敏，
  只做凭证替换、**不做**路径 scoping——路径要保留，它是参数语义的一部分），**再取截断摘要**。
  落到事件与持久化里的只有摘要，明文（含凭证的原文）永不进指纹、永不进事件。
- 摘要只用于**相等比较**，不承担可读性：这是"凭证零泄漏"与"规范化"的同一条边界。

### D4 — 进展证据，以及"只复位受影响的那一个模式"

两个不同的量，不共用：

- **相关进展（按动作，复位 ①/②）**：同一动作出现**与本次模式记录不同的观察**（即状态真的变了——
  `02 §6` 的"edit 后再次 read"）。① 的实现在此**收窄 ADR-0014 的 Q12(b)**：同指纹的成功若带来
  **不同**观察，复位计数并清 `soft_triggered`；同指纹、同观察的成功仍**不**复位（防"振荡洗计数"的
  原意保留在等价观察这一侧）。§5.3 的"只有出现相关进展时才复位受影响的那一个模式"由此成立：
  别的动作成功不碰这条模式（①/② 的模式键里带着动作指纹，天然隔离）。
- **项目进展（按决策，复位 ⑤）**：本决策内出现**新的成功观察**（`ok=True` 且其观察指纹在本逻辑
  run 从未出现过）。失败**不算**进展（"又学到一个错误"不是项目往前走）。⑤ 的窗口计数只被它复位；
  ③ 的签名序列只被"签名变化"打断。
  **"是不是 MUTATING 调用"不进判据**（与本文早期草稿相反，以本节为准）：判 MUTATING 需要一条
  事件之外的事实——工具的能力声明（`Capability.side_effect` 之类），而那会让检测器不再 100%
  由事件派生（D1），重启后重放同一段事件就可能算出不同的计数。代价是"成功的只读调用也算进展"：
  一个反复 `read` 同一个文件、每次都能读到内容的 run 不会触发 ⑤——它触发的是 ①/②（同动作同观察），
  那一族本来就更贴切。取舍与残余 10 同源。

### D5 — 动作面：一次 replan、一条暂停

- **replan**：① 沿用既有的 `tool/failure-guard(level=soft)` 事件与既有纠正消息（`injected_by`
  仍为 `tool_failure_guard`），逐字不变；②–⑤ 落新的**结构化 guard 事件** `guard/stuck`
  （`level=replan`）+ 注入新的纠正片段 `corrective:stuck_pattern`（`injected_by="stuck_guard"`）。
  "恰好一次"由构造保证：计数 `== T` 才 replan，`> T` 只能走暂停分支，且没有任何路径能把计数
  在 replan 之后倒回 T（复位是清零，不是减一）。**同批跨过 T 与 2T 时 replan 优先**（`#317`
  T9 审查 P1）：`02 §5.3` 写的是"首达阈值 ⇒ 恰好一次 replan；**replan 后**同一模式仍持续 ⇒
  暂停"，所以一批事件里若同一个模式既到 T 又到 2T，这一批只执行那条 replan（纠正消息落盘、
  模型看得见），暂停留给下一次仍然重复的观测。原实现按"暂停优先"挑动作（`worst_stuck_signal`
  的级别序），会把同批的 replan 直接压掉——模型一条纠正都没收到，而 `replan_count` 照样写 1。
- **暂停**：**同一个模式**在本批没有发出它自己的 replan 的前提下越过 2T 时落
  `guard/stuck(level=paused)`，随后走既有的 `_terminal_paused`（对账闸门 → closeout →
  恰好一条 `run/paused` → `mark_terminal_written`）。
  "同一模式"是**逐字照规格**（`02 §5.3`：replan 后**同一模式**仍持续 ⇒ 暂停）：抑制是
  **按模式**的（`worst_stuck_signal` 只把"本批也发了 replan 的那个模式"的 paused 级信号
  从挑选里去掉）。跨模式同批（例如 ② 首达 T、① 同批越过 2T）**不构成抑制**——仍按级别序
  挑出暂停，那条 ② 的 replan 这一批不执行，登记为残余 12，本票不改成"批级优先"。
  `reason=stuck`、`trigger_dimension=<模式名>`、`resume_requirements` = 三类依据（D7）。
  **不再**落 `run/failed`：`STATUS_IDENTICAL_TOOL_FAILURE_LOOP` 这个终态从生产路径消失
  （常量保留，历史会话仍能读）。
- **暂停不得越过完成闸门**（ADR-0047）：模型的决策若通过 quiescence + policy，run 就是
  `run/completed`——stuck 判定**只**在"这一轮无法完成"时才决定后续（replan 继续循环 / 暂停）。
  Must Not Do 的"不得仅因首达阈值就完成或失败一个 run"由此同时落在两侧。

### D6 — 暂停载荷与 closeout

- `run/paused.data.stuck`（仅 `reason=stuck` 出现，缺席即旧形状不变）：
  `{pattern, threshold, count, replan_count, fingerprint, environment_revision, policy_inputs,
  policy_version}`。
  `resume_requirements` 非空，这是客户端唯一的"不能一键重试"刹车灯；它列的是**这条暂停
  当前真能接受**的依据（`run_budget.stuck_resume_requirements`：`relevant_steer` 常在，
  环境 / 策略两条看快照里那一格在不在），不是固定三条——列一条永远 409 的依据就是 `03 §5`
  / ADR-0044 D4 禁止的"暗示可安全续跑"（T9 二轮审查 P2：委派子 run 没有证据端口时快照两格
  `None`，而旧实现照样列三条）。确定性 continuation 与 closeout 指令都渲染**同一份**可用
  集合（`describe_resume_requirements`）。
- **`policy_inputs` 与 `policy_version` 是同一次计算的两种投影**（`#317` T9 审查 P1）：逐维值
  （权限档 / 模型 / profile / reasoning effort / context providers）必须一起落盘，恢复侧才能把它们
  还原回自己的 amend、再用**同一个** `policy_version_of` 重算。只记摘要时，恢复侧拿"本次请求声明
  了什么"去重算——未声明的维度两侧不同名，摘要必然不同 ⇒ **没变也会被判成"变了"**，
  `policy_change` 成了可伪造的依据（fail-open）。有了逐维值，两侧摘要**按构造**对称：省略
  不是变化。（恢复侧还原的优先级：本次请求声明 > 会话级模型切换 > 暂停快照；快照里的 model
  逐字还原、**不做**目录回落——回落会悄悄换掉策略面，把"没变"算成"变了"。）
  **模型这一维有一个例外**：会话在暂停之前就切换过模型（`model/changed`）时，那一跳优先于
  快照 ⇒ 不带声明的恢复会用会话当前模型现算，摘要与快照不同 ⇒ `policy_change` 被采纳。这不是
  fail-open：恢复后**生效的**模型面确实与暂停时不同（"变了"是事实）；只是"普通恢复恒等于
  暂停时那一套"这句话对模型这一维不成立——判断"切换发生在暂停前还是暂停后"需要一条事件之外
  的时间线，与残余 1"只能判变过、判不了更新"同类（T9 二轮审查 P3-4，行为不改、登记于此）。
  `context_providers` 记**原值**：`None`（未声明 ⇒ 装配全部 wired）与 `[]`（显式零 provider）
  是两种策略面，归一化会让"从全量改成零"看上去没变，而把 `None` 还原成 `[]` 会把恢复腿的
  provider 丢光（T9 审查 P2）。
- **"两格同源"是机械判据，不是注释**（`#317` T9 二轮审查 P1）：`resume_evidence.
  recorded_policy_inputs` 是唯一的读取口——它要求五维**形状合法**且
  `digest_policy_inputs(逐维值) == policy_version`，否则返回 `None`。判据侧
  （`run_budget.stuck_resume_evidence` 的 `policy_change` 臂）拿 `None` 当"快照还原不回来"
  直接 409，恢复侧拿 `None` 当"没有可还原的东西"——两侧同一个函数，不会一个放行一个不还原。
  这条同时挡住两类快照：**只记了摘要的存量载荷**（本票之前写入的 JSONL）与逐维值/摘要
  **对不上**的载荷（手改、跨算法漂移）。
- **还原源必须是判据的基线那一条**（同前）：`paused_policy_inputs` 取该 run **最后一条**
  `run/paused`（与 `latest_paused_run` 给 `validate_resume` 的那条相同）。同一个 run 可以
  暂停多次（一次合法的 `policy_change` 恢复之后再暂停），取第一条会让两侧比两个不同的快照
  ——"什么都没改"照样能靠两个快照之差拿到依据，而且恢复腿会跑回**第一次**暂停的档位。
- **确定性 continuation** 与**模型 closeout 指令**都要有 stuck 分支：不许出现"提高 ceiling 后恢复"
  这类暗示（stuck 不接受 `budget_increase`）。模型 closeout 仍按 `02 §5.2` 预留容量尝试一次，
  失败/不合契约回落确定性组装。

### D7 — 三类恢复依据：观测，而非声明

`validate_resume` 对 `reason=stuck` 只接受 `relevant_steer` / `environment_change` / `policy_change`，
且**每一条都要在现场被观测到**（用户声明不算证据）：

| basis | 观测口径 | 缺依据时 |
| --- | --- | --- |
| `relevant_steer` | 事件流里存在 `seq > pause_seq` 的 `steer/requested`，且它的 `run_id` 与这个 run **逐字相同** | 409 |
| `environment_change` | 用**同一个** `workspace_revision(root)` 重算比暂停快照里记录的值不同 | 409 |
| `policy_change` | 用**同一个** `policy_version_of(...)` 从本次恢复解析出的生效策略重算，比快照里的值不同 | 409 |

- `relevant_steer` 只判**存在性**（seq 在暂停之后 + `run_id` 逐字相同）：steer 的**内容非空**
  是入口层约束（Web 的请求模型 `Field(min_length=1)`；CLI 没有 steer 入口），**不是**恢复判据
  ——`SessionService.send_message` 本身不校验内容（T9 二轮审查 S4）。
- `budget_increase` 对 stuck 暂停 ⇒ **409**（"抬高 ceiling"不是依据，`03 §3.4` 的暂停理由不是预算）。
- 摘要没有全序，"更新"的机械口径只能是**不等于快照**；两侧都由服务端计算，客户端无从伪造。
  快照缺该值（无端口 / 字段缺失）⇒ 该依据不可用（fail-closed，409 明说"无快照可比"）。
- **不要求**请求给出绝对 ceiling（那是预算暂停的恢复契约）：未点名即沿用暂停快照，但仍过
  `resume_headroom_ok`——一次"恢复了却立刻再停"的 409 比一次假恢复诚实。
- 被采纳的依据写进 `run/resumed.resume_evidence`（`{basis, ...}`，仅 stuck 恢复出现）：
  "哪一条证据放行了这次恢复"是必须可对账的事实。预算恢复的 `run/resumed` 逐字不变。

### D8 — 证据端口：注入而非内建

`agent/resume_evidence.py` 提供 `environment_revision(root)`（工作区树清单摘要：相对路径 +
尺寸 + mtime_ns，按路径排序后摘要）与 `policy_version_of(...)`（生效策略摘要），以及把两者打包的
端口对象。端口在装配点（`assembly.build_runtime`）注入 runtime，runtime 只在**暂停那一刻**读一次；
恢复侧由 `session/service.py` 用**同一份函数**在现场重算。工具不内建、不缓存、不跨进程传递。

**子 run 只拿环境那一半**（T9 二轮审查 P2）：`AgentFactory` 收到的端口在构造 child
runtime 时剥掉策略两格（`policy_version` / `policy_inputs` 记成 `None`）——child 的生效
策略面（profile / effort / context providers / 模型名）Factory 根本收不到，照抄父级那份等于
给 child 记一个它没跑过的面，恢复侧就会拿"跨进 child 的假变更"放行（fail-open）。环境那一半
是真的：child workspace 就是父的同一棵树（`multiagent/provider.py` 的 alias），
`environment_revision` 两边算的确实是同一个量。于是子会话的 stuck 暂停**可经环境变更恢复**，
策略那条如实不可用（残余 15）。

**摘要输入集**（`policy_version_of` 的实参，两侧同源）：权限档、模型、agent profile、
reasoning effort、context providers（排序后）。两个**刻意排除**项，都是 fail-open 的防线：

- `auto_approve` —— 它是审批路由的声明，不是运行时行为面；且创建路径只拿得到请求值、恢复路径只
  拿得到事件派生值，未显式声明时两者不同名 ⇒ 会算出一个**假**的 `policy_change`（其实没变却放行）。
- **ceiling 类**（local fuse 的值与来源、run 作用域四维）—— 它们是**预算**而不是策略（D7 已把
  `budget_increase` 判 409）。把 fuse 算进摘要，恢复请求只要顺手抬一格 `local_max_agent_turns`
  就能自己造出 `policy_change`，等于同一个漏洞换一扇侧门。fuse 仍照常参与 `limits` 快照与
  `resume_headroom_ok`，只是不构成"策略变了"。

排除项的代价是"只改了被排除项 ⇒ 恢复被拒"：用户此时仍可用 steer（`relevant_steer`，也是 continuation
推荐的那条路）或真实的策略变更恢复，不构成死锁。

D8 这一侧只多两条操作事实：还原的落点是 `session/model_switch.py` 的 `restore_policy_inputs`，
它由 `session/service.py` 在解析 fuse 与吃会话级模型切换**之前**调用（档位同时是 turn ceiling
的输入，两处必须同一个值）。

### D9 — 与既有不变量和契约的关系

- 仍然只有一条 Tool 执行路径、一个 retry 责任域（ToolExecutor）、**一个** loop guard 责任域；
- Model fallback 与 Tool retry 继续分离；本特性不改变 Provider 选择策略；
- 单终态不变量不变：stuck 暂停落的是**非终态** `run/paused`，`run/resumed` 以同一 `run_id` 接回；
- replay **不**执行模型 / 工具、**不**消耗预算、**不**产生新的 guard 动作（重放只重建状态）；
- optional capability 故障不能拖垮 Core 的规则不变：检测器与证据端口故障均不得把 run 变成
  异常失败（证据端口读不到 ⇒ 该值为 `None`，对应依据不可用，而不是抛异常）。

### D10 — Live Gate 场景③（ADR-0044 D10 的 3 次连跑）

真实配置的模型 + 生产 ToolRuntime 反复调用一个**确定性失败**的真实工具 ⇒ 恰好一次 replan ⇒
同模式持续 ⇒ `run/paused(reason=stuck)`。同一 sha/tree 连跑 **3/3**；失败尝试全部保留；
缺凭据 ⇒ BLOCKED / SKIPPED，**永不 PASS**（ADR-0044 D10 的证据字段与凭证零泄漏条款逐条适用）。

---

## 3. 落地映射

| 决策 | 落点 |
| --- | --- |
| D1 责任域 / 事件派生 | `agent/guards.py`（`StuckDetector` 等）、`agent/runtime.py` 的接入点 |
| D2 五模式与暂停点 | `agent/guards.py` 的阈值表 + 各子状态机；`02 §5.3` 是阈值权威 |
| D3 规范化 / 脱敏 | `agent/guards.py`（规范化：动作按 `canonical_observation` 同一套折叠）、`redaction.py`（`redact_secret_values`） |
| D4 进展证据 | `agent/guards.py`（两种进展量分别实现） |
| D5 动作面 | `agent/runtime.py`（replan 臂 / 暂停臂）、`session/event.py`（`guard/stuck` 常量 + 生成物）、`prompt/builtin.py` + `prompt/section.py`（`corrective:stuck_pattern`） |
| D6 载荷 / closeout | `agent/run_budget.py`（`build_pause_data(stuck=...)`、`deterministic_continuation(reason=...)`）、`agent/runtime.py`（`_closeout_instruction(reason=...)`） |
| D7 恢复依据 | `agent/run_budget.py`（`validate_resume` 的 stuck 分支、`build_resume_data(resume_evidence=...)`）、`session/service.py`（现场重算 + 传递） |
| D8 证据端口 | `agent/resume_evidence.py`（新）、`assembly.py`（注入）、`session/service.py`（恢复侧重算 + 还原快照里的策略输入）、`session/model_switch.py`（`restore_policy_inputs`） |
| D10 Live Gate | `evaluation/live_gate/scenarios/`（场景③）+ 证据文件入 `docs/live_gate/` |

---

## 4. 残余与已知边界（登记，不阻断）

1. **摘要不是全序**："更新"只能判"变过"（D7）。若将来要判"更新"，需要一个单调的 policy / 环境
   版本计数器——今天没有这样的数字源，本 ADR 选择 fail-closed 的近似（不等于快照）。
2. **环境 revision 的代价与精度**：工作区树清单是一次全树 walk，只在暂停 / 恢复各付一次；为避免
   超大目录拖住暂停，超过条目上限后**停止采集**并把"已截断"折进摘要——截断只会让"变过"更难被
   观测到（fail-closed：证据不足 ⇒ 409），不会伪造出假依据。
   **mtime 的粒度是这条观测的下限**（T9 实测，Windows）：同一路径在同一个时间片内被等长改写
   （`st_size` 不变、`st_mtime_ns` 相同）算"没变"——即"改过但观测不到"。方向同样是 fail-closed
   （证据不足 ⇒ 409，不放行），代价是用户可能得再动一下工作区；用例因此用显式
   `os.utime(ns=...)` 造两次可区分的写，而不是靠"写两次就一定有新 mtime"。
   **空目录与不可读目录两头都不许有歧义**：空目录有确定摘要（空输入哈希），读不到（不存在 /
   中途 I/O 错）一律 `None` ⇒ 该依据不可用。
3. **`metadata` 不进观察指纹**：工具若把语义信息放进 `metadata`，"观察是否变化"会看不见它。
   这是"每次必变字段（口径 / 时长）不能进指纹"与"语义完整"之间的取舍，登记在此。
4. **③ 的可达面窄**：本架构里"无工具调用的决策"要么完成、要么被 quiescence 闸门收口，所以
   ③ 的计数通常跨"同 run 的多次执行"累积（重启 / 恢复后重放）。这是架构事实，不是缺陷；
   用例按"同 run 多次执行 + 重放"覆盖它。
5. **`STATUS_IDENTICAL_TOOL_FAILURE_LOOP` 从生产路径消失**：常量与历史事件读法保留。原来的
   落点 `_terminal_failed_run` 因此**没有生产调用方**了（异常 / 取消臂各有自己的收口），今天
   只有测试直接调它——不是"只剩异常臂在用"（那句是订正前的错记）。它留在 `runtime.py` 里是
   为了让历史的 `run/failed(identical_tool_failure_loop)` 读法与"受控失败"这条臂级语义仍有
   单一实现；删它属于另一笔清理，本票不动。
6. **CLI 的三类依据**：CLI 必须能表达 stuck 恢复的依据（新开关或明确拒绝），否则"暂停了但命令行
   无法有依据地恢复"。本票按 CLI 现有入口形态落地，不新增交互式承诺。落地形态：`resume --basis`
   四个取值齐备（其中 `budget_increase` 对 stuck 一律 409），`run` / `resume` 两条链路的证据端口
   都走 `resume_evidence.evidence_port`（**唯一**构造点）。**已知边界（订正后）**：纯 CLI 今天
   实际只有 `environment_change` 一条走得通——用户在宿主侧动过工作区就是"世界变了"。
   另两条都要另加入口：`policy_change` 需要一次带档位 / 模型的 amend，而 `run` 不声明档位、`resume`
   也没有模型 / profile 开关；`relevant_steer` 需要一条 `run_id` 与该 run 相同的 `steer/requested`
   事件，而 CLI 没有 steer 入口（Web 有 `send_message(mode="steer")`，且它对暂停态同样不可用，
   见残余 9）。**"CLI 上三类都能用"是订正前的错记**：CLI 的 `run` 与 `resume` 各自的入口面
   决定了这件事，不是可以靠"事件流里存在"绕过去的。补 CLI 的策略开关 / steer 入口属另一票，
   本票不扩范围。
7. **`replan_count` 是契约值，不是账；而"纠正消息"与"它的事件"之间有一个窗口**：`run/paused.stuck.
   replan_count` 恒为 `1`。runtime 的两条 append 是**先事件、后纠正消息**（`_stuck_replan_arm` 的
   `[event, corrective]`），于是在两者之间崩溃时：
   - 事件已落盘、纠正消息丢了 ⇒ 重放把 `_replanned` 由那条 durable 事件重建为真，**不补发**；
     模型从未收到纠正，账上却写着"已纠正过一次"；
   - 两者都没落盘 ⇒ 重放会把"首达 T"这一格消费掉（各模式的首达闩在重放里照常置位，见
     `StuckDetector.advance` 里的 `_replanned` 置位与各模式的 `*_replanned`），之后的新重复也
    不再等于 T ⇒ 同样**不补发**；暂停载荷里的 `replan_count` 仍写 1。

   **两个窗口的共同代价是"一次账上有、模型没收到"的纠正（少一次，不是多一次）**——本 ADR 早期
   草稿写的"重放会再 replan 一次、账上出现两次纠正"方向反了：`count == threshold` 是等值判定，
   而重放已经把首达那一格消费掉，`_replanned` 又拦下任何新的 replan 信号（实测读数：三个 case
   ——对照 / 事件在 / 事件与消息都不在——后续都是 `none, none, paused(6), paused(7)`，没有任何
   一例再发 replan）。要闭合这一格需要一个"纠正**已送达**"的 durable 标记（或把两条 append 的
   顺序与补偿做成一步），本票不加：代价是一条没送到的纠正，不是安全边界（run 照样会到 2T 暂停，
   恢复依据的判定不受影响）。
8. **`ToolResult.runtime_signal` 只服务委派树账本，且只在当场那一轮**：它是 `exclude=True`
   （**不落盘**）的进程内信号，重放事件看不到它。本 run 的五模式判定不依赖它；它唯一的用途是把
   **树账本**（有自己 durable 家园的那本）的 ① 结论翻译成本 run 的信号（D1 的例外）。因此**同一段
   历史在崩溃前 / 崩溃后**的差别只有一格：崩溃前那一轮，失败的 delegate 调用由树账本计数；
   崩溃后重放时检测器会照事件把同一批失败数进 ①。两者的**阈值线相同**（都是 3/6），差别只在
   计数口径（树 = 跨后代累计，事件 = 本 run 逐条），所以恢复后的判定可能**更早**到线而不是更晚
   ——方向是 fail-closed 的一侧（更早暂停，不会漏停）。要彻底闭合需要一个"这次调用归谁数"的
   durable 标记，那会把子会话的事实写进父会话事件流，本票不做（代价登记于此）。
9. **恢复依据的"正文"不进模型上下文（T9 实测边界，登记不阻断）**：三类依据里
   `environment_change` / `policy_change` 是**观测**（两侧各算一次摘要再比），本身没有"正文"；
   `relevant_steer` 有正文，但暂停期间生产**没有**把它投进恢复那条腿上下文的路径——Web 的
   `send_message(mode="steer")` 要求会话有**在途 run**（暂停态调用得到 `SteerTargetNotFound`
   409），而运行时只排空内存里的 `steer_source` 队列（`runtime._drain_steers` +
   `_applicable_steers`），队列的生命周期随 run 结束收口。
   **实测读数**（`docs/live_gate/20260927T051359-048e2246a362-…` 第 3 次尝试）：恢复腿在
   `run/resumed` 之后没有任何工具调用，只把暂停前那份报告又念了一遍——依据被采纳、账记得
   对，但模型不知道用户说了什么。
   **后果与口径**：`relevant_steer` 仍只按事件流判"有没有相关 steer"（D7 不变），**不**承诺
   模型知道正文；Live Gate 场景的有据恢复因此改用 `environment_change`（世界真的变了）取证，
   且**不**断言恢复腿读到了上游产物（场景 v3 的"这里没有这条断言"注释指回本条）。要闭合这一格
   需要一个产品决定（恢复请求携带正文 / 暂停期间的 steer 排队到恢复后投递），属于另一个票。
   同一次 v2 运行还暴露了一个**已修**的接线缺陷：既有会话的 `build_runtime` 漏传
   `stuck_evidence` ⇒ 暂停快照两格为 `None`、环境 / 策略两条依据恒"无快照可比"（修复与
   回归用例：`session/service.py` 的那个 kwarg + `tests/session/test_resume_amend_passthrough.py`
   的 `TestResumePathStuckEvidence`）。登记在这里是因为它说明**暂停侧快照与恢复侧现算必须
   共用同一个端口实例**这条纪律只有实测才能钉住（单测当时全绿）。
10. **⑤ 可以被"新颖观察"无限推迟**：⑤ 的复位量是"本 run 从未出现过的成功观察指纹"（D4），
   判据里**不**区分"这个观察有没有让项目往前走"。所以一个反复读不同路径 / 读一个每次都变的
   计数器 / 反复 list 一个在增长的目录的 run，永远在制造新指纹 ⇒ ⑤ 永不触发，哪怕它对任务
   零进展。这是 D4 那条取舍（"不查 MUTATING、判据必须 100% 事件派生"）的直接代价：要闭合它
   需要一条事件之外的事实（工具的能力声明，或一个真正的"项目版本"信号源），而那会破坏 D1 的
   重启可重放性。今天兜住这类 run 的是 ①/②（同动作同观察）与预算；登记于此，不在本票扩范围。
11. **本票之前写入的存量 stuck 暂停：`policy_change` 一律 409（fail-closed）**：那些载荷只记了
   `policy_version` 摘要、没有逐维 `policy_inputs`（D6 的"两格同源"判据因此不成立），而摘要
   本身还原不回来 ⇒ 无法证明"策略真的变了"，`stuck_resume_evidence` 直接拒（实测：改前
   `ACCEPTED ⇒ fail-open`，改后 `BudgetConflict`）。**代价是存量会话
   的 `policy_change` 不可用**（`relevant_steer` / `environment_change` 不受影响：它们不依赖
   策略面，也不需要还原）。方向选 fail-closed 而不是"照旧现算"：照旧现算就是 T9 审查 P1 的原始
   fail-open（省略被算成变更）。要闭合它需要一次数据迁移（按当时的策略面回填逐维值）——没有
   这样的信息（摘要不可逆），所以只能等这些会话自己走完。同一条判据也把**畸形快照**（逐维值
   形状非法）拒在这里：`agent_profile` 被写成 list 的那份快照在改前是 `TypeError:
   unhashable type: 'list'`（`agent/profiles.py` 取档位时炸，恢复路径以 500 收场），改后是
   同一个 409（回归用例 `test_a_malformed_snapshot_is_a_conflict_not_a_crash`）。
12. **跨模式同批时，首达 T 那条 replan 仍可能被同批的暂停压掉**（二轮审查探针 D 的 D4）：
   `worst_stuck_signal` 的"replan 优先"是**按模式**的（D5）——一批里 ② 首达 T、① 同批越过 2T
   时，挑出的是暂停，② 的纠正这一批不执行（它的首达闩已被消费）。探针 D 实测：同批同模式
   （① 首达 T + ① 越过 2T）**先执行的是那条 replan**——它落的是**既有形状**
   `tool/failure-guard(level=soft, consecutive_failures=3)`（D5），该 run 的 `guard/stuck`
   里因此只有 `('paused', 'stuck.tool_failure_loop', 7)` 一行（`count=7`、`replan_count=1`、
   恰好一次暂停，D1/D2）；变异掉"replan 先执行"则停在 `count=6` 且 `tool/failure-guard`
   为空（D3）。跨模式的选择结果与修复前逐字相同
   （`paused/stuck.tool_failure_loop` 对 `paused/stuck.tool_failure_loop`，D4）。
   两个模式的计数**不同源**：① 数的是同动作同**失败**的重复，② 只在观察**成功且相同**时
   连续（失败打断它）；与 ① 同一段历史里累加的通常是无进展那一族（同动作的失败不产生"新的
   成功观察"⇒ ⑤ 也在涨）。所以"跨模式同批"要的形状（一个模式首达 T、另一个同批越过 2T）比
   这里读起来更窄——**结论不变**：仍可能压掉那条 replan。改成批级优先（"一批里有任何 replan
   就不暂停"）在本架构下也不会
   无限推迟暂停（每个模式的首达 replan 一个 episode 只可能发生一次 ⇒ 暂停最多推迟一批），但那
   超出 `02 §5.3` 的字面（它只承诺"**同一模式** replan 后仍持续 ⇒ 暂停"）；本票按字面收窄，
   形状登记于此。
13. **装配期失败留下的孤儿 `session/resumed`**（二轮审查探针 F 的 F2）：快照里的模型若在恢复前
   被移出 catalog，`build_runtime` 处会报 `ConfigError`（响亮失败，D6 的不做回落所期望的），
   而失败发生在 `Session.resume` **之后** ⇒ 事件流里留下一条 `session/resumed` 与随后的零 run
   事实。取证（两路**一次性探针**，脚本留在仓库外的临时目录，读数如下）：
   - 恢复请求**显式声明**一个不在 catalog 的模型（不涉及任何快照还原）留下的形状**逐字相同**
     ⇒ "事件先落、启动在后"是既有顺序缺口，不是本票引入；
   - 会话**已有**当前模型（`model/changed`）而它从 catalog 消失时，还原侧按既定优先级让开这一维
     （D6），`amend_with_session_model` 走 `#137` 的 warn + 回落默认链 ⇒ 既无孤儿事件也无响亮
     失败。这一支属 `#137` 的既有口径，本票不改，登记于此以免把两种子情况混为一谈。
   要闭合第 1 支需要把"模型可解析"提前到 `Session.resume` 之前（或把装配失败回滚成一条显式
   事实），属另一笔。
14. **快照里的档位名不存在时是裸 `KeyError`（响亮，但不是设计过的错误形状）**（Correctness 二轮
   复验的发现 2）：`recorded_policy_inputs` 只验**形状 + 摘要自洽**，不验值域；一份摘要自洽而
   `agent_profile` 指向已改名 / 已删除档位的快照（手改 JSONL，或部署换过档位表）在
   `session/service.py` 的 `_profile_turn_ceiling(amend)` 处抛 `KeyError`
   （`agent/profiles.py` 的 `BUILTIN_PROFILES[...]`），恢复以未捕获异常收场而不是 409。与残余 13
   同一族（快照里的配置值在恢复前失效 ⇒ 逐字还原、不做回落 ⇒ 响亮失败），差别只在错误形状：
   模型那一支是**设计过**的 `ConfigError`，档位这一支是既有路径的裸 `KeyError`。取证
   （`t9_probe_profile_domain.py`）：请求**显式声明**未知档位同样 `KeyError`（本票之前就如此，
   `profiles.py` 的 docstring 明写为响亮失败）⇒ 属既有口径，本票新增的只是"无需请求声明也能走到
   它"；两条腿都是**零副作用**（域校验发生在 `Session.resume` 之前 ⇒ 连残余 13 那条孤儿
   `session/resumed` 都不留，实测事件数 32→32）。逃生门与模型那一支相同：请求显式声明
   `agent_profile` 即恢复（声明优先于快照）。要闭合成 409 需要给四维定一份值域政策并与 D8 的
   "不做回落、响亮失败"对齐，属另一笔。
15. **委派子 run 的策略面在 T9 里没有定义**（T9 二轮审查 P2 的剩余部分）：本票给
   `AgentFactory` 接了证据端口，但只接**环境那一半**（理由见 D8）；子会话的 `policy_version` /
   `policy_inputs` 恒为 `None`，`policy_change` 因此对子 run 不可用——它的 stuck 暂停只能靠
   相关 steer 或环境变更解开。这一条不是疏漏而是取舍：child 的生效策略面要定义清楚，得先回答
   "子 agent 的 profile / effort / context providers 分别继承什么"（今天 Factory 一个都不接收），
   而那超出本票。记在这里的两件事：① 子 run 的暂停载荷**不再假装**策略可依据（旧实现列三条 ⇒
   恢复入口必然 409）；② 若将来要让子 run 支持 `policy_change`，落点是"给 child runtime 定义并
   传入它自己的策略面"，不是"父的面照抄一份"。
