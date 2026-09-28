"""MEM-V2-1（#297）类型层契约测试：typed envelope + 判别 payload + fail-closed 校验。

契约来源：`docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md` §4.1/§4.2/§4.3/§6.1，
Issue #297 的 R1（拒绝面）、R2（per-kind payload）、R5（scope 与身份）、R8（用户权威）。

每个用例成对：正控（合法记录通过）+ 反控（非法记录被拒）。这样变异检验才能区分
"校验真的在工作"和"校验被删掉后测试照样绿"——只有反控没有正控会让一个"全部拒绝"
的实现看起来全绿。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_harness.memory.v2 import (
    EpisodicPayload,
    EvidenceItem,
    MemoryKind,
    MemoryRecordV2,
    MemoryScope,
    MemoryStatus,
    MemoryTier,
    ProceduralPayload,
    SemanticCategory,
    SemanticPayload,
    SourceType,
    TrustedMemoryIdentity,
    UntrustedIdentityError,
    assert_trusted_identity,
)

NOW = "2026-09-24T00:00:00+00:00"


def semantic_payload() -> SemanticPayload:
    return SemanticPayload(
        subject="回答风格", fact="用户偏好简洁直接的回答", category=SemanticCategory.PREFERENCE,
    )


def episodic_payload() -> EpisodicPayload:
    return EpisodicPayload(
        situation="CI 在 Windows 上挂起", action="查看 worker 退出日志", outcome="定位到端口占用",
        lesson="先确认端口再重跑",
    )


def procedural_payload() -> ProceduralPayload:
    return ProceduralPayload(
        trigger="需要重跑前端单测", procedure="先清 test-results 再跑 vitest", success_condition="全绿且无残留",
    )


def record(**overrides) -> MemoryRecordV2:
    """一条合法的 user_global semantic 记录；用例只覆盖自己关心的字段。"""
    values: dict = {
        "id": "mem-1",
        "root_id": "root-1",
        "version": 1,
        "kind": MemoryKind.SEMANTIC,
        "tier": MemoryTier.COLLECTION,
        "scope": MemoryScope.USER_GLOBAL,
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "content": "用户偏好简洁直接的回答",
        "payload": semantic_payload(),
        "importance": 0.8,
        "strength": 0.9,
        "source_type": SourceType.AUTOMATIC,
        "source_session_id": "session-1",
        "source_event_ids": ["event-1"],
        "evidence": [EvidenceItem(role="user", excerpt="请简洁点", hash="hash-1")],
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return MemoryRecordV2(**values)


# --------------------------------------------------------------------------------------
# 正控：三种 kind 都能往返
# --------------------------------------------------------------------------------------


def test_semantic_record_roundtrip() -> None:
    built = record()
    assert built.kind is MemoryKind.SEMANTIC
    assert built.payload.category is SemanticCategory.PREFERENCE
    assert built.schema_version == 2
    assert built.status is MemoryStatus.ACTIVE and built.version == 1
    assert built.project_id is None


def test_episodic_record_roundtrip() -> None:
    built = record(
        kind=MemoryKind.EPISODIC, payload=episodic_payload(),
    )
    assert built.payload.lesson == "先确认端口再重跑"


def test_procedural_record_roundtrip() -> None:
    built = record(
        kind=MemoryKind.PROCEDURAL, payload=procedural_payload(),
    )
    assert built.payload.success_condition == "全绿且无残留"


def test_profile_tier_accepts_user_global_semantic() -> None:
    built = record(tier=MemoryTier.PROFILE)
    assert built.tier is MemoryTier.PROFILE


def test_project_scope_accepts_matching_project_id() -> None:
    built = record(scope=MemoryScope.PROJECT, project_id="project-x")
    assert built.project_id == "project-x"


def test_user_edit_source_is_representable() -> None:
    """R8：用户编辑权威必须能在记录契约里表达（后续票据此防止被助手证据覆盖）。

    这条只钉**无会话**的 `user_edit`（§6.1 豁免的字面形态）；带会话的 `user_edit`
    不允许空 provenance，由 `test_user_edit_with_a_session_still_requires_an_event_id` 钉住。
    """
    built = record(source_type=SourceType.USER_EDIT, source_event_ids=[], source_session_id=None)
    assert built.source_type is SourceType.USER_EDIT


# --------------------------------------------------------------------------------------
# 反控：内容与数值边界
# --------------------------------------------------------------------------------------


def test_content_length_boundary_500_accepted_501_rejected() -> None:
    assert len(record(content="x" * 500).content) == 500
    with pytest.raises(ValidationError):
        record(content="x" * 501)


def test_empty_content_rejected() -> None:
    with pytest.raises(ValidationError):
        record(content="")


@pytest.mark.parametrize(
    "importance", [-0.01, 1.01, float("nan"), float("inf"), float("-inf")],
)
def test_importance_out_of_range_rejected(importance: float) -> None:
    """越界值与非有限值（`nan` / `inf` / `-inf`）走同一条 `ge`/`le` 判据。

    `ge`/`le` 之所以够用：`nan` 与任何数比较均为假 ⇒ 不满足 `ge`；`inf` 不满足
    `le`。正控见 `test_score_inclusive_bounds_accepted`。
    """
    with pytest.raises(ValidationError):
        record(importance=importance)


@pytest.mark.parametrize(
    "strength", [-0.01, 1.01, float("nan"), float("inf"), float("-inf")],
)
def test_strength_out_of_range_rejected(strength: float) -> None:
    """与 `importance` 同一条判据（非有限值同样被 `ge`/`le` 挡住）。"""
    with pytest.raises(ValidationError):
        record(strength=strength)


@pytest.mark.parametrize("value", [0.0, 1.0])
def test_score_inclusive_bounds_accepted(value: float) -> None:
    built = record(importance=value, strength=value)
    assert built.importance == value and built.strength == value


def test_version_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        record(version=0)


def test_schema_version_must_be_literal_2() -> None:
    with pytest.raises(ValidationError):
        record(schema_version=1)


# --------------------------------------------------------------------------------------
# 反控：kind / payload 判别与必需字段
# --------------------------------------------------------------------------------------


def test_kind_payload_mismatch_rejected() -> None:
    with pytest.raises(ValidationError):
        record(kind=MemoryKind.EPISODIC, payload=semantic_payload())


@pytest.mark.parametrize("missing", ["subject", "fact", "category"])
def test_semantic_payload_missing_field_rejected(missing: str) -> None:
    fields = {"subject": "s", "fact": "f", "category": SemanticCategory.PREFERENCE}
    del fields[missing]
    with pytest.raises(ValidationError):
        SemanticPayload(**fields)


@pytest.mark.parametrize("missing", ["situation", "action", "outcome", "lesson"])
def test_episodic_payload_missing_field_rejected(missing: str) -> None:
    fields = {"situation": "s", "action": "a", "outcome": "o", "lesson": "l"}
    del fields[missing]
    with pytest.raises(ValidationError):
        EpisodicPayload(**fields)


@pytest.mark.parametrize("missing", ["trigger", "procedure", "success_condition"])
def test_procedural_payload_missing_field_rejected(missing: str) -> None:
    fields = {"trigger": "t", "procedure": "p", "success_condition": "c"}
    del fields[missing]
    with pytest.raises(ValidationError):
        ProceduralPayload(**fields)


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        record(kind="not_a_kind", payload=semantic_payload())


def test_unknown_semantic_category_rejected() -> None:
    with pytest.raises(ValidationError):
        SemanticPayload(subject="s", fact="f", category="not_a_category")


def test_unknown_source_type_rejected() -> None:
    with pytest.raises(ValidationError):
        record(source_type="telepathy")


def test_unknown_field_rejected() -> None:
    """§6.1：契约字段以外的输入 fail closed（内部字段属于存储层，不属于契约）。"""
    with pytest.raises(ValidationError):
        record(hidden_reasoning="不该出现在契约里")


# --------------------------------------------------------------------------------------
# 反控：tier / scope 组合规则（§4.2 / §4.3 / §6.1）
# --------------------------------------------------------------------------------------


def test_profile_tier_rejects_non_semantic_kind() -> None:
    with pytest.raises(ValidationError):
        record(tier=MemoryTier.PROFILE, kind=MemoryKind.EPISODIC, payload=episodic_payload())


def test_profile_tier_rejects_project_scope() -> None:
    with pytest.raises(ValidationError):
        record(tier=MemoryTier.PROFILE, scope=MemoryScope.PROJECT, project_id="project-x")


def test_project_scope_requires_project_id() -> None:
    with pytest.raises(ValidationError):
        record(scope=MemoryScope.PROJECT)


def test_user_global_scope_rejects_project_id() -> None:
    with pytest.raises(ValidationError):
        record(scope=MemoryScope.USER_GLOBAL, project_id="project-x")


def test_blank_project_id_rejected() -> None:
    with pytest.raises(ValidationError):
        record(scope=MemoryScope.PROJECT, project_id="")


# --------------------------------------------------------------------------------------
# 反控：provenance（§6.1 / R6）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("source_type", [SourceType.AUTOMATIC, SourceType.EXPLICIT_COMMAND])
def test_provenance_sources_reject_empty_source_event_ids(source_type: SourceType) -> None:
    """§6.1 的豁免面**只有**「无会话的 `user_edit`」（"may be empty only for direct UI edits
    without a session"）⇒ `automatic` 与 `explicit_command` 都必须指得出事件。

    豁免的另一半（`user_edit` + 有会话 ⇒ 仍然拒绝）见
    `test_user_edit_with_a_session_still_requires_an_event_id`。
    """
    with pytest.raises(ValidationError):
        record(source_type=source_type, source_event_ids=[])


def test_user_edit_with_a_session_still_requires_an_event_id() -> None:
    """豁免是**合取**（`user_edit` **且** 无会话），不是"`user_edit` 就放行"。

    判别性：正控 `test_user_edit_source_is_representable` 用的是 `source_session_id=None`，
    它钉不住"带会话的 `user_edit` 也能空 provenance"这条放宽——把豁免写成只看
    `source_type`，那条正控照样绿。只有本条能把豁免面收回到 §6.1 的字面。
    """
    with pytest.raises(ValidationError):
        record(source_type=SourceType.USER_EDIT, source_event_ids=[], source_session_id="session-1")
    assert record(
        source_type=SourceType.USER_EDIT, source_event_ids=[], source_session_id=None,
    ).source_type is SourceType.USER_EDIT


def test_evidence_is_required() -> None:
    with pytest.raises(ValidationError):
        record(evidence=[])


def test_evidence_excerpt_boundary_300_accepted_301_rejected() -> None:
    bounded = EvidenceItem(role="user", excerpt="y" * 300, hash="h")
    assert len(bounded.excerpt) == 300
    with pytest.raises(ValidationError):
        EvidenceItem(role="user", excerpt="y" * 301, hash="h")


def test_evidence_hash_is_required() -> None:
    with pytest.raises(ValidationError):
        EvidenceItem(role="user", excerpt="e")


# --------------------------------------------------------------------------------------
# 身份：请求侧字段与可信上下文冲突即拒绝（R1 末条 / §6.2）
# --------------------------------------------------------------------------------------


def test_trusted_identity_match_accepted() -> None:
    built = record()
    assert_trusted_identity(built, TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a"))


def test_trusted_identity_tenant_conflict_rejected() -> None:
    with pytest.raises(UntrustedIdentityError):
        assert_trusted_identity(record(), TrustedMemoryIdentity(tenant_id="tenant-b", user_id="user-a"))


def test_trusted_identity_user_conflict_rejected() -> None:
    with pytest.raises(UntrustedIdentityError):
        assert_trusted_identity(record(), TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-b"))


def test_trusted_identity_project_conflict_rejected() -> None:
    built = record(scope=MemoryScope.PROJECT, project_id="project-x")
    with pytest.raises(UntrustedIdentityError):
        assert_trusted_identity(
            built, TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a", project_id="project-y"),
        )


def test_trusted_identity_missing_project_rejected_for_project_scope() -> None:
    built = record(scope=MemoryScope.PROJECT, project_id="project-x")
    with pytest.raises(UntrustedIdentityError):
        assert_trusted_identity(built, TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a"))


def test_trusted_identity_match_for_project_scope_accepted() -> None:
    built = record(scope=MemoryScope.PROJECT, project_id="project-x")
    assert_trusted_identity(
        built, TrustedMemoryIdentity(tenant_id="tenant-a", user_id="user-a", project_id="project-x"),
    )


def test_untrusted_identity_error_is_permission_error() -> None:
    """入口层据此给 403 而不是 500（与 V1 的 PermissionError 口径同源）。"""
    assert issubclass(UntrustedIdentityError, PermissionError)


# --------------------------------------------------------------------------------------
# 值对象不可变
# --------------------------------------------------------------------------------------


def test_record_is_immutable() -> None:
    built = record()
    with pytest.raises(ValidationError):
        built.content = "改写"
