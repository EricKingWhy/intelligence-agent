# ADR-0039 — ToolExecutor 拥有唯一绝对执行预算（Bash 路径）

**Status**: Accepted（2026-09-21）
**Date**: 2026-09-21
**Related**: GitHub #244（父票，冻结决策 2026-09-18 grilling）与子票 #256/#257/#258；
`docs/tickets/architecture-audit-remediation-2026-09-18.md` T08；
`docs/adr/0002-bash-nonzero-exit-is-tool-success.md`（本 ADR 是该 ADR 的**唯一例外**的落点）；
`docs/SDD_TICKET_TRACKER.md` B-24 / B-25 段
**决策授权**: #244 的冻结决策原文「`ToolExecutor` 是唯一 deadline owner」；B-24 独立 Spec
审查判 P1 后给出的二选一（「实现单一绝对 deadline/取消边界并补竞态二值测试；或由用户明确批准
修改冻结决策后重定验收」）中的第一项——该评论在 #256 issue 内（2026-09-20）。
**⚠ 边界（未获用户批准，如实登记）**：实现"消费 deadline"必然要动 `sandbox/local.py` /
`sandbox/docker.py`，而 `#256` 票面的 Scope lock 写的是「本票只做契约和红证，不修改
Local/Docker 实现」，#244 冻结决策另要求「不要一个提交跨完三层」。这个偏离**没有用户批准留痕**：
按 AGENTS.md §9.1.1 属需要用户裁决的票面变更，本批次把它登记为待决项——tracker 的待裁决
条目随紧随本 ADR 的 docs 提交落地，#256 的 issue 评论已同步发出；票面**未擅自改写**。
**真实证据**: §4 列了可复算的读数与产物路径（树 = 本文档所在提交）。

---

## 1. Context

改造前 Bash 路径上同时存在**两个互不知情的预算**：

| 层 | 预算来源 | 起算点 |
| --- | --- | --- |
| `ToolExecutor` | `asyncio.timeout(tool.timeout_seconds)`（Bash 覆写为 60 秒） | attempt 开始 |
| `LocalSubprocessSandbox` / `DockerSandbox` | `DEFAULT_EXEC_TIMEOUT` 或调用方传的 `timeout`（同为 60 秒） | `exec()` 收到调用的时刻 |

两层都建预算，后果有三条：

1. 哪一层先到期取决于**调用延迟**（事件循环排队、Docker `ensure_started()` 冷启动、
   线程池排队），不是设计；同一份预算出现两种判定路径。
2. 超时返回时 Executor 已经离开 `await`，而沙箱的杀树/清理动作还在工作线程里跑——
   "超时返回"之后命令仍可能继续改 workspace（#244 AC2 要的"等待清理完成"做不到）。
3. 沙箱按自己的时钟算出的剩余量与 Executor 的判定可以分歧，测试只能靠"把内外时限拉开"
   来避开竞态（`tests/sandbox/test_docker_sandbox.py` 的 0.2s→1.0s 就是这么来的，见该文件
   两条 `# 预算 1.0s / 写入点 2.5s` 注释），等于把竞态写进夹具。

---

## 2. Decision

**D1 — 唯一 owner：`ToolExecutor`。** 每次 attempt 在 `t0 = perf_counter()` 处建立
**一个绝对** deadline（`t0 + tool.timeout_seconds`），放进 `tool_execution_deadline_var`
（`tooling/deadline.py`，与 `tool_output_sink_var` 同款 contextvar 通道），并用
`asyncio.timeout(deadline - perf_counter())` 作为兜底。每 attempt 一份，`finally` 复位；
并行工具各自成 task，互不串味。

**D2 — 后端只消费，不重新起算。** `Sandbox.exec(..., deadline=...)` 收到绝对边界时
一律以它为准，不再用 `timeout` 起相对预算；`timeout` 退化为"调用方名义值 + 退回路径的
默认值"。`deadline` 为 `None` 有**两种**来路：① 不经 ToolExecutor 的直接调用；
② 经过 Executor 但工具没有转发它（`tools/git.py` 仍是这种，见 L2）—— 收到 `None` **不能**
反推"没经过 Executor"。

**D3 — 过期不得产生新副作用。** 收到已过期的 deadline 时，Local 不得 `Popen`、
Docker 不得 `exec_create`（容器启动吃掉预算同样算过期）。

**D4 — `ExecResult.timed_out` 只表示"预算到期"。** 非零 exit_code 不是超时；这一位让
上层把"命令自己失败"与"预算用完"分开（`cancelled` 位仍表示取消）。

**D5 — BashTool 把到期如实映射成失败，并带上它手里的证据。** 沙箱返回
`timed_out=True` 时（无论 deadline 来自 Executor 还是后端默认），工具返回
`ok=False / error_code=TIMEOUT`，`retryable` 按 `side_effect` 派生（与 Executor 同一规则，
MUTATING ⇒ False），并把沙箱已捕获的 `exit_code / stdout / stderr / sandbox_duration_ms`
放进 `metadata`（模型可见）。**可达性如实说明**：两条后端都消费同一个 deadline，到期后还要
走"杀树 + 回收"才返回，而 Executor 的定时器是 loop 回调 ⇒ **生产形状下通常定时器先响**，
走不带 payload 的通用 TIMEOUT（实测见 L1）。payload 只在"沙箱先返回"时到达模型——本 ADR
不为此改取消语义（取舍见 L1），但工具**手里有证据就必须带出去**，不能静默丢弃。
`metadata` 不用 `duration_ms` 作键：Executor 回填时会用同名键覆盖成 attempt 墙钟。
两个通道各**自带上限**（`_TIMEOUT_PAYLOAD_MAX_CHARS = 2000`，超出留首尾 + 明确截断标记）：
`ArtifactOverflowHandler` 只扫 `data` 的 output/content/stdout/stderr/before/after 与
`message`，**不扫 `metadata`** ⇒ 放进 metadata 的大内容不受 Context 预算管（见 L7）。
上界是 `limit + 标记长度`（标记要报出截了多少，算不进 limit），文案按此口径写；`limit <= 0`
返回空串（`text[-0:]` 是整串，那会把上限变成假的）。

**D6 — 取消/超时返回前必须等清理完成。** 工具用 `asyncio.create_task` + `asyncio.shield`
持有执行 worker；被取消时置 `cancel_event` 并 join 到 worker 结束才 `raise`，清理未完成
（worker 抛错）时该异常压过超时/取消结论。取消语义本身不变：**deadline 之前**到达的取消
仍按取消（`CancelledError` 继续上抛），与 `asyncio.timeout` 自己的判定规则一致。

**D7 — Local/Docker 同形。** 两条后端在"过期不起新进程 / 超时杀树 / 部分输出 /
`timed_out` 位 / 取消位"上逐字段对齐。

---

## 3. Consequences / 已知限制（如实登记，不当作缺陷隐藏）

- **L1 竞态不对称（知情取舍）**：两条后端消费同一个绝对 deadline，到期瞬间 Executor 的
  `asyncio.timeout` 回调先于沙箱线程的"杀树 + 回收"（实测空闲收尾 0.44–0.53 秒、16 路负载
  下 1.0–3.3 秒）⇒ 生产路径上几乎总是走 Executor 的通用 TIMEOUT 失败，**不带** payload；
  只有沙箱先返回 `timed_out=True` 的那条路才带。两条路的**分类完全相同**
  （`ok=False / TIMEOUT`），差别在是否携带 payload（文案也不同）。
  实测三臂（本机、生产形状、真子进程）：Local 安静 / Local 阻塞事件循环 3 秒 / Docker 真 daemon
  —— 三臂全部 `tool_raised=CancelledError`、`payload_present=False`。要消掉这个不对称必须让
  工具把 payload 穿过 cancellation（或改写 `asyncio.timeout` 判定），代价是取消语义风险
  （deadline 后的外部取消会被吞成超时），本 ADR **明确不做**。
  专项用例 `test_executor_timeout_carries_partial_output` 是**故意**把沙箱支路钉死才走得到的
  （名义预算 30 秒、沙箱上限压到 1 秒），它证明的是"工具分支的实现正确"，**不是**"生产里模型
  总能拿到部分输出"。
- **L2 覆盖范围只到 Bash（残余，需单独票）**：`tools/git.py:100-102` 走
  `await asyncio.to_thread(sandbox.exec, command)`——不传 deadline、不传 cancel_event，
  连 `timeout` 都不传 ⇒ 沙箱按自己的 60 秒走，Executor 按 `Tool.timeout_seconds` 默认
  10 秒掐断，期间**没有任何人杀进程**。实测复现（假 git 睡 15 秒后写 marker）：
  Executor 10.01 秒返回 `ok=False / TIMEOUT`，marker 在**返回后 5.20 秒**被写入；
  默认 3 次重试形态下 30.02 秒返回，marker 在返回后 **19.80 秒**被写入，且三个被遗弃的进程
  同时在跑——`git_status` 是 READ_ONLY，`_ToolFailure.from_timeout` 因此给
  `retryable=True`，这条超时会**自动重试三次**，每次各留一个活进程。
  同一路径还有另一半：`GitStatusTool` / `GitDiffTool` 的 `execute` **从不读**
  `result.timed_out`，一律 `ToolResult.success(...)` ⇒ 沙箱自己的 60 秒预算到期时模型收到
  `ok=True / exit_code=-1 / "git status 已执行"`（与 D5 在 bash 上消灭的"到期却报成功"同型）。
  属 #244 冻结决策的**未覆盖面**，本 ADR 只登记，需要单独票。
- **L3 超时文案报名义预算**：`命令超时（上限 X 秒）` 里的 X 是 `effective_timeout`
  （调用方名义值），Docker 冷启动吃掉预算后真实剩余更小；工具侧文案报 `self.timeout_seconds`
  同样是名义值。判定不受影响，只影响文案。
- **L4 仓外后端是源码级破坏性变更**：自定义 `Sandbox` 实现若按旧签名写死
  `exec(self, command, *, timeout=None, cancel_event=None, on_output=None)`，BashTool 传
  `deadline=` 时会 `TypeError`（实测：被 BashTool 的宽 `except` 吞成
  `TOOL_EXECUTION_ERROR`）。仓内 9 处签名（`sandbox/base.py` + `sandbox/local.py` +
  `sandbox/docker.py` 三个实现，加 6 处测试替身）已全部同步；规格 05 未固定 `exec()` 签名，
  故不构成规格冲突。
- **L5 清理失败顶掉分类的潜伏耦合**：D6 的"清理异常压过超时"若命中
  `ConnectionError`（分类表里唯一 `retryable=True`），MUTATING bash 会被自动重试。当前
  不可达（Docker 清理失败抛 `RuntimeError`，仓内无 builtin `ConnectionError` 抛出点），
  登记为潜伏耦合。
- **L6 工具耗时上界由沙箱清理时长决定**：超时后 asyncio 定时器不会再响，返回时延
  = 沙箱杀树 + 回收时长（Local ≈ kill + `wait(5)` + reader join；Docker 全程
  `_bounded_call` 有界）。实测空闲 0.44–0.80 秒、负载下可达 3.3 秒。这正是 AC2"等待清理
  完成"的代价；夹具设计必须让"沙箱先到期"与"Executor 名义预算"在**量级上**拉开，
  而不是只差一两秒。
- **L7 `metadata` 不在 Artifact/Overflow 预算内（一般规则）**：Context 预算的机械边界是
  `ArtifactOverflowHandler.maybe_overflow`，它只看 `data` 的六个键与 `message`
  （`tooling/overflow.py:85-93`）。因此**任何**被塞进 `metadata` 的大文本都是绕过路径
  （不变量 #15：大内容走 artifact，模型只拿 summary + ref）。本 ADR 的处置是"进 metadata 的
  payload 自带上限"，不是"扩 overflow 的扫描面"——后者会改动所有工具的共用件，属另一票。
  **本仓库现存的同类实例（先于本 ADR，未修，登记为残余）**：
  `multiagent/tools.py:181-188` 把整个 `json.dumps(payload)` 放进 `metadata`
  （delegate/子代理回传结果，实测样例 ~16.5 KB；`overflow_configured=True` 时无上界）。
  同类欠账还有两处口径：超时 payload 的 2000 与 `Settings.artifact_overflow_chars`
  是两个互不感知的常数（前者是"模型这一眼"的显示预算，后者是 Context 预算，不合并，
  但改动其一时要成对考虑）；只读臂的"可安全重试"在 3/3 那次也照写
  （Executor 已达 `MAX_ATTEMPTS` 时不会再重试，措辞由 Executor 的通用文案统一负责）。

---

## 4. 证据与门禁（本文档所在提交树）

- 机制落点：`tooling/deadline.py`、`tooling/executor.py`（建立/复位）、
  `tools/bash.py`（转发 + 到期映射 + 清理 join）、`sandbox/base.py`（`deadline` 参数 +
  `ExecResult.timed_out`）、`sandbox/local.py`、`sandbox/docker.py`。
- 专项用例（`tests/sandbox/test_exec_hardening.py`）：
  `test_executor_owns_the_absolute_bash_deadline`、
  `test_executor_timeout_waits_for_bash_cleanup`、
  `test_executor_timeout_carries_partial_output`（沙箱支路 + payload）、
  `test_bash_timeout_payload_is_bounded_for_the_model`（payload 上限）、
  `test_bash_timeout_hint_follows_side_effect`（文案与 retryable 同源，只读子类）、
  `test_executor_bash_local_timeout_stops_process_tree`（真进程树）、
  `test_local_expired_absolute_deadline_does_not_start_process`、
  `test_docker_deadline_expiring_during_startup_does_not_create_exec`；
  另有 `tests/sandbox/test_exec_cancellation.py`（取消臂）与
  `tests/sandbox/test_docker_sandbox.py`（真 daemon 的 timeout/cancel 杀伤；
  本轮改注释的两条在本机真 daemon 上实跑 **2 passed / 10.94 s**）。
- 门禁读数（本文档所在树，本机实跑）：
  - 全量 `PYTHONUTF8=1 pytest -q` → **2641 passed / 2 skipped / 42 deselected / 0 failed**
    （450 s；同一树在独立审查者的另一次全量里出现 1 条与本批无关的 `tests/web` 顺序 flake，
    单项重跑 8.49 s 绿）；
  - 专项 `pytest tests/sandbox tests/tooling tests/tools -q` → **362 passed / 1 skipped**；
  - `ruff check .` clean；`git diff --check` clean。
- 红证（payload 上限）：把 `_clip_for_model` 的 `if len(text) <= limit` 变异成 `if True`
  （副本 `.workbuddy/mutant-bound/`）⇒ 该用例红在 `len(stdout) == 50011`。
- 红证（文案与 retryable 同源）：把 `retryable=not mutating` 变异回 `retryable=False`
  + 文案写死 MUTATING（副本 `.workbuddy/mutant-hint/`）⇒ 该用例红在
  `assert False is True`。
- 红证（BashTool 的 payload 一拿掉就必须红）：把 `metadata={...}` 变异成 `metadata={}`
  （副本 `.workbuddy/mutant-final/`，`PYTHONPATH` 前置并已确认导入的是副本）⇒
  `1 failed`，红在 `assert result.metadata["exit_code"] == -1`（`KeyError: 'exit_code'`，
  2.05 s）；真实代码绿。
- 夹具稳定性（P1 修复后的复算）：**P1 那条真子进程用例**
  （`test_executor_timeout_carries_partial_output`，空闲 1.36 s）在 16 路 CPU 负载下连跑
  **3 次全绿**（4.24 / 4.76 / 4.76 s）；两条替身用例（payload 上限 / 文案同源）同一负载下
  0.33–0.60 s。改用 1.0 秒固定余量的旧写法在同一负载下测得 **3 次红 2 次**。
- 红证（截断 helper 的退化入参）：删掉早退的副本 `.workbuddy/r5-nolimit/`（`PYTHONPATH` 前置，
  已确认 `sys.modules['agent_harness.tools.bash'].__file__` 指向副本、副本内无
  `if limit <= 0`）实测
  `limit=0` → 47 字符（文案写"保留首 0 + 末 0"，却把 6 字符整串原样返回）、
  `limit=-5` → 50 字符（"首 -3 + 末 -2"，比入参还长）——"截断"变成放大；
  加早退后两条断言进 `test_clip_for_model_degenerate_limit_returns_empty`（红臂读数见上）。
- 预算红证（#244 AC1 / #256 AC1，本文档所在树）：`.workbuddy/smoke_20260921/red_244_budget.py`
  （一次性探针，`.workbuddy/` 已 gitignore、不入库）驱动同一条 12 秒命令两臂：
  契约默认 `Tool.timeout_seconds = 10.0` ⇒ `ok=False / TIMEOUT / wall=10.4s`（红）；
  `BashTool.timeout_seconds = 60.0` ⇒ `ok=True / wall=12.1s`（绿）。原始输出存
  `.workbuddy/smoke_20260921/red_244_budget.out`。
- 生产形状竞态探针（L1 的读数出处）：`.workbuddy/probe_same_deadline_race.py`、
  `.workbuddy/probe_same_deadline_race_longblock.py`、`.workbuddy/probe_docker_same_deadline.py`、
  `.workbuddy/probe_c1_margin.py`；C7/L2 复现 `.workbuddy/probe_c7_git_residual.py`；
  L4 复现 `.workbuddy/probe_l4_old_signature.py`（均为本地一次性产物，不在仓库内）。
