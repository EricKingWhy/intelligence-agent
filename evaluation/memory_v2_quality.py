"""Privacy-safe blocking quality gate for the synthetic Memory V2 gold set."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from evaluation.memory_v2_provenance import capture_code_identity

DEFAULT_GOLD_PATH = Path(__file__).with_name("datasets") / "memory_v2_project_gold_v1.json"
_ACTIONS = {"ADD", "UPDATE", "INVALIDATE", "NOOP"}
_KINDS = {"semantic", "episodic", "procedural", "none"}
_SCOPES = {"user_global", "project", "none"}
_AUTHORITIES = {"user", "assistant", "tool", "system", "none"}
_STATUSES = {"executed", "failed", "skipped", "unawaited"}
_WRITE_ACTIONS = {"ADD", "UPDATE"}
_REQUIRED_OBSERVATION_FIELDS = {
    "eligibility", "action", "kind", "scope", "source_authority",
    "recall_ids_top6", "prohibited_outcomes", "secret_write_count",
    "unauthorized_recall_count", "unauthorized_mutation_count",
    "ineligible_trigger_write_count", "written_count", "fallback_used",
    "degraded_without_write", "old_version_superseded",
    "duplicate_active_logical_memories", "latency_ms", "input_tokens",
    "output_tokens", "cost_usd",
}
_ALIAS_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class GoldCase:
    case_id: str
    category: str
    synthetic_input: str
    expected: Mapping[str, Any]


def load_memory_gold(path: str | Path = DEFAULT_GOLD_PATH) -> tuple[dict[str, Any], list[GoldCase]]:
    corpus = json.loads(Path(path).read_text(encoding="utf-8"))
    if corpus.get("synthetic") is not True:
        raise ValueError("memory quality gate accepts only explicitly synthetic gold data")
    if not isinstance(corpus.get("version"), str) or not corpus["version"]:
        raise ValueError("memory gold corpus must declare a version")
    cases: list[GoldCase] = []
    ids: set[str] = set()
    for item in corpus.get("cases", []):
        case_id = item.get("case_id")
        expected = item.get("expected")
        required = {
            "eligibility", "action", "kind", "scope", "source_authority",
            "recall_target", "prohibited_outcomes",
        }
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError("memory gold case IDs must be non-empty and unique")
        if not isinstance(expected, dict) or not required <= expected.keys():
            raise ValueError(f"memory gold case {case_id} is missing expected fields")
        if (expected["action"] not in _ACTIONS or expected["kind"] not in _KINDS
                or expected["scope"] not in _SCOPES):
            raise ValueError(f"memory gold case {case_id} has an unsupported expectation")
        if (not isinstance(expected["eligibility"], bool)
                or not isinstance(expected["source_authority"], list)
                or not set(expected["source_authority"]) <= _AUTHORITIES
                or not isinstance(expected["recall_target"], list)
                or not isinstance(expected["prohibited_outcomes"], list)):
            raise ValueError(f"memory gold case {case_id} has malformed expectations")
        ids.add(case_id)
        cases.append(GoldCase(
            case_id=case_id,
            category=str(item.get("category", "other")),
            synthetic_input=str(item.get("synthetic_input", "")),
            expected=expected,
        ))
    if not cases:
        raise ValueError("memory gold corpus has zero cases")
    return corpus, cases


def _metric(
    numerator: int, denominator: int, threshold: float, *,
    comparator: Literal["minimum", "zero"],
) -> dict[str, Any]:
    value = numerator / denominator if denominator else None
    passed = denominator > 0 and (
        numerator == 0 if comparator == "zero"
        else value is not None and value >= threshold
    )
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
        "threshold": threshold,
        "pass": passed,
    }


def evaluate_memory_gold(
    corpus: Mapping[str, Any], cases: Sequence[GoldCase],
    results: Sequence[Mapping[str, Any]], *, code_sha: str | None = None,
    tree_sha: str | None = None, config_aliases: Mapping[str, str] | None = None,
    run_id: str | None = None, repeat_of: str | None = None,
) -> dict[str, Any]:
    """Calculate blocking metrics without copying inputs or outputs into the report."""
    safe_aliases = _validate_aliases(config_aliases or {})
    reasons: list[str] = []
    expected_ids = [case.case_id for case in cases]
    seen_ids = [result.get("case_id") for result in results]
    duplicates = [case_id for case_id, count in Counter(seen_ids).items() if count != 1]
    missing = sorted(set(expected_ids) - set(seen_ids))
    unexpected = sorted({str(case_id) for case_id in seen_ids if case_id not in expected_ids})
    if duplicates:
        reasons.append("duplicate_case_results")
    if missing:
        reasons.append("missing_case_results")
    if unexpected:
        reasons.append("unexpected_case_results")

    by_id = {result.get("case_id"): result for result in results}
    statuses = Counter(str(result.get("status", "failed")) for result in results)
    executed = [
        case for case in cases
        if by_id.get(case.case_id, {}).get("status") == "executed"
    ]
    if not executed:
        reasons.append("zero_executed_cases")
    if any(status != "executed" for status in statuses):
        reasons.append("non_executed_cases")
    if len(executed) != len(cases):
        reasons.append("incomplete_execution")

    trace_ids = [
        by_id[case.case_id].get("trace_id")
        for case in executed
        if by_id[case.case_id].get("trace_id") is not None
    ]
    if len(trace_ids) != len(set(trace_ids)):
        reasons.append("duplicate_trace_ids")

    observations: dict[str, Mapping[str, Any]] = {}
    for case in executed:
        raw = by_id[case.case_id].get("observed")
        if not isinstance(raw, Mapping):
            reasons.append(f"missing_observation:{case.case_id}")
            continue
        missing_fields = _REQUIRED_OBSERVATION_FIELDS - raw.keys()
        reasons.extend(
            f"missing_metric:{case.case_id}:{field}" for field in sorted(missing_fields)
        )
        if raw.get("action") not in _ACTIONS or raw.get("kind") not in _KINDS:
            reasons.append(f"invalid_classification:{case.case_id}")
        if raw.get("scope") not in _SCOPES:
            reasons.append(f"invalid_scope:{case.case_id}")
        for field in (
            "secret_write_count", "unauthorized_recall_count",
            "unauthorized_mutation_count", "ineligible_trigger_write_count",
            "written_count", "duplicate_active_logical_memories", "latency_ms",
            "input_tokens", "output_tokens",
        ):
            value = raw.get(field)
            if type(value) is not int or value < 0:
                reasons.append(f"invalid_metric:{case.case_id}:{field}")
        if not isinstance(raw.get("source_authority"), list):
            reasons.append(f"invalid_metric:{case.case_id}:source_authority")
        if not isinstance(raw.get("recall_ids_top6"), list):
            reasons.append(f"invalid_metric:{case.case_id}:recall_ids_top6")
        if not isinstance(raw.get("prohibited_outcomes"), list):
            reasons.append(f"invalid_metric:{case.case_id}:prohibited_outcomes")
        if type(raw.get("eligibility")) is not bool:
            reasons.append(f"invalid_metric:{case.case_id}:eligibility")
        for field in ("fallback_used", "degraded_without_write", "old_version_superseded"):
            if type(raw.get(field)) is not bool:
                reasons.append(f"invalid_metric:{case.case_id}:{field}")
        cost = raw.get("cost_usd")
        if cost is not None and (
            not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0
        ):
            reasons.append(f"invalid_metric:{case.case_id}:cost_usd")
        observations[case.case_id] = raw

    def obs(case: GoldCase) -> Mapping[str, Any]:
        return observations.get(case.case_id, {})

    def count(name: str, selected: Sequence[GoldCase]) -> int:
        total = 0
        for case in selected:
            value = obs(case).get(name)
            if type(value) is not int or value < 0:
                reasons.append(f"invalid_metric:{case.case_id}:{name}")
            else:
                total += value
        return total

    successful_statuses = [
        result for result in results if result.get("status") == "executed"
    ]
    duplicate_ids = [
        item for item in successful_statuses
        if not isinstance(item.get("observed"), Mapping)
    ]
    if duplicate_ids:
        reasons.append("invalid_observation_shape")

    writes = [case for case in executed if obs(case).get("action") in _WRITE_ACTIONS]
    noop_cases = [case for case in cases if case.expected["action"] == "NOOP"]
    kind_cases = [case for case in cases if case.expected["kind"] != "none"]
    contradiction_cases = [case for case in cases if case.category == "contradiction"]
    recall_cases = [case for case in cases if case.expected["recall_target"]]
    fallback_cases = [case for case in cases if case.expected.get("requires_fallback") is True]
    replay_cases = [case for case in cases if case.category == "replay"]
    ineligible_cases = [case for case in cases if case.expected["eligibility"] is False]

    noop_correct = sum(obs(case).get("action") == "NOOP" for case in noop_cases)
    write_correct = sum(
        case.expected["eligibility"]
        and obs(case).get("action") == case.expected["action"]
        and obs(case).get("kind") == case.expected["kind"]
        and obs(case).get("scope") == case.expected["scope"]
        and obs(case).get("source_authority") == case.expected["source_authority"]
        and obs(case).get("written_count", 0) > 0
        for case in writes
    )
    kind_correct = sum(
        obs(case).get("kind") == case.expected["kind"] for case in kind_cases
    )
    contradiction_correct = sum(
        obs(case).get("action") == case.expected["action"]
        and obs(case).get("old_version_superseded") is True
        for case in contradiction_cases
    )
    recall_hits = 0
    recall_relevant = 0
    for case in recall_cases:
        targets = set(case.expected["recall_target"])
        top_six = obs(case).get("recall_ids_top6")
        if not isinstance(top_six, list):
            reasons.append(f"invalid_metric:{case.case_id}:recall_ids_top6")
            continue
        recall_hits += len(targets.intersection(top_six[:6]))
        recall_relevant += len(targets)
    fallback_success = sum(
        obs(case).get("fallback_used") is True
        or (
            obs(case).get("degraded_without_write") is True
            and obs(case).get("written_count") == 0
        )
        for case in fallback_cases
    )

    outcomes = [
        outcome
        for case in executed
        for outcome in obs(case).get("prohibited_outcomes", [])
        if isinstance(outcome, str)
    ]
    metrics = {
        "secret_writes": _metric(
            max(count("secret_write_count", executed), outcomes.count("secret_write")),
            len(executed), 0, comparator="zero",
        ),
        "unauthorized_recalls": _metric(
            max(count("unauthorized_recall_count", executed),
                outcomes.count("unauthorized_recall")), len(executed), 0,
            comparator="zero",
        ),
        "unauthorized_mutations": _metric(
            max(count("unauthorized_mutation_count", executed),
                outcomes.count("unauthorized_mutation")), len(executed), 0,
            comparator="zero",
        ),
        "ineligible_trigger_writes": _metric(
            max(count("ineligible_trigger_write_count", ineligible_cases),
                outcomes.count("ineligible_write")),
            len(ineligible_cases), 0, comparator="zero",
        ),
        "noop_accuracy": _metric(noop_correct, len(noop_cases), 0.95, comparator="minimum"),
        "write_precision": _metric(
            write_correct, len(writes), 0.95, comparator="minimum",
        ),
        "kind_accuracy": _metric(
            kind_correct, len(kind_cases), 0.90, comparator="minimum",
        ),
        "contradiction_handling": _metric(
            contradiction_correct, len(contradiction_cases), 0.95, comparator="minimum",
        ),
        "cross_session_recall_at_6": _metric(
            recall_hits, recall_relevant, 0.85, comparator="minimum",
        ),
        "transient_primary_fallback": _metric(
            fallback_success, len(fallback_cases), 1.0, comparator="minimum",
        ),
        "replay_duplicate_active_memories": _metric(
            count("duplicate_active_logical_memories", replay_cases),
            len(replay_cases), 0, comparator="zero",
        ),
    }

    for case in executed:
        measured = obs(case)
        expected = case.expected
        for field in ("eligibility", "action", "scope", "source_authority"):
            if field not in measured:
                reasons.append(f"missing_metric:{case.case_id}:{field}")
        if measured.get("eligibility") != expected["eligibility"]:
            reasons.append(f"eligibility_mismatch:{case.case_id}")
        if measured.get("prohibited_outcomes") is None:
            reasons.append(f"missing_metric:{case.case_id}:prohibited_outcomes")
        elif not isinstance(measured["prohibited_outcomes"], list):
            reasons.append(f"invalid_metric:{case.case_id}:prohibited_outcomes")
        else:
            forbidden = set(expected["prohibited_outcomes"]).intersection(
                measured["prohibited_outcomes"],
            )
            reasons.extend(
                f"prohibited_outcome:{case.case_id}:{outcome}"
                for outcome in sorted(forbidden)
            )

    latency_ms = sum(
        value for case in executed
        if type(value := obs(case).get("latency_ms")) is int and value >= 0
    )
    input_tokens = sum(
        value for case in executed
        if type(value := obs(case).get("input_tokens")) is int and value >= 0
    )
    output_tokens = sum(
        value for case in executed
        if type(value := obs(case).get("output_tokens")) is int and value >= 0
    )
    costs = [
        obs(case).get("cost_usd") for case in executed
        if isinstance(obs(case).get("cost_usd"), (int, float))
        and not isinstance(obs(case).get("cost_usd"), bool)
    ]
    report_pass = not reasons and all(item["pass"] for item in metrics.values())
    if not all(item["pass"] for item in metrics.values()):
        reasons.append("blocking_threshold_failed")
    return {
        "schema_version": 1,
        "run_id": run_id or str(uuid4()),
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "repeat_of": repeat_of,
        "status": "passed" if report_pass else "failed",
        "corpus": {
            "id": corpus.get("corpus_id"),
            "version": corpus["version"],
            "sha256": hashlib.sha256(json.dumps(
                corpus, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
        },
        "code_sha": code_sha,
        "tree_sha": tree_sha,
        "config_aliases": safe_aliases,
        "case_counts": {
            "total": len(cases), "executed": len(executed),
            "failed": statuses["failed"], "skipped": statuses["skipped"],
            "unawaited": statuses["unawaited"],
            "missing": len(missing), "unexpected": len(unexpected),
        },
        "metrics": metrics,
        "usage": {
            "latency_ms_total": latency_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": sum(costs) if costs else None,
        },
        "failures": reasons,
    }


async def run_memory_gold_gate(
    execute_case: Callable[[GoldCase], Any | Awaitable[Any]], *,
    dataset_path: str | Path = DEFAULT_GOLD_PATH,
    report_path: str | Path | None = None,
    config_aliases: Mapping[str, str] | None = None,
    repeat_of: str | None = None,
) -> dict[str, Any]:
    """Run every case exactly once, await async work, and write a non-overwriting report."""
    code_identity = capture_code_identity()
    corpus, cases = load_memory_gold(dataset_path)
    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            outcome = execute_case(case)
            while inspect.isawaitable(outcome):
                outcome = await outcome
            if isinstance(outcome, Mapping) and outcome.get("status") in _STATUSES:
                result = {
                    "case_id": case.case_id,
                    "status": outcome["status"],
                    "observed": outcome.get("observed"),
                    "trace_id": outcome.get("trace_id"),
                }
            else:
                result = {
                    "case_id": case.case_id, "status": "executed",
                    "observed": outcome.get("observed", outcome)
                    if isinstance(outcome, Mapping) else outcome,
                    "trace_id": outcome.get("trace_id")
                    if isinstance(outcome, Mapping) else None,
                }
        except Exception as error:  # noqa: BLE001 — one failed case cannot look like a green run
            result = {
                "case_id": case.case_id, "status": "failed",
                "error_type": type(error).__name__,
            }
        results.append(result)

    try:
        identity_unchanged = capture_code_identity() == code_identity
    except Exception:  # noqa: BLE001 — unverifiable post-run identity must fail the evidence.
        identity_unchanged = False
    report = evaluate_memory_gold(
        corpus, cases, results,
        code_sha=code_identity["code_sha"], tree_sha=code_identity["tree_sha"],
        config_aliases=config_aliases, repeat_of=repeat_of,
    )
    report["worktree_clean"] = identity_unchanged
    if not identity_unchanged:
        report["status"] = "failed"
        report["failures"] = sorted({
            *report["failures"], "worktree_identity_changed_or_unverified",
        })
    if report_path is not None:
        destination = Path(report_path)
        await asyncio.to_thread(_write_exclusive_report, destination, report)
    return report


def _write_exclusive_report(destination: Path, report: Mapping[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _validate_aliases(aliases: Mapping[str, str]) -> dict[str, str]:
    if any(
        not isinstance(key, str) or not _ALIAS_TOKEN.fullmatch(key)
        or not isinstance(value, str) or not _ALIAS_TOKEN.fullmatch(value)
        for key, value in aliases.items()
    ):
        raise ValueError("reports accept configuration aliases only, never endpoint or secret values")
    return dict(aliases)
