"""`#318` HTTP / 服务边界：session 作用域预算（durable 行）的可见性与恢复语义。

车票 AC 里属于这一层的那几条：

- session ceiling 到线 ⇒ `run/paused`（`trigger_dimension=session.*`），载荷**自包含**：
  `limits.session`（ceiling）+ 顶层 `session`（账行 CAS 版本 + closeout 之后的最新
  consumed）——客户端凭这一条事件就能组出合法的恢复体，不必再发一轮投影；
- **只抬 session ceiling 的恢复是合法恢复**（session 触发的暂停里，"抬高"发生在
  durable 行上；run 维一个都不点名也成立，run headroom 照判）；
- session 行的 CAS：行**已存在**时点名 `budget.session.*` 必须带
  `budget.session.expected_version`（缺失 ⇒ 422）、版本过期 ⇒ 409，两者都**零副作用**
  （不落 `run/resumed`、不构造模型）；行不存在 = 首次钉死，无版本可竞争；
- **跨 run 聚合**：run 账每 run 清零、session 账不清——durable 行把前一个 run 的
  closeout 请求也记在树账上；
- 投影（`GET /budget`）带 `session` 段（version / limits / consumed / remaining）；
- fork 谱系：`session/forked.data.budget_session` 记父账行快照，父行零写入；
  child = 新身份（自己的账行等它自己的首个 run 才建出）；
- replay 零副作用（`03 §6`）对 session 账同样成立。

账本级机械契约（原子性 / CAS 409 矩阵 / 收窄-only / append-only 审计 / 并发竞争）在
`tests/multiagent/test_session_budget_ledger.py`；本文件只锁 HTTP + 服务层。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.cli import replay_command
from tests.web.test_budget_local_fuse_api import _create_idle_session, _web
from tests.web.test_run_pause_resume_api import (
    _continuation_json,
    _events,
    _one,
    _ScriptedProbe,
    _types,
)


def _done() -> AIMessage:
    return AIMessage(content="完成了")


def _budget(client: TestClient, session_id: str) -> dict:
    resp = client.get(f"/api/sessions/{session_id}/budget")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _session_row(app, session_id: str):  # type: ignore[no-untyped-def]
    """从 web 装配里拿 durable 账行（服务已 `_ensure_stores`，表必在）。"""
    ledger = app.state.agent.stores.delegation_tree_ledger
    return asyncio.run(ledger.get_session_budget(session_id))


def test_session_ceiling_pauses_then_session_raise_resumes(tmp_path):
    """session turns ceiling=1 ⇒ 第一个准入就被拒（预留语义 `0 + 1 >= 1`）；

    恢复**只抬 session ceiling**（run 维一个不点名）+ 账行 CAS ⇒ 同一 run 完成。
    这一条同时钉住 `validate_resume` 的 `#318` 豁免与 `budget.session.expected_version`
    的 happy path。
    """
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_continuation_json()], [_done()])

    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "开始", "budget": {"session": {"max_agent_turns_total": 1}}},
        )
        assert resp.status_code == 200, resp.text
        assert "run/paused" in resp.text, resp.text[:400]

        paused = _one(_events(client, session_id), "run/paused")
        data = paused["data"]
        assert data["reason"] == "budget_exhausted"
        assert data["trigger_dimension"] == "session.max_agent_turns_total"
        # 自包含暂停：ceiling 在 limits.session；账行版本 + consumed 在顶层 session。
        assert data["limits"]["session"]["max_agent_turns_total"] == 1
        assert data["session"]["version"] == 1
        assert data["session"]["consumed"]["agent_turns"] == 0
        # closeout 那次真实请求记进了 session 行（`02 §5.1` 把 closeout 与 primary
        # / fallback 并列）；本剧本不带 usage ⇒ tokens / cost 保持未知（None 粘性）。
        assert data["session"]["consumed"]["model_requests"] == 1

        resume = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": paused["run_id"],
                "resume_basis": "budget_increase",
                "budget": {
                    "expected_version": data["budget_version"],
                    "session": {"max_agent_turns_total": 3, "expected_version": 1},
                },
            },
        )
        assert resume.status_code == 200, resume.text
        assert "run/completed" in resume.text, resume.text[:400]

    assert len(probe.calls) == 2, "恰好两次执行：暂停那次 + 续跑那次"

    events = _events(client, session_id)
    types = _types(events)
    assert types.count("run/started") == 1, "同一 run_id 续跑，不新建 run"
    assert types.count("run/paused") == 1
    assert types.count("run/resumed") == 1
    assert types.count("run/completed") == 1

    # 投影：session 段给出抬升后的 ceiling、CAS 版本、跨 run 的消耗与 remaining
    projection = _budget(client, session_id)
    assert projection["session"]["version"] == 2
    assert projection["session"]["limits"]["max_agent_turns_total"] == 3
    assert projection["session"]["consumed"]["agent_turns"] == 1
    assert projection["session"]["remaining"]["agent_turns"] == 2


def test_session_cas_missing_version_422_and_stale_version_409(tmp_path):
    """行已存在时点名 `budget.session.*` 的恢复：缺 `session.expected_version` ⇒ 422；

    版本过期 ⇒ 409。两者都**零副作用**：事件流一条不添、模型一次不构造。
    """
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_continuation_json()])

    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "开始", "budget": {"session": {"max_agent_turns_total": 1}}},
        )
        assert resp.status_code == 200, resp.text
        assert "run/paused" in resp.text
    paused = _one(_events(client, session_id), "run/paused")
    before = _events(client, session_id)

    with _ScriptedProbe() as rejected:
        missing = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": paused["run_id"],
                "resume_basis": "budget_increase",
                "budget": {
                    "expected_version": paused["data"]["budget_version"],
                    "session": {"max_agent_turns_total": 3},
                },
            },
        )
        assert missing.status_code == 422, missing.text

        stale = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": paused["run_id"],
                "resume_basis": "budget_increase",
                "budget": {
                    "expected_version": paused["data"]["budget_version"],
                    "session": {"max_agent_turns_total": 3, "expected_version": 99},
                },
            },
        )
        assert stale.status_code == 409, stale.text

    assert rejected.calls == [], "被拒请求不得构造模型"
    assert _events(client, session_id) == before, "被拒请求不得落任何事件"


def test_create_rejects_session_expected_version_and_accepts_declaration(tmp_path):
    """新建会话不可能有账行 ⇒ session CAS 版本是矛盾请求（422）；

    不带版本的 session 声明照常接受（launch=false 只建会话，行等首个 run 才建出）。
    """
    app, client = _web(tmp_path)
    with _ScriptedProbe() as probe:
        rejected = client.post(
            "/api/sessions",
            params={"launch": "false"},
            json={"budget": {"session": {"max_agent_turns_total": 5,
                                         "expected_version": 1}}},
        )
        assert rejected.status_code == 422, rejected.text
        assert probe.calls == [], "被拒请求不得构造模型"

        accepted = client.post(
            "/api/sessions",
            params={"launch": "false"},
            json={"budget": {"session": {"max_agent_turns_total": 5}}},
        )
        assert accepted.status_code == 200, accepted.text
    assert asyncio.run(
        app.state.agent.stores.delegation_tree_ledger.get_session_budget(
            accepted.json()["session_id"]
        )
    ) is None, "launch=false 不建账行（首个 run 的首次准入才钉死）"


def test_session_consumption_aggregates_across_runs(tmp_path):
    """run 账每 run 清零、session 账跨 run 累计（`02 §5.1` 的分层语义）。

    run 1 的 run turns ceiling=1 当场暂停（零产出轮），closeout 那一次请求落进
    session 行；run 2 自己只发一次请求、走一个产出轮——但 durable 行里
    `model_requests == 2`：前一个 run 的 closeout 也在树账上。
    """
    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_continuation_json()], [_done()])

    with probe:
        first = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": "任务一",
                "budget": {"run": {"max_agent_turns_total": 1},
                           "session": {"max_agent_turns_total": 2}},
            },
        )
        assert first.status_code == 200, first.text
        assert "run/paused" in first.text
        paused = _one(_events(client, session_id), "run/paused")
        assert paused["data"]["trigger_dimension"] == "run.max_agent_turns_total"

        resume = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": paused["run_id"],
                "resume_basis": "budget_increase",
                "budget": {"expected_version": paused["data"]["budget_version"],
                           "run": {"max_agent_turns_total": 3}},
            },
        )
        assert resume.status_code == 200, resume.text
        assert "run/completed" in resume.text

    row = _session_row(app, session_id)
    assert row.limits.max_agent_turns_total == 2
    # 跨 run 累计：run 2 自己只有 1 请求 + 1 轮，run 1 的 closeout 请求在同一行上
    assert row.consumed.agent_turns == 1
    assert row.consumed.model_requests == 2
    assert row.version == 1, "本场景没有点名 session 维 ⇒ 账行版本不 bump"


def test_fork_records_parent_budget_lineage_and_parent_row_is_untouched(tmp_path):
    """fork = 新 SessionBudget 身份 + 父账行快照进 `session/forked`；父零写入。"""
    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    with _ScriptedProbe() as probe:
        resp = client.post(
            f"/api/sessions/{session_id}/messages", json={"content": "先做这个"},
        )
        assert resp.status_code == 200, resp.text
        assert "run/completed" in resp.text
    assert len(probe.calls) == 1

    before = _session_row(app, session_id)
    assert before is not None and before.consumed.agent_turns == 1

    user_seq = _one(_events(client, session_id), "user/message")["seq"]
    forked = client.post(f"/api/sessions/{session_id}/forks", json={"from_seq": user_seq})
    assert forked.status_code == 200, forked.text
    child_id = forked.json()["session_id"]

    fork_event = _one(_events(client, child_id), "session/forked")
    lineage = fork_event["data"]["budget_session"]
    assert lineage["parent_budget_key"] == session_id
    snapshot = lineage["snapshot"]
    assert snapshot["version"] == before.version
    assert snapshot["consumed"]["agent_turns"] == 1

    # 父行零写入：version 与 consumed 一个不变（fork 不迁移账）
    after = _session_row(app, session_id)
    assert after.version == before.version
    assert after.consumed.as_projection() == before.consumed.as_projection()

    # child = 新身份：自己的账行等它自己的首个 run 才建出
    assert _session_row(app, child_id) is None


def test_replay_consumes_nothing_from_session_budget(tmp_path):
    """replay 零副作用（`03 §6`）对 session 账同样成立：回放前后账行读数一致。"""
    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    with _ScriptedProbe([_continuation_json()]):
        resp = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "开始", "budget": {"session": {"max_agent_turns_total": 1}}},
        )
        assert resp.status_code == 200, resp.text
        assert "run/paused" in resp.text

    before = _session_row(app, session_id)
    assert before is not None
    before_projection = before.as_projection()

    out = asyncio.run(replay_command(session_id, workspace_dir=str(tmp_path)))
    assert out, "回放有输出"

    after = _session_row(app, session_id)
    assert after.as_projection() == before_projection, "回放不得写 session 账"
