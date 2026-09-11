# PRD: 统一 Prompt 注册表与模板化组装

> 状态：决策已收敛（grilling 20 问全部锁定），待用户批准
> 日期：2026-09-12
> 作者：ZCode + 用户共同决策
> 依据：
> - ADR-0023（本设计的决策记录，`docs/adr/0023-prompt-registry.md`）
> - ADR-0020a（`agent_profile` 运行时消费 — system_prompt 注入纪律）
> - ADR-0010（CapabilityRegistry）、ADR-0011（Skills 渐进披露）、ADR-0015（AgentFactory）
> - 上游参考：DeepSeek Harness / pi-mono / oh-my-pi / ZCode（对照表见 ADR-0023 Context）
> - 架构不变量 #1–#22（`AGENTS.md` §7）
> - 工程规格 `goal/Lightweight_Observable_Agent_Harness_Spec/docs/spec/`

---

## 0. 背景与根因

全仓库模型可见的自然语言文本散落在 **15+ 处、跨 8 个模块**（完整清单见 ADR-0023 Context）。由此产生三个具体痛点：

1. **没有单一入口**——想知道"这个 agent 到底看到哪些 prompt、按什么顺序"，需要在 8 个模块间来回跳。
2. **无法动态组装**——重复要求无法共享。实证：`coding` 与 `research_review` 两条 profile prompt 里都有"结束时给出简明总结…以及任何未解决事项"，**措辞已经漂移**。
3. **修改成本高**——调一句文案必须打开对应 `.py`、读懂上下文、改完跑测试确认没碰坏别的逻辑。

本 PRD 只补 prompt 管理层，**不改 Agent Loop、不改事件模型、不改 ContextProvider 检索语义**。

---

## 1. 目标与非目标

### 目标

1. **单一入口**：一处可查看全部 prompt section 及其最终组装顺序。
2. **严格模板化**：`{{variable}}` 插值，未声明 / 未提供 / 格式非法**一律抛错**（绝不静默渲染为空）。
3. **可组装**：section 按 scope 筛选组合；共享 section 消除重复；工具 guidance 自动归集。
4. **显式排序**：集中 order 表，一处看清全序。
5. **作用域**：不同 profile / agent 拿到不同的 section 组合。
6. **Persona 可配置**：env JSON 覆盖内置前后缀，不改代码。
7. **不变量校验**：R1–R7 + 进程启动自检，超出全部参考实现。
8. **修改成本降低**：改 profile/辅助 prompt 文案 = 开 1 个文件；改工具 guidance = 就地改工具元数据。

### 非目标（明确排除）

- **ContextProvider 的检索内容**（memory / skills 结果）——有界检索 + token 预算 + 失败降级 + `MEMORY_DEGRADED` 事件是它的核心语义，纳入注册表会破坏这些（不变量 #16/#21）。
- **历史事实回放**（`derive.py` 的压缩摘要）——那是过去事件的**数据**，不是我们创作的 prompt。
- **外部 SDK 的 prompt**（langmem 内部指令）。
- **Prompt 版本化 / A-B 实验**——四份参考实现都没有；git 提交即版本。
- **模板的条件 / 循环 / 继承**——四份参考实现都没有这个需求；自研替换器不支持。
- **`.md` 文件化存储**——项目无包内数据文件先例，`uv_build` 打包行为未证实；集中到 Python 模块已解决主要痛点。

---

## 2. 已锁定决策（20 项）

| # | 决策点 | 选择 |
|---|--------|------|
| Q1 | 正文存储 | **A** 集中到专用 Python 模块（`prompt/sections.py` 等） |
| Q2 | 模板引擎 | **A** 自研极简严格替换器（`{{name}}`，未知即抛，无递归） |
| Q3 | 作用域模型 | **A** 扁平注册表（重名抛错，同 `CapabilityRegistry`）+ **组装期筛选** |
| Q4 | 管理边界 | **C** 管 profile / 工具 guidance / 运行时上下文 / 辅助 LLM prompt / 纠偏框架；排除检索内容、历史回放、外部 SDK |
| Q5 | 落地节奏 | **B** 分三阶段 P0 / P1 / P2 |
| Q6 | Persona 配置 | **B** 环境变量 JSON（`AGENT_PERSONA`），沿用既有"复杂配置=env JSON"约定 |
| Q7 | 校验规则 | **D** R1–R5 + R7 完整规则集 + **进程启动自检**（R6 由稳定排序保证，非校验） |
| Q8 | 命名与排序 | **(a)+(i)** 冒号命名空间 + **集中 `SECTION_ORDERS` 常量表** |
| Q9 | 变量模型 | **B** section 声明 `requires`（从模板**自动推导**）+ 全局变量池 |
| Q10 | 注入目标 | **C** `system` + `meta_user` 两段 |
| Q11 | 装配点 | **A** 注册表作为 `ContextBuilder` 上游（P0 零契约变更） |
| Q12 | P0 section 组织 | **a** 单条搬运，组装产物与现有字符串**逐字节相同**（零行为变化） |
| Q13 | R5 必需项 | **a** 只要求 `identity`（`main` 无"结束总结"要求，故 `output` 不能设为必需） |
| Q14 | 变量声明 | **A** 变量名**预声明**（否则 R3/R4 注册期校验无法实现） |
| Q15 | P0 票数 | **A** 3 票；工作区 `feat/backend`；产物 ADR-0023 + 本 PRD |
| Q16 | 快照载体 | **b** 非持久化、运行时组装，注入在**当前用户消息之前** |
| Q17 | 工具 guidance 归属 | **C** 工具自带 `prompt_guidance` 元数据 + 注册时归集；其余集中 |
| Q18 | 总票数 | **B** 8 票（P0 三 + P1 二 + P2 三） |
| Q19 | 快照内容 | **C** cwd + 日期 + 模型名 + 可用工具清单 + OS |
| Q20 | 既有注入污染 | **B** 在 P2 的"纠偏/框架消息"票内顺带修 extractor 过滤 |

---

## 3. 架构设计

### 3.1 Section 数据模型

```
PromptSection
├── name: str              # 冒号命名空间，如 "profile:coding:identity"
├── order: int             # 取自集中 SECTION_ORDERS 表，非手写散值
├── scopes: frozenset[str] # 该 section 参与哪些 scope（如 {"profile:coding"}）
├── target: Target         # SYSTEM | META_USER
├── text: str              # 模板正文（含 {{var}}）
├── requires: frozenset[str]  # 从 text 自动扫描推导
└── description: str       # 供 available() 视图与文档
```

### 3.2 注册表 API

```
PromptRegistry
├── register(section) -> None        # R1 名唯一 / R2 scope 合法 / R3 变量名合法
├── variable(name, description)      # 预声明变量名（Q14=A）
├── sections(scope) -> list[Section] # 按 scope 筛选，按 (order, name) 稳定排序
├── available() -> list[Section]     # 全量视图（"一处看全貌"）
└── assemble(scope, variables) -> AssembledPrompt
                                     # R4 变量有值 / R5 identity 存在 / R7 非空
```

`AssembledPrompt` 按 `target` 分组产出两段文本（system / meta_user），供 `ContextBuilder` 分别注入。

### 3.3 模板器（自研，严格）

- 只识别 `{{name}}`，`name` 匹配 `^[a-z][a-z0-9_]*$`（不允许内部空格）。
- **未声明 / 未提供值 / 语法非法** → 抛错，绝不留空。
- 替换值**不再二次扫描**（无递归插值）。
- 约 50 行，零依赖，无属性访问 / 方法调用能力（无注入面）。

### 3.4 组装管线

```
注册（模块 import 时声明全部 section + 变量）
    ↓  R1/R2/R3 注册期校验
进程启动自检：对全部 scope 跑一次空组装（fail-fast）
    ↓
assemble(scope, variables)
    ↓  筛选 scopes ∋ scope → 稳定排序 (order, name)
    ↓  R4 变量有值 / R5 identity 存在 / R7 非空
    ↓  按 target 分组渲染
AssembledPrompt { system_text, meta_user_text }
    ↓
ContextBuilder
    ├── system_text  → prepend 为列表首条 SystemMessage（既有路径，ADR-0020a）
    └── meta_user_text → 运行时组装，插在**当前用户消息之前**，**永不落盘**
```

### 3.5 校验规则

| 规则 | 内容 | 时机 |
|---|---|---|
| R1 | section 名非空且唯一 | 注册期 |
| R2 | scope 标签合法 | 注册期 |
| R3 | 变量名合法 | 注册期 |
| R4 | 模板引用变量在组装时有值 | 组装期 |
| R5 | 每个 scope 恰有一条 `identity` | 组装期 |
| R6 | 排序确定性（`(order, name)` 稳定排序） | 设计保证 |
| R7 | 组装产物非空 | 组装期 |
| — | 全 scope 空组装演练 | **进程启动自检** |

### 3.6 集中 order 表

```
-1000  harness:identity              （预留，P0 不启用）
    0  persona:prefix                （env 覆盖前缀）
  100  profile:<name>:identity
  200  profile:<name>:*              （profile 专属补充段）
 1000  output:summary                （共享产出要求）
 2000+ tool:<name>                   （工具 guidance，order 再按名）
 3000  aux:compaction
 3100  aux:memory_extraction
 3200  aux:fork_tail
 9000  frame:untrusted_data
 9100  corrective:tool_failure_guard
 9500  runtime:context_snapshot      （meta_user 目标）
10200  persona:suffix                （env 覆盖后缀）
```

### 3.7 `meta_user` 快照的纪律（本设计最关键的安全边界）

已验证事实：`injected_by` 标记**只**被 `user_turn_count` 消费；持久化、`derive_messages()` 回放、记忆抽取**三条链路全部不设防**，且单独一条注入消息即令 `has_user_message` 为真、使 LLM 抽取的 USER 候选**不被降级为 SESSION**。

因此 `meta_user` 快照：

1. **不调用 `session.append`**——不落 JSONL；
2. **不进 `derive_messages()` 的事件投影**——因为不在 session events 里；
3. **不进记忆抽取**——因为抽取的输入来自 session events；
4. 由 `ContextBuilder` 在每次 build 时**运行时组装**，插在当前用户消息之前（列表内永远只有一份，旧的不累积）。

结果：三个污染面一次性消失，**不需要新增任何过滤器**。

---

## 4. 参考实现对照（摘要）

完整表见 ADR-0023。三条关键结论：

1. **严格插值是稀缺品**——四家中只有 DeepSeek Harness 做到未知变量抛错；pi 与 ZCode 静默渲染为空。本设计选严格。
2. **工具自注册片段是可组装的价值来源**——pi 的每工具 `{snippet, guidelines}` 即 D11 的原型。
3. **不变量校验是普遍短板**——只有 dsh 有专用 invariant 插件且仅覆盖 R1/R3 类；本设计的 R1–R7 + 启动自检超出全部参考实现。

---

## 5. 迁移范围

| 阶段 | 迁移对象 | 行为变化 |
|---|---|---|
| **P0** | `agent/profiles.py` 三条 profile system prompt | **零**（逐字节相同） |
| **P1** | `context/compactor.py:39` 六段式 / `memory/extractor.py:141` 抽取 / `session/fork.py:97` tail 摘要 + persona env 覆盖 | 文本等价搬迁；persona 是新增能力 |
| **P2** | 工具 `description`/guidance / 运行时上下文快照 / `runtime.py:928` 纠偏 + `knowledge/tools.py:27` untrusted 框架 + `recovery/coordinator.py` 裁决 | 前两者新增能力；第三者纯搬迁 + 顺带修 extractor 过滤 |

---

## 6. 票拆分（8 票）

### P0（机制，零行为变化）

| 票 | 标题 | Blocked by |
|---|---|---|
| T1 | **Prompt 注册表骨架** — `PromptSection` 数据模型 + `PromptRegistry`（register / variable / available，重名抛错）+ 集中 `SECTION_ORDERS` 表 + 自研严格模板器 + R1/R2/R3 注册期校验 | 无 |
| T2 | **组装与校验管线** — `sections(scope)` 筛选 + `assemble(scope, variables)` + `requires` 自动推导 + R4/R5/R7 + 进程启动自检 | T1 |
| T3 | **迁移三类 profile + 接线** — `profiles.py` 三条 system prompt 迁为 section；`assembly.py` 改为调 `assemble()`；组装产物与现有字符串**逐字节相同**断言；既有 3 个契约测试保持绿；新增 scoping / 校验测试 | T2 |

### P1（辅助 prompt + persona）

| 票 | 标题 | Blocked by |
|---|---|---|
| T4 | **辅助 LLM prompt 迁移** — 压缩六段式 / 记忆抽取 / fork tail 三处迁为 section；引入 `tail_text` 等变量；断言渲染结果与原文等价 | T3 |
| T5 | **Persona env 覆盖** — `AGENT_PERSONA` JSON 解析 + prefix/suffix 覆盖语义 + 非法 JSON / 未知键 / 超长值的失败处理与测试 | T3 |

### P2（工具 guidance + 运行时上下文 + 框架消息）

| 票 | 标题 | Blocked by |
|---|---|---|
| T6 | **工具 guidance 归集** — `Tool` 契约加 `prompt_guidance`（与 `side_effect`/`permission`/`reconcile_hint` 同构）；注册时自动归集为 `tool:<name>` section | T3 |
| T7 | **运行时上下文快照** — 非持久化运行时组装 + `meta_user` 注入路径（当前用户消息之前）+ Q19 内容清单 + prefix cache 稳定性测试 + **非持久化不变量测试**（断言快照不落 JSONL、不进 derive、不被抽取） | T3 |
| T8 | **纠偏/框架消息迁移 + 注入污染修复** — `runtime.py:928` 纠偏 / `knowledge/tools.py:27` untrusted 框架 / `recovery/coordinator.py` 裁决迁为 section；**顺带修** extractor 对 `injected_by` 的过滤 + 回归测试 | T3 |

### 依赖图

```
T1 → T2 → T3 ─┬→ T4
              ├→ T5
              ├→ T6
              ├→ T7
              └→ T8
```

T4–T8 之间无依赖，可并行。

---

## 7. 验收与门禁

### 每票门禁

1. TDD：先红后绿。
2. `uv run pytest`（该票新增 + 相邻回归）。
3. `uv run ruff check src/ tests/` 干净。
4. `git diff --check` 干净。
5. code-review 双轴（Standards + Spec），零 finding 后 commit。
6. commit 消息含票号与 ADR-0023。

### P0 专项验收（关键）

- **逐字节相同断言**：`assemble(profile=X).system_text` 与迁移前 `BUILTIN_PROFILES[X].system_prompt` **完全相等**——这是"零行为变化"的机器可验证证据。
- **既有契约测试保持绿**：`tests/agent/test_system_prompt_wiring.py`（3）、`tests/context/test_builder_system_prompt.py`（5+）。
- **scoping 可证伪**：`assemble("profile:coding")` 不含 `main` 的 section，反之亦然。
- **严格性可证伪**：模板引用未声明变量 → 抛错（而非空串）。
- **启动自检生效**：人为破坏一条 section 使某 scope 缺 identity → import 时 fail-fast。

### 阶段门禁

每阶段结束跑全量 `uv run pytest -q`，零失败。

---

## 8. 风险与未决

| 风险 | 等级 | 缓解 |
|---|---|---|
| P0 迁移引入行为漂移 | 低 | 逐字节相同断言 + 既有契约测试保持绿 |
| `meta_user` 快照污染历史/记忆 | 中 | 非持久化纪律（§3.7）+ T7 的专项不变量测试 |
| 自研模板器的边界（嵌套 `{{`、未闭合） | 低 | T1 覆盖非法语法用例 |
| 快照含工具清单的 token 冗余 | 低 | 已接受；Q19=C 清单可配置，后续可测 |
| 工具 guidance 分两处作者位置 | 低 | D11 刻意的取舍，换取工具内聚 |
| `requires` 自动推导对复杂模板的准确性 | 低 | 只支持 `{{name}}` 单一语法，无嵌套，扫描确定 |

### 未决（不阻塞 P0）

- `meta_user` 段在**多步 run 内**（工具调用中途）的注入位置精确定义——需在 T7 实现时确定并测试（候选：最后一条 `HumanMessage` 之前）。
- prefix cache 命中率的实测验证（当前是设计推理，非测量）。

---

## 9. 附：票外发现（仅报告，不在本 PRD 范围）

1. **`pyyaml` 未声明依赖**——`skills/discovery.py:95` 使用 `yaml.safe_load`，但 `pyproject.toml` 未声明 `pyyaml`，目前靠传递依赖侥幸可用。若未来引入 YAML 配置需先修。
2. **`injected_by` 保护不完整**——详见 §3.7。本设计的 P2/T8 顺带修 extractor 侧，但 `derive_messages()` 回放侧**未修**（因为从投影里过滤会让模型看不见该消息，需单独评估）。
3. **运行时上下文注入此前完全空白**——P2/T7 是首次引入，无历史包袱也无既有测试保护。
