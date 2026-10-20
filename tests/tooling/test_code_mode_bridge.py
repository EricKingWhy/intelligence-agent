"""Nested calls use the same admission and durable evidence (#1360)."""

import pytest
from pydantic import BaseModel

from agent_harness.storage import OperationContext, SqliteOperationLedger
from agent_harness.tooling import ToolExecutor, ToolRegistry, ToolResult
from agent_harness.tooling.nested import nested_tool_call_var
from tests.conftest import make_session
from tests.tooling.test_tool_exposure import _ProbeTool


class _Composer(_ProbeTool):
    is_code_execution = True

    async def execute(self, args: BaseModel) -> ToolResult:
        invoke = nested_tool_call_var.get()
        assert invoke is not None
        return await invoke('read', {'topic': 'nested'})


@pytest.mark.asyncio
async def test_nested_execution_has_parent_attribution_and_durable_result(tmp_path):
    session = make_session(tmp_path / 'session')
    ledger = SqliteOperationLedger(tmp_path / 'ledger.db')
    await ledger.initialize()
    registry = ToolRegistry()
    registry.register(_ProbeTool(name='read', description='Read'))
    registry.register(_Composer(name='exec', description='Compose'))
    executor = ToolExecutor(registry, operation_ledger=ledger)
    result = await executor.execute_batch(
        [{'id': 'outer', 'name': 'exec', 'args': {}}],
        operation_context=OperationContext(session_id=session.session_id, run_id='run'),
        session=session, step_id=1,
    )
    assert result[0].result.ok
    children = [op for op in await ledger.list_for_session(session.session_id)
                if op.parent_exec_call_id == 'outer']
    assert len(children) == 1
    child = children[0]
    events = [event for event in session.events
              if event.data.get('tool_call_id') == child.tool_call_id]
    assert {event.type for event in events} >= {'tool/call', 'tool/result'}
    assert all(event.data.get('parent_exec_call_id') == 'outer' for event in events
               if event.type in {'tool/call', 'tool/result'})
    assert nested_tool_call_var.get() is None


@pytest.mark.asyncio
async def test_nested_tool_span_is_child_of_exec(tmp_path):
    from tests.observability.test_tracer import FakeRecorder, _tracer

    registry = ToolRegistry()
    registry.register(_ProbeTool(name='read', description='Read'))
    registry.register(_Composer(name='exec', description='Compose'))
    recorder = FakeRecorder()
    tracer = _tracer(recorder)
    tracer.run_started()
    await ToolExecutor(registry).execute_batch([
        {'id': 'outer', 'name': 'exec', 'args': {}},
    ], tracer=tracer)
    outer = next(span for span in recorder.spans if span.name == 'exec')
    assert [span.name for span in outer.children] == ['read']
    assert outer.ended and outer.children[0].ended


@pytest.mark.asyncio
async def test_child_storage_failure_interrupts_outer_execution(tmp_path):
    class BrokenLedger(SqliteOperationLedger):
        async def create(self, operation):
            if operation.tool_name == 'read':
                raise OSError('storage unavailable')
            await super().create(operation)

    ledger = BrokenLedger(tmp_path / 'ledger.db')
    await ledger.initialize()
    registry = ToolRegistry()
    registry.register(_ProbeTool(name='read', description='Read'))
    registry.register(_Composer(name='exec', description='Compose'))
    with pytest.raises(OSError, match='storage unavailable'):
        await ToolExecutor(registry, operation_ledger=ledger).execute_batch(
            [{'id': 'outer', 'name': 'exec', 'args': {}}],
            operation_context=OperationContext(session_id='session'),
        )


@pytest.mark.asyncio
async def test_nested_mutation_requires_its_own_approval(tmp_path):
    from agent_harness.tooling.approval import ApprovalResponse
    from agent_harness.tooling.contract import PermissionPolicy, ToolSideEffect

    class Writer(_ProbeTool):
        side_effect = ToolSideEffect.MUTATING

    approvals = []

    async def approve(request):
        approvals.append(request.tool_name)
        return ApprovalResponse(approved=request.tool_name == 'exec')

    registry = ToolRegistry()
    registry.register(Writer(name='read', description='A mutating probe'))
    registry.register(_Composer(name='exec', description='Compose'))
    result, = await ToolExecutor(
        registry, policy=PermissionPolicy.READ_ONLY, approval_callback=approve,
        project_root=tmp_path,
    ).execute_batch([{'id': 'outer', 'name': 'exec', 'args': {}}])
    assert approvals == ['exec', 'read']
    assert not result.result.ok
    assert result.result.error_code == 'PERMISSION_DENIED'


@pytest.mark.asyncio
async def test_plan_checks_each_nested_tool(tmp_path):
    from agent_harness.session.workflow import WorkflowMode
    from agent_harness.tooling.contract import ToolSideEffect

    class Composer(_Composer):
        plan_mode_exempt = True

    class Writer(_ProbeTool):
        side_effect = ToolSideEffect.MUTATING

    registry = ToolRegistry()
    registry.register(Writer(name='read', description='Mutating'))
    registry.register(Composer(name='exec', description='Compose'))
    result, = await ToolExecutor(registry, workflow_mode=WorkflowMode.PLAN).execute_batch([
        {'id': 'outer', 'name': 'exec', 'args': {}},
    ])
    assert not result.result.ok
    assert result.result.error_code == 'PERMISSION_DENIED'


@pytest.mark.asyncio
async def test_nested_calls_share_parent_quota_window(tmp_path):
    from agent_harness.tooling.quota import ToolQuotaWindow

    class Twice(_Composer):
        async def execute(self, args):
            invoke = nested_tool_call_var.get()
            assert (await invoke('read', {})).ok
            return await invoke('read', {})

    registry = ToolRegistry()
    registry.register(_ProbeTool(name='read', description='Read'))
    registry.register(Twice(name='exec', description='Compose'))
    quota = ToolQuotaWindow(limits={'read': 1}, consumed={})
    result, = await ToolExecutor(registry).execute_batch([
        {'id': 'outer', 'name': 'exec', 'args': {}},
    ], tool_quota=quota)
    assert result.result.error_code == 'BUDGET_EXHAUSTED'
    assert quota.used('read') == 1


@pytest.mark.asyncio
async def test_nested_results_do_not_create_orphan_model_messages(tmp_path):
    from langchain_core.messages import ToolMessage

    from agent_harness.session import MODEL_COMPLETED, TOOL_RESULT
    from agent_harness.session.derive import derive_messages

    session = make_session(tmp_path)
    session.append(MODEL_COMPLETED, {'content': '', 'tool_calls': [
        {'id': 'outer', 'name': 'exec', 'args': {}},
    ]})
    session.append(TOOL_RESULT, {
        'tool_call_id': 'child', 'parent_exec_call_id': 'outer', 'content': 'large intermediate',
    })
    session.append(TOOL_RESULT, {'tool_call_id': 'outer', 'content': 'small final'})
    messages = derive_messages(session.events)
    assert [message.tool_call_id for message in messages if isinstance(message, ToolMessage)] == ['outer']


@pytest.mark.asyncio
async def test_child_retains_resource_exclusion_against_other_executor(tmp_path):
    import asyncio

    from agent_harness.tooling.resource_locks import ResourceLockRegistry

    entered = asyncio.Event()
    release = asyncio.Event()
    observed = []

    class LockedRead(_ProbeTool):
        def resource_keys(self, args):
            return ['shared-workspace-file']

        async def execute(self, args):
            observed.append(args.topic)
            if args.topic == 'nested':
                entered.set()
                await release.wait()
            return ToolResult.success(message='Read')

    registry = ToolRegistry()
    registry.register(LockedRead(name='read', description='Read'))
    registry.register(_Composer(name='exec', description='Compose'))
    locks = ResourceLockRegistry()
    first = ToolExecutor(registry, resource_locks=locks)
    second = ToolExecutor(registry, resource_locks=locks)
    outer = asyncio.create_task(first.execute_batch([
        {'id': 'outer', 'name': 'exec', 'args': {}},
    ]))
    await asyncio.wait_for(entered.wait(), timeout=1)
    other = asyncio.create_task(second.execute_batch([
        {'id': 'other', 'name': 'read', 'args': {'topic': 'other'}},
    ]))
    await asyncio.sleep(0)
    assert observed == ['nested']
    release.set()
    results = await asyncio.wait_for(asyncio.gather(outer, other), timeout=1)
    assert all(batch[0].result.ok for batch in results)
    assert observed == ['nested', 'other']


@pytest.mark.asyncio
async def test_delegated_runtime_keeps_its_model_tool_results(tmp_path):
    from langchain_core.messages import AIMessage, ToolMessage

    from agent_harness.agent import AgentRuntime
    from agent_harness.session.derive import derive_messages
    from tests.scripted_model import ScriptedModel

    child_session = make_session(tmp_path / 'child')
    child_model = ScriptedModel([
        AIMessage(content='', tool_calls=[
            {'id': 'child-model-read', 'name': 'read', 'args': {}},
        ]),
        AIMessage(content='done'),
    ])
    child_registry = ToolRegistry()
    child_registry.register(_ProbeTool(name='read', description='Read'))
    child_runtime = AgentRuntime(
        model=child_model, registry=child_registry, executor=ToolExecutor(child_registry),
    )
    inherited_bridges = []

    class Delegator(_ProbeTool):
        async def execute(self, args):
            # The production in-process provider drives the child in this task.
            inherited_bridges.append(nested_tool_call_var.get())
            await child_runtime.run(child_session, 'Read the value')
            return ToolResult.success(message='delegated')

    registry = ToolRegistry()
    registry.register(Delegator(name='read', description='Delegate'))
    registry.register(_Composer(name='exec', description='Compose'))
    result, = await ToolExecutor(registry).execute_batch([
        {'id': 'outer', 'name': 'exec', 'args': {}},
    ])
    assert result.result.ok
    child_results = [event for event in child_session.events if event.type == 'tool/result']
    assert len(child_results) == 1
    assert 'parent_exec_call_id' not in child_results[0].data
    assert inherited_bridges == [None]
    messages = derive_messages(child_session.events)
    results = [message for message in messages if isinstance(message, ToolMessage)]
    assert len(results) == 1
    assert '工具执行被中断' not in results[0].content
    assert results[0].tool_call_id == 'child-model-read'
