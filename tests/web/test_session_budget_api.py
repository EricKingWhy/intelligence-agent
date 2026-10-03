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
import shutil
import time

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.cli import replay_command
from tests.web.test_budget_local_fuse_api import (
    _create_idle_session,
    _ModelProbe,
    _web,
)
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


def test_launch_false_session_declaration_is_persisted(tmp_path):
    """#518 BUG-11：launch=false 建会话时 `budget.session.*` 必须持久化。

    修复前：声明只活在本次请求的 handle 里——账行等首 run 惰性建出，而只建会话
    没有 run；下一个 run 的 handle 拿不到创建时声明，建行落成全 None ⇒
    「200 但丢弃」（票面实测：GET /budget 读回 limits 全 NULL）。
    修复后：声明显式存在 ⇒ 创建时立即钉死账行（行不存在 = 首次钉死，无版本
    可竞争，与 resume 路径同款）；未声明保持 #318 惰性语义不变。
    """
    app, client = _web(tmp_path)
    resp = client.post(
        "/api/sessions",
        params={"launch": "false"},
        json={"budget": {"session": {"max_total_tokens": 100}}},
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]

    budget = _budget(client, session_id)
    session_view = budget["session"]
    assert session_view["limits"]["max_total_tokens"] == 100, session_view

    # 账行确实存在（投影读的是 durable 行，不是请求内存）。
    row = asyncio.run(
        app.state.agent.stores.delegation_tree_ledger.get_session_budget(session_id)
    )
    assert row is not None
    assert row.limits.max_total_tokens == 100


def test_create_rejects_session_expected_version_and_accepts_declaration(tmp_path):
    """新建会话不可能有账行 ⇒ session CAS 版本是矛盾请求（422）；

    不带版本的 session 声明照常接受（`#518` 起：声明显式存在 ⇒ 创建时立即
    建行持久化；未声明时行仍等首个 run 才建出）。
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
    row = asyncio.run(
        app.state.agent.stores.delegation_tree_ledger.get_session_budget(
            accepted.json()["session_id"]
        )
    )
    assert row is not None, "显式声明必须持久化（#518：不再有「200 但丢弃」）"
    assert row.limits.max_agent_turns_total == 5


def _read_call() -> AIMessage:
    """第一轮的**工具调用**决策（票面复现形态：多步 run 的第一步）。

    read 一个不存在的文件——工具失败照样产生 ToolResult、循环照样进第二轮，
    本用例关心的是"存在第二轮准入点"，不是工具成败。
    """
    return AIMessage(content="", tool_calls=[{
        "id": "tc-unknown-1", "name": "read",
        "args": {"path": "does-not-exist.txt"}, "type": "tool_call",
    }])


def test_unknown_accounting_pause_carries_evidence(tmp_path):
    """#518 BUG-10：由**账目未知**触发的 fail-closed 暂停带 `accounting_unknown` 依据。

    票面复现形态：多步 run（工具调用后第二轮）+ 模型不返回 usage（`_done()`
    不带 usage_metadata）。第一轮准入放行时行读数是"空和" 0（`11 §6.1`：只有
    一个请求都没有的空和才是 0）；第一轮请求落账后行转 NULL 粘住（未知）⇒
    第二轮准入 fail-closed 拦截——载荷必须显式说明"这次暂停是未知触发"，
    否则 "budget_exhausted + consumed:null" 运维无法解释。
    """
    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_read_call(), _done()])

    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "开始", "budget": {"session": {"max_total_tokens": 100}}},
        )
        assert resp.status_code == 200, resp.text

    # fail-closed 暂停落在**下一个准入点**（票面实测：第二轮准入），SSE 流可能在
    # 它之前收束 ⇒ 轮询事件存储等落盘，而不是断言流文本。
    paused_event: dict | None = None
    for _ in range(100):
        matched = [e for e in _events(client, session_id) if e["type"] == "run/paused"]
        if matched:
            paused_event = matched[0]
            break
        time.sleep(0.1)
    assert paused_event is not None, "无 usage 模型配 token ceiling ⇒ 第二轮准入必暂停"
    data = paused_event["data"]
    assert data["reason"] == "budget_exhausted"
    assert data["trigger_dimension"] == "session.max_total_tokens"
    assert data["consumed"]["total_tokens"] is None
    assert data["accounting_unknown"] == ["session.max_total_tokens"]
    # 机制锚：暂停的根源是 durable 行被第一轮无 usage 请求写成 NULL 粘住
    # （存储层与事件派生同一未知语义）——不是行读数 0 被 fail-closed 误判。
    row = _session_row(app, session_id)
    assert row.consumed.total_tokens is None


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


# ── #564 裁决 (a)+(b)：注册名校验前置 + 行陈旧名降告警 ──────────────────────


def test_resume_bad_session_tool_name_leaves_row_untouched(tmp_path):
    """(a)：resume 点名坏名 ⇒ 422，且**账行零改动**（eager CAS 之前就拒绝）。

    回归锚：修复前 eager CAS/ensure（merge-only）先并入 `nope_tool` 再 422——
    一次打错的请求把账行毒化（后续普通 resume 恒 422，会话不可恢复）。
    """
    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ModelProbe()
    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "task": "继续",
                "budget": {"session": {"tool_call_limits": {"nope_tool": 1}}},
            },
        )
    assert resp.status_code == 422, resp.text
    assert probe.calls == [], "校验前置 ⇒ 422 时模型不得被构造"
    assert _session_row(app, session_id) is None, "坏名不得触碰账行（建行也算触碰）"


def test_plain_messages_after_rejected_budget_resume_works(tmp_path):
    """(a) 主害回归：坏名 resume 被拒后，**不带 budget 的普通续聊必须照常 200**。"""
    _app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    with _ModelProbe():
        bad = client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "task": "继续",
                "budget": {"session": {"tool_call_limits": {"nope_tool": 1}}},
            },
        )
    assert bad.status_code == 422, bad.text

    probe = _ScriptedProbe([_done()])
    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/messages", json={"content": "继续"},
        )
    assert resp.status_code == 200, resp.text
    assert "run/completed" in resp.text, resp.text[:400]


def test_resume_bad_name_with_prior_row_leaves_row_intact(tmp_path):
    """(a)：既有账行 + 坏名 resume ⇒ 422 且行内容与 CAS 版本都原样。"""
    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([_done()])
    with probe:
        first = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": "开始",
                "budget": {"session": {"tool_call_limits": {"read": 2}}},
            },
        )
    assert first.status_code == 200, first.text
    row = _session_row(app, session_id)
    assert row is not None and row.limits.tool_call_limits == {"read": 2}
    version = row.version

    with _ModelProbe():
        bad = client.post(
            f"/api/sessions/{session_id}/messages",
            json={
                "content": "继续",
                "budget": {
                    "session": {
                        "tool_call_limits": {"nope_tool": 1},
                        "expected_version": version,
                    },
                },
            },
        )
    assert bad.status_code == 422, bad.text
    after = _session_row(app, session_id)
    assert after.limits.tool_call_limits == {"read": 2}, "坏名不得并入既有账行"
    assert after.version == version, "被拒请求不得推进账行 CAS 版本"


def test_stale_row_name_warns_and_session_still_resumes(tmp_path, caplog):
    """(b)：账行陈旧名（能力下线 / 工件 store 切换所致的历史合法名）⇒ **告警不拒绝**。

    回归锚：修复前 build_runtime 对账行现值按收窄 registry 重核 422——行里一个
    事后悬空的名字永久卡死会话。422 只属于**请求声明**（另测）；行值降为可观测告警。
    """
    import logging

    from agent_harness.agent.run_budget import SessionLimits

    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    ledger = app.state.agent.stores.delegation_tree_ledger
    asyncio.run(
        ledger.ensure_session_budget(
            session_id, root_session_id=session_id,
            limits=SessionLimits(tool_call_limits={"legacy_tool": 1}),
        )
    )

    with caplog.at_level(logging.WARNING, logger="agent_harness.assembly"):
        probe = _ScriptedProbe([_done()])
        with probe:
            resp = client.post(
                f"/api/sessions/{session_id}/messages", json={"content": "继续"},
            )
    assert resp.status_code == 200, resp.text
    assert "run/completed" in resp.text, resp.text[:400]
    assert any(
        "legacy_tool" in record.getMessage() for record in caplog.records
    ), "陈旧名必须响亮告警（不是静默忽略）"


def test_bad_name_resume_does_not_recreate_deleted_workspace(tmp_path):
    """P2-1（独立审查）：422 拒绝路径**零文件系统副作用**——已删的外部 cwd 不得被重建。

    修复前 validator 走完整 `_build_tooling` ⇒ `LocalSubprocessSandbox.__init__`
    对 workspace `mkdir`，发生在 #266 归属对账之前：坏名 422 会把用户删掉的
    cwd 凭空建回来。只有重启形态（全新 AppState，进程内 sandbox 缓存为空）才暴露。
    """
    _app, client = _web(tmp_path)
    external = tmp_path / "user-repo"
    external.mkdir()
    resp = client.post(
        "/api/sessions", json={"cwd": str(external)}, params={"launch": "false"},
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    shutil.rmtree(external)

    # 重启形态：同一 tmp_path（会话行在磁盘上），全新 AppState ⇒ registry 空。
    _app2, client2 = _web(tmp_path)
    with _ModelProbe():
        bad = client2.post(
            f"/api/sessions/{session_id}/resume",
            json={"task": "继续",
                  "budget": {"session": {"tool_call_limits": {"nope_tool": 1}}}},
        )
    assert bad.status_code == 422, bad.text
    assert not external.exists(), "被拒请求不得重建已删除的 cwd（422 零副作用）"


def test_valid_name_resume_still_404_when_workspace_deleted(tmp_path):
    """P2-1（独立审查）：#266 守卫不被"点名 session 账"的请求掩蔽——cwd 已删 ⇒ 404。

    修复前同名 validator 先实例化 Sandbox 并 mkdir ⇒ `workspace.is_dir()` 被
    抢先满足，会话在静默重建的空目录里继续跑（本该 404）。
    """
    _app, client = _web(tmp_path)
    external = tmp_path / "user-repo-2"
    external.mkdir()
    resp = client.post(
        "/api/sessions", json={"cwd": str(external)}, params={"launch": "false"},
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    shutil.rmtree(external)

    _app2, client2 = _web(tmp_path)
    with _ModelProbe():
        ok = client2.post(
            f"/api/sessions/{session_id}/resume",
            json={"task": "继续",
                  "budget": {"session": {"tool_call_limits": {"read": 2}}}},
        )
    assert ok.status_code == 404, ok.text
    assert not external.exists(), "守卫路径不得静默重建 cwd"
