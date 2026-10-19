"""会话级**持久目标对象**（#1359 阶段 1，抄 dsh durable goal）。

与 :mod:`agent_harness.session.workflow` 同形三件套：一个投影、一个写入口、
一份 payload 契约。区别只在状态机比工作流档大——目标有 id / revision / phase /
轮次上限，且每次变更携带**变更后的完整快照**（dsh `goal/change` 的做法）：
重放只需取最后一条合法事件，不做增量重算，坏事件也只损失它自己。

- ``effective_goal`` —— 倒序扫 ``goal/changed``，最后一次胜；坏 payload 记
  warning 后继续往下找（与 ``effective_workflow_mode`` 的 warning-continue
  语义逐字对齐）。``clear`` 是墓碑：最后一条是 clear 即"无目标"。
- ``append_goal_change`` —— ``GOAL_CHANGED`` 的**唯一**写入口，硬校验 phase
  转移合法性、revision CAS、``complete`` 只能从 ``active``、``blocked`` 必须带
  reason；任一违反 → 拒绝且**零事件**（与 ``task.py`` 的 handler 同判据）。

**activation 不落盘**（抄 dsh 的关键一条）：它描述"本进程是否允许自动续跑"，
不是目标事实。进程内集合归 ``SessionService``，resume / fork 后为空。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_harness.session.event import GOAL_CHANGED, SessionEvent

if TYPE_CHECKING:
    from agent_harness.session.session import Session

logger = logging.getLogger("agent_harness.session.goal")

#: 目标轮次上限默认值。票面 AC11 取 32（dsh 默认 256，本票刻意更保守）。
DEFAULT_MAX_GOAL_ROUNDS = 32

#: objective 长度上限（与 ``task.TASK_TEXT_MAX_LENGTH`` 同量级：都是逐字持久化
#: 的用户原文；这里独立定义，避免 goal 反向依赖 task 模块）。
GOAL_OBJECTIVE_MAX_LENGTH = 100_000

#: 五段式目标里**可测判据**两段（票面 AC6）：缺任一段 ⇒ ``needs_interview``。
_INTERVIEW_REQUIRED_SECTIONS = ("## Success criteria", "## Verification")

#: 合法操作全集。未知操作是**形状**错误（422），与"当前没有目标"无关——
#: 先判值域再判状态，否则空会话上 ``destroy`` 会被误报成 409。
_OPERATIONS = frozenset(
    {"create", "edit", "pause", "resume", "complete", "block", "clear"}
)


class GoalPhase(str, Enum):
    """durable 生命周期相位（activation 是进程内的另一件事，不在这里）。"""

    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETE = "complete"


@dataclass(frozen=True)
class GoalState:
    """``goal/changed`` 事件流的只读投影（幂等、可重放）。

    ``defined=False`` 是**正常状态**（从未创建 / 已 clear），不是错误。
    ``source_event_id`` / ``source_seq`` 指回产生当前目标的那条事件：目标事实源
    收敛（阶段 0）靠它把 ``user_goal`` 保护事实绑到目标事件上。
    """

    defined: bool = False
    goal_id: str | None = None
    revision: int = 0
    objective: str | None = None
    phase: GoalPhase | None = None
    blocked_reason: dict | None = None
    max_goal_rounds: int = DEFAULT_MAX_GOAL_ROUNDS
    rounds_started: int = 0
    source_event_id: str | None = None
    source_seq: int = -1

    @property
    def needs_interview(self) -> bool:
        """目标过于模糊：缺 ``## Success criteria`` 或 ``## Verification`` 段。

        这是票面 AC6 的**可测判据**——不是"感觉模糊"的自然语言判断。
        """
        if not self.defined or self.objective is None:
            return False
        return not all(section in self.objective for section in _INTERVIEW_REQUIRED_SECTIONS)

    @property
    def authoritative(self) -> bool:
        """是否已有合法目标事件（快照或 ``clear`` 墓碑）。

        ``source_event_id`` 只在命中合法事件时才有值（坏 payload 被跳过），
        所以它精确表达"目标事件源已接管读侧"：此后不再回落到首条用户消息 /
        ``task_text``——``clear`` 就是"没有目标"，而不是"退回去读旧文本"。
        """
        return self.source_event_id is not None

    def to_payload(self) -> dict:
        return {
            "defined": self.defined,
            "goal_id": self.goal_id,
            "revision": self.revision,
            "objective": self.objective,
            "phase": self.phase.value if self.phase is not None else None,
            "blocked_reason": self.blocked_reason,
            "max_goal_rounds": self.max_goal_rounds,
            "rounds_started": self.rounds_started,
            "needs_interview": self.needs_interview,
        }


@dataclass(frozen=True)
class GoalOutcome:
    """写入口的统一结果。``error_kind``：``shape`` = 载荷/转移非法（422）；
    ``conflict`` = revision 对不上（409）——与 ``TaskOutcome`` 同一口径
    （spec 11 §6.1：形状非法 422、状态对不上 409）。"""

    ok: bool
    reason: str | None
    error_kind: str | None
    state: GoalState


def _failure(state: GoalState, kind: str, reason: str) -> GoalOutcome:
    return GoalOutcome(ok=False, reason=reason, error_kind=kind, state=state)


def _decode_snapshot(data: object) -> GoalState | None:
    """解一条非 clear 的 ``goal/changed``；形状不对返回 None（调用方 warning）。"""
    if not isinstance(data, dict):
        return None
    goal_id = data.get("goal_id")
    revision = data.get("revision")
    objective = data.get("objective")
    raw_phase = data.get("phase")
    max_rounds = data.get("max_goal_rounds")
    rounds_started = data.get("rounds_started")
    blocked_reason = data.get("blocked_reason")
    if not isinstance(goal_id, str) or not goal_id:
        return None
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return None
    if not isinstance(objective, str) or not objective.strip():
        return None
    try:
        phase = GoalPhase(raw_phase)
    except ValueError:
        return None
    if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) or max_rounds < 1:
        return None
    if (
        not isinstance(rounds_started, int)
        or isinstance(rounds_started, bool)
        or rounds_started < 0
    ):
        return None
    if phase is GoalPhase.BLOCKED and not isinstance(blocked_reason, dict):
        return None
    return GoalState(
        defined=True,
        goal_id=goal_id,
        revision=revision,
        objective=objective,
        phase=phase,
        blocked_reason=blocked_reason if phase is GoalPhase.BLOCKED else None,
        max_goal_rounds=max_rounds,
        rounds_started=rounds_started,
    )


def effective_goal(events: list[SessionEvent]) -> GoalState:
    """派生会话**当下生效**的目标。

    倒序扫、last-wins。命中一条合法 ``goal/changed``：``clear`` 墓碑 → 无目标；
    快照 → 该目标（并记下来源事件）。坏 payload 记 warning 并继续往前找——不猜
    一个半截目标（与 ``effective_workflow_mode`` 同一条纪律）。
    """
    for event in reversed(events):
        if event.type != GOAL_CHANGED:
            continue
        data = event.data
        operation = data.get("operation") if isinstance(data, dict) else None
        if operation == "clear":
            return GoalState(source_event_id=event.event_id, source_seq=event.seq)
        state = _decode_snapshot(data)
        if state is None:
            logger.warning(
                "goal/changed (seq=%s) payload 非法，跳过继续查找", getattr(event, "seq", None)
            )
            continue
        return replace(state, source_event_id=event.event_id, source_seq=event.seq)
    return GoalState()


def _resolve_objective(objective: object) -> tuple[str | None, str | None]:
    if not isinstance(objective, str) or not objective.strip():
        return None, "objective 必须是非空字符串"
    if len(objective) > GOAL_OBJECTIVE_MAX_LENGTH:
        return None, f"objective 超过长度上限 {GOAL_OBJECTIVE_MAX_LENGTH} 字符"
    return objective.strip(), None


def _resolve_block_reason(reason: object) -> tuple[dict | None, str | None]:
    if not isinstance(reason, dict):
        return None, "blocked 必须带 blocked_reason={code, message}"
    code = reason.get("code")
    message = reason.get("message")
    if not isinstance(code, str) or not code or not isinstance(message, str) or not message.strip():
        return None, "blocked_reason 需要非空 code 与非空 message"
    return {"code": code, "message": message.strip()}, None


def _resolve_max_rounds(value: object, fallback: int) -> tuple[int | None, str | None]:
    if value is None:
        return fallback, None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None, f"max_goal_rounds 必须是正整数，得到 {value!r}"
    return value, None


def append_goal_change(
    session: Session,
    operation: str,
    *,
    objective: object = None,
    blocked_reason: object = None,
    max_goal_rounds: object = None,
    expected_revision: object = None,
) -> GoalOutcome:
    """``goal/changed`` 的**唯一**写入口（#1359 阶段 1）。

    抄 dsh ``GoalService`` 的校验骨架，按本仓的 ``TaskOutcome`` 形状返回拒绝
    原因而不是抛异常（Web 层据此映射 422 / 409，CLI 直接印文案）。**拒绝 = 零
    事件**：校验全部在 ``session.append`` 之前完成。

    合法操作与转移：

    - ``create``：当前无目标，或当前目标已 ``complete``（完成后可开新目标）。
    - ``edit``：改 objective，phase 不变，revision +1。
    - ``pause``：``active`` → ``paused``。
    - ``resume``：``active|paused|blocked`` → ``active``（轮次未超限）。
    - ``complete``：``active`` → ``complete``（票面：只允许从 active）。
    - ``block``：``active`` → ``blocked``，必须带 reason。
    - ``clear``：写墓碑，保留历史（append-only，不物理删除）。

    ``expected_revision`` 给了就必须等于当前 revision，否则 ``conflict``
    （AC3：并发 edit 后者收 409 + 当前 revision）。
    """
    current = effective_goal(session.events)

    if operation not in _OPERATIONS:
        return _failure(current, "shape", f"未知 goal 操作 {operation!r}")
    if expected_revision is not None and (
        not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
    ):
        return _failure(current, "shape", "expected_revision 必须是整数")
    if (
        expected_revision is not None
        and current.defined
        and expected_revision != current.revision
    ):
        return _failure(
            current,
            "conflict",
            f"revision 冲突：请求基于 r{expected_revision}，当前是 r{current.revision}",
        )

    if operation == "clear":
        if not current.defined:
            return _failure(current, "conflict", "当前没有目标可清除")
        session.append(
            GOAL_CHANGED,
            {
                "operation": "clear",
                "cleared": {
                    "goal_id": current.goal_id,
                    "revision": current.revision + 1,
                },
            },
        )
        return GoalOutcome(
            ok=True, reason=None, error_kind=None, state=effective_goal(session.events)
        )

    if operation == "create":
        if current.defined and current.phase is not GoalPhase.COMPLETE:
            return _failure(
                current,
                "conflict",
                f"已有 {current.phase.value if current.phase else 'active'} 目标；"
                "用 edit 修改、clear 清除，完成后才能新建",
            )
        text, error = _resolve_objective(objective)
        if error is not None:
            return _failure(current, "shape", error)
        rounds, error = _resolve_max_rounds(max_goal_rounds, DEFAULT_MAX_GOAL_ROUNDS)
        if error is not None:
            return _failure(current, "shape", error)
        assert text is not None and rounds is not None  # error 已返回

        session.append(
            GOAL_CHANGED,
            {
                "operation": "create",
                "goal_id": f"goal-{uuid4()}",
                "revision": 1,
                "objective": text,
                "phase": GoalPhase.ACTIVE.value,
                "max_goal_rounds": rounds,
                "rounds_started": 0,
            },
        )
        return GoalOutcome(
            ok=True, reason=None, error_kind=None, state=effective_goal(session.events)
        )

    # 形状先于状态：空会话上缺 reason 的 block 也应是 422，而不是 409。
    block_reason: dict | None = None
    if operation == "block":
        block_reason, error = _resolve_block_reason(blocked_reason)
        if error is not None:
            return _failure(current, "shape", error)

    if not current.defined:
        return _failure(current, "conflict", f"当前没有目标，无法 {operation}")

    phase = current.phase
    next_phase: GoalPhase
    next_objective = current.objective
    next_reason: dict | None = None
    next_max_rounds = current.max_goal_rounds

    if operation == "edit":
        text, error = _resolve_objective(objective)
        if error is not None:
            return _failure(current, "shape", error)
        next_objective = text
        rounds, error = _resolve_max_rounds(max_goal_rounds, current.max_goal_rounds)
        if error is not None:
            return _failure(current, "shape", error)
        next_max_rounds = rounds  # type: ignore[assignment]
        next_phase = phase  # type: ignore[assignment]
        if phase is GoalPhase.BLOCKED:
            next_reason = current.blocked_reason
    elif operation == "pause":
        if phase is not GoalPhase.ACTIVE:
            return _failure(
                current, "conflict",
                f"不能从 {phase.value if phase else '?'} 暂停；只有 active 可暂停",
            )
        next_phase = GoalPhase.PAUSED
    elif operation == "resume":
        if phase is GoalPhase.COMPLETE:
            return _failure(current, "conflict", "已完成的目标不能恢复；新建目标或 edit")
        if current.rounds_started >= current.max_goal_rounds:
            return _failure(
                current, "conflict",
                f"轮次已用尽 {current.rounds_started}/{current.max_goal_rounds}，"
                "先 edit 提高 max_goal_rounds 再 resume",
            )
        next_phase = GoalPhase.ACTIVE
    elif operation == "complete":
        if phase is not GoalPhase.ACTIVE:
            return _failure(
                current, "conflict",
                f"不能从 {phase.value if phase else '?'} 完成；只有 active 可完成",
            )
        next_phase = GoalPhase.COMPLETE
    elif operation == "block":
        if phase is not GoalPhase.ACTIVE:
            return _failure(
                current, "conflict",
                f"不能从 {phase.value if phase else '?'} 置 blocked；只有 active 可阻塞",
            )
        next_reason = block_reason
        next_phase = GoalPhase.BLOCKED
    else:  # pragma: no cover - _OPERATIONS 已挡住未知操作
        return _failure(current, "shape", f"未知 goal 操作 {operation!r}")

    data: dict = {
        "operation": operation,
        "goal_id": current.goal_id,
        "revision": current.revision + 1,
        "objective": next_objective,
        "phase": next_phase.value,
        "max_goal_rounds": next_max_rounds,
        "rounds_started": current.rounds_started,
    }
    if next_reason is not None:
        data["blocked_reason"] = next_reason
    session.append(GOAL_CHANGED, data)
    return GoalOutcome(
        ok=True, reason=None, error_kind=None, state=effective_goal(session.events)
    )


__all__ = [
    "DEFAULT_MAX_GOAL_ROUNDS",
    "GOAL_OBJECTIVE_MAX_LENGTH",
    "GoalOutcome",
    "GoalPhase",
    "GoalState",
    "append_goal_change",
    "effective_goal",
]
