"""Paired real-model Direct/CodeMode workloads through AgentRuntime (#1360).

Generated, deterministic data workloads; these are real executions, not model
transcripts or claimed production-user benchmarks. Run with an external JSON
model connection file (never commit that file). Approval callbacks automatically
approve once: measure requested interruptions, not human response latency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agent_harness.agent import AgentRuntime
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model
from agent_harness.sandbox.docker import DockerSandbox
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.storage import SqliteOperationLedger
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from agent_harness.tooling.approval import ApprovalResponse
from agent_harness.tooling.contract import PermissionPolicy, ToolPermission
from agent_harness.tooling.exposure import ToolExposureController, ToolSearchTool
from agent_harness.tools.exec import ExecTool
from agent_harness.tools.write import WriteTool


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ReadPartition(Tool):
    args_schema = EmptyArgs
    permission = ToolPermission.READ_ONLY
    replay_safe = True

    def __init__(self, sandbox, index: int, deferred: bool):
        self.sandbox, self.index, self.defer_when_many = sandbox, index, deferred

    @property
    def name(self):
        return f'read_partition_{self.index:02}'

    @property
    def description(self):
        return f'Read all rows in partition {self.index}. Returns data.rows with id, group, amount.'

    async def execute(self, args):
        rows = json.loads(await asyncio.to_thread(self.sandbox.read_text, f'p{self.index}.json'))
        return ToolResult.success(message='Partition read.', data={'rows': rows})


def rows_for(partition):
    return [{'id': partition * 100 + index, 'group': index % 4,
             'amount': (partition * 11 + index * 7) % 500 + 1,
             'description': f'record-{partition}-{index}-accounting-line-item'}
            for index in range(100)]


def task_for(index):
    partition = index % 31
    if index < 8:
        partitions = [(partition + offset) % 31 for offset in range(3)]
        group = index % 4
        expected = sum(row['amount'] for part in partitions for row in rows_for(part)
                       if row['group'] == group)
        task = f'Sum amount for group {group} across partitions {partitions}.'
        kind = 'aggregate'
    else:
        row_index = (index * 3) % 100
        expected = rows_for(partition)[row_index]['amount']
        task = f'Read partition {partition} and find amount for id {partition * 100 + row_index}.'
        kind = 'write' if index >= 16 else 'lookup'
    if kind == 'write':
        task += ' Save the numeric answer as plain text to answer.txt using the write tool.'
    return kind, task, expected


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(report, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    assert path.read_bytes() == data


async def run_pair_member(index, mode, config, root):
    kind, task, expected = task_for(index)
    run_root = root / f'{index:02}-{mode}'
    run_root.mkdir(parents=True, exist_ok=True)
    sandbox = DockerSandbox()
    ledger = SqliteOperationLedger(run_root / 'state.db')
    await ledger.initialize()
    session = Session.start(JsonlSessionStore(run_root / 'sessions'))
    approvals = []

    async def approve(request):
        approvals.append(request.tool_name)
        return ApprovalResponse(approved=True)

    started = time.perf_counter()
    error = None
    result = None
    answer = None
    file_ok = kind != 'write'
    try:
        await asyncio.to_thread(sandbox.ensure_started)
        for partition in range(31):
            await asyncio.to_thread(sandbox.write_text, f'p{partition}.json',
                                    json.dumps(rows_for(partition)))
        registry = ToolRegistry()
        for partition in range(31):
            registry.register(ReadPartition(sandbox, partition, mode == 'CodeMode'))
        registry.register(WriteTool(sandbox))
        controller = None
        if mode == 'CodeMode':
            registry.register(ExecTool(sandbox))
            controller = ToolExposureController(registry)
            registry.register(ToolSearchTool(controller))
        runtime = AgentRuntime(
            create_chat_model(config), registry,
            ToolExecutor(registry, policy=PermissionPolicy.READ_ONLY,
                         approval_callback=approve, operation_ledger=ledger, project_root=run_root),
            max_agent_turns=8, tool_exposure=controller, primary_model_name=config.model_name,
            system_prompt=(
                'Solve the task using the available tools. Final response must be exactly '
                'a JSON object with one numeric answer key: {"answer": 123}. '
                'Use only the partitions specified by the task. '
                + ('For filtering or aggregation you may search for exec and use Python. '
                   'Direct tools remain available through tool_search; choose the simpler approach. '
                   if mode == 'CodeMode' else '')
            ),
        )
        result = await asyncio.wait_for(runtime.run(session, task), timeout=240)
        final = result.final_text.strip()
        if final.startswith('```'):
            final = '\n'.join(final.splitlines()[1:-1])
        answer = json.loads(final)['answer']
        if kind == 'write':
            file_ok = (await asyncio.to_thread(sandbox.read_text, 'answer.txt')).strip() == str(expected)
    except Exception as exc:  # noqa: BLE001 -- preserve failed pair members in the report
        error = type(exc).__name__ + ': ' + str(exc)[:500]
    finally:
        await asyncio.to_thread(sandbox.delete)
    operations = await ledger.list_for_session(session.session_id)
    usage = next((event.data.get('usage_total', {}) for event in reversed(session.events)
                  if event.type == 'run/completed'), {})
    row = {
        'task': index, 'kind': kind, 'mode': mode, 'prompt': task,
        'expected': expected, 'answer': answer, 'file_ok': file_ok,
        'ok': result is not None and result.status == 'completed' and answer == expected and file_ok,
        'status': result.status if result is not None else 'error', 'error': error,
        'model_round_trips': sum(event.type == 'model/completed' for event in session.events),
        'usage': usage, 'approval_interruptions': len(approvals), 'approved_tools': approvals,
        'tool_failures': sum(op.state in {'FAILED', 'UNKNOWN', 'NEED_RECONCILE'} for op in operations),
        'exec_calls': sum(op.tool_name == 'exec' for op in operations),
        'wall_seconds': round(time.perf_counter() - started, 3),
        'session_id': session.session_id,
        'operations': [{'id': op.tool_call_id, 'name': op.tool_name, 'state': op.state,
                        'parent_exec_call_id': op.parent_exec_call_id} for op in operations],
    }
    print(json.dumps({key: row[key] for key in ('task', 'mode', 'ok', 'model_round_trips',
                                               'exec_calls', 'approval_interruptions', 'error')}), flush=True)
    return row


async def main(options):
    config = ModelConfig(**json.loads(options.model_config.read_text(encoding='utf-8')))
    report = {'model': config.model_name, 'tasks': options.tasks,
              'head': (await asyncio.to_thread(
                  subprocess.check_output, ['git', 'rev-parse', 'HEAD'], text=True)).strip(),
              'method': 'real AgentRuntime, real model, Docker tools, generated fixed data, paired modes',
              'approval_method': 'automated APPROVE_ONCE callback; interruptions count requests',
              'rows': []}
    # Alternate treatment order to reduce systematic warm-cache/order bias.
    for index in range(options.tasks):
        modes = ('Direct', 'CodeMode') if index % 2 == 0 else ('CodeMode', 'Direct')
        for mode in modes:
            report['rows'].append(await run_pair_member(index, mode, config, options.runtime_root))
            write_report(options.output, report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--tasks', type=int, default=20, choices=range(1, 21))
    asyncio.run(main(parser.parse_args()))
