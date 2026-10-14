"""real_eval 运行器（issue #1005）：config_hash / 续跑去重 / infra 重试 / 预算 / 全流程。

全部用 fake 模型（ScriptedModel）+ fake judge_fn，不触网。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_harness.model.scripted import ScriptedModel
from evaluation.real_eval.dataset import DATASETS_DIR
from evaluation.real_eval.runner import (
    RunConfig,
    _is_infra,
    config_hash,
    read_records,
    run_baseline,
)


def _tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _scripted_sut(case, _config):
    """按 case_type 造确定性剧本（生产 SUT 的 fake 替身）。"""
    if case.case_type == "tool_selection":
        return ScriptedModel([
            _tool_call("add", {"first_number": 3, "second_number": 5}, "c1"),
            AIMessage(content="8"),
        ])
    if case.case_type == "recovery":
        responses = [
            _tool_call("add", {"first_number": 1, "second_number": 2}, f"c{i}")
            for i in range(1, 4)
        ]
        responses.append(_tool_call("add", {"first_number": 2, "second_number": 1}, "c4"))
        responses.append(AIMessage(content="3"))
        return ScriptedModel(responses)
    if case.case_type == "kill_resume":
        return ScriptedModel([AIMessage(content="confirmed")])
    return ScriptedModel([AIMessage(content="ok")])


def _judge_fn(_prompt: str) -> str:
    return '{"verdict": true, "reason": "ok"}'


def test_config_hash_is_deterministic_and_sensitive():
    config = RunConfig.for_baseline("A")
    assert config_hash(config) == config_hash(RunConfig.for_baseline("A"))
    changed = RunConfig.for_baseline("A", max_tokens=2048)
    assert config_hash(config) != config_hash(changed)
    assert config_hash(RunConfig.for_baseline("B")) != config_hash(config)


def test_unknown_baseline_raises():
    with pytest.raises(ValueError, match="未知 baseline"):
        RunConfig.for_baseline("nope")


def test_is_infra_classification():
    assert _is_infra({"http_status": 429})
    assert _is_infra({"http_status": 503})
    assert _is_infra({"reason": "APIConnectionError"})
    assert _is_infra({"reason": "provider_account_unavailable"})
    assert _is_infra({"message": "无法连接模型供应商"})
    assert not _is_infra({"reason": "identical_tool_failure_loop"})
    assert not _is_infra({"http_status": 400})


def test_full_offline_run_over_real_dataset(tmp_path: Path):
    out = tmp_path / "run.jsonl"
    summary = run_baseline(
        RunConfig.for_baseline("A", reps=1),
        out_path=out, dataset_dir=DATASETS_DIR,
        sut_factory=_scripted_sut, judge_fn=_judge_fn,
    )
    assert summary["written"] == 34
    assert summary["skipped"] == 0
    assert summary["budget_stop"] is False
    assert summary["planned_total"] == 34
    assert summary["completion_ratio"] == 1.0
    records = read_records(out)
    assert len(records) == 34
    assert all(record["outcome"] == "ok" for record in records), [
        (r["case_name"], r["outcome"], r["error"]) for r in records if r["outcome"] != "ok"
    ]
    first = records[0]
    for key in (
        "run_id", "config_hash", "baseline", "case_name", "rep", "split",
        "assertion_kind", "ok", "outcome", "metrics", "judge", "usage",
        "latency_ms", "attempts", "error", "timestamp",
    ):
        assert key in first
    # 首行是运行元数据 header（read_records 已跳过）
    header = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert header["record_type"] == "header"
    assert header["config_hash"] == summary["config_hash"]
    assert header["planned_total"] == 34


def test_resume_dedup_and_partial_file(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    dataset_dir.mkdir()
    cases = [
        {"name": "ts_x", "task": "计算 1+1，用 add 工具。", "case_type": "tool_selection",
         "assertion": {"kind": "D"}, "expected": {"tools": ["add"]}, "split": "train",
         "tags": [], "source": "test"},
        {"name": "ts_y", "task": "计算 2+2，用 add 工具。", "case_type": "tool_selection",
         "assertion": {"kind": "D"}, "expected": {"tools": ["add"]}, "split": "train",
         "tags": [], "source": "test"},
    ]
    (dataset_dir / "train.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "r.jsonl"
    config = RunConfig.for_baseline("A", reps=1)

    first = run_baseline(config, out_path=out, dataset_dir=dataset_dir,
                         sut_factory=_scripted_sut, judge_fn=_judge_fn)
    assert first["written"] == 2

    second = run_baseline(config, out_path=out, dataset_dir=dataset_dir,
                          sut_factory=_scripted_sut, judge_fn=_judge_fn)
    assert second["written"] == 0 and second["skipped"] == 2

    # 模拟半截文件：只留 header + 第一条记录，续跑补齐缺的那条
    lines = out.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["record_type"] == "header"
    out.write_text(lines[0] + "\n" + lines[1] + "\n", encoding="utf-8")
    third = run_baseline(config, out_path=out, dataset_dir=dataset_dir,
                         sut_factory=_scripted_sut, judge_fn=_judge_fn)
    assert third["written"] == 1 and third["skipped"] == 1
    assert len(read_records(out)) == 2


def test_resume_rejects_mismatched_config(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    dataset_dir.mkdir()
    (dataset_dir / "train.jsonl").write_text(
        json.dumps({
            "name": "ts_x", "task": "x", "case_type": "tool_selection",
            "assertion": {"kind": "D"}, "expected": {"tools": ["add"]},
            "split": "train", "tags": [], "source": "test",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "r.jsonl"
    run_baseline(RunConfig.for_baseline("A", reps=1), out_path=out,
                 dataset_dir=dataset_dir, sut_factory=_scripted_sut, judge_fn=_judge_fn)
    # 换配置（max_tokens 变了 ⇒ config_hash 变了）续跑同文件：响亮拒绝，不静默混树
    with pytest.raises(ValueError, match="config_hash"):
        run_baseline(RunConfig.for_baseline("A", reps=1, max_tokens=2048),
                     out_path=out, dataset_dir=dataset_dir,
                     sut_factory=_scripted_sut, judge_fn=_judge_fn)


def test_read_records_missing_file_returns_empty(tmp_path: Path):
    assert read_records(tmp_path / "nope.jsonl") == []


def test_read_records_tolerates_truncated_tail(tmp_path: Path):
    out = tmp_path / "r.jsonl"
    good = json.dumps({"config_hash": "h", "case_name": "a", "rep": 1,
                       "outcome": "ok", "ok": True}, ensure_ascii=False)
    out.write_text(good + "\n" + '{"case_name":"b","rep":1,"ok":tru', encoding="utf-8")
    records = read_records(out)
    assert len(records) == 1
    assert records[0]["case_name"] == "a"


def test_read_records_keeps_last_for_duplicate_key(tmp_path: Path):
    out = tmp_path / "r.jsonl"
    infra = json.dumps({"config_hash": "h", "case_name": "a", "rep": 1,
                        "outcome": "infra", "ok": False}, ensure_ascii=False)
    ok = json.dumps({"config_hash": "h", "case_name": "a", "rep": 1,
                     "outcome": "ok", "ok": True}, ensure_ascii=False)
    out.write_text(infra + "\n" + ok + "\n", encoding="utf-8")
    records = read_records(out)
    assert len(records) == 1
    assert records[0]["outcome"] == "ok"


def test_infra_retry_then_record(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    dataset_dir.mkdir()
    (dataset_dir / "train.jsonl").write_text(
        json.dumps({
            "name": "ts_z", "task": "x", "case_type": "tool_selection",
            "assertion": {"kind": "D"}, "expected": {"tools": ["add"]},
            "split": "train", "tags": [], "source": "test",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    def boom_sut(_case, _config):
        raise RuntimeError("connection refused")

    out = tmp_path / "r.jsonl"
    summary = run_baseline(
        RunConfig.for_baseline("A", reps=1), out_path=out, dataset_dir=dataset_dir,
        sut_factory=boom_sut, judge_fn=_judge_fn, max_retries=2,
    )
    assert summary["written"] == 1
    record = read_records(out)[0]
    assert record["outcome"] == "infra"
    assert record["attempts"] == 3  # 初次 + 2 次重试
    assert "connection refused" in record["error"]


def test_non_infra_exception_records_fail_without_retry(tmp_path: Path):
    """非 provider 异常（harness bug）⇒ fail、不重试、不排除出分母。"""
    dataset_dir = tmp_path / "ds"
    dataset_dir.mkdir()
    (dataset_dir / "train.jsonl").write_text(
        json.dumps({
            "name": "ts_z", "task": "x", "case_type": "tool_selection",
            "assertion": {"kind": "D"}, "expected": {"tools": ["add"]},
            "split": "train", "tags": [], "source": "test",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    def broken_sut(_case, _config):
        raise KeyError("missing 'tools' key in harness glue")

    out = tmp_path / "r.jsonl"
    run_baseline(
        RunConfig.for_baseline("A", reps=1), out_path=out, dataset_dir=dataset_dir,
        sut_factory=broken_sut, judge_fn=_judge_fn, max_retries=2,
    )
    record = read_records(out)[0]
    assert record["outcome"] == "fail"
    assert record["attempts"] == 1  # 非 infra 不重试


def test_infra_records_are_retried_on_resume(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    dataset_dir.mkdir()
    (dataset_dir / "train.jsonl").write_text(
        json.dumps({
            "name": "ts_z", "task": "x", "case_type": "tool_selection",
            "assertion": {"kind": "D"}, "expected": {"tools": ["add"]},
            "split": "train", "tags": [], "source": "test",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "r.jsonl"
    config = RunConfig.for_baseline("A", reps=1)

    def boom(_case, _config):
        raise RuntimeError("connection refused")

    first = run_baseline(config, out_path=out, dataset_dir=dataset_dir,
                         sut_factory=boom, judge_fn=_judge_fn, max_retries=0)
    assert first["written"] == 1 and first["skipped"] == 0
    # infra 记录不冻结：同命令续跑会重试（拿到健康的 SUT 即转 ok）
    second = run_baseline(config, out_path=out, dataset_dir=dataset_dir,
                          sut_factory=_scripted_sut, judge_fn=_judge_fn)
    assert second["written"] == 1 and second["skipped"] == 0
    assert read_records(out)[0]["outcome"] == "ok"


def test_budget_stop(tmp_path: Path):
    out = tmp_path / "r.jsonl"
    summary = run_baseline(
        RunConfig.for_baseline("A", reps=1, budget_seconds=-1),
        out_path=out, dataset_dir=DATASETS_DIR,
        sut_factory=_scripted_sut, judge_fn=_judge_fn,
    )
    assert summary["budget_stop"] is True
    assert summary["written"] == 0


def _single_open_qa_dataset(tmp_path: Path, *, votes: int = 3) -> Path:
    dataset_dir = tmp_path / "ds"
    dataset_dir.mkdir()
    (dataset_dir / "train.jsonl").write_text(
        json.dumps({
            "name": "oa_x", "task": "t", "case_type": "open_qa",
            "assertion": {"kind": "J", "votes": votes}, "expected": {},
            "judge_criteria": "c", "split": "train", "tags": [], "source": "s",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return dataset_dir


class _UsageJudge:
    """judge_fn 替身：每次调用上报固定 token 用量。"""

    def __init__(self, tokens: int = 5) -> None:
        self._tokens = tokens
        self.last_usage: dict = {}

    def __call__(self, _prompt: str) -> str:
        self.last_usage = {"total_tokens": self._tokens}
        return '{"verdict": true, "reason": "r"}'


def test_judge_usage_counted_into_record_and_budget(tmp_path: Path):
    dataset_dir = _single_open_qa_dataset(tmp_path, votes=3)
    out = tmp_path / "r.jsonl"
    summary = run_baseline(
        RunConfig.for_baseline("A", reps=1), out_path=out, dataset_dir=dataset_dir,
        sut_factory=_scripted_sut, judge_fn=_UsageJudge(tokens=5),
    )
    record = read_records(out)[0]
    assert record["usage"]["judge_tokens"] == 15  # 3 票 × 5
    assert record["usage"]["total_tokens"] == 15  # judge 用量计入 total
    assert summary["total_tokens"] == 15


def test_judge_votes_read_from_assertion(tmp_path: Path):
    dataset_dir = _single_open_qa_dataset(tmp_path, votes=5)
    out = tmp_path / "r.jsonl"
    run_baseline(
        RunConfig.for_baseline("A", reps=1), out_path=out, dataset_dir=dataset_dir,
        sut_factory=_scripted_sut, judge_fn=_UsageJudge(),
    )
    record = read_records(out)[0]
    assert len(record["judge"]["votes"]) == 5


def test_budget_counts_existing_records_on_resume(tmp_path: Path):
    dataset_dir = tmp_path / "ds"
    dataset_dir.mkdir()
    rows = [
        {"name": "ts_a", "task": "a", "case_type": "tool_selection",
         "assertion": {"kind": "D"}, "expected": {"tools": ["add"]},
         "split": "train", "tags": [], "source": "s"},
        {"name": "ts_b", "task": "b", "case_type": "tool_selection",
         "assertion": {"kind": "D"}, "expected": {"tools": ["add"]},
         "split": "train", "tags": [], "source": "s"},
    ]
    (dataset_dir / "train.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    config = RunConfig.for_baseline("A", reps=1, budget_tokens=1)
    ch = config_hash(config, dataset_dir=dataset_dir)
    out = tmp_path / "r.jsonl"
    header = {"record_type": "header", "config_hash": ch}
    done = {
        "config_hash": ch, "case_name": "ts_a", "rep": 1, "split": "train",
        "assertion_kind": "D", "case_type": "tool_selection", "tags": [],
        "ok": True, "outcome": "ok", "metrics": {}, "judge": None,
        "usage": {"total_tokens": 1_000_000}, "latency_ms": 1, "attempts": 1,
        "error": None,
    }
    out.write_text(
        json.dumps(header, ensure_ascii=False) + "\n"
        + json.dumps(done, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    summary = run_baseline(config, out_path=out, dataset_dir=dataset_dir,
                           sut_factory=_scripted_sut, judge_fn=_judge_fn)
    # 既有记录的 tokens 计入累计 ⇒ 预算立即超标，不再跑
    assert summary["budget_stop"] is True
    assert summary["written"] == 0
    assert summary["skipped"] == 1
