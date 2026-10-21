"""Real restricted Docker worker integration; never substitutes host Python."""

import asyncio

import pytest

from agent_harness.agent.run_budget import consumed_from_events
from agent_harness.storage import OperationContext, SqliteOperationLedger
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tools.exec import ExecTool
from agent_harness.tools.read import ReadTool
from tests.conftest import make_session
from tests.sandbox.test_docker_sandbox import docker_required

pytest_plugins = ['tests.sandbox.test_docker_sandbox']


@docker_required
@pytest.mark.asyncio
async def test_code_can_only_read_workspace_through_tool_bridge(docker_sandbox, tmp_path):
    sandbox = docker_sandbox
    sandbox.ensure_started()
    sandbox.write_text('private.txt', 'bridge-only-value')
    session = make_session(tmp_path / 'session')
    ledger = SqliteOperationLedger(tmp_path / 'ledger.db')
    await ledger.initialize()
    registry = ToolRegistry()
    registry.register(ReadTool(sandbox))
    registry.register(ExecTool(sandbox))
    code = (
        "import pathlib, socket\n"
        "assert not pathlib.Path('/workspace/private.txt').exists()\n"
        "assert not pathlib.Path('/var/run/docker.sock').exists()\n"
        "pathlib.Path('/tmp/local.txt').write_text('scratch')\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=0.5)\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('network egress allowed')\n"
        "result = await tools.read(path='private.txt')\n"
        "assert result['ok']\n"
    )
    result, = await asyncio.wait_for(ToolExecutor(registry, operation_ledger=ledger).execute_batch(
        [{'id': 'outer', 'name': 'exec', 'args': {'code': code}}],
        session=session, operation_context=OperationContext(session_id=session.session_id),
    ), timeout=30)
    assert result.result.ok, result.result
    assert 'bridge-only-value' in result.result.data['output']
    children = [op for op in await ledger.list_for_session(session.session_id)
                if op.parent_exec_call_id == 'outer']
    assert len(children) == 1 and children[0].state == 'SUCCEEDED'


@docker_required
@pytest.mark.asyncio
async def test_code_exception_and_output_limit_are_structured(docker_sandbox):
    registry = ToolRegistry()
    registry.register(ExecTool(docker_sandbox))
    executor = ToolExecutor(registry)
    output, = await executor.execute_batch([{'id': 'output', 'name': 'exec', 'args': {
        'code': "print('word ' * 1000)", 'max_output_tokens': 20,
    }}])
    assert output.result.ok, output.result
    assert output.result.data['truncated']
    error, = await executor.execute_batch([{'id': 'error', 'name': 'exec', 'args': {
        'code': "raise ValueError('script-error')",
    }}])
    assert error.result.error_code == 'TOOL_EXECUTION_ERROR'
    assert 'script-error' in error.result.message


@docker_required
@pytest.mark.asyncio
async def test_timeout_kills_worker_before_returning(docker_sandbox):
    class ShortExec(ExecTool):
        timeout_seconds = 1.0

    registry = ToolRegistry()
    registry.register(ShortExec(docker_sandbox))
    result, = await ToolExecutor(registry).execute_batch([
        {'id': 'timeout', 'name': 'exec', 'args': {'code': 'while True: pass'}},
    ])
    assert result.result.error_code == 'TIMEOUT'
    assert not result.result.retryable


@docker_required
@pytest.mark.asyncio
async def test_real_kill_recovers_child_without_replaying_committed_write(docker_sandbox, tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    import docker

    from agent_harness.recovery import ReconcileVerdict, RecoveryCoordinator
    from agent_harness.recovery.coordinator import DecisionsReconcileCallback
    from agent_harness.session import JsonlSessionStore, detect_dangling
    from agent_harness.tools.write import WriteTool

    # Fresh Python process shares only the Docker volume and durable stores.
    config = {'root': str(tmp_path), 'container': docker_sandbox._container_name,
              'volume': docker_sandbox._volume_name}
    child = Path(__file__).parents[1] / 'integration' / '_code_mode_kill_child.py'
    completed = await asyncio.to_thread(subprocess.run,
        [sys.executable, str(child), json.dumps(config)],
        env={**os.environ, 'PYTHONUTF8': '1'}, capture_output=True, text=True, timeout=45,
    )
    try:
        assert completed.returncode == 137, completed.stderr
        session_id = (tmp_path / 'session-id').read_text(encoding='utf-8')
        ledger = SqliteOperationLedger(tmp_path / 'state.db')
        await ledger.initialize()
        operations = await ledger.list_for_session(session_id)
        child_op, = [op for op in operations if op.parent_exec_call_id == 'outer']
        assert child_op.state == 'SUCCEEDED'
        assert docker_sandbox.read_text('committed.txt') == 'once'
        registry = ToolRegistry()
        registry.register(ExecTool(docker_sandbox))
        registry.register(WriteTool(docker_sandbox))
        recovered = await RecoveryCoordinator(
            session_store=JsonlSessionStore(tmp_path / 'sessions'),
            workspace_registry=None, operation_ledger=ledger, tool_registry=registry,
            reconcile_callback=DecisionsReconcileCallback({'outer': ReconcileVerdict.ABANDON}),
        ).recover(session_id)
        assert not detect_dangling(recovered.events)
        nested = [event for event in recovered.events if event.type == 'tool/result'
                  and event.data.get('tool_call_id') == child_op.tool_call_id]
        assert len(nested) == 1
        assert nested[0].data['parent_exec_call_id'] == 'outer'
        assert nested[0].data['budget_delta']['tool_calls'] == 1
        outer = [event for event in recovered.events if event.type == 'tool/result'
                 and event.data.get('tool_call_id') == 'outer']
        assert len(outer) == 1
        assert outer[0].data['budget_delta']['tool_name'] == 'exec'
        assert outer[0].data['budget_delta']['tool_calls'] == 1
        assert outer[0].data['budget_delta']['tool_attempts'] == 1
        consumed = consumed_from_events(recovered.events)
        assert consumed.tool_calls_by_tool == {'exec': 1, 'write': 1}
        assert consumed.tool_attempts_by_tool == {'exec': 1, 'write': 1}
        assert docker_sandbox.read_text('committed.txt') == 'once'
    finally:
        # Clean only this test's orphaned computation worker. Production workers
        # also have the daemon-side 300s timeout when their host process dies.
        client = docker.from_env(use_context=False)
        try:
            for container in client.containers.list(all=True, filters={
                'label': f"agent-harness.owner={config['container']}",
            }):
                container.remove(force=True)
        finally:
            client.close()
