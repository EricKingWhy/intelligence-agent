"""Session 层的请求上下文 ContextVar（跨层传 run 归因，不改变 Provider 协议）。

为什么放 session 层：memory / context provider 比 agent 层低，不能反向 import
agent；它们只需要"当前 run 是谁"。runtime 在 begin_run 之后设置，可选消费方
（如 MEMORY_DEGRADED 的 run_id 归因）读取；不读则零影响。
"""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_harness.session.event import SESSION_STARTED

if TYPE_CHECKING:
    from agent_harness.session.event import SessionEvent
    from agent_harness.session.session import Session

#: 当前 run 的 id（begin_run 之后有效）；None = 不在 run 上下文中。
run_context_var: ContextVar[str | None] = ContextVar("run_context", default=None)

#: 当前 run 绑定的 Session 实例（W-26 / #380）。`update_plan` 这类**要写会话事件**
#: 的工具在执行期读它——`Tool.execute(args)` 协议不带 session（contract.py），而
#: 生产装配里工具注册发生在 Session 对象存在之前（build_runtime），构造注入接不上；
#: 与 `run_context_var` 同一设点（runtime begin_run 之后 set、收尾 reset）是既有
#: 通道（output_stream / deadline 的 contextvar 同族）。None = 不在 run 上下文：
#: 消费方如实失败，不假装可用。
current_session_var: ContextVar[Session | None] = ContextVar(
    "current_session", default=None,
)

#: 本 run 已作为自动注入进模型上下文的记忆 entry id（#202 / ADR-0031 D4）。
#: provider 在 `select()` 把实际被拼接进 SystemMessage 的 id 写入（frozenset
#: 写时替换，不共享可变集合）；runtime 在 run 开始设空集合、收尾 reset。
#: `retrieve_memory` 读它打 `injected` 标；无 run 上下文时默认空集合（全 false，
#: 不抛异常）。刻意不落 SessionEvent（不变量 #4：事件是运行事实非诊断明细）。
memory_injected_ids_var: ContextVar[frozenset[str]] = ContextVar(
    "memory_injected_ids", default=frozenset(),
)


@dataclass(frozen=True, slots=True)
class ConstraintToolContext:
    """Immutable provenance for constraint tools in one model request."""

    session_id: str
    run_id: str
    agent_id: str
    agent_profile: str | None
    source_event_id: str
    source_seq: int
    source_content: str
    protected_fact_token_budget: int
    user_input_request_rejection_code: str | None = None
    #: 当前会话是否为委派子会话（#1085 兜底判据）。约束工具只属于直接服务
    #: 用户的根会话（#663 AC01）；恢复后的 child 不得获得有效入口。
    is_delegated_child: bool = False


def session_is_delegated_child(events: Sequence[SessionEvent]) -> bool:
    """判定会话是否为委派子会话（#1085 执行期兜底用）。

    标记定义与 `session/service.py::_delegated_child_agent_profile` 同源：
    首事件为 session/started 且 data 带 `delegation_tree_id`。
    与那边不同：标记存在但 agent_id 不可解析时不抛错，直接判 True
    （fail-closed：无法重建授权就不放行）。
    """
    if not events:
        return False
    first = events[0]
    return (
        first.type == SESSION_STARTED
        and "delegation_tree_id" in (first.data or {})
    )


current_constraint_tool_context_var: ContextVar[ConstraintToolContext | None] = (
    ContextVar("current_constraint_tool_context", default=None)
)
