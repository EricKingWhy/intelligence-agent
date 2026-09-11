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
| #164–#168 | TODO | |
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
