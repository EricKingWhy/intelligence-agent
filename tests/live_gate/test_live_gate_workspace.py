"""一次性工作区（`#307` R4 / AC「一次性工作区销毁后开发仓库没有副作用」）。

判据的重点是 **`deleted` 必须是核实过的结论**：`LocalSubprocessSandbox.delete()` 在
Windows 上会因 `.git/objects` 只读而静默失败（`rmtree(ignore_errors=True)`），原型实测
留下过整棵 `.git`。所以本文件既测"删干净了"，也测"删不掉时必须如实报 False"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.sandbox.local import LocalSubprocessSandbox
from evaluation.live_gate import repo
from evaluation.live_gate.workspace import (
    SUPPORTED_BACKENDS,
    _assert_env_allowlisted,
    _force_remove,
    create_workspace,
)


def test_workspace_is_created_outside_the_repo_and_supports_production_sandbox() -> None:
    workspace = create_workspace()
    try:
        root = workspace.root.resolve()
        assert root.exists()
        assert repo.REPO_ROOT not in root.parents, "工作区必须在仓库之外"
        assert isinstance(workspace.sandbox, LocalSubprocessSandbox)
        assert workspace.sandbox.workspace_root == root / "workspace"
        assert workspace.record.backend in SUPPORTED_BACKENDS
        assert workspace.record.disposable is True
        assert workspace.record.env_allowlisted is True
        assert workspace.record.deleted is False, "销毁前不得声称已删"
    finally:
        workspace.teardown()


def test_teardown_deletes_and_verifies() -> None:
    workspace = create_workspace()
    root = workspace.root
    # 造一个真实产物，并**在 sandbox 子目录之外**放一个会话轨迹目录（`ScenarioContext.session_root`
    # 就落在那里）：sandbox 的 `delete()` 管不到它，销毁必须由 `teardown()` 收口。
    (root / "workspace" / "notes.txt").write_text("live-gate-ok", encoding="utf-8")
    (root / "sessions" / "s").mkdir(parents=True)
    (root / "sessions" / "s" / "events.jsonl").write_text('{"type": "run/started"}\n', encoding="utf-8")
    record = workspace.teardown()
    assert record.deleted is True
    assert record.teardown == "sandbox.delete+force-remove", "刻意记下真走过的两步"
    assert not root.exists()


def test_teardown_falls_back_to_force_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sandbox.delete()` 抛错 ⇒ 走清只读位的兜底，并**如实记下**用了哪条路径。"""
    workspace = create_workspace()
    root = workspace.root

    def _boom() -> None:
        raise OSError("simulated")

    monkeypatch.setattr(workspace.sandbox, "delete", _boom)
    record = workspace.teardown()
    assert record.teardown == "sandbox.delete+force-remove(OSError)", "异常类型如实记进记录"
    assert record.deleted is True
    assert not root.exists()


def test_teardown_reports_failure_instead_of_pretending(monkeypatch: pytest.MonkeyPatch) -> None:
    """删不掉时 `deleted` 必须是 False —— 证据里的这条结论不允许是"我调用过删除"。"""
    workspace = create_workspace()
    root = workspace.root

    def _boom() -> None:
        raise OSError("simulated")

    monkeypatch.setattr(workspace.sandbox, "delete", _boom)
    monkeypatch.setattr("evaluation.live_gate.workspace._force_remove", lambda path: None)
    record = workspace.teardown()
    assert record.deleted is False
    assert record.teardown == "sandbox.delete+force-remove(OSError)"
    assert root.exists()
    assert record.workspace_ids, "身份仍要在场，便于人工清场"
    _force_remove(root)  # 人工清场，避免临时目录残留


def test_workspace_identity_is_a_hash_not_a_host_path() -> None:
    workspace = create_workspace()
    try:
        identity = workspace.record.workspace_ids[0]
        assert len(identity) == 16
        assert identity.isalnum()
        assert str(workspace.root) not in identity
        assert Path.home().name not in identity
    finally:
        workspace.teardown()


def test_env_passthrough_sandbox_is_rejected(tmp_path: Path) -> None:
    """`passthrough_env=True` 等于把 `.env` 交给模型可执行命令 —— Live Gate 拒绝在此取证。

    ⚠ 工作区必须落在 `tmp_path`：`LocalSubprocessSandbox.delete()` 是 `rmtree`，
    传仓库根进去会真删开发仓库（本用例只关心白名单判据，不该开这个面）。
    """
    sandbox = LocalSubprocessSandbox(workspace_root=tmp_path / "workspace", passthrough_env=True)
    with pytest.raises(RuntimeError, match="env 白名单"):
        _assert_env_allowlisted(sandbox)
