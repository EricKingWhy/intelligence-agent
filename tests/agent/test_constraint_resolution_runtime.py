import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent import AgentRuntime
from agent_harness.agent.types import STATUS_PAUSED
from agent_harness.session import Session
from agent_harness.session.derive import build_protected_fact_data
from agent_harness.session.event import (
    RUN_PAUSED,
    TASK_PROTECTED_FACT,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
)
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry
from agent_harness.tools.request_constraint_resolution import (
    RequestConstraintResolutionTool,
)
from tests.scripted_model import ScriptedModel


@pytest.mark.asyncio
async def test_user_input_request_pauses_same_run_before_another_model_call(tmp_path):
    session = Session.start(JsonlSessionStore(tmp_path))
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    fact = build_protected_fact_data(
        session.events,
        session_id=session.session_id,
        fact_type="constraint",
        value="Use Python.",
        source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, fact)
    candidate = "For this task, use TypeScript."
    user_text = f"The previous rule may not fit: {candidate}"

    model = ScriptedModel([AIMessage(
        content="",
        tool_calls=[{
            "id": "ask-constraint-1",
            "name": "request_constraint_resolution",
            "args": {"fact_id": fact["fact_id"], "candidate": candidate},
        }],
    )])
    registry = ToolRegistry()
    registry.register(RequestConstraintResolutionTool())
    runtime = AgentRuntime(
        model,
        registry,
        ToolExecutor(registry, policy=PermissionPolicy.DANGER_FULL_ACCESS),
    )

    result = await runtime.run(session, user_text)

    request_events = [event for event in session.events if event.type == USER_INPUT_REQUESTED]
    pause_events = [event for event in session.events if event.type == RUN_PAUSED]
    assert result.status == STATUS_PAUSED
    assert len(model.snapshots) == 1
    assert len(request_events) == 1
    assert len(pause_events) == 1
    assert pause_events[0].data["reason"] == "user_input"
    assert pause_events[0].data["input_request_id"] == request_events[0].data["request_id"]
    assert pause_events[0].run_id == request_events[0].run_id
    assert request_events[0].data["candidate"] == candidate
    assert not any(event.type == "run/failed" for event in session.events)
