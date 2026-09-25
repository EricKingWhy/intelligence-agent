"""Explicit V2 remember/forget commands through the shared capability and ToolExecutor."""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_harness.memory.audit import (
    ENTRY_TOOL,
    OUTCOME_ABSENT,
    OUTCOME_FORGOTTEN,
    record_forget,
    record_memory_change,
)
from agent_harness.memory.types import memory_session_var
from agent_harness.memory.v2.commands import (
    current_user_message,
    explicit_forget_matches,
    explicit_forget_query_matches,
    explicit_remember_matches,
    has_forget_intent,
    trusted_identity_for_session,
)
from agent_harness.memory.v2.formation import MemoryPayload
from agent_harness.memory.v2.policy import find_secret
from agent_harness.memory.v2.types import (
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    MemoryTier,
    SemanticPayload,
    SourceType,
)
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode

logger = logging.getLogger(__name__)
_NEGATED_CONTENT = re.compile(
    r"\b(?:not|never|no|without|cannot|can't|can’t|don't|don’t|doesn't|doesn’t|"
    r"didn't|didn’t|won't|won’t|wouldn't|wouldn’t|isn't|isn’t|aren't|aren’t|"
    r"wasn't|wasn’t|weren't|weren’t|haven't|haven’t|hasn't|hasn’t|hadn't|hadn’t|"
    r"shouldn't|shouldn’t|mustn't|mustn’t|deny|denies|denied|denying)\b|"
    r"\b(?:free\s+(?:of|from)|devoid\s+of|absence\s+of)\b|"
    r"\bnon[- ]?(?:smoker|smoking|drinker|drinking|vegetarian|driver|diabetic|diabetes|"
    r"allergic|pregnant|member|resident|citizen|employee|user|binary|verbal|compliant|"
    r"existent|functional)\b|"
    r"\b[a-z][a-z0-9]*-free\b|"
    r"不(?:是|会|能|可以?|想|喜欢|要|需要|曾|同意|允许|授权|应|该|必|存在|具备|符合|"
    r"支持|建议|推荐|属于|愿意|安全|确定|适合|知道|再|太|够|吃|喝|做|用)|"
    r"没(?:有|法|能|得|吃|喝|做|去|来|看|听|写|用|学|懂|找|在|到)|"
    r"没有|并非|並非|并不是|並不是|否认|否定|無法|无(?:法|需|权|效|意|关|所谓)|"
    r"无[\u4e00-\u9fff]{1,16}(?:史|病|症状|疾病|记录|迹象)|"
    r"非(?:糖尿病(?:患者)?|高血压(?:患者)?|心脏病(?:患者)?|癌症(?:患者)?|哮喘(?:患者)?|"
    r"吸烟者|烟民|饮酒者|素食者|孕妇|过敏(?:者|体质)?|会员|居民|员工|用户|患者)|"
    r"未(?:患有|诊断|確诊|确诊|检出|有|曾|能|完成|发现|同意|授权|报告|出现)|从未|不曾",
    re.IGNORECASE,
)


class _RememberV2Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(min_length=1, max_length=500)
    kind: MemoryKind
    tier: MemoryTier = MemoryTier.COLLECTION
    payload: MemoryPayload
    importance: float = Field(default=0.8, ge=0.0, le=1.0)
    strength: float = Field(default=0.8, ge=0.0, le=1.0)

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value

    @model_validator(mode="after")
    def _payload_text_is_covered_by_content(self) -> _RememberV2Args:
        normalized_content = " ".join(self.content.casefold().split()).strip(
            " \t\r\n\"'“”‘’.,!?。！？;；:："
        )
        for field, value in self.payload.model_dump(mode="json").items():
            if field in {"kind", "category"} or not isinstance(value, str):
                continue
            normalized_value = " ".join(value.casefold().split()).strip(
                " \t\r\n\"'“”‘’.,!?。！？;；:："
            )
            if not normalized_value or normalized_value not in normalized_content:
                raise ValueError(f"payload.{field} must be derived from content")
        if _NEGATED_CONTENT.search(self.content) and (
            not isinstance(self.payload, SemanticPayload)
            or normalized_content not in " ".join(self.payload.fact.casefold().split()).strip(
                " \t\r\n\"'“”‘’.,!?。！？;；:："
            )
        ):
            raise ValueError(
                "negated content must remain verbatim in a semantic payload fact"
            )
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
            "只在用户本轮明确要求记住且所给内容出现在该用户消息中时，写入一条有类型的 V2 长期记忆。"
            "同一消息中出现任何拒绝保存的表述时，本次写入一律拒绝。payload 文本必须来自 content；"
            "密钥、令牌、密码和私钥永不保存。"
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
        try:
            source = await current_user_message(self._sessions, session_id)
            source_text = str(source.data.get("content", "")) if source is not None else ""
            if source is None or not explicit_remember_matches(source_text, args.content):
                return ToolResult.failure(
                    message="未找到本轮用户对这条内容的明确记忆指令；没有写入。",
                    error_code=ErrorCode.PERMISSION_DENIED,
                )
            if find_secret(source_text, args.content) is not None:
                return ToolResult.failure(
                    message="该内容匹配凭证或密钥规则，不能写入长期记忆。",
                    error_code=ErrorCode.PERMISSION_DENIED,
                )
            trusted = trusted_identity_for_session(session_id, self._workspace_index)
            event_text = source_text
            scope = MemoryScope.PROJECT if trusted.project_id is not None else MemoryScope.USER_GLOBAL
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
            "删除 V2 长期记忆。先按 query 找唯一 active 记录；若有多个，只返回候选供用户选择，"
            "不得自行挑选。选择已有候选时，用户必须在新消息中明确给出 memory_id。"
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
                    message="遗忘检索条件必须来自本轮用户消息；没有删除。",
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
