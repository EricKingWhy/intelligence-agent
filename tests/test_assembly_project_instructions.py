from __future__ import annotations

import importlib
import platform
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from agent_harness.assembly import (
    _project_instruction_cwd,
    build_runtime,
    initialize_stores,
    recovery_stores,
)
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


def _docker_available() -> bool:
    try:
        docker = importlib.import_module("docker")
        client = docker.from_env(use_context=False)
        client.ping()
        client.images.get("python:3-slim")
        client.close()
    except Exception:  # noqa: BLE001 - integration-test preflight
        return False
    return True


docker_required = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon or python:3-slim image is unavailable",
)


async def _build(
    tmp_path: Path,
    workspace: Path,
    session_id: str,
    *,
    workspace_as_sandbox: bool = False,
    sandbox_workspace_root: Path | None = None,
    sandbox_backend: str | None = None,
):
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
    workspace_registry = WorkspaceRegistry(
        root=tmp_path / "harness", backend="local",
    )
    if workspace_as_sandbox:
        runtime_workspace = workspace_registry.create(
            session_id,
            workspace_root=sandbox_workspace_root or workspace,
        )
    else:
        if sandbox_workspace_root is not None:
            workspace_registry.create(
                session_id,
                workspace_root=sandbox_workspace_root,
            )
        runtime_workspace = workspace
    with patch("agent_harness.assembly.create_chat_model", return_value=_ModelFactory()):
        runtime = await build_runtime(
            settings=settings,
            wiring=CapabilityWiring(),
            stores=stores,
            workspace_registry=workspace_registry,
            session_id=session_id,
            workspace=runtime_workspace,
            max_agent_turns=10,
            sandbox_backend=sandbox_backend,
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


@pytest.mark.asyncio
async def test_fork_runtime_loads_instructions_from_its_workspace_snapshot(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    root_rules = repository / "AGENTS.md"
    root_rules.write_text("root instruction at fork", encoding="utf-8")
    nested = repository / "src"
    nested.mkdir()
    nested_rules = nested / "AGENTS.md"
    nested_rules.write_text("nested instruction at fork", encoding="utf-8")
    (nested / "module.py").write_text("value = 1", encoding="utf-8")

    child_workspace = (
        tmp_path / "harness" / "workspaces" / "session-fork-project-instructions"
    )
    child_workspace.parent.mkdir(parents=True)
    shutil.copytree(repository, child_workspace)
    root_rules.write_text("parent root changed after fork", encoding="utf-8")
    nested_rules.write_text("parent nested changed after fork", encoding="utf-8")

    runtime, session = await _build(
        tmp_path,
        repository,
        "session-fork-project-instructions",
        sandbox_workspace_root=child_workspace,
    )
    read_tool = next(tool for tool in runtime.registry.list() if tool.name == "read")
    read_result = await read_tool.execute(read_tool.args_schema(path="src/module.py"))
    messages = await runtime._context_builder.build(session)
    prompt_text = "\n".join(str(message.content) for message in messages)

    assert read_result.ok
    assert "root instruction at fork" in prompt_text
    assert "nested instruction at fork" in prompt_text
    assert "parent root changed after fork" not in prompt_text
    assert "parent nested changed after fork" not in prompt_text


@pytest.mark.asyncio
async def test_runtime_accepts_an_existing_sandbox_as_workspace_input(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    (repository / "AGENTS.md").write_text("root instruction", encoding="utf-8")
    workspace = repository / "workspace"
    workspace.mkdir()

    runtime, session = await _build(
        tmp_path, workspace, "session-sandbox-workspace",
        workspace_as_sandbox=True,
    )
    messages = await runtime._context_builder.build(session)

    assert "root instruction" in "\n".join(str(message.content) for message in messages)


@pytest.mark.asyncio
async def test_docker_runtime_reads_project_instructions_from_private_workspace(
    tmp_path: Path,
) -> None:
    try:
        docker = importlib.import_module("docker")
        client = docker.from_env(use_context=False)
        client.ping()
    except Exception as exc:  # noqa: BLE001 — environment-dependent integration test
        pytest.skip(f"Docker SDK or daemon is unavailable: {exc}")
    else:
        client.close()

    workspace = tmp_path / "repository" / "workspace"
    nested = workspace / "src"
    nested.mkdir(parents=True)
    (workspace / "AGENTS.md").write_text("copied root instruction", encoding="utf-8")
    (nested / "CLAUDE.md").write_text("copied nested instruction", encoding="utf-8")
    (nested / "module.py").write_text("value = 1", encoding="utf-8")
    session_id = "session-docker-project-instructions"
    try:
        runtime, session = await _build(
            tmp_path, workspace, session_id, sandbox_backend="docker",
        )
        (workspace / "AGENTS.md").write_text("changed host root", encoding="utf-8")
        (nested / "CLAUDE.md").write_text("changed host nested", encoding="utf-8")

        first = await runtime._context_builder.build(session)
        read_tool = next(tool for tool in runtime.registry.list() if tool.name == "read")
        read_result = await read_tool.execute(
            read_tool.args_schema(path="src/module.py")
        )
        second = await runtime._context_builder.build(session)
        first_text = "\n".join(str(message.content) for message in first)
        second_text = "\n".join(str(message.content) for message in second)

        assert "copied root instruction" in first_text
        assert "changed host root" not in first_text
        assert read_result.ok
        assert "copied nested instruction" in second_text
        assert "changed host nested" not in second_text
    finally:
        registry = WorkspaceRegistry(root=tmp_path / "harness", backend="local")
        try:
            registry.delete(session_id)
        except KeyError:
            pass


@docker_required
@pytest.mark.asyncio
async def test_runtime_builds_with_docker_project_instructions(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "harness"),
        artifact_dir="",
        model_api_key="test-key",
    )
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    session_id = "session-docker-project-instructions-read-tool"
    session_store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(session_store, session_id=session_id, cwd=workspace)
    session.append(USER_MESSAGE, {"content": "inspect the source"})
    workspace_registry = WorkspaceRegistry(root=tmp_path / "harness", backend="local")
    sandbox = workspace_registry.create(
        session_id, workspace_root=workspace, backend="docker",
    )

    try:
        sandbox.write_text("AGENTS.md", "Docker workspace instruction")
        sandbox.write_text("module.py", "value = 1")
        with patch(
            "agent_harness.assembly.create_chat_model",
            return_value=_ModelFactory(),
        ):
            runtime = await build_runtime(
                settings=settings,
                wiring=CapabilityWiring(),
                stores=stores,
                workspace_registry=workspace_registry,
                session_id=session_id,
                workspace=sandbox,
                max_agent_turns=10,
            )

        messages = await runtime._context_builder.build(session)
        read_tool = next(tool for tool in runtime.registry.list() if tool.name == "read")
        read_result = await read_tool.execute(read_tool.args_schema(path="module.py"))

        assert "Docker workspace instruction" in "\n".join(
            str(message.content) for message in messages
        )
        assert read_result.ok
        assert read_result.data["content"] == "value = 1"
    finally:
        workspace_registry.delete(session_id)


@docker_required
@pytest.mark.asyncio
async def test_docker_runtime_loads_instructions_only_from_workspace_root(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    (repository / "AGENTS.md").write_text("parent-only instruction", encoding="utf-8")
    workspace = repository / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(
        "workspace instruction", encoding="utf-8",
    )

    runtime, session = await _build(
        tmp_path,
        workspace,
        "session-docker-project-instructions",
        sandbox_backend="docker",
    )
    try:
        messages = await runtime._context_builder.build(session)
        prompt_text = "\n".join(str(message.content) for message in messages)

        assert "workspace instruction" in prompt_text
        assert "parent-only instruction" not in prompt_text
    finally:
        WorkspaceRegistry(root=tmp_path / "harness").delete(
            "session-docker-project-instructions",
        )


def test_project_instruction_root_requires_exact_recorded_path_spelling(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requested_workspace = workspace.with_name("WORKSPACE")
    if not requested_workspace.exists() or not requested_workspace.samefile(workspace):
        pytest.skip("temporary filesystem distinguishes case-only paths")

    registry = WorkspaceRegistry(root=tmp_path / "harness", backend="local")
    session_id = "session-project-instruction-root-spelling"
    registry.create(session_id, workspace_root=workspace)
    sandbox = SimpleNamespace(workspace_root=requested_workspace)

    with pytest.raises(ValueError, match="does not match its recorded workspace roots"):
        _project_instruction_cwd(sandbox, registry, session_id)
