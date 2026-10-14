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

    # 模拟半截文件：只留第一条，续跑补齐缺的那条
    lines = out.read_text(encoding="utf-8").splitlines()
    out.write_text(lines[0] + "\n", encoding="utf-8")
    third = run_baseline(config, out_path=out, dataset_dir=dataset_dir,
                         sut_factory=_scripted_sut, judge_fn=_judge_fn)
    assert third["written"] == 1 and third["skipped"] == 1
    assert len(read_records(out)) == 2


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


def test_budget_stop(tmp_path: Path):
    out = tmp_path / "r.jsonl"
    summary = run_baseline(
        RunConfig.for_baseline("A", reps=1, budget_seconds=-1),
        out_path=out, dataset_dir=DATASETS_DIR,
        sut_factory=_scripted_sut, judge_fn=_judge_fn,
    )
    assert summary["budget_stop"] is True
    assert summary["written"] == 0
