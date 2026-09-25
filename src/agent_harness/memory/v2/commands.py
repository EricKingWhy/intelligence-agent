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
    r"shouldnt|mustn't|mustn’t|mustnt|can't|can’t|cannot|won't|won’t|wont|"
    r"wouldn't|wouldn’t|wouldnt|couldn't|couldn’t|couldnt|never)"
    r"\s+(?:remember|memorize)\b|"
    r"\bno\s+need\s+to\s+(?:remember|memorize)\b|"
    r"\bnot\s+to\s+(?:remember|memorize)\b|"
    r"\bremember\s+not\s+to\b|"
    r"\b(?:don't|don’t|dont|do\s+not|shouldn't|shouldn’t|shouldnt|should\s+not)\s+"
    r"(?:want|need|wish)\s+"
    r"(?:you\s+)?to\s+(?:remember|memorize|store|save|retain|record|keep|include|add|put|write)\b|"
    r"\b(?:don't|don’t|dont|do\s+not|shouldn't|shouldn’t|shouldnt|should\s+not)\s+"
    r"(?:want|need|wish)\b[^.!?。！？;；\r\n]{0,80}\b"
    r"(?:remember|remembering|remembered|memorize|memorizing|memorized|store|storing|stored|save|saving|saved|retain|retaining|retained|record|recording|recorded|"
    r"keep|keeping|kept|include|including|included|add|adding|added|put|putting|write|writing|written)\b|"
    r"\b(?:(?:would|should|could|might)\s+)?prefer\b[^.!?。！？;；\r\n]{0,80}\bnot\s+"
    r"(?:to\s+)?[^.!?。！？;；\r\n]{0,40}\b(?:remember|remembering|remembered|memorize|memorized|store|stored|save|saved|retain|retained|"
    r"record|recorded|keep|kept|include|included|add|added|put|written)\b|"
    r"\b(?:would|(?:i|we|they|he|she|you)['’]d)\s+rather\b"
    r"[^.!?。！？;；\r\n]{0,80}\bnot\s+(?:to\s+)?"
    r"(?:remember|memorize|store|save|retain|record|keep|include|add|put|write)\b|"
    r"\b(?:don't|don’t|dont|do\s+not|shouldn't|shouldn’t|shouldnt|should\s+not)\s+"
    r"(?:want|need)\b[^.!?。！？;；\r\n]{0,80}\b(?:in|inside)\s+(?:the\s+)?"
    r"(?:long[- ]term\s+)?memory\b|"
    r"不要记住|别记住|不需要记住|不要记|别记|"
    r"不想(?:让你)?(?:去)?(?:记住|记得|记)|不希望(?:你|让你)?(?:记住|记得|记)|"
    r"不愿意(?:让你)?(?:去)?(?:记住|记得|记)|"
    r"\b(?:(?:do|should|must|can|will)\s+not|don't|don’t|dont|shouldn't|shouldn’t|"
    r"shouldnt|mustn't|mustn’t|mustnt|can't|can’t|cannot|won't|won’t|wont|"
    r"wouldn't|wouldn’t|wouldnt|couldn't|couldn’t|couldnt|never)"
    r"\s+(?:remember|memorize|store|save|retain|record|keep|include|add|put|write)\b|"
    r"\bno\s+need\s+to\s+(?:remember|memorize|store|save|retain|record|keep|include|add|put|write)\b|"
    r"\bnot\s+(?:to\s+)?(?:remember|memorize|store|save|retain|record|keep|include|add|put|write)\b|"
    r"\bavoid\s+(?:remembering|memorizing|storing|saving|retaining|recording|keeping|including|adding|putting|writing)\b|"
    r"\b(?:exclude|omit)\b[^.!?。！？;；\r\n]{0,80}\b(?:from|in)\s+(?:the\s+)?memory\b|"
    r"\bleave\s+out\b[^.!?。！？;；\r\n]{0,80}\b(?:from|of)\s+(?:the\s+)?memory\b|"
    r"\bkeep\b[^.!?。！？;；\r\n]{0,80}\b(?:out|outside)\s+(?:of\s+)?(?:the\s+)?memory\b|"
    r"\bopt(?:ing|ed)?\s+out\b|"
    r"\b(?:withdraw|withdraws|withdrew|withdrawn|revoke|revokes|revoked|rescind|rescinds|rescinded)\s+"
    r"(?:my\s+)?(?:consent|permission|authorization)\b|"
    r"\b(?:no|without)\s+(?:my\s+)?(?:consent|permission|authorization)\b|"
    r"\b(?:do\s+not|don't|don’t|dont|should\s+not|shouldn't|shouldn’t|shouldnt)\s+"
    r"(?:agree|authorize|authorise|permit|allow)\b"
    r"[^.!?。！？;；\r\n]{0,100}\b(?:remember|memorize|store|save|retain|record|keep|"
    r"include|add|put|write|memory|storage)\b|"
    r"\b(?:do\s+not|don't|don’t|dont|should\s+not|shouldn't|shouldn’t|shouldnt)\s+"
    r"agree\b[^.!?。！？;；\r\n]{0,40}\bto\s+(?:this|that)\b|"
    r"\b(?:do\s+not|don't|don’t|dont|should\s+not|shouldn't|shouldn’t|shouldnt)\s+"
    r"(?:authorize|authorise|permit|allow)\b[^.!?。！？;；\r\n]{0,40}\b(?:this|that)\b|"
    r"\b(?:(?:do|should|must|can|will)\s+not|don't|don’t|dont|shouldn't|shouldn’t|"
    r"shouldnt|mustn't|mustn’t|mustnt|can't|can’t|cannot|won't|won’t|wont|"
    r"wouldn't|wouldn’t|wouldnt|couldn't|couldn’t|couldnt|never)"
    r"\s+(?:give\s+)?(?:my\s+)?consent\b|"
    r"\brefuse(?:s|d)?\s+(?:to\s+)?(?:my\s+)?consent\b|"
    r"(?:我)?(?:不同意|不允许|不授权|不批准|拒绝同意|拒绝授权|撤回同意|撤回授权|没有同意|未同意|选择退出)"
    r"|"
    r"(?:我)?拒绝[^。！？!?;；\r\n]{0,20}(?:记忆|记住|保存|存储|记录)|"
    r"(?:我)?(?:退出记忆|退出长期记忆)"
    r"|(?:我)?(?:不同意|不允许|不授权|不批准|拒绝|撤回同意|撤回授权|没有同意|未同意|选择退出|退出)"
    r"[^。！？!?;；\r\n]{0,60}(?:写入|加入|添加|放进|放入|存入|存进|保存|存储|储存|记录|记住)"
    r".{0,12}(?:记忆|长期记忆)|"
    r"不要保存|别保存|不要存储|别存储|不要储存|别储存|不要记录|别记录|不许记录|"
    r"(?:不要|别|不许|避免)[^。！？!?;；\r\n]{0,40}(?:写入|加入|添加|放进|放入|存入|存进|记入).{0,12}(?:记忆|长期记忆)|"
    r"(?:不想|不希望(?:你)?|不愿意(?:你)?|不需要(?:你)?|无需|不用)[^。！？!?;；\r\n]{0,40}"
    r"(?:写入|加入|添加|放进|放入|存入|存进|记入|保存|存储|储存|记录).{0,12}(?:记忆|长期记忆))",
    re.IGNORECASE,
)
_REMEMBER_RECALL_QUESTION = re.compile(
    r"\b(?:if|whether)\b|"
    r"(?:是否|是不是|有没有|有无|有沒有|有無)",
    re.IGNORECASE,
)
_CHINESE_RECALL_SUFFIX = re.compile(
    r"(?:吗|么|嗎|麼)\s*$",
    re.IGNORECASE,
)
_CHINESE_LITERAL_GREETING = re.compile(r"(?:问候语|招呼语)[^。！？!?;；\r\n]{0,12}你好吗\s*$")
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


def _command_clause(user_text: str, intent: re.Pattern[str]) -> tuple[str, int, int]:
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
        clause = tail[:boundary.start()] if boundary is not None else tail
        return clause, match.start(), match.end() + len(clause)
    return "", -1, -1


def _recall_question_start(user_text: str, start: int, end: int, content: str) -> int | None:
    """Locate the latest remember command governing a recall question marker."""
    clause = user_text[start:end]
    command = _REMEMBER_INTENT.search(clause)
    command_end = command.end() if command is not None else 0
    suffix = _CHINESE_RECALL_SUFFIX.search(clause)
    if suffix is not None:
        if content and _CHINESE_LITERAL_GREETING.search(clause):
            return None
        preceding_intents = list(
            _REMEMBER_INTENT.finditer(clause, command_end, suffix.start())
        )
        if preceding_intents:
            return start + preceding_intents[-1].start()
        return start
    for marker in _REMEMBER_RECALL_QUESTION.finditer(clause):
        preceding_intents = list(
            _REMEMBER_INTENT.finditer(clause, command_end, marker.start())
        )
        if preceding_intents:
            return start + preceding_intents[-1].start()
        prefix = clause[command_end:marker.start()].strip(" \t\r\n,，:：")
        marker_text = marker.group().casefold()
        if not prefix or (
            marker_text not in {"if", "whether"}
            and prefix in {"我", "你", "他", "她", "它"}
        ):
            return start
    return None


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
    if not content.strip() or _REMEMBER_NEGATION.search(user_text):
        return False
    clause, command_start, clause_end = _command_clause(user_text, _REMEMBER_INTENT)
    proposed = content.strip().strip(" \t\r\n\"'“”‘’.,!?。！？;；")
    folded = proposed.casefold()
    if not folded or folded not in clause.casefold():
        return False
    question_start = _recall_question_start(user_text, command_start, clause_end, folded)
    return question_start is None or folded in user_text[command_start:question_start].casefold()


def explicit_forget_matches(user_text: str, memory_id: str) -> bool:
    clause, _, _ = _command_clause(user_text, _FORGET_INTENT)
    return bool(
        has_forget_intent(user_text)
        and memory_id.casefold() in clause.casefold()
    )


def explicit_forget_query_matches(user_text: str, query: str) -> bool:
    clause, _, _ = _command_clause(user_text, _FORGET_INTENT)
    return bool(
        has_forget_intent(user_text)
        and query.strip()
        and query.strip().casefold() in clause.casefold()
    )


def has_forget_intent(user_text: str) -> bool:
    clause, _, _ = _command_clause(user_text, _FORGET_INTENT)
    return bool(not _FORGET_NEGATION.search(user_text) and clause)


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
