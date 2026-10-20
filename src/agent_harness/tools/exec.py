"""Computation-only Python orchestration; external effects use ToolExecutor."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from pydantic import BaseModel, ConfigDict, Field

from agent_harness.context.tokens import estimate_tokens
from agent_harness.sandbox.docker import DockerSandbox
from agent_harness.tooling.contract import (
    Tool,
    ToolExposure,
    ToolPermission,
    ToolSideEffect,
)
from agent_harness.tooling.nested import nested_tool_call_var
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode, ToolResult


class ExecArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')

    code: str = Field(min_length=1, max_length=65_536, description=
        'Raw Python source with top-level await; no JSON or markdown wrapper.')
    yield_time_ms: int = Field(default=10_000, ge=1, le=60_000, description=
        'Maximum interval before yielding control while awaiting worker output, in milliseconds.')
    max_output_tokens: int = Field(default=10_000, ge=1, le=10_000)


def _typescript(schema: dict, root: dict | None = None, depth: int = 0) -> str:
    """Render the supported JSON Schema shapes, retaining a bounded declaration."""
    if schema is True:
        return 'unknown'
    if schema is False:
        return 'never'
    if not isinstance(schema, dict):
        return 'unknown'
    root = root or schema
    if depth > 12:
        return 'unknown'
    if '$ref' in schema:
        ref = schema['$ref']
        target = root
        if not isinstance(ref, str) or not ref.startswith('#/'):
            return 'unknown'
        for part in ref[2:].split('/'):
            target = target.get(part.replace('~1', '/').replace('~0', '~'), {})
        return _typescript(target, root, depth + 1)
    if 'const' in schema:
        return json.dumps(schema['const'])
    if 'enum' in schema:
        return ' | '.join(json.dumps(value) for value in schema['enum']) or 'never'
    for union in ('anyOf', 'oneOf'):
        if union in schema:
            return ' | '.join(_typescript(item, root, depth + 1) for item in schema[union])
    kind = schema.get('type')
    if isinstance(kind, list):
        return ' | '.join(_typescript({**schema, 'type': item}, root, depth + 1) for item in kind)
    if kind == 'array':
        return '(' + _typescript(schema.get('items', {}), root, depth + 1) + ')[]'
    if kind == 'object' or 'properties' in schema:
        required = schema.get('required', [])
        fields = [json.dumps(name) + ('' if name in required else '?') + ': ' +
                  _typescript(prop, root, depth + 1)
                  for name, prop in schema.get('properties', {}).items()]
        additional = schema.get('additionalProperties', True)
        if additional is not False:
            # unknown admits declared property types too (TypeScript index signatures).
            fields.append('[key: string]: unknown')
        return '{ ' + '; '.join(fields) + ' }'
    return {'string': 'string', 'integer': 'number', 'number': 'number',
            'boolean': 'boolean', 'null': 'null'}.get(kind, 'unknown')


class ExecTool(Tool):
    name = 'exec'
    args_schema = ExecArgs
    side_effect = ToolSideEffect.MUTATING
    permission = ToolPermission.WORKSPACE_WRITE
    exposure = ToolExposure.DEFERRED
    reconcile_hint = ReconcileHint(
        verifiable=False,
        suggested_action='Inspect child operations by parent_exec_call_id; completed children '
                         'are committed independently. Never replay the entire exec cell blindly.',
    )
    timeout_seconds = 300.0
    is_code_execution = True
    # Compute is allowed in Plan; every nested tool retains its own Plan gate.
    plan_mode_exempt = True

    description = (
        'Run raw Python source to compose tool calls and filter or aggregate results. '
        'Top-level await is supported: result = await tools.read(path="note.txt"). '
        'Tools return {ok, message, data, error_code, metadata}; check ok before continuing. '
        'For non-identifier tool names use tools["exact-name"](argument_object). '
        'Set result or print the final answer. Python can write isolated /tmp only; '
        'the user workspace and network are accessible only through tools. '
        'Every nested call keeps its own authorization, quota, deadline and ledger. '
        'Calls are serial; recursive exec and blocking user-input tools are unavailable. '
        'Execution is bounded to 300 seconds; output defaults to 10000 tokens. '
        'yield_time_ms yields control while waiting, not a detached background cell. '
        'Prefer direct calls for short ordinary tasks. Docker is required; no host fallback.'
    )

    def __init__(self, sandbox) -> None:
        self._sandbox = sandbox

    def description_for_registry(self, registry) -> str:
        declarations = []
        for tool in registry.list():
            if tool.name == self.name or registry.resolve_exposure(tool).value == 'hidden':
                continue
            declarations.append(
                json.dumps(tool.name) + '(args: ' +
                _typescript(tool.args_schema.model_json_schema()) + '): Promise<ToolResult>;'
            )
        return self.description + (
            '\nAvailable tools (TypeScript signatures describe argument shapes; '
            'write Python when executing):\ninterface Tools {\n' +
            '\n'.join(declarations) + '\n}'
        )

    async def execute(self, args: ExecArgs) -> ToolResult:
        if not isinstance(self._sandbox, DockerSandbox):
            return ToolResult.failure(
                message='exec requires the restricted Docker backend; host execution is disabled.',
                error_code=ErrorCode.PERMISSION_DENIED,
            )
        invoke = nested_tool_call_var.get()
        if invoke is None:
            return ToolResult.failure(
                message='exec requires an invocation-scoped ToolExecutor bridge.',
                error_code=ErrorCode.PERMISSION_DENIED,
            )
        worker = self._sandbox.code_worker()
        start = asyncio.create_task(asyncio.to_thread(
            worker.start, args.code, args.max_output_tokens * 4,
        ))
        reader = None
        chunks = []
        chars = 0
        try:
            await asyncio.shield(start)
            while True:
                reader = asyncio.create_task(asyncio.to_thread(worker.receive))
                while not reader.done():
                    await asyncio.wait(
                        [reader], timeout=min(args.yield_time_ms / 1000, 0.5),
                    )
                frame = reader.result()
                kind = frame.get('type')
                if kind == 'call':
                    name, arguments = frame.get('name'), frame.get('args')
                    if not isinstance(name, str) or not isinstance(arguments, dict):
                        raise ValueError('Invalid code bridge tool request')
                    result = await invoke(name, arguments)
                    await asyncio.to_thread(worker.send, {
                        'type': 'result', 'result': result.model_dump(mode='json'),
                    })
                elif kind == 'output' and isinstance(frame.get('text'), str):
                    text = frame['text'][:max(0, args.max_output_tokens * 4 - chars)]
                    chars += len(text)
                    if text:
                        chunks.append(text)
                elif kind == 'done' and isinstance(frame.get('ok'), bool):
                    output = ''.join(chunks)
                    # Use the repository's conservative offline token counter.
                    truncated = bool(frame.get('truncated'))
                    if estimate_tokens(output) > args.max_output_tokens:
                        low, high = 0, len(output)
                        while low < high:
                            mid = (low + high + 1) // 2
                            if estimate_tokens(output[:mid]) <= args.max_output_tokens:
                                low = mid
                            else:
                                high = mid - 1
                        output, truncated = output[:low], True
                    data = {'output': output, 'truncated': truncated}
                    if frame['ok']:
                        return ToolResult.success(message='Code execution completed.', data=data)
                    return ToolResult.failure(
                        message=str(frame.get('error', 'Code execution failed.'))[:2000],
                        error_code=ErrorCode.TOOL_EXECUTION_ERROR, metadata=data,
                    )
                else:
                    raise ValueError('Invalid code bridge frame')
        finally:
            # Cancellation cannot abandon startup or a live container/thread.
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(start)
            cleanup = asyncio.create_task(asyncio.to_thread(worker.close))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            finally:
                if reader is not None:
                    with suppress(Exception, asyncio.CancelledError):
                        await reader
