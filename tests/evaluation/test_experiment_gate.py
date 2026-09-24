from __future__ import annotations

import asyncio
import gc
import inspect
import warnings
from types import SimpleNamespace
from typing import Any

import pytest

from agent_harness.agent import AgentRuntime
from agent_harness.model.scripted import ScriptedModel
from agent_harness.tooling import ToolExecutor, ToolRegistry
from evaluation.runner import EvalCase, run_case, run_case_async
from evaluation.support import AddTool


def _case() -> EvalCase:
    return EvalCase(
        name="tool_selection_add",
        case_type="tool_selection",
        task="Calculate 1 + 2 with the add tool.",
        script=[
            {
                "content": "",
                "tool_calls": [{
                    "name": "add",
                    "args": {"first_number": 1, "second_number": 2},
                    "id": "call-eval-1",
                }],
            },
            {"content": "1 + 2 = 3"},
        ],
        expected={"tools": ["add"]},
        tags=["p0"],
    )


def _item(case: EvalCase | None = None) -> SimpleNamespace:
    value = case or _case()
    return SimpleNamespace(
        id="item-eval-1",
        metadata={
            "name": value.name,
            "case_type": value.case_type,
            "script": value.script,
            "tags": value.tags,
        },
        input={"task": value.task},
        expected_output=value.expected,
    )


class _AsyncExperimentClient:
    def __init__(
        self,
        items: list[Any],
        *,
        omit_evaluator: str | None = None,
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        self.items = items
        self.omit_evaluator = omit_evaluator
        self.result_payload = result_payload
        self.result: SimpleNamespace | None = None

    def get_dataset(self, _name: str) -> SimpleNamespace:
        return SimpleNamespace(items=self.items)

    def run_experiment(
        self,
        *,
        name: str,
        run_name: str,
        data: list[Any],
        task: Any,
        evaluators: list[Any],
        max_concurrency: int,
    ) -> SimpleNamespace:
        assert max_concurrency == 1

        async def run_items() -> list[SimpleNamespace]:
            results = []
            for item in data:
                if self.result_payload is None:
                    output = task(item=item)
                    if inspect.isawaitable(output):
                        output = await output
                else:
                    output = {"result": self.result_payload}
                evaluations = []
                for evaluator in evaluators:
                    if getattr(evaluator, "__name__", "") == self.omit_evaluator:
                        continue
                    value = evaluator(
                        input=item.input,
                        output=output,
                        expected_output=item.expected_output,
                        metadata=item.metadata,
                    )
                    if isinstance(value, list):
                        evaluations.extend(value)
                    else:
                        evaluations.append(value)
                results.append(SimpleNamespace(
                    item=item,
                    output=output,
                    evaluations=[SimpleNamespace(**value) for value in evaluations],
                ))
            return results

        self.result = SimpleNamespace(
            name=name,
            run_name=run_name,
            item_results=asyncio.run(run_items()),
            run_evaluations=[],
            dataset_run_id="run-test",
            dataset_run_url="https://langfuse.example/runs/run-test",
        )
        return self.result


def _failing_runtime_factory(_case: EvalCase) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(AddTool())
    # run_case_async uses this exhausted model only for the negative control.
    return AgentRuntime(ScriptedModel([]), registry, ToolExecutor(registry))


def _run_experiment(tmp_path, client: _AsyncExperimentClient, **kwargs):
    from evaluation.experiment import run_langfuse_experiment

    return run_langfuse_experiment(
        "p0_core",
        public_key="pk",
        secret_key="sk",
        base_url="https://example.invalid",
        client_factory=lambda **_client_kwargs: client,
        session_root=tmp_path / "sessions",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_run_case_async_is_the_core_and_sync_wrapper_rejects_running_loop(
    tmp_path,
) -> None:
    result, _events = await run_case_async(_case(), session_root=tmp_path / "async")
    assert result.ok is True
    assert result.metrics["duplicate_confirmed_side_effects"] == 0

    with pytest.raises(RuntimeError, match="run_case_async"):
        run_case(_case(), session_root=tmp_path / "sync-in-loop")


@pytest.mark.asyncio
async def test_case_without_operation_ledger_does_not_report_duplicate_metric_as_zero(
    tmp_path,
) -> None:
    from langchain_core.messages import AIMessage

    case = EvalCase(name="untracked", case_type="other", task="Finish without tools.")

    def untracked_runtime(_case: EvalCase) -> AgentRuntime:
        registry = ToolRegistry()
        return AgentRuntime(
            ScriptedModel([AIMessage(content="done")]),
            registry,
            ToolExecutor(registry),
        )

    result, _events = await run_case_async(
        case, session_root=tmp_path / "untracked", runtime_factory=untracked_runtime,
    )

    assert result.metrics["duplicate_confirmed_side_effects"] is None
    assert result.ok is False


@pytest.mark.asyncio
async def test_failed_agent_runtime_status_cannot_pass_a_case(tmp_path) -> None:
    from langchain_core.messages import AIMessage

    from agent_harness.storage import SqliteOperationLedger

    session_root = tmp_path / "failed-status"
    ledger = SqliteOperationLedger(session_root / "state.db")
    await ledger.initialize()
    registry = ToolRegistry()
    registry.register(AddTool())
    case = EvalCase(name="failed-runtime", case_type="other", task="Finish.")

    def failing_runtime(_case: EvalCase) -> AgentRuntime:
        return AgentRuntime(
            ScriptedModel([AIMessage(
                content="",
                tool_calls=[{
                    "id": "call-at-step-limit",
                    "name": "add",
                    "args": {"first_number": 1, "second_number": 2},
                    "type": "tool_call",
                }],
            )]),
            registry,
            ToolExecutor(registry, operation_ledger=ledger),
            max_agent_turns=1,
        )

    result, _events = await run_case_async(
        case, session_root=session_root, runtime_factory=failing_runtime,
    )

    assert result.metrics["status"] == "max_steps_exceeded"
    assert result.metrics["duplicate_confirmed_side_effects"] == 0
    assert result.ok is False


def test_langfuse_experiment_awaits_task_and_checks_case_and_evaluator_results(
    tmp_path,
) -> None:
    item = _item()
    client = _AsyncExperimentClient([item])

    with warnings.catch_warnings(record=True) as observed_warnings:
        warnings.simplefilter("always")
        passed = _run_experiment(tmp_path, client)
        gc.collect()
    assert not [
        warning for warning in observed_warnings
        if issubclass(warning.category, RuntimeWarning)
        and "was never awaited" in str(warning.message)
    ]
    assert passed["status"] == "ok"
    assert passed["total"] == passed["passed"] == 1
    assert client.result.item_results[0].output["result"]["ok"] is True
    assert client.result.item_results[0].output["result"]["metrics"][
        "duplicate_confirmed_side_effects"
    ] == 0

    failed_client = _AsyncExperimentClient([item])
    failed = _run_experiment(
        tmp_path,
        failed_client,
        runtime_factory=_failing_runtime_factory,
        experiment_name="deliberately-failing-control",
    )
    assert failed["status"] == "failed"
    assert failed["failed"] == 1
    assert failed_client.result.item_results[0].output["result"]["ok"] is False

    missing_evaluator_client = _AsyncExperimentClient([item], omit_evaluator="p0_pass")
    missing_evaluator = _run_experiment(tmp_path, missing_evaluator_client)
    assert missing_evaluator["status"] == "failed"
    assert any(
        "p0_pass" in reason
        for reason in missing_evaluator["failures"][0]["reasons"]
    )


def test_duplicate_dataset_items_fail_before_creating_an_experiment_run(tmp_path) -> None:
    duplicate_item = _item()
    client = _AsyncExperimentClient([duplicate_item, duplicate_item])

    result = _run_experiment(tmp_path, client)

    assert result["status"] == "failed"
    assert client.result is None
    assert any(
        "duplicate" in reason
        for failure in result["failures"]
        for reason in failure["reasons"]
    )


def test_gate_requires_measured_duplicate_side_effect_metric(tmp_path) -> None:
    item = _item()
    client = _AsyncExperimentClient(
        [item],
        result_payload={
            "name": item.metadata["name"],
            "case_type": item.metadata["case_type"],
            "ok": True,
            "metrics": {"dangling_tool_calls": 0},
        },
    )

    result = _run_experiment(tmp_path, client)

    assert result["status"] == "failed"
    assert any(
        "duplicate_confirmed_side_effects" in reason
        for failure in result["failures"]
        for reason in failure["reasons"]
    )


def test_langfuse_gate_rejects_duplicate_terminal_ledger_evidence(
    tmp_path, monkeypatch,
) -> None:
    from agent_harness.storage.sqlite import SqliteOperationLedger

    original_list_for_session = SqliteOperationLedger.list_for_session

    async def list_with_duplicate_terminal(self, session_id: str):
        operations = await original_list_for_session(self, session_id)
        return [*operations, operations[0].model_copy()] if operations else operations

    monkeypatch.setattr(
        SqliteOperationLedger, "list_for_session", list_with_duplicate_terminal,
    )
    item = _item()
    client = _AsyncExperimentClient([item])

    gate = _run_experiment(tmp_path, client)

    case_result = client.result.item_results[0].output["result"]
    assert case_result["metrics"]["duplicate_confirmed_side_effects"] == 1
    assert case_result["ok"] is False
    assert gate["status"] == "failed"
    assert any(
        "duplicate_confirmed_side_effects" in reason
        for failure in gate["failures"]
        for reason in failure["reasons"]
    )


def test_kill_resume_case_uses_its_specific_recovery_metrics(tmp_path) -> None:
    case = EvalCase(
        name="kill_resume_confirm_success",
        case_type="kill_resume",
        task="Recover the confirmed operation and finish the run.",
        expected={"verdict": "CONFIRM_SUCCESS"},
        tags=["p0", "recovery"],
    )
    client = _AsyncExperimentClient(
        [_item(case)],
        result_payload={
            "name": case.name,
            "case_type": case.case_type,
            "ok": True,
            "metrics": {
                "kill_resume_ok": True,
                "agent_runtime_completed": True,
                "dangling_tool_calls": 0,
                "duplicate_confirmed_side_effects": 0,
            },
        },
    )

    result = _run_experiment(tmp_path, client)

    assert result["status"] == "ok"
    assert result["recovery_applicable"] == 1
    assert result["recovery_passed"] == 1


@pytest.mark.parametrize(
    ("case_type", "metrics"),
    [
        ("tool_selection", []),
        ("tool_selection", {"dangling_tool_calls": "0"}),
        ("recovery", {}),
        ("kill_resume", {"kill_resume_ok": True}),
    ],
)
def test_malformed_or_incomplete_case_metrics_fail_closed(
    tmp_path, case_type: str, metrics: Any,
) -> None:
    item = _item(EvalCase(
        name=f"case-{case_type}", case_type=case_type, task="run",
    ))
    client = _AsyncExperimentClient(
        [item],
        result_payload={"name": item.metadata["name"], "case_type": case_type,
                        "ok": True, "metrics": metrics},
    )

    result = _run_experiment(tmp_path, client)

    assert result["status"] == "failed"
    assert result["passed"] == 0
    assert result["failed"] == 1
