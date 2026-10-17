import os
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from agent_harness.assembly import uses_sandbox_path_semantics
from agent_harness.config import Settings
from agent_harness.context import project_instructions as project_instructions_module
from agent_harness.context.project_instructions import (
    ProjectInstructionSource,
    ProjectInstructionStore,
)


def test_load_for_session_collects_repository_to_cwd_with_sources(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    cwd = repository / "src" / "feature"
    cwd.mkdir(parents=True)
    root_file = repository / "AGENTS.md"
    nested_file = cwd / "CLAUDE.md"
    root_file.write_text("repository rules", encoding="utf-8")
    nested_file.write_text("feature rules", encoding="utf-8")

    snapshot = ProjectInstructionStore().load_for_session("session-1", cwd)

    assert snapshot.project_root == repository
    assert snapshot.source_paths == (root_file, nested_file)
    assert snapshot.prompt_text is not None
    assert snapshot.prompt_text.index("repository rules") < snapshot.prompt_text.index(
        "feature rules"
    )
    assert "Managed/deployment and user-global instructions take precedence" in (
        snapshot.prompt_text
    )
    assert "closest directory take precedence" in snapshot.prompt_text
    assert "do not change system or developer instructions" in snapshot.prompt_text
    assert str(root_file) in snapshot.prompt_text
    assert str(nested_file) in snapshot.prompt_text


def test_missing_instructions_are_visible_without_prompt_content(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    cwd = repository / "src"
    cwd.mkdir()

    snapshot = ProjectInstructionStore().load_for_session("session-2", cwd)

    assert snapshot.status == "missing"
    assert snapshot.prompt_text is None
    assert snapshot.searched_directories == (repository, cwd)


def test_single_and_aggregate_limits_mark_truncated_sources(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    cwd = repository / "src"
    cwd.mkdir()
    root_file = repository / "AGENTS.md"
    nested_file = cwd / "CLAUDE.md"
    root_file.write_text("123456789", encoding="utf-8")
    nested_file.write_text("abcdefghi", encoding="utf-8")

    snapshot = ProjectInstructionStore(
        max_file_bytes=7, max_total_bytes=2048,
    ).load_for_session("session-3", cwd)

    assert snapshot.total_included_bytes == 14
    assert [source.included_bytes for source in snapshot.sources] == [7, 7]
    assert all(source.truncated for source in snapshot.sources)
    assert "TRUNCATED" in (snapshot.prompt_text or "")
    assert snapshot.total_prompt_bytes <= 2048


def test_instruction_changes_require_explicit_reload(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    instruction_file = repository / "AGENTS.md"
    instruction_file.write_text("version one", encoding="utf-8")
    store = ProjectInstructionStore()

    first = store.load_for_session("session-4", repository)
    instruction_file.write_text("version two", encoding="utf-8")
    unchanged = store.load_for_session("session-4", repository)
    reloaded = store.reload_for_session("session-4", repository)

    assert unchanged is first
    assert "version one" in (unchanged.prompt_text or "")
    assert "version two" in (reloaded.prompt_text or "")
    assert "version one" not in (reloaded.prompt_text or "")


def test_reading_a_nested_path_lazily_adds_its_instructions(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    cwd = repository / "workspace"
    cwd.mkdir()
    nested = cwd / "src" / "deep"
    nested.mkdir(parents=True)
    root_file = repository / "AGENTS.md"
    nested_file = nested / "AGENTS.md"
    root_file.write_text("root rules", encoding="utf-8")
    nested_file.write_text("deep rules", encoding="utf-8")
    source_file = nested / "module.py"
    source_file.write_text("value = 1", encoding="utf-8")
    store = ProjectInstructionStore()

    initial = store.load_for_session("session-5", cwd)
    loaded = store.load_for_path("session-5", cwd, source_file)

    assert initial.source_paths == (root_file,)
    assert loaded.source_paths == (root_file, nested_file)
    assert str(nested_file) in (loaded.prompt_text or "")


def test_reload_refreshes_previously_loaded_nested_instructions(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    cwd = repository / "workspace"
    nested = cwd / "src"
    nested.mkdir(parents=True)
    nested_file = nested / "AGENTS.md"
    source_file = nested / "module.py"
    nested_file.write_text("nested version one", encoding="utf-8")
    source_file.write_text("value = 1", encoding="utf-8")
    store = ProjectInstructionStore()
    store.load_for_session("session-reload-nested", cwd)
    store.load_for_path("session-reload-nested", cwd, source_file)

    nested_file.write_text("nested version two", encoding="utf-8")
    reloaded = store.reload_for_session("session-reload-nested", cwd)

    assert nested_file in reloaded.source_paths
    assert "nested version two" in (reloaded.prompt_text or "")
    assert "nested version one" not in (reloaded.prompt_text or "")

    nested_file.unlink()
    reloaded_after_delete = store.reload_for_session("session-reload-nested", cwd)
    assert nested_file not in reloaded_after_delete.source_paths


def test_instruction_prompt_metadata_obeys_the_aggregate_byte_limit(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    cwd = repository / "workspace"
    cwd.mkdir()
    store = ProjectInstructionStore(max_total_bytes=1024)
    store.load_for_session("session-metadata-limit", cwd)

    for index in range(24):
        nested = cwd / f"module_{index:02}"
        nested.mkdir()
        (nested / "AGENTS.md").write_text("", encoding="utf-8")
        source_file = nested / "module.py"
        source_file.write_text("value = 1", encoding="utf-8")
        snapshot = store.load_for_path(
            "session-metadata-limit", cwd, source_file,
        )

    prompt = snapshot.prompt_text or ""
    assert len(prompt.encode("utf-8")) <= 1024
    assert snapshot.total_prompt_bytes == len(("\n\n" + prompt).encode("utf-8"))
    assert snapshot.total_prompt_bytes <= 1024
    assert len(snapshot.source_paths) < 24
    assert snapshot.as_status()["truncated_sources"]


def test_instruction_prompt_byte_count_does_not_rerender_existing_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    cwd = repository / "src" / "feature"
    cwd.mkdir(parents=True)
    (repository / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (cwd / "AGENTS.md").write_text("feature rules", encoding="utf-8")
    (cwd / "CLAUDE.md").write_text("extra rules", encoding="utf-8")

    rendered_source_counts: list[int] = []
    render_prompt = project_instructions_module._render_prompt

    def track_render_prompt(
        sources: tuple[ProjectInstructionSource, ...]
        | list[ProjectInstructionSource],
    ) -> str | None:
        rendered_source_counts.append(len(sources))
        return render_prompt(sources)

    monkeypatch.setattr(
        project_instructions_module, "_render_prompt", track_render_prompt,
    )
    snapshot = ProjectInstructionStore().load_for_session(
        "session-prompt-byte-count", cwd,
    )

    assert rendered_source_counts == []
    prompt = snapshot.prompt_text or ""
    assert rendered_source_counts == [3]
    assert snapshot.total_prompt_bytes == len(("\n\n" + prompt).encode("utf-8"))


def test_instruction_open_rejects_a_path_replaced_during_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    instruction_file = repository / "AGENTS.md"
    instruction_file.write_text("repository rules", encoding="utf-8")
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("outside secret", encoding="utf-8")

    real_os_open = os.open

    def racing_os_open(
        path: str | os.PathLike[str], flags: int, *args: object,
        **kwargs: object,
    ) -> int:
        if Path(path) == instruction_file:
            path = outside_file
        return real_os_open(path, flags, *args, **kwargs)

    isolated_os = SimpleNamespace(
        O_RDONLY=os.O_RDONLY,
        O_BINARY=getattr(os, "O_BINARY", 0),
        O_NOFOLLOW=getattr(os, "O_NOFOLLOW", 0),
        open=racing_os_open,
        fdopen=os.fdopen,
        fstat=os.fstat,
        close=os.close,
    )
    monkeypatch.setattr(project_instructions_module, "os", isolated_os)

    snapshot = ProjectInstructionStore().load_for_session(
        "session-open-race", repository,
    )

    assert os.open is real_os_open
    assert "outside secret" not in (snapshot.prompt_text or "")
    assert snapshot.as_status()["unreadable_sources"]


def test_load_for_path_rejects_targets_outside_the_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    cwd = repository / "workspace"
    cwd.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret = True", encoding="utf-8")
    store = ProjectInstructionStore()
    store.load_for_session("session-path-boundary", cwd)

    with pytest.raises(ValueError, match="outside the project root"):
        store.load_for_path("session-path-boundary", cwd, outside)


def test_status_reports_load_state_sources_and_truncation(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    instruction_file = repository / "AGENTS.md"
    instruction_file.write_text("0123456789", encoding="utf-8")
    store = ProjectInstructionStore(max_file_bytes=4, max_total_bytes=1024)

    assert store.status_for_session("session-6")["status"] == "not_loaded"
    store.load_for_session("session-6", repository)
    status = store.status_for_session("session-6")

    assert status["status"] == "loaded"
    assert status["source_paths"] == [str(instruction_file)]
    assert status["truncated_sources"] == [str(instruction_file)]
    assert status["total_included_bytes"] == 4
    assert status["total_prompt_bytes"] <= 1024


def test_missing_instructions_leave_runtime_context_byte_identical(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    snapshot = ProjectInstructionStore().load_for_session("session-7", repository)
    baseline = "stable runtime context"

    assert snapshot.with_runtime_context(baseline) == baseline


def test_settings_expose_bounded_instruction_size_limits() -> None:
    settings = Settings(
        _env_file=None,
        project_instructions_file_max_bytes=1024,
        project_instructions_total_max_bytes=4096,
    )

    assert settings.project_instructions_file_max_bytes == 1024
    assert settings.project_instructions_total_max_bytes == 4096


def test_instruction_size_limits_reject_zero_and_unsafe_values() -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, project_instructions_file_max_bytes=0)
    with pytest.raises(ValueError):
        Settings(_env_file=None, project_instructions_total_max_bytes=5_000_000)


class _FakeContainerSandbox:
    """Docker 语义替身（#1130 AC-4）：容器内 POSIX 工作区根，指令读写一律经
    Sandbox API；宿主文件系统对它不可见。"""

    def __init__(self, backing: Path) -> None:
        self._backing = backing

    @property
    def workspace_root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    def path_kind(self, relative_path: str) -> str | None:
        target = self._backing / relative_path
        if target.is_file():
            return "file"
        if target.is_dir():
            return "directory"
        return None

    def read_text(self, relative_path: str) -> str:
        return (self._backing / relative_path).read_text(encoding="utf-8")

    def write_text(self, relative_path: str, content: str) -> None:
        (self._backing / relative_path).write_text(content, encoding="utf-8")


def test_reload_for_sandbox_session_reads_through_sandbox_api(tmp_path: Path) -> None:
    """#1130 AC-4（确定性回归，docker-free）：重载走 Sandbox workspace API——
    会话创建后对宿主的指令改动不出现在隔离会话中；私有 workspace 内的改动出现。

    若重载路由退回 `reload_for_session(session_id, host_cwd)`（#1130 症状），
    本用例在 web 层的 Docker 兄弟用例里会以宿主路径/宿主内容变红；
    本用例把 store 侧的分支语义钉死（`uses_sandbox_path_semantics` 判据本身）。
    """
    host_project = tmp_path / "host-project"
    host_project.mkdir()
    host_file = host_project / "AGENTS.md"
    host_file.write_text("host version one", encoding="utf-8")

    container_backing = tmp_path / "container-backing"
    container_backing.mkdir()
    sandbox = _FakeContainerSandbox(container_backing)
    # 前置条件：替身须走沙箱语义。注意本断言不覆盖 web 路由的分流本身——
    # "Docker 会话重载走 sandbox 分支"由 docker-gated 的公共端点用例覆盖
    #（tests/web/test_project_instructions_docker_reload.py）。
    assert uses_sandbox_path_semantics(sandbox) is True

    store = ProjectInstructionStore()
    # 会话创建时的快照导入：私有 workspace 与宿主同内容起步。
    sandbox.write_text("AGENTS.md", "host version one")
    initial = store.load_for_sandbox_session("session-sandbox-reload", sandbox)
    assert initial.source_paths == (PurePosixPath("/workspace/AGENTS.md"),)
    assert "host version one" in (initial.prompt_text or "")

    # 会话创建后：宿主改动（隔离会话不应看到）+ 私有 workspace 改动（应当看到）。
    host_file.write_text("host version two", encoding="utf-8")
    sandbox.write_text("AGENTS.md", "private workspace version")

    reloaded = store.reload_for_sandbox_session("session-sandbox-reload", sandbox)

    assert reloaded.source_paths == (PurePosixPath("/workspace/AGENTS.md"),)
    prompt = reloaded.prompt_text or ""
    assert "private workspace version" in prompt
    assert "host version two" not in prompt
    assert "host version one" not in prompt


def test_reload_for_sandbox_session_keeps_host_cwd_untouched(tmp_path: Path) -> None:
    """#1130 AC-4：沙箱语义重载从不读取宿主 cwd——宿主目录里根本没有
    指令文件时，重载结果是 `missing` 而不是把宿主路径拼进来源。"""
    host_project = tmp_path / "host-project"
    host_project.mkdir()
    container_backing = tmp_path / "container-backing"
    container_backing.mkdir()
    sandbox = _FakeContainerSandbox(container_backing)

    store = ProjectInstructionStore()
    initial = store.load_for_sandbox_session("session-sandbox-missing", sandbox)
    assert initial.status == "missing"
    reloaded = store.reload_for_sandbox_session("session-sandbox-missing", sandbox)
    assert reloaded.status == "missing"
    assert reloaded.source_paths == ()
    assert not any(
        str(path).startswith(str(host_project)) for path in reloaded.source_paths
    )
