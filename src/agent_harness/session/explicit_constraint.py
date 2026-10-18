"""用户显式「作为可复用规则」手势的确定性登记（#1323）。

## 为什么存在

`remember_as_procedural_rule` 在 #1191 里只是**准入信号**：Memory V2 用它回答
「模型提出的这条规则允许入库吗」（`memory/v2/policy.py` 的 `explicit_rule_sources`），
真正的会话级写入仍靠模型自发调用 `register_constraint`。用户勾选复选框是一个
确定性手势，把它交给模型「要不要调工具」的判断 ⇒ 同一操作在旧会话上时成时不成
（#1323）。本模块把该手势落成**消息事件落盘后立刻执行**的机械写入。

## 判据与边界

- 与 project/global 的机械写入同一条纪律（`session/service.py` 方法尾部：所有写成功
  之后才登记）；本模块只被 user/message 落盘之后的运行时代码调用。
- 与 `register_constraint` 工具同三项判据：原文来源（`build_protected_fact_data`）、
  同值去重、保护事实预算（`preview_constraint_registration`）。工具路径与机械路径
  因此不会各说各话。
- 不改 #1191 Fork 1 的裁定「会话级不发 `protected_facts` 注解」：这里直接写会话事件流
  的 `task/protected_fact`，与工具路径同一个 durable 落点。
- 不经过模型：调用点固定在 `AgentRuntime.run_stream` 的 user/message 落盘之后。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from agent_harness.context.builder import preview_constraint_registration
from agent_harness.session.derive import build_protected_fact_data
from agent_harness.session.scoped_constraints import ConstraintScope

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查，避免与 session 包互相导入
    from agent_harness.session.session import Session
    from agent_harness.session.event import SessionEvent

logger = logging.getLogger(__name__)

_NON_SESSION_SCOPES = frozenset(
    {ConstraintScope.PROJECT.value, ConstraintScope.GLOBAL.value}
)


def is_session_explicit_rule(metadata: dict[str, Any]) -> bool:
    """消息元数据是否代表「用户显式要求把这条消息登记为**会话级**规则」。

    非会话级（project/global）不在这里登记：那条路径由服务层在写成功后走
    `ScopedConstraintStore.register`。前端对三档都会带
    `remember_as_procedural_rule`（`web/src/App.tsx`），故必须用注解的 scope
    排除非会话档，否则一次勾选会在 SESSION 组多出一条不该有的记录。
    """
    if metadata.get("remember_as_procedural_rule") is not True:
        return False
    annotations = metadata.get("protected_facts")
    if not isinstance(annotations, list):
        return True
    return not any(
        isinstance(annotation, dict) and annotation.get("scope") in _NON_SESSION_SCOPES
        for annotation in annotations
    )


def register_explicit_constraint(
    session: Session,
    *,
    content: str,
    source_event_id: str,
    token_budget: int | None,
) -> SessionEvent | None:
    """把用户勾选的这条消息逐字登记为会话级约束；返回新事件，未登记返回 None。

    失败方向是「不登记 + 日志」：与 Memory 旁路同一条故障边界——登记失败不得把
    已落盘的用户消息和整个 run 拖垮。正常路径（原文来源有效、未重复、预算内）恒登记。
    """
    try:
        data = build_protected_fact_data(
            session.events,
            session_id=session.session_id,
            fact_type="constraint",
            value=content,
            source_event_id=source_event_id,
        )
    except (TypeError, ValueError):
        logger.warning(
            "显式规则手势未登记：user/message %s 未通过 protected-fact 校验",
            source_event_id,
            exc_info=True,
        )
        return None

    preview = preview_constraint_registration(
        session.events, session_id=session.session_id, fact_data=data,
    )
    if preview.duplicate is not None:
        return None
    if token_budget is not None and preview.estimated_tokens_after > token_budget:
        logger.warning(
            "显式规则手势未登记：新增后保护事实预算将超限（session=%s）",
            session.session_id,
        )
        return None

    try:
        return session.register_protected_fact(
            fact_type="constraint",
            value=content,
            source_event_id=source_event_id,
        )
    except ValueError:
        # 校验在 build 之后、落盘之前仍可能因并发事件改变来源语义而失败；机械路径
        # 不把一条旁路登记的错误升级成整轮 run 的失败。
        logger.warning(
            "显式规则手势未登记：来源在写前失效（source=%s）",
            source_event_id,
            exc_info=True,
        )
        return None
