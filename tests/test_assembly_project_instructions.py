from __future__ import annotations

import platform
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from agent_harness.assembly import build_runtime, initialize_stores, recovery_stores
from agent_harness.capability.wiring import CapabilityWiring
from agent_harness.config import Settings
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import USER_MESSAGE, JsonlSessionStore, Session


class _ModelFactory:
    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(content="ok")


async def _build(tmp_path: Path, workspace: Path, session_id: str):
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "harness"),
        artifact_dir="",
        model_api_key="test-key",
    )
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    session_store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(session_store, session_id=session_id, cwd=workspace)
    session.append(USER_MESSAGE, {"content": "inspect the source"})
    with patch("agent_harness.assembly.create_chat_model", return_value=_ModelFactory()):
        runtime = await build_runtime(
            settings=settings,
            wiring=CapabilityWiring(),
            stores=stores,
            workspace_registry=WorkspaceRegistry(
                root=tmp_path / "harness", backend="local",
            ),
            session_id=session_id,
            workspace=workspace,
            max_agent_turns=10,
        )
    return runtime, session


@pytest.mark.asyncio
async def test_no_instruction_files_leave_the_model_request_unchanged(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    workspace = repository / "workspace"
    workspace.mkdir()

    runtime, session = await _build(tmp_path, workspace, "session-no-instructions")
    messages = await runtime._context_builder.build(session)
    expected_context = DEFAULT_REGISTRY.assemble("runtime:context_snapshot", {
        "cwd": str(Path.cwd()),
        "os": f"{platform.system()} {platform.release()}",
        "date": date.today().isoformat(),  # noqa: DTZ011 - matches runtime local-date prompt.
    }).meta_user_text

    assert sum(
        isinstance(message, HumanMessage) and message.content == expected_context
        for message in messages
    ) == 1
    assert not any(
        isinstance(message, HumanMessage)
        and "Project instructions" in message.content
        for message in messages
    )


@pytest.mark.asyncio
async def test_runtime_injects_root_rules_then_lazily_loaded_nested_rules(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    root_rules = repository / "AGENTS.md"
    root_rules.write_text("root instruction", encoding="utf-8")
    workspace = repository / "workspace"
    nested = workspace / "src"
    nested.mkdir(parents=True)
    nested_rules = nested / "CLAUDE.md"
    nested_rules.write_text("nested instruction", encoding="utf-8")
    source = nested / "module.py"
    source.write_text("value = 1", encoding="utf-8")

    runtime, session = await _build(tmp_path, workspace, "session-project-instructions")
    first_request = await runtime._context_builder.build(session)
    read_tool = next(tool for tool in runtime.registry.list() if tool.name == "read")
    read_result = await read_tool.execute(read_tool.args_schema(path="src/module.py"))
    second_request = await runtime._context_builder.build(session)

    assert read_result.ok
    first_text = "\n".join(str(message.content) for message in first_request)
    second_text = "\n".join(str(message.content) for message in second_request)
    assert str(root_rules) in first_text and "root instruction" in first_text
    assert str(nested_rules) not in first_text
    assert str(nested_rules) in second_text and "nested instruction" in second_text
