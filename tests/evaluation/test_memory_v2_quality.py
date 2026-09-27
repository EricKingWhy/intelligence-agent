from __future__ import annotations

import json
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from agent_harness.agent.types import STATUS_COMPLETED
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.commands import explicit_remember_matches
from agent_harness.memory.v2.eligibility import (
    MEMORY_OPT_OUT_FIELD,
    decide_run_end_eligibility,
)
from agent_harness.memory.v2.executor import MemoryJobExecutor
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.jobs import SqliteMemoryV2JobStore
from agent_harness.memory.v2.policy import resolve_evidence_source
from agent_harness.memory.v2.projection import build_formation_input
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import (
    EvidenceItem,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    SemanticCategory,
    SourceType,
    TrustedMemoryIdentity,
)
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.tooling.result import ToolResult
from evaluation import memory_v2_quality
from evaluation.memory_v2_quality import (
    GoldCase,
    evaluate_memory_gold,
    load_memory_gold,
    run_memory_gold_gate,
)
from scripts.run_memory_v2_real_gold_gate import (
    _foreign_project_identity,
    _model_execution_summary,
    _trusted_identity,
)
from tests.memory.v2._records import make_draft, payload_for
from tests.memory.v2.test_v2_executor import (
    FakeInvoker,
    _add,
    _adjudication,
    _candidate,
    _formation_candidates,
    _formation_no_memory,
    _roles,
    _update,
)

_GOLD_MODEL_OUTPUTS = {
    "positive_semantic_preference": ("semantic", "user_global", "ADD", "preference"),
    "positive_project_fact": ("semantic", "project", "ADD", "project_fact"),
    "positive_episode": ("episodic", "user_global", "ADD", "project_fact"),
    "positive_procedure": ("procedural", "project", "ADD", "project_fact"),
    "explicit_remember": ("semantic", "project", "ADD", "project_fact"),
    "transient_noop": None,
    "unsupported_assistant_claim": (
        "semantic", "user_global", "NOOP", "profile", "assistant",
    ),
    "secret_probe": ("semantic", "user_global", "NOOP", "project_fact", "user", "secret"),
    "sensitive_without_consent": (
        "semantic", "user_global", "NOOP", "project_fact", "user", "sensitive",
    ),
    "explicit_opt_out": None,
    "wrong_project_isolation": None,
    "contradiction_supersession": ("semantic", "project", "UPDATE", "project_fact"),
    "contradiction_user_wins": ("semantic", "project", "UPDATE", "project_fact"),
    "cross_session_recall_one": None,
    "cross_session_recall_two": None,
    "primary_transient_fallback": ("semantic", "project", "ADD", "project_fact"),
    "replay_committed_job": ("semantic", "project", "ADD", "project_fact"),
}
_GOLD_RECALL_LABELS = {
    "cross_session_recall_one": "gold-project-name",
    "cross_session_recall_two": "gold-pagination",
}
_GOLD_RECALL_FACTS = {
    "cross_session_recall_one": "The synthetic project is named Sample Harbor.",
    "cross_session_recall_two": "The project API lists results with cursor pagination.",
}
_GOLD_RECALL_DISTRACTORS = (
    "Project status summaries should stay concise.",
    "The release cutoff is Wednesday.",
    "Deployments require approval before production.",
    "Generated reports go to the archive bucket.",
    "A nightly job checks database backups.",
    "Support escalations go to the on-call engineer.",
    "Service authentication uses signed tokens.",
)


def _observed_source_authority(events, written, candidate_evidence):
    event_by_id = {event.event_id: event for event in events}
    if written:
        committed_source_ids = {
            event_id for record in written for event_id in record.source_event_ids
        }
        if committed_source_ids <= event_by_id.keys():
            observed_sources = {
                resolve_evidence_source(event_by_id[event_id]).value
                for event_id in committed_source_ids
            }
        else:
            observed_sources = set()
    elif candidate_evidence:
        refs = build_formation_input(events).refs
        role_by_ref = {
            ref: resolve_evidence_source(event_by_id[event_id]).value
            for ref, event_id in refs.items() if event_id in event_by_id
        }
        observed_sources = {
            role_by_ref[item["event_id"]] for item in candidate_evidence
            if item["event_id"] in role_by_ref
        }
    else:
        observed_sources = {
            resolve_evidence_source(event).value for event in events
            if event.type == USER_MESSAGE
        }
    if "user" in observed_sources:
        return ["user"]
    return sorted(observed_sources - {"tool", "other"})


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
        "fallback_success": expected.get("requires_fallback", False),
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


def _gold_payload(
    kind: str, text: str, *, category: SemanticCategory = SemanticCategory.PROJECT_FACT,
) -> dict:
    memory_kind = MemoryKind(kind)
    values = {
        MemoryKind.SEMANTIC: {
            "subject": "synthetic fact", "fact": text,
            "category": category,
        },
        MemoryKind.EPISODIC: {
            "situation": "synthetic situation", "action": text,
            "outcome": "completed", "lesson": text,
        },
        MemoryKind.PROCEDURAL: {
            "trigger": "synthetic procedure", "procedure": text,
            "success_condition": "completed successfully",
        },
    }[memory_kind]
    return payload_for(memory_kind, **values).model_dump(mode="json")


async def _execute_gold_case_with_memory_v2(case: GoldCase, database_path):
    store = SqliteMemoryV2Store(database_path)
    await store.initialize()
    jobs = SqliteMemoryV2JobStore(database_path)
    await jobs.initialize()
    index = InMemoryMemoryV2Index()
    service = MemoryV2Service(store, index)
    trusted = TrustedMemoryIdentity("gold-tenant", "gold-user", "gold-project")
    provider_output = _GOLD_MODEL_OUTPUTS[case.case_id]
    assistant_text = case.synthetic_input if case.case_id == "unsupported_assistant_claim" else "Acknowledged."
    events = [
        SessionEvent(
            event_id="gold-user-event", seq=1, type=USER_MESSAGE, session_id="gold-session",
            run_id=None, data={
                "content": (
                    "What should be retained from this synthetic conversation?"
                    if case.case_id == "unsupported_assistant_claim"
                    else case.synthetic_input
                ),
                               **({MEMORY_OPT_OUT_FIELD: True}
                                  if case.case_id == "explicit_opt_out" else {})},
        ),
        SessionEvent(
            event_id="gold-assistant-event", seq=2, type=MODEL_COMPLETED,
            session_id="gold-session", run_id="gold-run", data={"content": assistant_text},
        ),
    ]
    if provider_output is not None and provider_output[0] == "procedural":
        events.extend([
            SessionEvent(
                event_id=f"gold-tool-{index}", seq=index + 1, type=TOOL_RESULT,
                session_id="gold-session", run_id="gold-run",
                data={"tool_call_id": f"gold-call-{index}",
                      "content": ToolResult.success("done").model_dump_json()},
            )
            for index in (2, 3)
        ])

    async def seed(content: str, identity: TrustedMemoryIdentity):
        draft = make_draft(
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope.PROJECT,
            project_id=identity.project_id,
            content=content,
            payload=_gold_payload("semantic", content),
            source_type=SourceType.AUTOMATIC,
            source_session_id="gold-seed-session",
            source_event_ids=["gold-seed-event"],
            evidence=[EvidenceItem(role="user", excerpt=content[:120], hash="a" * 64)],
        )
        record = await service.create(draft, identity)
        await index.upsert(record)
        return record

    recall_labels: dict[str, str] = {}
    if case.category == "contradiction":
        previous_fact = (
            "The synthetic project codename is Amber Fox."
            if case.case_id == "contradiction_user_wins"
            else "The synthetic deploy window is Monday."
        )
        previous = await seed(previous_fact, trusted)
    elif case.category == "cross_session_recall":
        label = _GOLD_RECALL_LABELS[case.case_id]
        record = await seed(_GOLD_RECALL_FACTS[case.case_id], trusted)
        recall_labels[record.id] = label
        for distractor in _GOLD_RECALL_DISTRACTORS:
            await seed(distractor, trusted)
    elif case.case_id == "wrong_project_isolation":
        foreign = TrustedMemoryIdentity("gold-tenant", "gold-user", "other-project")
        await seed(case.synthetic_input, foreign)
    else:
        previous = None

    eligibility = decide_run_end_eligibility(
        terminal_status=STATUS_COMPLETED, events=events,
    )
    observer_rows: list[tuple[str, dict]] = []
    result = None
    invoker = None
    written = ()
    candidate_evidence: list[dict] = []
    candidate_content = (
        case.synthetic_input.removeprefix("Remember that ").rstrip(".")
        if case.category == "explicit_command" else case.synthetic_input
    )
    if case.case_id == "contradiction_user_wins":
        candidate_content = "The synthetic project codename is Cedar Lantern."
    if eligibility.eligible:
        if provider_output is None:
            formation = _formation_no_memory()
            adjudications = []
        else:
            kind, scope, action, category, *extras = provider_output
            evidence_role = extras[0] if extras and extras[0] in {"user", "assistant"} else "user"
            sensitivity = extras[1] if evidence_role == "user" and len(extras) > 1 else (
                extras[0] if extras and extras[0] not in {"user", "assistant"} else None
            )
            project_id = trusted.project_id if scope == "project" else None
            evidence_alias = "e2" if evidence_role == "assistant" else "e1"
            evidence = [{"event_id": evidence_alias, "role": evidence_role,
                         "excerpt": candidate_content}]
            if kind == "procedural":
                evidence.extend([
                    {"event_id": "e3", "role": "tool", "excerpt": "first successful action"},
                    {"event_id": "e4", "role": "tool", "excerpt": "second successful action"},
                ])
            candidate_evidence = evidence
            candidate = _candidate(
                kind=kind, scope=scope, project_id=project_id,
                content=candidate_content,
                payload=_gold_payload(
                    kind, candidate_content, category=SemanticCategory(category),
                ),
                evidence=evidence,
            )
            if sensitivity is not None:
                candidate["sensitivity"] = sensitivity
                if sensitivity == "sensitive":
                    candidate["sensitive_category"] = "health"
            formation = _formation_candidates(candidate)
            if action == "UPDATE":
                assert previous is not None
                adjudications = [_adjudication(_update(previous.id, **candidate))]
            elif action == "ADD":
                adjudications = [_adjudication(_add(**candidate))]
            else:
                adjudications = []
        if case.case_id == "primary_transient_fallback":
            invoker = FakeInvoker(
                formation=[TimeoutError(), TimeoutError(), TimeoutError(), formation],
                adjudication=adjudications,
            )
        else:
            invoker = FakeInvoker(formation=[formation], adjudication=adjudications)
        await jobs.enqueue(
            idempotency_key=f"gold-{case.case_id}", trusted=trusted,
            session_id="gold-session", run_id="gold-run",
        )
        claimed = await jobs.claim(worker_id="gold-worker")
        assert claimed is not None
        executor = MemoryJobExecutor(
            jobs=jobs, writer=service, searcher=service, invoker=invoker,
            observer=lambda name, metadata: observer_rows.append((name, metadata)),
        )
        started = time.monotonic()
        result = await executor.run(
            claimed, worker_id="gold-worker", run_events=events,
            roles=_roles(fallback=case.case_id == "primary_transient_fallback"),
            explicit_remember=explicit_remember_matches(
                case.synthetic_input, candidate_content,
            ),
        )
        if case.category == "replay":
            replay = await executor.run(
                claimed, worker_id="gold-worker", run_events=events, roles=_roles(),
            )
            assert replay is not None and replay.written == ()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        written = result.written if result is not None else ()
    else:
        elapsed_ms = 0

    active = await store.list_active(
        trusted, scope=MemoryScope.USER_GLOBAL, limit=100,
    )
    active += await store.list_active(trusted, scope=MemoryScope.PROJECT, limit=100)
    if case.case_id == "contradiction_user_wins":
        project_texts = [record.content for record in active
                         if record.scope is MemoryScope.PROJECT]
        assert any("Cedar Lantern" in content for content in project_texts)
        assert all("Amber Fox" not in content for content in project_texts)
    superseded = await service.list_records(
        trusted, status=MemoryStatus.SUPERSEDED, limit=100,
    )
    recall_ids: list[str] = []
    unauthorized_recall_count = 0
    unauthorized_mutation_count = 0
    if case.category == "cross_session_recall":
        hits = await service.hybrid_search(
            case.synthetic_input, trusted,
            scopes=[MemoryScope.PROJECT], limit=6,
        )
        recall_ids = [
            recall_labels[hit.record.id] for hit in hits if hit.record.id in recall_labels
        ]
    elif case.case_id == "wrong_project_isolation":
        hits = await service.search(
            case.synthetic_input, trusted, scope=MemoryScope.PROJECT, limit=6,
        )
        unauthorized_recall_count = len(hits)
        foreign_draft = make_draft(
            scope=MemoryScope.PROJECT, project_id="other-project",
            content="forbidden synthetic mutation", payload=_gold_payload(
                "semantic", "forbidden synthetic mutation",
            ),
        )
        try:
            await service.create(foreign_draft, trusted)
        except PermissionError:
            pass
        else:
            unauthorized_mutation_count = 1

    eligibility_value = eligibility.eligible
    action = "UPDATE" if superseded else "ADD" if written else "NOOP"
    kind = written[0].kind.value if written else "none"
    scope = written[0].scope.value if written else (
        "project" if case.category == "cross_session_recall" else "none"
    )
    source_authority = _observed_source_authority(
        events, written, candidate_evidence,
    )
    roots = Counter(record.root_id for record in active)
    duplicate_count = sum(count - 1 for count in roots.values() if count > 1)
    secret_write_count = sum("NEVER-A-REAL-CREDENTIAL" in record.content for record in active)
    input_tokens = sum(
        value for name, data in observer_rows if name == "model"
        if type(value := data.get("input_tokens")) is int
    )
    output_tokens = sum(
        value for name, data in observer_rows if name == "model"
        if type(value := data.get("output_tokens")) is int
    )
    prohibited = []
    if secret_write_count:
        prohibited.append("secret_write")
    if unauthorized_recall_count:
        prohibited.append("unauthorized_recall")
    if unauthorized_mutation_count:
        prohibited.append("unauthorized_mutation")
    if not eligibility.eligible and written:
        prohibited.append("ineligible_write")
    return {
        "eligibility": eligibility_value,
        "action": action,
        "kind": kind,
        "scope": scope,
        "source_authority": source_authority,
        "recall_ids_top6": recall_ids,
        "prohibited_outcomes": prohibited,
        "secret_write_count": secret_write_count,
        "unauthorized_recall_count": unauthorized_recall_count,
        "unauthorized_mutation_count": unauthorized_mutation_count,
        "ineligible_trigger_write_count": int(not eligibility.eligible and bool(written)),
        "written_count": len(written),
        "fallback_used": bool(result and result.fallback_used),
        "fallback_success": bool(
            result and result.stage.value == "completed"
            and any(
                name == "model" and metadata.get("model_role") == "fallback"
                and metadata.get("outcome") == "success"
                for name, metadata in observer_rows
            )
        ),
        "degraded_without_write": bool(
            result and result.stage.value == "degraded" and not written
        ),
        "old_version_superseded": bool(superseded),
        "duplicate_active_logical_memories": duplicate_count,
        "latency_ms": elapsed_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": None,
    }


def test_frozen_gold_declares_expected_contract_and_is_synthetic():
    corpus, cases = load_memory_gold()

    assert corpus["synthetic"] is True
    assert corpus["version"] == "1.3.0"
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


def test_written_memory_with_unresolved_provenance_cannot_fall_back_to_candidate():
    event = SessionEvent(
        event_id="gold-user-event", seq=1, type=USER_MESSAGE,
        session_id="gold-session", run_id=None, data={"content": "synthetic fact"},
    )

    observed = _observed_source_authority(
        [event], [SimpleNamespace(source_event_ids=("missing-event",))],
        [{"event_id": "e1"}],
    )

    assert observed == []


def test_unwritten_candidate_with_unresolved_evidence_cannot_fall_back_to_user():
    event = SessionEvent(
        event_id="gold-user-event", seq=1, type=USER_MESSAGE,
        session_id="gold-session", run_id=None, data={"content": "unrelated question"},
    )

    observed = _observed_source_authority(
        [event], [], [{"event_id": "missing-ref"}],
    )

    assert observed == []


@pytest.mark.asyncio
async def test_frozen_gold_executes_all_cases_through_memory_v2_with_fake_providers(
    tmp_path, monkeypatch,
):
    _corpus, cases = load_memory_gold()
    identity = {"code_sha": "a" * 40, "tree_sha": "b" * 40}
    monkeypatch.setattr(memory_v2_quality, "capture_code_identity", lambda: identity)

    async def execute(case):
        case_root = tmp_path / case.case_id
        case_root.mkdir()
        return {
            "status": "executed",
            "observed": await _execute_gold_case_with_memory_v2(
                case, case_root / "memory-v2.db",
            ),
            "trace_id": f"trace-{case.case_id}",
        }

    report = await run_memory_gold_gate(
        execute, report_path=tmp_path / "gold-report.json",
        config_aliases={"primary": "memory.primary", "fallback": "memory.fallback"},
    )

    assert report["status"] == "passed", report["failures"]
    assert report["case_counts"]["executed"] == len(cases)
    saved = (tmp_path / "gold-report.json").read_text(encoding="utf-8")
    assert "synthetic_input" not in saved
    assert all(case.synthetic_input not in saved for case in cases)


@pytest.mark.parametrize(
    "case_id",
    ["unsupported_assistant_claim", "secret_probe", "sensitive_without_consent"],
)
@pytest.mark.asyncio
async def test_candidate_policy_rejection_does_not_change_trigger_eligibility(
    tmp_path, case_id,
):
    _corpus, cases = load_memory_gold()
    case = next(item for item in cases if item.case_id == case_id)

    observed = await _execute_gold_case_with_memory_v2(
        case, tmp_path / f"{case_id}.db",
    )

    assert observed["eligibility"] is True
    assert observed["action"] == "NOOP"


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
        ("primary_transient_fallback", "fallback_success", False,
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


def test_attempted_but_failed_fallback_does_not_count_as_success():
    corpus, cases = load_memory_gold()
    results = _results(cases)
    fallback = next(
        item for item in results
        if item["case_id"] == "primary_transient_fallback"
    )
    fallback["status"] = "degraded"
    fallback["observed"].update({
        "action": "NOOP",
        "kind": "none",
        "fallback_used": True,
        "fallback_success": False,
        "degraded_without_write": True,
        "written_count": 0,
    })

    report = evaluate_memory_gold(corpus, cases, results)

    assert report["case_counts"]["failed"] == 1
    assert report["case_counts"]["degraded"] == 1
    assert "degraded_cases" in report["failures"]
    assert report["metrics"]["transient_primary_fallback"] == {
        "numerator": 1, "denominator": 1, "value": 1.0,
        "threshold": 1.0, "pass": True,
    }
    assert report["metrics"]["fallback_model_success"] == {
        "numerator": 0, "denominator": 1, "value": 0.0,
        "threshold": 1.0, "pass": False,
    }


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_cost_fails_and_report_remains_strict_json(cost):
    corpus, cases = load_memory_gold()
    results = _results(cases)
    results[0]["observed"]["cost_usd"] = cost

    report = evaluate_memory_gold(corpus, cases, results)

    assert report["status"] == "failed"
    assert f"invalid_metric:{cases[0].case_id}:cost_usd" in report["failures"]
    json.dumps(report, allow_nan=False)


def test_real_runner_keeps_cases_isolated_in_shared_collection():
    first = _trusted_identity("case-one")
    second = _trusted_identity("case-two")
    foreign = _foreign_project_identity(first)

    assert first != second
    assert foreign.tenant_id == first.tenant_id
    assert foreign.user_id == first.user_id
    assert foreign.project_id != first.project_id


def test_real_runner_reports_primary_success_and_injected_fallback_budget():
    primary_success = {
        "alias": "memory.primary", "role": "primary", "stage": "formation",
        "attempt": 1, "outcome": "success",
    }
    fallback_attempts = [
        {
            "alias": "memory.primary", "role": "primary", "stage": "formation",
            "attempt": number, "outcome": "injected_transient_failure",
            "error_type": "TimeoutError",
        }
        for number in (1, 2, 3)
    ] + [{
        "alias": "memory.fallback", "role": "fallback", "stage": "formation",
        "attempt": 1, "outcome": "provider_error",
        "error_type": "PermissionDeniedError",
    }]
    summary = _model_execution_summary({"case_results": [
        {"case_id": "primary-success", "status": "executed",
         "model_attempts": [primary_success]},
        {"case_id": "primary_transient_fallback", "status": "degraded",
         "observed": {"fallback_success": False}, "model_attempts": fallback_attempts},
    ]})

    assert summary["primary"]["successful_cases"] == 1
    assert summary["fallback"]["attempts"] == 1
    assert summary["injected_failure_case"] == {
        "case_id": "primary_transient_fallback",
        "primary_transient_attempts": 3,
        "fallback_attempts": 1,
        "total_model_calls": 4,
        "fallback_success": False,
    }
    assert summary["budgets"]["observed_within_budget"] is True


@pytest.mark.asyncio
async def test_case_diagnostics_are_safe_and_keep_provider_error_type(tmp_path, monkeypatch):
    _corpus, cases = load_memory_gold()
    monkeypatch.setattr(memory_v2_quality, "capture_code_identity", lambda: {
        "code_sha": "a" * 40, "tree_sha": "b" * 40,
    })

    async def execute(case):
        if case.case_id == "primary_transient_fallback":
            return {
                "status": "failed", "error_type": "PermissionDeniedError",
                "observed": {
                    "decision_diagnostics": {
                        "formation_decision": "CANDIDATES",
                        "formation_skip_reason": None,
                        "formation_candidate_count": 1,
                        "selection_accepted_count": 1,
                        "selection_rejected_counts": {"over_cap": 0},
                        "adjudication_action_counts": {"UPDATE": 1},
                        "discarded_action_counts": {"target_unresolved": 1},
                        "raw_model_output": "must never be included in reports",
                    },
                },
                "model_attempts": [{
                    "alias": "memory.fallback", "role": "fallback",
                    "stage": "formation", "attempt": 1,
                    "outcome": "provider_error",
                    "error_type": "PermissionDeniedError",
                    "response": "private model response must not be retained",
                }],
            }
        return {"status": "skipped"}

    report_path = tmp_path / "safe-report.json"
    report = await run_memory_gold_gate(execute, report_path=report_path)
    saved = report_path.read_text(encoding="utf-8")
    fallback = next(
        item for item in report["case_results"]
        if item["case_id"] == "primary_transient_fallback"
    )

    assert fallback["error_type"] == "PermissionDeniedError"
    assert fallback["model_attempts"] == [{
        "alias": "memory.fallback", "role": "fallback", "stage": "formation",
        "attempt": 1, "outcome": "provider_error",
        "error_type": "PermissionDeniedError",
    }]
    assert fallback["observed"]["decision_diagnostics"] == {
        "formation_decision": "CANDIDATES",
        "formation_skip_reason": None,
        "formation_candidate_count": 1,
        "selection_accepted_count": 1,
        "selection_rejected_counts": {"over_cap": 0},
        "adjudication_action_counts": {"UPDATE": 1},
        "discarded_action_counts": {"target_unresolved": 1},
    }
    assert "private model response" not in saved
    assert "must never be included in reports" not in saved
    assert all(case.synthetic_input not in saved for case in cases)


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
