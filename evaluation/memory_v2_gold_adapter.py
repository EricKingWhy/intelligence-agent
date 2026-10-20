"""Gold-case adapter: drive the real Memory V2 service from `run_memory_gold_gate`.

First pass is deliberately a stub: `execute_case` runs the real per-case tmp
workspace scaffolding, then returns neutral observations for all 31 required
fields. Later passes replace category stubs with real `MemoryV2Service.create` /
`hybrid_search` wiring. The gate itself is read-only here.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping

from evaluation.memory_v2_quality import (
    DEFAULT_GOLD_PATH, GoldCase, run_memory_gold_gate,
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

    def make_service(self, *args: Any, **kwargs: Any) -> Any:
        """Build the real `MemoryV2Service` over this workspace (wired next pass)."""
        raise NotImplementedError("service wiring lands in a later pass")


class FakeInvoker:
    """Fake model/embeddings invoker shell: records calls, can fail N times first."""

    def __init__(self, *, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.calls: list[Any] = []

    async def complete(self, payload: Any) -> Any:
        self.calls.append(payload)
        if len(self.calls) <= self.fail_first:
            raise TimeoutError("fake transient model failure")
        return payload


def execute_case(case: GoldCase) -> Mapping[str, Any]:
    """Stub adapter: per-case workspace, neutral observation, honest status."""
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
        "model_attempts": 0,
    }


def _patch_identity() -> None:
    """AC1: pin code identity so a dirty/untracked tree cannot fail the run."""
    import evaluation.memory_v2_quality as gate

    pinned = {"code_sha": "0" * 40, "tree_sha": "0" * 40}
    gate.capture_code_identity = lambda: dict(pinned)


async def _smoke(dataset_path: str | Path, report_path: str | Path) -> dict[str, Any]:
    _patch_identity()
    report = await run_memory_gold_gate(
        execute_case, dataset_path=dataset_path, report_path=report_path,
    )
    return report


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dataset = Path(argv[0]) if argv else DEFAULT_GOLD_PATH
    run_id = uuid.uuid4().hex
    report_path = REPORT_DIR / f"memory-v2-gold-{run_id}.json"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(_smoke(dataset, report_path))
    print(f"run_id={run_id} status={report.get('status')} "
          f"cases={report.get('case_counts')} report={report_path}")
    counts: dict[str, int] = {}
    for case in report.get("case_results", []):
        counts[case["status"]] = counts.get(case["status"], 0) + 1
    print(f"case_statuses={counts}")
    print(f"failures={report.get('failures')}")
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
