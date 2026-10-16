"""real_eval Judge（issue #1005）：3 票多数判定 + JSON 抽取（fake judge_fn，不触网）。"""

from __future__ import annotations

from evaluation.real_eval.judge import extract_verdict, majority_judge


def _seq_fn(verdicts: list[str]) -> object:
    """返回一个按顺序吐 responses 的 judge_fn。"""
    pending = list(verdicts)

    def judge_fn(_prompt: str) -> str:
        return pending.pop(0)

    return judge_fn


def test_extract_verdict_variants():
    assert extract_verdict('{"verdict": true, "reason": "ok"}') == (True, "ok")
    assert extract_verdict('```json\n{"verdict": false, "reason": "no"}\n```') == (False, "no")
    assert extract_verdict('{"verdict": "yes", "reason": "x"}') == (True, "x")
    assert extract_verdict('prefix {"verdict": false} suffix') == (False, "")
    assert extract_verdict("no json here") == (None, "")
    assert extract_verdict(None) == (None, "")


def test_majority_all_true():
    result = majority_judge(
        task="t", criteria="c", actual="a",
        judge_fn=_seq_fn(['{"verdict": true, "reason": "r1"}'] * 3),
    )
    assert result.ok and result.valid_votes == 3
    assert result.flip is False
    assert len(result.votes) == 3


def test_judge_tokens_accumulated_from_usage_attribute():
    """judge_fn 自报 last_usage ⇒ 各票 token 之和进 JudgeResult.judge_tokens。"""
    pending = ['{"verdict": true, "reason": "r"}'] * 3

    class _Client:
        def __init__(self) -> None:
            self.last_usage: dict = {}

        def __call__(self, _prompt: str) -> str:
            self.last_usage = {"total_tokens": 7}
            return pending.pop(0)

    result = majority_judge(task="t", criteria="c", actual="a", judge_fn=_Client())
    assert result.judge_tokens == 21  # 3 票 × 7


def test_judge_tokens_zero_for_plain_function():
    result = majority_judge(
        task="t", criteria="c", actual="a",
        judge_fn=_seq_fn(['{"verdict": true, "reason": "r"}'] * 3),
    )
    assert result.judge_tokens == 0


def test_majority_two_of_three_is_flip():
    result = majority_judge(
        task="t", criteria="c", actual="a",
        judge_fn=_seq_fn([
            '{"verdict": true, "reason": "r1"}',
            '{"verdict": true, "reason": "r2"}',
            '{"verdict": false, "reason": "r3"}',
        ]),
    )
    assert result.ok is True
    assert result.flip is True  # 3 票非一致


def test_majority_one_of_three_fails():
    result = majority_judge(
        task="t", criteria="c", actual="a",
        judge_fn=_seq_fn([
            '{"verdict": true, "reason": "r1"}',
            '{"verdict": false, "reason": "r2"}',
            '{"verdict": false, "reason": "r3"}',
        ]),
    )
    assert result.ok is False and result.flip is True


def test_unparseable_vote_is_recorded_and_excluded():
    result = majority_judge(
        task="t", criteria="c", actual="a",
        judge_fn=_seq_fn([
            '{"verdict": true, "reason": "r1"}',
            "not json",
            '{"verdict": true, "reason": "r3"}',
        ]),
    )
    assert result.valid_votes == 2
    assert result.ok is True
    assert result.flip is True  # 有废票 => 非一致
    errors = [vote["error"] for vote in result.votes]
    assert any(error for error in errors)


def test_all_unparseable_yields_error_and_no_ok():
    result = majority_judge(
        task="t", criteria="c", actual="a",
        judge_fn=_seq_fn(["nope", "nope", "nope"]),
    )
    assert result.ok is False
    assert result.valid_votes == 0
    assert result.error is not None


def test_judge_fn_exception_recorded_per_vote():
    calls = {"n": 0}

    def judge_fn(_prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return '{"verdict": true, "reason": "r"}'

    result = majority_judge(task="t", criteria="c", actual="a", judge_fn=judge_fn)
    assert result.votes[0]["error"] is not None
    assert result.valid_votes == 2
    assert result.ok is True
