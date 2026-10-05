# issue #649 方案依据：结构化摘要解析中保留标题与正文 Markdown 标题的区分

> **状态：施工前调研（Reuse First，`AGENTS.md` §6.1 / SDD §1.3）。**
> 结论：**ADAPT**——只取「围栏感知 + 反斜杠转义」两条确定性规则 + MIME 的
> 「保留定界符不得被正文伪造」原则；**不**引入全量 Markdown 依赖、**不**改
> 八节持久格式、**不**新增 LLM 调用。
>
> **核实边界（诚实标注）**：本调研首轮执行时 WebFetch 在非交互模式下不可用，
> 机制来自 WebSearch 返回的官方文档/仓库片段与稳定公共知识。凡未直接取到正文的
> 项在下方标注「未逐页核实」，不作为强主张；本轮为同 issue 的落地续跑，按交接
> 要求复用本结论、未重做五来源调研。未复制上游代码，故 License 栏只给「是否
> 实质复制」结论。

## §1 问题

`src/agent_harness/context/compactor.py::_parse_summary_sections` 旧实现把所有
`startswith("## ")` 的行当作节边界。模型摘要某一节**正文里出现行首 `##` 的普通
Markdown 二级标题**（`## embedded heading`）时，标题序列与契约失配 →
`ValueError("Summary section headings do not match the contract")` →
归类 `heading_mismatch` → 两次尝试后 `compaction_failed`，持续阻止压缩
（票面 T13d）。

关键补充发现（决定方案形态）：

- 程序化四节（§1/§2/§6/§7）是 **JSON 单行**（`json.dumps`，换行被转义），本身
  不会伪造节边界；
- 真正的歧义来自**模型节正文里恰好逐字等于任一保留标题的行**：它若等于程序化
  标题（如 `## 文件清单`），模型解析（期望 4 节）会当正文保留，但组装成八节后
  再解析就会被当边界 → 必须**转义后再解析**，且正文里非保留的 `##` 标题不得被
  改写。

## §2 来源与判定

### 来源 1 — CommonMark Spec 0.31.2（规范，`https://spec.commonmark.org/0.31.2/`）

- **机制**：ATX 标题（1–6 个 `#`，≤3 空格缩进，`#` 后须空格 / EOL）；围栏代码块
  （``` 或 ~~~，≥3，开闭同类同长，内部一律字面）；反斜杠转义（`` \# `` 输出字面
  `#`）。⇒ 标题识别必须**跳过围栏内部**，且可用反斜杠表达「字面标题行」。
- **契合**：本仓节标题是 `## `，正好落在 ATX 标题与围栏语义内；与 §7 不变量无
  冲突（纯确定性、无 LLM、不改 SessionEvent / 持久历史）。
- **判定**：**ADAPT**（只取「围栏感知 + 反斜杠转义」两条规则）。
- **License**：spec 文本 CC-BY-SA 4.0（搜索片段「licensed under CC BY-SA 4.0」，
  未逐页核实）；本票为规则适配、不复制文本。

### 来源 2 — RFC 2046（MIME Part 2，IETF，`https://www.rfc-editor.org/rfc/rfc2046.html`）

- **机制**：官方原文「The boundary delimiter MUST NOT appear inside any of the
  encapsulated parts, on a line by itself or as the prefix of any line.」——保留
  定界符与正文冲突的经典处置：要么选不冲突的定界符，要么让定界符可判定地不可伪造。
- **契合**：保留节标题即「定界符」；本票把「任意 `## ` 即定界」收紧为「**逐字
  等于保留标题**才是定界」，并对正文中的同文行做确定性转义，正是该原则的落地。
- **判定**：**ADAPT**（冲突判定原则，非代码）。
- **License**：IETF Trust / BCP 78；未复制代码/文本。

### 来源 3 — Anthropic Claude Compaction（一手文档，`platform.claude.com/docs/en/build-with-claude/compaction`；AWS Bedrock 镜像 `docs.aws.amazon.com/bedrock/.../claude-messages-compaction.html`）

- **机制**：压缩触发后 API 返回一个专用 **compaction block 承载摘要**，随后以压缩
  后上下文继续；摘要不靠 Markdown 标题回解析（搜索片段：「Creates a compaction
  block containing the summary. Continues the response with the compacted
  context.」）。
- **契合**：说明「结构标记用正文无法伪造的载体（content block / XML）」可根除
  歧义；但本仓八节摘要格式已冻结、且可读性与精确比对依赖 Markdown 标题，不能整体
  换格式。
- **判定**：**PORT DESIGN → 本票仅 ADAPT 思想**（保留 Markdown 格式，改判定规则
  使其不可伪造）。
- **License**：文档专有，未复制。

### 来源 4 — Cline（Apache-2.0，`cline/cline`，TS；auto-compact `summarize_task`）

- **机制**：`summarize_task` 生成结构化摘要后用「继续」提示把摘要作为**不透明
  上下文**回填，不再二次结构解析（搜索片段：issue #5474 描述 summarize_task 压缩
  对话）。
- **契合**：反证——「只有当消费者要重新解析摘要结构时，保留标题 / 正文标题冲突才
  致命」。本仓因 §1 精确比对、§5 清单一致性闸门必须重解析，故必须固定消歧。
- **判定**：**DEFER 借鉴**（不改回填形态）。
- **License**：Apache-2.0，未复制代码。

### 来源 5（备选，未采用）— markdown-it-py（MIT）/ mistune（BSD-3）

- **机制与判定**：完整 CommonMark 解析器能天然跳过围栏并 token 化标题，但本票只动
  解析 / 转义、受 Scope Lock 约束，引入新依赖超出范围 → **判定：BUILD**（自实现
  最小围栏 / 转义子集）。理由：复用阶梯第 2 / 5 级不成立（仓内无等价实现、未装
  依赖），第 7 级最小代码足够。
- **License**：未采用，不涉及复制；标记「未逐页核实」的 License 结论不作为主张。

## §3 总判定

**ADAPT**（CommonMark 围栏 + 反斜杠转义、RFC 2046 冲突原则）——**不做**全量
Markdown 依赖、**不改**持久格式、**不新增** LLM 调用。与冻结依据一致：

- `00_PROJECT_VISION.md` §3：Python / Async-first、Session event-sourced、完整
  保存 ≠ 完整注入、No hidden second path；
- `06_CONTEXT_ARTIFACT_MEMORY.md` §1–5 / §8–9：完整历史不删除、模型输入有界、
  tool pair 不拆、hard guard 不绕过；
- `docs/adr/0007-context-compaction-three-tier-fallback.md`（#348 / #346 / #383
  修订）：两次尝试上限、失败留任务可见状态、失败即保留原投影。

规格与决议无冲突：本票**不新增** deterministic fallback、**不删除**原始历史、
**不更改**模型预算契约。

## §4 本仓落地（补丁要点，实现见代码）

1. `_fence_delimiter` / `_fenced_mask`：确定性识别并标记围栏代码块内部（含定界行），
   围栏内一律按字面正文，不参与节边界判定。
2. `_escape_section_body`：把**围栏外、逐字等于保留标题**的行前缀 `\` 转义；
   非保留的正文 `##` 标题（`## embedded heading`）不转义、原样保留。
3. `_unescape_section_body`：解析时把 `\` + 保留标题解码回原值（正文保真，
   「禁止默默吞掉」）。
4. `_parse_summary_sections`：节边界只由「围栏外、未转义、逐字等于某一保留标题」
   的行定义；四节 / 八节的**数量 / 顺序 / 非空**仍由「收集到的标题序列 `==
   headings`」严格判定（缺节 / 错序 / 重复保留标题 / 节外前置文本 / 空节照旧
   `ValueError`）。
5. `_assemble_summary`：组装时对每节正文走 `_escape_section_body`，保证八节再解析
   时程序化精确比对与正文保真同时成立。

## §5 证据

- 红测：`tests/context/test_compactor_section_parse.py`（新增，13 项）。
- 票面最小探针：修复前 `ValueError: Summary section headings do not match the
  contract`；修复后返回
  `['done\n## embedded heading\ntext', '(none)', 'working', 'continue']`。
- 既有回归：`tests/context/test_compactor.py` + `tests/context/test_plan_reinjection.py`
  保持全绿。
