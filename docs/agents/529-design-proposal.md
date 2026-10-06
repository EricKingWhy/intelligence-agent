# issue #529 设计方案：Skill 沉淀闭环（成功 run → 验证 → Skill）

> 状态：**DESIGN PROPOSAL — 待用户裁决，未施工**（2026-10-06）
> 归属：intelligence-agent issue #529（BUILD 型）
> 分工实况：Claude Code 经中转站无联网权限、复杂起草 prompt 下工具循环不可靠（伪造工具调用格式）；代码摸底与成熟产品一手调研由 Muse 完成，设计文档由 Muse 起草。

---

## §0 一句话方案

**run 末由模型按"沉淀判据"提议候选 Skill → 模型起草标准 SKILL.md 草稿 → 静态 lint + 用户确认 → 写入项目 skill 目录并刷新 registry → 全程事件留痕。**
第一版只做最小闭环：不做隔离重放（lint + 人审），不做 Marketplace（SPEC 09 明文排除）。

---

## §1 背景与现状

### 1.1 已有（KEEP）

| 组件 | 位置 | 事实 |
|---|---|---|
| `LoadSkillTool` | `src/agent_harness/skills/tool.py:38-97` | `side_effect=READ_ONLY`（65-66）、`permission=READ_ONLY`（68-70），`execute` 经 `asyncio.to_thread` 读盘（72-97） |
| `SkillCatalog` | `src/agent_harness/skills/discovery.py:89-95` | dataclass：`entries/errors/conflicts`；**只读，无写入路径** |
| `parse_skill_markdown` | `discovery.py:108` | 解析 SKILL.md frontmatter（name/description/when_to_use） |
| `SkillDiscovery.discover()` | `discovery.py:167` | 目录扫描 → catalog |
| `SkillCapability` | `src/agent_harness/skills/capability.py:9` | catalog()/load(name)/contributes_tools()，load 失败映射为 `CapabilityError` |
| `SkillCatalogContextProvider` | `src/agent_harness/skills/context_provider.py:20` | `select()`（30）渐进披露 |
| skill 目录 | `wiring.py:280-281` | `~/.intelligence-agent/skills`（global）+ `<workspace>/skills`（project） |
| `MemoryExtractor` | `src/agent_harness/memory/extractor.py:179` | `extract(events)`（184），`_heuristic_extract`（292） |
| run 末 consolidation | `src/agent_harness/memory/consolidation.py` | run 收口时的记忆整理 |
| 审计留痕 | `src/agent_harness/memory/audit.py:30,53` | `record_forget` / `record_memory_change`——记忆写入留痕先例 |
| `RUN_COMPLETED` | `src/agent_harness/session/event.py:29` | `run/completed`；注意 `:108` 注释：run/completed 只说明 Runtime 收口 |

### 1.2 缺口（BUILD 本票）

1. **无写入路径**：SkillCatalog 是只读 dataclass；`load_skill` 是 READ_ONLY tool。成功 run 里的经验沉淀不下来。
2. **无验证门**：没有任何机制能把候选 skill 挡在生效之前。
3. **无治理面**：用户无法从闭环里获得"可见/可编辑/可删除"的沉淀 skill。

### 1.3 规格约束

- SPEC 09 §2：Skill V1 = global/project 目录、发现解析、按需 load、手动指定；**不做 Marketplace**；"Skill 是 Context Capability，不等于 Tool"（ADR-0011 Q3 同款表述）。
- Reuse matrix（SPEC 13）：Skills / SKILL.md → **PORT DESIGN**（progressive disclosure）。
- 不变量 #16：Memory = Capability + Context Provider。

---

## §2 方案依据（成熟产品一手调研，对标协议 §1.3）

判定口径 = SPEC 13 复用矩阵：REUSE / ADAPT / PORT DESIGN / BUILD / DEFER。

### 依据 A — Claude Code `/run-skill-generator`（官方文档一手）

- **来源**：`https://docs.anthropic.com/en/docs/claude-code/skills`，读取日期 2026-10-06。
- **机制摘要**：把一次成功运行里奏效的东西（安装命令、环境变量、启动脚本）记录为 recipe，写成 `.claude/skills/run-<name>/` 的 SKILL.md；`/verify` 在无 recipe 时也会自行记录。治理句："Claude edits the recorded file only when it steered a run wrong... so you can commit the file without per-session diffs"——**模型起草、人类 commit（人管治理）**。
- **契合点**：与不变量 #11（权限边界）兼容——模型不直接让 skill 生效，人确认后才登记；与 SPEC 09（Skill 是 Context Capability）兼容。
- **判定**：**PORT DESIGN**——"模型起草草稿 → 人确认 → 登记"三段式；本仓实现自写。
- **License**：公开技术文档，合理引用，无代码复制。

### 依据 B — oh-my-pi `learn` → managed skill（源码双重核实）

- **来源**：dsh-cc 计划文档（2026-09-23/24 对 oh-my-pi 源码实测），`packages/coding-agent/src/tools/learn.ts:102-143`、`tools/manage-skill.ts:65-87`、`managed-skills.ts:20`。第三方笔记，已按协议要求做源码 file:line 双重核实。
- **机制摘要**：`learn` 可选把 lesson 提升为 skill：**先写 memory、再 mint skill**；命名冲突 → `isError + shadowed: true`；skill 写失败 = **部分成功**（memory 保留）；`MAX_MANAGED_SKILL_BYTES = 64_000`（按完整序列化文件计）；**血泪教训**：learn 的提升不刷新 skill registry（只有 manage_skill 调 `refreshSkills`）。
- **契合点**：部分成功语义与本仓 run 末 consolidation（memory 先行）天然兼容；"写入后必须刷新 registry"直接对应本仓 `SkillCapability` 持 catalog 的投影结构——不刷新即出现"写了但不可见"的漂移。
- **判定**：**PORT DESIGN**——写入语义（memory 先行/部分成功）、64KB 上限、refresh 一等接缝。
- **License**：oh-my-pi 为 Apache-2.0；本票只搬机制不抄代码，来源署名保留于本文档。

### 依据 C — Claude Code skill-creator 插件（官方插件）

- **来源**：`skill-creator@claude-plugins-official`（Anthropic 官方插件市场），经第三方研究文档转述官方插件内容，读取日期 2026-10-06。
- **机制摘要**：经验型 eval 循环：draft → 2–3 个真实测试 prompt → with-skill/baseline 并行 → 人看 HTML 评审 → 迭代；反僵化（禁 ALWAYS/NEVER 大写）、反过拟合；description 当 ML 问题优化（~20 个触发 eval、60/40 train/test 切分）。
- **契合点**：eval 循环成本高，与本票"简单优先"冲突——第一版只取其**静态可检查子集**（反僵化规则、description 触发语要求）；完整 eval 循环 DEFER。
- **判定**：**ADAPT**（lint 规则子集）+ **DEFER**（eval 循环，V2 视 lint 效果再定）。
- **License**：公开方法论文档，合理引用，无代码复制。

### 依据 D — Agent Skills 开放标准

- **来源**：`https://agentskills.io`（经 Anthropic 官方文档引用），读取日期 2026-10-06。
- **机制摘要**：SKILL.md 格式标准；frontmatter 字段：name / description（推荐）/ when_to_use / allowed-tools / disallowed-tools / model / metadata / license / compatibility。
- **契合点**：本仓 `parse_skill_markdown` 已按此解析，无冲突。
- **判定**：**REUSE**（格式标准）。
- **License**：开放标准，自由采用。

### 依据 E/F — 本仓既有机制

- **E：`memory/audit.py:53` `record_memory_change`** → skill 注册/更新/删除的事件留痕机制。**REUSE**。
- **F：`discovery.py:108` `parse_skill_markdown`** → lint 直接复用同一解析器（单真相，避免两套校验漂移）。**REUSE**。
- **G：`claude plugin validate`**（官方文档，2026-10-06）→ lint 规则对标。**PORT DESIGN**。

---

## §3 "成功 run" 判定标准（什么算可沉淀）

`run/completed` 是必要非充分条件（`event.py:108`：它只说明 Runtime 收口）。可沉淀需同时满足：

1. **目标达成**：run 的 plan/目标被标记完成（或用户明确确认成功）。空转、失败重试成功的 run 不算。
2. **证据充分**：关键步骤有 tool 结果支撑（event stream 可查），非纯模型臆想；步骤可复述为通用流程（不是一次性运气）。
3. **可复现性**：同样输入下流程可重复；含外部偶然因素（某网站当时恰好可用）的不沉淀，或在正文标注前置条件。
4. **去重**：与现有 skill（name/description 相似）及 semantic memory 不重复——重复的走"更新既有 skill"分支。
5. **负面清单**（一票否决）：含凭证/密钥/用户隐私的不沉淀；含不可逆危险动作（删库、发外网）且无显式标注的不沉淀；单次偶发成功不沉淀。

**触发方式（第一版）**：手动触发为主（用户指令或模型在 run 末 consolidation 时按上列判据**提议**，用户确认才进入草稿）。run 末自动全量扫描 = DEFER（简单优先，避免噪音 skill 泛滥——对标 oh-my-pi learn 是显式调用而非自动）。

---

## §4 候选 Skill 生成形态

- **谁生成：模型**（对标 A：`/run-skill-generator` 是模型从成功 run 里提炼 recipe）。程序抽取（`MemoryExtractor._heuristic_extract`）只能做启发式裁剪，提炼"怎么做"需要模型理解。判定：PORT DESIGN（A）。
- **输入**：该 run 的 event stream（经 extractor 裁剪）+ consolidation 产出 + 去重扫描结果。
- **草稿格式**：标准 SKILL.md（依据 D），frontmatter 必备 `name` / `description`，推荐 `when_to_use`（触发条件）与 `allowed-tools`（所需工具声明）；正文按 CONTEXT → INSTRUCTIONS → EXAMPLES 组织（对标 skill-creator 插件的正文结构）。
- **草稿态**：先落盘到**待审 staging 区**（`<workspace>/skills/.staging/<name>/SKILL.md`），**不进 catalog**。状态机：`draft → lint-pass → human-approved → registered`，任一步失败回 `draft`（可改）或丢弃。

---

## §5 验证环节（第一版：静态 lint + 人审）

**第一版做静态 lint，人审做最终门；隔离重放 DEFER 到 V2。**

理由：
- 重放需要沙箱 + 结果判定器，成本高且判定器本身难写（skill 的"成功"是开放性的）；
- lint + 人审已能挡住绝大多数坏 skill（格式坏、触发条件空、危险动作未标注、超限、冲突）——简单优先，符合本仓"简单实用"原则；
- 对标：oh-my-pi 的 learn→skill 第一版也没有重放，只有冲突检查 + 大小上限（依据 B）；Claude Code 的 eval 循环是 V2 级别能力（依据 C）。

### 5.1 lint 规则（程序化，全部可测试）

| 规则 | 实现 | 来源 |
|---|---|---|
| frontmatter 可解析，`name`/`description` 非空 | 复用 `parse_skill_markdown`（依据 F）+ #588 的 name 校验 | REUSE |
| 触发条件非空：`when_to_use` 或 description 含触发语 | 新增规则 | BUILD |
| 危险动作标注：正文含删库/删文件/外发网络等模式 → 必须显式声明 `allowed-tools`，否则 lint 失败 | 新增规则（模式表可配置；对标依据 C 的反僵化：危险动作显式化而非藏正文） | BUILD |
| 大小上限 64KB（整个 SKILL.md 序列化后） | 常量 `MAX_SKILL_BYTES = 64_000` | PORT DESIGN（依据 B） |
| name 冲突：与 catalog 现有 name 重复 → 拒绝注册，走"更新既有 skill"分支（`shadowed` 语义） | 扫 `catalog.entries` | PORT DESIGN（依据 B） |
| 反僵化：正文出现 ALWAYS/NEVER 全大写指令词 → 警告（不阻断） | 新增规则 | ADAPT（依据 C 子集） |

### 5.2 人审

用户确认（CLI/Web 确认面，沿用现有 approve 交互范式）。**未经确认的草稿永不进入 catalog**（对标 A 的"人 commit"）。

### 5.3 V2（DEFER）：隔离重放

fork 一个沙箱 run，用 2–3 个合成测试 prompt 跑 with-skill vs baseline（对标 C 的 eval 循环），人看 diff 评审。是否做、怎么做，待第一版 lint 效果数据出来再定。

---

## §6 登记治理

### 6.1 写入路径（新增，核心施工面）

- `SkillDiscovery` 新增 `register(entry) / update(name, entry) / remove(name)`：写文件到 **project skill 目录**（`<workspace>/skills/<name>/SKILL.md`；global 目录仅用户手动维护，闭环不写 global——避免污染用户全局）。
- **每次写入后必须刷新 registry**：`SkillCapability` 持有的 catalog 重建（重新 `discover()`），否则出现 oh-my-pi 式"写了但不可见"（依据 B 血泪教训）。refresh 是写入方法的**一等接缝**（同事务语义：写文件 + 刷新，任一失败整体回滚并报错）。
- 写入走正常 tool permission 流程（见 §8 不变量 #11）。

### 6.2 事件留痕（对标 `memory/audit.py`）

新增 SessionEvent 类型：`skill/registered`、`skill/updated`、`skill/removed`，载荷含 skill name、来源 run id、lint 结果摘要、确认者。append-only，不改历史（§8 #3）。

### 6.3 用户可见、可编辑、可删除

- **文件即真相**：skill 以 SKILL.md 文件形式存在于项目目录，用户可直接编辑/删除（registry 是投影，见 §8 #22）。
- 删除/外部编辑后：下次 `discover()` 自然收敛；删除发 `skill/removed` 事件（由定期 discover diff 或用户动作触发——施工时定，推荐 discover diff）。
- Web/CLI 管理面：第一版只读展示（catalog 列表）；编辑/删除走文件（简单优先）。管理按钮 DEFER。

---

## §7 与记忆模块（episodic/semantic）的关系

**复用，不独立。**

- 沉淀判据的输入裁剪复用 `MemoryExtractor`（`extract(events)` 的事件裁剪逻辑），不另写一套 event 扫描。
- 对标 oh-my-pi（依据 B）：**memory 落盘先行，skill 失败不影响 memory**（部分成功语义）。run 末 consolidation 照常写 episodic/semantic；skill 沉淀是同一 run 的第二条产出线，失败时 memory 保留。
- 不变量 #16：Memory = Capability + Context Provider；Skill 同样是 Context Capability——两者正交：**skill 不写进 memory store，memory 不生成 skill 文件**。去重时互相参照（§3-4），仅此而已。

---

## §8 不变量检查

| # | 不变量 | 本票符合性 |
|---|---|---|
| #3 | append-only（历史只增不改） | skill 注册只追加 `skill/registered` 等事件，不改历史事件 |
| #7 | 统一执行路径（零旁路） | 沉淀流程走 tool + event，不开隐藏写入路径；`register` 经 ToolRegistry 暴露 |
| #11 | 权限边界 | skill 写入是外部可见动作，需用户确认；模型不能静默自助注册 |
| #16 | Memory = Capability + Context Provider | skill 同为 Context Capability，与 memory 正交（§7） |
| #22 | Web/CLI 不维护第二套真相 | 文件即真相，registry 是 discover 的投影 |
| SPEC 09 | 不做 Marketplace | 第一版只做本地沉淀，不做分发/市场 |

---

## §9 测试策略

1. **判据测试**：空转 run（无 tool 调用）的 `run/completed` 不触发提议；失败 run 不触发。
2. **lint 测试**：坏 frontmatter / 空触发条件 / 危险动作未标注 / 超 64KB / name 冲突 / ALWAYS 大写警告——各一条用例。
3. **注册测试**：`register` → 文件落盘 → registry 刷新后 `catalog()` 可见、`load(name)` 可读；`remove` → 不可见。
4. **刷新回归**：写入后未刷新即报错（oh-my-pi 教训的回归测试）。
5. **事件留痕**：`skill/registered` 事件落盘，载荷含 run id + lint 摘要。
6. **部分成功**：skill 写失败（磁盘只读模拟）时 memory consolidation 产出仍保留。
7. **人审门**：未确认的草稿永不进入 catalog（staging 残留可清理）。

---

## §10 开放项（只给推荐，不代拍）

1. **触发方式**：第一版只做手动触发，还是 run 末模型自动提议？
   → 推荐：手动触发先行（简单，噪音少）；自动提议 DEFER 到 V2。
2. **staging 区位置**：内存还是落盘？
   → 推荐：落盘 `<workspace>/skills/.staging/`（可恢复、可审计）。
3. **删除/外部编辑的事件**：定期 discover diff 还是用户动作触发？
   → 推荐：discover diff（简单，与现有 discover 节奏一致）。
4. **隔离重放进 V2 的条件**：lint 拦截率低于多少时启动？
   → 推荐：第一版跑 1–2 个月看数据再定。
5. **更新既有 skill 的合并策略**：模型重写全文 vs 程序化 patch？
   → 推荐：模型重写 + lint + 人审（与新建同流程），`shadowed` 标记旧版本。

---

## 附：施工切分建议（供拍板后参考）

1. `discovery.py`：`register/update/remove` + refresh 接缝 + 64KB 常量（PORT B）
2. 新增 `skills/lint.py`：lint 规则（§5.1）
3. 新增 `skills/promote.py`：判据（§3）+ 草稿生成 prompt 模板（§4）+ 状态机
4. `session/event.py`：`skill/registered|updated|removed` 事件类型
5. 测试：§9 用例
