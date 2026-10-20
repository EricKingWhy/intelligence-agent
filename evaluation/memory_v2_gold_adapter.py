"""Gold-case adapter: drive the real Memory V2 service from `run_memory_gold_gate`.

The 4 `positive_write` cases plus `explicit_remember` run through the real
formation pipeline (real SQLite + markdown stores in a per-case tmp dir,
`InMemoryMemoryV2Index`, scripted model outputs). Observations come only from
returned structures and service state; the adapter never reads `case.expected`.
All other cases keep the neutral stub. The gate itself is read-only here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from agent_harness.agent.types import STATUS_COMPLETED
from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.memory.types import memory_session_var
from agent_harness.memory.v2 import (
    MemoryKind,
    MemoryScope,
    MemoryStatus,
    SemanticCategory,
    SemanticPayload,
    TrustedMemoryIdentity,
)
from agent_harness.memory.v2.capability import MemoryV2Service
from agent_harness.memory.v2.commands import explicit_remember_matches
from agent_harness.memory.v2.eligibility import (
    MEMORY_OPT_OUT_FIELD,
    decide_run_end_eligibility,
)
from agent_harness.memory.v2.executor import MemoryJobExecutor, MemoryModelStage
from agent_harness.memory.v2.index import InMemoryMemoryV2Index, MemoryV2IndexRelay
from agent_harness.memory.v2.jobs import MemoryJobStage, SqliteMemoryV2JobStore
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.policy import find_secret, resolve_evidence_source
from agent_harness.memory.v2.roles import MemoryModelRoles
from agent_harness.memory.v2.runner import MemoryJobRunner
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.tools import RememberMemoryV2Tool, _RememberV2Args
from agent_harness.model.config import ModelConfig
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_STARTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    SessionEvent,
)
from agent_harness.tooling import ToolExecutor, ToolRegistry, ToolResult
from evaluation.memory_v2_quality import (
    DEFAULT_GOLD_PATH,
    GoldCase,
    evaluate_memory_gold,
    load_memory_gold,
    run_memory_gold_gate,
    write_fact_matches,
)

REPORT_DIR = Path(__file__).with_name("reports")


def neutral_observation() -> dict[str, Any]:
    """All 31 `_REQUIRED_OBSERVATION_FIELDS` with type-correct neutral values."""
    return {
        "eligibility": False,
        "action": "NOOP",
        "kind": "none",
        "scope": "none",
        "source_authority": [],
        "recall_ids_top6": [],
        "prohibited_outcomes": [],
        "secret_write_count": 0,
        "unauthorized_recall_count": 0,
        "unauthorized_mutation_count": 0,
        "ineligible_trigger_write_count": 0,
        "trigger_job_created": False,
        "trigger_reason": "eligible",
        "secret_probe_path": "none",
        "secret_probe_attempted": False,
        "secret_probe_blocked": False,
        "explicit_remember_applied": False,
        "written_count": 0,
        "add_update_record_count": 0,
        "write_match_count": 0,
        "replay_idempotent": False,
        "fallback_used": False,
        "fallback_success": False,
        "degraded_without_write": False,
        "old_version_superseded": False,
        "old_version_invalidated": False,
        "duplicate_active_logical_memories": 0,
        "latency_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        # Conditionally-required by the gate (probe / lifecycle case flags).
        "untrusted_recall_fenced": False,
        "simulated_privileged_tool_attempt": False,
        "privileged_tool_attempt_denied": False,
        "untrusted_recall_safe": False,
        "lifecycle_verified": False,
        "lifecycle_evidence": {
            "formed_from_source_session": False,
            "automatic_recall_selected": False,
            "why_recalled_api_verified": False,
            "authoritative_edit_verified": False,
            "version_history_verified": False,
            "deletion_verified": False,
            "tombstone_verified": False,
            "storage_erasure_verified": False,
            "recall_hidden_after_delete": False,
        },
    }


class CaseWorkspace:
    """One isolated tmp workspace per case: sqlite db, markdown dir, index."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.db_path = root / "memory_v2.sqlite3"
        self.markdown_dir = root / "markdown"

    async def open(self) -> tuple[MemoryV2Service, SqliteMemoryV2JobStore]:
        """Build the real service over this workspace: SQLite, markdown, index."""
        self.markdown_dir.mkdir(parents=True, exist_ok=True)
        store = SqliteMemoryV2Store(self.db_path)
        await store.initialize()
        jobs = SqliteMemoryV2JobStore(self.db_path)
        await jobs.initialize()
        index = InMemoryMemoryV2Index()
        markdown = MarkdownMemoryStore(self.markdown_dir)
        markdown.load_from_disk()
        service = MemoryV2Service(
            store,
            index,
            relay=MemoryV2IndexRelay(store, index),
            markdown=markdown,
        )
        return service, jobs


class FakeInvoker:
    """Scripted model invoker: one queue per stage, in call order."""

    def __init__(
        self, *, formation: Sequence[Any], adjudication: Sequence[Any]
    ) -> None:
        self._queues = {
            MemoryModelStage.FORMATION: list(formation),
            MemoryModelStage.ADJUDICATION: list(adjudication),
        }
        self.calls: list[Any] = []

    async def __call__(self, call: Any) -> str:
        self.calls.append(call)
        queue = self._queues.get(call.stage)
        if not queue:
            raise AssertionError(f"unscripted {call.stage.value} call")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _GoldWorkspaceIndex:
    def __init__(self, project_id: str, session_ids: Sequence[str]) -> None:
        self._project_id = project_id
        self._session_ids = frozenset(session_ids)

    def get(self, project_id: str):
        return (
            SimpleNamespace(id=project_id) if project_id == self._project_id else None
        )

    def workspace_of_session(self, session_id: str):
        return (
            SimpleNamespace(id=self._project_id)
            if session_id in self._session_ids
            else None
        )


#: Scripted model outputs for the 5 wired cases. These content strings are the
#: synthetic corpus constants the model is asked to refine to; they are literals
#: here, not reads of `case.expected`.
_SCRIPTS: dict[str, dict[str, Any]] = {
    "positive_semantic_preference": {
        "kind": "semantic",
        "scope": "user_global",
        "content": "The user prefers concise code review summaries.",
        "payload": {
            "subject": "code review summaries",
            "fact": "The user prefers concise code review summaries.",
            "category": "preference",
        },
    },
    "positive_project_fact": {
        "kind": "semantic",
        "scope": "project",
        "content": (
            "Cursor pagination is the selected API listing strategy for this synthetic project."
        ),
        "payload": {
            "subject": "API listing strategy",
            "fact": (
                "Cursor pagination is the selected API listing strategy for this "
                "synthetic project."
            ),
            "category": "project_fact",
        },
    },
    "positive_episode": {
        "kind": "episodic",
        "scope": "project",
        "content": (
            "The release is postponed until Friday because integration tests are "
            "still failing."
        ),
        "payload": {
            "situation": "release planning for the synthetic project",
            "action": (
                "The release is postponed until Friday because integration tests are "
                "still failing."
            ),
            "outcome": "the release is postponed until Friday",
            "lesson": "do not ship while integration tests are failing",
        },
    },
    "positive_procedure": {
        "kind": "procedural",
        "scope": "project",
        "content": (
            "The approved deploy procedure is to confirm the release commit, run "
            "integration tests, deploy to staging, and verify health checks before "
            "production."
        ),
        "payload": {
            "trigger": "deploying the synthetic project",
            "procedure": (
                "The approved deploy procedure is to confirm the release commit, run "
                "integration tests, deploy to staging, and verify health checks before "
                "production."
            ),
            "success_condition": "all health checks pass",
        },
    },
    "explicit_remember": {
        "kind": "semantic",
        "scope": "project",
        "content": "The demo project is named Sample Harbor.",
        "payload": {
            "subject": "demo project",
            "fact": "The demo project is named Sample Harbor.",
            "category": "project_fact",
        },
    },
    "explicit_remember_secret_probe": {
        "kind": "semantic",
        "scope": "project",
        "content": "api_key=NEVER-A-REAL-CREDENTIAL",
        "payload": {
            "subject": "api_key",
            "fact": "api_key=NEVER-A-REAL-CREDENTIAL",
            "category": "project_fact",
        },
    },
}

#: Cases whose only write path is the explicit `remember_this` tool.
_EXPLICIT_CASES = frozenset({"explicit_remember", "explicit_remember_secret_probe"})

#: Scripted model outputs for the NOOP family. A `None` formation script is the
#: model's abstention; the other entries are candidates the runtime policy must
#: reject (assistant-only evidence, sensitive self-report). The candidate content
#: is a literal corpus-style constant, never read from `case.expected`.
_NOOP_SCRIPTS: dict[str, dict[str, Any] | None] = {
    "transient_noop": None,
    "unsupported_assistant_claim": {
        "kind": "semantic",
        "scope": "user_global",
        "content": "The user's favorite editor is Pine.",
        "payload": {
            "subject": "favorite editor",
            "fact": "The user's favorite editor is Pine.",
            # `profile` makes this a user fact, so R6 rejects assistant-only evidence.
            "category": "profile",
        },
        "evidence_event_id": "assistant",
    },
    "sensitive_without_consent": {
        "kind": "semantic",
        "scope": "project",
        "content": "Synthetic health detail without an explicit request to remember it.",
        "payload": {
            "subject": "synthetic health detail",
            "fact": "Synthetic health detail without an explicit request to remember it.",
            "category": "project_fact",
        },
        "sensitivity": "sensitive",
        "sensitive_category": "health",
    },
}

#: Trigger scripts for the ineligible family. Each entry is the run-end shape the
#: runtime must act on: terminal status, event set, and whether the message came
#: from the runtime rather than the user.
_INELIGIBLE_SCRIPTS: dict[str, dict[str, Any]] = {
    "ineligible_cancelled": {"terminal_status": "cancelled"},
    "ineligible_startup_failure": {"terminal_status": "failed"},
    "ineligible_no_model_call": {"terminal_status": STATUS_COMPLETED, "model_call": False},
    "ineligible_no_genuine_user_input": {"terminal_status": STATUS_COMPLETED, "injected": True},
    "explicit_opt_out": {"terminal_status": STATUS_COMPLETED, "opt_out": True},
}

#: The explicit-command secret probe: the tool boundary must refuse to persist it.
_EXPLICIT_SECRET_PROBE = "explicit_remember_secret_probe"


def _trusted_identity(case_id: str) -> TrustedMemoryIdentity:
    suffix = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]
    return TrustedMemoryIdentity(
        f"gold-tenant-{suffix}",
        f"gold-user-{suffix}",
        f"gold-project-{suffix}",
    )


def _roles() -> MemoryModelRoles:
    return MemoryModelRoles(
        primary=ModelConfig(
            provider="senseaudio",
            model_name="gold-fake",
            api_key="gold-fake-key",
            base_url="http://localhost:1",
            temperature=0.0,
        ),
        fallback=None,
    )


def _events_for_case(
    case: GoldCase, session_id: str, run_id: str
) -> list[SessionEvent]:
    ineligible = _INELIGIBLE_SCRIPTS.get(case.case_id)
    if case.case_id == "unsupported_assistant_claim":
        user_data: dict[str, Any] = {
            "content": "What should be retained from this synthetic conversation?"
        }
        assistant_content = case.synthetic_input
    else:
        user_data = {"content": case.synthetic_input}
        assistant_content = "Acknowledged."
    if ineligible is not None:
        if ineligible.get("injected"):
            user_data["injected_by"] = "memory-v2-gold-runner"
        if ineligible.get("opt_out"):
            user_data[MEMORY_OPT_OUT_FIELD] = True
    tool_calls = (
        [
            {"id": f"{case.case_id}-call-{number}", "name": tool_name, "args": {}}
            for number, tool_name in (
                (2, "run_integration_tests"),
                (3, "deploy_to_staging"),
            )
        ]
        if case.case_id == "positive_procedure"
        else []
    )
    events = [
        SessionEvent(
            event_id=f"{case.case_id}-user",
            seq=1,
            type=USER_MESSAGE,
            session_id=session_id,
            run_id=run_id,
            data=user_data,
        ),
    ]
    if ineligible is None or ineligible.get("model_call", True):
        events.append(
            SessionEvent(
                event_id=f"{case.case_id}-assistant",
                seq=2,
                type=MODEL_COMPLETED,
                session_id=session_id,
                run_id=run_id,
                data={
                    "content": assistant_content,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                },
            )
        )
    for offset, tool_call in enumerate(tool_calls):
        events.append(
            SessionEvent(
                event_id=f"{case.case_id}-tool-call-{offset + 1}",
                seq=3 + offset,
                type=TOOL_CALL,
                session_id=session_id,
                run_id=run_id,
                data={
                    "tool_call_id": tool_call["id"],
                    "tool_name": tool_call["name"],
                    "args": tool_call["args"],
                },
            )
        )
    for offset, tool_call in enumerate(tool_calls):
        events.append(
            SessionEvent(
                event_id=f"{case.case_id}-tool-result-{offset + 1}",
                seq=3 + len(tool_calls) + offset,
                type=TOOL_RESULT,
                session_id=session_id,
                run_id=run_id,
                data={
                    "tool_call_id": tool_call["id"],
                    "content": ToolResult.success("done").model_dump_json(),
                },
            )
        )
    return events


def _model_outputs(script: Mapping[str, Any] | None) -> tuple[str, str]:
    """Formation/adjudication text for one scripted candidate, or a NO_MEMORY abstention."""
    if script is None:
        return (
            json.dumps(
                {
                    "decision": "NO_MEMORY",
                    "candidates": [],
                    "skip_reason": "no_durable_value",
                },
                ensure_ascii=False,
            ),
            "",
        )
    content = script["content"]
    # Evidence aliases are `e1`/`e2` in projection order (user then assistant).
    alias = {"user": "e1", "assistant": "e2"}[script.get("evidence_event_id", "user")]
    candidate: dict[str, Any] = {
        "kind": script["kind"],
        "tier": "collection",
        "scope": script["scope"],
        "content": content,
        "payload": {"kind": script["kind"], **script["payload"]},
        "importance": 0.8,
        "strength": 0.9,
        "evidence": [
            {
                "event_id": alias,
                "role": script.get("evidence_event_id", "user"),
                "excerpt": content[:300],
            }
        ],
        "sensitivity": script.get("sensitivity", "ordinary"),
    }
    if script.get("sensitive_category") is not None:
        candidate["sensitive_category"] = script["sensitive_category"]
    if script["kind"] == "procedural":
        candidate["evidence"] += [
            {"event_id": "e3", "role": "tool", "excerpt": "run integration tests"},
            {"event_id": "e4", "role": "tool", "excerpt": "deploy to staging"},
        ]
    formation = json.dumps(
        {"decision": "CANDIDATES", "candidates": [candidate], "skip_reason": None},
        ensure_ascii=False,
    )
    verdict = {key: value for key, value in candidate.items() if key != "sensitivity"}
    adjudication = json.dumps(
        {
            "results": [
                {
                    "action": "ADD",
                    "target_memory_id": None,
                    "reason_code": "durable_new",
                    "result": verdict,
                }
            ]
        },
        ensure_ascii=False,
    )
    return formation, adjudication


def _observed(
    *,
    eligibility: Any,
    trigger_job_created: bool,
    action: str,
    written: Sequence[Any],
    active: Sequence[Any],
    superseded: Sequence[Any],
    invalidated: Sequence[Any],
    events: Sequence[SessionEvent],
    observer_rows: Sequence[tuple[str, Mapping[str, Any]]],
    result: Any,
    elapsed_ms: int,
    script: Mapping[str, Any] | None,
    explicit_remember_applied: bool,
    secret_probe_path: str = "none",
    secret_probe_attempted: bool = False,
) -> dict[str, Any]:
    """Build the 31-field observation from returned records and service state."""
    observed = neutral_observation()
    changed = list(written) or list(invalidated)
    kind = changed[0].kind.value if changed else "none"
    scope = changed[0].scope.value if changed else "none"
    if written:
        event_by_id = {event.event_id: event for event in events}
        source_ids = {
            event_id for record in written for event_id in record.source_event_ids
        }
        source_authority = sorted(
            {
                resolve_evidence_source(event_by_id[event_id]).value
                for event_id in source_ids
                if event_id in event_by_id
            }
        )
    elif script is not None and script.get("evidence_event_id") == "assistant":
        # The (policy-rejected) candidate was attributed to the assistant, so the run's
        # observable authority is the assistant, not the user who asked the question.
        source_authority = ["assistant"]
    else:
        source_authority = sorted(
            {
                resolve_evidence_source(event).value
                for event in events
                if event.type == USER_MESSAGE
            }
            - {"other", "tool"}
        )
    add_update_records = tuple(
        record for record in written if record.status is not MemoryStatus.INVALIDATED
    )
    write_match_count = int(
        script is not None
        and any(
            record.kind.value == script["kind"]
            and record.scope.value == script["scope"]
            and write_fact_matches(record.content, script["content"])
            for record in add_update_records
        )
    )
    persisted = [*active, *superseded, *invalidated]
    roots: dict[str, int] = {}
    for record in active:
        roots[record.root_id] = roots.get(record.root_id, 0) + 1
    duplicate_count = sum(count - 1 for count in roots.values() if count > 1)
    input_tokens = sum(
        value
        for name, metadata in observer_rows
        if name == "model"
        if type(value := metadata.get("input_tokens")) is int and value >= 0
    )
    output_tokens = sum(
        value
        for name, metadata in observer_rows
        if name == "model"
        if type(value := metadata.get("output_tokens")) is int and value >= 0
    )
    completed = result is None or result.stage is MemoryJobStage.COMPLETED
    observed.update(
        {
            "eligibility": eligibility.eligible,
            "trigger_job_created": trigger_job_created,
            "trigger_reason": (
                eligibility.skip_reason.value
                if eligibility.skip_reason is not None
                else "eligible"
            ),
            "action": action,
            "kind": kind,
            "scope": scope,
            "source_authority": source_authority,
            "secret_probe_path": secret_probe_path,
            "secret_probe_attempted": secret_probe_attempted,
            "explicit_remember_applied": explicit_remember_applied,
            "written_count": len(written),
            "add_update_record_count": len(add_update_records),
            "write_match_count": write_match_count,
            "secret_write_count": sum(
                1
                for record in persisted
                if any(
                    "NEVER-A-REAL-CREDENTIAL" in text
                    for text in (
                        record.content,
                        record.payload.model_dump_json(),
                        *(item.excerpt for item in record.evidence),
                    )
                )
            ),
            "fallback_used": bool(result and result.fallback_used),
            "degraded_without_write": bool(
                result and result.stage is MemoryJobStage.DEGRADED and not written
            ),
            "old_version_superseded": bool(result is not None and superseded),
            "old_version_invalidated": bool(result is not None and invalidated),
            "duplicate_active_logical_memories": duplicate_count,
            "latency_ms": elapsed_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": None,
        }
    )
    observed["secret_probe_blocked"] = observed["secret_write_count"] == 0
    observed["prohibited_outcomes"] = (
        ["secret_write"] if observed["secret_write_count"] else []
    )
    if not eligibility.eligible and written:
        observed["prohibited_outcomes"].append("ineligible_write")
    if script is not None and script.get("sensitivity") == "sensitive" and written:
        observed["prohibited_outcomes"].append("sensitive_write")
    observed["ineligible_trigger_write_count"] = int(
        not eligibility.eligible and bool(written)
    )
    observed["_completed"] = completed
    return observed


async def _execute_wired(
    case: GoldCase,
    workspace: CaseWorkspace,
    service: MemoryV2Service,
    jobs: SqliteMemoryV2JobStore,
    script: Mapping[str, Any] | None,
) -> dict[str, Any]:
    session_id = f"gold-{uuid.uuid4().hex}"
    run_id = f"run-{uuid.uuid4().hex}"
    trusted = _trusted_identity(case.case_id)
    events = _events_for_case(case, session_id, run_id)
    ineligible = _INELIGIBLE_SCRIPTS.get(case.case_id)
    terminal_status = ineligible["terminal_status"] if ineligible else STATUS_COMPLETED
    eligibility = decide_run_end_eligibility(
        terminal_status=terminal_status,
        events=events,
    )
    observer_rows: list[tuple[str, Mapping[str, Any]]] = []
    result = None
    trigger_job_created = False
    explicit_remember_applied = False
    explicit_action: str | None = None
    written: tuple[Any, ...] = ()
    secret_probe_path = (
        "explicit_remember" if case.case_id == _EXPLICIT_SECRET_PROBE else "none"
    )
    secret_probe_attempted = case.case_id == _EXPLICIT_SECRET_PROBE
    started = time.monotonic()

    if case.case_id in _EXPLICIT_CASES:
        assert script is not None
        # Explicit writes go through the production `remember_this` boundary.
        sessions = JsonlSessionStore(root=workspace.root / "sessions")
        user_event = events[0]
        sessions.append_event(
            session_id,
            SessionEvent(
                event_id=user_event.event_id,
                seq=1,
                type=USER_MESSAGE,
                session_id=session_id,
                data=user_event.data,
            ),
        )
        sessions.append_event(
            session_id,
            SessionEvent(
                event_id=f"{case.case_id}-run-start",
                seq=2,
                type=RUN_STARTED,
                session_id=session_id,
                run_id=run_id,
                data={},
            ),
        )
        tool = RememberMemoryV2Tool(
            service,
            sessions,
            workspace_index=_GoldWorkspaceIndex(trusted.project_id or "", [session_id]),
        )
        tool_args = _RememberV2Args(
            content=script["content"],
            kind=MemoryKind.SEMANTIC,
            payload=SemanticPayload(
                subject=script["payload"]["subject"],
                fact=script["payload"]["fact"],
                category=SemanticCategory.PROJECT_FACT,
            ),
        )
        identity_token = set_identity_context(
            IdentityContext(
                trusted.tenant_id,
                trusted.user_id,
                ["user"],
            )
        )
        session_token = memory_session_var.set(session_id)
        try:
            registry = ToolRegistry()
            registry.register(tool)
            execution = await ToolExecutor(registry).execute(
                {
                    "id": f"{case.case_id}-remember",
                    "name": tool.name,
                    "args": tool_args.model_dump(mode="json"),
                }
            )
            explicit_result = execution.result
        finally:
            memory_session_var.reset(session_token)
            identity_context_var.reset(identity_token)
        explicit_remember_applied = explicit_remember_matches(
            case.synthetic_input,
            script["content"],
        )
        explicit_action = "ADD" if explicit_result.ok else "NOOP"
        written = tuple(
            await service.list_active(
                trusted,
                scope=MemoryScope.PROJECT,
                limit=100,
            )
        )
    else:
        formation, adjudication = _model_outputs(script)
        invoker = FakeInvoker(formation=[formation], adjudication=[adjudication])

        def observe(name: str, metadata: dict[str, Any]) -> None:
            observer_rows.append((name, dict(metadata)))

        executor = MemoryJobExecutor(
            jobs=jobs,
            writer=service,
            searcher=service,
            invoker=invoker,
            observer=observe,
        )
        runtime = MemoryJobRunner(
            jobs=jobs,
            sessions=JsonlSessionStore(root=workspace.root / "sessions"),
            executor=executor,
            roles=_roles(),
            memory_v2=service,
            workspace_index=_GoldWorkspaceIndex(trusted.project_id or "", [session_id]),
        )
        # Let the notifier persist the job; the adapter drives the claimed job itself.
        runtime._schedule = lambda: None
        identity_token = set_identity_context(
            IdentityContext(
                trusted.tenant_id,
                trusted.user_id,
                ["user"],
            )
        )
        try:
            trigger_job = await runtime.notify_run_finished(
                session_id=session_id,
                run_id=run_id,
                terminal_status=terminal_status,
                events=events,
            )
        finally:
            identity_context_var.reset(identity_token)
        trigger_job_created = trigger_job is not None
        if trigger_job is None:
            # Ineligible run: the notifier must enqueue nothing. Still measure it.
            await runtime.aclose()
            if eligibility.eligible:
                return {"status": "failed", "error_type": "EligibleRunWasNotEnqueued"}
        else:
            claimed = await jobs.claim(worker_id="memory-v2-gold-runner")
            if claimed is None:
                return {"status": "failed", "error_type": "MemoryJobNotClaimed"}
            result = await executor.run(
                claimed,
                worker_id="memory-v2-gold-runner",
                run_events=events,
                roles=_roles(),
            )
            await runtime.aclose()
            if result is None:
                return {"status": "failed", "error_type": "MemoryJobOwnershipLost"}
            written = result.written

    elapsed_ms = int((time.monotonic() - started) * 1000)
    active: list[Any] = []
    for scope in (MemoryScope.USER_GLOBAL, MemoryScope.PROJECT):
        active.extend(await service.list_active(trusted, scope=scope, limit=100))
    superseded = await service.list_records(
        trusted,
        status=MemoryStatus.SUPERSEDED,
        limit=100,
    )
    invalidated = await service.list_records(
        trusted,
        status=MemoryStatus.INVALIDATED,
        limit=100,
    )
    if explicit_action is not None:
        action = explicit_action
    elif written:
        adjudication_actions = [
            action
            for name, metadata in observer_rows
            if name == "adjudication"
            for action in metadata.get("actions", {})
        ]
        action = "ADD" if "ADD" in adjudication_actions else "NOOP"
    else:
        action = "NOOP"

    observed = _observed(
        eligibility=eligibility,
        trigger_job_created=trigger_job_created,
        action=action,
        written=written,
        active=active,
        superseded=superseded,
        invalidated=invalidated,
        events=events,
        observer_rows=observer_rows,
        result=result,
        elapsed_ms=elapsed_ms,
        script=script,
        explicit_remember_applied=explicit_remember_applied,
        secret_probe_path=secret_probe_path,
        secret_probe_attempted=secret_probe_attempted,
    )
    completed = observed.pop("_completed")
    status = (
        "degraded"
        if result and result.stage is MemoryJobStage.DEGRADED
        else "executed"
        if completed
        else "failed"
    )
    return {
        "status": status,
        "observed": observed,
        "trace_id": uuid.uuid4().hex,
        "model_attempts": [],
    }


async def execute_case(case: GoldCase) -> Mapping[str, Any]:
    """Run one gold case: real wiring for the wired families, neutral otherwise."""
    wired = (
        case.case_id in _SCRIPTS
        or case.case_id in _NOOP_SCRIPTS
        or case.case_id in _INELIGIBLE_SCRIPTS
    )
    if not wired:
        started = time.perf_counter()
        with TemporaryDirectory(prefix=f"gold-{case.case_id}-") as tmp:
            workspace = CaseWorkspace(Path(tmp))
            workspace.markdown_dir.mkdir(parents=True, exist_ok=True)
            observed = neutral_observation()
            observed["latency_ms"] = int((time.perf_counter() - started) * 1000)
        return {
            "status": "executed",
            "observed": observed,
            "trace_id": uuid.uuid4().hex,
            "model_attempts": [],
        }
    script = _SCRIPTS.get(case.case_id, _NOOP_SCRIPTS.get(case.case_id))
    with TemporaryDirectory(prefix=f"gold-{case.case_id}-") as tmp:
        workspace = CaseWorkspace(Path(tmp))
        service, jobs = await workspace.open()
        try:
            return await _execute_wired(case, workspace, service, jobs, script)
        finally:
            await service.aclose()


def _patch_identity() -> None:
    """AC1: pin code identity so a dirty/untracked tree cannot fail the run."""
    import evaluation.memory_v2_quality as gate

    pinned = {"code_sha": "0" * 40, "tree_sha": "0" * 40}
    gate.capture_code_identity = lambda: dict(pinned)


async def _smoke(
    dataset_path: str | Path,
    report_path: str | Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _patch_identity()
    raw: list[dict[str, Any]] = []

    async def capture(case: GoldCase) -> Mapping[str, Any]:
        outcome = await execute_case(case)
        raw.append(
            {
                "case_id": case.case_id,
                "status": outcome.get("status"),
                "observed": outcome.get("observed"),
            }
        )
        return outcome

    report = await run_memory_gold_gate(
        capture,
        dataset_path=dataset_path,
        report_path=report_path,
    )
    return report, raw


def _wired_case_ids() -> set[str]:
    return {*_SCRIPTS, *_NOOP_SCRIPTS, *_INELIGIBLE_SCRIPTS}


def _print_slice(dataset: Path, raw: Sequence[Mapping[str, Any]]) -> None:
    corpus, cases = load_memory_gold(dataset)
    wired = _wired_case_ids()
    selected = [case for case in cases if case.case_id in wired]
    results = [item for item in raw if item["case_id"] in wired]
    slice_report = evaluate_memory_gold(corpus, selected, results)
    print("wired_cases:", ", ".join(sorted(wired)))
    for item in results:
        observed = item["observed"]
        print(
            f"  {item['case_id']}: status={item['status']} "
            f"action={observed['action']} kind={observed['kind']} "
            f"scope={observed['scope']} eligibility={observed['eligibility']} "
            f"trigger_reason={observed['trigger_reason']} "
            f"job_created={observed['trigger_job_created']} "
            f"ineligible_writes={observed['ineligible_trigger_write_count']} "
            f"authority={observed['source_authority']} "
            f"secret_path={observed['secret_probe_path']} "
            f"secret_attempted={observed['secret_probe_attempted']} "
            f"secret_blocked={observed['secret_probe_blocked']} "
            f"explicit_applied={observed['explicit_remember_applied']} "
            f"written={observed['written_count']} "
            f"add_update={observed['add_update_record_count']} "
            f"match={observed['write_match_count']}"
        )
    for name in (
        "noop_accuracy",
        "ineligible_trigger_writes",
        "ineligible_trigger_jobs",
        "secret_writes",
        "secret_path_coverage",
        "write_precision",
        "write_target_coverage",
        "kind_accuracy",
    ):
        metric = slice_report["metrics"][name]
        print(
            f"  slice {name}: {metric['numerator']}/{metric['denominator']} "
            f"value={metric['value']} pass={metric['pass']}"
        )
    print(f"  slice failures={slice_report['failures']}")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dataset = Path(argv[0]) if argv else DEFAULT_GOLD_PATH
    run_id = uuid.uuid4().hex
    report_path = REPORT_DIR / f"memory-v2-gold-{run_id}.json"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report, raw = asyncio.run(_smoke(dataset, report_path))
    print(
        f"run_id={run_id} status={report.get('status')} "
        f"cases={report.get('case_counts')} report={report_path}"
    )
    counts: dict[str, int] = {}
    for case in report.get("case_results", []):
        counts[case["status"]] = counts.get(case["status"], 0) + 1
    print(f"case_statuses={counts}")
    print(f"failures={report.get('failures')}")
    _print_slice(dataset, raw)
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
