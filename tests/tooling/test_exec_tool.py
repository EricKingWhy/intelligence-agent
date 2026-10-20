"""Public exec contract and backend refusal (#1360)."""

import pytest
from pydantic import ValidationError

from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tools.exec import ExecArgs, ExecTool


def test_exec_accepts_source_and_rejects_extra_fields():
    args = ExecArgs(code="result = await tools.read(path='note.txt')")
    assert args.yield_time_ms == 10_000
    assert args.max_output_tokens == 10_000
    with pytest.raises(ValidationError):
        ExecArgs(code='pass', unsafe=True)


@pytest.mark.asyncio
async def test_exec_never_falls_back_to_host_python(tmp_path):
    registry = ToolRegistry()
    registry.register(ExecTool(None))
    result, = await ToolExecutor(registry).execute_batch([
        {'id': 'exec', 'name': 'exec', 'args': {'code': 'print(1)'}},
    ])
    assert not result.result.ok
    assert 'Docker' in result.result.message
