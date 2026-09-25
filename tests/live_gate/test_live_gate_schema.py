"""`schema.py` 的判定与版本面（`#307` R1 / R5 / Contracts）。

重点只有两条：**`PASS` 不可由调用方传入**（只能由 3/3 算出来），以及**注入/替身把判定
上限锁到 `FAIL`**。这两条一旦被放宽，"Fake 结果计入 Live Gate"就重新变成可能。
"""

from __future__ import annotations

import pytest

from evaluation.live_gate.schema import (
    GATE_ATTEMPTS,
    SCHEMA_VERSION,
    Verdict,
    decide_verdict,
    load_evidence,
)
from tests.live_gate._evidence_factory import make_evidence

PASS = Verdict.PASS
FAIL = Verdict.FAIL


def test_gate_attempts_is_three_and_not_configurable() -> None:
    """3/3 是硬判据：计划次数是常量，不是参数（允许调小 = 允许"1/1 也算过"）。"""
    assert GATE_ATTEMPTS == 3


def test_three_of_three_is_the_only_pass_path() -> None:
    assert decide_verdict(attempt_statuses=[PASS, PASS, PASS]) is PASS


@pytest.mark.parametrize(
    "statuses",
    [
        [PASS, PASS, FAIL],
        [FAIL, PASS, PASS],
        [PASS, FAIL, PASS],
        [FAIL, FAIL, FAIL],
        [PASS, PASS],  # 尝试数不足：缺的那次不得当成成功
        [PASS, PASS, PASS, PASS],  # 多跑也不算 3/3
        [],
    ],
)
def test_any_shortfall_is_fail(statuses: list[Verdict]) -> None:
    assert decide_verdict(attempt_statuses=statuses) is FAIL


def test_injection_or_seam_caps_verdict_at_fail() -> None:
    """全 PASS 也救不回注入：`#305` 明文禁止假模型/假工具计入 Live Gate。"""
    assert decide_verdict(attempt_statuses=[PASS, PASS, PASS], injected_failure="attempt:2") is FAIL
    assert decide_verdict(
        attempt_statuses=[PASS, PASS, PASS], injected_seams=("capability",),
    ) is FAIL


def test_evidence_has_no_secret_shaped_field_names() -> None:
    """Contracts：证据里不得有 token / secret / 完整授权 header 字段 —— 形状级自检。"""
    text = make_evidence().to_json().lower()
    for forbidden in ("api_key", "authorization", "bearer ", "access_token", "secret_key"):
        assert forbidden not in text, forbidden


def test_load_evidence_rejects_unknown_schema_version() -> None:
    payload = make_evidence().model_dump(mode="json")
    payload["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(ValueError, match="schema_version"):
        load_evidence(payload)


def test_load_evidence_round_trips() -> None:
    evidence = make_evidence()
    loaded = load_evidence(evidence.model_dump(mode="json"))
    assert loaded.verdict is evidence.verdict
    assert loaded.sha == evidence.sha
    assert [attempt.index for attempt in loaded.attempts] == [1, 2, 3]
