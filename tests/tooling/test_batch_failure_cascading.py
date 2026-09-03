"""Safe failure cascading for serial and parallel Tool batches."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from agent_harness.storage import (
    OperationContext,
    OperationState,
    SqliteOperationLedger,
)
from agent_harness.tooling import (
    ErrorCode,
    Tool,
    ToolExecutor,
    ToolPermission,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
)


class _NoArgs(BaseModel):
    pass


class _RequiredArgs(BaseModel):
    value: str


class _MutatingTool(Tool):
    def __init__(self, name: str, *, fail_retryably: bool = False) -> None:
        self._name = name
        self._fail_retryably = fail_retryably
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Mutate a deterministic counter."

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    async def execute(self, args: BaseModel) -> ToolResult:
        self.calls += 1
        if self._fail_retryably:
            return ToolResult.failure(
                "temporary failure",
                error_code=ErrorCode.TRANSIENT_ERROR,
                retryable=True,
            )
        return ToolResult.success("mutated")


class _ValidatingMutatingTool(_MutatingTool):
    @property
    def args_schema(self) -> type[BaseModel]:
        return _RequiredArgs


class _DangerousMutatingTool(_MutatingTool):
    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER


class _ReadTool(Tool):
    def __init__(self, name: str, *, fails: bool = False) -> None:
        self._name = name
        self._fails = fails
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Read deterministic data."

    @property
    def args_schema(self) -> type[BaseModel]:
        return _NoArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        self.calls += 1
        if self._fails:
            return ToolResult.failure(
                "read failed",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
                retryable=False,
            )
        return ToolResult.success("read")


@pytest.mark.asyncio
async def test_serial_batch_cancels_remaining_calls_after_retry_exhaustion(
    tmp_path: Path,
) -> None:
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    failing = _MutatingTool("failing", fail_retryably=True)
    second = _MutatingTool("second")
    third = _MutatingTool("third")
    registry = ToolRegistry()
    for tool in (failing, second, third):
        registry.register(tool)
    executor = ToolExecutor(registry, operation_ledger=ledger)
    calls = [
        {"id": "call-1", "name": "failing", "args": {}},
        {"id": "call-2", "name": "second", "args": {}},
        {"id": "call-3", "name": "third", "args": {}},
    ]

    results = await executor.execute_batch(
        calls,
        operation_context=OperationContext(session_id="session-1"),
    )

    assert failing.calls == 3
    assert second.calls == 0
    assert third.calls == 0
    assert [result.tool_call_id for result in results] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    assert [result.result.error_code for result in results] == [
        ErrorCode.TRANSIENT_ERROR,
        ErrorCode.CANCELLED,
        ErrorCode.CANCELLED,
    ]
    operations = await ledger.list_for_session("session-1")
    assert [operation.state for operation in operations] == [
        OperationState.FAILED,
        OperationState.CANCELLED,
        OperationState.CANCELLED,
    ]
    assert operations[1].result_json == results[1].result.model_dump_json()


@pytest.mark.asyncio
async def test_parallel_read_failure_does_not_cancel_other_calls(
    tmp_path: Path,
) -> None:
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    first = _ReadTool("first")
    failing = _ReadTool("failing", fails=True)
    third = _ReadTool("third")
    registry = ToolRegistry()
    for tool in (first, failing, third):
        registry.register(tool)
    executor = ToolExecutor(registry, operation_ledger=ledger)

    results = await executor.execute_batch(
        [
            {"id": "call-1", "name": "first", "args": {}},
            {"id": "call-2", "name": "failing", "args": {}},
            {"id": "call-3", "name": "third", "args": {}},
        ],
        operation_context=OperationContext(session_id="session-1"),
    )

    assert [first.calls, failing.calls, third.calls] == [1, 1, 1]
    assert [result.tool_call_id for result in results] == [
        "call-1",
        "call-2",
        "call-3",
    ]
    assert [result.result.error_code for result in results] == [
        None,
        ErrorCode.TOOL_EXECUTION_ERROR,
        None,
    ]
    operations = await ledger.list_for_session("session-1")
    assert [operation.state for operation in operations] == [
        OperationState.SUCCEEDED,
        OperationState.FAILED,
        OperationState.SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_preflight_failure_has_no_operation_and_cancels_remainder(
    tmp_path: Path,
) -> None:
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    invalid = _ValidatingMutatingTool("invalid")
    second = _MutatingTool("second")
    registry = ToolRegistry()
    registry.register(invalid)
    registry.register(second)
    executor = ToolExecutor(registry, operation_ledger=ledger)

    results = await executor.execute_batch(
        [
            {"id": "call-invalid", "name": "invalid", "args": {}},
            {"id": "call-cancelled", "name": "second", "args": {}},
        ],
        operation_context=OperationContext(session_id="session-1"),
    )

    assert invalid.calls == 0
    assert second.calls == 0
    assert results[0].result.error_code is ErrorCode.INVALID_ARGUMENT
    assert results[1].result.error_code is ErrorCode.CANCELLED
    assert await ledger.get("call-invalid") is None
    cancelled = await ledger.get("call-cancelled")
    assert cancelled is not None
    assert cancelled.state is OperationState.CANCELLED


@pytest.mark.asyncio
async def test_permission_denial_has_no_operation_or_side_effect(
    tmp_path: Path,
) -> None:
    ledger = SqliteOperationLedger(tmp_path / "state.db")
    await ledger.initialize()
    dangerous = _DangerousMutatingTool("dangerous")
    registry = ToolRegistry()
    registry.register(dangerous)
    executor = ToolExecutor(registry, operation_ledger=ledger)

    execution = await executor.execute(
        {"id": "call-denied", "name": "dangerous", "args": {}},
        operation_context=OperationContext(session_id="session-1"),
    )

    assert dangerous.calls == 0
    assert execution.result.error_code is ErrorCode.PERMISSION_DENIED
    assert await ledger.get("call-denied") is None
