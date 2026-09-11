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
| #162 PromptRegistry T2 | TODO | |
| #163–#168 | TODO | |
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
