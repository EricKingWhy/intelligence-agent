from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.context.project_instructions import ProjectInstructionStore


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
        max_file_bytes=7, max_total_bytes=10,
    ).load_for_session("session-3", cwd)

    assert snapshot.total_included_bytes == 10
    assert [source.included_bytes for source in snapshot.sources] == [7, 3]
    assert all(source.truncated for source in snapshot.sources)
    assert "TRUNCATED" in (snapshot.prompt_text or "")


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
    store = ProjectInstructionStore(max_file_bytes=4, max_total_bytes=8)

    assert store.status_for_session("session-6")["status"] == "not_loaded"
    store.load_for_session("session-6", repository)
    status = store.status_for_session("session-6")

    assert status["status"] == "loaded"
    assert status["source_paths"] == [str(instruction_file)]
    assert status["truncated_sources"] == [str(instruction_file)]
    assert status["total_included_bytes"] == 4


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
