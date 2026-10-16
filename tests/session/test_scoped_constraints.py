"""项目/全局级约束规则层（#1191 / #1176 Phase 2）单测。

覆盖：三 scope 落点、项目键归一化（worktree 共享）、历史保留链（不物理删除）、
幂等撤销、原子落盘、损坏文件 fail-closed。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_harness.session.scoped_constraints import (
    ConstraintScope,
    ScopeConstraintError,
    ScopedConstraintStore,
    effective_project_key,
    project_label_for_key,
    resolve_project_key,
)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "t"], check=True
    )


def test_global_register_list_revoke_keeps_history(tmp_path: Path) -> None:
    store = ScopedConstraintStore(tmp_path / "c")
    a = store.register(
        ConstraintScope.GLOBAL,
        fact_type="constraint",
        value="be kind",
        registered_at="2026-10-16T00:00:00.000+00:00",
    )
    assert a.fact_id.startswith("glb-")
    assert [c.value for c in store.list(ConstraintScope.GLOBAL)] == ["be kind"]

    revoked = store.revoke(
        ConstraintScope.GLOBAL,
        a.fact_id,
        revoked_by="local",
        revoked_at="2026-10-16T01:00:00.000+00:00",
    )
    assert revoked is not None and revoked.status == "revoked"
    # 历史保留链：行仍在文件里，只改状态。
    rows = json.loads((tmp_path / "c" / "global.json").read_text("utf-8"))
    assert len(rows) == 1 and rows[0]["status"] == "revoked"
    assert rows[0]["revoked_by"] == "local"
    # 幂等：再撤一次原样返回，不重复写。
    again = store.revoke(
        ConstraintScope.GLOBAL,
        a.fact_id,
        revoked_by="local",
        revoked_at="2026-10-16T02:00:00.000+00:00",
    )
    assert again is not None and again.revoked_at == revoked.revoked_at


def test_project_scope_requires_key_and_is_isolated_per_key(tmp_path: Path) -> None:
    store = ScopedConstraintStore(tmp_path / "c")
    with pytest.raises(ValueError):
        store.register(
            ConstraintScope.PROJECT,
            fact_type="constraint",
            value="x",
            registered_at="t",
        )
    p1 = store.register(
        ConstraintScope.PROJECT,
        project_key="/repo/one",
        fact_type="constraint",
        value="v1",
        registered_at="t1",
    )
    store.register(
        ConstraintScope.PROJECT,
        project_key="/repo/two",
        fact_type="constraint",
        value="v2",
        registered_at="t2",
    )
    assert p1.fact_id.startswith("prj-")
    assert [c.value for c in store.list(ConstraintScope.PROJECT, project_key="/repo/one")] == ["v1"]
    assert [c.value for c in store.list(ConstraintScope.PROJECT, project_key="/repo/two")] == ["v2"]


def test_session_scope_is_rejected_here(tmp_path: Path) -> None:
    store = ScopedConstraintStore(tmp_path / "c")
    with pytest.raises(ValueError):
        store.list(ConstraintScope.SESSION)
    with pytest.raises(ValueError):
        store.register(ConstraintScope.SESSION, fact_type="constraint", value="x", registered_at="t")


def test_corrupt_file_fails_closed(tmp_path: Path) -> None:
    store = ScopedConstraintStore(tmp_path / "c")
    path = tmp_path / "c" / "global.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ScopeConstraintError):
        store.list(ConstraintScope.GLOBAL)


def test_resolve_project_key_shares_across_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(worktree)], check=True
    )
    key_main = resolve_project_key(repo)
    key_wt = resolve_project_key(worktree)
    assert key_main == str(repo.resolve())
    assert key_wt == key_main
    # 子目录同样共享。
    sub = repo / "pkg" / "sub"
    sub.mkdir(parents=True)
    assert resolve_project_key(sub) == key_main


def test_resolve_project_key_non_git_returns_none(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert resolve_project_key(plain) is None


def test_effective_project_key_prefers_persisted_then_cwd(tmp_path: Path) -> None:
    """单一取用口径（审查 finding C1）：持久化键优先；缺失才按 cwd 现场解析。"""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
    resolved = resolve_project_key(repo)
    assert resolved is not None
    # 持久化键在手 → 用它（不理会 cwd，哪怕 cwd 是另一个仓库）。
    assert effective_project_key("/persisted/key", str(repo)) == "/persisted/key"
    # 无持久化键 → 按 cwd 现场解析。
    assert effective_project_key(None, str(repo)) == resolved
    # 两者皆无 → None。
    assert effective_project_key(None, None) is None
    assert effective_project_key(None, str(tmp_path / "nope")) is None


def test_project_label_prefers_owner_repo_then_dirname(tmp_path: Path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git_init(repo)
    assert project_label_for_key(resolve_project_key(repo)) == "myrepo"
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:EricKingWhy/intelligence-agent.git"],
        check=True,
    )
    assert project_label_for_key(resolve_project_key(repo)) == (
        "EricKingWhy/intelligence-agent"
    )
