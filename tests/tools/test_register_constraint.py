import pytest

from agent_harness.session.context import (
    ConstraintToolContext,
    current_constraint_tool_context_var,
    current_session_var,
    run_context_var,
)
from agent_harness.session.derive import build_protected_fact_data
from agent_harness.session.event import (
    TASK_PROTECTED_FACT,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
)
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling.approval import needs_approval
from agent_harness.tooling.contract import (
    PermissionPolicy,
    ToolPermission,
    ToolSideEffect,
)
from agent_harness.tools.register_constraint import (
    RegisterConstraintArgs,
    RegisterConstraintTool,
)
from agent_harness.tools.request_constraint_resolution import (
    RequestConstraintResolutionArgs,
    RequestConstraintResolutionTool,
)


def _bind(session, source, *, budget=8192):
    context = ConstraintToolContext(
        session_id=session.session_id, run_id="run-1", agent_id="default",
        agent_profile="main", source_event_id=source.event_id,
        source_seq=source.seq, source_content=source.data["content"],
        protected_fact_token_budget=budget,
    )
    return (
        current_session_var.set(session),
        run_context_var.set("run-1"),
        current_constraint_tool_context_var.set(context),
    )


def _reset(tokens):
    current_constraint_tool_context_var.reset(tokens[2])
    run_context_var.reset(tokens[1])
    current_session_var.reset(tokens[0])


def _session(tmp_path, session_id="constraint-test"):
    return Session.start(JsonlSessionStore(tmp_path), session_id=session_id)


def test_tool_contract_is_add_only_mutating_workspace_write():
    tool = RegisterConstraintTool()

    assert tool.name == "register_constraint"
    assert tool.side_effect is ToolSideEffect.MUTATING
    assert tool.permission is ToolPermission.WORKSPACE_WRITE
    assert set(tool.args_schema.model_fields) == {"value"}


def test_constraint_resolution_wait_is_not_a_workspace_approval():
    tool = RequestConstraintResolutionTool()

    assert tool.permission is ToolPermission.READ_ONLY
    assert tool.side_effect is ToolSideEffect.MUTATING
    assert not needs_approval(tool.permission, PermissionPolicy.READ_ONLY)


@pytest.mark.asyncio
async def test_registers_exact_current_user_substring_once(tmp_path):
    session = _session(tmp_path)
    text = "本次修改不能新增第三方依赖。"
    source = session.append(USER_MESSAGE, {"content": text})
    tokens = _bind(session, source)
    try:
        result = await RegisterConstraintTool().execute(
            RegisterConstraintArgs(value=text)
        )
    finally:
        _reset(tokens)

    facts = [event for event in session.events if event.type == TASK_PROTECTED_FACT]
    assert result.ok
    assert result.data["status"] == "registered"
    assert len(facts) == 1
    assert facts[0].data["fact_type"] == "constraint"
    assert facts[0].data["value"] == text
    assert facts[0].data["source_event_id"] == source.event_id
    assert "supersedes_fact_id" not in facts[0].data


@pytest.mark.asyncio
async def test_rejects_value_not_present_in_frozen_current_source(tmp_path):
    session = _session(tmp_path)
    source = session.append(USER_MESSAGE, {"content": "Use the existing dependency."})
    tokens = _bind(session, source)
    try:
        result = await RegisterConstraintTool().execute(
            RegisterConstraintArgs(value="Add Redis.")
        )
    finally:
        _reset(tokens)

    assert result.ok
    assert result.data["status"] == "rejected"
    assert not any(event.type == TASK_PROTECTED_FACT for event in session.events)


@pytest.mark.asyncio
async def test_conflict_question_is_durable_and_duplicate_call_reuses_it(tmp_path):
    session = _session(tmp_path)
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    fact_data = build_protected_fact_data(
        session.events, session_id=session.session_id, fact_type="constraint",
        value="Use Python.", source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, fact_data)
    fact_id = fact_data["fact_id"]
    candidate = "For this task, use TypeScript."
    current_source = session.append(USER_MESSAGE, {"content": candidate})
    tokens = _bind(session, current_source)
    tool = RequestConstraintResolutionTool()
    try:
        result = await tool.execute(
            RequestConstraintResolutionArgs(fact_id=fact_id, candidate=candidate)
        )
        assert result.ok
        assert result.data["status"] == "requested"
        assert result.pending_events is not None
        assert result.pending_events[0][0] == USER_INPUT_REQUESTED
        session.append(
            result.pending_events[0][0], result.pending_events[0][1], run_id="run-1",
        )

        duplicate = await tool.execute(
            RequestConstraintResolutionArgs(fact_id=fact_id, candidate=candidate)
        )
    finally:
        _reset(tokens)

    requests = [event for event in session.events if event.type == USER_INPUT_REQUESTED]
    assert len(requests) == 1
    assert duplicate.data == {
        "status": "pending",
        "request_id": requests[0].data["request_id"],
    }
    assert requests[0].data["source_event_id"] == current_source.event_id
    assert requests[0].data["fact_id"] == fact_id
    assert tool.batch_exclusive is True


@pytest.mark.asyncio
async def test_conflict_question_rejects_candidate_not_from_current_user(tmp_path):
    session = _session(tmp_path)
    old_source = session.append(USER_MESSAGE, {"content": "Use Python."})
    fact_data = build_protected_fact_data(
        session.events, session_id=session.session_id, fact_type="constraint",
        value="Use Python.", source_event_id=old_source.event_id,
    )
    session.append(TASK_PROTECTED_FACT, fact_data)
    current_source = session.append(USER_MESSAGE, {"content": "Please update the report."})
    tokens = _bind(session, current_source)
    try:
        result = await RequestConstraintResolutionTool().execute(
            RequestConstraintResolutionArgs(
                fact_id=fact_data["fact_id"], candidate="Switch to TypeScript.",
            )
        )
    finally:
        _reset(tokens)

    assert result.ok
    assert result.data["status"] == "rejected"
    assert not result.pending_events
    assert not any(event.type == USER_INPUT_REQUESTED for event in session.events)


def test_register_prompt_rejects_tentative_constraints_and_stops_without_work():
    guidance = RegisterConstraintTool().prompt_guidance.lower()

    assert "maybe" in guidance
    assert "not decided" in guidance
    assert "no requested work" in guidance
    assert "stop" in guidance
    assert "not saved" in guidance
    assert "exactly once before replying" in guidance
    assert "acknowledging or repeating the rule does not save it" in guidance


def test_resolution_schema_and_guidance_require_exact_single_request():
    properties = RequestConstraintResolutionArgs.model_json_schema()["properties"]
    guidance = RequestConstraintResolutionTool().prompt_guidance.lower()

    assert "active protected constraint" in properties["fact_id"]["description"].lower()
    assert "verbatim contiguous text from the current direct user message" in properties["candidate"]["description"].lower()
    assert "omit correction framing such as 'i correct this rule:'" in properties["candidate"]["description"].lower()
    assert "exactly once" in guidance
    assert "wait for the user" in guidance
    assert "do not use another tool" in guidance
    assert "answer to this choice is not a new constraint" in guidance


def test_constraint_guidance_distinguishes_save_confirmation_and_authorization():
    guidance = RegisterConstraintTool().prompt_guidance.lower()

    assert guidance.startswith("classify the current direct user message before calling")
    assert "only claim it was saved after data.status is registered or already_registered" in guidance
    assert "authorization is not a protected fact" in guidance
    assert "i approve you to push this branch" in guidance
    assert "only merge after all tests pass" in guidance


def test_possible_conflict_guidance_requests_clarification_without_deferring():
    guidance = RequestConstraintResolutionTool().prompt_guidance.lower()

    assert "even when the possible requirement is phrased as maybe or might" in guidance
    assert "do not wait for the user to confirm the conflict" in guidance
    assert "a pending card does not authorize the conflicting action" in guidance
    assert "do not infer task-only scope from 'this task' phrasing alone" in guidance
