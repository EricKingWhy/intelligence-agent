"""Real-cloud privacy gate for Memory V2 traces; leaves synthetic traces as evidence."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from agent_harness.observability import LangfuseSink
from agent_harness.session import RUN_COMPLETED
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


def _scope_payload(item):
    for name in ("scope", "instrumentation_scope"):
        scope = getattr(item, name, None)
        if scope is None:
            continue
        if isinstance(scope, Mapping):
            return dict(scope)
        for method_name in ("model_dump", "dict"):
            method = getattr(scope, method_name, None)
            if callable(method):
                value = method()
                if isinstance(value, Mapping):
                    return dict(value)
        attributes = getattr(scope, "attributes", None)
        if isinstance(attributes, Mapping):
            return {"attributes": dict(attributes)}
    attributes = getattr(item, "scope_attributes", None)
    return {"attributes": dict(attributes)} if isinstance(attributes, Mapping) else None


def _is_sdk_public_key_path(path: str) -> bool:
    parts = path.split(".")
    if len(parts) == 5:
        return (
            parts[0] == "trace_payload"
            and _is_index(parts[1], "traces")
            and parts[2:] == ["scope", "attributes", "public_key"]
        )
    return (
        len(parts) == 6
        and parts[0] == "trace_payload"
        and _is_index(parts[1], "traces")
        and _is_index(parts[2], "observations")
        and parts[3:] == ["scope", "attributes", "public_key"]
    )


def _is_index(value: str, name: str) -> bool:
    prefix = f"{name}["
    return value.startswith(prefix) and value.endswith("]") and value[len(prefix):-1].isdigit()


def test_langfuse_scope_payload_preserves_sdk_public_key_path():
    payload = _scope_payload(SimpleNamespace(
        instrumentation_scope=SimpleNamespace(attributes={"public_key": "public-route-id"}),
    ))

    assert payload == {"attributes": {"public_key": "public-route-id"}}


def test_langfuse_public_key_exception_is_limited_to_trace_scope_fields():
    assert _is_sdk_public_key_path(
        "trace_payload.traces[0].scope.attributes.public_key",
    )
    assert _is_sdk_public_key_path(
        "trace_payload.traces[0].observations[1].scope.attributes.public_key",
    )
    assert not _is_sdk_public_key_path(
        "trace_payload.traces[0].metadata.scope.attributes.public_key",
    )


@pytest.mark.integration
@pytest.mark.live_services
@pytest.mark.asyncio
async def test_memory_v2_real_langfuse_trace_omits_content(tmp_path):
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    if not public_key or not secret_key:
        pytest.skip("Configure Langfuse credentials for the live privacy gate")

    from langfuse import Langfuse
    from langfuse.api.commons.errors.not_found_error import NotFoundError

    base_url = os.environ.get("LANGFUSE_BASE_URL", "")
    class _RecordingLangfuseSink(LangfuseSink):
        memory_trace_ids: set[str]

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.memory_trace_ids = set()

        def start_observation(self, *, name: str, **kwargs):
            observation = super().start_observation(name=name, **kwargs)
            if name.startswith("memory-") and observation is not None:
                self.memory_trace_ids.add(observation.trace_id)
            return observation

    sink = _RecordingLangfuseSink(
        public_key=public_key,
        secret_key=secret_key,
        base_url=base_url,
        trace_content="full",
        tracing_environment="memory-v2-302",
    )

    from agent_harness.memory.v2.capability import MemoryV2Service
    from agent_harness.memory.v2.index import InMemoryMemoryV2Index
    from agent_harness.memory.v2.jobs import SqliteMemoryV2JobStore
    from agent_harness.memory.v2.store import SqliteMemoryV2Store
    from tests.memory.v2.test_v2_executor import (
        Env,
        FakeInvoker,
        _add,
        _adjudication,
        _candidate,
        _formation_candidates,
        _run,
    )

    store = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await store.initialize()
    jobs = SqliteMemoryV2JobStore(tmp_path / "memory-v2.db")
    await jobs.initialize()
    index = InMemoryMemoryV2Index()
    env = Env(store=store, jobs=jobs, index=index, service=MemoryV2Service(store, index))

    class _SyntheticMemoryFormation:
        async def notify_run_finished(self, **_kwargs):
            _job, result, _events = await _run(
                env,
                FakeInvoker(
                    formation=[_formation_candidates(_candidate())],
                    adjudication=[_adjudication(_add())],
                ),
                observer=sink.memory_observation,
            )
            assert result is not None and result.outcome.value == "committed"

    marker = uuid4().hex
    private_prompt = f"synthetic-memory-v2-prompt-{marker}"
    private_answer = f"synthetic-memory-v2-answer-{marker}"
    registry = ToolRegistry()
    runtime = AgentRuntime(
        ScriptedModel([AIMessage(content=private_answer)]),
        registry,
        ToolExecutor(registry),
        memory_formation=_SyntheticMemoryFormation(),
        observability_sink=sink,
    )
    session = make_session(tmp_path / "session")
    client = None
    try:
        result = await runtime.run(session, private_prompt)
        assert result.status == "completed"
        completed = next(event for event in session.events if event.type == RUN_COMPLETED)
        trace_id = completed.data["trace_id"]
        assert trace_id
        sink.flush()

        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url or None,
        )
        trace_ids = {trace_id, *sink.memory_trace_ids}
        traces = {}
        for target_trace_id in trace_ids:
            for _attempt in range(16):
                try:
                    candidate = client.api.trace.get(target_trace_id)
                    minimum_observations = 2 if target_trace_id == trace_id else 1
                    if len(candidate.observations or []) >= minimum_observations:
                        traces[target_trace_id] = candidate
                        break
                except NotFoundError:
                    pass
                await asyncio.sleep(3)
        assert trace_id in traces, "60s 内未回捞到完整 Memory V2 trace"
        assert sink.memory_trace_ids <= traces.keys(), "Memory V2 observations 未全部落入 Langfuse"

        observations = [
            observation
            for trace in traces.values()
            for observation in (trace.observations or [])
        ]
        runtime_trace = traces[trace_id]
        assert runtime_trace.input in (None, "", {})
        assert runtime_trace.output in (None, "", {})
        assert observations
        assert all(
            getattr(observation, "input", None) in (None, "", {})
            and getattr(observation, "output", None) in (None, "", {})
            for observation in observations
        )
        generation = next(item for item in observations if item.type == "GENERATION")
        assert len(generation.metadata["input_sha256"]) == 64
        assert len(generation.metadata["output_sha256"]) == 64
        memory_observations = [
            item for item in observations if item.name.startswith("memory-")
        ]
        assert {item.name for item in memory_observations} >= {
            "memory-job", "memory-model", "memory-schema", "memory-formation",
            "memory-selection", "memory-adjudication", "memory-apply",
        }
        assert all(item.metadata and item.metadata.get("observation") for item in memory_observations)
        payload_data = {
            "traces": [
                {
                    "input": trace.input,
                    "output": trace.output,
                    "metadata": trace.metadata,
                    "scope": _scope_payload(trace),
                    "observations": [
                        {
                            "input": getattr(item, "input", None),
                            "output": getattr(item, "output", None),
                            "metadata": getattr(item, "metadata", None),
                            "scope": _scope_payload(item),
                        }
                        for item in (trace.observations or [])
                    ],
                }
                for trace in traces.values()
            ],
        }
        payload = json.dumps(payload_data, ensure_ascii=False, default=str)
        assert private_prompt not in payload
        assert private_answer not in payload
        assert "请以后都用 pnpm 装依赖" not in payload
        assert "用户偏好用 pnpm 安装依赖" not in payload
        assert "请用 pnpm" not in payload
        def matching_paths(value, needle: str, path: str) -> list[str]:
            if isinstance(value, dict):
                return [
                    match
                    for key, item in value.items()
                    for match in matching_paths(item, needle, f"{path}.{key}")
                ]
            if isinstance(value, (list, tuple)):
                return [
                    match
                    for index, item in enumerate(value)
                    for match in matching_paths(item, needle, f"{path}[{index}]")
                ]
            if isinstance(value, str) and needle in value:
                return [path]
            return []

        public_key_paths = matching_paths(payload_data, public_key, "trace_payload")
        unexpected_public_key_paths = [
            path for path in public_key_paths
            if not _is_sdk_public_key_path(path)
        ]
        secret_key_paths = matching_paths(payload_data, secret_key, "trace_payload")
        assert not unexpected_public_key_paths and not secret_key_paths, (
            "Langfuse trace exposed a secret or placed its public routing key outside "
            "the SDK instrumentation scope: "
            f"public_key_paths={unexpected_public_key_paths} secret_key_paths={secret_key_paths}"
        )
        print(
            f"[memory v2 langfuse] trace_ids={','.join(sorted(traces))} "
            f"trace_count={len(traces)} "
            f"observations={len(observations)} memory_observations={len(memory_observations)} "
            "content_fields=omitted synthetic=true"
        )
    finally:
        try:
            sink.shutdown()
        finally:
            if client is not None:
                client.shutdown()
