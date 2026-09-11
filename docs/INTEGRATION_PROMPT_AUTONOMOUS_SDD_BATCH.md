# 集成提示词：自主 SDD 批次（#150 / #161–#168 / #149–#160）

> **写给集成 AI**。本文件按票累积追加，每票一节：改了什么 / 验证证据 / 集成注意。
> 分支：`feat/backend`（本 Agent 只做本地 commit，**不 push、不 merge**）。
> 权威进度账本：`docs/PHASE_STATUS.md`。执行协议锚点：`docs/AUTONOMOUS_SDD_PROGRESS.md`。

---

## 0. 批次状态（滚动更新）

| 票 | 状态 | commit |
| --- | --- | --- |
| #150 ARCH-7 单实例锁 | DONE | `9a45a20`（+ 流程锚点 `f3224d9`） |
| #161 PromptRegistry T1 | DONE | `36fd7ef` + `a51fde2` |
| #162 PromptRegistry T2 | DONE | `af6cf44` |
| #163 PromptRegistry T3 | DONE | `aa40fc3` |
| #164 PromptRegistry T4 | DONE | `2c77c3b` |
| #165–#168 | TODO | |
| #149 / #151–#160 | TODO | |

---

## 1. #150 ARCH-7 启动期单实例锁

**commit**：`9a45a20`（流程锚点文档 `f3224d9`）

### 改了什么

- 新增 `src/agent_harness/instance_lock.py`：`InstanceLock(root).acquire()/release()`，
  OS 级 advisory lock（POSIX `fcntl.flock(LOCK_EX|LOCK_NB)` / Windows
  `msvcrt.locking(LK_NBLCK)`），进程内幂等 + 引用计数 + 线程安全。
- 新增 `tests/test_instance_lock.py`（11 条，多数为真实子进程）。
- `src/agent_harness/web/app.py`：`create_app` 的 `lifespan` 取锁（**在
  `setup_logging` 之后**），外层 `finally` 在 `state.shutdown()` 与
  `flush_process_sink()` 之后释放。
- `src/agent_harness/cli.py`：`main()` 取锁后才 dispatch；`--help`/`-h` 豁免取锁
  但仍走 flush 契约；`InstanceLockError` → stderr + `SystemExit(2)`。
- 新增 `docs/AUTONOMOUS_SDD_PROGRESS.md`（流程锚点，非产品文档）。

### 行为变化（**会直接影响集成方的启动方式**）

1. **同一 `WORKSPACE_DIR` 同时只能跑一个进程**。CLI 与 Web 并发被**有意拒绝**
   （#150 AC5）——这不是 bug。集成时若同时开 dev server 和 CLI 命令（例如
   `agent-harness sessions`），第二条会以 rc=2 响亮失败。
2. `--help` / `-h` 不取锁（它不触碰该根）。
3. 逃生门 `ALLOW_SHARED_ROOT=1`：占用时降级为 WARNING 放行，且该 WARNING 会落进
   `${WORKSPACE_DIR}/../logs/agent.jsonl`。**默认关闭**。
4. 锁文件 `${WORKSPACE_DIR}/.instance.lock` 只用于诊断（写 pid / started_at）。
   进程被杀后文件会残留，**内容陈旧不影响取锁**（OS 已释放）。

### 验证证据

- 真实双进程：CLI 第二进程 rc=2，stderr 点名锁路径 + 占用者 pid；
  真实 `uvicorn` 第二实例 `Application startup failed. Exiting.`；
  `taskkill` 持有者后 2s 内新实例起得来（证明 OS 释放锁、残留文件无影响）；
  `ALLOW_SHARED_ROOT=1` rc=0 且 WARNING 出现在 `logs/agent.jsonl`。
- 门禁：ruff clean；全量 pytest `1655 passed / 10 skipped / 39 deselected / 0 failed`
  （含同批 T1 的 26 条）。
- code-review 两轴修复项已收口：注册表 key 改 `realpath+normcase`（避免 Windows
  大小写 / 8.3 短名导致同进程"自锁假阳性"）、线程安全、`--help` 豁免、
  移除死表面（`filename` 参数 / `path` 属性 / `_degraded`）。

### 集成注意

- **Windows mandatory 区间锁**：锁区间取在载荷之外的偏移（`1 << 20`）。若把它改回
  byte 0，锁文件里的 pid 诊断信息会连同进程一起读不出来（错误信息退化为"未知占用者"）。
- 若集成方在 `D:\intelligence-agent`（main worktree）同时跑后端测试与 dev server，
  注意两者共用 `.agent/workspace` 时会互斥；测试用 tmp workspace_dir 不受影响。

---

## 2. #161 PromptRegistry T1 注册表骨架

**commit**：`36fd7ef` + `a51fde2`（code-review 收口）

### 改了什么

新增 `src/agent_harness/prompt/` 包（**纯新增，未触碰任何现有生产路径**）：

| 文件 | 内容 |
| --- | --- |
| `errors.py` | `PromptError(message, *, code)`——形制照抄 `CapabilityError` |
| `template.py` | `_scan` / `render` / `extract_variables`——单趟扫描，不 `re.sub`、不重扫替换值 |
| `section.py` | `Target`（三值）/ `SECTION_ORDERS`（14 键）/ `PromptSection`（`requires` 为 property） |
| `registry.py` | `PromptRegistry`（register / variable / sections / available / declared_variables）+ `AssembledPrompt` |
| `__init__.py` | 按 PRD §10.2 导出，**暂不含 `DEFAULT_REGISTRY`**（T3 加） |

新增 `tests/prompt/`（27 条：模板器 13 + 注册表 14）。

### 关键约束（后续票必须守住）

- **`"*"` 只覆盖 `profile:<name>`**，不匹配 `aux:*` / `frame:*` / `runtime:*`
  （结构性前缀判定）。`test_sections_excludes_wildcard_for_aux_scope` 是那道闸。
- 模板器替换值**原样写入、不再扫描**——注入面从算法上堵死。
- 全部错误走单一 `PromptError` + `code`，测试按 `code` 断言。

### ⚠ 规格冲突（需集成方/T2 决策，本 Agent 未自行改 PRD/ADR）

PRD `docs/PRD_PROMPT_REGISTRY.md` **§10.7** 的 `AssembledPrompt` 代码块只有
`system_text` / `meta_user_text` 两字段；但 §10.4 明确写「`FRAGMENT` 的消费者用
`.fragment_text`（与 `.system_text` / `.meta_user_text` 并列）」，交接文档 §4.2 亦
为三段。T1 按**三段**实现（`system_text` / `meta_user_text` / `fragment_text`）。
**§10.7 视为过期待 T2 修正**——T2 实现 `assemble` 时必须产出 `fragment_text` 段
（否则 FRAGMENT section 无处落），并建议顺手把 §10.7 的代码块补齐。

另一处票面笔误（无需动作）：#161 AC 写「7 个文件按 §10.1/§10.2 创建」，其自身清单
为 8 个；已按清单创建 8 个。

### 验证证据

- `tests/prompt/` 27 passed；ruff clean；`git diff --check` clean。
- 全量 pytest `1655 passed / 10 skipped / 39 deselected / 0 failed`。
- code-review 两轴：Standards 零硬违规（§10.5 正则、§10.6 算法、§10.4 表逐项核对通过）、
  Spec 零缺项（12 条模板测试 + 13 条注册表测试逐条存在且断言符合票面）。
  收口两条 judgement call：scope 校验单点化、补孤立 `}}` 前置分支测试。

---

## 3. #162 PromptRegistry T2 组装与校验管线

**commit**：`af6cf44`

### 改了什么（仍为**纯新增**，未碰生产路径）

- `registry.py` 追加 `PromptRegistry.assemble(scope, variables)` 与模块级
  `run_self_check(registry, scopes)`；`__init__.py` 增加两者导出。
- 新增 `tests/prompt/test_assemble.py`（15）、`tests/prompt/test_self_check.py`（6）。

### 必须守住的语义

- **单 section 时产物 = 原文逐字节**（`"\n\n".join([x]) == x`）。T3 的「零行为变化」
  全靠这一条——迁移后的 prompt 文本必须与原内联字符串 `repr` 相等。
- 分隔符固定 `"\n\n"`；三个 `Target` **各自独立分区**，空段是 `""` 而非 `None`。
- **R4 不在 `assemble` 内重复实现**（`render` 已抛 `missing_variable`）。
- R5 只对 `profile:` 前缀 scope 要求 identity。
- `run_self_check` **不吞异常**（fail-fast 是它的全部意义），变量按 `requires`
  自动填空串——**不能改成传 `{}`**，否则 T4 的 `aux:fork_tail`（含 `{{tail_text}}`）
  会在 import 期误报 `missing_variable`。

### ⚠ 三条给 T3 的交接要点

1. **`builtin` 必须走 `registry.register(...)`**：§10.8 把"模板语法"列为自检职责，
   但实际由 `register`（R3a）承担——坏模板进不了 `_sections`，自检单独发现不了语法错误。
2. **T3 需自建 scope 集推导**（「注册表里所有非 `*` 的 scope」）。T2 的
   `run_self_check` 签名按票面冻结为接收 `scopes` 参数，未提供 `_declared_scopes`。
3. PRD 需随 T3 补齐两处：§10.7 的 `AssembledPrompt` 代码块（补 `fragment_text`）、
   §10.2 导出清单（补 `run_self_check`、`DEFAULT_REGISTRY`）。

### 两条票面测试因结构不可达而改形（**已报告，非偷工**）

1. `test_assemble_duplicate_identity_raises`：`_sections` 以 section 名为 key 且 R1
   拒重名 → 「两条同名 identity」构造不出来，`len(ids)` 只可能 0 或 1，R5 的 `!= 1`
   实际退化为 `== 0`。代码**保留 `!= 1`**（PRD §10.7 逐行照抄 + 防将来换分层注册时
   静默放行），改为新增 `test_duplicate_identity_section_cannot_be_registered`
   锁住该不可达性的**成因**（重名注册必抛 `duplicate_section`）。
2. `test_self_check_still_raises_on_bad_template_syntax`：语法错误在 register 期已拦，
   改名 `test_bad_template_rejected_at_registration`。**若谁把 register 的语法校验去掉，
   这两条会红**，提醒职责必须补回自检。

### 验证证据

- 真实 Python 会话跑通组装管线：单 section `repr` 等价 `True`、三段互不混装、
  空段 `== ""` 且 `is not None`、缺 identity 自检 fail-fast 抛 `missing_identity`、
  含变量 scope 自检不误报。
- `tests/prompt/` 48 passed；ruff clean；`git diff --check` clean。
- 全量 pytest `1676 passed / 10 skipped / 39 deselected / 0 failed`。

---

## 4. #163 PromptRegistry T3 三类 profile 迁移（逐字节相同）

**commit**：`aa40fc3`

### 改了什么

- **新建** `src/agent_harness/prompt/builtin.py`：三条 `profile:*:identity` section
  （文本与迁移前逐字节相同）、公开的 `build_registry()`、`_DECLARED_VARIABLES`、
  `_declared_scopes()`、`DEFAULT_REGISTRY`，以及 import 期的 `run_self_check`。
- `prompt/__init__.py`：导出 `DEFAULT_REGISTRY`。
- `agent/profiles.py`：**只在 `_builtin_prompt(name)` 一处接线**（ADR-0023 D12）。
  diff 仅 import + helper + 三处 `system_prompt=`，其余字段逐字未动。
- **`assembly.py` / `agent/factory.py` 零改动**；既有契约测试断言未改。
- 新增 `tests/prompt/test_profiles_migration.py`（9）、`test_builtin_registry.py`（8）。

### 为什么是单点接线（别在集成时"改回 PRD 字面"）

`agent/profiles.py` 一处接注册表后：parent 走 `assembly.py` 的
`profile_spec.system_prompt`、child 走 `factory.py` 的 `spec.system_prompt`，
两条路径**零改动**即一致，不存在两个调用点漂移的可能。若改成"在 factory 里按
`spec.name` 查注册表"，自定义 `AgentSpec`（B2 契约）与
`multiagent/provider.py` 传入的自定义 profiles 字典会被内置文案覆盖。

### ⚠ 给 T5 的关键约束（本 Agent 此前注释写错，已修正）

交接文档 **§4.4**：`DEFAULT_REGISTRY = build_registry()` **永不读环境变量**；
persona 由装配点 `build_registry(persona=…)` 注入。因此 `_builtin_prompt` 读
`DEFAULT_REGISTRY` 拿到的永远是**不含 persona 的 base 文本**——profile 的逐字节
断言在 T5 之后依然成立。**不要**把 persona 塞进 `DEFAULT_REGISTRY`：那会让
`profile:*:identity` 的等价断言在设了 `AGENT_PERSONA` 的机器上红。
`test_default_registry_equals_build_registry_in_p0` 就是这条不变量的机器化表达。

### 给 T4 的交接

- `_declared_scopes()` 自动覆盖 `aux:*`（不含 `*`），所以 T4 新增 aux section 后
  自检自动覆盖，**无需维护豁免名单**。
- 新 section 只要正文含 `{{var}}`，**必须**先登记进 `_DECLARED_VARIABLES`
  （先 `variable()` 后 `register()`），否则 import 期抛 `undefined_variable`。

### 验证证据

- **逐字节等价**：基线取自 `git show HEAD:src/agent_harness/agent/profiles.py`
  的 `ast` 抽取（**非手抄**），main(143) / coding(93) / research_review(87) 三条
  与注册表组装产物 `==` 为 True（`——`、全角 `（）`、拼接处空格全保留）；
  两轴 code-review 独立复算通过。
- **真实端到端**：装配真实 runtime → 跑真实 turn → 捕获**模型实际收到的
  SystemMessage** → 与注册表文本比对，三条 profile 全部 `True`。
- 三条冻结契约测试 16 passed 且未出现在 diff 中；ruff clean；`git diff --check` clean。
- 全量 pytest `1693 passed / 10 skipped / 39 deselected / 0 failed`。

---

## 5. #164 PromptRegistry T4 三条辅助 prompt 迁移

**commit**：`2c77c3b`

### 改了什么（三处调用点各只改 1 处）

| 现状 | 迁移后 |
| --- | --- |
| `context/compactor.py` 模块常量 `_SIX_SECTION_PROMPT`（**已删**） | `assemble("aux:compaction").system_text`（SYSTEM，**保留结尾 `\n`**） |
| `memory/extractor.py` 内联 SystemMessage 正文 | `assemble("aux:memory_extraction").system_text`（SYSTEM） |
| `session/fork.py` f-string | `assemble("aux:fork_tail", {"tail_text": …}).meta_user_text`（**META_USER**） |

`builtin.py` 追加 `_AUX_SECTIONS` 三条 + `_DECLARED_VARIABLES` 登记 `tail_text`。
新增 `tests/prompt/test_aux_prompts.py`（13）。

### 必须守住的三点（集成时别"顺手修"）

1. `aux:memory_extraction` 正文里的 `[{scope, content, importance}]` 是**单层花括号
   字面量**，不是模板变量。改成 `{{…}}` 会凭空引入一个无人赋值的必填变量 →
   **import 期就崩**。
2. `aux:compaction` 正文**结尾有一个 `\n`**（组装不做 strip）。删掉它，"逐字节相同"
   就是假的；`test_compaction_prompt_preserves_trailing_newline` 专门盯这个
   （T3 的 profile 正文首尾无空白，测不出这类 bug）。
3. `aux:fork_tail` 是 `META_USER`（现状是单条 `HumanMessage`）。target 的判据是
   **消息角色**，与"是否持久化"无关。

### 既有测试改动 3 条（票面只预告 1 条，另 2 条是同类强制改动）

全在 `tests/prompt/`，**均不在 PRD §10.9 保护清单内**；都是 T3 写下、pinned 到 P0
状态的断言，T4 后必然变化，且改后**更严格**：
`test_builtin_registry_has_three_profile_sections`（→ 按 `profile:` 前缀过滤，不再钉总数）、
`test_declared_variables_is_empty_in_p0`（→ 精确集合 `{tail_text}`）、
`test_declared_scopes_excludes_wildcard_and_covers_all_profiles`（→ 精确列表 6 项）。
三条保护清单测试**未出现在 diff 中**。

**⚠ 注意**：`test_builtin_registry_has_six_sections`（精确 6）是票面要求的，
后续每张票加 section 都会顶到它。若集成方觉得摩擦大，可放宽为按 scope 前缀分类计数
——但那是改票面要求，需你决定。

### AC 字面偏差（如实记录，未擅自"补齐"）

票面 AC 要求 `grep -rn "_SIX_SECTION_PROMPT" src/ tests/` 为空；实际返回 2 行
**注释**（`builtin.py` 与 `test_aux_prompts.py` 各一处，写明该文本迁移前的出处）。
**无任何代码残留或依赖**，故保留这两处 provenance 注释。若集成方坚持字面达标，
删掉这两行注释即可。

### 验证证据

- **逐字节等价**：三条 `LEGACY_*` 基线取自 `git show HEAD:<path>` 的 ast 抽取
  （非手抄、非反向拷贝）——compaction 263 字符且尾 `\n` 保留 / extraction 256 /
  fork head 93 + 变量；两轴 review 独立复算全部 PASS。
- **真实端到端**（真类 + 脚本化模型）：`TailSummarizer.summarize` 单条 HumanMessage
  等价；`MemoryExtractor.extract` SystemMessage 等价且花括号保留；
  `ContextCompactor.compact` **真实触发压缩**（`fallback_used=False`、产出摘要），
  发给模型的 system 指令等价且尾换行保留。
- `tests/prompt/` 77 passed；ruff clean；`git diff --check` clean。
- 全量 pytest `1706 passed / 10 skipped / 39 deselected / 0 failed`。

### 既有 flaky（与本批无关，别算到本批头上）

`tests/test_web_api.py::test_disconnect_leaves_run_running_and_cancel_stops_it`
被独立审查者在**同一份代码**上 5 跑 2 失败，clean HEAD 亦通过，且该文件不在本批
任何 diff 内。已按 §8 记入 `docs/FRONTEND_ISSUES_LOG.md` **OBS-9.3**，未顺手修。

---

## §6 T5 — #165 Persona 环境覆盖（commit `2638d68`）

### 做了什么

新增一层 **persona 覆盖**：`AGENT_PERSONA` 环境变量（JSON）可给 system prompt 加前缀/后缀，parent 与 child 一致。

| 文件 | 变化 |
| --- | --- |
| `src/agent_harness/prompt/persona.py` | **新增**：`PersonaConfig` / `parse_persona_config` / `persona_sections` / `apply_persona` |
| `src/agent_harness/prompt/builtin.py` | `build_registry(persona=None)` 注入 `persona:prefix`(0) / `persona:suffix`(10200) |
| `src/agent_harness/assembly.py` | 解析 env → registry 组装父 prompt → persona 透传 Factory |
| `src/agent_harness/agent/factory.py` | `create()` 用 `apply_persona(spec.system_prompt, persona)` 包裹 child prompt |
| `src/agent_harness/config.py` | 新增 `agent_persona: str = ""`（紧跟 `capabilities`） |
| `tests/prompt/test_persona.py`、`tests/test_assembly_persona.py` | **新增**：约 32 个用例 |

### 集成方【不要】做的事

1. **不要给 `DEFAULT_REGISTRY` 加 persona 读取**。它是**零配置基线**，永远不读环境。persona 只经 `build_registry(persona)` 这条显式路径进入。测试同时断言它「等于零配置构建」且「不等于 persona 构建」——两向都锁了。
2. **不要把 config 默认值从 `""` 改成 `None`**。`agent_persona` 与 `capabilities` 同形制（原始 `str`），`parse_persona_config("")` 即空 persona。
3. **不要用 `apply_persona` 去处理 aux prompt**。`"*"` scope 只匹配 `profile:<name>`，这是**结构性边界**（保护压缩/记忆提取等辅助调用不被 persona 污染）。若把 aux 也包上 persona，是在破坏规格而非修 bug。
4. **不要为了「统一」把 `apply_persona` 改名成 `compose_agent_prompt`**。交接文档 §4.5 的旧名与三参签名已过期；票面 prescribed `apply_persona(base, persona)`。
5. **T6 加 tool guidance 时不要改 `apply_persona`**。guidance 的 order(2000) 在 persona 后缀(10200) 之前，且 `apply_persona` 包裹的是**已组装完**的 profile 文本，顺序天然正确。

### 证据

- 门禁：`ruff` clean；全量 pytest **1739 passed / 10 skipped / 39 deselected / 0 failed**；`git diff --check` clean。
- 不动点：零配置产物逐字节等于 T3/T4 迁移后文本；K1/K2/K3 冻结契约与 profile 接线用例**零改动**通过。
- 真实验证：真实 `.env` → `Settings` → `assembly` 四场景（空/前缀/前后缀/坏键），坏键响亮失败不降级。

### 前向兼容注意

- `.env` 新增项 `AGENT_PERSONA`（默认空）——集成方需在真实 `.env` 里同步（若需要），空值零影响。
- 已知边界 OBS-9.6：`harness:identity`(order −1000) 若将来注册，会与 `persona:prefix`(0) 产生顺序歧义，届时漂移守卫会先变红，需先决策再注册。**当前无该 section，行为不受影响。**

---

## §7 T6 — #166 工具 guidance 归集（commit `ef64d44`）

### 做了什么

建立第三条 prompt 通道：**工具自带 guidance**，只在工具确实注册时才进 system prompt（ADR-0023 D11）。

| 文件 | 变化 |
| --- | --- |
| `src/agent_harness/tooling/contract.py` | `Tool` 加可选 property `prompt_guidance`（默认 `None`） |
| `src/agent_harness/prompt/tool_sections.py` | **新增**：`tool_guidance_sections` + `join_guidance` |
| `src/agent_harness/prompt/builtin.py` | `build_registry(persona=None, *, tool_sections=())` |
| `src/agent_harness/prompt/persona.py` | **新增** `compose_agent_prompt`；`apply_persona` 转薄封装 |
| `src/agent_harness/assembly.py` | 注入收窄后 registry 的 guidance；factory 传 `include_tool_guidance=True` |
| `src/agent_harness/agent/factory.py` | `include_tool_guidance`（默认 `False`），guidance 取 `child_registry` |
| `src/agent_harness/multiagent/tools.py` | `DelegateTool.prompt_guidance` |
| 3 个新测试文件 | 30 个用例 |

### 集成方【不要】做的事

1. **不要给 `include_tool_guidance` 改默认值为 `True`**。B2 契约（`tests/agent/test_system_prompt_wiring.py`）断言 `child.system_prompt == spec.system_prompt`；默认开启会让它变成"靠 `ReadTool` 恰好没有 guidance 才绿"。生产装配点已显式传 `True`，改动默认值只会破坏契约而不增加功能。
2. **不要让 child 的 guidance 改读 `source_registry`**。必须是 `child_registry`（已按 `spec.tool_scope` 收窄），否则 child 会看到它无权使用的工具的操作说明（越权信息泄漏）。
3. **不要给任何工具 guidance 写 `{{x}}`**。注册表的变量声明是模块级的（`_DECLARED_VARIABLES`），工具 guidance 无处声明变量，含 `{{x}}` 会在装配期抛 `undefined_variable`。需要动态内容就用 property 动态生成**整段**文本（`DelegateTool` 就是先例：f-string 注入 `_max_delegations`）。
4. **不要把 `prompt/tool_sections.py` 改成 import `agent_harness.tooling`**。它用 `Protocol` 结构类型接工具，是为保持 prompt 包无下游依赖（有测试 `test_tool_sections_does_not_import_tooling` 固定）。
5. **不要为了"省一次计算"给 `profile_spec is None` 分支加条件**。该分支也构建 `prompt_registry`（虽只用它的 `join_guidance` 那条），是工单骨架的刻意形状，开销可忽略。
6. **不要用 `join_guidance` 的返回值判空后再拼**。它无 guidance 时返回 `None`，这是刻意的——返回 `""` 会让 prompt 多出一个空段落，破坏逐字节断言。

### 证据

- 门禁：`ruff` clean；全量 pytest **1769 passed / 10 skipped / 39 deselected / 0 failed**；`git diff --check` clean。
- 冻结契约：`git diff HEAD -- tests/agent/test_system_prompt_wiring.py tests/test_assembly_agent_profile.py tests/context/test_builder_system_prompt.py` **输出为空**（断言逐字未改）。
- 真实验证：真实装配链 + 真实 `AgentRuntime` 跑真 turn（含真实 delegate 委派，3 次模型调用），父提示含「委派须知」且在身份文本之后，child（coding）不含，guidance 未进 tool schema。

### 前向兼容注意

- **无新 env / 配置项**：`include_tool_guidance` 是构造参数，装配点固定传 `True`；集成方无需改 `.env`。
- **待用户裁定的文本重复**（已上报 #166，**不是 bug，不要顺手删**）：`子代理看不到你们的对话历史` 出现在 4 处（`profile:main:identity`、`DelegateTool.prompt_guidance`、`_DelegateArgs.task` description、`DelegateTool.description`）。删除方案已列在 issue comment，动 profile 正文会连带改 T3 逐字节基线。
- T7（运行时上下文快照，order 9500）与 T8（纠偏/框架消息，order 9000~9200）都会往同一张 order 表加 section，**在 persona 后缀 10200 之前**，与本票的 2000 无冲突。

---

## §8 T7 — #167 运行时上下文快照（commit `1a4c41f`）

### 做了什么

把「当前工作目录 / 操作系统 / 日期 / 模型 / 可用工具」（PRD Q19=C）作为**非持久化** `meta_user` 段注入，位置在**当前用户消息之前**。

| 文件 | 变化 |
| --- | --- |
| `src/agent_harness/prompt/builtin.py` | 新增 section `runtime:context_snapshot`（META_USER / 9500 / 非 `*`）+ 五个变量声明 |
| `src/agent_harness/context/builder.py` | 新增 `runtime_context_provider`（默认 `None`）+ `_inject_runtime_context()` |
| `src/agent_harness/assembly.py` | 渲染闭包 `_render_runtime_context()` |
| 3 个新测试文件 | 30 个用例（含污染边界与防假绿对照） |

### 集成方【不要】做的事

1. **不要在 `_inject_runtime_context` 里加 `session.append`**（哪怕"顺手记一笔好排查"）。这一条会同时复活三个污染面：JSONL 永久滞留、`derive_messages` 每轮重放累积、`memory/extractor.py` 的 `has_user_message` 降级保护失效（注入内容可被洗成跨会话 USER 记忆）。这是本票唯一绝对不能犯的错。
2. **不要给 `memory/extractor.py` 加"过滤注入内容"的过滤器**。本设计用"快照不在 events 里"从根上避免；加过滤器是**错的**方向（那是 T8 针对既有 `injected_by` 事件的另一件事）。
3. **不要把快照改成 `SystemMessage` 或拼进 `system_prompt`**。前者会让易变日期毁掉 system-role 的 prefix cache；后者会破坏 T5/T6 与 C4/C5/C7 的逐字节契约。
4. **不要改 `ContextBuilder` 既有参数的名称与位置**。`runtime_context_provider` 是 `*` 之后的 keyword-only、默认 `None`；改成位置参数会影响大量既有构造点。
5. **不要把 `tools` 改成全量 registry**。快照必须用**收窄后**的 registry，否则 coding profile 会列出它调不到的工具（与 T6 收集 guidance 同一原则）。
6. **不要把 `model` 改成 `settings.model_name`**。用户用 `model_name` 参数选目录里的模型时后者可能为空，快照就会报一个假模型名。用 `config.model_name`。
7. **不要为了"省一次计算"缓存快照文本**。provider 是 callable 且每次 build 调用一次是**契约**（跨午夜会话需要新日期）；缓存会让日期过期。

### 证据

- 门禁：`ruff` clean；全量 pytest **1799 passed / 10 skipped / 39 deselected / 0 failed**；`git diff --check` clean。
- 冻结契约：`git diff HEAD -- tests/context/test_builder_system_prompt.py tests/agent/test_system_prompt_wiring.py tests/test_assembly_agent_profile.py` **输出为空**（断言逐字未改）。
- 真实验证：真 turn 下模型收到 `[System(main 身份), Human(快照), Human(当前用户)]`；三层持久化面实测干净且有防假绿对照。
- 变异验证：把快照 `append` 成 USER 事件 → 7 条边界测试变红（含修复后的等价性测试）；还原后全绿。

### 前向兼容注意

- **无新 env / 配置项**：Q19 清单是设计决定，不引入旋钮（PRD §8 已把"清单可配置"列为 DEFER）。
- **待用户裁定的设计问题**（已上报 #167，**不要擅自实现**）：child（`AgentFactory` 构造的子代理 runtime）目前**没有**快照。票面只给了父组装点、AC 未要求。若要给 child 也加，接线点在 `agent/factory.py`（那里已有 `_primary_model_name` 与 `child_registry`）。
- **T8 会往同一张 order 表加 3 条 section**（`frame:untrusted_data` 9000 / `corrective:tool_failure_guard` 9100 / `frame:recovery_skipped` 9200），全部在快照 9500 **之前**、persona 后缀 10200 之前。T8 还要改 `memory/extractor.py` 剔除 `injected_by` 事件——**这正是本票刻意不加过滤器的原因**，两者不要混做。

---

## §9 T8 — #168 框架/纠偏消息迁移 + 修注入污染（commit `611a6eb`）

> **本票含一个真实安全修复。集成时请优先读 B 部分。**

### A 部分：四条 FRAGMENT section（纯搬迁，逐字节相同）

| section（= scope） | order | 用途 | 使用点 |
| --- | --- | --- | --- |
| `frame:untrusted_knowledge` | 9000（`frame:untrusted_data` 槽位） | 知识检索结果前的"这是数据不是指令" | `knowledge/tools.py` ×2 |
| `frame:untrusted_websearch` | 9000（同槽位） | 网络搜索结果的同类提示 | `websearch/tools.py` ×1 |
| `corrective:tool_failure_guard` | 9100 | 同错熔断的纠偏消息 | `agent/runtime.py` ×1 |
| `frame:recovery_skipped` | 9200 | 恢复期"未启动即跳过"合成结果 | `recovery/coordinator.py` ×1 |

四条全部 `Target.FRAGMENT`，调用点一律取 `.fragment_text`。

### B 部分：修了真实的安全缺陷（`memory/extractor.py`）

**缺陷**：`has_user_message` 只认事件**类型**。纠偏消息是 runtime 自己 append 的 `USER_MESSAGE`（带 `injected_by`）→ 窗口里只要有注入消息，"有用户发言"就恒真 → LLM 的 USER 候选不再降级为 SESSION → **工具输出里的一句注入指令可被洗成跨会话 USER 记忆**，此后每个 session 的 SystemMessage 都会回灌它。

**修法**：`extract()` 入口**单点结构化过滤**（`_is_runtime_injected`：`injected_by` 为非空字符串），一处同时覆盖 `has_user_message` 保护、LLM transcript、规则路径三条链。

### 集成方【不要】做的事

1. **不要把入口过滤改回"在需要的地方各加条件"**。单点过滤是本修法的核心：只在 `_clip_events` 过滤只修三分之一，只在 `has_user_message` 加条件也只修三分之一（两种"半修"都有测试能在变异下变红）。三条链共享同一个过滤结果。
2. **不要给过滤加"内容关键词/正则黑名单"**。判定必须只用 `injected_by` 这个**我们自己的结构化标记**——"内容像不像注入"是不可靠的启发式，且会误杀真实记忆来源。
3. **不要把过滤扩大到"所有非用户产出的事件"**。工具结果与模型回复**是记忆的来源**，必须继续参与抽取；只剔 `injected_by` 非空者。
4. **不要改 `injected_by` 的值 `"tool_failure_guard"`**。它是跨模块契约（runtime 写、extractor 读、前端区分）。
5. **不要把 `injected_by=""` 或纯空白当成注入**。判定是 `.strip()` 后非空；有边界测试固定。
6. **不要用 `repr(tool_name)` 填 `corrective` 模板**。模板自带单引号，传 `repr()` 会产出 `''bash''`。
7. **不要把四条 fragment 改成 SYSTEM / META_USER**。它们不是消息，是嵌进 `ToolResult.message` / 事件 content 的文本；改成消息类 target 后调用点会拿到空串（组装分区互不混装）。
8. **不要把两条 untrusted 提示合并成一个 section**（同族但模块不同；合并会让"改网络搜索提示要动知识模块"）。

### 证据

- 门禁：`ruff` clean；全量 pytest **1827 passed / 10 skipped / 39 deselected / 0 failed**；`git diff --check` clean；既有 `tests/memory/` 断言零改动（`git diff HEAD` 为空）。
- 真实熔断：真 turn + 真实 `read` 工具同参连失败 3 次触发真实 SOFT 熔断，注入消息与**从 git HEAD 源码 AST 提取并渲染**的迁移前 f-string 逐字节相同（61 字符）。
- 真实抽取：注入指令未被洗成 USER 记忆（降级 SESSION + provenance）；反向对照（放回真实用户消息）USER 保持 USER。
- 真实恢复：skip 文案与 HEAD 渲染逐字节相同，`CANCELLED` / `retryable=False` 未变。
- AST 提取比对：knowledge / websearch 两条提示 == HEAD 源码常量。
- 变异：删入口过滤 → 7 条红；两种"半修" → 各 5 条红；"键存在即注入" → 4 条红。

### 前向兼容注意

- **无新 env / 配置项**。
- **两处 AC 字面偏差已上报 issue，等用户裁定**（集成时不要"顺手修"）：
  1. AC 要求 `grep -rn "_RESULT_DATA_UNTRUSTED_NOTE" src/ tests/` 无输出，实际 2 行命中，**均为 `prompt/builtin.py` 的 provenance 注释**（常量与使用确已删除）。
  2. 票面测试表一行自相矛盾（单条注入事件过滤后即空集），实现按实际语义拆成两条测试。
- **仅报告未改的潜在不一致**：`session.py:366` 的 `user_turn_count` 用真值判定 `not e.data.get("injected_by")`，本票 extractor 用 `.strip()`；对 `injected_by="  "` 语义不同（当前无产出点）。集成方若要统一，需另行批准。
- **#158（MEM-3 冲突消解 retrieve-before-write）现在可以开工**：它依赖 `memory/extractor.py`，而本票对该文件的改动已收口（改动面：新增 `_is_runtime_injected` + `extract()` 入口两处，其余零改动）。

---

## PromptRegistry 组完成（#161–#168）

| 票 | commit | 主题 |
| --- | --- | --- |
| #161 T1 | `36fd7ef` + `a51fde2` | 注册表骨架（section/template/registry/errors） |
| #162 T2 | `af6cf44` | assemble + R4/R5/R7 + 启动自检 |
| #163 T3 | `aa40fc3` | 三类 profile 迁移（逐字节） |
| #164 T4 | `2c77c3b` | 三条辅助 LLM prompt 迁移（逐字节） |
| #165 T5 | `2638d68` | Persona 环境覆盖（`AGENT_PERSONA`） |
| #166 T6 | `ef64d44` | 工具 guidance 归集（`Tool.prompt_guidance`） |
| #167 T7 | `1a4c41f` | 运行时上下文快照（meta_user，非持久化） |
| #168 T8 | `611a6eb` | 框架/纠偏消息迁移 + 修注入污染 |

全量门禁 1655 → **1827 passed**（+172 用例），全程 0 failed。8 张 issue 全部关闭。

### 组级集成注意（跨票）

- **`.env` 新增项仅 `AGENT_PERSONA`**（T5，默认空 = 零行为变化）。其余票无新配置。
- **注册表现状**：11 条 section（3 profile + 3 aux + 1 快照 + 4 框架/纠偏），8 个声明变量（`tail_text`/`cwd`/`os`/`date`/`model`/`tools`/`tool_name`/`consecutive_failures`）。
- **冻结契约**（全程断言未改）：`tests/agent/test_system_prompt_wiring.py` B1/B2、`tests/test_assembly_agent_profile.py` C1–C7、`tests/context/test_builder_system_prompt.py` G1–G4。
- **两条待用户裁定的设计/内容问题**（已在各票 issue 记录）：①T6 的 guidance 与 `profile:main:identity` 存在逐字重复；②T7 的 child（子代理 runtime）没有运行时快照。
