"""real_eval 统计（issue #1005）：纯函数单元测试。"""

from __future__ import annotations

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


def test_empty_records_are_zero():
    assert stats.pass_rate([]) == 0.0
    assert stats.infra_rate([]) == 0.0
    assert stats.repeat_sd([]) == 0.0


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


def test_repeat_sd_and_mde():
    # 两题通过率 1.0 与 0.0 -> 总体 SD = 0.5，MDE = 1.0
    records = [
        _rec("q1", 1, ok=True), _rec("q1", 2, ok=True),
        _rec("q2", 1, ok=False), _rec("q2", 2, ok=False),
    ]
    assert stats.repeat_sd(records) == 0.5
    assert stats.mde(records) == 1.0


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


def test_summarize_has_all_keys():
    records = [_rec("q1", 1), _rec("q2", 1, ok=False, kind="J", judge={"valid_votes": 3, "flip": True})]
    summary = stats.summarize(records)
    for key in (
        "overall_pass_rate", "infra_rate", "per_question_pass", "question_pass_ci95",
        "group_pass_rate", "repeat_sd", "mde", "train_vs_test", "tokens_per_question",
        "latency_p50_ms", "latency_p95_ms", "judge_flip_rate", "failure_breakdown",
    ):
        assert key in summary
