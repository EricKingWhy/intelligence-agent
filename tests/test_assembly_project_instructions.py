from __future__ import annotations

import platform
import shutil
from datetime import date
from pathlib import Path, PurePosixPath
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
        "date": date.today().isoformat(),  # matches runtime local-date prompt.
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
async def test_container_workspace_first_context_build_uses_sandbox_loader(
    tmp_path: Path,
) -> None:
    """#1048 P1 回归：容器（sandbox 语义）工作区会话的**首次 context build** 必须
    用与 `build_runtime` **同源**的 sandbox 指令加载器。

    `build_runtime` 在构建期用 `load_for_sandbox_session` 把会话 cwd 缓存成容器根
    （`/workspace`）；若 `_render_runtime_context` 改用宿主 `instruction_cwd` 加载，
    二者不一致，`ProjectInstructionStore` 会抛 `ValueError: session cwd changed`——
    每个 Docker 会话第一个请求就炸。本用例用不依赖 daemon 的容器替身复现该路径。
    """
    from agent_harness.context.project_instructions import project_instruction_store
    from agent_harness.sandbox.base import ShellEnvironment, ShellFamily

    host_source = tmp_path / "host_source"
    host_source.mkdir()

    class _ContainerSandbox:
        """容器语义面：workspace_root 是容器内 POSIX 路径（非宿主 Path）。"""

        @property
        def workspace_root(self) -> PurePosixPath:
            return PurePosixPath("/workspace")

        @property
        def shell_environment(self) -> ShellEnvironment:
            # 与 DockerSandbox 同款：容器内固定 /bin/sh（OBS-012）。
            return ShellEnvironment(name="/bin/sh", family=ShellFamily.POSIX_SH)

        def path_kind(self, path: str) -> None:
            return None  # 快照为空 ⇒ 无项目指令（本用例只验加载器一致性）

        def resolve_within_workspace(self, path: str) -> PurePosixPath:
            return self.workspace_root / path

    sandbox = _ContainerSandbox()

    class _ContainerRegistry:
        """把会话解析到容器替身（真实注册表在无 daemon 时不可构造 docker 后端）。"""

        def __init__(self, inner: WorkspaceRegistry) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

        def exists(self, session_id: str) -> bool:
            return True

        def get(self, session_id: str) -> _ContainerSandbox:
            return sandbox

        def recorded_workspace_roots(self, session_id: str) -> list[str]:
            return [str(host_source)]

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "harness"),
        artifact_dir="",
        model_api_key="test-key",
    )
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    session_store = JsonlSessionStore(root=tmp_path / "sessions")
    session_id = "session-container-runtime-context"
    session = Session.start(session_store, session_id=session_id, cwd=host_source)
    session.append(USER_MESSAGE, {"content": "inspect the source"})
    registry = _ContainerRegistry(
        WorkspaceRegistry(root=tmp_path / "harness", backend="local")
    )

    with patch("agent_harness.assembly.create_chat_model", return_value=_ModelFactory()):
        runtime = await build_runtime(
            settings=settings,
            wiring=CapabilityWiring(),
            stores=stores,
            workspace_registry=registry,  # type: ignore[arg-type]
            session_id=session_id,
            workspace=sandbox,  # type: ignore[arg-type]
            max_agent_turns=10,
        )
    # 修复前：这一次 build 抛 ValueError("session cwd changed")。
    messages = await runtime._context_builder.build(session)
    assert messages
    # 指令缓存以**容器根**为 cwd（证明走的是 sandbox 加载器，而非宿主 instruction_cwd）。
    status = project_instruction_store(settings).status_for_session(session_id)
    assert status["session_cwd"] == "/workspace", status


# #1048 AC2：`windows_psec` 不再是新会话可选后端（`registry.create` 对它响亮
# ValueError），故无法再经 registry 新建 PSEC 会话、再把该 sandbox 交给
# `build_runtime`。原先两条 Windows-only 用例正是在这条已移除的路径上验证
# 「sandbox 工作区按 POSIX 语义加载项目指令 / ReadTool 读文件」。容器工作区的同类
# 语义现由 `uses_sandbox_path_semantics`（只有 LocalSubprocessSandbox 为宿主语义）
# 统一裁决，并被本文件上面的 `test_container_workspace_first_context_build_uses_sandbox_loader`
# 与 `tests/web/test_workspace_files_docker_semantics.py` 等跨平台用例覆盖。


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
