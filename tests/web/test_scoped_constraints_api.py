"""#1191（Phase 2）作用域三层 HTTP 面测试：`effective-constraints` + 项目/全局级撤销。

覆盖：三 scope 合流与分组、项目键/标注、非 git 会话无 PROJECT 组、覆盖关系
（`overridden_by`）、项目/全局级撤销（历史保留链）、非 git 登记项目级 → 422。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from agent_harness.session import Session
from agent_harness.session.event import USER_MESSAGE
from agent_harness.session.scoped_constraints import project_label, resolve_project_key
from tests.web.test_budget_local_fuse_api import _web


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def _seed(tmp_path: Path, *, cwd: Path | None) -> tuple[object, TestClient, str, str]:
    """播种一个会话（可选 cwd）；返回 (app, client, sid, 会话级 fact_id)。"""
    app, client = _web(tmp_path)
    store = app.state.agent.store
    sid = "s-1191"
    session = Session.start(store, session_id=sid, cwd=None if cwd is None else str(cwd))
    content = "回复代码审查意见时先给结论，别长篇铺垫。"
    e1 = session.append(USER_MESSAGE, {"content": content})
    first = session.register_protected_fact(
        fact_type="constraint", value=content, source_event_id=e1.event_id
    )
    return app, client, sid, first.data["fact_id"]


def _get(client: TestClient, sid: str) -> dict:
    resp = client.get(f"/api/sessions/{sid}/effective-constraints")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_effective_constraints_groups_three_scopes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_repo(repo)
    app, client, sid, session_fact = _seed(tmp_path, cwd=repo)
    store = app.state.agent.scoped_constraint_store
    project_key = resolve_project_key(repo)
    assert project_key is not None
    project = store.register(
        "project",
        project_key=project_key,
        fact_type="constraint",
        value="所有 Python 改动先写 failing test。",
        registered_at="2026-10-16T00:00:00.000+00:00",
    )
    glob = store.register(
        "global",
        fact_type="constraint",
        value="和我说话温柔一点。",
        registered_at="2026-10-16T00:00:00.000+00:00",
    )

    body = _get(client, sid)
    scopes = {row["scope"] for row in body["facts"]}
    assert scopes == {"session", "project", "global"}
    by_id = {row["fact_id"]: row for row in body["facts"]}
    assert by_id[session_fact]["scope"] == "session"
    assert by_id[project.fact_id]["scope"] == "project"
    assert by_id[glob.fact_id]["scope"] == "global"
    # 项目元数据：键 + 标注（无 remote → 目录名）。
    assert body["project"] == {"label": project_label(repo)}
    # 无覆盖时 overridden_by 为 None。
    assert by_id[glob.fact_id]["overridden_by"] is None
    # 同一 fact_id 至多一次。
    ids = [row["fact_id"] for row in body["facts"]]
    assert len(ids) == len(set(ids))


def test_non_git_session_has_no_project_group(tmp_path: Path) -> None:
    _app, client, sid, _fact = _seed(tmp_path, cwd=None)
    body = _get(client, sid)
    assert body["project"] is None
    assert all(row["scope"] != "project" for row in body["facts"])


def test_global_overridden_by_session_same_normalized_value(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_repo(repo)
    app, client, sid, session_fact = _seed(tmp_path, cwd=repo)
    store = app.state.agent.scoped_constraint_store
    # 与会话约束 type 相同、value 去首尾空白后逐字相等 → 被会话覆盖。
    glob = store.register(
        "global",
        fact_type="constraint",
        value="  回复代码审查意见时先给结论，别长篇铺垫。  ",
        registered_at="2026-10-16T00:00:00.000+00:00",
    )
    by_id = {row["fact_id"]: row for row in _get(client, sid)["facts"]}
    assert by_id[glob.fact_id]["overridden_by"] == session_fact


def test_revoke_project_and_global_constraints(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_repo(repo)
    app, client, sid, _fact = _seed(tmp_path, cwd=repo)
    store = app.state.agent.scoped_constraint_store
    project_key = resolve_project_key(repo)
    project = store.register(
        "project", project_key=project_key, fact_type="constraint",
        value="项目级约束。", registered_at="2026-10-16T00:00:00.000+00:00",
    )
    glob = store.register(
        "global", fact_type="constraint",
        value="全局约束。", registered_at="2026-10-16T00:00:00.000+00:00",
    )
    r1 = client.post(
        f"/api/memory/constraints/{project.fact_id}/revoke",
        json={"scope": "project", "session_id": sid},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "revoked"
    r2 = client.post(
        f"/api/memory/constraints/{glob.fact_id}/revoke",
        json={"scope": "global", "session_id": sid},
    )
    assert r2.status_code == 200, r2.text
    by_id = {row["fact_id"]: row for row in _get(client, sid)["facts"]}
    assert by_id[project.fact_id]["status"] == "revoked"
    assert by_id[glob.fact_id]["status"] == "revoked"
    # 幂等：再撤一次仍 200。
    r3 = client.post(
        f"/api/memory/constraints/{glob.fact_id}/revoke",
        json={"scope": "global", "session_id": sid},
    )
    assert r3.status_code == 200 and r3.json()["status"] == "revoked"


def test_revoke_unknown_target_is_404(tmp_path: Path) -> None:
    _app, client, sid, _fact = _seed(tmp_path, cwd=None)
    resp = client.post(
        "/api/memory/constraints/glb-nope/revoke",
        json={"scope": "global", "session_id": sid},
    )
    assert resp.status_code == 404, resp.text


def test_register_project_scope_without_git_is_422(tmp_path: Path) -> None:
    _app, client, sid, _fact = _seed(tmp_path, cwd=None)
    content = "请项目级：提交信息用英文。"
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        json={
            "content": content,
            "protected_facts": [
                {"fact_type": "constraint", "value": content, "scope": "project"}
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    assert "不在 git 仓库内" in resp.text
    # 零副作用（审查 finding C4 的回归锚）：登记挪到"所有写成功之后"，被拒请求
    # 不落规则层任何行——旧实现先写规则层再 launch，被拒时项目/全局级约束已生效。
    from agent_harness.session.scoped_constraints import ConstraintScope

    store = _app.state.agent.scoped_constraint_store
    assert store.list(ConstraintScope.GLOBAL) == []


def test_effective_constraints_backfills_project_key_into_session_meta(
    tmp_path: Path,
) -> None:
    """会话元数据惰性补写（#1191 §三.3，审查 finding C1）。

    存量会话（元数据行存在但 `project_key` 为 NULL）首次打开面板即以 cwd 解析出的
    项目键**补写一次**；此后不再重解析（读回持久值）。
    """
    import asyncio

    repo = tmp_path / "repo"
    _git_repo(repo)
    app, client, sid, _fact = _seed(tmp_path, cwd=repo)
    project_key = resolve_project_key(repo)
    assert project_key is not None
    store = app.state.agent.session_meta_store

    # 建表 + 播一条 project_key 为 NULL 的存量元数据行。
    asyncio.run(_seed_null_project_key_meta(app, sid))
    assert asyncio.run(store.get(sid)).project_key is None  # type: ignore[union-attr]

    _get(client, sid)

    assert asyncio.run(store.get(sid)).project_key == project_key  # type: ignore[union-attr]


async def _seed_null_project_key_meta(app: object, sid: str) -> None:
    from agent_harness.storage.session_meta import SessionMeta

    await app.state.agent.ensure_stores()  # type: ignore[attr-defined]
    await app.state.agent.session_meta_store.upsert(  # type: ignore[attr-defined]
        SessionMeta(session_id=sid, created_at="2026-10-16T00:00:00Z")
    )
