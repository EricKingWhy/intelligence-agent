"""Run the frozen synthetic Memory V2 gold set through configured real models.

Example:
    uv run python scripts/run_memory_v2_real_gold_gate.py \
      --env-file D:\\intelligence-agent-backend\\.env

The report contains only case IDs, fixed metrics, model aliases, and safe error
types. It never stores prompts, model responses, memory text, or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import platform
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from agent_harness.agent.types import STATUS_COMPLETED
from agent_harness.config import Settings
from agent_harness.memory.embeddings import create_embeddings
from agent_harness.memory.milvus_vector_store import MilvusVectorStore
from agent_harness.memory.v2 import (
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    MemoryTier,
    SemanticCategory,
    SemanticPayload,
    SourceType,
    TrustedMemoryIdentity,
)
from agent_harness.memory.v2.budget import (
    FALLBACK_MAX_ATTEMPTS,
    MEMORY_JOB_MAX_CALLS,
    PRIMARY_MAX_ATTEMPTS,
)
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.commands import explicit_remember_matches
from agent_harness.memory.v2.eligibility import (
    MEMORY_OPT_OUT_FIELD,
    decide_run_end_eligibility,
)
from agent_harness.memory.v2.executor import MemoryJobExecutor, MemoryModelCall
from agent_harness.memory.v2.index import MemoryV2IndexRelay, MemoryV2VectorIndex
from agent_harness.memory.v2.jobs import MemoryJobStage, SqliteMemoryV2JobStore
from agent_harness.memory.v2.milvus_index import MilvusMemoryV2Index
from agent_harness.memory.v2.policy import resolve_evidence_source
from agent_harness.memory.v2.roles import (
    MEMORY_FALLBACK_ALIAS,
    MEMORY_PRIMARY_ALIAS,
    MemoryModelRoles,
    resolve_memory_roles,
)
from agent_harness.memory.v2.runner import ChatModelInvoker
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import MemoryRecordV2
from agent_harness.model.fallback import is_transient_model_error
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.tooling.result import ToolResult
from evaluation.memory_v2_quality import (
    GoldCase,
    run_memory_gold_gate,
    write_memory_gold_report,
)

_RECALL_LABELS = {
    "cross_session_recall_one": "gold-project-name",
    "cross_session_recall_two": "gold-pagination",
}
_RECALL_FACTS = {
    "cross_session_recall_one": "The synthetic project is named Sample Harbor.",
    "cross_session_recall_two": "The project API lists results with cursor pagination.",
}
_RECALL_DISTRACTORS = (
    "Project status summaries should stay concise.",
    "The release cutoff is Wednesday.",
    "Deployments require approval before production.",
    "Generated reports go to the archive bucket.",
    "A nightly job checks database backups.",
    "Support escalations go to the on-call engineer.",
    "Service authentication uses signed tokens.",
)
_SECRET_SENTINEL = "NEVER-A-REAL-CREDENTIAL"


class _RecordingInvoker:
    """Use the configured invoker and retain only safe per-call facts."""

    def __init__(self, *, inject_primary_transient: bool) -> None:
        self._inner = ChatModelInvoker()
        self._inject_primary_transient = inject_primary_transient
        self.attempts: list[dict[str, Any]] = []

    async def __call__(self, call: MemoryModelCall) -> str:
        role = call.role.value
        stage = call.stage.value
        alias = MEMORY_PRIMARY_ALIAS if role == "primary" else MEMORY_FALLBACK_ALIAS
        if (
            self._inject_primary_transient and role == "primary"
            and stage == "formation" and call.attempt <= 3
        ):
            self.attempts.append({
                "alias": alias, "role": role, "stage": stage,
                "attempt": call.attempt, "outcome": "injected_transient_failure",
                "error_type": "TimeoutError",
            })
            raise TimeoutError()
        try:
            response = await self._inner(call)
        except Exception as error:
            self.attempts.append({
                "alias": alias, "role": role, "stage": stage,
                "attempt": call.attempt,
                "outcome": (
                    "transient_provider_error"
                    if is_transient_model_error(error) else "provider_error"
                ),
                "error_type": type(error).__name__,
            })
            raise
        self.attempts.append({
            "alias": alias, "role": role, "stage": stage,
            "attempt": call.attempt, "outcome": "success",
        })
        return response


def _events_for_case(case: GoldCase, session_id: str, run_id: str) -> list[SessionEvent]:
    if case.case_id == "unsupported_assistant_claim":
        user_content = "What should be retained from this synthetic conversation?"
        assistant_content = case.synthetic_input
    else:
        user_content = case.synthetic_input
        assistant_content = "Acknowledged."
    events = [
        SessionEvent(
            event_id=f"{case.case_id}-user", seq=1, type=USER_MESSAGE,
            session_id=session_id, run_id=run_id,
            data={
                "content": user_content,
                **({MEMORY_OPT_OUT_FIELD: True}
                   if case.case_id == "explicit_opt_out" else {}),
            },
        ),
        SessionEvent(
            event_id=f"{case.case_id}-assistant", seq=2, type=MODEL_COMPLETED,
            session_id=session_id, run_id=run_id,
            data={"content": assistant_content},
        ),
    ]
    if case.case_id == "positive_procedure":
        events.extend(
            SessionEvent(
                event_id=f"{case.case_id}-tool-{number}", seq=number + 1,
                type=TOOL_RESULT, session_id=session_id, run_id=run_id,
                data={
                    "tool_call_id": f"{case.case_id}-call-{number}",
                    "content": ToolResult.success("done").model_dump_json(),
                },
            )
            for number in (2, 3)
        )
    return events


def _trusted_identity(case_id: str) -> TrustedMemoryIdentity:
    """Keep cases isolated inside the one temporary collection used by a run."""
    suffix = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]
    return TrustedMemoryIdentity(
        f"gold-tenant-{suffix}",
        f"gold-user-{suffix}",
        f"gold-project-{suffix}",
    )


def _foreign_project_identity(identity: TrustedMemoryIdentity) -> TrustedMemoryIdentity:
    return TrustedMemoryIdentity(
        identity.tenant_id, identity.user_id, f"{identity.project_id}-other",
    )


def _seed_draft(content: str, identity: TrustedMemoryIdentity) -> MemoryDraftV2:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return MemoryDraftV2(
        kind=MemoryKind.SEMANTIC,
        tier=MemoryTier.COLLECTION,
        scope=MemoryScope.PROJECT,
        project_id=identity.project_id,
        content=content,
        payload=SemanticPayload(
            subject="synthetic fact", fact=content,
            category=SemanticCategory.PROJECT_FACT,
        ),
        importance=0.8,
        strength=0.9,
        source_type=SourceType.AUTOMATIC,
        source_session_id="gold-seed-session",
        source_event_ids=["gold-seed-event"],
        evidence=[EvidenceItem(
            role="user", excerpt=content[:120], hash=digest,
        )],
    )


async def _seed_memory(
    service: MemoryV2Service, index: MemoryV2VectorIndex,
    content: str, identity: TrustedMemoryIdentity,
) -> MemoryRecordV2:
    record = await service.create(_seed_draft(content, identity), identity)
    await index.upsert(record)
    return record


def _decision_diagnostics(
    observer_rows: Sequence[tuple[str, Mapping[str, Any]]],
    job_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Return stage decisions and enum counters without candidate content."""
    diagnostics: dict[str, Any] = {}
    formation = next((metadata for name, metadata in reversed(observer_rows)
                      if name == "formation"), None)
    if formation is not None:
        diagnostics.update({
            "formation_decision": formation.get("outcome"),
            "formation_skip_reason": formation.get("skip_reason"),
            "formation_candidate_count": formation.get("candidates"),
        })
    selection = next((metadata for name, metadata in reversed(observer_rows)
                      if name == "selection"), None)
    if selection is not None:
        diagnostics.update({
            "selection_accepted_count": selection.get("accepted"),
            "selection_rejected_counts": selection.get("counts"),
        })
    adjudication = next((metadata for name, metadata in reversed(observer_rows)
                         if name == "adjudication"), None)
    if adjudication is not None:
        diagnostics["adjudication_action_counts"] = adjudication.get("actions")
    diagnostics["discarded_action_counts"] = job_state.get("discarded", {})
    return diagnostics


async def _execute_case(
    case: GoldCase, *, database_path: Path, roles: MemoryModelRoles,
    vector_store: MilvusVectorStore,
) -> Mapping[str, Any]:
    session_id = f"gold-{uuid4().hex}"
    run_id = f"run-{uuid4().hex}"
    # Stale vectors from prior cases must not crowd out this case's top-six recall.
    trusted = _trusted_identity(case.case_id)
    events = _events_for_case(case, session_id, run_id)
    eligibility = decide_run_end_eligibility(
        terminal_status=STATUS_COMPLETED, events=events,
    )
    store = SqliteMemoryV2Store(database_path)
    jobs = SqliteMemoryV2JobStore(database_path)
    await store.initialize()
    await jobs.initialize()
    index = MilvusMemoryV2Index(vector_store)
    service = MemoryV2Service(store, index, relay=MemoryV2IndexRelay(store, index))
    observer_rows: list[tuple[str, dict[str, Any]]] = []
    job_state: Mapping[str, Any] = {}
    recalled_labels: dict[str, str] = {}
    invoker = _RecordingInvoker(
        inject_primary_transient=case.case_id == "primary_transient_fallback",
    )
    result = None
    written: tuple[MemoryRecordV2, ...] = ()
    elapsed_ms = 0
    active: list[MemoryRecordV2] = []
    superseded: list[MemoryRecordV2] = []
    recall_ids: list[str] = []
    unauthorized_recall_count = 0
    unauthorized_mutation_count = 0
    try:
        if case.category == "contradiction":
            previous_fact = (
                "The synthetic project codename is Amber Fox."
                if case.case_id == "contradiction_user_wins"
                else "The synthetic deploy window is Monday."
            )
            await _seed_memory(service, index, previous_fact, trusted)
        elif case.category == "cross_session_recall":
            label = _RECALL_LABELS[case.case_id]
            record = await _seed_memory(
                service, index, _RECALL_FACTS[case.case_id], trusted,
            )
            recalled_labels[record.id] = label
            for distractor in _RECALL_DISTRACTORS:
                await _seed_memory(service, index, distractor, trusted)
        elif case.case_id == "wrong_project_isolation":
            foreign = _foreign_project_identity(trusted)
            await _seed_memory(service, index, case.synthetic_input, foreign)

        if eligibility.eligible:
            if roles.primary is None:
                return {
                    "status": "failed", "error_type": "MissingPrimaryModel",
                    "model_attempts": [],
                }
            await jobs.enqueue(
                idempotency_key=f"gold-{case.case_id}-{uuid4().hex}",
                trusted=trusted, session_id=session_id, run_id=run_id,
            )
            claimed = await jobs.claim(worker_id="memory-v2-gold-runner")
            if claimed is None:
                return {"status": "failed", "error_type": "MemoryJobNotClaimed"}

            def observe(name: str, metadata: dict[str, Any]) -> None:
                observer_rows.append((name, dict(metadata)))
                if name == "schema" and metadata.get("schema_valid") is False:
                    stage = metadata.get("model_stage")
                    for attempt in reversed(invoker.attempts):
                        if attempt["stage"] == stage and attempt["outcome"] == "success":
                            attempt["outcome"] = "invalid_model_output"
                            attempt["error_type"] = "ModelOutputError"
                            break

            executor = MemoryJobExecutor(
                jobs=jobs, writer=service, searcher=service,
                invoker=invoker, observer=observe,
            )
            candidate_content = (
                case.synthetic_input.removeprefix("Remember that ").rstrip(".")
                if case.category == "explicit_command" else case.synthetic_input
            )
            started = time.monotonic()
            result = await executor.run(
                claimed, worker_id="memory-v2-gold-runner", run_events=events,
                roles=roles,
                explicit_remember=explicit_remember_matches(
                    case.synthetic_input, candidate_content,
                ),
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if result is None:
                return {
                    "status": "failed", "error_type": "MemoryJobOwnershipLost",
                    "model_attempts": invoker.attempts,
                }
            job_state = (await jobs.get(claimed.job_id)).state
            written = result.written
            if case.expected.get("replay") is True:
                replay = await executor.run(
                    claimed, worker_id="memory-v2-gold-runner", run_events=events,
                    roles=roles,
                )
                if replay is None or replay.written:
                    return {
                        "status": "failed", "error_type": "ReplayWasNotIdempotent",
                        "model_attempts": invoker.attempts,
                    }

        for scope in (MemoryScope.USER_GLOBAL, MemoryScope.PROJECT):
            active.extend(await service.list_active(trusted, scope=scope, limit=100))
        superseded = await service.list_records(
            trusted, status=MemoryStatus.SUPERSEDED, limit=100,
        )
        if case.category == "cross_session_recall":
            hits = await service.hybrid_search(
                case.synthetic_input, trusted,
                scopes=[MemoryScope.PROJECT], limit=6,
            )
            recall_ids = [
                recalled_labels[hit.record.id]
                for hit in hits if hit.record.id in recalled_labels
            ]
        elif case.case_id == "wrong_project_isolation":
            hits = await service.search(
                case.synthetic_input, trusted, scope=MemoryScope.PROJECT, limit=6,
            )
            unauthorized_recall_count = len(hits)
            foreign_draft = _seed_draft(
                "forbidden synthetic mutation",
                _foreign_project_identity(trusted),
            )
            try:
                await service.create(foreign_draft, trusted)
            except PermissionError:
                pass
            else:
                unauthorized_mutation_count = 1

        outcome_kinds = [
            action for name, metadata in observer_rows if name == "adjudication"
            for action in metadata.get("actions", {})
        ]
        if superseded:
            action = "UPDATE"
        elif written:
            action = "ADD" if "ADD" in outcome_kinds else "NOOP"
        else:
            action = "NOOP"
        kind = written[0].kind.value if written else "none"
        scope = written[0].scope.value if written else (
            "project" if case.category == "cross_session_recall" else "none"
        )
        if written:
            event_by_id = {event.event_id: event for event in events}
            source_ids = {
                event_id for record in written for event_id in record.source_event_ids
            }
            source_authority = sorted({
                resolve_evidence_source(event_by_id[event_id]).value
                for event_id in source_ids if event_id in event_by_id
            })
        elif case.case_id == "unsupported_assistant_claim":
            source_authority = ["assistant"]
        else:
            source_authority = sorted({
                resolve_evidence_source(event).value
                for event in events if event.type == USER_MESSAGE
            })

        secret_write_count = sum(_SECRET_SENTINEL in record.content for record in active)
        active_roots = Counter(record.root_id for record in active)
        duplicate_count = sum(count - 1 for count in active_roots.values() if count > 1)
        prohibited: list[str] = []
        if secret_write_count:
            prohibited.append("secret_write")
        if unauthorized_recall_count:
            prohibited.append("unauthorized_recall")
        if unauthorized_mutation_count:
            prohibited.append("unauthorized_mutation")
        if not eligibility.eligible and written:
            prohibited.append("ineligible_write")
        if case.case_id == "unsupported_assistant_claim" and written:
            prohibited.append("unauthorized_mutation")
        if case.case_id == "sensitive_without_consent" and written:
            prohibited.append("sensitive_write")

        input_tokens = sum(
            value for name, metadata in observer_rows if name == "model"
            if type(value := metadata.get("input_tokens")) is int and value >= 0
        )
        output_tokens = sum(
            value for name, metadata in observer_rows if name == "model"
            if type(value := metadata.get("output_tokens")) is int and value >= 0
        )
        fallback_used = bool(result and result.fallback_used)
        fallback_call_success = any(
            attempt["role"] == "fallback" and attempt["outcome"] == "success"
            for attempt in invoker.attempts
        )
        completed = bool(result and result.stage is MemoryJobStage.COMPLETED)
        observed = {
            "eligibility": eligibility.eligible,
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
            "fallback_used": fallback_used,
            "fallback_success": fallback_call_success and completed,
            "degraded_without_write": bool(
                result and result.stage is MemoryJobStage.DEGRADED and not written
            ),
            "old_version_superseded": bool(superseded),
            "duplicate_active_logical_memories": duplicate_count,
            "latency_ms": elapsed_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": None,
            "decision_diagnostics": _decision_diagnostics(observer_rows, job_state),
        }
        status = (
            "degraded" if result and result.stage is MemoryJobStage.DEGRADED
            else "executed" if result is None or result.stage is MemoryJobStage.COMPLETED
            else "failed"
        )
        result_error = None
        if status in {"failed", "degraded"}:
            result_error = next((
                attempt["error_type"] for attempt in reversed(invoker.attempts)
                if attempt.get("error_type")
            ), None)
            if result_error is None:
                result_error = (
                    "ModelOutputError"
                    if any(a["outcome"] == "invalid_model_output" for a in invoker.attempts)
                    else "MemoryJobDegraded"
                )
        return {
            "status": status, "observed": observed,
            "error_type": result_error,
            "model_attempts": invoker.attempts,
        }
    finally:
        await service.aclose()


def _tool_versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for package in ("intelligence-agent", "langchain-core", "langchain-openai"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    return versions


def _environment_capabilities(settings: Settings, roles: MemoryModelRoles) -> dict[str, bool]:
    return {
        "memory_primary": roles.primary is not None,
        "memory_fallback": roles.fallback is not None,
        "milvus_memory": bool(
            settings.milvus_uri and settings.milvus_token.get_secret_value()
        ),
        "embedding": bool(
            settings.embedding_model and settings.embedding_base_url
            and settings.embedding_api_key.get_secret_value()
        ),
        "langfuse": bool(
            settings.langfuse_public_key.get_secret_value()
            and settings.langfuse_secret_key.get_secret_value()
            and settings.langfuse_base_url
        ),
        "qiniu_artifact": bool(
            settings.artifact_store_endpoint and settings.artifact_store_bucket
            and settings.artifact_store_access_key.get_secret_value()
            and settings.artifact_store_secret_key.get_secret_value()
        ),
    }


def _model_execution_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    cases = report.get("case_results", [])
    call_counts: Counter[tuple[str, str]] = Counter()
    primary_success_cases = 0
    budget_checks_pass = True
    fallback_case: Mapping[str, Any] = {}
    for case in cases:
        attempts = case.get("model_attempts", [])
        if case.get("case_id") == "primary_transient_fallback":
            fallback_case = case
        if case.get("status") == "executed" and any(
            item.get("alias") == MEMORY_PRIMARY_ALIAS
            and item.get("outcome") == "success" for item in attempts
        ):
            primary_success_cases += 1
        per_stage_role: Counter[tuple[str, str]] = Counter()
        for item in attempts:
            role = item.get("role")
            stage = item.get("stage")
            if role not in {"primary", "fallback"} or stage not in {
                "formation", "adjudication",
            }:
                budget_checks_pass = False
                continue
            call_counts[(role, item["outcome"])] += 1
            per_stage_role[(role, stage)] += 1
        if len(attempts) > MEMORY_JOB_MAX_CALLS:
            budget_checks_pass = False
        if any(
            count > (PRIMARY_MAX_ATTEMPTS if role == "primary" else FALLBACK_MAX_ATTEMPTS)
            for (role, _stage), count in per_stage_role.items()
        ):
            budget_checks_pass = False

    fallback_attempts = fallback_case.get("model_attempts", [])
    injected_count = sum(
        item.get("role") == "primary"
        and item.get("stage") == "formation"
        and item.get("outcome") == "injected_transient_failure"
        for item in fallback_attempts
    )
    fallback_count = sum(item.get("role") == "fallback" for item in fallback_attempts)
    observed = fallback_case.get("observed")
    fallback_success = bool(
        isinstance(observed, Mapping) and observed.get("fallback_success") is True
    )
    fallback_budget_verified = (
        injected_count == PRIMARY_MAX_ATTEMPTS
        and 1 <= fallback_count <= FALLBACK_MAX_ATTEMPTS
        and len(fallback_attempts) <= MEMORY_JOB_MAX_CALLS
    )
    budget_checks_pass = budget_checks_pass and fallback_budget_verified
    return {
        "primary": {
            "attempts": sum(
                count for (role, _outcome), count in call_counts.items()
                if role == "primary"
            ),
            "successful_calls": call_counts[("primary", "success")],
            "successful_cases": primary_success_cases,
        },
        "fallback": {
            "attempts": sum(
                count for (role, _outcome), count in call_counts.items()
                if role == "fallback"
            ),
            "successful_calls": call_counts[("fallback", "success")],
            "successful_cases": int(fallback_success),
        },
        "injected_failure_case": {
            "case_id": "primary_transient_fallback",
            "primary_transient_attempts": injected_count,
            "fallback_attempts": fallback_count,
            "total_model_calls": len(fallback_attempts),
            "fallback_success": fallback_success,
        },
        "budgets": {
            "primary_attempts_per_stage": PRIMARY_MAX_ATTEMPTS,
            "fallback_attempts_per_stage": FALLBACK_MAX_ATTEMPTS,
            "calls_per_job": MEMORY_JOB_MAX_CALLS,
            "observed_within_budget": budget_checks_pass,
        },
    }


async def _drop_owned_collection(
    vectors: MilvusVectorStore, *, collection_name: str,
) -> MilvusVectorStore:
    settings, embeddings = vectors._settings, vectors._embeddings
    for attempt in range(2):
        try:
            if collection_name in await vectors.connect():
                await vectors._call("drop_collection", collection_name=collection_name)
            if collection_name not in await vectors.connect():
                return vectors
        except Exception:  # noqa: BLE001 — retry once, then fail closed on cleanup.
            if attempt:
                await vectors.close()
                raise RuntimeError("temporary Milvus collection cleanup could not be verified") from None
        await vectors.close()
        vectors = MilvusVectorStore(settings, embeddings)
    await vectors.close()
    raise RuntimeError("temporary Milvus collection remained after cleanup")


async def _run(args: argparse.Namespace) -> int:
    load_dotenv(args.env_file, override=True)
    settings = Settings()
    roles = resolve_memory_roles(settings)
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    started = time.monotonic()
    temporary_root = tempfile.TemporaryDirectory(prefix="memory-v2-real-gold-")
    root = Path(temporary_root.name)
    collection_name = f"memv2gold_{uuid4().hex[:12]}"
    live_settings = settings.model_copy(update={
        "workspace_dir": str(root / "workspace"),
        "milvus_collection": collection_name,
    })
    vectors: MilvusVectorStore | None = None
    collection_owned = False
    cleanup_status = "not_created"
    setup_error_type: str | None = None
    async def execute(case: GoldCase) -> Mapping[str, Any]:
        if vectors is None:
            return {"status": "failed", "error_type": "MilvusNotInitialized"}
        case_root = root / case.case_id
        case_root.mkdir()
        return await _execute_case(
            case, database_path=case_root / "memory-v2.db", roles=roles,
            vector_store=vectors,
        )

    try:
        try:
            if not all((
                live_settings.milvus_uri,
                live_settings.milvus_token.get_secret_value(),
                live_settings.embedding_model,
                live_settings.embedding_base_url,
                live_settings.embedding_api_key.get_secret_value(),
            )):
                raise RuntimeError("Milvus or embedding configuration is incomplete")
            vectors = MilvusVectorStore(live_settings, create_embeddings(live_settings))
            if collection_name in await vectors.connect():
                raise RuntimeError("generated temporary collection name already exists")
            collection_owned = True
            await vectors.initialize()
            if not vectors.created_collection:
                raise RuntimeError("runner did not create its isolated Milvus collection")
        except Exception as error:  # noqa: BLE001 — retain a safe failed preflight report.
            setup_error_type = type(error).__name__
            report = await run_memory_gold_gate(
                lambda _case: {
                    "status": "skipped", "error_type": setup_error_type,
                },
                config_aliases={
                    "primary": MEMORY_PRIMARY_ALIAS, "fallback": MEMORY_FALLBACK_ALIAS,
                },
                repeat_of=args.repeat_of,
            )
        else:
            report = await run_memory_gold_gate(
                execute, config_aliases={
                    "primary": MEMORY_PRIMARY_ALIAS, "fallback": MEMORY_FALLBACK_ALIAS,
                }, repeat_of=args.repeat_of,
            )
    finally:
        if vectors is not None and collection_owned:
            try:
                vectors = await _drop_owned_collection(
                    vectors, collection_name=collection_name,
                )
                cleanup_status = "verified_absent"
            except Exception as error:  # noqa: BLE001 — cleanup failure blocks the gate.
                cleanup_status = "failed"
                cleanup_error_type = type(error).__name__
            else:
                cleanup_error_type = None
        else:
            cleanup_error_type = None
        if vectors is not None:
            await vectors.close()
        temporary_root.cleanup()

    if setup_error_type is not None:
        report["failures"] = sorted({*report["failures"], "milvus_gold_preflight_failed"})
        report["status"] = "failed"
    if cleanup_status == "failed":
        report["failures"] = sorted({
            *report["failures"], "temporary_milvus_cleanup_unverified",
        })
        report["status"] = "failed"
    model_execution = _model_execution_summary(report)
    report["model_execution"] = model_execution
    model_execution_failures = []
    if model_execution["primary"]["successful_cases"] == 0:
        model_execution_failures.append("primary_model_no_success")
    if not model_execution["budgets"]["observed_within_budget"]:
        model_execution_failures.append("model_retry_budget_violation")
    if model_execution_failures:
        report["failures"] = sorted({*report["failures"], *model_execution_failures})
        report["status"] = "failed"
    report["runner"] = {
        "lane": "project_memory_gold_real_models",
        "command": subprocess.list2cmdline(sys.argv),
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "tool_versions": _tool_versions(),
        "environment_capabilities": _environment_capabilities(settings, roles),
        "storage": "temporary_sqlite_per_case",
        "index": "dedicated_temporary_milvus_collection",
        "langfuse": "disabled_for_synthetic_gold_content",
        "milvus_collection": collection_name if collection_owned else None,
        "cleanup": cleanup_status,
        "cleanup_error_type": cleanup_error_type,
        "setup_error_type": setup_error_type,
    }
    report_path = args.output_dir / (
        f"memory-v2-real-gold-v{report['corpus']['version']}-"
        f"{report['code_sha'][:12]}-{report['run_id'][:8]}.json"
    )
    write_memory_gold_report(report_path, report)
    print(
        f"[memory v2 real gold] status={report['status']} "
        f"cases={report['case_counts']['executed']}/{report['case_counts']['total']} "
        f"failed={report['case_counts']['failed']} "
        f"primary_success_cases={model_execution['primary']['successful_cases']} "
        f"fallback_success={str(model_execution['injected_failure_case']['fallback_success']).lower()} "
        f"artifact={report_path}"
    )
    for case_result in report["case_results"]:
        if case_result["status"] in {"failed", "degraded"}:
            aliases = ",".join(
                attempt["alias"] for attempt in case_result.get("model_attempts", [])
            ) or "none"
            print(
                f"[memory v2 real gold] case={case_result['case_id']} "
                f"error_type={case_result.get('error_type', 'unknown')} "
                f"model_aliases={aliases}"
            )
    return 0 if report["status"] == "passed" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("docs/evidence"))
    parser.add_argument("--repeat-of")
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(_run(args))
    except Exception as error:  # noqa: BLE001 — provider bodies and config values stay private.
        print(f"[memory v2 real gold] failed ({type(error).__name__})")
        raise SystemExit(1) from None
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
