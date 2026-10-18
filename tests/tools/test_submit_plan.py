"""#1356 方案 2：`submit_plan` 工具（对标 Claude Code `ExitPlanMode`）。

接缝分两层：
- **门禁层**（AC3/AC4）：真 `LocalSubprocessSandbox` + 真 `ToolRegistry` + 真
  `ToolExecutor`，档位经 `workflow/mode-changed` durable 事件设定——与
  `tests/tooling/test_executor_526.py` 同款。执行期上下文（会话 / run / 准入）
  用 `tests/tools/test_ask_user.py` 的 ContextVar 绑定接缝补上，断言 `execute`
  **真的跑到**（不是"没被门禁拦"这种弱断言）。
- **行为层**（AC5/AC8）：真 `Session` + `FakeArtifactStore`，断言 durable 事件流
  与 artifact 往返（不变量 #22：只认事件流）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.session.context import (
    ConstraintToolContext,
    current_constraint_tool_context_var,
    current_session_var,
    run_context_var,
)
from agent_harness.session.event import USER_INPUT_REQUESTED
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.session.workflow import WorkflowMode, append_workflow_mode_change
from agent_harness.storage.artifact import FakeArtifactStore
from agent_harness.tooling import ErrorCode, ToolExecutor, ToolRegistry
from agent_harness.tools.submit_plan import SubmitPlanArgs, SubmitPlanTool

_PLAN = (
    "# 计划\n\n## 目标\n把 A 改成 B。\n\n## 分步改动\n1. 改 a.py\n2. 跑测试\n"
)


def _session(tmp_path: Path, session_id: str = "submit-plan-test") -> Session:
    return Session.start(JsonlSessionStore(root=tmp_path / "sessions"),
                         session_id=session_id)


def _bind(session: Session, *, run_id: str = "run-1"):
    context = ConstraintToolContext(
        session_id=session.session_id,
        run_id=run_id,
        agent_id="default",
        agent_profile="main",
        source_event_id="src",
        source_seq=0,
        source_content="",
        protected_fact_token_budget=8192,
    )
    return (
        current_session_var.set(session),
        run_context_var.set(run_id),
        current_constraint_tool_context_var.set(context),
    )


def _reset(tokens) -> None:
    current_constraint_tool_context_var.reset(tokens[2])
    run_context_var.reset(tokens[1])
    current_session_var.reset(tokens[0])


def _tc(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"id": call_id, "name": name, "args": args}


def _args(markdown: str = _PLAN, files: list[str] | None = None) -> dict:
    return {
        "plan_markdown": markdown,
        "touched_files": files if files is not None else ["src/a.py", "tests/test_a.py"],
    }


# ── 参数校验 ────────────────────────────────────────────────────────────


def test_args_forbid_extra_fields():
    with pytest.raises(ValidationError):
        SubmitPlanArgs(**_args(), extra_field=1)


def test_plan_markdown_at_the_limit_is_accepted():
    args = SubmitPlanArgs(**_args(markdown="x" * 100_000))
    assert len(args.plan_markdown) == 100_000


def test_plan_markdown_over_the_limit_is_rejected():
    with pytest.raises(ValidationError):
        SubmitPlanArgs(**_args(markdown="x" * 100_001))


def test_empty_plan_markdown_is_rejected():
    with pytest.raises(ValidationError):
        SubmitPlanArgs(**_args(markdown="   \n  "))


# ── AC3 / AC4：Plan 门禁 ────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path / "ws")


@pytest.fixture
def store() -> FakeArtifactStore:
    return FakeArtifactStore()


@pytest.fixture
def registry(sandbox, store) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(SubmitPlanTool(store))
    return reg


@pytest.mark.asyncio
async def test_plan_mode_lets_submit_plan_reach_execute(registry, sandbox, store, tmp_path):
    """AC3：PLAN 档下 `plan_mode_exempt` 放行，`execute` 真的跑到（落出请求事件）。"""
    session = _session(tmp_path)
    append_workflow_mode_change(session, WorkflowMode.PLAN)
    tokens = _bind(session)
    try:
        execution = await ToolExecutor(registry).execute(
            _tc("submit_plan", _args()), session=session,
        )
    finally:
        _reset(tokens)

    assert execution.result.ok is True, execution.result.message
    assert execution.result.error_code is None
    assert execution.result.data["status"] == "requested"
    # 执行确实发生了：pending_events 里带出了请求载荷。
    assert any(
        event_type == USER_INPUT_REQUESTED
        for event_type, _data in execution.result.pending_events
    )


@pytest.mark.asyncio
async def test_normal_mode_fails_and_points_at_update_plan(registry, sandbox, store, tmp_path):
    """AC4：NORMAL 档下失败，错误文案指路 `update_plan`。"""
    session = _session(tmp_path)
    tokens = _bind(session)
    try:
        execution = await ToolExecutor(registry).execute(
            _tc("submit_plan", _args()), session=session,
        )
    finally:
        _reset(tokens)

    assert execution.result.ok is False
    assert execution.result.error_code == ErrorCode.INVALID_ARGUMENT
    assert "update_plan" in execution.result.message
    # 整单拒绝：无事件、无 artifact。
    assert not execution.result.pending_events
    assert store._artifacts == {}


# ── AC5 / AC8：暂停载荷 + artifact 往返 ────────────────────────────────


@pytest.mark.asyncio
async def test_execute_returns_plan_approval_request(store, tmp_path):
    """AC5：`USER_INPUT_REQUESTED(kind="plan_approval")`，载荷字段齐全。"""
    session = _session(tmp_path)
    append_workflow_mode_change(session, WorkflowMode.PLAN)
    tokens = _bind(session)
    try:
        result = await SubmitPlanTool(store).execute(SubmitPlanArgs(**_args()))
    finally:
        _reset(tokens)

    assert result.ok is True
    assert result.data["status"] == "requested"
    request_id = result.data["request_id"]

    assert len(result.pending_events) == 1
    event_type, payload = result.pending_events[0]
    assert event_type == USER_INPUT_REQUESTED
    assert payload["kind"] == "plan_approval"
    assert payload["request_id"] == request_id
    assert payload["run_id"] == "run-1"
    assert payload["plan_markdown"] == _PLAN
    assert payload["touched_files"] == ["src/a.py", "tests/test_a.py"]
    assert payload["plan_sha256"] == hashlib.sha256(_PLAN.encode("utf-8")).hexdigest()
    assert isinstance(payload["artifact_id"], str) and payload["artifact_id"]


@pytest.mark.asyncio
async def test_plan_is_persisted_to_the_artifact_store(store, tmp_path):
    """AC8：artifact 内容与提交正文逐字节一致，可按既有读回接口取回。"""
    session = _session(tmp_path)
    append_workflow_mode_change(session, WorkflowMode.PLAN)
    tokens = _bind(session)
    try:
        result = await SubmitPlanTool(store).execute(SubmitPlanArgs(**_args()))
    finally:
        _reset(tokens)

    artifact_id = result.pending_events[0][1]["artifact_id"]
    artifact = await store.load(artifact_id)
    assert artifact.content == _PLAN
    assert artifact.source_tool == "submit_plan"
    assert artifact.mime_type == "text/markdown"


@pytest.mark.asyncio
async def test_second_call_in_same_run_does_not_duplicate_the_request(store, tmp_path):
    """每 run 单请求去重（复用 `user_input_common`）：已待批时返回 pending。"""
    session = _session(tmp_path)
    append_workflow_mode_change(session, WorkflowMode.PLAN)
    tokens = _bind(session)
    try:
        first = await SubmitPlanTool(store).execute(SubmitPlanArgs(**_args()))
        # 模拟 executor 的 durable 先行落盘（真实路径由 executor 完成）。
        session.append(USER_INPUT_REQUESTED, first.pending_events[0][1], run_id="run-1")
        second = await SubmitPlanTool(store).execute(SubmitPlanArgs(**_args()))
    finally:
        _reset(tokens)

    assert second.ok is True
    assert second.data["status"] == "pending"
    assert second.data["request_id"] == first.data["request_id"]
    assert not second.pending_events


@pytest.mark.asyncio
async def test_missing_session_fails_loudly(store):
    """无 run 上下文（装配缺失）⇒ TOOL_EXECUTION_ERROR，不静默成功。"""
    result = await SubmitPlanTool(store).execute(SubmitPlanArgs(**_args()))
    assert result.ok is False
    assert result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
