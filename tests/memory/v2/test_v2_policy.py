"""#298 / MEM-V2-2 的政策执法：候选上限、per-kind 规则、秘密 / 敏感、用户事实来源。

Seam：`select_candidates` —— 纯函数，输入是**已经通过形状校验**的候选 + 本轮事件 + 一个
运行时拥有的同意信号，输出是"接受哪些 / 拒绝哪些 + 稳定 reason"。

# 为什么这个文件必须与 `test_v2_formation.py` 分开

R7 要求秘密 / 敏感策略是**模型之外**的运行时边界。所以本文件的每一条都用
**形状完全合法**的候选做输入——"模型说这条没问题"这件事在这里没有任何权重。
如果把这些用例写进形状测试文件，就会出现"形状合法 ⇒ 政策也过"的错觉。

# 三条判据的取舍（连同 ADR-0043 登记）

1. **秘密**：运行时自带确定性扫描器（前缀 / 结构），不看模型的 `sensitivity` 自陈。
   模型把一条真密钥报成 `ordinary` 也一样拒。
2. **敏感**：同意信号由**运行时**持有（`explicit_remember`），模型无法授予；
   自动形成路径上它恒为 False ⇒ 所有 `sensitive` 候选都被拒。
   运行时**不**自建 9 类敏感分类器（词表不是分类器，声称有会是一层假防线）。
3. **证据角色**：角色由运行时按 `event_id` 回查事件判定，**不看模型给的 `role`**——
   模型写 `role="user"` 不会让工具证据变成用户证据。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_harness.memory.v2.formation import (
    FormationCandidate,
    SensitiveCategory,
    Sensitivity,
    parse_formation_result,
)
from agent_harness.memory.v2.policy import (
    CANDIDATE_CAP_BY_KIND,
    CANDIDATE_CAP_TOTAL,
    EvidenceSource,
    PolicyRejection,
    SecretKind,
    durable_value,
    find_secret,
    resolve_evidence_source,
    select_candidates,
)
from agent_harness.memory.v2.types import MemoryKind
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)

# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


def _event(event_id: str, event_type: str, data: dict | None = None) -> SessionEvent:
    return SessionEvent(
        event_id=event_id, seq=0, type=event_type, session_id="s", data=data or {},
    )


def _user(event_id: str = "u:1") -> SessionEvent:
    return _event(event_id, USER_MESSAGE, {"content": "请用 pnpm"})


def _injected_user(event_id: str = "u:inj") -> SessionEvent:
    return _event(event_id, USER_MESSAGE, {"content": "样板纠偏", "injected_by": "guard"})


def _model(event_id: str = "m:1") -> SessionEvent:
    return _event(event_id, MODEL_COMPLETED, {"content": "好的"})


def _tool(event_id: str = "t:1") -> SessionEvent:
    return _event(event_id, TOOL_RESULT, {"tool_call_id": "c1", "content": "ok"})


def _payload(kind: str = "semantic", **overrides) -> dict:
    base = {
        "semantic": {"subject": "包管理器", "fact": "偏好 pnpm", "category": "preference"},
        "episodic": {
            "situation": "装依赖", "action": "用 pnpm", "outcome": "成功", "lesson": "优先 pnpm"},
        "procedural": {
            "trigger": "装依赖", "procedure": "用 pnpm i", "success_condition": "锁文件更新"},
    }[kind]
    base = dict(base)
    base.update(overrides)
    return {"kind": kind, **base}


def _candidate(**overrides) -> FormationCandidate:
    """一份**形状完全合法**的候选。政策用例只在这之上做最小改动。"""
    raw: dict[str, Any] = {
        "kind": "semantic",
        "tier": "collection",
        "scope": "user_global",
        "content": "用户偏好用 pnpm 安装依赖",
        "importance": 0.5,
        "strength": 0.5,
        "evidence": [{"event_id": "u:1", "role": "user", "excerpt": "请用 pnpm"}],
    }
    raw.update(overrides)
    if "payload" not in overrides:
        raw["payload"] = _payload(
            raw["kind"], **({"category": "preference"} if raw["kind"] == "semantic" else {}))
    return parse_formation_result(
        {"decision": "CANDIDATES", "candidates": [raw], "skip_reason": None}
    ).candidates[0]


def _select(candidates, *, events=None, explicit_remember: bool = False):
    return select_candidates(
        candidates,
        events=[_user(), _model(), _tool()] if events is None else events,
        explicit_remember=explicit_remember,
    )


def _evidence(event_id: str, role: str = "assistant", excerpt: str = "…") -> dict:
    return {"event_id": event_id, "role": role, "excerpt": excerpt}


# --------------------------------------------------------------------------------------
# 第 1 组：秘密扫描器本身（R7 / AC9）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIE", SecretKind.PRIVATE_KEY),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", SecretKind.PRIVATE_KEY),
        (
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH0abcdefghijklmnop user@host",
            SecretKind.PRIVATE_KEY,
        ),
        ("my key is sk-abcdefghijklmnopqrstuv", SecretKind.PROVIDER_TOKEN),
        ("ghp_0123456789abcdefghijklmnop", SecretKind.PROVIDER_TOKEN),
        ("github_pat_11ABCDEFG0abcdefghijkl", SecretKind.PROVIDER_TOKEN),
        ("AKIAIOSFODNN7EXAMPLE", SecretKind.PROVIDER_TOKEN),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwx", SecretKind.BEARER_TOKEN),
        (
            (
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
                "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"
            ),
            SecretKind.JWT,
        ),
        ("postgres://admin:hunter2@db.internal:5432/app", SecretKind.CREDENTIAL_URL),
        ("password = correct-horse-battery", SecretKind.PASSWORD_ASSIGNMENT),
        ("API_KEY: live_1234567890", SecretKind.PASSWORD_ASSIGNMENT),
    ],
)
def test_the_runtime_secret_scanner_recognizes_each_kind(text: str, expected: SecretKind) -> None:
    assert find_secret(text) is expected


@pytest.mark.parametrize(
    "text",
    [
        "用户偏好用 pnpm 安装依赖",
        "skipped the step",  # 以 sk 开头，但不是密钥前缀
        "secret 这个词本身不是密钥",
        "the key insight is to avoid retries",
        "password fields are validated client-side",  # 提到 password 但没有赋值
        "",
    ],
)
def test_ordinary_text_is_not_flagged_as_a_secret(text: str) -> None:
    """反控：扫描器不是"看到敏感词就报警"——那会把正常记忆全部拒掉。"""
    assert find_secret(text) is None


# --------------------------------------------------------------------------------------
# 第 2 组：秘密候选一律拒绝，且**不看**模型自陈（R7 / AC3）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"content": "用户的 token 是 sk-abcdefghijklmnopqrstuv"},
        {"payload": _payload(fact="npm token: ghp_0123456789abcdefghijklmnop")},
        {"evidence": [_evidence("u:1", "user", "password = correct-horse-battery")]},
    ],
    ids=["in content", "in payload", "in evidence excerpt"],
)
def test_a_secret_anywhere_in_a_candidate_rejects_it(overrides: dict) -> None:
    outcome = _select([_candidate(**overrides)])
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.SECRET]
    assert outcome.accepted == ()


def test_a_model_declared_secret_is_rejected_even_without_a_lexical_match() -> None:
    """模型说它是秘密 ⇒ 拒。模型的自陈只能收紧、不能放松。"""
    outcome = _select([_candidate(sensitivity=Sensitivity.SECRET)])
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.SECRET]


def test_a_lexical_secret_is_rejected_even_when_the_model_says_ordinary() -> None:
    """**R7 的判别性用例**：运行时扫描与模型分类相互独立。"""
    outcome = _select([_candidate(content="部署密钥 sk-abcdefghijklmnopqrstuv")])
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.SECRET]


def test_a_secret_outranks_a_missing_sensitive_consent() -> None:
    """同时踩两条判据时报**更重**的那条。

    它钉的是 `_reject` 的判据顺序：若把敏感同意放在秘密之前，这条会报
    `sensitive_without_consent`——同一份内容被归因成"用户没同意存健康信息"，
    而真实原因是"这里有密钥，永不存储"。归因错了，排障方向就错了。
    """
    outcome = _select([_candidate(
        sensitivity=Sensitivity.SENSITIVE,
        sensitive_category=SensitiveCategory.FINANCIAL,
        content="银行卡密码 password = correct-horse-battery",
    )])
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.SECRET]


def test_a_secret_in_the_project_identifier_rejects_the_candidate() -> None:
    """`project_id` 也是要写进记录的字段——密钥藏在那儿一样是泄漏。"""
    outcome = _select([_candidate(
        scope="project", project_id="sk-abcdefghijklmnopqrstuv")])
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.SECRET]


def test_a_secret_is_rejected_even_when_the_user_asked_to_remember_it() -> None:
    """§5.7.1：即使用户要求，凭据/密钥也永不存储。"""
    outcome = _select(
        [_candidate(sensitivity=Sensitivity.SECRET, content="sk-abcdefghijklmnopqrstuv")],
        explicit_remember=True,
    )
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.SECRET]


# --------------------------------------------------------------------------------------
# 第 3 组：敏感类别需要**运行时**持有的同意（R7 / §5.7.2）
# --------------------------------------------------------------------------------------


def test_a_sensitive_candidate_without_consent_is_rejected() -> None:
    outcome = _select([_candidate(
        sensitivity=Sensitivity.SENSITIVE, sensitive_category=SensitiveCategory.HEALTH)])
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.SENSITIVE_WITHOUT_CONSENT
    ]


def test_a_sensitive_candidate_is_accepted_when_the_runtime_holds_consent() -> None:
    outcome = _select(
        [_candidate(
            sensitivity=Sensitivity.SENSITIVE, sensitive_category=SensitiveCategory.HEALTH)],
        explicit_remember=True,
    )
    assert len(outcome.accepted) == 1
    assert outcome.rejected == ()


def test_ordinary_content_is_stored_automatically_without_consent() -> None:
    """§5.7.2 的后半句：普通长期信息无需显式请求。"""
    assert len(_select([_candidate()]).accepted) == 1


def test_the_model_cannot_grant_its_own_consent() -> None:
    """候选里没有任何字段能表达"用户同意"——同意只从 `explicit_remember` 进来。"""
    fields = set(FormationCandidate.model_fields)
    assert not {name for name in fields if "consent" in name or "remember" in name}
    outcome = _select([_candidate(
        sensitivity=Sensitivity.SENSITIVE, sensitive_category=SensitiveCategory.LEGAL)])
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.SENSITIVE_WITHOUT_CONSENT
    ]


# --------------------------------------------------------------------------------------
# 第 4 组：证据角色由运行时回查事件判定（R6 的地基）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event, expected",
    [
        (_user(), EvidenceSource.USER),
        (_model(), EvidenceSource.ASSISTANT),
        (_tool(), EvidenceSource.TOOL),
        (_injected_user(), EvidenceSource.OTHER),
        (_event("r:1", "run/completed", {}), EvidenceSource.OTHER),
    ],
)
def test_evidence_source_is_resolved_from_the_event_not_the_model(
    event: SessionEvent, expected: EvidenceSource,
) -> None:
    assert resolve_evidence_source(event) is expected


# --------------------------------------------------------------------------------------
# 第 5 组：R6 —— USER / profile 事实必须有直接用户证据或显式确认
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"tier": "profile", "payload": _payload("semantic", category="profile")},
        {"payload": _payload("semantic", category="profile")},
    ],
    ids=["profile tier", "semantic category=profile"],
)
def test_a_user_fact_resting_only_on_assistant_evidence_is_rejected(overrides: dict) -> None:
    """两种"用户事实"的形态：profile 档位，以及 `category=profile` 的 semantic。"""
    outcome = _select([_candidate(evidence=[_evidence("m:1")], **overrides)])
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.USER_FACT_WITHOUT_USER_EVIDENCE
    ]


def test_a_user_fact_resting_only_on_tool_evidence_is_rejected() -> None:
    outcome = _select([_candidate(
        tier="profile", payload=_payload("semantic", category="profile"),
        evidence=[_evidence("t:1", "tool", "ok")])])
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.USER_FACT_WITHOUT_USER_EVIDENCE
    ]


def test_a_forged_user_role_does_not_make_assistant_evidence_a_user_statement() -> None:
    """模型把 `role` 写成 `user` 也改不了事件本身是模型回复这件事。"""
    outcome = _select([_candidate(
        tier="profile", payload=_payload("semantic", category="profile"),
        evidence=[_evidence("m:1", "user", "用户说他喜欢简洁")])])
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.USER_FACT_WITHOUT_USER_EVIDENCE
    ]


def test_a_user_fact_with_unresolvable_evidence_is_rejected() -> None:
    """指不到本轮任何事件的证据无法证明"用户说过"⇒ fail closed。"""
    outcome = _select([_candidate(
        tier="profile", payload=_payload("semantic", category="profile"),
        evidence=[_evidence("ghost:1", "user", "我今年 30 岁")])])
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.USER_FACT_WITHOUT_USER_EVIDENCE
    ]


def test_a_runtime_injected_user_message_is_not_direct_user_evidence() -> None:
    """注入的纠偏样板消息不是用户说的（沿用 V1 `_is_runtime_injected` 的口径）。"""
    outcome = _select(
        [_candidate(
            tier="profile", payload=_payload("semantic", category="profile"),
            evidence=[_evidence("u:inj", "user", "样板纠偏")])],
        events=[_injected_user(), _model()],
    )
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.USER_FACT_WITHOUT_USER_EVIDENCE
    ]


def test_a_user_fact_with_genuine_user_evidence_is_accepted() -> None:
    outcome = _select([_candidate(
        tier="profile", payload=_payload("semantic", category="profile"),
        evidence=[_evidence("u:1", "user", "我在用 Windows")])])
    assert len(outcome.accepted) == 1


def test_an_explicit_remember_request_counts_as_user_confirmation() -> None:
    outcome = _select(
        [_candidate(
            tier="profile", payload=_payload("semantic", category="profile"),
            evidence=[_evidence("m:1", "assistant", "你在用 Windows")])],
        explicit_remember=True,
    )
    assert len(outcome.accepted) == 1


@pytest.mark.parametrize(
    "payload",
    [
        _payload("semantic", category="project_fact"),
        _payload("semantic", category="constraint"),
        _payload("semantic", category="preference"),
    ],
)
def test_non_user_facts_may_rest_on_assistant_evidence(payload: dict) -> None:
    """§4.4：助手/工具输出**可以**支撑项目事实、情节与过程——只有用户事实不行。"""
    outcome = _select([_candidate(payload=payload, evidence=[_evidence("m:1")])])
    assert len(outcome.accepted) == 1


def test_an_episode_may_rest_on_tool_evidence() -> None:
    outcome = _select([_candidate(
        kind="episodic", payload=_payload("episodic"),
        evidence=[_evidence("t:1", "tool", "ok")])])
    assert len(outcome.accepted) == 1


# --------------------------------------------------------------------------------------
# 第 6 组：R5 —— Procedural 需要两条独立事件，除非用户明确陈述了规则
# --------------------------------------------------------------------------------------


def test_a_procedural_candidate_from_a_single_assistant_event_is_rejected() -> None:
    outcome = _select([_candidate(
        kind="procedural", payload=_payload("procedural"),
        evidence=[_evidence("m:1", "assistant", "先跑 pnpm i")])])
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.PROCEDURAL_THRESHOLD_NOT_MET
    ]


def test_a_procedural_candidate_from_two_identical_evidence_items_is_rejected() -> None:
    """同一事件的两次引用不是"两条独立事件"——这是本条要求最容易漏掉的一面。"""
    outcome = _select([_candidate(
        kind="procedural", payload=_payload("procedural"),
        evidence=[
            _evidence("m:1", "assistant", "先跑 pnpm i"),
            _evidence("m:1", "assistant", "先跑 pnpm i"),
        ])])
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.PROCEDURAL_THRESHOLD_NOT_MET
    ]


def test_a_procedural_candidate_from_two_independent_events_is_accepted() -> None:
    outcome = _select([_candidate(
        kind="procedural", payload=_payload("procedural"),
        evidence=[
            _evidence("m:1", "assistant", "先跑 pnpm i"),
            _evidence("t:1", "tool", "已经成功两次"),
        ])])
    assert len(outcome.accepted) == 1


def test_a_procedural_candidate_from_one_user_stated_rule_is_accepted() -> None:
    """R5 的豁免支：用户明确陈述了规则 ⇒ 一条事件即可。"""
    outcome = _select([_candidate(
        kind="procedural", payload=_payload("procedural"),
        evidence=[_evidence("u:1", "user", "以后一律用 pnpm")])])
    assert len(outcome.accepted) == 1


def test_an_injected_user_message_cannot_exempt_the_procedural_threshold() -> None:
    outcome = _select(
        [_candidate(
            kind="procedural", payload=_payload("procedural"),
            evidence=[_evidence("u:inj", "user", "样板纠偏")])],
        events=[_injected_user(), _model()],
    )
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.PROCEDURAL_THRESHOLD_NOT_MET
    ]


# --------------------------------------------------------------------------------------
# 第 7 组：R4 —— 候选上限与按 durable value 排序截断
# --------------------------------------------------------------------------------------


def _many(kind: str, count: int, *, importance: float = 0.5) -> list[FormationCandidate]:
    """同 kind 的 `count` 条候选；内容互不相同，证据指向同一批可解析事件。

    procedural 的 fixture 带**两条**证据：R5 的门槛比名额先判，用一条证据的 procedural
    会全部死在 `PROCEDURAL_THRESHOLD_NOT_MET` 上，名额那条规则就永远测不到。
    """
    return [
        _candidate(
            kind=kind,
            content=f"{kind} #{index}",
            payload=_payload(
                kind, **({"category": "preference"} if kind == "semantic" else {})),
            importance=importance,
            strength=1.0,
            evidence=(
                [
                    _evidence("m:1", "assistant", f"#{index}"),
                    _evidence("t:1", "tool", f"#{index}"),
                ]
                if kind == "procedural"
                else [_evidence("m:1", "assistant", f"#{index}")]
            ),
        )
        for index in range(count)
    ]


def test_durable_value_blends_importance_and_strength() -> None:
    high = _candidate(importance=0.8, strength=1.0)
    low = _candidate(importance=0.8, strength=0.5)
    assert durable_value(high) > durable_value(low)


def test_the_caps_match_the_prd() -> None:
    assert CANDIDATE_CAP_TOTAL == 5
    assert CANDIDATE_CAP_BY_KIND[MemoryKind.SEMANTIC] == 3
    assert CANDIDATE_CAP_BY_KIND[MemoryKind.EPISODIC] == 2
    assert CANDIDATE_CAP_BY_KIND[MemoryKind.PROCEDURAL] == 1


def test_semantic_candidates_are_capped_at_three() -> None:
    outcome = _select(_many("semantic", 6))
    assert len(outcome.accepted) == 3
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.OVER_CAP] * 3


def test_the_highest_durable_value_survives_the_cap() -> None:
    candidates = _many("semantic", 4)  # 同分 ⇒ 模型顺序 #0..#3
    candidates[3] = _candidate(
        content="最高价值", importance=0.99, strength=1.0,
        evidence=[_evidence("m:1", "assistant", "top")])
    outcome = _select(candidates)
    assert outcome.accepted[0].content == "最高价值"
    # 4 条里只留 3 条 ⇒ 走的是**除最高分之外**排序最低的那条。
    assert [item.candidate.content for item in outcome.rejected] == ["semantic #2"]


def test_the_total_cap_binds_even_when_every_per_kind_cap_allows() -> None:
    """Semantic 3 + Episodic 2 + Procedural 1 = 6 > 5 ⇒ 总量上限必须真的生效。"""
    candidates = (
        _many("semantic", 3, importance=0.9)
        + _many("episodic", 2, importance=0.9)
        + _many("procedural", 1, importance=0.1)
    )
    outcome = _select(candidates)
    assert len(outcome.accepted) == 5
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.OVER_CAP]


def test_a_high_value_procedural_displaces_one_of_the_low_value_semantics() -> None:
    """没有 procedural 时 3 semantic + 2 episodic 恰好占满总量；有它则末位 semantic 出局。"""
    candidates = (
        _many("procedural", 1, importance=0.9)
        + _many("episodic", 2, importance=0.2)
        + _many("semantic", 3, importance=0.2)
    )
    outcome = _select(candidates)
    assert len(outcome.accepted) == 5
    assert outcome.accepted[0].kind is MemoryKind.PROCEDURAL
    assert [item.candidate.content for item in outcome.rejected] == ["semantic #2"]


def test_procedural_is_capped_at_one_even_under_the_total_cap() -> None:
    candidates = _many("procedural", 2, importance=0.9)
    outcome = _select(candidates)
    assert len(outcome.accepted) == 1
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.OVER_CAP]


def test_accepted_candidates_are_returned_in_ranked_order() -> None:
    low = _many("semantic", 1, importance=0.1)[0]
    high = _many("semantic", 1, importance=0.9)[0]
    outcome = _select([low, high])
    assert [candidate.importance for candidate in outcome.accepted] == [0.9, 0.1]


def test_ties_keep_the_model_order() -> None:
    """确定性：同分按模型给出的顺序，不引入任何不稳定排序。"""
    first, second, third = _many("semantic", 3)
    outcome = _select([first, second, third])
    assert [candidate.content for candidate in outcome.accepted] == [
        first.content, second.content, third.content]


def test_a_policy_rejected_candidate_does_not_consume_a_cap_slot() -> None:
    """秘密候选不占名额——否则两条垃圾能把三条合法记忆挤掉。"""
    candidates = [
        _candidate(content=f"key: sk-abcdefghijklmnopqrstuv{index}") for index in range(2)
    ] + _many("semantic", 3)
    outcome = _select(candidates)
    assert len(outcome.accepted) == 3
    assert sorted(item.reason.value for item in outcome.rejected) == ["secret", "secret"]


# --------------------------------------------------------------------------------------
# 第 8 组：整体不变量
# --------------------------------------------------------------------------------------


def test_every_candidate_lands_exactly_once_in_accepted_or_rejected() -> None:
    candidates = (
        _many("semantic", 6)
        + _many("episodic", 2)
        + _many("procedural", 2)
        + [
            _candidate(content="password = correct-horse-battery"),
            _candidate(
                sensitivity=Sensitivity.SENSITIVE,
                sensitive_category=SensitiveCategory.HEALTH),
        ]
    )
    outcome = _select(candidates)
    seen = [id(candidate) for candidate in outcome.accepted]
    seen += [id(item.candidate) for item in outcome.rejected]
    assert sorted(seen) == sorted(id(candidate) for candidate in candidates)


def test_the_caps_hold_for_an_adversarial_mixture() -> None:
    candidates = _many("semantic", 9) + _many("episodic", 9) + _many("procedural", 9)
    outcome = _select(candidates)
    assert len(outcome.accepted) <= CANDIDATE_CAP_TOTAL
    by_kind: dict[str, int] = {}
    for candidate in outcome.accepted:
        by_kind[candidate.kind.value] = by_kind.get(candidate.kind.value, 0) + 1
    for kind, cap in CANDIDATE_CAP_BY_KIND.items():
        assert by_kind.get(kind.value, 0) <= cap


def test_an_empty_candidate_list_is_a_quiet_no_op() -> None:
    outcome = _select([])
    assert outcome.accepted == () and outcome.rejected == ()


def test_the_rejection_reasons_are_stable_strings() -> None:
    """reason code 会进事件与日志 ⇒ 值必须稳定（不是 `repr`）。"""
    assert SecretKind.PROVIDER_TOKEN.value == "provider_token"
    assert PolicyRejection.OVER_CAP.value == "over_cap"
    assert all(isinstance(reason.value, str) for reason in PolicyRejection)
    assert all(isinstance(source.value, str) for source in EvidenceSource)
