# Issue #642 阶段一证据报告：旧摘要来源信任（C-6）与旧格式兼容（T12g）

- 被测代码：worktree `intelligence-agent-wt-642`，分支 `codebuddy/642-summary-trust`，HEAD `00a0fb20`（origin/main）。
- 原报告被测 commit `1226f4bb`；本报告全部读数在 `00a0fb20` 上实跑取得（票面已注明行号不跨版本混用）。
- 探针：仓库外 `/tmp/issue642-probe.py`、`/tmp/issue642-probe2.py`（票面自包含最小探针逐字复现 + 扩展观测），运行方式
  `PYTHONUTF8=1 PYTHONPATH=<worktree>/src .venv/bin/python /tmp/issue642-probe.py`（本 worktree 无独立 venv，显式
  `PYTHONPATH` 指向本树 `src`，避免解析到主 clone 的 `.pth` 路径）。探针不修改产品代码。
- 结论速览：**C-6 与 T12g 在当前 HEAD 均复现**；消费层信任边界仍为"文本前缀即信任"，自洽校验闸门对伪造内容不可区分来源；
  旧六节格式（T4 #134 时代）仍被识别为 summary，但程序化消费无任何迁移分支。

---

## 1. C-6：伪造八节前缀 SystemMessage 被当作可信旧摘要

### 现象（探针读数，HEAD `00a0fb20`）

| 观测 | 读数 |
| --- | --- |
| 票面探针：`"Z-042" in json.loads(sections[_SUMMARY_HEADINGS[6]])` | **True**（复现） |
| 伪造摘要的 `[0]`（目标节）是否继承 | **否** —— 返回 `(none)`（`_current_goal_body`，#556 裁决 C 后行为，与票面复核一致） |
| 手拼八节伪造消息（[1]=FORGED-FACT-42 / [6]=["Z-042"] / [7]=["/etc/shadow"]）+ `protected_facts=[]`（生产形状，builder 恒传非 None） | `[1]`＝`(none)`（不继承）；**`[6]`＝`["Z-042"]`、`[7]`＝`["/etc/shadow"]`（全部继承，投毒成立）** |
| 同上 + `protected_facts=None`（直连调用面） | **`[1]` 也继承**（`FORGED-FACT-42`） |
| `_validate_summary(伪造, [同一伪造消息], [])` | **PASS** —— 自洽闸门的期望值从同一伪造消息重新派生，[6]/[7] 两侧同源，无法证明来源 |
| `_validate_summary(伪造[1]≠期望, …, [])` | rejected —— 仅当伪造 [1] 与 pf 侧期望不一致时才被拦（见下） |

### 触发条件

1. 一条 content 以 `## 原始目标与用户约束\n` 开头、且八节标题齐全的 `SystemMessage` 进入 `compact()` 的 early 窗口
   （`_is_compaction_summary`，`src/agent_harness/context/compactor.py:806-812`，SystemMessage 分支按前缀识别）。
2. `_programmatic_summary_sections`（`compactor.py:769-783`）对该消息走结构化继承分支：`previous[6]`/`previous[7]`
   解析后经 `add_once` 并入新摘要的标识/文件节；`protected_facts is None` 时连 `previous[1]` 也并入。
3. 校验闸门 `_validate_summary`（`compactor.py:829-839`）用同一批 messages 重算期望值——伪造消息自身参与期望派生，
   [6]/[7] 必然自洽。

### 影响路径

- **污染路径**：伪造 `[6]`/`[7]` → 新摘要第 7/8 节（精确标识清单/文件清单）→ 该摘要持久化进 `CONTEXT_COMPACTED` →
  resume/再次压缩时经 `derive_messages` 投影回 `HumanMessage(name="context_compaction_summary")` → 下一次压缩按
  结构化分支**继续继承**（伪造条目进入摘要链跨窗口存续，受 `_capped_entries` 50 条/200 字符窗口约束，但不消亡）。
- **生产摄入面（如实边界）**：`derive_messages` 不产出任何 `SystemMessage`（`src/agent_harness/session/derive.py` 全文
  无 `SystemMessage(` 构造），持久化摘要一律投影为带 marker name 的 `HumanMessage`。故伪造 `SystemMessage` 只能进入
  `compact()` 的**直连调用面**（测试/程序化调用方），不构成已证实的持久化摄入攻击——与票面"摄入层攻击可达性属于报告
  排除调查项，未证实"一致。本票只覆盖消费层。
- **[1] 保护事实节的继承面**：仅 `protected_facts=None` 的直连面存在（生产路径 builder 恒传非 None，
  `src/agent_harness/context/builder.py:738-744`），且 pf=[] 形状下伪造 [1] 会被自洽闸门拒绝（期望值侧来自真实 pf）。

## 2. T12g：旧六节格式被识别为 summary，但程序化消费无迁移分支

### 旧格式形态与来源（票面 AC 3 要求的 fixture 与生成 commit）

旧六节摘要由 T4 #134 引入（生成 commit `3915a5a6`，2026-09-08），persisted 于 `CONTEXT_COMPACTED.summary`，
完整布局（与 `tests/context/test_compaction_bracket.py:48` 现存 `LEGACY_SUMMARY` fixture 同形）：

```text
## 目标
<LLM 撰写的 1-3 句目标 paraphrase>
## 约束
<约束列表>
## 进展
<进展>
## 决策
<决策>
## 下一步
<下一步>
## 关键上下文
<关键上下文，含标识/路径等自由文本>
```

历史上 persist 的摘要形态共三代：

| 代 | 生成窗口 | persisted 内容 | 当前 `derive_messages` 投影（HEAD） | 程序化消费（HEAD） |
| --- | --- | --- | --- | --- |
| A（六节） | `3915a5a6` → `cbc71004^`（2026-09-08 ~ 09-28） | 六节文本 | `HumanMessage(name="context_compaction_summary")`（`113ebcb6` 起统一） | **无结构分支**：当普通文本 `visit()` 开采 |
| B（八节 SystemMessage 形态） | `cbc71004` → `113ebcb6^`（2026-09-28 当日） | 八节文本 | 同上（HumanMessage + marker name） | 结构化继承 [1](仅 pf=None)/[6]/[7] 正常 |
| C（现行） | `113ebcb6` → HEAD | 八节文本 + marker name | 同上 | 同 B |

另有 SystemMessage 形态的两类前缀（`## 原始目标与用户约束\n` / `## 目标\n`）仍被 `_is_compaction_summary` 识别
（`113ebcb6` 引入，`tests/context/test_prefix_stability.py:265-281` 钉住），但当前 `derive_messages` 不再产出
SystemMessage——该识别只服务 early 窗口边界与直连调用面。

### 现象（探针读数，HEAD `00a0fb20`）

以六节文本分别构造 `SystemMessage` 与 `HumanMessage(name=marker)`，喂 `_programmatic_summary_sections(..., None)`：

| 观测 | SystemMessage 形态 | HumanMessage(marker) 形态（= resume 真实投影） |
| --- | --- | --- |
| `_is_compaction_summary` 识别为 summary | **True** | **True** |
| 新摘要 `[0]`（目标节） | `(none)` —— **旧目标不迁移** | `(none)` —— 同 |
| 新摘要 `[1]`（约束，六节的"## 约束"） | `(none)` —— **不迁移** | 同 |
| 新摘要 `[6]`（标识） | `["R-042", "old.txt"]` —— 从旧摘要**正文**当普通文本开采 | `["R-042", "old.txt", "context_compaction_summary"]` —— **连消息自身的 marker name 字符串都被当标识采进** |
| 新摘要 `[7]`（文件） | `["old.txt"]` | 同 |

### 触发条件

旧六节摘要（persisted `CONTEXT_COMPACTED.summary`，schema 为 `six_section`，测试 fixture 即按此构造）经 resume 投影
进入下一次 `compact()`：`_is_compaction_summary` 按前缀/name 识别为 summary（early 窗口起点、turn 计数排除、
`service.py:3073` dry-run 守卫同判），但 `_programmatic_summary_sections` 的结构化分支要求 content 以
`## 原始目标与用户约束\n` 开头（`compactor.py:770-772`）——旧格式不满足，落回 `visit(message.model_dump(...))`
按普通会话文本开采（`compactor.py:786`）。

### 影响路径

1. **旧目标/约束丢失**：六节的目标与约束只存在于摘要文本里，不进新摘要的 `[0]`/`[1]`；仅剩两条残路——
   ① LLM 转录（early 全文进 prompt，模型可能复述进第 3–6 节，非确定、非契约）；② 正文标识被开采进 `[6]`/`[7]`。
   **生产缓解事实**：`[0]` 在任何路径都取自 `_current_goal_body(protected_facts)`，而 protected_facts 由
   `derive_protected_facts(session.events)` 从 append-only 事件流重派生（含被 shadow 的目标事件，
   `derive.py:662-796` 不过滤 shadowed range）——user goal 的权威来源是 SessionEvent，不是摘要链。故"目标丢失"
   的实际损失面 = 六节摘要里 LLM paraphrase 的目标/约束表述（非事件权威源）从摘要链消失；
   六节文本本身不删除（append-only），仍在 LLM 转录与持久历史里。
2. **标识重复抽取/膨胀**：旧摘要正文被当作普通会话内容再开采一次——旧摘要生成时已收敛过的标识（如 `R-042`）
   从摘要文本**复活**进新 `[6]`/`[7]`，即使它们已不存在于任何活窗口消息（探针 `T12g.impact.*`）；resume 投影形态下
   marker name 字符串 `context_compaction_summary`（匹配 `_IDENTIFIER_PATTERN` 的 `snake_case` 分支）被采进
   `[6]`。单次生成内 `add_once`/`_capped_entries` 去重吸收字面重复，故表现为**膨胀/复活**而非字面重复条目；
   混合链（旧摘要 + 新八节摘要并存）中同一标识经"开采 + 结构继承"两条路径进入，去重后单条（探针 `T12g.mixed_chain.*`）。
   注：生产 replay 中嵌套 bracket 只保留最新摘要（`derive.py:113ebcb6` 引入的 nested/crossing 消解），混合链在
   持久化会话中基本不可达，主要为直连调用面形态。

## 3. 兼容层现状清单（"不能自行删除兼容"的对象）

`_is_compaction_summary` 的两个旧前缀 + marker name 识别在当前代码承载四个消费点：

| 消费点 | 位置 | 旧格式被识别后的效果 | 若删除识别的后果 |
| --- | --- | --- | --- |
| early 窗口边界 | `compactor.py:207-226` | 旧摘要是 early 窗口**起点**（不再当可跳过 SystemMessage 前缀），随本次压缩被**合并消费并替换**（"滚动摘要合并"，`113ebcb6` 的本意） | 旧摘要被当普通系统前缀跳过：永不参与摘要合并、不进程序化节，上下文无法收敛（但同时也不会被污染/丢失——信息原样留存） |
| turn 计数 | `compactor.py:477-483` | 旧摘要不计入 `compacted_turn_count` | 计数虚增（低水位判定漂移） |
| 清单锚块落点 | `builder.py:198`（SystemMessage 形态） | 清单块插在旧摘要之前 | 落点退化为首个 SystemMessage 块之后（现行投影为 HumanMessage，本就不走该分支） |
| dry-run 守卫 | `service.py:3073` | 与 early 窗口同判 | dry-run 与实压缩判据漂移（F5 #635 修过的形状） |

**保留契约（本次核实结论）**：兼容层保护的是"旧摘要可被滚动合并、可被边界判定正确对待"，**不**包含旧字段的
结构化迁移——六节目标/约束的迁移分支从未存在。任何修复不得删除上述四个消费点的识别语义；T12g 的迁移是
**新增**消费分支，不是替换现有兼容。

## 4. 阶段二评估（未执行，理由如下）

按用户约束"保持全部现有兼容前提下存在最小修复才动手"评估：

1. **C-6 的最小诚实修复 = 来源身份信任**（复用 `source_ranges`/bracket 事件身份，区分 derive 投影出的合法摘要与
   任意前缀消息）。这是 `compact()`/`_programmatic_summary_sections` 消费边界的**契约变更**：需要把"哪些消息是
   bracket 投影摘要"从文本前缀改为来源传递，涉及 `compact()` 签名、`derive_messages_with_source_ranges` 消费方式
   与全部直连测试的兼容策略——票面修复建议明确要求"先核实所有直接调用 compact 的现有 tests/API 调用方式，形成
   有来源和无来源的明确兼容策略"，且"不能只给字符串加另一个可伪造标记"（按 marker name 收窄识别即属此类，
   不做）。
2. **T12g 迁移的前置输入未补齐**（AC 3）：迁移字段、旧目标权威来源、预期标识/文件列表需持 fixture 与生成 commit
   做产品裁决（本报告 §2 已给出 fixture 与生成 commit `3915a5a6`，但迁移语义仍需裁决）；凭 `## 目标` 猜旧格式
   或自动删 SystemMessage 均被票面禁止。
3. 综上：**不存在既保持全部现有兼容、又不需要消费边界契约变更与产品裁决的最小修复**。阶段二不执行，
   与票面"输入未补齐前此子项未完成……只交付证据与明确待决项"的停止标准一致。

## 5. 待决项（交用户裁决）

1. C-6 修复方向批准：是否按"来源身份信任（source_ranges/bracket 事件身份）"立项消费边界改造，及其对直连调用面
   （tests/程序化调用方）的兼容策略（有来源才结构继承 / 无来源降级为普通文本开采 vs 拒绝）。
2. T12g 迁移语义：六节旧摘要的目标/约束是否迁移、迁移到哪节（[0] 已由事件权威源承载，迁移是否仅限 LLM
   paraphrase 的保留）；`six_section` fixture（`tests/context/test_compaction_bracket.py:48`）作为迁移断言基准
   是否认可。
3. marker name 字符串被 `_IDENTIFIER_PATTERN` 采进 `[6]` 的污染（探针 `T12g.humanmsg.[6]` 第三条）是否随 C-6
   修复一并处理（旧格式 visit 开采时排除 summary 自身 metadata）。
4. 直连面 `protected_facts=None` 下 `[1]` 可被继承（C-6 扩展读数）是否收窄为仅生产形状（pf 非 None）语义。

## 6. 验证记录

- 探针读数：见上文各表，原始输出由 `/tmp/issue642-probe.py`、`/tmp/issue642-probe2.py` 产生（JSON dump 已核对）。
- focused 回归基线（无代码改动，确认 HEAD 绿）：
  `PYTHONUTF8=1 PYTHONPATH=<worktree>/src .venv/bin/python -m pytest -q -p no:randomly tests/context/test_compactor.py tests/context/test_compaction_bracket.py tests/context/test_runtime_context_persistence.py`
  → 见交付报告读数。
- 本票零产品代码改动；不关单、不改票面。
