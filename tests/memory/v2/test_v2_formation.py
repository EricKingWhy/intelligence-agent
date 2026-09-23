"""#298 / MEM-V2-2 的 Formation / Adjudication 契约与严格校验（AC3 的形状半边）。

Seam：`parse_formation_result` / `parse_adjudication_result` 与两份契约模型。
政策执法（候选上限、秘密/敏感、用户事实来源）在 `test_v2_policy.py`——那半边必须
能脱离 schema 独立成立（R7），所以测试也分开。

本文件钉三件事：**传输层不复用 V1 的字符级修复**（R3：解析失败是一次失败尝试）、
**身份字段在契约里根本没有位置**（§6.2 末句）、**每个方向的搭配都 fail closed**。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_harness.memory.v2.formation import (
    AdjudicatedContent,
    AdjudicationAction,
    AdjudicationReasonCode,
    AdjudicationResult,
    FormationCandidate,
    FormationDecision,
    FormationResult,
    ModelOutputError,
    ModelSkipReason,
    Sensitivity,
    parse_adjudication_result,
    parse_formation_result,
)
from agent_harness.memory.v2.types import MemoryKind


def _semantic_payload() -> dict:
    return {"kind": "semantic", "subject": "回答风格", "fact": "偏好简洁", "category": "preference"}


def _candidate(**overrides) -> dict:
    values = {
        "kind": "semantic",
        "tier": "collection",
        "scope": "user_global",
        "content": "用户偏好简洁直接的回答",
        "payload": _semantic_payload(),
        "importance": 0.8,
        "strength": 0.9,
        "evidence": [{"event_id": "s:1", "role": "user", "excerpt": "请简洁点"}],
    }
    values.update(overrides)
    return values


def _formation(**overrides) -> str:
    values = {"decision": "CANDIDATES", "candidates": [_candidate()], "skip_reason": None}
    values.update(overrides)
    return json.dumps(values, ensure_ascii=False)


# --------------------------------------------------------------------------------------
# 第 1 层：传输归一（剥围栏可以，"修 JSON" 不可以）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        _formation(),
        f"```json\n{_formation()}\n```",
        f"```\n{_formation()}\n```",
        f"  \n{_formation()}\n  ",
    ],
)
def test_a_well_formed_object_parses_through_the_transport_layer(raw: str) -> None:
    result = parse_formation_result(raw)

    assert result.decision is FormationDecision.CANDIDATES
    assert len(result.candidates) == 1


def test_an_already_parsed_mapping_parses() -> None:
    result = parse_formation_result(
        {"decision": "CANDIDATES", "candidates": [_candidate()], "skip_reason": None})

    assert result.candidates[0].content == "用户偏好简洁直接的回答"


@pytest.mark.parametrize(
    "raw",
    [
        "",                                         # 空输出
        "   ",                                      # 只有空白
        "I think the user prefers brevity.",        # 不是 JSON
        "[]",                                       # 是 JSON 但形状不对
        '{"decision": "CANDIDATES", "candidates": [],}',
    ],
)
def test_malformed_output_is_a_failed_attempt_not_an_abstention(raw: str) -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(raw)


def test_a_trailing_comma_is_not_repaired() -> None:
    """判别性：V1 的 `_repair_json` 会把尾逗号删掉后重校验通过；V2 **刻意不做**字符级修复。

    这条把"R3：解析失败 = 一次失败尝试"钉在实现上：一旦有人为了省一次重试把 V1 的
    修复器搬过来，这条会变红。
    """
    with pytest.raises(ModelOutputError):
        parse_formation_result(
            '{"decision":"NO_MEMORY","skip_reason":"no_durable_value",}')


def test_the_error_summary_names_the_offending_field() -> None:
    with pytest.raises(ModelOutputError) as raised:
        parse_formation_result(_formation(candidates=[_candidate(content="")]))

    assert "candidates.0.content" in str(raised.value)


def test_a_non_string_non_mapping_input_is_rejected() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(123)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# 顶层决定的两个方向（§6.2）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", [item.value for item in ModelSkipReason])
def test_every_documented_skip_reason_is_representable(reason: str) -> None:
    result = parse_formation_result(
        {"decision": "NO_MEMORY", "candidates": [], "skip_reason": reason})

    assert result.decision is FormationDecision.NO_MEMORY
    assert result.skip_reason is ModelSkipReason(reason)


@pytest.mark.parametrize(
    "overrides",
    [
        {"decision": "NO_MEMORY", "candidates": [_candidate()], "skip_reason": "no_durable_value"},
        {"decision": "NO_MEMORY", "candidates": [], "skip_reason": None},
        {"decision": "CANDIDATES", "candidates": [], "skip_reason": "no_durable_value"},
        {"decision": "CANDIDATES", "candidates": [_candidate()], "skip_reason": "no_durable_value"},
    ],
)
def test_self_contradictory_decisions_are_rejected(overrides: dict) -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(**overrides))


@pytest.mark.parametrize("decision", ["candidates", "MAYBE", None, 1])
def test_an_unrecognized_decision_is_rejected(decision) -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(decision=decision))


def test_an_unrecognized_skip_reason_is_rejected() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(
            {"decision": "NO_MEMORY", "candidates": [], "skip_reason": "feels_like_it"})


def test_an_extra_field_is_a_parse_failure_not_an_ignored_field() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(confidence=0.9))


# --------------------------------------------------------------------------------------
# 候选项：身份不可伪造（AC3 的 forged-identity 方向）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field_", ["id", "tenant_id", "user_id", "version", "created_at", "status"])
def test_a_candidate_cannot_supply_server_owned_fields(field_: str) -> None:
    """模型输出里出现服务器拥有的字段 ⇒ 解析失败。

    这是 §6.2「Runtime replaces all identity fields」在类型上的落地：契约里根本
    没有这些位置，所以"伪造身份"不是被覆盖，而是被拒绝。
    """
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(**{field_: "x"})]))


def test_a_candidate_cannot_supply_an_evidence_hash() -> None:
    """`hash` 由运行时按摘录算——模型自带哈希等于自己给自己盖章。"""
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(
            evidence=[{"event_id": "s:1", "role": "user", "excerpt": "请简洁点", "hash": "deadbeef"}],
        )]))


# --------------------------------------------------------------------------------------
# 候选项：组合规则的每个方向（与信封共用 `assert_content_contract`）
# --------------------------------------------------------------------------------------


def test_a_valid_candidate_roundtrips_every_field() -> None:
    candidate = parse_formation_result(_formation()).candidates[0]

    assert candidate.kind is MemoryKind.SEMANTIC
    assert candidate.tier.value == "collection"
    assert candidate.scope.value == "user_global"
    assert candidate.project_id is None
    assert candidate.importance == 0.8 and candidate.strength == 0.9
    assert candidate.sensitivity is Sensitivity.ORDINARY
    assert candidate.sensitive_category is None
    assert candidate.evidence[0].event_id == "s:1"


def test_payload_kind_must_match_the_candidate_kind() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(kind="episodic")]))


@pytest.mark.parametrize(
    "overrides",
    [
        {"tier": "profile", "kind": "episodic",
         "payload": {"kind": "episodic", "situation": "s", "action": "a", "outcome": "o",
                     "lesson": "l"}},
        {"tier": "profile", "scope": "project", "project_id": "p-1"},
    ],
)
def test_the_profile_tier_combination_rules_hold_for_candidates(overrides: dict) -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(**overrides)]))


def test_a_project_candidate_requires_a_project_id() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(scope="project")]))


def test_a_user_global_candidate_must_not_carry_a_project_id() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(project_id="p-1")]))


def test_a_project_candidate_is_valid_with_one() -> None:
    candidate = parse_formation_result(
        _formation(candidates=[_candidate(scope="project", project_id="p-1")])).candidates[0]

    assert (candidate.scope.value, candidate.project_id) == ("project", "p-1")


def test_content_over_the_limit_is_rejected() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(content="长" * 501)]))


def test_content_may_be_exactly_at_the_limit() -> None:
    candidate = parse_formation_result(
        _formation(candidates=[_candidate(content="长" * 500)])).candidates[0]

    assert len(candidate.content) == 500


def test_a_candidate_needs_at_least_one_evidence_reference() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(evidence=[])]))


def test_an_empty_evidence_excerpt_is_rejected() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(
            evidence=[{"event_id": "s:1", "role": "user", "excerpt": ""}])]))


def test_an_over_long_evidence_excerpt_is_rejected() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(
            evidence=[{"event_id": "s:1", "role": "user", "excerpt": "x" * 301}])]))


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan")])
@pytest.mark.parametrize("field_", ["importance", "strength"])
def test_importance_and_strength_are_bounded(field_: str, value: float) -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(**{field_: value})]))


# --------------------------------------------------------------------------------------
# 敏感度自陈的搭配（政策层另测；这里只钉形状）
# --------------------------------------------------------------------------------------


def test_a_sensitive_candidate_must_name_an_applicable_category() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(sensitivity="sensitive")]))


def test_a_sensitive_candidate_with_a_category_is_valid() -> None:
    candidate = parse_formation_result(_formation(candidates=[_candidate(
        sensitivity="sensitive", sensitive_category="health")])).candidates[0]

    assert candidate.sensitivity is Sensitivity.SENSITIVE
    assert candidate.sensitive_category.value == "health"


@pytest.mark.parametrize("sensitivity", ["ordinary", "secret"])
def test_a_non_sensitive_candidate_must_not_name_a_category(sensitivity: str) -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(
            sensitivity=sensitivity, sensitive_category="financial")]))


def test_an_unrecognized_sensitive_category_is_rejected() -> None:
    with pytest.raises(ModelOutputError):
        parse_formation_result(_formation(candidates=[_candidate(
            sensitivity="sensitive", sensitive_category="vibes")]))


# --------------------------------------------------------------------------------------
# Adjudication（§6.3）：每个方向都 fail closed
# --------------------------------------------------------------------------------------


def _content(**overrides) -> dict:
    values = _candidate()
    values.pop("sensitivity", None)
    values.update(overrides)
    return values


@pytest.mark.parametrize("reason", [item.value for item in AdjudicationReasonCode])
def test_every_documented_reason_code_is_representable(reason: str) -> None:
    result = parse_adjudication_result(
        {"action": "NOOP", "reason_code": reason})

    assert result.action is AdjudicationAction.NOOP
    assert result.reason_code is AdjudicationReasonCode(reason)


@pytest.mark.parametrize(
    "payload",
    [
        # ADD：不给目标、必须给结果
        {"action": "ADD", "target_memory_id": "mem-1", "result": _content(),
         "reason_code": "durable_new"},
        {"action": "ADD", "reason_code": "durable_new"},
        # UPDATE：必须有目标、也必须有结果
        {"action": "UPDATE", "result": _content(), "reason_code": "enrich_existing"},
        {"action": "UPDATE", "target_memory_id": "mem-1", "reason_code": "enrich_existing"},
        # INVALIDATE：必须有目标、不得带替换结果
        {"action": "INVALIDATE", "reason_code": "contradicts_existing"},
        {"action": "INVALIDATE", "target_memory_id": "mem-1", "result": _content(),
         "reason_code": "contradicts_existing"},
        # NOOP：什么都不带
        {"action": "NOOP", "target_memory_id": "mem-1", "reason_code": "duplicate"},
        {"action": "NOOP", "result": _content(), "reason_code": "duplicate"},
        # reason_code 是必填
        {"action": "NOOP"},
    ],
)
def test_self_contradictory_adjudications_are_rejected(payload: dict) -> None:
    with pytest.raises(ModelOutputError):
        parse_adjudication_result(payload)


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("ADD", {"action": "ADD", "result": _content(), "reason_code": "durable_new"}),
        ("UPDATE", {"action": "UPDATE", "target_memory_id": "mem-1", "result": _content(),
                    "reason_code": "enrich_existing"}),
        ("INVALIDATE", {"action": "INVALIDATE", "target_memory_id": "mem-1",
                        "reason_code": "contradicts_existing"}),
        ("NOOP", {"action": "NOOP", "reason_code": "duplicate"}),
    ],
)
def test_each_action_has_a_well_formed_shape(action: str, payload: dict) -> None:
    result = parse_adjudication_result(payload)

    assert result.action.value == action


def test_an_adjudication_result_is_content_only() -> None:
    """`result` 是内容字段齐全的 draft 形状——没有 id / 版本 / 身份。"""
    result = parse_adjudication_result(
        {"action": "ADD", "result": _content(), "reason_code": "durable_new"})

    assert isinstance(result.result, AdjudicatedContent)
    assert "id" not in AdjudicatedContent.model_fields
    assert "tenant_id" not in AdjudicatedContent.model_fields


def test_an_adjudication_result_cannot_forge_identity_fields() -> None:
    with pytest.raises(ModelOutputError):
        parse_adjudication_result({
            "action": "ADD", "result": _content(tenant_id="someone-else"),
            "reason_code": "durable_new"})


def test_a_forged_adjudication_payload_raises_a_pydantic_error_directly() -> None:
    """直接构造（不经解析器）时同样是硬失败——契约模型自己就拒绝。"""
    with pytest.raises(ValidationError):
        AdjudicationResult(
            action=AdjudicationAction.NOOP, target_memory_id="mem-1",
            reason_code=AdjudicationReasonCode.DUPLICATE)


def test_a_formation_result_rejects_a_forged_identity_directly() -> None:
    with pytest.raises(ValidationError):
        FormationResult(
            decision=FormationDecision.CANDIDATES,
            candidates=[FormationCandidate(**_candidate(user_id="someone-else"))])


def test_candidates_are_not_capped_at_the_contract_layer() -> None:
    """上限是**政策**而不是形状（§5.2.4：超额候选按 durable value 排序后丢弃）。

    所以 6 个候选必须能**解析成功**——把上限写进 schema 会让"超额"变成一次失败尝试，
    而 PRD 要求的是截断到 5。
    """
    result = parse_formation_result(_formation(candidates=[_candidate() for _ in range(6)]))

    assert len(result.candidates) == 6
