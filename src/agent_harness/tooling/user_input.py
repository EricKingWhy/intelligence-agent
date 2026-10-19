"""#1204：产出「持久化用户输入请求」的工具名字集。

`request_constraint_resolution`（窄场景：长期约束冲突）与 `ask_user`（通用版：
运行中提问）执行期都落一条 `USER_INPUT_REQUESTED` 事件并暂停 run。Runtime /
Executor / Session Service / 恢复链按**工具名**识别这两条管线，本常量是唯一
来源——避免每个判定点各写一遍 `== "request_constraint_resolution"` 而漏掉新工具。

放在 tooling 层（而非 `tools/`）：消费方横跨 agent / session / recovery /
multiagent 各层，`tools/` 包 `__init__` 会拉起全部内置工具，反向 import 易成环。
"""

from __future__ import annotations

#: 会暂停 run、等待用户回答的工具名。新增同类工具只改这一处。
USER_INPUT_TOOL_NAMES: frozenset[str] = frozenset({
    "request_constraint_resolution",
    "ask_user",
    # #1356：计划提交同样暂停 run 等真人裁决。超时语义**不**随名字集走——
    # `plan_approval` 由 timeout driver 按 `kind` 排除（审批没有合理默认动作）。
    "submit_plan",
})
