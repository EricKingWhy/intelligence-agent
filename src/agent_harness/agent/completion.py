"""完成闸门的唯一 seam：Runtime Quiescence 聚合 + CompletionPolicy（`02 §5.4` / T8 #316）。

机制与取舍的完整叙述见 `docs/adr/0047-completion-quiescence-and-completion-policy.md`；
本文件只写代码自己看不出来的操作约束：

- `collect_quiescence_report` 是**纯函数**：只吃三份已落盘的输入（会话事件、账本行、
  最新模型决策是否请求工具），不碰存储、不认识 Runtime——六条谓词因此都能用"造事件 /
  造账本行"的确定性用例单独驱动（票面 Verification）。
- 谓词的作用域是**会话**而不是 run：上次执行留下的欠账同样挡住这次完成
  （ADR-0047 D1 的推导）。
- 非静止时 Runtime **不调用** policy（`02 §5.4` 的"调用它之前 MUST 先证明六条"）——
  "域策略绕过 quiescence"因此不是靠约定，而是调用图上不可能。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_harness.session.approval import unresolved_approval_ids
from agent_harness.session.derive import detect_dangling
from agent_harness.session.event import (
    AGENT_DELEGATION_FINISHED,
    AGENT_DELEGATION_STARTED,
)
from agent_harness.storage.operation import (
    Operation,
    OperationState,
    has_unproven_side_effect,
)

if TYPE_CHECKING:
    from agent_harness.session.event import SessionEvent

# ── 六条谓词的 blocker kind（`02 §5.4` 的编号顺序，值即报告里的稳定词）──
QUIESCENCE_DANGLING_TOOL = "dangling_tool"
QUIESCENCE_UNRESOLVED_APPROVAL = "unresolved_approval"
QUIESCENCE_ACTIVE_CHILD = "active_child"
QUIESCENCE_UNSETTLED_OPERATION = "unsettled_operation"
QUIESCENCE_PENDING_RECONCILE = "pending_reconcile"
QUIESCENCE_NEW_TOOL_CALLS = "new_tool_calls"

#: 诊断 / 结果里拒绝理由的前缀（稳定串：同输入同输出，客户端与用例都能断言）。
QUIESCENCE_BLOCKED_PREFIX = "quiescence_blocked"
POLICY_REJECTED_PREFIX = "completion_policy_rejected"

#: 拒绝来自哪一道闸门（诊断字段；两道闸门的处置相同、归因不同）。
BLOCK_SOURCE_QUIESCENCE = "quiescence"
BLOCK_SOURCE_POLICY = "policy"

#: 账本行"未定 reconcile 状态"的三档（谓词 4）。`NEED_RECONCILE` **不**在这里：
#: 它属于谓词 5（已进对账流程的欠账），两档的分界见 ADR-0047 D1。
_UNSETTLED_STATES = frozenset(
    {OperationState.PENDING, OperationState.RUNNING, OperationState.UNKNOWN}
)

#: 诊断里每类 blocker 最多列出多少个 id（kind 与总数照旧全给，只有 id 列表截断）。
_MAX_DIAGNOSTIC_REFS = 20


@dataclass(frozen=True)
class QuiescenceBlocker:
    """一条未静止事实：`kind` 是六条谓词之一，`refs` 是把它落到具体对象的 id。

    `refs` 只放标识（tool_call_id / approval_id / child_session_id），不放参数或
    任何自由文本——诊断面不得成为凭证或用户数据的泄漏通道（ADR-0047 D3）。
    """

    kind: str
    refs: tuple[str, ...] = ()

    def as_projection(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "count": len(self.refs),
            "refs": list(self.refs[:_MAX_DIAGNOSTIC_REFS]),
        }


@dataclass(frozen=True)
class QuiescenceReport:
    """六条谓词的一次聚合结果（完成闸门的输入快照）。"""

    blockers: tuple[QuiescenceBlocker, ...] = field(default_factory=tuple)

    @property
    def quiescent(self) -> bool:
        return not self.blockers

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(blocker.kind for blocker in self.blockers)

    def refusal_reason(self) -> str | None:
        """静止时 `None`；否则一条稳定理由（kind 排序后拼接，不含值）。"""
        if not self.blockers:
            return None
        return f"{QUIESCENCE_BLOCKED_PREFIX}:{','.join(sorted(set(self.kinds)))}"

    def as_projection(self) -> dict[str, Any]:
        """诊断投影：静止与否 + 逐条 blocker（`count` 是全量，`refs` 截断）。

        **不**含 `reason`：那是 `refusal_reason()` 的职责，两处都放会让"这次为什么被拒"
        在同一份诊断里出现两个可漂移的副本。键集恒定（静止时是空列表而不是缺键）。
        """
        return {
            "quiescent": self.quiescent,
            "blockers": [blocker.as_projection() for blocker in self.blockers],
        }


def _unfinished_child_sessions(events: Sequence[SessionEvent]) -> list[str]:
    """`agent/delegation-started` 里没有配对 `-finished` 的 `child_session_id`（谓词 3）。

    以 `child_session_id` 为配对键（`multiagent/tools.py` 的 pending_events 形状）：
    同一次执行里 started 与 finished 共享这个 id，缺失该键的历史数据跳过
    （腐烂数据只让这一项失去判据，不让聚合抛错——`storage.operation` 同一条纪律）。

    当前串联委派把两条事件一起落盘，所以本判据在生产链路上恒为空——它守望的是
    durable 事实的形状，边界与残余见 ADR-0047 §4 第 1 条。
    """
    started: dict[str, None] = {}
    finished: set[str] = set()
    for event in events:
        child_id = event.data.get("child_session_id")
        if not isinstance(child_id, str) or not child_id:
            continue
        if event.type == AGENT_DELEGATION_STARTED:
            started.setdefault(child_id, None)
        elif event.type == AGENT_DELEGATION_FINISHED:
            finished.add(child_id)
    return [child_id for child_id in started if child_id not in finished]


def collect_quiescence_report(
    *,
    events: Sequence[SessionEvent],
    operations: Sequence[Operation] = (),
    new_tool_calls: bool = False,
) -> QuiescenceReport:
    """按 `02 §5.4` 的编号顺序聚合六条谓词（顺序固定 ⇒ 报告可逐条断言）。

    三条输入的来源：`events` = 本会话已落盘的 SessionEvent；`operations` =
    Operation Ledger 的本会话行（**没有 Ledger 的部署传空序列**：那是"账本里没有
    欠账"的空真，不是豁免）；`new_tool_calls` = 最新被接纳的模型决策是否请求了工具
    （由调用方给，本函数看不到"最新"这件事）。
    """
    blockers: list[QuiescenceBlocker] = []

    dangling = detect_dangling(list(events))
    if dangling:
        blockers.append(QuiescenceBlocker(QUIESCENCE_DANGLING_TOOL, tuple(dangling)))

    unresolved_approvals = unresolved_approval_ids(list(events))
    if unresolved_approvals:
        blockers.append(
            QuiescenceBlocker(QUIESCENCE_UNRESOLVED_APPROVAL, tuple(unresolved_approvals))
        )

    active_children = _unfinished_child_sessions(events)
    if active_children:
        blockers.append(QuiescenceBlocker(QUIESCENCE_ACTIVE_CHILD, tuple(active_children)))

    unsettled = tuple(
        operation.tool_call_id
        for operation in operations
        if operation.state in _UNSETTLED_STATES
    )
    if unsettled:
        blockers.append(QuiescenceBlocker(QUIESCENCE_UNSETTLED_OPERATION, unsettled))

    waiting_reconcile = tuple(
        operation.tool_call_id
        for operation in operations
        if operation.state is OperationState.NEED_RECONCILE
        or has_unproven_side_effect(operation)
    )
    if waiting_reconcile:
        blockers.append(
            QuiescenceBlocker(QUIESCENCE_PENDING_RECONCILE, waiting_reconcile)
        )

    if new_tool_calls:
        blockers.append(QuiescenceBlocker(QUIESCENCE_NEW_TOOL_CALLS))

    return QuiescenceReport(blockers=tuple(blockers))


@dataclass(frozen=True)
class CompletionDecision:
    """策略结论。`accepted=False` 时 `reason` 应给出稳定理由（缺省由调用方兜底成类名）。"""

    accepted: bool
    reason: str | None = None


class CompletionPolicy(ABC):
    """完成证据的可插拔 seam（`02 §5.4`）——Core 只提供一个，域策略只**增加**证据。

    Runtime 只在 quiescence 通过后调用它（非静止时不调用），所以实现方拿到的一定是
    `report.quiescent is True`；策略仍应自己拒绝非静止报告（第三方可能直接调它，
    绕过 Runtime 就不该顺带绕过六条谓词）。
    """

    @abstractmethod
    async def decide(
        self, *, report: QuiescenceReport, final_text: str, run_id: str,
    ) -> CompletionDecision:
        """对"这次最终响应能不能收口这个 run"给出结论。"""


class DefaultCompletionPolicy(CompletionPolicy):
    """默认通用策略 = 静止后接受最终模型响应（`02 §5.4`）。

    不读 `final_text` 的内容：让最终响应"看起来不好"就把 run 判成不能完成，属于
    `02 §7` 禁止的那一类主观判据（fallback 的禁令同源）。空响应 / 内容审查那一族的
    判定在既有的其他臂里，这里不设第二个判据点。
    """

    async def decide(
        self, *, report: QuiescenceReport, final_text: str, run_id: str,
    ) -> CompletionDecision:
        if not report.quiescent:
            return CompletionDecision(
                accepted=False, reason=report.refusal_reason()
            )
        return CompletionDecision(accepted=True)


def policy_rejection_reason(
    policy: CompletionPolicy, decision: CompletionDecision,
) -> str:
    """策略拒绝时的稳定理由（策略没给理由时用策略类名兜底）。

    "拒绝了但没有理由"不该发生，但也不该让调用方拿到 `None` 或空串——那会让
    "为什么没完成"在诊断面失去可断言的形状（票面 AC：reports a stable reason）。
    """
    if decision.reason:
        return decision.reason
    return f"{POLICY_REJECTED_PREFIX}:{type(policy).__name__}"
