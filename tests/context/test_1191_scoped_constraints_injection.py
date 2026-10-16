"""#1191（Phase 2 / Fork 3）注入侧单测：项目/全局级约束→上下文注入。

覆盖：`scoped_context_facts` 的 active/覆盖过滤，以及装配层供给器
`_build_scoped_fact_provider` 的项目键**取值口径**——持久化键优先、缺失回落现场
cwd（审查 finding C1：面板与注入必须同源，否则面板列着、注入取不到）。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from agent_harness.assembly import _build_scoped_fact_provider
from agent_harness.config import Settings
from agent_harness.session.derive import ProtectedFact
from agent_harness.session.event import SESSION_STARTED, SessionEvent
from agent_harness.session.scoped_constraints import (
    ScopedConstraintStore,
    resolve_project_key,
    scoped_context_facts,
)
from agent_harness.storage.session_meta import SessionMeta
from agent_harness.storage.sqlite import SqliteSessionMetaStore

_REGISTERED_AT = "2026-10-16T00:00:00.000+00:00"


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def _session_fact(fact_id: str, value: str) -> ProtectedFact:
    return ProtectedFact(
        fact_id=fact_id,
        type="constraint",
        value=value,
        source_event_id=f"ev-{fact_id}",
        source_seq=1,
        status="active",
        session_id="s-1191",
    )


def _events(cwd: Path) -> list[SessionEvent]:
    return [
        SessionEvent(
            type=SESSION_STARTED, session_id="s-1191", data={"cwd": str(cwd)},
        )
    ]


def test_scoped_context_facts_injects_project_and_global_skips_overridden(
    tmp_path: Path,
) -> None:
    """规则层 active 条目注入；被会话级同文覆盖的项目/全局条目不注入。"""
    store = ScopedConstraintStore(tmp_path / "constraints")
    key = "/repos/demo"
    project = store.register(
        "project", project_key=key, fact_type="constraint",
        value="所有 Python 改动先写 failing test。", registered_at=_REGISTERED_AT,
    )
    global_ = store.register(
        "global", fact_type="constraint",
        value="和我说话温柔一点。", registered_at=_REGISTERED_AT,
    )
    # 与 global 同文（去首尾空白后逐字相等）的会话级约束 → 该 global 条目不注入。
    overridden_global = store.register(
        "global", fact_type="constraint",
        value="  先给结论。  ", registered_at=_REGISTERED_AT,
    )
    session_facts = [_session_fact("sf-1", "先给结论。")]

    facts = scoped_context_facts(
        store, session_facts, session_id="s-1191", project_key=key,
    )
    ids = {fact.fact_id for fact in facts}
    assert ids == {project.fact_id, global_.fact_id}
    assert overridden_global.fact_id not in ids
    # 注入体带合成来源（无会话事件），scope 如实。
    injected = {fact.fact_id: fact for fact in facts}
    assert injected[project.fact_id].scope == "project"
    assert injected[global_.fact_id].scope == "global"
    assert injected[project.fact_id].source_event_id == f"scoped:{project.fact_id}"


def test_scoped_context_facts_skips_revoked(tmp_path: Path) -> None:
    """撤销的规则层条目不再注入（历史保留链的读取面）。"""
    store = ScopedConstraintStore(tmp_path / "constraints")
    revoked = store.register(
        "global", fact_type="constraint", value="旧的。", registered_at=_REGISTERED_AT,
    )
    store.revoke("global", revoked.fact_id, revoked_at=_REGISTERED_AT)
    assert scoped_context_facts(
        store, [], session_id="s-1191", project_key=None,
    ) == []


def test_provider_uses_persisted_project_key_over_live_cwd(tmp_path: Path) -> None:
    """供给器按元数据持久化的 project_key 取项目级约束（审查 finding C1）。

    场景：会话记录 key=A 后，本次 build 的 cwd 已解析到**另一个仓库** B——供给器
    仍须用 A（与面板/登记/撤销同源），否则面板列着 A 的条目、注入侧却取不到。
    """
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _git_repo(repo_a)
    _git_repo(repo_b)
    key_a = resolve_project_key(repo_a)
    assert key_a is not None and resolve_project_key(repo_b) != key_a

    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
    )
    store = ScopedConstraintStore(Path(settings.workspace_dir) / "constraints")
    registered = store.register(
        "project", project_key=key_a, fact_type="constraint",
        value="项目 A 的规则。", registered_at=_REGISTERED_AT,
    )
    meta_store = SqliteSessionMetaStore(Path(settings.workspace_dir) / "harness.db")
    asyncio.run(meta_store.initialize())
    asyncio.run(
        meta_store.upsert(
            SessionMeta(
                session_id="s-1191", created_at=_REGISTERED_AT, project_key=key_a,
            )
        )
    )

    provider = _build_scoped_fact_provider(settings)
    facts = provider(_events(repo_b))  # cwd 指向 B，但持久化键是 A

    assert [fact.fact_id for fact in facts] == [registered.fact_id]


def test_provider_falls_back_to_live_cwd_without_persisted_key(tmp_path: Path) -> None:
    """元数据没有持久化键时按 cwd 现场解析（票面 §三.3 的兜底口径）。"""
    repo = tmp_path / "repo"
    _git_repo(repo)
    key = resolve_project_key(repo)
    assert key is not None

    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
    )
    store = ScopedConstraintStore(Path(settings.workspace_dir) / "constraints")
    registered = store.register(
        "project", project_key=key, fact_type="constraint",
        value="现场解析的规则。", registered_at=_REGISTERED_AT,
    )
    # 库/表都不建（模拟直构装配的缺省环境）→ 读持久化键回落 None → 现场 cwd 解析。
    provider = _build_scoped_fact_provider(settings)

    assert [fact.fact_id for fact in provider(_events(repo))] == [registered.fact_id]
