# Independent Review / Debug Playbook

> 本文承接 `AGENTS.md` §4.1–§4.2 的按需检查清单。只有进入独立审查或疑难 Bug 调查分支时读取；
> 角色边界、凭证红线和施工授权仍以 `AGENTS.md` §4 为准。

## Independent Review

必须同时审查代码正确性与当前 Engineering Specification 一致性，并覆盖：

- 逻辑与边界条件；
- Async、并发与 Race Condition；
- 状态一致性；
- SessionEvent 不变量；
- Tool Call / ToolResult 配对；
- Operation Ledger / Recovery；
- Context 污染；
- Capability 边界；
- 测试缺口；
- 不必要复杂度。

完成判据：每个 finding 都能指向具体代码或规格证据；没有 finding 时也必须说明审过的范围与风险面，
不能只以“测试通过”或“代码能跑”作为结论。

## Difficult Bug Investigation

执行顺序：

`复现 → Trace / JSONL / SessionEvent → 假设 → 验证 → Root Cause → 最小修复 → 回归`

优先使用项目自己的可观察链路。涉及 Crash 或 Tool 副作用时，必须同时检查：

- SessionEvent；
- Checkpoint；
- Operation Ledger；
- Sandbox 状态；
- Artifact；
- `tool_call_id` consistency。

完成判据：复现能稳定区分修复前后；根因解释全部观测证据；最小修复通过针对性回归，并如实登记
仍未覆盖的竞态、环境限制或恢复语义。
