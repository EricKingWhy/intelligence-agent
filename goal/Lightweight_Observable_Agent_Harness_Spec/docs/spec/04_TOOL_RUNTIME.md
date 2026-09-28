# 04 — Tool Runtime

## 1. 目标

所有 Tool 共用一个统一 Runtime：

```text
LLM Tool Call
→ Tool Contract
→ ToolRegistry
→ Pydantic Validation
→ Permission/Risk Policy
→ Dependency-aware Scheduler
→ ToolExecutor
→ Timeout / Error Classification / Retry
→ Operation Ledger
→ ToolResult
→ SessionEvent
→ ToolMessage
```

Local / Knowledge / MCP / Web / SubAgent Tool 均不得绕过。

## 2. Tool Contract

至少包含：

```text
name
description
args_schema
timeout_seconds
side_effect
risk_level
resource_keys?
execute()
```

建议额外支持：
- `idempotency_mode`
- `supports_reconcile`
- `dependency_metadata`
- `result_policy`

模型侧 Tool Definition 与 Runtime Tool MUST 来自同一 Contract，避免名称和 Schema 漂移。

## 3. ToolRegistry

只负责：
- register
- get
- list
- export model definitions

重复 Tool Name MUST 早失败。

Registry MUST NOT 负责：
- retry
- timeout
- session
- LLM routing
- 真正执行

## 4. ToolResult

统一结构：

```text
ok
message
data
error_code
retryable
metadata
artifact_ref?
operation_id?
```

建议 ErrorCode 至少：
- INVALID_ARGUMENT
- TIMEOUT
- TRANSIENT_ERROR
- PERMISSION_DENIED
- NOT_FOUND
- AMBIGUOUS
- EXECUTION_ERROR
- CANCELLED
- REPEATED_TOOL_CALL
- NEED_RECONCILE
- UNKNOWN

## 5. Validation

```text
raw model args
→ args_schema.model_validate()
→ valid: execute
→ invalid: INVALID_ARGUMENT ToolResult
→ model receives result
→ model may self-correct
```

Executor MUST NOT 偷偷替模型猜测/修复参数。

## 6. Retry

只有 ToolExecutor 能进行 Tool 执行域 Retry。

默认可重试：
- TIMEOUT（视 Tool 语义）
- TRANSIENT_ERROR
- 临时服务不可用

默认不重试：
- INVALID_ARGUMENT
- PERMISSION_DENIED
- NOT_FOUND
- AMBIGUOUS
- 确定性业务失败

底层 SDK 自动 retry SHOULD 关闭或明确纳入总预算，避免 Retry Amplification。

预算配额与暂停 MUST NOT 改变本节的责任边界：`ToolExecutor` 仍是唯一能对 Tool 执行域做 retry 的地方；
逻辑调用与尝试的计数区别见 §9.1。

## 7. Dependency-aware Scheduler

冻结新规则：

> 是否并行由**显式数据依赖 + 资源冲突 + side effect**决定，而不是仅按 READ_ONLY/MUTATING 二分。

并行条件：
- 无 `depends_on`；
- 无资源冲突；
- 权限允许；
- Tool Contract 声明允许并行。

例如：
- `read(a.py)` 与 `read(b.py)`：可并行；
- `edit(a.py)` 与 `edit(b.py)`：若资源隔离明确，也可并行；
- `edit(a.py) → pytest`：必须串行；
- B 的参数依赖 A 输出：必须串行。

### V1 限制

V1 MUST NOT 让 Runtime 靠 LLM 自由文本“猜”依赖。

依赖来源：
- `depends_on: [tool_call_id]`
- `resource_keys`
- Tool metadata
- 同文件 / 同 workspace 资源冲突

Scheduler 可构建轻量 DAG，拓扑批次执行。

## 8. Permission / Risk

建议风险等级：
- READ_ONLY
- WORKSPACE_WRITE
- NETWORK
- DESTRUCTIVE
- PRIVILEGED

Policy 结果：
- ALLOW
- DENY
- REQUIRE_APPROVAL

Approval 应是单次 Tool Call 授权，不默认永久升级 Session 权限。

## 9. Operation Ledger

对于可能产生真实外部副作用的 Tool：

- Tool Call 开始前写 `PENDING/RUNNING`；
- 完成后写 `SUCCEEDED/FAILED/CANCELLED`；
- crash 后 RUNNING 进入 UNKNOWN/NEED_RECONCILE。

READ_ONLY Tool 可采用更轻的 ledger policy，但必须保持可追踪性。

### 9.1 逻辑调用计数、显式配额与 deadline 接纳边界

- **计数**：一次**规范化逻辑工具调用**在它被接纳进 `ToolExecutor`（**唯一接纳点**）时计一个
  `tool_calls`；每一次**实际尝试**（含 retry）计一个 `tool_attempts`。retry MUST NOT 产生新的逻辑调用。
  在接纳点**之前**被拒的调用 MUST NOT 消耗配额，且拒绝理由必须可审计。
- **显式配额**：`budget.run.tool_call_limits` 接受**已注册**工具名到正整数**绝对** ceiling 的映射；
  未注册工具名或不合法上限 ⇒ 在**任何工作开始前**拒绝。配额耗尽 ⇒ 阻止该工具的新调用，并走
  `02 §5.2` 的 closeout / 暂停 / 恢复生命周期——**不**改变权限与 retry 语义。
- **默认**：除 `max_delegations=8`（`10 §5.1`）外，MUST NOT 为 read / edit / bash / web / MCP 等工具
  引入任意低默认配额。未配置配额 = 不限，但仍计入 `tool_calls` / `tool_attempts`。
- **deadline 接纳边界**：`deadline_at` 过后 MUST NOT 再接纳新的工具调用；已在途的工具按其**既有**
  timeout / cancel / Operation Ledger 语义收尾，不确定的 mutating 结果转 `NEED_RECONCILE`
  （MUST NOT 盲重跑）。`ToolExecutor` 仍是绝对 deadline 的唯一责任域（ADR-0039）。

## 10. Bash 特殊语义

`bash` Tool Runtime 成功 != 命令业务成功。

例如 `pytest` 返回 exit code 1：
- Tool 调用本身成功；
- `exit_code=1` 是业务结果；
- Agent 根据 stdout/stderr 决定下一步；
- Executor 不得当成 transient network failure 自动 retry。

## 11. Acceptance Criteria

- 重复 Tool Name 失败；
- 参数错误不重试；
- timeout/transient 按策略重试；
- deterministic error 不重试；
- `tool_call_id` 与 result 正确配对；
- 无依赖 Tool 可并行；
- 有依赖 Tool 严格按 DAG 执行；
- 冲突写操作不并发；
- Approval 可阻断并恢复；
- MCP/Knowledge/Coding Tool 均走同一 Executor；
- 一次多调用批次按**逻辑调用**计数（每个规范化调用一次），retry 只增 `tool_attempts`；
- 接纳点之前被拒的调用不消耗配额；显式 per-tool 配额耗尽后该工具新调用被阻并进入暂停/恢复生命周期；
- 未配置配额的工具不限，且未引入任何新增默认配额（`max_delegations=8` 除外，见 `10 §5.1`）；
- deadline 过后不接纳新调用；未知 mutating 结果转 `NEED_RECONCILE` 且不自动重跑。
