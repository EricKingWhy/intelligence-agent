from pathlib import Path, PurePosixPath

import pytest

from agent_harness.context.project_instructions import ProjectInstructionStore
from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.tools.read import ReadTool


@pytest.mark.asyncio
async def test_read_loads_instruction_files_for_the_target_directory(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    cwd = repository / "workspace"
    target_directory = cwd / "src"
    target_directory.mkdir(parents=True)
    instruction_file = target_directory / "AGENTS.md"
    instruction_file.write_text("source-scoped rules", encoding="utf-8")
    target_file = target_directory / "module.py"
    target_file.write_text("value = 1", encoding="utf-8")
    store = ProjectInstructionStore()
    store.load_for_session("session-1", cwd)
    sandbox = LocalSubprocessSandbox(workspace_root=cwd)
    tool = ReadTool(
        sandbox,
        project_instructions_loader=lambda path: store.load_for_path(
            "session-1", cwd, cwd / path,
        ),
    )

    result = await tool.execute(tool.args_schema(path="src/module.py"))

    assert result.ok
    assert str(instruction_file) in store.status_for_session("session-1")["source_paths"]


@pytest.mark.asyncio
async def test_read_passes_workspace_relative_paths_from_container_sandboxes() -> None:
    workspace_root = PurePosixPath("/workspace")
    loaded_paths: list[Path] = []

    class _ContainerSandbox:
        @property
        def workspace_root(self) -> PurePosixPath:
            return workspace_root

        def read_text(self, path: str) -> str:
            return "value = 1"

        def resolve_within_workspace(self, path: str) -> PurePosixPath:
            return workspace_root / path

    tool = ReadTool(
        _ContainerSandbox(),  # type: ignore[arg-type]
        project_instructions_loader=loaded_paths.append,
    )

    result = await tool.execute(tool.args_schema(path="src/module.py"))

    assert result.ok
    assert loaded_paths == [Path("src/module.py")]
