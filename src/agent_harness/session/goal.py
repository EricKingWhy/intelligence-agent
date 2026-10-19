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

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_harness.session.event import (
    GOAL_CHANGED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)

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

#: 自动续跑消息的来源标记（#1359 阶段 3）。落在 ``message/queued`` / ``user/message``
#: 的 ``origin`` 键上：``rounds_started`` 只统计真正进入模型历史的本来源消息；
#: 重启重建队列时也靠它把 ``origin`` 带回内存镜像。
GOAL_ORIGIN = "goal"

#: 无进展判定的 reason code（票面 AC9）。用户能据此区分"人暂停"与"系统判定原地打转"。
NO_PROGRESS_CODE = "no-progress"

#: 决策 b（#1359 阶段 3）撞审批点时的 reason code：自动续跑的下一轮会停在审批闸门
#: 上（当前权限档不是 danger-full-access），所以置 ``blocked`` 而不是入队。
#: 与 ``NO_PROGRESS_CODE`` 同一读法，但原因不同——这个不是"打转"，是"要人"。
APPROVAL_REQUIRED_CODE = "approval-required"

#: 合法操作全集。未知操作是**形状**错误（422），与"当前没有目标"无关——
#: 先判值域再判状态，否则空会话上 ``destroy`` 会被误报成 409。
#: ``round_started`` 是运行时内部记账（不是用户命令）：REST 面的 Literal 不收它。
_OPERATIONS = frozenset(
    {"create", "edit", "pause", "resume", "complete", "block", "clear", "round_started"}
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
    #: 非用户主动暂停的原因（#1359 阶段 3 无进展检测）。``None`` = 用户暂停
    #: 或从未暂停；与 ``blocked_reason`` 分开，避免把"打转"伪装成"阻塞"。
    pause_reason: dict | None = None
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
            "pause_reason": self.pause_reason,
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
    raw_pause_reason = data.get("pause_reason")
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
        pause_reason=(
            raw_pause_reason
            if phase is GoalPhase.PAUSED and isinstance(raw_pause_reason, dict)
            else None
        ),
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


def render_goal_round_prompt(state: GoalState, round_number: int) -> str:
    """一轮自动续跑的模型可见输入（抄 dsh ``prompt.ts`` + omp 防漂移两句）。

    只渲染**当前激活 revision 的快照**：目标文本以 JSON 字符串嵌入，避免 objective
    里的换行 / 引号破坏外层结构。``round_number`` 是即将开始的轮次（1-based）。
    """
    objective = json.dumps(state.objective or "", ensure_ascii=False)
    return (
        "<goal_round>\n"
        f"Objective: {objective}\n"
        f"Round: {round_number}/{state.max_goal_rounds}\n\n"
        "Continue working toward the objective in this same session. Treat the current "
        "workspace, tool results, and durable session state as authoritative; inspect "
        "them instead of assuming earlier narration is still current. Make concrete "
        "progress and verify the result. Before claiming completion, gather evidence "
        "that the whole objective is achieved, read the current goal, and mark it "
        "complete. If work remains, leave the goal active for the next round. Follow "
        "the configured goal-tool policy before reporting a blocker.\n\n"
        "NEVER redefine success as a smaller, easier, or already-completed subset.\n"
        "Budget exhaustion ≠ completion.\n"
        "</goal_round>"
    )


def _tool_digest(kind: str, payload: object) -> str:
    """工具事件的一次稳定摘要（键序固定 + UTF-8 + sha256）。"""
    serialized = json.dumps(
        [kind, payload], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _result_digest_payload(content: object) -> object:
    """``tool/result`` 的 digest 载荷：只留语义字段，丢掉**每次调用都变**的元数据。

    ``content`` 是整份 ``ToolResult.model_dump_json()``，而 ToolExecutor 每次尝试都会
    回填 ``attempt`` / ``max_attempts`` / ``duration_ms`` / ``total_duration_ms``——直接
    对整串做摘要，两轮"同样的调用、同样的结果"会因耗时不同而永远判为有进展，AC9 的
    无进展检测就此失效。只保留 ``ok`` / ``message`` / ``data``（结果语义）；非 JSON
    content（历史事件 / 其他生产者）退回原串，不猜。
    """
    if not isinstance(content, str):
        return content
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(parsed, dict):
        return content
    return {key: parsed.get(key) for key in ("ok", "message", "data")}


def goal_round_digests(events: list[SessionEvent]) -> list[str]:
    """按**goal 来源的 user/message** 切分轮次，返回每轮工具活动的 digest。

    digest 覆盖 ``tool/call``（工具名 + 参数）与 ``tool/result``（只取结果语义
    ``ok``/``message``/``data``，不取调用 id 与执行耗时——同形活动每轮的 id 与耗时都
    不同，带上就会把"原地打转"误判成有进展）：
    "调了同样的工具、拿到同样的结果"与"什么工具都没调"都能被识别。非 goal
    用户消息会关闭当前桶——人工介入那一轮的工具活动不算进 goal 轮次。
    """
    rounds: list[list[str]] = []
    active: list[str] | None = None
    for event in events:
        if event.type == USER_MESSAGE:
            if event.data.get("origin") == GOAL_ORIGIN:
                active = []
                rounds.append(active)
            else:
                active = None
            continue
        if active is None:
            continue
        if event.type == TOOL_CALL:
            active.append(_tool_digest("call", {
                "tool_name": event.data.get("tool_name"),
                "args": event.data.get("args"),
            }))
        elif event.type == TOOL_RESULT:
            active.append(_tool_digest(
                "result", {"content": _result_digest_payload(event.data.get("content"))},
            ))
    return [":".join(parts) for parts in rounds]


def goal_rounds_stalled(events: list[SessionEvent]) -> bool:
    """最近两轮 goal 轮次是否无进展（票面 AC9）。

    抄 omp ``goalContinuationActivity`` 的判据：**当前轮 digest 为空**，或与上一轮
    **相同** ⇒ 无进展。少于两轮不判——第一轮还没形成"两轮对比"。
    """
    digests = goal_round_digests(events)
    if len(digests) < 2:
        return False
    current, previous = digests[-1], digests[-2]
    return current == "" or current == previous


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


def _resolve_pause_reason(reason: object) -> tuple[dict | None, str | None]:
    """``pause_reason`` 可选；给了就必须是 ``{code, message}``（票面 AC9）。"""
    if reason is None:
        return None, None
    if not isinstance(reason, dict):
        return None, "pause_reason 必须是 {code, message}"
    code = reason.get("code")
    message = reason.get("message")
    if not isinstance(code, str) or not code or not isinstance(message, str) or not message.strip():
        return None, "pause_reason 需要非空 code 与非空 message"
    return {"code": code, "message": message.strip()}, None


def append_goal_change(
    session: Session,
    operation: str,
    *,
    objective: object = None,
    blocked_reason: object = None,
    pause_reason: object = None,
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
    - ``pause``：``active`` → ``paused``；可选 ``pause_reason``（系统判定用）。
    - ``resume``：``active|paused|blocked`` → ``active``（轮次未超限）。
    - ``complete``：``active`` → ``complete``（票面：只允许从 active）。
    - ``block``：``active`` → ``blocked``，必须带 reason。
    - ``clear``：写墓碑，保留历史（append-only，不物理删除）。
    - ``round_started``：``active`` 下轮次 +1（阶段 3 准入记账，REST 面不收）。

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

    # 阶段 3 准入记账：只有真正进入模型历史的那条 goal 轮次才走到这里
    # （调用点在 ``deliver_next_undelivered`` 拿到 run_id 之后）。它不是用户
    # 命令，因此不进 REST 的 Literal。
    if operation == "round_started":
        if not current.defined:
            return _failure(current, "conflict", "当前没有目标，无法开始轮次")
        if current.phase is not GoalPhase.ACTIVE:
            return _failure(
                current, "conflict",
                f"目标不是 active（{current.phase.value if current.phase else '?'}），"
                "不能开始轮次",
            )
        if current.rounds_started >= current.max_goal_rounds:
            return _failure(
                current, "conflict",
                f"轮次已用尽 {current.rounds_started}/{current.max_goal_rounds}",
            )
        round_number = current.rounds_started + 1
        session.append(
            GOAL_CHANGED,
            {
                "operation": "round_started",
                "goal_id": current.goal_id,
                "revision": current.revision + 1,
                "objective": current.objective,
                "phase": GoalPhase.ACTIVE.value,
                "max_goal_rounds": current.max_goal_rounds,
                "rounds_started": round_number,
                "round": round_number,
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
    resolved_pause_reason: dict | None = None
    if operation == "pause":
        resolved_pause_reason, error = _resolve_pause_reason(pause_reason)
        if error is not None:
            return _failure(current, "shape", error)

    if not current.defined:
        return _failure(current, "conflict", f"当前没有目标，无法 {operation}")

    phase = current.phase
    next_phase: GoalPhase
    next_objective = current.objective
    next_reason: dict | None = None
    next_pause_reason: dict | None = None
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
        elif phase is GoalPhase.PAUSED:
            next_pause_reason = current.pause_reason
    elif operation == "pause":
        if phase is not GoalPhase.ACTIVE:
            return _failure(
                current, "conflict",
                f"不能从 {phase.value if phase else '?'} 暂停；只有 active 可暂停",
            )
        next_phase = GoalPhase.PAUSED
        next_pause_reason = resolved_pause_reason
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
    if next_pause_reason is not None:
        data["pause_reason"] = next_pause_reason
    session.append(GOAL_CHANGED, data)
    return GoalOutcome(
        ok=True, reason=None, error_kind=None, state=effective_goal(session.events)
    )


__all__ = [
    "APPROVAL_REQUIRED_CODE",
    "DEFAULT_MAX_GOAL_ROUNDS",
    "GOAL_OBJECTIVE_MAX_LENGTH",
    "GOAL_ORIGIN",
    "NO_PROGRESS_CODE",
    "GoalOutcome",
    "GoalPhase",
    "GoalState",
    "append_goal_change",
    "effective_goal",
    "goal_round_digests",
    "goal_rounds_stalled",
    "render_goal_round_prompt",
]
