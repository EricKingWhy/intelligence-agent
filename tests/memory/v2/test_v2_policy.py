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
   注意这里的 `event_id` 在生产路径上装的是**投影发的别名**（`e1`…），不是会话日志的真实
   id；本文件默认走 `refs=None`（真 id）那条路，别名键空间的用例在最后一组。
   （全貌见 `projection` 模块 docstring。）
"""

from __future__ import annotations

import dataclasses
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
    ToolOutcome,
    durable_value,
    find_secret,
    read_tool_result,
    resolve_evidence_source,
    resolve_tool_outcome,
    select_candidates,
)
from agent_harness.memory.v2.types import MemoryKind
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.tooling.result import ErrorCode, ToolResult

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
    """一条**读不出 `ok`** 的 `tool/result`（`content` 不是 `ToolResult` 的 JSON）。

    刻意保留这个形状：`resolve_tool_outcome` 对它是 `UNKNOWN`，而 UNKNOWN 不算
    "成功/纠正"——所以它反过来是 R5 门槛的一组反控素材。
    """
    return _event(event_id, TOOL_RESULT, {"tool_call_id": "c1", "content": "ok"})


def _tool_ok(event_id: str = "t:ok1", tool_call_id: str = "c1") -> SessionEvent:
    """一条运行时读得出 `ok=True` 的 `tool/result`。"""
    result = ToolResult.success("done")
    return _event(
        event_id, TOOL_RESULT,
        {"tool_call_id": tool_call_id, "content": result.model_dump_json()},
    )


def _tool_failed(event_id: str = "t:bad", tool_call_id: str = "c9") -> SessionEvent:
    """一条读得出 `ok=False` 的 `tool/result`（"纠正"那半边）。"""
    result = ToolResult.failure("boom", error_code=ErrorCode.TOOL_EXECUTION_ERROR)
    return _event(
        event_id, TOOL_RESULT,
        {"tool_call_id": tool_call_id, "content": result.model_dump_json()},
    )


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


def _default_events() -> list[SessionEvent]:
    """默认事件池：一条用户消息、一条助手消息、一条**读不出**的工具结果，
    以及两条**读得出**的工具结果（成功 + 失败各一）。

    为什么池子里要有"读得出 ok"的两条：R5 的"成功/纠正"半边要求证据事件本身合格，
    没有它们的池子只能测到"独立"那半边。
    """
    return [_user(), _model(), _tool(), _tool_ok("t:ok1", "c1"), _tool_failed()]


def _select(candidates, *, events=None, explicit_remember: bool = False, refs=None):
    return select_candidates(
        candidates,
        events=_default_events() if events is None else events,
        explicit_remember=explicit_remember,
        refs=refs,
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
            _evidence("t:ok1", "tool", "第一次成功了"),
            _evidence("t:bad", "tool", "第二次踩了坑改过来"),
        ])])
    assert len(outcome.accepted) == 1


def test_a_procedural_candidate_from_one_qualifying_event_is_rejected() -> None:
    """只有一条读得出 `ok` 的结果事件时不算两条——另一条是助手文本，不是"行动"。"""
    outcome = _select([_candidate(
        kind="procedural", payload=_payload("procedural"),
        evidence=[
            _evidence("m:1", "assistant", "先跑 pnpm i"),
            _evidence("t:ok1", "tool", "成功了"),
        ])])
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.PROCEDURAL_THRESHOLD_NOT_MET
    ]


def test_an_unreadable_tool_result_does_not_count_as_a_qualifying_event() -> None:
    """`UNKNOWN`（`content` 不是 `ToolResult` 的 JSON）不能凑门槛。

    两条都读不出来时门槛必须是**不满足**：否则两条形态污染的结果就能造出一条规则，
    而运行时其实没有任何证据说明这两次行动发生过。
    """
    outcome = _select(
        [_candidate(
            kind="procedural", payload=_payload("procedural"),
            evidence=[_evidence("t:1", "tool", "读不出来"),
                      _evidence("t:2", "tool", "也读不出来")])],
        events=[_tool("t:1"), _tool("t:2")],
    )
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.PROCEDURAL_THRESHOLD_NOT_MET
    ]


def test_a_tool_call_without_any_result_does_not_count_as_a_qualifying_event() -> None:
    """`MISSING`：只有 `tool/call`、没有 `tool/result`。用一条没跑完的调用凑门槛
    同样是伪造——"发起了"与"发生了"是两件事。"""
    outcome = _select(
        [_candidate(
            kind="procedural", payload=_payload("procedural"),
            evidence=[_evidence("tc:pending", "tool", "调用已发出"),
                      _evidence("m:1", "assistant", "继续")])],
        events=[
            _event("tc:pending", TOOL_CALL,
                   {"tool_call_id": "c7", "tool_name": "run_shell", "args": {}}),
            _model(),
        ],
    )
    assert [item.reason for item in outcome.rejected] == [
        PolicyRejection.PROCEDURAL_THRESHOLD_NOT_MET
    ]


def test_two_failed_attempts_count_as_two_qualifying_events() -> None:
    """R5 的"纠正"半边：失败也**可验证**（运行时读得出 `ok=False`），而"做法对不对"
    是语义判断。把失败排除会让"失败两次 ⇒ 换个方案"这类正当经验永远形不成规则。"""
    outcome = _select(
        [_candidate(
            kind="procedural", payload=_payload("procedural"),
            evidence=[
                _evidence("t:bad", "tool", "第一次失败"),
                _evidence("t:bad2", "tool", "第二次失败"),
            ])],
        events=[_tool_failed("t:bad", "c1"), _tool_failed("t:bad2", "c2")],
    )
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

    procedural 的 fixture 带**两条合格**证据（两条读得出 `ok` 的工具结果）：R5 的门槛比
    名额先判，用一条证据的 procedural 会全部死在 `PROCEDURAL_THRESHOLD_NOT_MET` 上，
    名额那条规则就永远测不到。
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
                    _evidence("t:ok1", "tool", f"#{index}"),
                    _evidence("t:bad", "tool", f"#{index}"),
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


# --------------------------------------------------------------------------------------
# 第 9 组：`tool/result` 的运行时读法（R5"成功/纠正"的判据来源）
# --------------------------------------------------------------------------------------
#
# `tool/result` 的 `data` 只有 `{tool_call_id, content}`，而 `content` 是 `ToolResult`
# 的 JSON——"这次工具成没成"因此是**可以**确定性读出的。这组用例钉住读法的每一个分岔：
# 读得出就是成功/失败，读不出就是 unknown（**不猜**），没有结果事件就是 missing。


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (ToolResult.success("ok").model_dump_json(), ToolOutcome.SUCCESS),
        (ToolResult.failure("no", error_code=ErrorCode.TIMEOUT).model_dump_json(),
         ToolOutcome.FAILURE),
    ],
)
def test_a_readable_result_event_maps_to_success_or_failure(
    content: str, expected: ToolOutcome,
) -> None:
    event = _event("t:1", TOOL_RESULT, {"tool_call_id": "c1", "content": content})
    assert resolve_tool_outcome(event) is expected


@pytest.mark.parametrize(
    "content",
    ["", "not json", '"a string"', "[1, 2]", "null", '{"message": "ok 字段缺席"}',
     '{"ok": "true"}', '{"ok": 1}'],
)
def test_an_unreadable_result_maps_to_unknown(content: str) -> None:
    """`"true"` / `1` 都不算成功：JSON 里 `ok` 只会是真布尔，宽松判定会把
    `"false"` 这类字符串读成"成功"，那是把模型的形态污染变成一条假证据。"""
    event = _event("t:1", TOOL_RESULT, {"tool_call_id": "c1", "content": content})
    assert resolve_tool_outcome(event) is ToolOutcome.UNKNOWN


@pytest.mark.parametrize(
    "event",
    [
        _event("u:1", USER_MESSAGE, {"content": "问"}),
        _event("m:1", MODEL_COMPLETED, {"content": "答"}),
        _event("tc:1", TOOL_CALL, {"tool_call_id": "c1", "tool_name": "t", "args": {}}),
        _event("rc:1", "run/completed", {"status": "ok"}),
    ],
)
def test_a_non_result_event_is_missing(event: SessionEvent) -> None:
    """`MISSING` 与 `UNKNOWN` 是两件事：前者"没有结果"，后者"结果读不出来"。
    两者都不算成功，但归因不同——事件里的 attempt/stage 要能区分。"""
    assert resolve_tool_outcome(event) is ToolOutcome.MISSING


def test_a_result_event_with_contaminated_data_shape_still_answers() -> None:
    """`data` 形状污染时退化成"读不出"，而不是抛错 brick 掉整个政策执行。"""
    event = _event("t:1", TOOL_RESULT, {"content": ToolResult.success("ok").model_dump_json()})
    contaminated = dataclasses.replace(event, data=None)  # type: ignore[arg-type]
    assert resolve_tool_outcome(contaminated) is ToolOutcome.UNKNOWN


def test_read_tool_result_exposes_the_fields_the_projection_needs() -> None:
    """`projection` 要的 `message` / `artifact_ref` 与政策要的 `ok` 走**同一次解析**。
    两处各解析一次 = 两处各写一份容错，某个字段口径改了另一处不知道。"""
    result = ToolResult.success("install ok", data={"anything": 1}).model_copy(
        update={"artifact_ref": "art-9f2c"})
    view = read_tool_result(_event(
        "t:1", TOOL_RESULT, {"tool_call_id": "c1", "content": result.model_dump_json()}))
    assert view is not None
    assert (view.ok, view.message, view.artifact_ref) == (True, "install ok", "art-9f2c")


@pytest.mark.parametrize(
    "content",
    ["", "not json", '"a string"', "[1, 2]", '{"ok": "true", "message": 5, "artifact_ref": 7}'],
)
def test_read_tool_result_answers_none_or_defaults_on_damaged_content(content: str) -> None:
    """读不出 `ok` ⇒ `None`（不猜）；字段类型不符 ⇒ 那个字段退化成空（其余仍然可用）。"""
    view = read_tool_result(
        _event("t:1", TOOL_RESULT, {"tool_call_id": "c1", "content": content}))
    if content == '{"ok": "true", "message": 5, "artifact_ref": 7}':
        assert view is not None
        assert (view.ok, view.message, view.artifact_ref) == (None, "", None)
    else:
        assert view is None


def test_read_tool_result_refuses_a_non_result_event() -> None:
    """不按事件类型设防时，一段**长得像工具结果**的用户文本会被读成执行结果。

    最后一条是关键：用户粘一段 `{"ok": true}` 进对话（完全可能出现），
    若只看 `content` 像不像 JSON，它就会被当成"某次工具成功了"。
    """
    assert read_tool_result(_user()) is None
    assert read_tool_result(_model()) is None
    assert read_tool_result(
        _event("u:2", USER_MESSAGE, {"content": '{"ok": true, "message": "hi"}'})) is None


def test_the_tool_outcome_values_are_stable_strings() -> None:
    assert {outcome.value for outcome in ToolOutcome} == {
        "success", "failure", "unknown", "missing",
    }


# --------------------------------------------------------------------------------------
# AC3 的 unsupported-source：证据必须指得到本轮的一个真实事件
# --------------------------------------------------------------------------------------


def test_a_candidate_whose_evidence_names_no_known_event_is_rejected() -> None:
    """普通的 semantic 候选也要求可解析的 provenance——不是只有用户事实才需要。

    与 `ghost:1` 那条用户事实用例的区别：那条命中更具体的
    `USER_FACT_WITHOUT_USER_EVIDENCE`（判据在前），这里是一条**不需要任何权威**的普通
    偏好候选，所以兜底判据是唯一可能命中者。
    """
    outcome = _select([_candidate(evidence=[_evidence("ghost:1", "assistant", "编的")])])

    assert outcome.accepted == ()
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.UNSUPPORTED_SOURCE]


def test_one_resolvable_reference_is_enough_for_provenance() -> None:
    """判据是"**至少**有一条指得到"，不是"每一条都要指得到"。

    模型多列一条解析不到的依据（比如它把更早的上下文也算了进来）不该让整条候选作废：
    同错拒整条会把"模型多说了半句"变成"这条记忆永久丢失"。
    """
    outcome = _select([_candidate(evidence=[
        _evidence("m:1", "assistant", "好的"),
        _evidence("ghost:1", "assistant", "从别处抄来的"),
    ])])

    assert len(outcome.accepted) == 1
    assert outcome.rejected == ()


# --------------------------------------------------------------------------------------
# 别名键空间：传了 `refs` 时，模型可引的只有投影发给它的别名（T6b 的 P0 修复）
# --------------------------------------------------------------------------------------
#
# 模型只看得到投影载荷，而载荷里的事件引用是**别名**（`e1`…，真实 `event_id` 不进模型）。
# 所以"政策按真 id 建键空间"会让生产路径上每条候选都落 `unsupported_source`——自动记忆
# 零写入，且日志上看起来只是"模型没给出可解析的证据"。这组用例把键空间钉在别名上，
# 并留一条默认参数的行为锁（`refs=None` ⇒ 真 id，显式命令路径不受影响）。


def test_an_aliased_evidence_item_resolves_through_the_refs_table() -> None:
    outcome = _select(
        [_candidate(evidence=[_evidence("e1", "user", "请用 pnpm")])], refs={"e1": "u:1"})
    assert len(outcome.accepted) == 1
    assert outcome.rejected == ()


def test_a_real_event_id_stops_being_a_valid_citation_when_refs_are_supplied() -> None:
    """别名的存在就是为了让模型**不可能**引真 id——键空间必须只认别名。"""
    outcome = _select(
        [_candidate(evidence=[_evidence("u:1", "user", "请用 pnpm")])], refs={"e1": "u:1"})
    assert [item.reason for item in outcome.rejected] == [PolicyRejection.UNSUPPORTED_SOURCE]


def test_a_ref_pointing_outside_the_run_resolves_to_nothing() -> None:
    """对照表不是"模型可引的清单"，它只是键的翻译；指到本轮没有的事件仍然解析不到。"""
    refs = {"e1": "u:1", "e2": "u:0-not-in-this-run"}
    ghost = _select(
        [_candidate(evidence=[_evidence("e2", "user", "更早那轮说的")])], refs=refs)
    assert [item.reason for item in ghost.rejected] == [PolicyRejection.UNSUPPORTED_SOURCE]

    # 正控：同一张表里的 `e1` 仍然可用（拒的不是"整张表作废"）。
    assert len(_select(
        [_candidate(evidence=[_evidence("e1", "user", "请用 pnpm")])], refs=refs).accepted) == 1


def test_the_procedural_threshold_counts_aliased_events() -> None:
    """R5 的"两条合格事件"也要在别名键空间里数——`sources` 与 `qualifying` 两处键空间
    一分叉，门槛就永远凑不齐（这正是这个洞在政策层的形态）。"""
    outcome = _select(
        [_candidate(
            kind="procedural", payload=_payload("procedural"),
            evidence=[_evidence("e1", "tool", "第一次成功了"),
                      _evidence("e2", "tool", "第二次踩了坑改过来")])],
        refs={"e1": "t:ok1", "e2": "t:bad"},
    )
    assert len(outcome.accepted) == 1


def test_user_authority_is_resolved_through_the_alias_not_the_raw_id() -> None:
    """R6 的地基不变：角色仍由**回查事件**判定，别名只是"用哪个键去查"。"""
    user_fact = {"tier": "profile", "payload": _payload("semantic", category="profile")}
    refs = {"e1": "u:1", "e2": "m:1"}

    accepted = _select(
        [_candidate(evidence=[_evidence("e1", "user", "我在用 Windows")], **user_fact)],
        refs=refs)
    assert len(accepted.accepted) == 1

    # 模型把助手的回复标成 `role="user"`，也不因为换了个键就变成用户证据。
    rejected = _select(
        [_candidate(evidence=[_evidence("e2", "user", "用户说他喜欢简洁")], **user_fact)],
        refs=refs)
    assert [item.reason for item in rejected.rejected] == [
        PolicyRejection.USER_FACT_WITHOUT_USER_EVIDENCE
    ]


def test_without_refs_the_keyspace_remains_the_real_event_ids() -> None:
    """默认参数把键空间留在真 id 上：显式命令路径与既有调用方行为逐字不变。"""
    assert len(_select([_candidate()]).accepted) == 1
    assert [item.reason for item in _select(
        [_candidate(evidence=[_evidence("e1", "user", "请用 pnpm")])]).rejected
    ] == [PolicyRejection.UNSUPPORTED_SOURCE]
