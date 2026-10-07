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
# 输入均为 casefold 后的串，无需 re.IGNORECASE（P4-3 顺手清理）。
_KEEP_INTENT = re.compile(r"保留|留住|留在|留着|留下|\bkeep\b|\bpreserve\b|\bretain\b")
# 紧邻分支专用（P4-2）：只收中文 keep 词。target 末字符是 word char 时，
# target 末字与 keep 首字同为 word char，\b 不成立，英文词在该位置恒不命中；
# 但 target 以非 word char 结尾（如标点「foo-keep」）时 target 侧 \b 成立，
# 旧代码（bc04b60b^，紧邻检查用含英文词的 _KEEP_INTENT）会命中并返回 True，
# 新代码不命中——该 edge case 下删英文词有行为差异（新代码不拦，under-block
# 方向），并非零变化（P4-1 审查实测：old=True / new=False）。
_KEEP_INTENT_ADJACENT = re.compile(r"保留|留住|留在|留着|留下")
# P4-1：「遗留/残留/停留/滞留/挽留/拘留/扣留」是含「留」的**非 keep 词**，
# 其后紧跟的「留在/留着/留下」等是词内子串误命中（如「遗留在备份里」），
# 前一字落入本集合即跳过该匹配。真 keep 词（「保留」等）不受影响。
_KEEP_FALSE_PRECEDERS = frozenset("遗残停滞挽拘扣")
# P3：keep 词后可跳过的有限助词（至多 2 个），再遇「的」即定语标志。
_KEEP_PARTICLES = frozenset("来在")
_KEEP_ATTRIBUTIVE_MARKER = "的"
_KEEP_PARTICLE_SKIP_LIMIT = 2


def _keep_attributive(folded: str, keep_end: int) -> bool:
    """P3：keep 匹配之后（允许跳过至多 2 个助词 来/在）紧跟「的」= 定语标志。

    keep 修饰其后的名词（「留着的东西」「留下来的内容」），不作用于 target，
    不构成管辖。裸后置（后随字符非「的」）仍构成管辖。
    """
    i = keep_end
    skipped = 0
    while i < len(folded) and folded[i] in _KEEP_PARTICLES and skipped < _KEEP_PARTICLE_SKIP_LIMIT:
        i += 1
        skipped += 1
    return folded[i:i + 1] == _KEEP_ATTRIBUTIVE_MARKER


def _keep_word_interior(folded: str, keep_start: int) -> bool:
    """P4-1：keep 匹配前一字构成非 keep 词（遗留/残留/停留等）→ 词内子串误命中。"""
    return keep_start > 0 and folded[keep_start - 1] in _KEEP_FALSE_PRECEDERS


def _governing_keep_end(folded_clause: str, before: str) -> int | None:
    """返回 before 片段内最后一个可能管辖 target 的 keep 意图结束下标。

    过滤掉两类不构成管辖的 keep 匹配：定语形态（P3，keep+助词+「的」修饰
    其后名词）与词内子串误命中（P4-1，「遗留」中的「留」）。
    """
    for m in reversed(list(_KEEP_INTENT.finditer(before))):
        if _keep_word_interior(folded_clause, m.start()):
            continue
        if _keep_attributive(folded_clause, m.end()):
            continue
        return m.end()
    return None


def _reverse_keep_intent(clause: str, target: str) -> bool:
    """#806：target 被「保留」意图管辖时返回 True（forget guard 必须拒绝）。

    取 target 在 folded clause 中的**最后一次出现**做判定（步进 1 收集全部
    出现位置，P4-2 最新表态优先）：末次出现之前片段内最后一个 keep 意图比
    最后一个 forget 意图更近（last_keep_end > last_forget_end）即拒绝——
    「忘记新邮箱旧档，保留新邮箱」末次出现被「保留」管辖必须拒绝；反过来
    「忘记A，保留B，忘记B」末次出现被「忘记」管辖 → 放行（用户最后明确
    说忘记 B，先前的保留不再压过最新表态）。全程在 casefold 后的串上操作，
    不用 folded 下标切原串（casefold 可能改变字符串长度）。
    取舍一：「留下」「留住」「留在」收进 keep 意图是 fail-closed——删除类 guard
    宁可误拦不可误删。
    取舍二：「不保留」「别保留」会先命中「保留」被当作 keep 意图，对 target 造成
    over-block（连想删的也拦下）；方向同样 fail-closed，与取舍一一致。
    取舍三（P3-1 后置管辖）：keep 意图**紧邻** target 之后（「新密码留着」，
    含把字「把新密码留着」——其 keep 动词同样紧邻 target，无需单独把字规则）
    视为 keep 管辖 → 拒绝。仅取紧邻形态：间隔一字符即不构成管辖（「新密码，
    留着」「新钥匙留着」「把旧的留着」——把字句里「留着」管辖「旧的」而非
    target，误拦即 over-block bug）。定语形态同不管辖（审查清零 P3）：keep
    之后（允许跳过至多 2 个助词 来/在）紧跟「的」是定语标志（「留着的东西/
    留下来的内容」——keep 修饰其后的名词，不作用于 target），不视为管辖，
    与「把旧的留着」同类 over-block 边界；裸后置（后随字符非「的」）仍构成
    管辖。前向分支（keep 在 target 之前）同样应用定语豁免（P3：
    「被留下来的旧档案」不拦）与词内子串防误命中（P4-1：「遗留在备份里的
    旧档案」——「遗留」中的「留」不构成 keep 意图）。
    取舍四：英文后置形态（"the new key stays/keep it"）不处理——英文 keep
    意图由前置最近意图规则覆盖（"forget the old key, keep the new key"）；
    紧邻分支因此只收中文 keep 词（target 末字为 word char 时 \b 锚定的英文词
    在该分支恒不命中；target 以非 word char 结尾时有差异，见
    _KEEP_INTENT_ADJACENT 注释）。并列/悬垂后置
    （「把新密码和旧密码都留着」）不构成紧邻 → 不拦，方向 under-block。
    紧邻判据在 over-block（误拦正常删除）与 under-block（漏拦
    并列形态）之间取窄，与「误拦也是 bug」的边界设计一致。
    """
    folded_clause = clause.casefold()
    needle = target.casefold()
    if not needle:
        return False
    occurrences = []
    start = 0
    while (idx := folded_clause.find(needle, start)) >= 0:
        occurrences.append(idx)
        start = idx + 1
    if not occurrences:
        return False
    idx = occurrences[-1]
    before = folded_clause[:idx]
    keep_end = _governing_keep_end(folded_clause, before)
    if keep_end is not None:
        forget_ends = [m.end() for m in _FORGET_INTENT.finditer(before)]
        if not forget_ends or keep_end > forget_ends[-1]:
            return True
    adjacent = _KEEP_INTENT_ADJACENT.match(folded_clause, idx + len(needle))
    return adjacent is not None and not _keep_attributive(folded_clause, adjacent.end())


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
        # #794：逗号不终止「记住/忘记」指令的子句（中文「记住X，Y」里逗号后才是实质内容）。
        # 跳过逗号边界，子句延续到下一个非逗号边界或句末；intent 之前的前缀判定不受影响。
        boundary = None
        for bmatch in _COMMAND_BOUNDARY.finditer(tail):
            if bmatch.group() in (",", "，"):
                continue
            boundary = bmatch
            break
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
    """Require the target to be inside the clause governed by the user's forget command.

    #806 审查披露（P4-3）：memory_id 先 strip 再匹配——tool 参数两侧空白视为
    调用噪声，`f("忘记旧密码", " 旧密码 ")` 因此由 False 变 True（更放行），
    属有意的语义变化。
    """
    clause, _, _ = _command_clause(user_text, _FORGET_INTENT)
    stripped = memory_id.strip()
    return bool(
        has_forget_intent(user_text)
        and stripped
        and stripped.casefold() in clause.casefold()
        and not _reverse_keep_intent(clause, stripped)
    )


def explicit_forget_query_matches(user_text: str, query: str) -> bool:
    clause, _, _ = _command_clause(user_text, _FORGET_INTENT)
    stripped = query.strip()
    return bool(
        has_forget_intent(user_text)
        and stripped
        and stripped.casefold() in clause.casefold()
        and not _reverse_keep_intent(clause, stripped)
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
