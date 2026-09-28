from __future__ import annotations

import json
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from agent_harness.config import Settings
from agent_harness.identity import IdentityContext, identity_context_var
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.commands import explicit_remember_matches
from agent_harness.memory.v2.executor import MemoryJobExecutor
from agent_harness.memory.v2.index import InMemoryMemoryV2Index
from agent_harness.memory.v2.jobs import SqliteMemoryV2JobStore
from agent_harness.memory.v2.policy import resolve_evidence_source
from agent_harness.memory.v2.projection import build_formation_input
from agent_harness.memory.v2.recall import MemoryV2ContextProvider, run_context_var
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
from agent_harness.memory.vector_store import VectorStoreError
from agent_harness.session import (
    MEMORY_RECALLED,
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    Session,
    SessionEvent,
    derive_messages,
)
from agent_harness.session.store import JsonlSessionStore
from evaluation import memory_v2_quality
from evaluation.memory_v2_quality import (
    GoldCase,
    evaluate_memory_gold,
    load_memory_gold,
    run_memory_gold_gate,
)
from scripts.run_memory_v2_real_gold_gate import (
    _create_gate_chat_model,
    _drop_owned_collection,
    _eligibility_for_case,
    _events_for_case,
    _execute_case,
    _foreign_project_identity,
    _GoldWorkspaceIndex,
    _has_gold_collection_schema,
    _initialize_owned_collection,
    _matches_recall_target,
    _model_execution_summary,
    _require_approved_gate_roles,
    _resolve_approved_gate_roles,
    _run_secret_write_probe,
    _trusted_identity,
    _verify_cross_session_lifecycle,
    _verify_untrusted_recall_stays_data,
)
from tests.memory.v2._records import make_draft, payload_for
from tests.memory.v2._vector import FakeMemoryVectorClient
from tests.memory.v2.test_v2_executor import (
    FakeInvoker,
    _add,
    _adjudication,
    _candidate,
    _formation_candidates,
    _formation_no_memory,
    _invalidate,
    _roles,
    _update,
)

_GOLD_MODEL_OUTPUTS = {
    "positive_semantic_preference": ("semantic", "user_global", "ADD", "preference"),
    "positive_project_fact": ("semantic", "project", "ADD", "project_fact"),
    "positive_episode": ("episodic", "project", "ADD", "project_fact"),
    "positive_procedure": ("procedural", "project", "ADD", "project_fact"),
    "explicit_remember": ("semantic", "project", "ADD", "project_fact"),
    "transient_noop": None,
    "unsupported_assistant_claim": (
        "semantic", "user_global", "NOOP", "profile", "assistant",
    ),
    "secret_probe": ("semantic", "user_global", "NOOP", "project_fact", "user", "secret"),
    "secret_direct_probe": None,
    "secret_api_edit_probe": None,
    "secret_fallback_probe": None,
    "secret_replay_probe": None,
    "explicit_remember_secret_probe": None,
    "sensitive_without_consent": (
        "semantic", "user_global", "NOOP", "project_fact", "user", "sensitive",
    ),
    "explicit_opt_out": None,
    "ineligible_cancelled": None,
    "ineligible_startup_failure": None,
    "ineligible_no_model_call": None,
    "ineligible_no_genuine_user_input": None,
    "wrong_project_isolation": None,
    "contradiction_supersession": ("semantic", "project", "UPDATE", "project_fact"),
    "contradiction_user_wins": ("semantic", "project", "UPDATE", "project_fact"),
    "contradiction_invalidation": ("semantic", "project", "INVALIDATE", "project_fact"),
    "cross_session_recall_one": ("semantic", "project", "ADD", "project_fact"),
    "cross_session_recall_two": ("semantic", "project", "ADD", "project_fact"),
    "primary_transient_fallback": ("semantic", "project", "ADD", "project_fact"),
    "replay_committed_job": ("semantic", "project", "ADD", "project_fact"),
}
_GOLD_RECALL_LABELS = {
    "cross_session_recall_one": "gold-project-name",
    "cross_session_recall_two": "gold-pagination",
}
_GOLD_RECALL_FACTS = {
    "cross_session_recall_one": "On our synthetic project, the prototype is named Sample Harbor.",
    "cross_session_recall_two": "The project API uses cursor pagination for listings.",
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
        "trigger_reason": {
            "cancelled": "cancelled",
            "startup_failure": "unsupported_terminal_failure",
            "no_model_call": "no_model_call",
            "no_genuine_user_input": "no_user_input",
            "explicit_opt_out": "explicit_opt_out",
        }.get(expected.get("run_end_trigger"), "eligible"),
        "action": action,
        "kind": expected["kind"],
        "scope": expected["scope"],
        "source_authority": expected["source_authority"],
        "recall_ids_top6": expected["recall_target"],
        "prohibited_outcomes": [],
        "secret_write_count": 0,
        "secret_probe_path": expected.get("secret_path", "none"),
        "secret_probe_attempted": expected.get("secret_path") is not None,
        "secret_probe_blocked": expected.get("secret_path") is not None,
        "explicit_remember_applied": expected.get("secret_path") == "explicit_remember",
        "unauthorized_recall_count": 0,
        "unauthorized_mutation_count": 0,
        "ineligible_trigger_write_count": 0,
        "written_count": int(action in {"ADD", "UPDATE"}),
        "fallback_used": expected.get("requires_fallback", False),
        "fallback_success": expected.get("requires_fallback", False),
        "degraded_without_write": False,
        "old_version_superseded": expected.get("supersedes_old_version", False),
        "old_version_invalidated": expected.get("invalidates_old_version", False),
        "lifecycle_verified": expected.get("lifecycle_required") is True,
        "lifecycle_evidence": ({
            "formed_from_source_session": True,
            "automatic_recall_selected": True,
            "why_recalled_api_verified": True,
            "authoritative_edit_verified": True,
            "version_history_verified": True,
            "deletion_verified": True,
            "tombstone_verified": True,
            "storage_erasure_verified": True,
            "recall_hidden_after_delete": True,
        } if expected.get("lifecycle_required") is True else {}),
        "untrusted_recall_fenced": expected.get("untrusted_recall_probe") is True,
        "simulated_privileged_tool_attempt": (
            expected.get("untrusted_recall_probe") is True
        ),
        "privileged_tool_attempt_denied": (
            expected.get("untrusted_recall_probe") is True
        ),
        "untrusted_recall_safe": expected.get("untrusted_recall_probe") is True,
        "trigger_job_created": False,
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
    events = _events_for_case(
        case, "gold-session", "gold-run",
        user_content=(
            case.expected.get("formation_input")
            if case.category == "cross_session_recall" else None
        ),
    )

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
    untrusted_recall_evidence: dict[str, bool] | None = None
    if case.category == "contradiction":
        previous_fact = (
            "The synthetic project codename is Amber Fox."
            if case.case_id == "contradiction_user_wins"
            else "The synthetic deploy window is Monday."
        )
        previous = await seed(previous_fact, trusted)
    elif case.category == "cross_session_recall":
        for distractor in _GOLD_RECALL_DISTRACTORS:
            await seed(distractor, trusted)
    elif case.case_id == "wrong_project_isolation":
        foreign = TrustedMemoryIdentity("gold-tenant", "gold-user", "other-project")
        await seed(case.synthetic_input, foreign)
    else:
        previous = None

    eligibility = _eligibility_for_case(case, events)
    observer_rows: list[tuple[str, dict]] = []
    result = None
    invoker = None
    written = ()
    trigger_job_created = False
    secret_path = case.expected.get("secret_path")
    secret_probe_attempted = secret_path is not None
    secret_probe_blocked = False
    candidate_evidence: list[dict] = []
    elapsed_ms = 0
    candidate_content = (
        case.synthetic_input.removeprefix("Remember that ").rstrip(".")
        if case.category == "explicit_command" else case.synthetic_input
    )
    if case.category == "cross_session_recall":
        candidate_content = case.expected["formation_input"]
    if case.case_id == "contradiction_user_wins":
        candidate_content = "The synthetic project codename is Cedar Lantern."
    explicit_remember_applied = explicit_remember_matches(
        case.synthetic_input, candidate_content,
    )
    if secret_path in {"direct", "api_edit"}:
        secret_probe_blocked = await _run_secret_write_probe(
            secret_path, service=service, index=index, trusted=trusted,
            workspace_root=database_path.parent,
        )
    elif eligibility.eligible:
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
            elif action == "INVALIDATE":
                assert previous is not None
                adjudications = [_adjudication(_invalidate(previous.id))]
            elif action == "ADD":
                adjudications = [_adjudication(_add(**candidate))]
            else:
                adjudications = []
        if case.expected.get("requires_fallback") is True:
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
        trigger_job_created = True
        claimed = await jobs.claim(worker_id="gold-worker")
        assert claimed is not None
        executor = MemoryJobExecutor(
            jobs=jobs, writer=service, searcher=service, invoker=invoker,
            observer=lambda name, metadata: observer_rows.append((name, metadata)),
        )
        started = time.monotonic()
        result = await executor.run(
            claimed, worker_id="gold-worker", run_events=events,
            roles=_roles(fallback=case.expected.get("requires_fallback") is True),
            explicit_remember=explicit_remember_applied,
        )
        if case.expected.get("replay") is True:
            replay = await executor.run(
                claimed, worker_id="gold-worker", run_events=events, roles=_roles(),
            )
            assert replay is not None and replay.written == ()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        written = result.written if result is not None else ()
        if case.category == "cross_session_recall":
            recall_labels.update({
                record.id: _GOLD_RECALL_LABELS[case.case_id] for record in written
            })
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
    invalidated = await service.list_records(
        trusted, status=MemoryStatus.INVALIDATED, limit=100,
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
        if case.expected.get("untrusted_recall_probe") is True:
            sessions = JsonlSessionStore(root=database_path.parent / "sessions")
            untrusted_recall_evidence = await _verify_untrusted_recall_stays_data(
                service=service, index=index, sessions=sessions, trusted=trusted,
                session_id="gold-untrusted-probe",
                run_id="gold-untrusted-probe-run", sandbox_root=database_path.parent,
            )
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
    trigger_reason = (
        eligibility.skip_reason.value if eligibility.skip_reason is not None else "eligible"
    )
    action = (
        "UPDATE" if superseded else "INVALIDATE" if invalidated
        else "ADD" if written else "NOOP"
    )
    changed = written or invalidated
    kind = changed[0].kind.value if changed else "none"
    scope = changed[0].scope.value if changed else (
        "project" if case.category == "cross_session_recall" else "none"
    )
    source_authority = _observed_source_authority(
        events, written, candidate_evidence,
    )
    roots = Counter(record.root_id for record in active)
    duplicate_count = sum(count - 1 for count in roots.values() if count > 1)
    persisted = [*active, *superseded, *invalidated]
    secret_write_count = sum(
        any(
            "NEVER-A-REAL-CREDENTIAL" in text
            for text in (
                record.content, record.payload.model_dump_json(),
                *(item.excerpt for item in record.evidence),
            )
        )
        for record in persisted
    )
    if secret_path not in {"direct", "api_edit"}:
        secret_probe_blocked = secret_write_count == 0
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
    if (
        case.expected.get("untrusted_recall_probe") is True
        and (
            untrusted_recall_evidence is None
            or untrusted_recall_evidence.get("untrusted_recall_safe") is not True
        )
    ):
        prohibited.append("privileged_prompt_effect")
    return {
        "eligibility": eligibility_value,
        "trigger_reason": trigger_reason,
        "secret_probe_path": secret_path or "none",
        "secret_probe_attempted": secret_probe_attempted,
        "secret_probe_blocked": secret_probe_blocked,
        "explicit_remember_applied": explicit_remember_applied,
        "action": action,
        "kind": kind,
        "scope": scope,
        "source_authority": source_authority,
        "recall_ids_top6": recall_ids,
        "prohibited_outcomes": prohibited,
        "secret_write_count": secret_write_count,
        "unauthorized_recall_count": unauthorized_recall_count,
        "unauthorized_mutation_count": unauthorized_mutation_count,
        "ineligible_trigger_write_count": int(
            not case.expected["eligibility"] and bool(written)
        ),
        "trigger_job_created": trigger_job_created,
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
        "old_version_invalidated": bool(invalidated),
        "lifecycle_verified": case.expected.get("lifecycle_required") is True,
        "lifecycle_evidence": ({
            "formed_from_source_session": True,
            "automatic_recall_selected": True,
            "why_recalled_api_verified": True,
            "authoritative_edit_verified": True,
            "version_history_verified": True,
            "deletion_verified": True,
            "tombstone_verified": True,
            "storage_erasure_verified": True,
            "recall_hidden_after_delete": True,
        } if case.expected.get("lifecycle_required") is True else {}),
        **(untrusted_recall_evidence or {}),
        "duplicate_active_logical_memories": duplicate_count,
        "latency_ms": elapsed_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": None,
    }


def test_frozen_gold_declares_expected_contract_and_is_synthetic():
    corpus, cases = load_memory_gold()

    assert corpus["synthetic"] is True
    assert corpus["version"] == "1.8.0"
    assert len(cases) >= 15
    assert {
        "cancelled", "startup_failure", "no_model_call", "no_genuine_user_input",
        "explicit_opt_out",
    } <= {
        case.expected.get("run_end_trigger")
        for case in cases if case.expected["eligibility"] is False
    }
    episode = next(case for case in cases if case.case_id == "positive_episode")
    assert episode.expected["scope"] == "project"
    assert all(
        case.expected["action"] == "ADD" for case in cases
        if case.category == "cross_session_recall"
    )
    assert sum(case.expected.get("lifecycle_required") is True for case in cases) == 1
    for case in cases:
        assert {
            "eligibility", "action", "kind", "scope", "source_authority",
            "recall_target", "prohibited_outcomes",
        } <= case.expected.keys()


def test_noop_accuracy_ignores_ineligible_trigger_profiles():
    corpus, cases = load_memory_gold()
    results = _results(cases)
    baseline = evaluate_memory_gold(corpus, cases, results)
    ineligible = next(item for item in results if item["case_id"] == "explicit_opt_out")
    ineligible["observed"]["action"] = "ADD"

    changed = evaluate_memory_gold(corpus, cases, results)

    assert changed["metrics"]["noop_accuracy"] == baseline["metrics"]["noop_accuracy"]


def test_gate_rejects_an_unapproved_fallback_model():
    with pytest.raises(RuntimeError, match="fallback model does not match"):
        _require_approved_gate_roles(SimpleNamespace(
            primary=SimpleNamespace(
                provider="deepseek", model_name="cline-pass/deepseek-v4.1-flash",
            ), fallback=SimpleNamespace(
                provider="mimo", model_name="another-model",
            ),
        ))


def test_gate_uses_cline_gateway_primary_and_mimo_fallback():
    settings = Settings(
        _env_file=None, model_provider="Cline",
        model_name="cline-pass/deepseek-v4.1-flash", model_api_key="primary-test-key",
        model_base_url="https://api.cline.bot/api/v1",
        fallback_model_provider="mimo", fallback_model_name="mimo-v2.6-flash",
        fallback_model_api_key="fallback-test-key",
        fallback_model_base_url="https://api.xiaomimimo.com/v1",
    )

    roles = _resolve_approved_gate_roles(settings)

    assert roles.primary.provider == "deepseek"
    assert roles.primary.model_name == "cline-pass/deepseek-v4.1-flash"
    assert roles.primary.base_url == "https://api.cline.bot/api/v1"
    assert roles.primary.get_secret_value() == "primary-test-key"
    assert roles.primary.fallback is None
    assert roles.fallback.provider == "mimo"
    assert roles.fallback.model_name == "mimo-v2.6-flash"
    assert roles.fallback.base_url == "https://api.xiaomimimo.com/v1"
    assert roles.fallback.get_secret_value() == "fallback-test-key"
    assert roles.fallback.fallback is None


def test_gate_streams_only_cline_primary_to_avoid_nonstandard_nonstream_envelope(monkeypatch):
    created = []

    class FakeModel:
        def __init__(self, streaming=False):
            self.streaming = streaming

        def model_copy(self, *, update):
            return FakeModel(streaming=update["streaming"])

    def fake_create_chat_model(config, *, reasoning_effort=None):
        created.append((config.provider, config.model_name, reasoning_effort))
        return FakeModel()

    monkeypatch.setattr(
        "scripts.run_memory_v2_real_gold_gate.create_chat_model", fake_create_chat_model,
    )

    primary = _create_gate_chat_model(SimpleNamespace(
        provider="deepseek", model_name="cline-pass/deepseek-v4.1-flash",
    ), reasoning_effort="deep")
    fallback = _create_gate_chat_model(SimpleNamespace(
        provider="mimo", model_name="mimo-v2.6-flash",
    ), reasoning_effort="deep")

    assert primary.streaming is True
    assert fallback.streaming is False
    assert created == [
        ("deepseek", "cline-pass/deepseek-v4.1-flash", "deep"),
        ("mimo", "mimo-v2.6-flash", "deep"),
    ]


def test_recall_target_label_requires_its_gold_fact_to_be_in_formed_memory():
    _corpus, cases = load_memory_gold()
    case = next(item for item in cases if item.case_id == "cross_session_recall_one")

    assert _matches_recall_target(
        SimpleNamespace(content="The prototype is Sample Harbor."), case,
    )
    assert not _matches_recall_target(
        SimpleNamespace(content="The project uses cursor pagination."), case,
    )


def test_real_procedure_gold_events_replay_complete_tool_call_batch():
    _corpus, cases = load_memory_gold()
    case = next(item for item in cases if item.case_id == "positive_procedure")
    events = _events_for_case(case, "gold-session", "gold-run")
    completion = next(event for event in events if event.type == MODEL_COMPLETED)
    calls = {
        event.data["tool_call_id"]: event
        for event in events if event.type == TOOL_CALL
    }
    results = {
        event.data["tool_call_id"]: event
        for event in events if event.type == TOOL_RESULT
    }

    assert calls.keys() == results.keys()
    assert len(calls) == 2
    assert all(calls[key].seq < results[key].seq for key in calls)
    assert [event.type for event in events if event.type in {TOOL_CALL, TOOL_RESULT}] == [
        TOOL_CALL, TOOL_CALL, TOOL_RESULT, TOOL_RESULT,
    ]
    assert [call["id"] for call in completion.data["tool_calls"]] == list(calls)

    messages = derive_messages(events)
    assistant = next(message for message in messages if getattr(message, "tool_calls", None))
    assert [call["id"] for call in assistant.tool_calls] == list(calls)
    assert [message.tool_call_id for message in messages if hasattr(message, "tool_call_id")] == list(results)


@pytest.mark.asyncio
async def test_collection_race_does_not_grant_cleanup_ownership():
    class RacingVectors:
        created_collection = False

        def __init__(self):
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1
            return [] if self.connect_calls == 1 else ["temporary_gold"]

        async def initialize(self):
            assert "temporary_gold" in await self.connect()
            self.created_collection = False

    vectors = RacingVectors()
    ownership = []
    collection_owned = await _initialize_owned_collection(
        vectors, collection_name="temporary_gold",
        on_collection_state=lambda value: ownership.append(value),
    )

    assert collection_owned is False
    assert ownership == ["attempted", "unverified"]
    assert vectors.connect_calls == 3


@pytest.mark.asyncio
async def test_collection_create_timeout_is_unowned_when_acknowledgement_is_ambiguous():
    class LostCreateResponse:
        created_collection = False
        dimension = 3
        connect_calls = 0

        async def connect(self):
            self.connect_calls += 1
            return [] if self.connect_calls == 1 else ["temporary_gold"]

        async def _call(self, operation, **_kwargs):
            assert operation == "describe_collection"
            return {"fields": [
                {"name": name, **({"params": {"dim": 3}} if name == "vector" else {}),
                 **({"is_partition_key": True} if name == "tenant_id" else {})}
                for name in (
                    "id", "memory_id", "tenant_id", "user_id", "scope", "session_id",
                    "content", "metadata", "vector",
                )
            ]}

        async def initialize(self):
            raise TimeoutError("response lost after remote create")

    ownership = []
    with pytest.raises(TimeoutError):
        await _initialize_owned_collection(
            LostCreateResponse(), collection_name="temporary_gold",
            on_collection_state=lambda value: ownership.append(value),
        )

    assert ownership == ["attempted", "unverified"]


@pytest.mark.asyncio
async def test_collection_with_mismatched_schema_is_not_claimed_after_create_error():
    class RacingVectors:
        created_collection = False
        dimension = 3

        def __init__(self):
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1
            return [] if self.connect_calls == 1 else ["temporary_gold"]

        async def _call(self, operation, **_kwargs):
            assert operation == "describe_collection"
            return {"fields": [{"name": "id"}, {"name": "vector", "params": {"dim": 9}}]}

        async def initialize(self):
            raise TimeoutError("create failed after another collection appeared")

    vectors = RacingVectors()
    ownership = []
    with pytest.raises(TimeoutError):
        await _initialize_owned_collection(
            vectors, collection_name="temporary_gold",
            on_collection_state=ownership.append,
        )

    assert ownership == ["attempted", "unverified"]
    assert await _has_gold_collection_schema(vectors, "temporary_gold") is False


@pytest.mark.asyncio
async def test_collection_create_lookup_failure_cannot_be_reported_clean():
    class UnavailableAfterCreate:
        created_collection = False

        def __init__(self):
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1
            if self.connect_calls == 1:
                return []
            raise TimeoutError("ownership lookup unavailable")

        async def initialize(self):
            raise TimeoutError("create acknowledgement unavailable")

    ownership = []
    with pytest.raises(TimeoutError):
        await _initialize_owned_collection(
            UnavailableAfterCreate(), collection_name="temporary_gold",
            on_collection_state=ownership.append,
        )

    assert ownership == ["attempted", "unverified"]


@pytest.mark.asyncio
async def test_collection_cleanup_refuses_a_mismatched_schema(monkeypatch):
    class UnownedVectors:
        _settings = object()
        _embeddings = object()
        created_collection = True
        drop_called = False

        async def connect(self):
            return ["temporary_gold"]

        async def _call(self, operation, **_kwargs):
            if operation == "drop_collection":
                self.drop_called = True
                return None
            assert operation == "describe_collection"
            return {"fields": [{"name": "id"}, {"name": "vector", "params": {"dim": 9}}]}

        async def close(self):
            return None

    vectors = UnownedVectors()
    monkeypatch.setattr(
        "scripts.run_memory_v2_real_gold_gate.MilvusVectorStore",
        lambda *_args: vectors,
    )

    with pytest.raises(RuntimeError, match="schema could not be verified"):
        await _drop_owned_collection(vectors, collection_name="temporary_gold")

    assert vectors.drop_called is False


@pytest.mark.asyncio
async def test_collection_cleanup_retries_transient_readback_after_drop():
    class TransientReadbackVectors:
        _settings = object()
        _embeddings = object()
        created_collection = True
        dimension = 3

        def __init__(self):
            self.connect_calls = 0
            self.drop_calls = 0
            self.close_calls = 0

        async def connect(self):
            self.connect_calls += 1
            if self.connect_calls == 1:
                return ["temporary_gold"]
            if self.connect_calls == 2:
                raise RuntimeError("transient readback failure")
            return []

        async def _call(self, operation, **_kwargs):
            if operation == "drop_collection":
                self.drop_calls += 1
                return None
            assert operation == "describe_collection"
            return {"fields": [
                {"name": name,
                 **({"params": {"dim": 3}} if name == "vector" else {}),
                 **({"is_partition_key": True} if name == "tenant_id" else {})}
                for name in (
                    "id", "memory_id", "tenant_id", "user_id", "scope", "session_id",
                    "content", "metadata", "vector",
                )
            ]}

        async def close(self):
            self.close_calls += 1

    vectors = TransientReadbackVectors()

    await _drop_owned_collection(vectors, collection_name="temporary_gold")

    assert vectors.connect_calls == 3
    assert vectors.drop_calls == 1
    assert vectors.close_calls == 1


@pytest.mark.asyncio
async def test_collection_cleanup_refuses_matching_schema_without_create_ack():
    class AmbiguousVectors:
        _settings = object()
        _embeddings = object()
        created_collection = False
        drop_called = False

        async def connect(self):
            return ["temporary_gold"]

        async def _call(self, operation, **_kwargs):
            if operation == "drop_collection":
                self.drop_called = True
                return None
            assert operation == "describe_collection"
            return {"fields": [
                {"name": name, **({"params": {"dim": 3}} if name == "vector" else {}),
                 **({"is_partition_key": True} if name == "tenant_id" else {})}
                for name in (
                    "id", "memory_id", "tenant_id", "user_id", "scope", "session_id",
                    "content", "metadata", "vector",
                )
            ]}

        async def close(self):
            return None

    vectors = AmbiguousVectors()
    with pytest.raises(RuntimeError, match="ownership could not be verified"):
        await _drop_owned_collection(vectors, collection_name="temporary_gold")

    assert vectors.drop_called is False


@pytest.mark.parametrize("case_id", ["../escape", "..\\escape", "C:escape", "\\absolute"])
def test_gold_case_ids_cannot_escape_the_temporary_case_root(tmp_path, case_id):
    path = tmp_path / "malicious.json"
    path.write_text(json.dumps({
        "corpus_id": "test", "version": "1", "synthetic": True,
        "cases": [{"case_id": case_id, "expected": {
            "eligibility": True, "action": "NOOP", "kind": "none", "scope": "none",
            "source_authority": [], "recall_target": [], "prohibited_outcomes": [],
        }}],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="case IDs"):
        load_memory_gold(path)


def test_gold_cases_must_be_objects(tmp_path):
    path = tmp_path / "malformed.json"
    path.write_text(json.dumps({
        "corpus_id": "test", "version": "1", "synthetic": True,
        "cases": [None],
    }), encoding="utf-8")

    with pytest.raises(TypeError, match="cases must be objects"):
        load_memory_gold(path)


@pytest.mark.parametrize(
    ("case_id", "skip_reason"),
    [
        ("ineligible_cancelled", "cancelled"),
        ("ineligible_startup_failure", "unsupported_terminal_failure"),
        ("ineligible_no_model_call", "no_model_call"),
        ("ineligible_no_genuine_user_input", "no_user_input"),
        ("explicit_opt_out", "explicit_opt_out"),
    ],
)
def test_real_gate_trigger_profiles_are_ineligible(case_id, skip_reason):
    _corpus, cases = load_memory_gold()
    case = next(case for case in cases if case.case_id == case_id)
    events = _events_for_case(case, "trigger-session", "trigger-run")
    user_events = [event for event in events if event.type == USER_MESSAGE]

    decision = _eligibility_for_case(case, events)

    assert user_events
    if case_id == "ineligible_no_genuine_user_input":
        assert user_events[0].data["injected_by"] == "memory-v2-gold-runner"
    assert decision.eligible is False
    assert decision.skip_reason.value == skip_reason


@pytest.mark.asyncio
async def test_real_gold_api_edit_probe_uses_the_http_route(tmp_path):
    store = SqliteMemoryV2Store(tmp_path / "api-probe.db")
    await store.initialize()
    index = InMemoryMemoryV2Index()
    service = MemoryV2Service(store, index)
    try:
        blocked = await _run_secret_write_probe(
            "api_edit", service=service, index=index,
            trusted=_trusted_identity("api-edit-probe"), workspace_root=tmp_path,
        )
    finally:
        await service.aclose()

    assert blocked is True


@pytest.mark.asyncio
async def test_cross_session_lifecycle_uses_provider_and_authorized_web_apis(tmp_path):
    store = SqliteMemoryV2Store(tmp_path / "lifecycle.db")
    await store.initialize()
    index = InMemoryMemoryV2Index()
    service = MemoryV2Service(store, index)
    sessions = JsonlSessionStore(root=tmp_path / "sessions")
    trusted = _trusted_identity("lifecycle-probe")
    source_session_id = "gold-lifecycle-source"
    recall_session_id = "gold-lifecycle-recall"
    source_run_id = "gold-lifecycle-source-run"
    recall_run_id = "gold-lifecycle-recall-run"
    content = "On our synthetic project, the prototype is named Sample Harbor."
    source = Session.start(sessions, session_id=source_session_id)
    source_event = source.append(
        USER_MESSAGE, {"content": content}, run_id=source_run_id,
    )
    source.append(
        MODEL_COMPLETED, {"content": "Acknowledged."}, run_id=source_run_id,
    )
    record = await service.create(make_draft(
        scope=MemoryScope.PROJECT, project_id=trusted.project_id,
        content=content,
        payload=payload_for(
            MemoryKind.SEMANTIC, subject="prototype", fact=content,
            category=SemanticCategory.PROJECT_FACT,
        ),
        source_session_id=source_session_id,
        source_event_ids=[source_event.event_id],
    ), trusted)
    await index.upsert(record)
    query_session = Session.start(sessions, session_id=recall_session_id)
    query_session.append(
        USER_MESSAGE, {"content": "What is the prototype name in this project?"},
        run_id=recall_run_id,
    )
    workspace_index = _GoldWorkspaceIndex(
        trusted.project_id, [source_session_id, recall_session_id],
    )
    identity_token = identity_context_var.set(IdentityContext(
        trusted.tenant_id, trusted.user_id, ["user"],
    ))
    run_token = run_context_var.set(recall_run_id)
    try:
        injected = await MemoryV2ContextProvider(
            service, workspace_index=workspace_index,
        ).select(query_session, 2000)
    finally:
        run_context_var.reset(run_token)
        identity_context_var.reset(identity_token)

    recalled = [
        item for event in query_session.events if event.type == MEMORY_RECALLED
        for item in event.data["memories"]
    ]
    automatic_recall_selected = (
        any(item["memory_id"] == record.id for item in recalled)
        and any(record.content in str(message.content) for message in injected)
    )
    lifecycle = await _verify_cross_session_lifecycle(
        database_path=tmp_path / "lifecycle.db", sessions=sessions,
        service=service, trusted=trusted, workspace_index=workspace_index,
        record=record, recall_session_id=recall_session_id,
        source_session_id=source_session_id,
        automatic_recall_selected=automatic_recall_selected,
    )

    assert all(lifecycle.values()), lifecycle


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
    lifecycle_case = next(
        item for item in report["case_results"]
        if item["case_id"] == "cross_session_recall_one"
    )
    assert lifecycle_case["observed"]["lifecycle_evidence"]["storage_erasure_verified"]
    assert report["usage"] == {
        "latency_ms_total": 20 * len(cases),
        "input_tokens": 32 * len(cases),
        "output_tokens": 8 * len(cases),
        "cost_usd": None,
    }
    assert "synthetic_input" not in json.dumps(report)


def test_lifecycle_gate_requires_per_case_boolean_evidence():
    corpus, cases = load_memory_gold()
    results = _results(cases)
    lifecycle = next(
        item for item in results if item["case_id"] == "cross_session_recall_one"
    )
    lifecycle["observed"].pop("lifecycle_evidence")

    report = evaluate_memory_gold(corpus, cases, results)

    assert report["status"] == "failed"
    assert "invalid_metric:cross_session_recall_one:lifecycle_evidence" in report["failures"]


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

    assert report["status"] == "passed", {
        "failures": report["failures"],
        "cases": [
            (item["case_id"], item["status"], item.get("error_type"), item.get("observed"))
            for item in report["case_results"] if item["status"] != "executed"
        ],
    }
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
        ("wrong_project_isolation", "untrusted_recall_safe", False,
         "non_privileged_recall"),
        ("wrong_project_isolation", "untrusted_recall_fenced", False,
         "non_privileged_recall"),
        ("wrong_project_isolation", "simulated_privileged_tool_attempt", False,
         "non_privileged_recall"),
        ("wrong_project_isolation", "privileged_tool_attempt_denied", False,
         "non_privileged_recall"),
        ("explicit_opt_out", "ineligible_trigger_write_count", 1, "ineligible_trigger_writes"),
        ("explicit_opt_out", "trigger_job_created", True, "ineligible_trigger_jobs"),
        ("transient_noop", "action", "ADD", "noop_accuracy"),
        ("transient_noop", "action", "ADD", "write_precision"),
        ("positive_semantic_preference", "written_count", 0, "write_precision"),
        ("positive_semantic_preference", "kind", "episodic", "kind_accuracy"),
        ("contradiction_supersession", "old_version_superseded", False,
         "contradiction_handling"),
        ("contradiction_invalidation", "old_version_invalidated", False,
         "contradiction_handling"),
        ("cross_session_recall_one", "recall_ids_top6", [], "cross_session_recall_at_6"),
        ("cross_session_recall_one", "lifecycle_verified", False,
         "cross_session_lifecycle"),
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
    if gate == "kind_accuracy":
        second_case = next(
            item for item in results if item["case_id"] == "positive_project_fact"
        )
        second_case["observed"]["kind"] = "episodic"

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
        "numerator": 2, "denominator": 2, "value": 1.0,
        "threshold": 1.0, "pass": True,
    }
    assert report["metrics"]["fallback_model_success"] == {
        "numerator": 1, "denominator": 2, "value": 0.5,
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
                        "schema_failure_stage": "formation",
                        "schema_failure_kind": "contract_violation",
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
        "schema_failure_stage": "formation",
        "schema_failure_kind": "contract_violation",
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
async def test_vector_store_error_report_keeps_only_allowlisted_category(tmp_path, monkeypatch):
    identity = {"code_sha": "a" * 40, "tree_sha": "b" * 40}
    monkeypatch.setattr(memory_v2_quality, "capture_code_identity", lambda: identity)
    selected = {
        "positive_semantic_preference": "unavailable",
        "positive_project_fact": "private-value",
    }

    async def execute(case):
        if case.case_id in selected:
            raise VectorStoreError(selected[case.case_id])
        return {"status": "skipped"}

    report_path = tmp_path / "safe-vector-report.json"
    report = await run_memory_gold_gate(execute, report_path=report_path)
    saved = report_path.read_text(encoding="utf-8")
    results = {item["case_id"]: item for item in report["case_results"]}

    assert report["schema_version"] == 3
    assert results["positive_semantic_preference"]["error_code"] == "unavailable"
    assert "error_code" not in results["positive_project_fact"]
    assert "private-value" not in saved
    assert "Memory vector store" not in saved


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


@pytest.mark.asyncio
async def test_automatic_gold_case_reaches_stubbed_invoker(tmp_path, monkeypatch):
    _corpus, cases = load_memory_gold()
    case = next(case for case in cases if case.case_id == "positive_semantic_preference")
    settings = Settings(milvus_collection="fake-gold")
    invoker_calls = []

    class StubbedInvoker:
        def __init__(self, *, inject_primary_transient):
            self.attempts = []

        async def __call__(self, call):
            invoker_calls.append(call)
            self.attempts.append({
                "alias": "memory.primary" if call.role.value == "primary" else "memory.fallback",
                "role": call.role.value,
                "stage": call.stage.value,
                "attempt": call.attempt,
                "outcome": "transient_provider_error",
                "error_type": "TimeoutError",
            })
            raise TimeoutError("provider call is intentionally stubbed")

    monkeypatch.setattr(
        "scripts.run_memory_v2_real_gold_gate._RecordingInvoker", StubbedInvoker,
    )

    outcome = await _execute_case(
        case, database_path=tmp_path / "memory.db", roles=_roles(fallback=True),
        vector_store=FakeMemoryVectorClient(settings),
    )

    assert invoker_calls
    assert outcome["status"] == "degraded"


@pytest.mark.asyncio
async def test_explicit_remember_secret_gold_case_is_blocked_without_write(tmp_path):
    _corpus, cases = load_memory_gold()
    case = next(case for case in cases if case.case_id == "explicit_remember_secret_probe")
    settings = Settings(milvus_collection="fake-gold")

    outcome = await _execute_case(
        case, database_path=tmp_path / "memory.db", roles=_roles(fallback=True),
        vector_store=FakeMemoryVectorClient(settings),
    )

    assert outcome["status"] == "executed"
    assert outcome["observed"]["secret_probe_attempted"] is True
    assert outcome["observed"]["secret_probe_blocked"] is True
    assert outcome["observed"]["secret_write_count"] == 0
    assert outcome["observed"]["action"] == "NOOP"
