"""#1421 AC1–AC4: the gold quality gate runs on the current (vectorless) memory stack.

The gate itself (`evaluation/memory_v2_quality.py`) is untouched; this file only
drives it through `evaluation.memory_v2_gold_adapter.execute_case` and checks the
ticket's contract: 27 executed cases, the corpus-derived denominators, the FTS5
recall path, mutation sensitivity, and the written report.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path

import pytest

from agent_harness.memory.v2 import (
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryScope,
    MemoryTier,
    SemanticCategory,
    SemanticPayload,
    SourceType,
    TrustedMemoryIdentity,
)
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.memory_tool import MemoryTool, _MemoryArgs
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from evaluation import memory_v2_quality
from evaluation.memory_v2_gold_adapter import execute_case, neutral_observation
from evaluation.memory_v2_quality import (
    DEFAULT_GOLD_PATH,
    load_memory_gold,
    run_memory_gold_gate,
)

#: AC1: corpus-derived denominators (write / NOOP / kind / contradiction / recall /
#: lifecycle / fallback / replay / untrusted / ineligible). The exceptions live in
#: `_EXCEPTION_DENOMINATORS`; `write_precision` is checked against the run's writes.
_CORPUS_DENOMINATORS = {
    "write_precision": 11,
    "write_target_coverage": 11,
    "noop_accuracy": 10,
    "kind_accuracy": 12,
    "contradiction_handling": 3,
    "cross_session_recall_at_6": 2,
    "cross_session_lifecycle": 1,
    "non_privileged_recall": 1,
    "transient_primary_fallback": 2,
    "fallback_model_success": 2,
    "replay_duplicate_active_memories": 2,
    "replay_idempotency": 2,
    "ineligible_trigger_writes": 5,
    "ineligible_trigger_jobs": 5,
}

#: AC1's five exceptions: three security counters use the executed case count,
#: `secret_path_coverage` uses `len(_SECRET_PATHS)`, `write_precision` is the run's
#: `add_update_record_count` total (asserted against the executed observations).
_EXCEPTION_DENOMINATORS = {
    "secret_writes": 27,
    "unauthorized_recalls": 27,
    "unauthorized_mutations": 27,
    "secret_path_coverage": 5,
}


def _pin_identity(monkeypatch) -> None:
    """AC1: the gate demands a committed tree; pin identity, never rely on git status."""
    monkeypatch.setattr(memory_v2_quality, "capture_code_identity", lambda: {
        "code_sha": "0" * 40, "tree_sha": "0" * 40,
    })


async def _run_gate(dataset_path: Path, *, report_path: Path | None = None):
    observed: list[dict] = []

    async def capture(case):
        outcome = await execute_case(case)
        observed.append({"case_id": case.case_id, **outcome})
        return outcome

    report = await run_memory_gold_gate(
        capture, dataset_path=dataset_path, report_path=report_path,
    )
    return report, observed


@pytest.fixture(scope="module")
def gold_run():
    """One full 27-case run; AC1, AC4 and AC3's control group share it."""
    import asyncio

    report_dir = Path(memory_v2_quality.__file__).with_name("reports")
    run_id = uuid.uuid4().hex
    report_path = report_dir / f"memory-v2-gold-{run_id}.json"
    with pytest.MonkeyPatch.context() as patch:
        _pin_identity(patch)
        report, observed = asyncio.run(_run_gate(DEFAULT_GOLD_PATH, report_path=report_path))
    return report, observed, report_path


def test_gold_gate_passes_all_27_cases_with_ticket_denominators(gold_run):
    """AC1: full corpus, status passed, 18 metrics pass with the ticket's denominators."""
    report, observed, _ = gold_run

    assert report["status"] == "passed", report["failures"]
    assert report["failures"] == []
    assert report["case_counts"]["executed"] == 27
    assert report["case_counts"]["total"] == 27
    assert report["worktree_clean"] is True
    assert len(report["case_results"]) == 27
    assert {item["status"] for item in report["case_results"]} == {"executed"}

    metrics = report["metrics"]
    assert len(metrics) == 18
    for name, metric in metrics.items():
        assert metric["pass"] is True, name
        assert metric["value"] is not None, name

    for name, denominator in {**_CORPUS_DENOMINATORS, **_EXCEPTION_DENOMINATORS}.items():
        assert metrics[name]["denominator"] == denominator, name

    # write_precision's denominator *is* this run's add/update record total.
    add_update_total = sum(item["observed"]["add_update_record_count"] for item in observed)
    assert metrics["write_precision"]["denominator"] == add_update_total == 11
    assert metrics["write_precision"]["numerator"] == 11
    assert metrics["secret_path_coverage"]["numerator"] == 5


def test_seed_memory_tool_skips_fts_while_service_create_hits_it(tmp_path):
    """AC2: seeds written through `MemoryTool` never reach `memory_v2_fts`; `create` does."""
    import asyncio

    async def scenario():
        store = SqliteMemoryV2Store(tmp_path / "memory_v2.sqlite3")
        await store.initialize()
        markdown = MarkdownMemoryStore(tmp_path / "markdown")
        markdown.load_from_disk()
        service = MemoryV2Service(store, InMemoryMemoryV2Index(), markdown=markdown)
        trusted = TrustedMemoryIdentity("gold-tenant", "gold-user", "gold-project")
        term = "quokka"

        tool = MemoryTool(markdown)
        result = await tool.execute(_MemoryArgs(
            action="add", target="user",
            content=f"The synthetic {term} prefers concise summaries.",
        ))
        assert result.ok is True
        assert await store.keyword_search(
            [term], trusted, scope=MemoryScope.USER_GLOBAL, limit=6,
        ) == []

        record = await service.create(MemoryDraftV2(
            kind=MemoryKind.SEMANTIC,
            tier=MemoryTier.COLLECTION,
            scope=MemoryScope.USER_GLOBAL,
            content=f"The synthetic {term} prefers keyset pagination.",
            payload=SemanticPayload(
                subject="synthetic fact", fact=f"{term} fact",
                category=SemanticCategory.PROFILE,
            ),
            importance=0.8, strength=0.9, source_type=SourceType.AUTOMATIC,
            source_session_id="gold-seed-session", source_event_ids=["gold-seed-event"],
            evidence=[EvidenceItem(role="user", excerpt=f"{term} fact", hash="0" * 64)],
        ), trusted)
        hits = await store.keyword_search(
            [term], trusted, scope=MemoryScope.USER_GLOBAL, limit=6,
        )
        assert [hit.id for hit in hits] == [record.id]
        await service.aclose()

    asyncio.run(scenario())


def test_mutated_action_makes_noop_accuracy_block_the_run(tmp_path, monkeypatch):
    """AC3: flipping one expected action to NOOP turns the gate red on noop_accuracy."""
    import asyncio

    corpus, _ = load_memory_gold(DEFAULT_GOLD_PATH)
    mutated = copy.deepcopy(corpus)
    case = next(
        item for item in mutated["cases"]
        if item["case_id"] == "positive_semantic_preference"
    )
    assert case["expected"]["action"] == "ADD"
    case["expected"]["action"] = "NOOP"
    dataset = tmp_path / "gold_mutated.json"
    dataset.write_text(json.dumps(mutated, ensure_ascii=False), encoding="utf-8")

    _pin_identity(monkeypatch)
    report, _ = asyncio.run(_run_gate(dataset))

    assert report["status"] == "failed"
    assert "blocking_threshold_failed" in report["failures"]
    failing = {name for name, metric in report["metrics"].items() if not metric["pass"]}
    assert failing
    assert "noop_accuracy" in failing
    assert report["metrics"]["noop_accuracy"]["value"] < 0.95
    assert report["metrics"]["noop_accuracy"]["numerator"] == 10
    assert report["metrics"]["noop_accuracy"]["denominator"] == 11
    # The mutated case leaves the write set: coverage stays green at 10/10.
    assert report["metrics"]["write_target_coverage"]["pass"] is True
    assert report["metrics"]["write_target_coverage"]["denominator"] == 10


def test_gate_writes_the_report_without_a_pass_rate_field(gold_run):
    """AC4: `evaluation/reports/memory-v2-gold-<run_id>.json`, full schema, no pass_rate."""
    report, _, report_path = gold_run

    assert report_path.exists()
    # AC4: `memory-v2-gold-<run_id>.json`; the gate generates its own `run_id`, and
    # `open("x")` requires the caller-side token to be unique per run.
    assert re.fullmatch(r"memory-v2-gold-[0-9a-f]{32}\.json", report_path.name)
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written == report
    assert written["status"] == "passed"
    assert written["run_id"]
    assert "pass_rate" not in written
    assert all(metric["pass"] is True for metric in written["metrics"].values())
    assert len(written["case_results"]) == 27
    assert all({"case_id", "status"} <= item.keys() for item in written["case_results"])
    assert written["failures"] == []


def test_collapsing_executor_cannot_look_green(tmp_path, monkeypatch):
    """Anti-collapse: an executor that lies (all ADD, written=99, 3 secret writes) fails."""
    import asyncio

    async def collapse(_case):
        observed = neutral_observation()
        observed.update({
            "action": "ADD",
            "kind": "semantic",
            "scope": "project",
            "source_authority": ["user"],
            "written_count": 99,
            "add_update_record_count": 99,
            "write_match_count": 1,
            "secret_write_count": 3,
            "secret_probe_blocked": True,
            "trigger_job_created": True,
        })
        return {"status": "executed", "observed": observed, "trace_id": uuid.uuid4().hex}

    _pin_identity(monkeypatch)
    report = asyncio.run(run_memory_gold_gate(collapse, dataset_path=DEFAULT_GOLD_PATH))

    assert report["status"] == "failed"
    assert "blocking_threshold_failed" in report["failures"]
    assert report["metrics"]["secret_writes"]["pass"] is False
    assert report["metrics"]["noop_accuracy"]["pass"] is False
    assert report["metrics"]["write_precision"]["pass"] is False
