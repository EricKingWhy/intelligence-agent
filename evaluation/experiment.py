"""Langfuse Experiment 上报（ADR-0018 D9/D10）。

数据集在云端的实验走 Langfuse 原生 ``run_experiment``：task = 项目 runner
（调真实 AgentRuntime），evaluators = deterministic assertions（代码判断）。
Langfuse 未配置 → 优雅跳过（本地 run_dataset 报告恒产生，不受影响）。
真实模型 smoke 入口见 :func:`run_smoke_experiment`（T8：手动触发，结构性断言）。
"""

from __future__ import annotations

import tempfile
from collections import Counter
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from evaluation.runner import EvalCase, run_case_async


def _metadata_tags(metadata: Any) -> set[str]:
    if not isinstance(metadata, dict):
        return set()
    tags = metadata.get("tags", [])
    if not isinstance(tags, (list, tuple, set)):
        return set()
    return {str(tag) for tag in tags}


def _recovery_status(result: dict[str, Any], metadata: Any) -> tuple[bool, bool]:
    metrics = result.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    required = set()
    case_type = result.get("case_type")
    tags = _metadata_tags(metadata)
    if case_type == "kill_resume":
        required.update({"kill_resume_ok", "agent_runtime_completed"})
    else:
        if case_type == "recovery" or "recovery" in tags:
            required.add("recovered")
        required.update(key for key in ("recovered", "kill_resume_ok") if key in metrics)
    if not required:
        return True, False
    passed = all(key in metrics and metrics[key] is True for key in required)
    return passed, True


def _count_metric(metrics: dict[str, Any], name: str) -> int | None:
    value = metrics.get(name)
    return value if type(value) is int and value >= 0 else None


def _deterministic_evaluators() -> list[Callable[..., dict[str, Any]]]:
    """代码判断评分器；所有 P0 Gate 判据都以结构化 Score 返回。"""

    def p0_pass(*, output: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        result = output.get("result", {})
        passed = isinstance(result, dict) and result.get("ok") is True
        return {"name": "p0_pass", "value": 1 if passed else 0}

    def dangling(*, output: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        result = output.get("result", {})
        metrics = result.get("metrics", {}) if isinstance(result, dict) else {}
        count = _count_metric(metrics, "dangling_tool_calls") \
            if isinstance(metrics, dict) else None
        return {
            "name": "dangling_tool_calls",
            "value": count if count is not None else 1,
        }

    def recovery(*, output: dict[str, Any], metadata: Any = None,
                 **_kwargs: Any) -> dict[str, Any]:
        result = output.get("result", {})
        passed, applicable = _recovery_status(
            result if isinstance(result, dict) else {}, metadata,
        )
        return {
            "name": "recovery_pass",
            "value": 1 if passed else 0,
            "metadata": {"applicable": applicable},
        }

    def counted_metric(
        name: str, tags: set[str], *, required: bool = False,
    ) -> Callable[..., dict[str, Any]]:
        def evaluate(*, output: dict[str, Any], metadata: Any = None,
                     **_kwargs: Any) -> dict[str, Any]:
            result = output.get("result", {})
            metrics = result.get("metrics", {})
            metrics = metrics if isinstance(metrics, dict) else {}
            applicable = (
                required or name in metrics
                or bool(tags.intersection(_metadata_tags(metadata)))
            )
            count = _count_metric(metrics, name) if name in metrics else 0
            value = count if count is not None else (1 if applicable else 0)
            return {
                "name": name,
                "value": value,
                "metadata": {"applicable": applicable},
            }

        return evaluate

    return [
        p0_pass,
        dangling,
        recovery,
        counted_metric("duplicate_confirmed_side_effects", set(), required=True),
        counted_metric("permission_violations", {"permission", "permissions"}),
    ]


def _case_from_item(item: Any) -> EvalCase:
    """Langfuse DatasetItem → EvalCase（round-trip：metadata/name + input.task）。"""
    metadata = getattr(item, "metadata", None) or {}
    item_input = getattr(item, "input", None) or {}
    return EvalCase(
        name=str(metadata.get("name") or getattr(item, "id", "unnamed")),
        case_type=str(metadata.get("case_type") or "tool_selection"),
        task=str(item_input.get("task", "")),
        script=list(metadata.get("script", [])),
        expected=getattr(item, "expected_output", None) or {},
        tags=list(metadata.get("tags", [])),
    )


def run_langfuse_experiment(
    dataset_name: str,
    *,
    public_key: str,
    secret_key: str,
    base_url: str,
    experiment_name: str | None = None,
    run_name: str | None = None,
    runtime_factory: Callable[[EvalCase], Any] | None = None,
    session_root: str | Path | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """跑云端数据集实验（deterministic 模式默认 ScriptedModel）。"""
    if not public_key or not secret_key:
        return {
            "status": "skipped",
            "reason": "LANGFUSE_PUBLIC_KEY/SECRET_KEY 未配置——云端实验跳过（本地报告不受影响）",
        }
    from langfuse import Langfuse  # 云端操作前台执行，非旁路热路径

    factory = client_factory or (lambda **kwargs: Langfuse(**kwargs))
    client = factory(public_key=public_key, secret_key=secret_key, base_url=base_url)
    dataset = client.get_dataset(dataset_name)
    dataset_items = list(dataset.items)

    own_root = session_root is None
    root = Path(session_root) if session_root is not None else Path(
        tempfile.mkdtemp(prefix="eval-experiment-"),
    )

    async def task(item: Any) -> dict[str, Any]:
        case = _case_from_item(item)
        item_identity = _item_identity(item, case.name)
        case_session_root = root / sha256(item_identity.encode("utf-8")).hexdigest()
        result, _events = await run_case_async(
            case, session_root=case_session_root, runtime_factory=runtime_factory,
        )
        return {"result": result.model_dump()}

    selected_experiment_name = experiment_name or f"eval-{dataset_name}"
    selected_run_name = run_name or f"run-{dataset_name}"
    expected_ids = [_item_identity(item, f"dataset-item-{index}")
                    for index, item in enumerate(dataset_items)]
    expected_names = [_item_name(item, identity)
                      for item, identity in zip(dataset_items, expected_ids, strict=True)]
    if (
        not dataset_items
        or len(set(expected_ids)) != len(expected_ids)
        or len(set(expected_names)) != len(expected_names)
    ):
        return _experiment_gate_result(
            SimpleNamespace(item_results=[]),
            dataset_items,
            experiment_name=selected_experiment_name,
            run_name=selected_run_name,
            session_root=str(root) if own_root else str(session_root),
        )
    experiment_result = client.run_experiment(
        name=selected_experiment_name,
        run_name=selected_run_name,
        data=dataset_items,
        task=task,
        evaluators=_deterministic_evaluators(),
        max_concurrency=1,
    )
    return _experiment_gate_result(
        experiment_result,
        dataset_items,
        experiment_name=selected_experiment_name,
        run_name=selected_run_name,
        session_root=str(root) if own_root else str(session_root),
    )


def _score_value(score: Any) -> Any:
    if isinstance(score, dict):
        return score.get("value")
    return getattr(score, "value", None)


def _score_name(score: Any) -> str:
    if isinstance(score, dict):
        return str(score.get("name", ""))
    return str(getattr(score, "name", ""))


def _item_identity(item: Any, fallback: str) -> str:
    identity = getattr(item, "id", None)
    if identity is None:
        metadata = getattr(item, "metadata", None) or {}
        identity = metadata.get("name")
    return str(identity if identity is not None else fallback)


def _item_name(item: Any, fallback: str) -> str:
    metadata = getattr(item, "metadata", None) or {}
    name = metadata.get("name")
    return str(name if name is not None else fallback)


def _experiment_gate_result(
    experiment_result: Any,
    dataset_items: list[Any],
    *,
    experiment_name: str,
    run_name: str,
    session_root: str,
) -> dict[str, Any]:
    """Fail closed unless every dataset item and deterministic Gate is accounted for."""
    expected_ids = [
        _item_identity(item, f"dataset-item-{index}")
        for index, item in enumerate(dataset_items)
    ]
    expected_names = [
        _item_name(item, identity)
        for item, identity in zip(dataset_items, expected_ids, strict=True)
    ]
    failures: list[dict[str, Any]] = []
    if not dataset_items:
        failures.append({"item_id": None, "reasons": ["dataset is empty"]})
    if len(set(expected_ids)) != len(expected_ids):
        failures.append({"item_id": None, "reasons": ["dataset contains duplicate item identities"]})
    if len(set(expected_names)) != len(expected_names):
        failures.append({"item_id": None, "reasons": ["dataset contains duplicate case names"]})

    item_results = getattr(experiment_result, "item_results", None)
    if not isinstance(item_results, (list, tuple)):
        item_results = []
        failures.append({"item_id": None, "reasons": ["ExperimentResult has no item_results"]})

    actual_ids = [
        _item_identity(getattr(item_result, "item", None), f"result-{index}")
        for index, item_result in enumerate(item_results)
    ]
    actual_counts = Counter(actual_ids)
    expected_counts = Counter(expected_ids)
    if len(item_results) != len(dataset_items):
        failures.append({
            "item_id": None,
            "reasons": [f"processed {len(item_results)} of {len(dataset_items)} dataset items"],
        })
    for item_id, count in actual_counts.items():
        if count > 1:
            failures.append({"item_id": item_id, "reasons": ["duplicate item result"]})
    for item_id, count in (expected_counts - actual_counts).items():
        failures.append({"item_id": item_id, "reasons": ["missing item result"]})
    for item_id, count in (actual_counts - expected_counts).items():
        failures.append({"item_id": item_id, "reasons": ["unexpected item result"]})

    passed = recovery_applicable = recovery_passed = 0
    dangling_total = duplicate_side_effects = permission_violations = 0
    for item_result in item_results:
        item = getattr(item_result, "item", None)
        item_id = _item_identity(item, "unknown")
        reasons: list[str] = []
        if expected_counts[item_id] != 1 or actual_counts[item_id] != 1:
            reasons.append("item result identity is missing, duplicate, or unexpected")

        output = getattr(item_result, "output", None)
        result = output.get("result") if isinstance(output, dict) else None
        if not isinstance(result, dict):
            reasons.append("task output has no CaseResult")
            metrics: dict[str, Any] = {}
        else:
            raw_metrics = result.get("metrics")
            metrics = raw_metrics if isinstance(raw_metrics, dict) else {}
            if not isinstance(raw_metrics, dict):
                reasons.append("CaseResult.metrics is not an object")
            if result.get("ok") is not True:
                reasons.append("CaseResult.ok is not true")

        raw_scores = getattr(item_result, "evaluations", None)
        raw_scores = raw_scores if isinstance(raw_scores, (list, tuple)) else []
        if not isinstance(getattr(item_result, "evaluations", None), (list, tuple)):
            reasons.append("ExperimentResult item has no evaluations")
        score_values: dict[str, Any] = {}
        for score in raw_scores:
            name = _score_name(score)
            if name in score_values:
                reasons.append(f"duplicate evaluator result: {name}")
            score_values[name] = _score_value(score)
        for score_name, expected_value in (("p0_pass", 1), ("dangling_tool_calls", 0)):
            if score_values.get(score_name) != expected_value:
                reasons.append(f"{score_name} is missing or failed")
        dangling_count = _count_metric(metrics, "dangling_tool_calls")
        if dangling_count is None:
            reasons.append("dangling_tool_calls metric is missing or invalid")
        elif dangling_count != 0:
            reasons.append("dangling tool calls are not zero")
            dangling_total += dangling_count

        metadata = getattr(item, "metadata", None)
        recovery_ok, recovery_is_applicable = _recovery_status(
            result if isinstance(result, dict) else {}, metadata,
        )
        if recovery_is_applicable:
            recovery_applicable += 1
            if not recovery_ok or score_values.get("recovery_pass") != 1:
                reasons.append("recovery Gate did not pass")
            else:
                recovery_passed += 1

        tags = _metadata_tags(metadata)
        for metric_name, score_name, tag_names, required in (
            ("duplicate_confirmed_side_effects", "duplicate_confirmed_side_effects",
             set(), True),
            ("permission_violations", "permission_violations", {"permission", "permissions"},
             False),
        ):
            applicable = (
                required or metric_name in metrics
                or bool(tag_names.intersection(tags))
            )
            if applicable:
                count = _count_metric(metrics, metric_name)
                if count is None:
                    reasons.append(f"{metric_name} metric is missing or invalid")
                    count = 1
                if score_values.get(score_name) != count:
                    reasons.append(f"{score_name} evaluator is missing or inconsistent")
                if count != 0:
                    reasons.append(f"{metric_name} is non-zero")
                if metric_name == "duplicate_confirmed_side_effects":
                    duplicate_side_effects += count
                else:
                    permission_violations += count

        if reasons:
            failures.append({"item_id": item_id, "reasons": reasons})
        else:
            passed += 1

    return {
        "status": "ok" if not failures and passed == len(dataset_items) else "failed",
        "experiment": experiment_name,
        "run_name": run_name,
        "session_root": session_root,
        "dataset_run_id": getattr(experiment_result, "dataset_run_id", None),
        "dataset_run_url": getattr(experiment_result, "dataset_run_url", None),
        "total": len(dataset_items),
        "passed": passed,
        "failed": max(0, len(dataset_items) - passed),
        "recovery_applicable": recovery_applicable,
        "recovery_passed": recovery_passed,
        "dangling_tool_calls": dangling_total,
        "duplicate_confirmed_side_effects": duplicate_side_effects,
        "permission_violations": permission_violations,
        "failures": failures,
    }
