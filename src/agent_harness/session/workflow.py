"""会话级工作流档投影（#526 B1）。

独立于 :mod:`agent_harness.session.approval` 的权限档：工作流档描述**怎么干活**
（是否只读探索、不改动），不是 PermissionPolicy 的档位。

- ``effective_workflow_mode`` —— 倒序扫 ``workflow/mode-changed``，最后一次胜；
  坏值 warning 后继续往下找，无事件回落 NORMAL。
- ``append_workflow_mode_change`` —— ``WORKFLOW_MODE_CHANGED`` 的唯一写入口。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING

from agent_harness.session.event import WORKFLOW_MODE_CHANGED, SessionEvent

if TYPE_CHECKING:
    from agent_harness.session.session import Session

logger = logging.getLogger("agent_harness.session.workflow")


class WorkflowMode(str, Enum):
    """会话级工作流档（不是 PermissionPolicy 档位）。

    对标 Claude Code 的 plan mode（Read-only, no modifications）与 ZCode 的
    Plan mode：PLAN 表示当前会话处于只读探索档，NORMAL 为默认。
    """

    NORMAL = "normal"
    PLAN = "plan"


def effective_workflow_mode(events: list[SessionEvent]) -> WorkflowMode:
    """派生会话**当下生效**的工作流档。

    投影范式与 :func:`effective_permission_mode` 一致：倒序扫、last-wins。
    命中一条 ``workflow/mode-changed`` 取 ``data["mode"]``——``"plan"`` → PLAN、
    ``"normal"`` → NORMAL；**坏值记 warning 并继续往下找**（不猜更严/更松的档）。
    无 ``workflow/mode-changed`` 时回落默认 NORMAL。
    """
    for event in reversed(events):
        if event.type != WORKFLOW_MODE_CHANGED:
            continue
        raw = event.data.get("mode")
        if raw == WorkflowMode.PLAN.value:
            return WorkflowMode.PLAN
        if raw == WorkflowMode.NORMAL.value:
            return WorkflowMode.NORMAL
        logger.warning(
            "workflow/mode-changed 的 mode=%r 不是合法工作流档，跳过继续查找", raw
        )
    return WorkflowMode.NORMAL


def append_workflow_mode_change(session: Session, mode: WorkflowMode) -> WorkflowMode:
    """追加 ``workflow/mode-changed``——该事件的**唯一**写入口（#526 B1）。

    Runtime 只读门禁的 durable 真相：以 session 事件为准，不写内存影子状态。
    走 ``Session.append``（append-only，不产生 resume 副作用）。
    """
    session.append(WORKFLOW_MODE_CHANGED, {"mode": mode.value})
    return mode


__all__ = ["WorkflowMode", "append_workflow_mode_change", "effective_workflow_mode"]
