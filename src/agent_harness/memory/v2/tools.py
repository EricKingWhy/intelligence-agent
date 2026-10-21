"""Explicit V2 remember/forget commands through the shared capability and ToolExecutor."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_harness.memory.audit import (
    ENTRY_TOOL,
    OUTCOME_ABSENT,
    OUTCOME_FORGOTTEN,
    record_forget,
    record_memory_change,
)
from agent_harness.memory.types import memory_session_var
from agent_harness.memory.v2.capability import (
    MarkdownBudgetExhausted,
    MemoryContentRejected,
)
from agent_harness.memory.v2.commands import (
    _REMEMBER_NEGATION,
    current_user_message,
    explicit_forget_matches,
    explicit_forget_query_matches,
    has_forget_intent,
    trusted_identity_for_session,
)
from agent_harness.memory.v2.formation import MemoryPayload
from agent_harness.memory.v2.memory_tool import _grounded_in_user_message
from agent_harness.memory.v2.policy import find_secret
from agent_harness.memory.v2.types import (
    EpisodicPayload,
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    MemoryTier,
    ProceduralPayload,
    SemanticCategory,
    SemanticPayload,
    SourceType,
    assert_kind_tier_payload_contract,
)
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode

logger = logging.getLogger(__name__)


#: 内容侧的"偏好/画像类"信号（#1353 §0 裁决 3 的 kind 推断表：target=user **或**内容为
#: 偏好/画像类）。ponytail: 词面启发式，只决定落 USER.md 还是 MEMORY.md；误判代价是
#: 记忆文件分拣不理想，不影响正确性。要更准就把判断交给模型显式传 target/kind。
_PROFILE_CONTENT = re.compile(
    r"\b(?:i|we)\s+(?:prefer|like|love|enjoy|always|usually|never|am|'m)\b|"
    r"\bmy\s+(?:name|role|job|title|timezone|preferred|favorite|favourite)\b|"
    r"(?:我|本人)(?:更?喜欢|偏好|习惯|叫|是|通常|一般)|我的(?:名字|姓名|职业|角色|时区|偏好)",
    re.IGNORECASE,
)


def _synthesize_payload(kind: MemoryKind, content: str, tier: MemoryTier) -> MemoryPayload:
    """payload 缺省时的确定性合成（#1440 F-05）：只由 content 派生，不猜额外语义。"""
    if kind is MemoryKind.EPISODIC:
        return EpisodicPayload(
            situation=content, action=content, outcome=content, lesson=content,
        )
    if kind is MemoryKind.PROCEDURAL:
        return ProceduralPayload(
            trigger=content, procedure=content, success_condition=content,
        )
    category = (
        SemanticCategory.PROFILE if tier is MemoryTier.PROFILE else SemanticCategory.PREFERENCE
    )
    return SemanticPayload(subject=content, fact=content, category=category)


class _RememberV2Args(BaseModel):
    """#1440 F-05 / #1353 §0 裁决 3：垫片不做逐字摘录校验，`kind`/`tier`/`payload` 可缺省。

    模型给不出结构化 payload 是常态（#1025 的双重编码即证据），所以 `payload` 缺省时
    由 `content` 确定性合成；`kind`/`tier` 按票面映射表推断，显式值永远优先。
    content 的硬闸只有 add 软校验（在本轮 user message 里找得到依据），不是逐字相等。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(min_length=1, max_length=500)
    #: `user` = 用户画像/偏好（USER.md）；`memory` = 助手笔记（MEMORY.md）。与 `memory` 工具同名同义。
    target: Literal["memory", "user"] = "memory"
    kind: MemoryKind | None = None
    tier: MemoryTier | None = None
    payload: MemoryPayload | None = None
    importance: float = Field(default=0.8, ge=0.0, le=1.0)
    strength: float = Field(default=0.8, ge=0.0, le=1.0)

    @field_validator("payload", mode="before")
    @classmethod
    def _coerce_stringified_payload(cls, value: Any) -> Any:
        """#1025（方案 A）：字段作者在契约里显式声明接受字符串形态。

        经 OpenAI 兼容网关的模型常把对象字段双重编码成 JSON 字符串；
        合法 JSON 字符串先 ``json.loads`` 再走正常判联合校验。解析失败
        原样返回 ⇒ 保留 Pydantic 原错误（不静默吞错、不猜测修复）；
        dict 入站零影响。``RecursionError`` 与 ``ValueError`` 一起捕获
        （executor.py 同款先例）：极端深嵌套不能冒出 ``model_validate``
        破坏「对外永不抛异常」不变量。只声明本字段——不碰其他字段。
        """
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (ValueError, RecursionError):
                return value
        return value

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value

    @model_validator(mode="before")
    @classmethod
    def _infer_kind_tier_payload(cls, data: Any) -> Any:
        """票面映射表（#1353 §0 裁决 3 / #1440 §1）：显式值永远优先。

        - 显式 `kind` → 以显式为准；否则取 payload 自带的 kind；再否则推断。
        - `tier` 缺省：semantic 且（target=user 或内容为偏好/画像类）→ profile，其余 collection。
        - `payload` 缺省 → `_synthesize_payload` 由 content 确定性派生。
        """
        if not isinstance(data, dict):
            return data
        raw_payload = data.get("payload")
        parsed_payload: Any = raw_payload
        if isinstance(raw_payload, str):
            try:
                parsed_payload = json.loads(raw_payload)
            except (ValueError, RecursionError):
                return data  # 非法字符串：原样留给字段级 coercion 报 Pydantic 原错误
        # 显式值永远优先（票面映射表）。`kind` 也可由 payload 自带的判别字段补上。
        explicit_kind = data.get("kind")
        kind: MemoryKind
        if explicit_kind is not None:
            kind = MemoryKind(explicit_kind)
        elif isinstance(parsed_payload, dict) and parsed_payload.get("kind"):
            kind = MemoryKind(parsed_payload["kind"])
        else:
            kind = MemoryKind.SEMANTIC
        content = data.get("content")
        target = data.get("target") or "memory"
        profile_signal = target == "user" or (
            isinstance(content, str) and bool(_PROFILE_CONTENT.search(content))
        )
        # 缺省 tier：只有 semantic 能进 profile（§4.2），其余一律 collection。
        tier = data.get("tier") or (
            MemoryTier.PROFILE.value
            if kind is MemoryKind.SEMANTIC and profile_signal
            else MemoryTier.COLLECTION.value
        )
        payload = parsed_payload
        if payload is None and isinstance(content, str) and content.strip():
            payload = _synthesize_payload(kind, content, MemoryTier(tier)).model_dump(mode="json")
        return {**data, "kind": kind.value, "tier": tier, "payload": payload}

    @model_validator(mode="after")
    def _kind_tier_payload_combination(self) -> _RememberV2Args:
        # scope/project 由 execute 期按身份决定，模型可控的组合错误只有这两条；
        # 在参数面提前拦截（权威规则见 types.assert_content_contract），报错经执行器
        # 带规则文本回模型自纠，而不是走进 execute 后被防泄漏分支吞成通用文案。
        assert self.kind is not None and self.tier is not None and self.payload is not None
        assert_kind_tier_payload_contract(kind=self.kind, tier=self.tier, payload=self.payload)
        return self


class RememberMemoryV2Tool(Tool):
    """Write one typed, explicit-command V2 record with user-event provenance."""

    def __init__(self, service: Any, sessions: Any, *, workspace_index: Any | None = None) -> None:
        self._service = service
        self._sessions = sessions
        self._workspace_index = workspace_index

    @property
    def name(self) -> str:
        return "remember_this"

    @property
    def description(self) -> str:
        return (
            "只在用户本轮明确要求记住（说了\"记住/记一下\"等）时，把该内容写入一条 V2 长期记忆。"
            "content 必填（≤500 字符）：写用户本轮消息里的原话或它的连续片段；"
            "允许把用户原话压缩成一条条目，但必须能在本轮用户消息里找到依据——"
            "凭空编造的内容会被软校验拒绝。"
            "target 可选：user（用户画像/偏好，写入 USER.md）| memory（助手笔记，写入 MEMORY.md，默认）。"
            "kind/tier/payload 均可省略，运行时按 target 与内容推断："
            "semantic（稳定事实/偏好）| episodic（情境/行动/结果/教训）| procedural（操作规则）；"
            "需要精确控制时再显式给出，payload 结构随 kind："
            "semantic={kind:\"semantic\",subject,fact,category}，"
            "episodic={kind:\"episodic\",situation,action,outcome,lesson}，"
            "procedural={kind:\"procedural\",trigger,procedure,success_condition}。"
            "例：用户说\"记住我喜欢喝美式\"→ content=\"我喜欢喝美式\"，target=\"user\"（或让运行时推断）；"
            "用户说\"记住我不吃香菜\"→ content=\"我不吃香菜\"（否定整句照写，不要只写\"不吃香菜\"）。"
            "密钥、令牌、密码和私钥永不保存；临时任务状态与本轮中间结果不该写入。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _RememberV2Args

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.WORKSPACE_WRITE

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=False,
            suggested_action="用 retrieve_memory 检查显式写入结果；不要在未确认用户指令时重试。",
        )

    async def execute(self, args: _RememberV2Args) -> ToolResult:
        session_id = memory_session_var.get() or ""
        assert args.kind is not None and args.tier is not None and args.payload is not None
        try:
            source = await current_user_message(self._sessions, session_id)
            source_text = str(source.data.get("content", "")) if source is not None else ""
            # 撤回同意/明确说"别记住"是**信任边界**，不是逐字摘录校验——F-05 推翻的是
            # 后者（`explicit_remember_matches` 的子句内逐字命中），前者保留。
            if source is not None and _REMEMBER_NEGATION.search(source_text):
                return ToolResult.failure(
                    message="用户本轮消息里有不要记住/撤回同意的表达；没有写入。",
                    error_code=ErrorCode.PERMISSION_DENIED,
                )
            # #1440 F-05 / #1353 §0 裁决 3：不再做逐字摘录校验，只做 add 软校验——
            # content 必须能在本 run 的 user message 里找到依据（规范化后子串命中）。
            if source is None or not _grounded_in_user_message(args.content, source_text):
                return ToolResult.failure(
                    message=(
                        "content 必须能在本轮用户消息里找到依据（软校验）；"
                        "请把 content 改成用户本轮消息里的原话或它的连续片段后重试；没有写入。"
                    ),
                    error_code=ErrorCode.INVALID_ARGUMENT,
                )
            if find_secret(source_text, args.content) is not None:
                return ToolResult.failure(
                    message="该内容匹配凭证或密钥规则，不能写入长期记忆。",
                    error_code=ErrorCode.PERMISSION_DENIED,
                )
            trusted = trusted_identity_for_session(session_id, self._workspace_index)
            event_text = source_text
            # §4.2：profile 档位只承载 user_global 的 semantic——用户画像不随项目走。
            profile = args.tier is MemoryTier.PROFILE
            scope = (
                MemoryScope.USER_GLOBAL
                if profile or trusted.project_id is None
                else MemoryScope.PROJECT
            )
            draft = MemoryDraftV2(
                kind=args.kind, tier=args.tier, scope=scope,
                project_id=trusted.project_id if scope is MemoryScope.PROJECT else None,
                content=args.content, payload=args.payload,
                importance=args.importance, strength=args.strength,
                source_type=SourceType.EXPLICIT_COMMAND, source_session_id=session_id,
                source_event_ids=[source.event_id],
                evidence=[EvidenceItem(
                    role="user", excerpt=args.content[:300],
                    hash=hashlib.sha256(event_text.encode("utf-8")).hexdigest(),
                )],
            )
            record = await self._service.create(draft, trusted)
            record_memory_change(
                entry_point=ENTRY_TOOL, action="explicit_remember",
                memory_ids=(record.id,), affected_count=1,
            )
        except MemoryContentRejected as error:
            # #1440 F-12：凭证/威胁拒绝的**真因**必须可见，不再伪装成"身份无权"。
            logger.info("V2 explicit memory write rejected as unsafe content")
            return ToolResult.failure(
                message=str(error), error_code=ErrorCode.PERMISSION_DENIED,
            )
        except MarkdownBudgetExhausted as error:
            # #1440 F-12：预算超限是**永久条件**（重试落在同一条上限上），且三段式
            # 说明（当前用量/上限/条目）是模型就地合并的唯一依据，必须原样透出。
            logger.info("V2 explicit memory write rejected by markdown budget")
            return ToolResult.failure(
                message=str(error), error_code=ErrorCode.BUDGET_EXHAUSTED,
            )
        except PermissionError:
            return ToolResult.failure(
                message="当前身份无权写入长期记忆。", error_code=ErrorCode.PERMISSION_DENIED,
            )
        except ValueError as error:
            logger.info("V2 explicit memory write rejected (%s)", type(error).__name__)
            return ToolResult.failure(
                message="记忆内容未通过 V2 校验，没有写入。",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        except Exception as error:  # noqa: BLE001 — do not log or return candidate content.
            logger.warning("V2 explicit memory write failed (%s)", type(error).__name__)
            return ToolResult.failure(
                message=f"V2 记忆写入失败（{type(error).__name__}），本条没有写入。",
                error_code=ErrorCode.TRANSIENT_ERROR,
            )
        return ToolResult.success(
            message="已按用户本轮的明确指令保存为 V2 记忆。",
            data={"memory_id": record.id, "kind": record.kind.value, "version": record.version},
        )


class _ForgetV2Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str | None = Field(default=None, min_length=1, max_length=128)
    query: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _one_selector(self) -> _ForgetV2Args:
        if (self.memory_id is None) == (self.query is None):
            raise ValueError("provide exactly one of memory_id or query")
        return self


class ForgetMemoryV2Tool(Tool):
    """Delete an unambiguous active match; ambiguous queries are read-only selections."""

    def __init__(self, service: Any, sessions: Any, *, workspace_index: Any | None = None) -> None:
        self._service = service
        self._sessions = sessions
        self._workspace_index = workspace_index

    @property
    def name(self) -> str:
        return "forget_memory"

    @property
    def description(self) -> str:
        return (
            "只在用户本轮明确要求遗忘（说了\"忘掉/删除某条记忆\"等）时，删除 V2 长期记忆。"
            "memory_id 与 query 必须二选一，不能同时给或都不给。"
            "query：按内容找唯一 active 记录，必须逐字摘自用户本轮消息原文（不得改写或总结），"
            "多个匹配时只返回候选供用户选择，不得自行挑选；"
            "memory_id：精确删除一条，通常来自 retrieve_memory 结果；"
            "选择已有候选时，用户必须在新消息中明确给出 memory_id。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _ForgetV2Args

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=False,
            suggested_action="用 retrieve_memory 核对删除状态；未知结果时不要盲目重复删除。",
        )

    async def execute(self, args: _ForgetV2Args) -> ToolResult:
        session_id = memory_session_var.get() or ""
        try:
            source = await current_user_message(self._sessions, session_id)
            source_text = str(source.data.get("content", "")) if source is not None else ""
            if source is None or not has_forget_intent(source_text):
                return ToolResult.failure(
                    message="未找到本轮用户明确的遗忘指令；没有删除。",
                    error_code=ErrorCode.PERMISSION_DENIED,
                )
            if args.query is not None and not explicit_forget_query_matches(source_text, args.query):
                return ToolResult.failure(
                    message="遗忘检索条件必须逐字摘自本轮用户消息原文；"
                            "请把 query 改成用户本轮消息里的原话后重试。没有删除。",
                    error_code=ErrorCode.PERMISSION_DENIED,
                )
            trusted = trusted_identity_for_session(session_id, self._workspace_index)
            if args.memory_id is not None:
                if not explicit_forget_matches(source_text, args.memory_id):
                    return ToolResult.failure(
                        message="用户必须在本轮消息中明确选择该 memory_id；没有删除。",
                        error_code=ErrorCode.PERMISSION_DENIED,
                    )
                receipt = await self._service.delete(args.memory_id, trusted)
                record_forget(
                    entry_point=ENTRY_TOOL, memory_id=args.memory_id,
                    outcome=OUTCOME_FORGOTTEN if receipt.deleted else OUTCOME_ABSENT,
                )
                return ToolResult.success(
                    message="已处理该 V2 记忆的删除请求。",
                    data={"memory_id": args.memory_id, "deleted": receipt.deleted},
                )

            matches = await self._service.list_records(
                trusted, query=args.query, status=MemoryStatus.ACTIVE, limit=3,
            )
            if not matches:
                return ToolResult.success(message="没有找到匹配的 active V2 记忆。", data={"matches": []})
            if len(matches) > 1:
                return ToolResult.success(
                    message="找到多条匹配记忆，未删除任何记录；请把用户选中的 memory_id 交给工具。",
                    data={"selection_required": True, "matches": [
                        {"memory_id": record.id, "content": record.content,
                         "kind": record.kind.value, "scope": record.scope.value}
                        for record in matches
                    ]},
                )
            record = matches[0]
            receipt = await self._service.delete(record.id, trusted)
            record_forget(
                entry_point=ENTRY_TOOL, memory_id=record.id,
                outcome=OUTCOME_FORGOTTEN if receipt.deleted else OUTCOME_ABSENT,
            )
        except KeyError:
            if args.memory_id is not None:
                record_forget(
                    entry_point=ENTRY_TOOL, memory_id=args.memory_id,
                    outcome=OUTCOME_ABSENT,
                )
            return ToolResult.success(message="该 V2 记忆不存在或当前身份不可见。", data={"deleted": False})
        except PermissionError:
            return ToolResult.failure(
                message="当前身份无权删除该 V2 记忆。", error_code=ErrorCode.PERMISSION_DENIED,
            )
        except ValueError as error:
            logger.info("V2 explicit memory delete rejected (%s)", type(error).__name__)
            return ToolResult.failure(
                message="遗忘请求未通过 V2 校验，删除状态未改变。",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        except Exception as error:  # noqa: BLE001 — content-free diagnostic only.
            logger.warning("V2 explicit memory delete failed (%s)", type(error).__name__)
            return ToolResult.failure(
                message=f"V2 记忆删除失败（{type(error).__name__}）；删除状态需要重新核对。",
                error_code=ErrorCode.TRANSIENT_ERROR,
            )
        return ToolResult.success(
            message="已删除唯一匹配的 V2 记忆。",
            data={"memory_id": record.id, "deleted": receipt.deleted},
        )
