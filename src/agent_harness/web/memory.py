"""用户侧记忆入口：V1 列表/遗忘与 V2 脱敏召回解释。

边界：

- V1 列表/遗忘入口不接受 namespace 参数；tenant/user 来自 `AuthSeamMiddleware`，归属校验
  仍由领域能力执行。V1 审计写结构化日志，不写 `SessionEvent`。
- V2 why-recalled 入口只从当前身份与 WorkspaceIndex 解析可信项目，再按每个 memory ID
  调用 SQLite 权威读取；响应不含正文或证据。

来源闸复用项目 API 的 `require_trusted_origin`（ADR-0025 D1）：本地信任模式下只接受本机
来源；配置了 `jwt_secret` 时由认证层接管。**刻意不复制一份**——安全规则有两份副本就是两个
漂移面。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_harness.capability.base import DegradeReason
from agent_harness.identity import get_identity_context
from agent_harness.memory.audit import (
    ENTRY_API,
    OUTCOME_ABSENT,
    OUTCOME_DENIED,
    OUTCOME_FORGOTTEN,
    record_forget,
    record_memory_change,
)
from agent_harness.memory.capability import MemoryCapability
from agent_harness.memory.errors import MemoryNotFound
from agent_harness.memory.types import MemoryEntry, MemoryScope, public_metadata
from agent_harness.memory.v2.capability import (
    InvalidMemoryPayload,
    MemoryIndexDeletePending,
    StaleMemoryVersion,
)
from agent_harness.memory.v2.recall import RANKING_VERSION, trusted_identity_for_session
from agent_harness.memory.v2.types import (
    MemoryKind as MemoryKindV2,
)
from agent_harness.memory.v2.types import (
    MemoryPayload,
    MemoryRecordV2,
)
from agent_harness.memory.v2.types import (
    MemoryScope as MemoryScopeV2,
)
from agent_harness.memory.v2.types import (
    MemoryStatus as MemoryStatusV2,
)
from agent_harness.session.errors import InvalidSessionId, SessionNotFound
from agent_harness.session.event import MEMORY_RECALLED
from agent_harness.web.domain_errors import http_error, memory_http_error
from agent_harness.web.projects import require_trusted_origin

if TYPE_CHECKING:
    from fastapi import FastAPI

#: 单页上限：记忆正文可能很长，列表必须有闸（客户端可传更小值）。
_MAX_LIMIT = 200
# Offset listing materializes all rows through the requested page before slicing;
# bound it to keep list requests finite and page_count within SQLite's integer range.
_MAX_OFFSET = 10_000

#: 装配期没能启用记忆时的**逐原因**说法（#225）。为什么要分开写：真机上
#: `CAPABILITIES` 里配着 memory、向量库连不上，装配期降级但路由层只能重复
#: "请在 CAPABILITIES 中配置 memory"——用户于是去改一个本来就配好的开关。
#: 前三码是**配置状态**（重试无用：`CAPABILITIES` 里少写/写错/缺前置项）；
#: `INIT_FAILED` 是**装配时出错**（改 CAPABILITIES 没用，要看日志、修它指向的东西）。
#: 机制与边界见 ADR-0010「补充（#225）」。
_DEGRADED_MESSAGE: dict[DegradeReason, str] = {
    DegradeReason.NOT_CONFIGURED: "memory capability 未启用：请在 CAPABILITIES 中配置 memory。",
    DegradeReason.DISABLED: (
        "memory capability 已在 CAPABILITIES 中登记但被禁用（enabled=false）："
        "请改为 enabled=true（或删掉这条登记）。"
    ),
    DegradeReason.MISSING_SETTINGS: (
        "memory capability 缺前置配置：向量检索（MILVUS_URI / MILVUS_TOKEN / "
        "MILVUS_COLLECTION）与嵌入模型（EMBEDDING_MODEL / EMBEDDING_BASE_URL / "
        "EMBEDDING_API_KEY）两组都齐才装配记忆。补齐后重启后端。"
    ),
    DegradeReason.INIT_FAILED: (
        "memory capability 初始化失败：CAPABILITIES 里已登记且启用，但装配时出错"
        "（记忆向量库不可达是常见原因）。这不是「没配置」——改 CAPABILITIES 没用；"
        "完整原因在后端日志的「capability 'memory' 初始化失败」那条里，排除后重启后端即可恢复。"
    ),
}


class MemorySummary(BaseModel):
    """Backward-compatible V1 fields plus additive, redacted V2 fields."""

    id: str
    content: str
    scope: str
    metadata: dict = Field(default_factory=dict)
    created_at: str
    root_id: str | None = None
    version: int | None = None
    kind: str | None = None
    tier: str | None = None
    status: str | None = None
    project_id: str | None = None
    source_type: str | None = None
    source_session_id: str | None = None
    source_event_ids: list[str] | None = None
    payload: dict | None = None
    evidence: list[dict[str, str]] | None = None


class MemoryDeleted(BaseModel):
    """Backward-compatible deletion receipt."""

    id: str
    deleted: bool


class MemoryEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=500)
    payload: MemoryPayload
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    strength: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class MemoryBulkDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: MemoryKindV2 | None = None
    confirmation: str

    @model_validator(mode="after")
    def _confirm(self) -> MemoryBulkDeleteRequest:
        if self.confirmation != "DELETE":
            raise ValueError('confirmation must be "DELETE"')
        return self


class MemorySettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extraction_enabled: bool | None = None
    recall_enabled: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> MemorySettingsPatch:
        if self.extraction_enabled is None and self.recall_enabled is None:
            raise ValueError("at least one setting must be provided")
        return self


class MemorySettingsResponse(BaseModel):
    extraction_enabled: bool
    recall_enabled: bool


class MemoryRecallExplanation(BaseModel):
    """Redacted explanation; intentionally has no record content or raw evidence."""

    memory_id: str
    kind: str
    scope: str
    source_type: str
    version: int
    source_session_id: str | None
    source_event_ids: list[str]
    ranking: dict[str, str | float | int]


class SessionMemoryRecall(BaseModel):
    run_id: str | None
    seq: int
    time: str
    memories: list[MemoryRecallExplanation]


def _summary(entry: MemoryEntry) -> MemorySummary:
    return MemorySummary(id=entry.id, content=entry.content, scope=entry.scope.value,
                         metadata=public_metadata(entry.metadata), created_at=entry.created_at)


def _v2_summary(record: MemoryRecordV2) -> MemorySummary:
    return MemorySummary(
        id=record.id, content=record.content, scope=record.scope.value,
        metadata={}, created_at=record.created_at, root_id=record.root_id,
        version=record.version, kind=record.kind.value, tier=record.tier.value,
        status=record.status.value, project_id=record.project_id,
        source_type=record.source_type.value, source_session_id=record.source_session_id,
        source_event_ids=list(record.source_event_ids), payload=record.payload.model_dump(),
        evidence=[{"role": item.role, "hash": item.hash} for item in record.evidence],
    )


def register_memory_routes(app: FastAPI) -> None:
    """把记忆路由挂到既有 app（`create_app` 里一行调用的接入面）。"""

    async def _capability() -> MemoryCapability:
        _, wiring = await app.state.agent.get_wiring()
        components = wiring.memory
        if components is None:
            # 能力缺席 → 503（比 404 诚实："能力没启用"不是"这个资源不存在"）。
            # 但"为什么缺席"必须分得开（#225）：`wiring.degradations` 里记着装配期
            # 的分类原因，**缺省 = 不在 CAPABILITIES 里**（见该字段的说明）。
            # `detail` 是 `{code, message}`：code 给机器（前端据它决定"未启用"还是
            # "故障 + 重试"），message 给人。判别走码，不走中文。
            recorded = wiring.degradations.get("memory")
            reason = DegradeReason.NOT_CONFIGURED if recorded is None else DegradeReason(recorded)
            raise HTTPException(
                status_code=503,
                detail={"code": reason.value, "message": _DEGRADED_MESSAGE[reason]},
            )
        return components.capability

    async def _v2_service(*, required: bool = True):
        _, wiring = await app.state.agent.get_wiring()
        service = wiring.memory_v2
        if service is None and required:
            raise HTTPException(
                status_code=503,
                detail={"code": "memory_v2_unavailable", "message": "V2 记忆服务尚未装配。"},
            )
        return service

    async def _trusted_v2(project_id: str | None = None):
        identity = get_identity_context()
        if "user" not in identity.scopes:
            raise HTTPException(status_code=403, detail="user memory scope is not authorized")
        if project_id is None:
            from agent_harness.memory.v2.types import TrustedMemoryIdentity

            return TrustedMemoryIdentity(identity.tenant_id, identity.user_id)
        state = app.state.agent
        await state.ensure_stores()
        workspace = state.workspace_index.get(project_id)
        if workspace is None:
            # The caller supplied only a selector; it becomes trusted project context only
            # after the server resolves it through the initialized project ledger.
            raise HTTPException(status_code=404, detail="project not found")
        from agent_harness.memory.v2.types import TrustedMemoryIdentity

        return TrustedMemoryIdentity(identity.tenant_id, identity.user_id, workspace.id)

    @app.get("/api/memories")
    async def list_memories(
        limit: int = Query(50, ge=1, le=_MAX_LIMIT, description="单页条数上限"),
        offset: int = Query(0, ge=0, le=_MAX_OFFSET, description="跳过的条数（按创建时间倒序）"),
        q: str | None = Query(None, max_length=500),
        kind: Annotated[MemoryKindV2 | None, Query(description="记忆类型筛选")] = None,
        status: Annotated[MemoryStatusV2 | None, Query(description="生命周期筛选")] = None,
        scope: Annotated[MemoryScopeV2 | None, Query(description="归属范围筛选")] = None,
        project_id: str | None = Query(None, min_length=1, max_length=256),
        _: None = Depends(require_trusted_origin),
    ) -> list[MemorySummary]:
        """列出 V1 与当前身份可见的 V2 记忆，并支持 V2 typed filters.

        Management reads use authoritative stores, never vector search. Existing unfiltered
        clients retain their array response and V1 summary fields; V2 fields are additive.
        """
        capability = await _capability()
        service = await _v2_service(required=False)
        v2_filter = any(value is not None for value in (kind, status, scope, project_id))
        if v2_filter and service is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "memory_v2_unavailable", "message": "V2 记忆筛选尚未装配。"},
            )
        trusted = await _trusted_v2(project_id) if service is not None else None
        try:
            page_count = limit + offset
            entries: list[MemoryEntry] = []
            if not v2_filter:
                if q:
                    folded = q.casefold()
                    # Search the authoritative list in pages until enough matches are
                    # available for this response. Filtering only the first page silently
                    # omitted older matches whenever recent records did not match `q`.
                    scan_offset = 0
                    chunk_size = _MAX_LIMIT
                    while len(entries) < page_count:
                        batch = await capability.list_entries(
                            MemoryScope.USER, chunk_size, scan_offset,
                        )
                        if not batch:
                            break
                        scan_offset += len(batch)
                        entries.extend(entry for entry in batch if (
                            folded in entry.content.casefold()
                            or folded in str(public_metadata(entry.metadata)).casefold()
                        ))
                        if len(batch) < chunk_size:
                            break
                else:
                    entries = await capability.list_entries(MemoryScope.USER, page_count, 0)
            v2_records = []
            if service is not None:
                v2_records = await service.list_records(
                    trusted, query=q, kind=kind, status=status, scope=scope,
                    project_id=project_id, limit=page_count, offset=0,
                )
        except PermissionError as error:
            # 认证通过但身份没有 "user" scope（`MemoryNamespace.of` 的授权校验）。不翻译就会
            # 以未登记领域异常的形状冒成 500——AC6 要的是明确状态码；同一身份的 DELETE 也是
            # 403，两个入口必须给同一个答案。
            raise memory_http_error(error) from error
        summaries = [_summary(entry) for entry in entries]
        summaries.extend(_v2_summary(record) for record in v2_records)
        summaries.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return summaries[offset:offset + limit]

    @app.delete("/api/memories/{memory_id}")
    async def forget_memory(
        memory_id: str,
        project_id: str | None = Query(None, min_length=1, max_length=256),
        _: None = Depends(require_trusted_origin),
    ) -> MemoryDeleted:
        """Delete V2 authoritatively, falling back to the compatible V1 endpoint."""
        service = await _v2_service(required=False)
        if service is not None:
            trusted = await _trusted_v2(project_id)
            try:
                receipt = await service.delete(memory_id, trusted)
            except KeyError:
                pass
            except PermissionError as error:
                raise memory_http_error(error) from error
            except MemoryIndexDeletePending as error:
                # SQLite deletion is committed before derived-index deletion. The durable
                # outbox remains for retry; report that pending state without leaking details.
                record_forget(
                    entry_point=ENTRY_API, memory_id=memory_id, outcome=OUTCOME_FORGOTTEN,
                )
                record_memory_change(
                    entry_point=ENTRY_API, action="delete_index_pending",
                    memory_ids=error.memory_ids, affected_count=error.affected_count,
                )
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "memory_index_delete_pending",
                        "message": "V2 记忆已从权威记录删除，派生索引删除待重试。",
                    },
                ) from None
            else:
                record_forget(
                    entry_point=ENTRY_API, memory_id=memory_id,
                    outcome=OUTCOME_FORGOTTEN if receipt.deleted else OUTCOME_ABSENT,
                )
                if receipt.deleted:
                    record_memory_change(
                        entry_point=ENTRY_API, action="delete",
                        memory_ids=[item.memory_id for item in receipt.memories],
                        affected_count=len(receipt.memories),
                    )
                return MemoryDeleted(id=memory_id, deleted=receipt.deleted)

        capability = await _capability()
        try:
            forgotten = await capability.forget(memory_id)
        except PermissionError as error:
            record_forget(entry_point=ENTRY_API, memory_id=memory_id, outcome=OUTCOME_DENIED)
            raise memory_http_error(error) from error

        if not forgotten:
            record_forget(entry_point=ENTRY_API, memory_id=memory_id, outcome=OUTCOME_ABSENT)
            error = MemoryNotFound(memory_id)
            raise memory_http_error(error) from error

        record_forget(entry_point=ENTRY_API, memory_id=memory_id, outcome=OUTCOME_FORGOTTEN)
        return MemoryDeleted(id=memory_id, deleted=True)

    @app.get("/api/memories/{memory_id}")
    async def get_memory(
        memory_id: str,
        project_id: str | None = Query(None, min_length=1, max_length=256),
        _: None = Depends(require_trusted_origin),
    ) -> MemorySummary:
        service = await _v2_service()
        trusted = await _trusted_v2(project_id)
        try:
            return _v2_summary(await service.read(memory_id, trusted))
        except KeyError as error:
            raise HTTPException(status_code=404, detail="memory not found") from error

    @app.get("/api/memories/{memory_id}/versions")
    async def get_memory_versions(
        memory_id: str,
        project_id: str | None = Query(None, min_length=1, max_length=256),
        _: None = Depends(require_trusted_origin),
    ) -> list[MemorySummary]:
        service = await _v2_service()
        trusted = await _trusted_v2(project_id)
        try:
            record = await service.read(memory_id, trusted)
            versions = await service.versions(record.root_id, trusted)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="memory not found") from error
        return [_v2_summary(item) for item in versions]

    @app.patch("/api/memories/{memory_id}")
    async def edit_memory(
        memory_id: str,
        request: MemoryEditRequest,
        project_id: str | None = Query(None, min_length=1, max_length=256),
        _: None = Depends(require_trusted_origin),
    ) -> MemorySummary:
        service = await _v2_service()
        trusted = await _trusted_v2(project_id)
        try:
            record = await service.edit(
                memory_id, trusted, expected_version=request.expected_version,
                content=request.content, payload=request.payload,
                importance=request.importance, strength=request.strength,
            )
        except StaleMemoryVersion as error:
            raise HTTPException(status_code=409, detail="memory version is stale") from error
        except InvalidMemoryPayload as error:
            raise HTTPException(
                status_code=422, detail="payload kind must match the memory kind",
            ) from error
        except KeyError as error:
            raise HTTPException(status_code=404, detail="memory not found") from error
        except PermissionError as error:
            raise memory_http_error(error) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail="memory is not editable in its current state") from error
        record_memory_change(
            entry_point=ENTRY_API, action="edit", memory_ids=(record.id,), affected_count=1,
        )
        return _v2_summary(record)

    @app.post("/api/memories/bulk-delete")
    async def bulk_delete_memories(
        request: MemoryBulkDeleteRequest,
        project_id: str | None = Query(None, min_length=1, max_length=256),
        _: None = Depends(require_trusted_origin),
    ) -> dict[str, int]:
        service = await _v2_service()
        trusted = await _trusted_v2(project_id)
        try:
            receipts = await service.bulk_delete(trusted, kind=request.kind)
        except PermissionError as error:
            raise memory_http_error(error) from error
        except MemoryIndexDeletePending as error:
            record_memory_change(
                entry_point=ENTRY_API, action="bulk_delete_index_pending",
                memory_ids=error.memory_ids, affected_count=error.affected_count,
            )
            raise HTTPException(
                status_code=503,
                detail={"code": "memory_index_delete_pending", "message": "V2 删除已提交，派生索引仍待重试。"},
            ) from None
        deleted_receipts = [receipt for receipt in receipts if receipt.deleted]
        memory_ids = [
            memory.memory_id for receipt in deleted_receipts for memory in receipt.memories
        ]
        if deleted_receipts:
            record_memory_change(
                entry_point=ENTRY_API, action="bulk_delete", memory_ids=memory_ids,
                affected_count=len(deleted_receipts),
            )
        return {
            "affected_count": len(deleted_receipts),
            "deleted_version_count": len(memory_ids),
        }

    @app.get("/api/memory-settings")
    async def get_memory_settings(
        _: None = Depends(require_trusted_origin),
    ) -> MemorySettingsResponse:
        service = await _v2_service()
        trusted = await _trusted_v2()
        settings = await service.get_settings(trusted)
        return MemorySettingsResponse(
            extraction_enabled=settings.extraction_enabled,
            recall_enabled=settings.recall_enabled,
        )

    @app.patch("/api/memory-settings")
    async def patch_memory_settings(
        request: MemorySettingsPatch,
        _: None = Depends(require_trusted_origin),
    ) -> MemorySettingsResponse:
        service = await _v2_service()
        trusted = await _trusted_v2()
        settings = await service.update_settings(
            trusted, extraction_enabled=request.extraction_enabled,
            recall_enabled=request.recall_enabled,
        )
        record_memory_change(
            entry_point=ENTRY_API, action="settings", affected_count=1,
        )
        return MemorySettingsResponse(
            extraction_enabled=settings.extraction_enabled,
            recall_enabled=settings.recall_enabled,
        )

    @app.get("/api/sessions/{session_id}/memory-recalls")
    async def get_session_memory_recalls(
        session_id: str, _: None = Depends(require_trusted_origin),
    ) -> list[SessionMemoryRecall]:
        """Return authorized, redacted explanations for automatic recalls in one session."""
        state = app.state.agent
        await state.ensure_stores()
        from agent_harness.web.app import session_service

        try:
            events = await session_service(state).get_events(session_id)
        except (InvalidSessionId, SessionNotFound) as error:
            raise http_error(error) from error
        service = await _v2_service()
        try:
            trusted = trusted_identity_for_session(session_id, state.workspace_index)
        except PermissionError as error:
            raise memory_http_error(error) from error

        ranking_fields = {
            "ranking_version", "dense", "keyword", "importance", "strength",
            "source_authority", "kind_weight", "scope_weight", "age_days", "decay", "score",
        }
        results: list[SessionMemoryRecall] = []
        for event in events:
            if event.type != MEMORY_RECALLED:
                continue
            explanations: list[MemoryRecallExplanation] = []
            raw_memories = event.data.get("memories", [])
            if not isinstance(raw_memories, list):
                continue
            for raw in raw_memories:
                if not isinstance(raw, dict) or not isinstance(raw.get("memory_id"), str):
                    continue
                try:
                    record = await service.read(raw["memory_id"], trusted)
                except KeyError:
                    # Do not reveal whether an event named a record owned by another identity.
                    continue
                version = raw.get("version")
                if type(version) is not int or record.version != version:
                    continue
                raw_ranking = raw.get("ranking")
                if not isinstance(raw_ranking, dict):
                    raw_ranking = {}
                ranking = {
                    key: value for key, value in raw_ranking.items()
                    if key in ranking_fields and type(value) in (str, int, float)
                    and (not isinstance(value, float) or math.isfinite(value))
                    and (key != "ranking_version" or value == RANKING_VERSION)
                }
                explanations.append(MemoryRecallExplanation(
                    memory_id=record.id,
                    kind=record.kind.value,
                    scope=record.scope.value,
                    source_type=record.source_type.value,
                    version=record.version,
                    source_session_id=record.source_session_id,
                    source_event_ids=list(record.source_event_ids),
                    ranking=ranking,
                ))
            if explanations:
                results.append(SessionMemoryRecall(
                    run_id=event.run_id, seq=event.seq, time=event.time,
                    memories=explanations,
                ))
        return results
