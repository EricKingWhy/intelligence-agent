"""Runtime checks for explicit V2 memory commands.

Consent and source provenance come from the current durable user/message event, never
from model-generated tool arguments alone.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from agent_harness.identity import get_identity_context
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.session import (
    RUN_STARTED,
    USER_MESSAGE,
    SessionEvent,
    run_context_var,
)

_REMEMBER_INTENT = re.compile(
    r"(?:\bremember\b|\bmemorize\b|\bkeep\s+in\s+mind\b|\bdon['’]?t\s+forget\b|"
    r"记住|记一下|帮我记|请记|记得)", re.IGNORECASE,
)
_REMEMBER_NEGATION = re.compile(
    r"(?:\b(?:do\s+not|don't|dont|never)\s+remember\b|"
    r"\bno\s+need\s+to\s+remember\b|不要记住|别记住|不需要记住)",
    re.IGNORECASE,
)
_FORGET_INTENT = re.compile(
    r"(?:\bforget\b|\bdelete\b.*\bmemory\b|\bremove\b.*\bmemory\b|"
    r"忘掉|忘记|删除.*记忆|删掉.*记忆)", re.IGNORECASE,
)
_FORGET_NEGATION = re.compile(
    r"(?:\b(?:do\s+not|don't|dont|never)\s+forget\b|"
    r"别忘记|不要忘记|别忘了|不要忘了)",
    re.IGNORECASE,
)


async def current_user_message(sessions: Any, session_id: str) -> SessionEvent | None:
    """Return the latest user message before the current run's start event."""
    if not session_id:
        return None
    run_id = run_context_var.get()
    events = await asyncio.to_thread(sessions.read_events, session_id)
    run_start = next((
        event.seq for event in reversed(events)
        if event.type == RUN_STARTED and (run_id is None or event.run_id == run_id)
    ), None)
    candidates = [
        event for event in events
        if event.type == USER_MESSAGE and (run_start is None or event.seq < run_start)
    ]
    return candidates[-1] if candidates else None


def explicit_remember_matches(user_text: str, content: str) -> bool:
    """Require a user-authored remember command that contains the exact proposed fact."""
    return bool(
        not _REMEMBER_NEGATION.search(user_text)
        and _REMEMBER_INTENT.search(user_text)
        and content.strip()
        and content.strip().casefold() in user_text.casefold()
    )


def explicit_forget_matches(user_text: str, memory_id: str) -> bool:
    return bool(has_forget_intent(user_text) and memory_id.casefold() in user_text.casefold())


def explicit_forget_query_matches(user_text: str, query: str) -> bool:
    return bool(
        has_forget_intent(user_text)
        and query.strip()
        and query.strip().casefold() in user_text.casefold()
    )


def has_forget_intent(user_text: str) -> bool:
    return bool(not _FORGET_NEGATION.search(user_text) and _FORGET_INTENT.search(user_text))


def trusted_identity_for_session(
    session_id: str, workspace_index: Any | None = None,
) -> TrustedMemoryIdentity:
    """Resolve owner from authenticated context and project from the session ledger."""
    identity = get_identity_context()
    if "user" not in identity.scopes:
        raise PermissionError("user memory scope is not authorized")
    project_id = None
    if workspace_index is not None and session_id:
        workspace = workspace_index.workspace_of_session(session_id)
        project_id = workspace.id if workspace is not None else None
    return TrustedMemoryIdentity(identity.tenant_id, identity.user_id, project_id)
