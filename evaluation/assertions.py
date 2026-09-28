"""Deterministic assertions（spec 12 §6：代码判断，LLM judge 不做这里的事）。

全部纯函数：输入事件流 / 用例期望，输出结构化指标。零伪造——断不出来
就是 False / 空列表，绝不猜测。
"""

from __future__ import annotations

from collections import Counter

from agent_harness.session import (
    OPERATION_RECONCILE_REQUIRED,
    SESSION_RESUMED,
    TOOL_CALL,
    TOOL_FAILURE_GUARD,
    TOOL_RESULT,
    SessionEvent,
)
from agent_harness.storage import Operation, OperationState


def dangling_tool_call_ids(events: list[SessionEvent]) -> list[str]:
    """悬空 tool_call 检测：有 tool/call 无配对 tool/result 的 id 列表。

    spec 12 §9 初始 Gate：dangling tool call = 0。恢复/委派合成的 tool/result
    与 call 全局按 id 配对（tooling 侧同一语义）。
    """
    called: list[str] = []
    answered: set[str] = set()
    for event in events:
        if event.type == TOOL_CALL:
            called.append(str(event.data.get("tool_call_id")))
        elif event.type == TOOL_RESULT:
            answered.add(str(event.data.get("tool_call_id")))
    return [call_id for call_id in called if call_id not in answered]


def duplicate_confirmed_side_effect_count(
    operations: list[Operation] | None,
    events: list[SessionEvent],
) -> int | None:
    """Measure duplicate terminal Ledger entries and repeat recovery evidence.

    ``None`` means the Ledger evidence is missing, incomplete, or non-terminal;
    callers must not turn that into a passing zero. Session events additionally
    reveal duplicate result confirmations and repeated reconcile decisions.
    """
    if operations is None:
        return None

    call_ids = {
        str(event.data["tool_call_id"])
        for event in events
        if event.type == TOOL_CALL and event.data.get("tool_call_id") is not None
    }
    operation_ids = {operation.operation_id for operation in operations}
    if call_ids != operation_ids:
        return None

    terminal_states = {
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.CANCELLED,
    }
    if any(operation.state not in terminal_states for operation in operations):
        return None

    terminal_counts = Counter(
        operation.operation_id
        for operation in operations
        if operation.state in terminal_states
    )
    duplicate_terminals = sum(
        count - 1 for count in terminal_counts.values() if count > 1
    )
    result_ids = [
        str(event.data["tool_call_id"])
        for event in events
        if event.type == TOOL_RESULT and event.data.get("tool_call_id") is not None
    ]
    duplicate_results = sum(
        count - 1 for count in Counter(result_ids).values() if count > 1
    )
    reconcile_ids = [
        str(event.data["tool_call_id"])
        for event in events
        if event.type == OPERATION_RECONCILE_REQUIRED
        and event.data.get("tool_call_id") is not None
    ]
    duplicate_reconciles = sum(
        count - 1 for count in Counter(reconcile_ids).values() if count > 1
    )
    return duplicate_terminals + duplicate_results + duplicate_reconciles


def tool_selection_ok(events: list[SessionEvent], expected_tools: list[str]) -> bool:
    """tool_selection 断言：run 实际请求的工具集合 == 期望集合（计一次）。

    顺序不敏感（同一批内顺序由调度决定）；多调少调都算失败。
    """
    requested: list[str] = []
    for event in events:
        if event.type == TOOL_CALL:
            name = event.data.get("tool_name")
            if name is not None and name not in requested:
                requested.append(str(name))
    return sorted(requested) == sorted(expected_tools)


def recovery_guard_ok(events: list[SessionEvent]) -> bool:
    """recovery 断言（软熔断路径）：护栏软触发后 run 仍走到成功终态。

    证据链：TOOL_FAILURE_GUARD(level=soft) 在场 + run/completed 存在
    （模型在纠正消息后改变策略并完成），dangling 由调用方统一断言。
    """
    has_soft_guard = any(
        e.type == TOOL_FAILURE_GUARD and e.data.get("level") == "soft"
        for e in events
    )
    has_completed = any(e.type == "run/completed" for e in events)
    return has_soft_guard and has_completed


def kill_resume_ok(events: list[SessionEvent]) -> bool:
    """kill/resume 断言：恢复后的会话补齐合成 tool/result（无悬空）
    且 session/resumed 事件在场。"""
    has_resumed = any(e.type == SESSION_RESUMED for e in events)
    return has_resumed and not dangling_tool_call_ids(events)
