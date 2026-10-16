"""#1204：`request_constraint_resolution` 与 `ask_user` 共享的准入 / 去重语义。

两个工具都在执行期落一条持久化 `USER_INPUT_REQUESTED` 并暂停 run，准入清单
（deadline / 预算余量闸、作用域与委派子会话闸）与「每 run 只许一个待回答请求」
的去重语义逐字一致——唯 `request_constraint_resolution` 额外校验「来源须是可
核实的直接用户原文」。把共享部分提到这一处，两个工具不再各写一份、也不会漂移。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_harness.session.event import USER_INPUT_REQUESTED, USER_MESSAGE
from agent_harness.tooling import ToolResult

if TYPE_CHECKING:
    from agent_harness.session.context import ConstraintToolContext
    from agent_harness.session.event import SessionEvent
    from agent_harness.session.session import Session

#: context 缺席 / 作用域不符时的 reason_code（消费方映射到各自文案）。
SCOPE_REJECTION_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
SCOPE_REJECTION_NOT_ALLOWED = "SCOPE_NOT_ALLOWED"

#: deadline / 预算余量闸被触发时的 reason_code → 文案（两个工具逐字共用）。
ADMISSION_REJECTION_MESSAGES: dict[str, str] = {
    "DEADLINE_CONFIGURED": (
        "当前 run 或 session 配置了绝对 deadline，不能等待用户澄清；未创建问题。"
    ),
    "RESUME_UNAVAILABLE": (
        "剩余预算不足以保证收到回答后同一 run 能继续；未创建问题。"
    ),
}
_ADMISSION_FALLBACK = "无法保证同一 run 在收到回答后继续；未创建问题。"


def scope_rejection_code(
    session: Session,
    run_id: str,
    context: ConstraintToolContext | None,
) -> str | None:
    """共享作用域闸：返回拒绝码或不拒绝（None）。

    - context 缺席 ⇒ ``SOURCE_UNAVAILABLE``（当前模型输入里没有可归因的 run 上下文）。
    - session / run / agent 身份不符，或当前是委派子会话（#1085 #663 AC01）⇒
      ``SCOPE_NOT_ALLOWED``。
    """
    if context is None:
        return SCOPE_REJECTION_SOURCE_UNAVAILABLE
    if (
        context.session_id != session.session_id
        or context.run_id != run_id
        or context.agent_id != "default"
        or context.agent_profile not in (None, "main", "coding")
        or context.is_delegated_child
    ):
        return SCOPE_REJECTION_NOT_ALLOWED
    return None


def pending_user_input_request(
    events: list[SessionEvent], *, run_id: str,
) -> SessionEvent | None:
    """本 run 尚未被回答的待处理用户输入请求（每 run 去重的唯一判据）。"""
    answered = {
        event.data.get("input_request_id")
        for event in events
        if event.type == USER_MESSAGE
        and isinstance(event.data.get("input_request_id"), str)
    }
    return next(
        (
            event for event in reversed(events)
            if event.type == USER_INPUT_REQUESTED
            and event.run_id == run_id
            and isinstance(event.data.get("request_id"), str)
            and event.data["request_id"] not in answered
        ),
        None,
    )


def admission_rejection_message(code: str) -> str:
    return ADMISSION_REJECTION_MESSAGES.get(code, _ADMISSION_FALLBACK)


def rejected_result(reason_code: str, reason: str) -> ToolResult:
    """准入被拒的统一返回（成功语义——是一次被记录的判定，非工具故障）。"""
    return ToolResult.success(
        message=reason,
        data={
            "status": "rejected",
            "reason_code": reason_code,
            "reason": reason,
        },
    )
