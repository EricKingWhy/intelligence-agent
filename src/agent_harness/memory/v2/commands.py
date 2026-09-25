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
    r"(?:"
    r"\b(?:(?:do|should|must|can|will)\s+not|don't|don’t|dont|shouldn't|shouldn’t|"
    r"shouldnt|mustn't|mustn’t|mustnt|can't|can’t|cannot|won't|won’t|wont|never)"
    r"\s+remember\b|"
    r"\bno\s+need\s+to\s+remember\b|\bnot\s+to\s+remember\b|"
    r"\bremember\s+not\s+to\b|"
    r"\b(?:don't|don’t|dont|do\s+not|shouldn't|shouldn’t|shouldnt|should\s+not)\s+"
    r"(?:want|need)\s+"
    r"(?:you\s+)?to\s+(?:remember|store|save|retain|record|keep|include|add|put|write)\b|"
    r"\b(?:don't|don’t|dont|do\s+not|shouldn't|shouldn’t|shouldnt|should\s+not)\s+"
    r"(?:want|need)\b[^.!?。！？;；\r\n]{0,80}\b"
    r"(?:store|storing|stored|save|saving|saved|retain|retaining|retained|record|recording|recorded|"
    r"keep|keeping|kept|include|including|included|add|adding|added|put|putting|write|writing|written)\b|"
    r"\b(?:don't|don’t|dont|do\s+not|shouldn't|shouldn’t|shouldnt|should\s+not)\s+"
    r"(?:want|need)\b[^.!?。！？;；\r\n]{0,80}\b(?:in|inside)\s+(?:the\s+)?memory\b|"
    r"不要记住|别记住|不需要记住|不要记|别记|"
    r"不想(?:让你)?(?:去)?(?:记住|记得|记)|不希望你(?:记住|记得|记)|"
    r"\b(?:(?:do|should|must|can|will)\s+not|don't|don’t|dont|shouldn't|shouldn’t|"
    r"shouldnt|mustn't|mustn’t|mustnt|can't|can’t|cannot|won't|won’t|wont|never)"
    r"\s+(?:store|save|retain|record|keep|include|add|put|write)\b|"
    r"\bno\s+need\s+to\s+(?:store|save|retain|record|keep|include|add|put|write)\b|"
    r"\bnot\s+(?:to\s+)?(?:store|save|retain|record|keep|include|add|put|write)\b|"
    r"\bavoid\s+(?:storing|saving|retaining|recording|keeping|including|adding|putting|writing)\b|"
    r"\b(?:exclude|omit)\b[^.!?。！？;；\r\n]{0,80}\b(?:from|in)\s+(?:the\s+)?memory\b|"
    r"\bleave\s+out\b[^.!?。！？;；\r\n]{0,80}\b(?:from|of)\s+(?:the\s+)?memory\b|"
    r"\bkeep\b[^.!?。！？;；\r\n]{0,80}\b(?:out|outside)\s+(?:of\s+)?(?:the\s+)?memory\b|"
    r"\b(?:(?:do|should|must|can|will)\s+not|don't|don’t|dont|shouldn't|shouldn’t|"
    r"shouldnt|mustn't|mustn’t|mustnt|can't|can’t|cannot|won't|won’t|wont|never)"
    r"\s+consent\s+to\b[^.!?。！？;；\r\n]{0,100}\b"
    r"(?:store|storing|stored|save|saving|saved|retain|retaining|retained|record|recording|"
    r"recorded|keep|keeping|kept|include|including|included|add|adding|added|put|putting|"
    r"write|writing|written|memory|storage)\b|"
    r"不要保存|别保存|不要存储|别存储|不要储存|别储存|不要记录|别记录|不许记录|"
    r"(?:不要|别|不许|避免)[^。！？!?;；\r\n]{0,40}(?:写入|加入|添加|放进|放入|存入|存进|记入).{0,12}(?:记忆|长期记忆)|"
    r"(?:不想|不希望(?:你)?|不需要(?:你)?|无需|不用)[^。！？!?;；\r\n]{0,40}"
    r"(?:写入|加入|添加|放进|放入|存入|存进|记入|保存|存储|储存|记录).{0,12}(?:记忆|长期记忆))",
    re.IGNORECASE,
)
_REMEMBER_RECALL_QUESTION = re.compile(
    r"\bremember\s+(?:if|whether)\b|"
    r"(?:记得|记住)[^。！？!?;；\r\n]*(?:是否|是不是|有没有|有无|有沒有|有無|吗|么|嗎|麼)",
    re.IGNORECASE,
)
_FORGET_INTENT = re.compile(
    r"(?:\bforget\b|\b(?:delete|remove)\b[^.!?。！？;；\r\n]*\bmemory\b|"
    r"忘掉|忘记|删除.*记忆|删掉.*记忆)", re.IGNORECASE,
)
_FORGET_NEGATION = re.compile(
    r"(?:\b(?:(?:do|should|must|can|will)\s+not|don't|don’t|dont|shouldn't|shouldn’t|"
    r"shouldnt|mustn't|mustn’t|mustnt|can't|can’t|cannot|won't|won’t|wont|never)"
    r"\s+(?:forget|delete|remove|erase)\b|"
    r"\bno\s+need\s+to\s+(?:forget|delete|remove|erase)\b|"
    r"\bnot\s+to\s+(?:forget|delete|remove|erase)\b|"
    r"\b(?:don't|don’t|dont|do\s+not|shouldn't|shouldn’t|shouldnt|should\s+not)\s+"
    r"(?:want|need)\s+(?:you\s+)?(?:to\s+)?(?:forget|delete|remove|erase)\b|"
    r"别忘记|不要忘记|别忘了|不要忘了|不要删除|别删除|不要删掉|别删掉|不要删|别删|"
    r"不要清除|别清除|不要抹掉|别抹掉|"
    r"不想(?:让你)?(?:去)?(?:忘记|删除|删掉|清除|抹掉)|"
    r"不希望你(?:忘记|删除|删掉|清除|抹掉))",
    re.IGNORECASE,
)
_COMMAND_BOUNDARY = re.compile(
    r"[,，.!?。！？;；\r\n]|\b(?:but|however|except|although|whereas)\b|(?:但是|不过|然而|但)",
    re.IGNORECASE,
)


def _command_clause(user_text: str, intent: re.Pattern[str]) -> str:
    allowed_prefixes = {
        "please", "kindly", "can you", "could you", "would you", "will you",
        "can you please", "could you please", "would you please",
        "i want you to", "i would like you to", "i'd like you to", "i’d like you to",
        "请", "请你", "麻烦", "麻烦你", "帮我", "请帮我", "请你帮我", "麻烦帮我",
        "麻烦你帮我", "我希望你", "我想让你", "我想请你",
    }
    for match in intent.finditer(user_text):
        prefix_start = 0
        for boundary in _COMMAND_BOUNDARY.finditer(user_text, 0, match.start()):
            prefix_start = boundary.end()
        prefix = user_text[prefix_start:match.start()].strip(" \t\r\n\"'“”‘’")
        if prefix and " ".join(prefix.casefold().split()) not in allowed_prefixes:
            continue
        tail = user_text[match.end():]
        boundary = _COMMAND_BOUNDARY.search(tail)
        return tail[:boundary.start()] if boundary is not None else tail
    return ""


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
    """Require the proposed fact to be in the clause governed by the user's command."""
    if (
        _REMEMBER_NEGATION.search(user_text)
        or _REMEMBER_RECALL_QUESTION.search(user_text)
        or not content.strip()
    ):
        return False
    clause = _command_clause(user_text, _REMEMBER_INTENT)
    proposed = content.strip().strip(" \t\r\n\"'“”‘’.,!?。！？;；")
    return bool(proposed and proposed.casefold() in clause.casefold())


def explicit_forget_matches(user_text: str, memory_id: str) -> bool:
    return bool(
        has_forget_intent(user_text)
        and memory_id.casefold() in _command_clause(user_text, _FORGET_INTENT).casefold()
    )


def explicit_forget_query_matches(user_text: str, query: str) -> bool:
    return bool(
        has_forget_intent(user_text)
        and query.strip()
        and query.strip().casefold() in _command_clause(user_text, _FORGET_INTENT).casefold()
    )


def has_forget_intent(user_text: str) -> bool:
    return bool(not _FORGET_NEGATION.search(user_text) and _command_clause(user_text, _FORGET_INTENT))


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
