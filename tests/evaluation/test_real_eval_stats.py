"""real_eval 统计（issue #1005）：纯函数单元测试。"""

from __future__ import annotations

import pytest

from evaluation.real_eval import stats


def _rec(
    name: str,
    rep: int,
    *,
    ok: bool = True,
    outcome: str = "ok",
    split: str = "train",
    kind: str = "D",
    case_type: str = "tool_selection",
    tags: tuple[str, ...] = (),
    tokens: int = 10,
    latency: int = 100,
    judge: dict | None = None,
) -> dict:
    return {
        "case_name": name,
        "rep": rep,
        "ok": ok,
        "outcome": outcome,
        "split": split,
        "assertion_kind": kind,
        "case_type": case_type,
        "tags": list(tags),
        "usage": {"total_tokens": tokens},
        "latency_ms": latency,
        "judge": judge,
    }


def test_pass_rate_excludes_infra_from_denominator():
    records = [
        _rec("q1", 1, ok=True),
        _rec("q1", 2, ok=False),
        _rec("q2", 1, ok=False, outcome="infra"),
        _rec("q2", 2, ok=True),
    ]
    # 非 infra 3 条，其中 ok 2 条
    assert stats.pass_rate(records) == 2 / 3
    assert stats.infra_rate(records) == 1 / 4


def test_empty_records():
    # 无有效样本 ⇒ pass_rate 未定义（None），不伪造 0.0
    assert stats.pass_rate([]) is None
    assert stats.infra_rate([]) == 0.0
    assert stats.repeat_sd([]) == 0.0


def test_all_infra_pass_rate_is_none():
    records = [_rec("q1", 1, ok=False, outcome="infra"),
               _rec("q2", 1, ok=False, outcome="infra")]
    assert stats.pass_rate(records) is None
    assert stats.summarize(records)["no_valid_samples"] is True
    # 有一半 infra 时仍按非 infra 计
    mixed = records + [_rec("q3", 1, ok=True)]
    assert stats.pass_rate(mixed) == 1.0
    assert stats.summarize(mixed)["no_valid_samples"] is False


def test_per_question_pass_and_undefined_for_all_infra():
    records = [
        _rec("q1", 1, ok=True),
        _rec("q1", 2, ok=False),
        _rec("q2", 1, outcome="infra", ok=False),
    ]
    result = stats.per_question_pass(records)
    assert result["q1"] == 0.5
    assert result["q2"] is None  # 全 infra -> 分母为 0 -> None（不伪造 0）


def test_bootstrap_ci_deterministic_and_bounded():
    values = [0.0, 0.5, 1.0, 1.0, 0.0]
    lo1, hi1 = stats.bootstrap_ci(values, n_resamples=2000)
    lo2, hi2 = stats.bootstrap_ci(values, n_resamples=2000, seed=stats.BOOTSTRAP_SEED)
    assert (lo1, hi1) == (lo2, hi2), "固定 seed 必须可复现"
    assert 0.0 <= lo1 <= hi1 <= 1.0
    # 常量输入 -> CI 收在常量上（bootstrap 正确性下界）
    constant = [0.25, 0.25, 0.25]
    assert stats.bootstrap_ci(constant, n_resamples=500) == (0.25, 0.25)


def test_question_pass_ci_reports_questions():
    records = [_rec("q1", 1, ok=True), _rec("q2", 1, ok=False)]
    result = stats.question_pass_ci(records, n_resamples=500)
    assert result["n_questions"] == 2
    assert result["ci95_low"] <= result["mean"] <= result["ci95_high"]


def test_group_pass_rate_by_case_type_and_tags():
    records = [
        _rec("q1", 1, ok=True, case_type="tool_selection", tags=("a",)),
        _rec("q2", 1, ok=False, case_type="recovery", tags=("a", "b")),
    ]
    groups = stats.group_pass_rates(records)
    assert groups["case_type:tool_selection"] == 1.0
    assert groups["case_type:recovery"] == 0.0
    assert groups["tag:a"] == 0.5
    assert groups["tag:b"] == 0.0


def test_repeat_sd_is_within_question_and_question_pass_sd_is_between():
    # q1 两次都过（题内方差 0）；q2 一过一挂（p=0.5，题内方差 0.25）
    records = [
        _rec("q1", 1, ok=True), _rec("q1", 2, ok=True),
        _rec("q2", 1, ok=True), _rec("q2", 2, ok=False),
    ]
    # 题内重复 SD = sqrt(mean(0, 0.25)) = 0.353553...
    assert stats.repeat_sd(records) == pytest.approx(0.35355339, abs=1e-6)
    # 题间 SD：通过率 [1.0, 0.5] 的总体 SD = 0.25
    assert stats.question_pass_sd(records) == pytest.approx(0.25, abs=1e-9)
    # 二者是不同的量（不是同一个数）
    assert stats.repeat_sd(records) != stats.question_pass_sd(records)


def test_mde_uses_question_level_standard_error():
    # 三题通过率 1.0 / 0.5 / 0.0：题间 SD = sqrt(2/9)? 手算：mean=0.5，
    # 方差 = ((0.5)^2+(0)^2+(0.5)^2)/3 = 0.5/3 -> SD=sqrt(1/6)
    records = [
        _rec("q1", 1, ok=True), _rec("q1", 2, ok=True),
        _rec("q2", 1, ok=True), _rec("q2", 2, ok=False),
        _rec("q3", 1, ok=False), _rec("q3", 2, ok=False),
    ]
    sd = stats.question_pass_sd(records)
    expected_se = sd / (3 ** 0.5)
    assert stats.mde(records) == pytest.approx((1.959963984540054 + 0.8416212335729143) * expected_se)
    # 单题（SE 无定义）MDE 为 0
    assert stats.mde([_rec("q1", 1, ok=True)]) == 0.0


def test_train_vs_test():
    records = [
        _rec("q1", 1, ok=True, split="train"),
        _rec("q2", 1, ok=False, split="test"),
    ]
    assert stats.train_vs_test(records) == {"train": 1.0, "test": 0.0}


def test_tokens_per_question():
    records = [
        _rec("q1", 1, tokens=10), _rec("q1", 2, tokens=10),
        _rec("q2", 1, tokens=20),
    ]
    assert stats.tokens_per_question(records) == 20.0  # (10+10+20)/2


def test_percentile_linear_interpolation():
    values = [1.0, 2.0, 3.0, 4.0]
    assert stats.percentile(values, 50) == 2.5
    assert stats.percentile(values, 100) == 4.0
    assert stats.percentile([], 50) == 0.0
    assert stats.latency_percentiles(
        [_rec("q1", 1, latency=100), _rec("q2", 1, latency=200)]
    ) == {"p50_ms": 150.0, "p95_ms": 195.0}


def test_judge_flip_rate():
    judged = {"valid_votes": 3, "flip": True}
    stable = {"valid_votes": 3, "flip": False}
    records = [
        _rec("q1", 1, kind="J", judge=judged),
        _rec("q2", 1, kind="J", judge=stable),
        _rec("q3", 1, kind="D", judge=None),  # 非 J 不计入
    ]
    assert stats.judge_flip_rate(records) == 0.5


def test_classify_failure_heuristics_and_override():
    assert stats.classify_failure(_rec("q1", 1, ok=False, outcome="infra")) == "environment"
    assert stats.classify_failure(_rec("q2", 1, ok=False, kind="D")) == "capability_gap"
    assert stats.classify_failure(_rec("q3", 1, ok=False, kind="J")) == "judge_misgrade"
    overrides = {"q3": "task_ambiguity"}
    assert stats.classify_failure(_rec("q3", 1, ok=False, kind="J"), overrides) == "task_ambiguity"
    assert stats.failure_breakdown(
        [_rec("q1", 1, ok=False, outcome="infra"),
         _rec("q2", 1, ok=False, kind="D"),
         _rec("q3", 1, ok=True)],
        overrides,
    )["environment"] == 1


def test_classify_failure_illegal_override_falls_back_to_heuristic():
    # 非法 override 值不得被采纳，回落到启发式分类
    overrides = {"q1": "not_a_real_class"}
    assert stats.classify_failure(
        _rec("q1", 1, ok=False, kind="D"), overrides
    ) == "capability_gap"
    assert stats.classify_failure(
        _rec("q2", 1, ok=False, kind="J"), overrides
    ) == "judge_misgrade"


def test_per_question_metrics_are_train_only():
    """逐题指标只对 train；Test 题不进 per_question_pass。"""
    records = [
        _rec("q1", 1, ok=True, split="train"),
        _rec("q2", 1, ok=False, split="test"),
    ]
    per_q = stats.per_question_pass(records)
    assert set(per_q) == {"q1"}
    # 全量口径（split=None）才看得到 test
    assert set(stats.per_question_pass(records, split=None)) == {"q1", "q2"}
    # 聚合的 train_vs_test 仍分开报
    assert stats.train_vs_test(records) == {"train": 1.0, "test": 0.0}


def test_summarize_has_all_keys_and_values():
    records = [
        _rec("q1", 1, ok=True),
        _rec("q2", 1, ok=False, kind="J", judge={"valid_votes": 3, "flip": True}),
    ]
    summary = stats.summarize(records)
    for key in (
        "overall_pass_rate", "no_valid_samples", "n_scored", "infra_rate",
        "per_question_pass", "question_pass_ci95", "group_pass_rate",
        "question_pass_sd", "repeat_sd", "mde", "train_vs_test",
        "tokens_per_question", "latency_p50_ms", "latency_p95_ms",
        "judge_flip_rate", "failure_breakdown",
    ):
        assert key in summary
    # 头条指标就是 overall_pass_rate（池化），非题级均值
    assert summary["overall_pass_rate"] == 0.5
    assert summary["no_valid_samples"] is False
    assert summary["n_scored"] == 2
    assert summary["judge_flip_rate"] == 1.0
