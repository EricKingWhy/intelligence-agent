"""Real process crash after a nested write commits, before its result event."""

import asyncio
import json
import os
import sys
from pathlib import Path

from agent_harness.sandbox.docker import DockerSandbox
from agent_harness.session import MODEL_COMPLETED, JsonlSessionStore, Session
from agent_harness.storage import OperationContext, SqliteOperationLedger
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tools.exec import ExecTool
from agent_harness.tools.write import WriteTool


async def main(config):
    root = Path(config['root'])
    root.mkdir(parents=True, exist_ok=True)
    session = Session.start(JsonlSessionStore(root / 'sessions'))
    (root / 'session-id').write_text(session.session_id, encoding='utf-8')
    sandbox = DockerSandbox(container_name=config['container'], volume_name=config['volume'])
    ledger = SqliteOperationLedger(root / 'state.db')
    await ledger.initialize()
    registry = ToolRegistry()
    registry.register(WriteTool(sandbox))
    registry.register(ExecTool(sandbox))
    code = "result = await tools.write(path='committed.txt', content='once')"
    call = {'id': 'outer', 'name': 'exec', 'args': {'code': code}}
    session.append(MODEL_COMPLETED, {'content': '', 'tool_calls': [call]}, run_id='run')

    def kill(stage, call_id):
        if stage == 'terminal' and call_id != 'outer':
            os._exit(137)

    executor = ToolExecutor(registry, operation_ledger=ledger, kill_hook=kill)
    executor.emit_call_event(session, tool_call_id='outer', tool_name='exec',
                             args=call['args'], run_id='run', step_id=1)
    await executor.execute_batch([call], session=session,
        operation_context=OperationContext(session_id=session.session_id, run_id='run'))
    raise RuntimeError('Kill hook did not trigger')


if __name__ == '__main__':
    asyncio.run(main(json.loads(sys.argv[1])))
