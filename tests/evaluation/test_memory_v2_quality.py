from __future__ import annotations

import json

import pytest

from evaluation import memory_v2_quality
from evaluation.memory_v2_quality import (
    evaluate_memory_gold,
    load_memory_gold,
    run_memory_gold_gate,
)


def _observed(case):
    expected = case.expected
    action = expected["action"]
    return {
        "eligibility": expected["eligibility"],
        "action": action,
        "kind": expected["kind"],
        "scope": expected["scope"],
        "source_authority": expected["source_authority"],
        "recall_ids_top6": expected["recall_target"],
        "prohibited_outcomes": [],
        "secret_write_count": 0,
        "unauthorized_recall_count": 0,
        "unauthorized_mutation_count": 0,
        "ineligible_trigger_write_count": 0,
        "written_count": int(action in {"ADD", "UPDATE"}),
        "fallback_used": expected.get("requires_fallback", False),
        "degraded_without_write": False,
        "old_version_superseded": expected.get("supersedes_old_version", False),
        "duplicate_active_logical_memories": 0,
        "latency_ms": 20,
        "input_tokens": 32,
        "output_tokens": 8,
        "cost_usd": None,
    }


def _results(cases):
    return [
        {"case_id": case.case_id, "status": "executed", "observed": _observed(case)}
        for case in cases
    ]


def test_frozen_gold_declares_expected_contract_and_is_synthetic():
    corpus, cases = load_memory_gold()

    assert corpus["synthetic"] is True
    assert corpus["version"] == "1.0.0"
    assert len(cases) >= 15
    for case in cases:
        assert {
            "eligibility", "action", "kind", "scope", "source_authority",
            "recall_target", "prohibited_outcomes",
        } <= case.expected.keys()


def test_frozen_gold_passes_all_blocking_metrics_with_complete_measurements():
    corpus, cases = load_memory_gold()

    report = evaluate_memory_gold(
        corpus, cases, _results(cases), code_sha="code", tree_sha="tree",
        config_aliases={"primary": "memory.primary", "fallback": "memory.fallback"},
    )

    assert report["status"] == "passed"
    assert report["case_counts"]["executed"] == len(cases)
    assert report["metrics"]["cross_session_recall_at_6"]["numerator"] == 2
    assert report["metrics"]["transient_primary_fallback"]["value"] == 1
    assert report["usage"] == {
        "latency_ms_total": 20 * len(cases),
        "input_tokens": 32 * len(cases),
        "output_tokens": 8 * len(cases),
        "cost_usd": None,
    }
    assert "synthetic_input" not in json.dumps(report)


@pytest.mark.parametrize(
    ("case_id", "field", "value", "gate"),
    [
        ("secret_probe", "secret_write_count", 1, "secret_writes"),
        ("wrong_project_isolation", "unauthorized_recall_count", 1, "unauthorized_recalls"),
        ("wrong_project_isolation", "unauthorized_mutation_count", 1, "unauthorized_mutations"),
        ("explicit_opt_out", "ineligible_trigger_write_count", 1, "ineligible_trigger_writes"),
        ("transient_noop", "action", "ADD", "noop_accuracy"),
        ("transient_noop", "action", "ADD", "write_precision"),
        ("positive_semantic_preference", "written_count", 0, "write_precision"),
        ("positive_semantic_preference", "kind", "episodic", "kind_accuracy"),
        ("contradiction_supersession", "old_version_superseded", False,
         "contradiction_handling"),
        ("cross_session_recall_one", "recall_ids_top6", [], "cross_session_recall_at_6"),
        ("primary_transient_fallback", "fallback_used", False,
         "transient_primary_fallback"),
        ("replay_committed_job", "duplicate_active_logical_memories", 1,
         "replay_duplicate_active_memories"),
    ],
)
def test_each_quality_mutation_fails_its_blocking_gate(case_id, field, value, gate):
    corpus, cases = load_memory_gold()
    results = _results(cases)
    target = next(item for item in results if item["case_id"] == case_id)
    target["observed"][field] = value
    if field in {"secret_write_count", "unauthorized_recall_count",
                 "unauthorized_mutation_count"}:
        outcome = {
            "secret_write_count": "secret_write",
            "unauthorized_recall_count": "unauthorized_recall",
            "unauthorized_mutation_count": "unauthorized_mutation",
        }[field]
        target["observed"]["prohibited_outcomes"].append(outcome)

    report = evaluate_memory_gold(corpus, cases, results)

    assert report["status"] == "failed"
    assert report["metrics"][gate]["pass"] is False


def test_failed_skipped_unawaited_zero_and_duplicate_runs_never_pass():
    corpus, cases = load_memory_gold()
    skipped = [{"case_id": case.case_id, "status": "skipped"} for case in cases]

    report = evaluate_memory_gold(corpus, cases, skipped)
    assert report["status"] == "failed"
    assert "zero_executed_cases" in report["failures"]
    assert report["case_counts"]["skipped"] == len(cases)

    duplicated = _results(cases)
    duplicated[0]["trace_id"] = "same-trace"
    duplicated[1]["trace_id"] = "same-trace"
    duplicate_report = evaluate_memory_gold(corpus, cases, duplicated)
    assert duplicate_report["status"] == "failed"
    assert "duplicate_trace_ids" in duplicate_report["failures"]

    unawaited = _results(cases)
    unawaited[0]["status"] = "unawaited"
    assert evaluate_memory_gold(corpus, cases, unawaited)["status"] == "failed"


@pytest.mark.asyncio
async def test_runner_awaits_every_case_and_never_reports_skips_as_green(
    tmp_path, monkeypatch,
):
    _corpus, cases = load_memory_gold()
    awaited: list[str] = []
    monkeypatch.setattr(memory_v2_quality, "capture_code_identity", lambda: {
        "code_sha": "a" * 40, "tree_sha": "b" * 40,
    })

    async def execute(case):
        awaited.append(case.case_id)
        return {"status": "skipped", "observed": None}

    report_path = tmp_path / "run.json"
    report = await run_memory_gold_gate(
        execute, report_path=report_path,
        config_aliases={"primary": "memory.primary"},
    )

    assert awaited == [case.case_id for case in cases]
    assert report["status"] == "failed"
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["worktree_clean"] is True
    assert saved["config_aliases"] == {"primary": "memory.primary"}
    assert "synthetic_input" not in report_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_runner_fails_if_worktree_identity_changes_during_run(tmp_path, monkeypatch):
    _corpus, _cases = load_memory_gold()
    identities = iter((
        {"code_sha": "a" * 40, "tree_sha": "b" * 40},
        {"code_sha": "c" * 40, "tree_sha": "d" * 40},
    ))
    monkeypatch.setattr(
        memory_v2_quality, "capture_code_identity", lambda: next(identities),
    )

    report = await run_memory_gold_gate(
        lambda case: {"status": "skipped", "observed": None},
        report_path=tmp_path / "changed-tree.json",
    )

    assert report["code_sha"] == "a" * 40
    assert report["tree_sha"] == "b" * 40
    assert report["worktree_clean"] is False
    assert "worktree_identity_changed_or_unverified" in report["failures"]
    assert report["status"] == "failed"


def test_gold_rejects_duplicate_ids_and_non_synthetic_data(tmp_path):
    corpus, _cases = load_memory_gold()
    corpus["cases"].append(corpus["cases"][0])
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(corpus), encoding="utf-8")

    with pytest.raises(ValueError, match="unique"):
        load_memory_gold(path)

    corpus["synthetic"] = False
    path.write_text(json.dumps(corpus), encoding="utf-8")
    with pytest.raises(ValueError, match="synthetic"):
        load_memory_gold(path)


def test_quality_report_rejects_configuration_values():
    corpus, cases = load_memory_gold()

    with pytest.raises(ValueError, match="aliases only"):
        evaluate_memory_gold(
            corpus, cases, _results(cases),
            config_aliases={"primary": "https://provider.invalid/v1?token=private"},
        )
