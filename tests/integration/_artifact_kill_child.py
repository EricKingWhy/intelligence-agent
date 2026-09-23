"""Real-process worker for the Artifact stored / result event unwritten Gate."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import anyio
from pydantic import BaseModel

from agent_harness.config import Settings
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_CALL,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.storage import OperationContext, SqliteOperationLedger
from agent_harness.storage.local_artifact import LocalArtifactStore
from agent_harness.storage.s3_artifact import S3ArtifactStore
from agent_harness.tooling.approval import ApprovalRequest, ApprovalResponse
from agent_harness.tooling.contract import Tool, ToolPermission, ToolSideEffect
from agent_harness.tooling.executor import ToolExecutor
from agent_harness.tooling.overflow import ArtifactOverflowHandler
from agent_harness.tooling.registry import ToolRegistry
from agent_harness.tooling.result import ToolResult

TOOL_NAME = "artifact_gate_output"
TOOL_CALL_ID = "artifact-gate-call"
RUN_ID = "artifact-gate-run"
ARTIFACT_CONTENT = "artifact-gate-payload-0123456789\n" * 256
OVERFLOW_CHARS = 512


class _Args(BaseModel):
    marker_path: str


class _ArtifactOutputTool(Tool):
    @property
    def name(self) -> str:
        return TOOL_NAME

    @property
    def description(self) -> str:
        return "Write one marker and return deterministic oversized output for recovery testing."

    @property
    def args_schema(self) -> type[BaseModel]:
        return _Args

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER

    async def execute(self, args: BaseModel) -> ToolResult:
        marker_path = Path(args.marker_path)
        await anyio.to_thread.run_sync(_record_side_effect, marker_path)
        return ToolResult.success(
            "Artifact Gate output created.", {"output": ARTIFACT_CONTENT},
        )


def _record_side_effect(marker_path: Path) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    with marker_path.open("a", encoding="utf-8") as marker:
        marker.write("executed\n")


async def _approve(_request: ApprovalRequest) -> ApprovalResponse:
    return ApprovalResponse(approved=True)


async def _run(config: dict[str, str]) -> None:
    provider = config["provider"]
    if provider == "local":
        settings = Settings(
            _env_file=None,
            artifact_dir=config["artifact_dir"],
            artifact_overflow_chars=OVERFLOW_CHARS,
        )
        artifact_store = LocalArtifactStore(
            settings, session_id=config["session_id"],
        )
    elif provider == "qiniu":
        # Keep live Gate credentials in-process per AGENTS.md §4.3; never serialize them.
        settings = Settings(artifact_overflow_chars=OVERFLOW_CHARS)
        artifact_store = S3ArtifactStore(settings, session_id=config["session_id"])
    else:
        raise ValueError("unsupported artifact test provider")

    root = Path(config["root"])
    session_store = JsonlSessionStore(root / "sessions")
    ledger = SqliteOperationLedger(root / "state.db")
    await ledger.initialize()

    session_id = config["session_id"]
    session = Session.start(session_store, session_id=session_id)
    session.append(USER_MESSAGE, {"content": "exercise artifact recovery"})
    session.append(
        MODEL_COMPLETED,
        {
            "content": "",
            "tool_calls": [{"id": TOOL_CALL_ID, "name": TOOL_NAME,
                            "args": {"marker_path": config["marker_path"]}}],
        },
        run_id=RUN_ID,
        step_id=1,
    )
    # AgentRuntime's durable-before-execute ordering: the call event exists before
    # ToolExecutor can cause a side effect or persist an Artifact.
    session.append(
        TOOL_CALL,
        {
            "tool_call_id": TOOL_CALL_ID,
            "tool_name": TOOL_NAME,
            "args": {"marker_path": config["marker_path"]},
        },
        run_id=RUN_ID,
        step_id=1,
    )

    def kill_after_terminal(stage: str, tool_call_id: str) -> None:
        if stage == "terminal" and tool_call_id == TOOL_CALL_ID:
            os._exit(137)

    tools = ToolRegistry()
    tools.register(_ArtifactOutputTool())
    executor = ToolExecutor(
        tools,
        approval_callback=_approve,
        operation_ledger=ledger,
        kill_hook=kill_after_terminal,
        overflow_handler=ArtifactOverflowHandler(
            artifact_store,
            overflow_chars=OVERFLOW_CHARS,
            fail_open=False,
        ),
    )
    await executor.execute(
        {
            "id": TOOL_CALL_ID,
            "name": TOOL_NAME,
            "args": {"marker_path": config["marker_path"]},
        },
        operation_context=OperationContext(
            session_id=session_id, run_id=RUN_ID, agent_id="default",
        ),
        session=session,
        step_id=1,
    )
    raise RuntimeError("artifact Gate kill hook was not reached")


if __name__ == "__main__":
    asyncio.run(_run(json.loads(sys.argv[1])))
