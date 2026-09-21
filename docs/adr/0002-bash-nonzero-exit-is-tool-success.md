# bash 非零 exit_code 归为 Tool 调用成功

bash 工具的 `ToolResult` 永远 `ok=True`（除非 Sandbox 本身崩溃），shell 命令的
`exit_code / stdout / stderr` 放进 `ToolResult.data` 供模型读取。非零 exit_code（如 pytest 不通过）
不是 Tool Runtime 异常——它不进 `ErrorCode` 词汇表、不触发 `ToolExecutor` 的重试层。

**Considered Options**:
- 新增 `ErrorCode.NONZERO_EXIT` 把 exit_code!=0 标为失败（被否）：违背 SourcePlan 核心命题
  "命令业务成功 ≠ Tool 调用成功"，且会让 `ToolExecutor` 的 `retryable` 驱动误把测试失败当
  可重试错误，污染 Day4 已冻结的 `ErrorCode` 语义。

**Consequences**:
- `ErrorCode` 词汇表保持六类不变，命令业务失败的语义在 `data.exit_code` 里读，不在错误码里。
- 模型必须自己读 stdout/stderr 判断"是不是真失败了"——这是预期行为，不是缺陷。
- 未来若要"测试失败自动重跑"，应由 Agent 层决策，不是 ToolExecutor 自动重试。

---

**修订（2026-09-21，ADR-0039）**：上面「永远 `ok=True`」有一个例外——**执行预算到期**
（`ExecResult.timed_out`，在本工具内不再区分预算来自 Executor 还是后端默认值）。
预算到期不是命令自己的结论，工具改用 `ok=False / error_code=TIMEOUT` 表达，
沙箱已捕获的 `exit_code / stdout / stderr / sandbox_duration_ms` 移入 `metadata`
（截断上限见 ADR-0039 D5）；`retryable` 按 `side_effect` 派生（MUTATING ⇒ False），
与 Executor 的 `from_timeout` 同一规则。
本 ADR 对「命令**业务失败**」的判定不变：非零 exit_code 仍是成功调用。
机制、取舍与已知限制见 `0039-tool-executor-owns-absolute-deadline.md`。
**范围**：本条修订只覆盖 bash 工具；`git` 工具至今不读 `timed_out`（到期仍报 `ok=True`），
属 ADR-0039 L2 登记的未覆盖面，需单独票。
