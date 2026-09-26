"""Load upstream public memory QA sets and store comparable, content-free baselines.

The dataset files are supplied by the operator and never copied into this repository.
LoCoMo is CC BY-NC 4.0 and is restricted to non-commercial internal evaluation.
LongMemEval's cleaned dataset is MIT licensed. No question, answer, or conversation
content is sent to Langfuse or written to a baseline artifact by this module.
"""

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
from typing import Any
from uuid import uuid4

from evaluation.memory_v2_provenance import capture_code_identity

_BENCHMARKS = {
    "locomo": {
        "name": "LoCoMo",
        "url": "https://github.com/snap-research/locomo",
        "license": "CC BY-NC 4.0",
        "non_commercial_only": True,
    },
    "longmemeval": {
        "name": "LongMemEval",
        "url": "https://github.com/xiaowu0162/LongMemEval",
        "license": "MIT",
        "non_commercial_only": False,
    },
}
_OBSERVATION_FIELDS = {
    "answer_correct", "retrieved_session_ids", "stored_record_count",
    "pollution_count", "injected_tokens", "latency_ms", "input_tokens",
    "output_tokens", "cost_usd",
}
_ALIAS_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True, slots=True)
class PublicTurn:
    role: str
    content: str
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class PublicSession:
    session_id: str
    timestamp: str | None
    turns: tuple[PublicTurn, ...]


@dataclass(frozen=True, slots=True)
class PublicBenchmarkCase:
    benchmark: str
    case_id: str
    category: str
    sessions: tuple[PublicSession, ...]
    question: str
    expected_answer: str | None
    relevant_session_ids: tuple[str, ...]
    relevant_turn_ids: tuple[str, ...] = ()
    expected_abstention: bool = False


def load_locomo(
    path: str | Path, *, limit: int | None = None,
) -> list[PublicBenchmarkCase]:
    """Read the official LoCoMo JSON shape without vendoring or modifying it."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    samples = raw if isinstance(raw, list) else raw.get("data")
    if not isinstance(samples, list):
        raise TypeError("LoCoMo input must be a list of conversation samples")
    result: list[PublicBenchmarkCase] = []
    for sample in samples:
        conversation = sample.get("conversation")
        if not isinstance(conversation, dict):
            raise TypeError("LoCoMo sample is missing conversation sessions")
        sessions, turn_to_session = _locomo_sessions(conversation)
        qa_items = sample.get("qa")
        if not isinstance(qa_items, list):
            raise TypeError("LoCoMo sample is missing annotated qa items")
        sample_id = str(sample.get("sample_id", "sample"))
        for index, qa in enumerate(qa_items):
            category = str(qa.get("category", "unknown"))
            expected_abstention = category == "5"
            evidence = tuple(str(value) for value in qa.get("evidence", []) if value)
            relevant_sessions = tuple(sorted({
                turn_to_session[turn_id] for turn_id in evidence
                if turn_id in turn_to_session
            }))
            result.append(PublicBenchmarkCase(
                benchmark="locomo",
                case_id=f"{sample_id}-qa-{index:04d}",
                category=category,
                sessions=sessions,
                question=str(qa["question"]),
                expected_answer=(None if expected_abstention else str(qa["answer"])),
                relevant_session_ids=relevant_sessions,
                relevant_turn_ids=evidence,
                expected_abstention=expected_abstention,
            ))
            if limit is not None and len(result) >= limit:
                return _unique_cases(result)
    return _unique_cases(result)


def _locomo_sessions(
    conversation: Mapping[str, Any],
) -> tuple[tuple[PublicSession, ...], dict[str, str]]:
    session_keys = sorted(
        (key for key in conversation if key.startswith("session_")
         and key[len("session_"):].isdigit()),
        key=lambda key: int(key[len("session_"):]),
    )
    sessions: list[PublicSession] = []
    turn_to_session: dict[str, str] = {}
    for key in session_keys:
        session_id = key
        turns: list[PublicTurn] = []
        raw_turns = conversation[key]
        if not isinstance(raw_turns, list):
            raise TypeError(f"LoCoMo {key} must contain a turn list")
        for raw_turn in raw_turns:
            turn_id = str(raw_turn.get("dia_id", "")) or None
            turns.append(PublicTurn(
                role=str(raw_turn.get("speaker", "unknown")),
                content=str(raw_turn.get("text", "")),
                turn_id=turn_id,
            ))
            if turn_id is not None:
                turn_to_session[turn_id] = session_id
        sessions.append(PublicSession(
            session_id=session_id,
            timestamp=conversation.get(f"{key}_date_time"),
            turns=tuple(turns),
        ))
    if not sessions:
        raise ValueError("LoCoMo sample contains no numbered sessions")
    return tuple(sessions), turn_to_session


def load_longmemeval(
    path: str | Path, *, limit: int | None = None,
) -> list[PublicBenchmarkCase]:
    """Read official LongMemEval_S/M/oracle JSON instances."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError("LongMemEval input must be a list of question instances")
    result: list[PublicBenchmarkCase] = []
    for item in raw[:limit] if limit is not None else raw:
        expected_abstention = str(item["question_id"]).endswith("_abs")
        ids = item.get("haystack_session_ids")
        dates = item.get("haystack_dates")
        raw_sessions = item.get("haystack_sessions")
        if not (isinstance(ids, list) and isinstance(dates, list)
                and isinstance(raw_sessions, list)
                and len(ids) == len(dates) == len(raw_sessions)):
            raise ValueError("LongMemEval session IDs, dates, and contents must align")
        sessions: list[PublicSession] = []
        for session_id, timestamp, raw_turns in zip(ids, dates, raw_sessions, strict=True):
            turns = tuple(PublicTurn(
                role=str(turn.get("role", "unknown")),
                content=str(turn.get("content", "")),
            ) for turn in raw_turns)
            sessions.append(PublicSession(str(session_id), str(timestamp), turns))
        result.append(PublicBenchmarkCase(
            benchmark="longmemeval",
            case_id=str(item["question_id"]),
            category=str(item["question_type"]),
            sessions=tuple(sessions),
            question=str(item["question"]),
            expected_answer=(None if expected_abstention else str(item["answer"])),
            relevant_session_ids=tuple(str(value) for value in item.get(
                "answer_session_ids", [],
            )),
            relevant_turn_ids=tuple(
                f"{session_id}:{index}"
                for session_id, turns in zip(ids, raw_sessions, strict=True)
                for index, turn in enumerate(turns)
                if turn.get("has_answer") is True
            ),
            expected_abstention=expected_abstention,
        ))
    return _unique_cases(result)


def _unique_cases(cases: Sequence[PublicBenchmarkCase]) -> list[PublicBenchmarkCase]:
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("public benchmark question IDs must be unique")
    return list(cases)


async def run_public_baseline(
    benchmark: str,
    cases: Sequence[PublicBenchmarkCase],
    execute_case: Callable[[PublicBenchmarkCase], Any | Awaitable[Any]], *,
    dataset_path: str | Path,
    config_aliases: Mapping[str, str],
    report_path: str | Path,
    freeze_path: str | Path | None = None,
    repeat_of: str | None = None,
    code_identity: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run a public set and atomically keep only non-blocking aggregate evidence."""
    run_identity = capture_code_identity()
    if code_identity is not None and dict(code_identity) != run_identity:
        raise RuntimeError("public benchmark code identity changed before execution")
    if benchmark not in _BENCHMARKS:
        raise ValueError(f"unsupported public benchmark: {benchmark}")
    if not cases:
        raise ValueError("public benchmark run has zero cases")
    if any(
        not isinstance(key, str) or not _ALIAS_TOKEN.fullmatch(key)
        or not isinstance(value, str) or not _ALIAS_TOKEN.fullmatch(value)
        for key, value in config_aliases.items()
    ):
        raise ValueError("reports accept configuration aliases only, never endpoint or secret values")
    if any(case.benchmark != benchmark for case in cases):
        raise ValueError("public benchmark case belongs to a different adapter")
    _unique_cases(cases)
    run_id = str(uuid4())
    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            outcome = execute_case(case)
            while inspect.isawaitable(outcome):
                outcome = await outcome
            if isinstance(outcome, Mapping) and outcome.get("status") in {
                "executed", "failed", "skipped", "unawaited",
            } and "status" in outcome:
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
        except Exception as error:  # noqa: BLE001 — keep benchmark failure output content-free
            result = {"case_id": case.case_id, "status": "failed",
                      "error_type": type(error).__name__}
        results.append(result)

    report = _aggregate_public_run(
        benchmark, cases, results, dataset_path=Path(dataset_path),
        config_aliases=config_aliases, run_id=run_id, repeat_of=repeat_of,
        code_identity=run_identity,
    )
    try:
        identity_unchanged = capture_code_identity() == run_identity
    except Exception:  # noqa: BLE001 — unverifiable post-run identity must fail the evidence.
        identity_unchanged = False
    report["worktree_clean"] = identity_unchanged
    if not identity_unchanged:
        report["status"] = "failed"
        report["failures"] = sorted({
            *report["failures"], "worktree_identity_changed_or_unverified",
        })
    destination = Path(report_path)
    await asyncio.to_thread(_write_report_exclusive, destination, report)
    if report["status"] == "completed" and freeze_path is not None:
        baseline = dict(report)
        baseline["baseline_frozen_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            await asyncio.to_thread(_write_report_exclusive, Path(freeze_path), baseline)
            report["baseline_frozen"] = True
        except FileExistsError:
            report["baseline_frozen"] = False
    else:
        report["baseline_frozen"] = False
    return report


def _aggregate_public_run(
    benchmark: str, cases: Sequence[PublicBenchmarkCase], results: Sequence[Mapping[str, Any]],
    *, dataset_path: Path, config_aliases: Mapping[str, str], run_id: str,
    repeat_of: str | None, code_identity: Mapping[str, str],
) -> dict[str, Any]:
    reasons: list[str] = []
    expected_ids = [case.case_id for case in cases]
    actual_ids = [item.get("case_id") for item in results]
    if len(actual_ids) != len(set(actual_ids)):
        reasons.append("duplicate_case_results")
    if set(expected_ids) != set(actual_ids) or len(expected_ids) != len(actual_ids):
        reasons.append("incomplete_or_unexpected_case_results")
    by_id = {item.get("case_id"): item for item in results}
    statuses = Counter(str(item.get("status", "failed")) for item in results)
    executed = [case for case in cases if by_id.get(case.case_id, {}).get("status") == "executed"]
    if len(executed) != len(cases):
        reasons.append("non_executed_cases")
    trace_ids = [
        by_id[case.case_id].get("trace_id") for case in executed
        if by_id[case.case_id].get("trace_id") is not None
    ]
    if len(trace_ids) != len(set(trace_ids)):
        reasons.append("duplicate_trace_ids")

    observations: dict[str, Mapping[str, Any]] = {}
    for case in executed:
        observed = by_id[case.case_id].get("observed")
        if not isinstance(observed, Mapping):
            reasons.append(f"missing_observation:{case.case_id}")
            continue
        missing = _OBSERVATION_FIELDS - observed.keys()
        reasons.extend(f"missing_metric:{case.case_id}:{field}" for field in sorted(missing))
        if type(observed.get("answer_correct")) is not bool:
            reasons.append(f"invalid_metric:{case.case_id}:answer_correct")
        for field in (
            "stored_record_count", "pollution_count", "injected_tokens", "latency_ms",
            "input_tokens", "output_tokens",
        ):
            value = observed.get(field)
            if type(value) is not int or value < 0:
                reasons.append(f"invalid_metric:{case.case_id}:{field}")
        if not isinstance(observed.get("retrieved_session_ids"), list):
            reasons.append(f"invalid_metric:{case.case_id}:retrieved_session_ids")
        cost = observed.get("cost_usd")
        if cost is not None and (
            not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0
        ):
            reasons.append(f"invalid_metric:{case.case_id}:cost_usd")
        observations[case.case_id] = observed

    answer_cases = executed
    answer_hits = sum(
        observations[case.case_id].get("answer_correct") is True for case in answer_cases
    )
    relevant_count = sum(len(case.relevant_session_ids) for case in executed)
    recall_hits = sum(
        len(set(case.relevant_session_ids).intersection(
            observations.get(case.case_id, {}).get("retrieved_session_ids", [])[:6],
        ))
        for case in executed
    )
    stored = sum(_int_metric(observations.get(case.case_id, {}), "stored_record_count")
                 for case in executed)
    pollution = sum(_int_metric(observations.get(case.case_id, {}), "pollution_count")
                    for case in executed)
    costs = [
        value for case in executed
        if isinstance(value := observations.get(case.case_id, {}).get("cost_usd"), (int, float))
        and not isinstance(value, bool)
    ]
    data = dataset_path.read_bytes().replace(b"\r\n", b"\n")
    info = _BENCHMARKS[benchmark]
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "repeat_of": repeat_of,
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "completed" if not reasons and len(executed) == len(cases) else "failed",
        "blocking": False,
        "benchmark": info["name"],
        "adapter_version": 1,
        "source": info["url"],
        "license": info["license"],
        "non_commercial_only": info["non_commercial_only"],
        "dataset": {
            "filename": dataset_path.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "case_count": len(cases),
        },
        "tool": {
            "name": "agent-harness-memory-v2",
            "code_sha": code_identity["code_sha"],
            "tree_sha": code_identity["tree_sha"],
            "config_aliases": dict(config_aliases),
        },
        "case_counts": {
            "total": len(cases), "executed": len(executed),
            "failed": statuses["failed"], "skipped": statuses["skipped"],
            "unawaited": statuses["unawaited"],
        },
        "metrics": {
            "answer_quality": {
                "numerator": answer_hits, "denominator": len(answer_cases),
                "value": answer_hits / len(answer_cases) if answer_cases else None,
            },
            "session_recall_at_6": {
                "numerator": recall_hits, "denominator": relevant_count,
                "value": recall_hits / relevant_count if relevant_count else None,
            },
            "stored_record_count": stored,
            "pollution_rate": pollution / stored if stored else None,
            "pollution_count": pollution,
            "injected_tokens": sum(
                _int_metric(observations.get(case.case_id, {}), "injected_tokens")
                for case in executed
            ),
            "latency_ms_total": sum(
                _int_metric(observations.get(case.case_id, {}), "latency_ms")
                for case in executed
            ),
            "input_tokens": sum(
                _int_metric(observations.get(case.case_id, {}), "input_tokens")
                for case in executed
            ),
            "output_tokens": sum(
                _int_metric(observations.get(case.case_id, {}), "output_tokens")
                for case in executed
            ),
            "cost_usd": sum(costs) if costs else None,
        },
        "failures": reasons,
    }
    return report


def _int_metric(observed: Mapping[str, Any], name: str) -> int:
    value = observed.get(name)
    return value if type(value) is int and value >= 0 else 0


def _write_report_exclusive(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
