from __future__ import annotations

import json
from dataclasses import replace

import pytest

from evaluation.memory_v2_public_benchmarks import (
    load_locomo,
    load_longmemeval,
    run_public_baseline,
)


def _locomo_file(tmp_path):
    path = tmp_path / "locomo-synthetic.json"
    path.write_text(json.dumps([{
        "sample_id": "synthetic-conversation",
        "conversation": {
            "session_1_date_time": "2026-01-01",
            "session_1": [
                {"speaker": "speaker_a", "dia_id": "D1:1", "text": "synthetic session text"},
                {"speaker": "speaker_b", "dia_id": "D1:2", "text": "more synthetic text"},
            ],
            "session_2_date_time": "2026-01-02",
            "session_2": [
                {"speaker": "speaker_a", "dia_id": "D2:1", "text": "later synthetic fact"},
            ],
        },
        "qa": [{
            "question": "Which synthetic fact appeared later?",
            "answer": "later synthetic fact",
            "category": 1,
            "evidence": ["D2:1"],
        }],
    }]), encoding="utf-8")
    return path


def _longmemeval_file(tmp_path):
    path = tmp_path / "longmemeval-synthetic.json"
    path.write_text(json.dumps([{
        "question_id": "synthetic-q1",
        "question_type": "multi-session",
        "question": "What synthetic preference was recorded?",
        "answer": "short answers",
        "question_date": "2026-01-03",
        "haystack_session_ids": ["s1", "s2"],
        "haystack_dates": ["2026-01-01", "2026-01-02"],
        "haystack_sessions": [
            [{"role": "user", "content": "I prefer short answers.", "has_answer": True}],
            [{"role": "assistant", "content": "I will keep that in mind."}],
        ],
        "answer_session_ids": ["s1"],
    }]), encoding="utf-8")
    return path


def _measurement(case, **overrides):
    value = {
        "answer_correct": True,
        "retrieved_session_ids": list(case.relevant_session_ids),
        "stored_record_count": 2,
        "pollution_count": 0,
        "injected_tokens": 24,
        "latency_ms": 50,
        "input_tokens": 40,
        "output_tokens": 12,
        "cost_usd": None,
    }
    return {**value, **overrides}


def test_locomo_loader_joins_evidence_to_session_ids(tmp_path):
    cases = load_locomo(_locomo_file(tmp_path))

    assert len(cases) == 1
    assert cases[0].case_id == "synthetic-conversation-qa-0000"
    assert cases[0].relevant_session_ids == ("session_2",)
    assert cases[0].relevant_turn_ids == ("D2:1",)
    assert cases[0].sessions[0].turns[0].role == "speaker_a"


def test_longmemeval_loader_preserves_evidence_sessions_and_turns(tmp_path):
    cases = load_longmemeval(_longmemeval_file(tmp_path))

    assert len(cases) == 1
    assert cases[0].relevant_session_ids == ("s1",)
    assert cases[0].relevant_turn_ids == ("s1:0",)
    assert cases[0].sessions[1].turns[0].role == "assistant"


@pytest.mark.asyncio
async def test_public_baseline_is_content_free_nonblocking_and_freezes_first_run(tmp_path):
    path = _locomo_file(tmp_path)
    cases = load_locomo(path)
    awaited: list[str] = []

    async def execute(case):
        awaited.append(case.case_id)
        return {"observed": _measurement(case), "trace_id": f"trace-{case.case_id}"}

    first_path = tmp_path / "reports" / "first.json"
    baseline_path = tmp_path / "baseline.json"
    first = await run_public_baseline(
        "locomo", cases, execute, dataset_path=path,
        config_aliases={"primary": "memory.primary"},
        report_path=first_path, freeze_path=baseline_path,
    )

    assert awaited == [cases[0].case_id]
    assert first["status"] == "completed"
    assert first["blocking"] is False
    assert first["non_commercial_only"] is True
    assert first["baseline_frozen"] is True
    assert first["metrics"]["answer_quality"]["value"] == 1
    assert first["metrics"]["session_recall_at_6"]["value"] == 1
    serialized = first_path.read_text(encoding="utf-8")
    assert "synthetic session text" not in serialized
    assert "Which synthetic fact" not in serialized
    frozen_before = baseline_path.read_bytes()

    second = await run_public_baseline(
        "locomo", cases, execute, dataset_path=path,
        config_aliases={"primary": "memory.primary"},
        report_path=tmp_path / "reports" / "repeat.json",
        freeze_path=baseline_path, repeat_of=first["run_id"],
    )
    assert second["baseline_frozen"] is False
    assert second["repeat_of"] == first["run_id"]
    assert baseline_path.read_bytes() == frozen_before


@pytest.mark.asyncio
async def test_public_baseline_skips_and_duplicate_traces_cannot_freeze(tmp_path):
    path = _longmemeval_file(tmp_path)
    cases = load_longmemeval(path)
    cases.append(replace(cases[0], case_id="synthetic-q2"))
    baseline_path = tmp_path / "baseline.json"

    skipped = await run_public_baseline(
        "longmemeval", cases,
        lambda _case: {"status": "skipped"}, dataset_path=path,
        config_aliases={"reader": "memory.primary"},
        report_path=tmp_path / "skipped.json", freeze_path=baseline_path,
    )
    assert skipped["status"] == "failed"
    assert skipped["case_counts"]["skipped"] == 2
    assert skipped["baseline_frozen"] is False
    assert not baseline_path.exists()

    duplicate_trace = await run_public_baseline(
        "longmemeval", cases,
        lambda case: {"observed": _measurement(case), "trace_id": "same-trace"},
        dataset_path=path, config_aliases={"reader": "memory.primary"},
        report_path=tmp_path / "duplicate-trace.json", freeze_path=baseline_path,
    )
    assert duplicate_trace["status"] == "failed"
    assert "duplicate_trace_ids" in duplicate_trace["failures"]
    assert not baseline_path.exists()


@pytest.mark.asyncio
async def test_public_baseline_without_answer_quality_cannot_freeze(tmp_path):
    path = _locomo_file(tmp_path)
    cases = load_locomo(path)
    baseline_path = tmp_path / "baseline.json"

    report = await run_public_baseline(
        "locomo", cases,
        lambda case: {"observed": _measurement(case, answer_correct=None)},
        dataset_path=path, config_aliases={"reader": "memory.primary"},
        report_path=tmp_path / "missing-answer.json", freeze_path=baseline_path,
    )

    assert report["status"] == "failed"
    assert report["metrics"]["answer_quality"]["denominator"] == len(cases)
    assert report["metrics"]["answer_quality"]["value"] == 0
    assert "invalid_metric:synthetic-conversation-qa-0000:answer_correct" in report["failures"]
    assert report["baseline_frozen"] is False
    assert not baseline_path.exists()


def test_public_benchmark_loader_rejects_duplicate_question_ids(tmp_path):
    path = _longmemeval_file(tmp_path)
    content = json.loads(path.read_text(encoding="utf-8"))
    content.append(content[0])
    path.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        load_longmemeval(path)
