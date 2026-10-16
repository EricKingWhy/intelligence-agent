"""#1190 HTTP 面：`GET .../protected-facts` 与 `POST .../protected-facts/{id}/revoke`（黑盒）。

- GET：返回全量投影（active/revoked/superseded），字段齐，按 `source_seq` 倒序。
- POST：同步撤销 → 200 `{fact_id, status: "revoked", revoked_at}`；追加
  `task/fact_revocation`（不物理删除）；幂等；目标非 active → 409；不存在 → 404。
- 来源闸（ADR-0025 D1）：跨源 → 403。

黑盒：只经 HTTP + 直接读 `/events`。会话经 `Session` 直接播种到 app 的 store
（与 `test_constraint_resolution_api.py` 同款），避免为了造事实去跑一整个 run。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.session import TASK_FACT_REVOCATION, USER_MESSAGE
from agent_harness.session.service import SessionService
from agent_harness.session.session import Session
from agent_harness.web.app import session_service
from tests.web.test_budget_local_fuse_api import _web


def _seed(tmp_path: Path) -> tuple[TestClient, str, str]:
    """播种一个有两个约束事实的会话，返回 (client, session_id, 第一个 fact_id)。"""
    app, client = _web(tmp_path)
    store = app.state.agent.store
    sid = "s-1190"
    session = Session.start(store, session_id=sid)
    e1 = session.append(USER_MESSAGE, {"content": "回复代码审查意见时先给结论，别长篇铺垫。"})
    first = session.register_protected_fact(
        fact_type="constraint",
        value="回复代码审查意见时先给结论，别长篇铺垫。",
        source_event_id=e1.event_id,
    )
    e2 = session.append(USER_MESSAGE, {"content": "秋招每天投 20 份简历。"})
    session.register_protected_fact(
        fact_type="user_goal",
        value="秋招每天投 20 份简历。",
        source_event_id=e2.event_id,
    )
    return client, sid, first.data["fact_id"]


def _event_types(client: TestClient, session_id: str) -> list[str]:
    resp = client.get(f"/api/sessions/{session_id}/events")
    assert resp.status_code == 200, resp.text
    return [e["type"] for e in resp.json()]


class TestListProtectedFacts:
    def test_lists_all_facts_with_fields_sorted_desc(self, tmp_path: Path):
        client, sid, first_id = _seed(tmp_path)
        resp = client.get(f"/api/sessions/{sid}/protected-facts")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"facts"}
        facts = body["facts"]
        assert len(facts) >= 2
        required = {
            "fact_id", "type", "value", "status", "source_event_id",
            "source_seq", "registered_at", "revoked_at", "revoked_by",
        }
        for fact in facts:
            assert set(fact) == required, fact
            assert fact["status"] in {"active", "revoked", "superseded"}
            assert fact["registered_at"]  # 来源事件时间非空
        seqs = [fact["source_seq"] for fact in facts]
        assert seqs == sorted(seqs, reverse=True)
        assert any(fact["fact_id"] == first_id for fact in facts)

    def test_unknown_session_404(self, tmp_path: Path):
        _, client = _web(tmp_path)
        resp = client.get("/api/sessions/does-not-exist-123/protected-facts")
        assert resp.status_code == 404, resp.text

    def test_bad_session_id_422(self, tmp_path: Path):
        _, client = _web(tmp_path)
        resp = client.get("/api/sessions/bad id!/protected-facts")
        assert resp.status_code == 422, resp.text

    def test_cross_origin_403(self, tmp_path: Path):
        client, sid, _ = _seed(tmp_path)
        resp = client.get(
            f"/api/sessions/{sid}/protected-facts",
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403, resp.text


class TestRevokeProtectedFact:
    def test_revoke_returns_receipt_and_appends_event(self, tmp_path: Path):
        client, sid, fact_id = _seed(tmp_path)
        resp = client.post(f"/api/sessions/{sid}/protected-facts/{fact_id}/revoke")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["fact_id"] == fact_id
        assert body["status"] == "revoked"
        assert body["revoked_at"]
        assert TASK_FACT_REVOCATION in _event_types(client, sid)

        # GET 反映撤销（逐条对账）
        facts = client.get(f"/api/sessions/{sid}/protected-facts").json()["facts"]
        revoked = next(f for f in facts if f["fact_id"] == fact_id)
        assert revoked["status"] == "revoked"
        assert revoked["revoked_at"] == body["revoked_at"]
        assert revoked["revoked_by"] == "local"

    def test_revoke_is_idempotent(self, tmp_path: Path):
        client, sid, fact_id = _seed(tmp_path)
        first = client.post(f"/api/sessions/{sid}/protected-facts/{fact_id}/revoke")
        assert first.status_code == 200
        before = _event_types(client, sid).count(TASK_FACT_REVOCATION)
        second = client.post(f"/api/sessions/{sid}/protected-facts/{fact_id}/revoke")
        assert second.status_code == 200
        assert second.json()["status"] == "revoked"
        after = _event_types(client, sid).count(TASK_FACT_REVOCATION)
        assert after == before  # 不重复落事件

    def test_revoke_unknown_fact_404(self, tmp_path: Path):
        client, sid, _ = _seed(tmp_path)
        resp = client.post(f"/api/sessions/{sid}/protected-facts/pf-nope/revoke")
        assert resp.status_code == 404, resp.text

    def test_revoke_superseded_fact_409(self, tmp_path: Path):
        app, client = _web(tmp_path)
        sid = "s-sup"
        session = Session.start(app.state.agent.store, session_id=sid)
        e1 = session.append(USER_MESSAGE, {"content": "用 Python 写。"})
        old = session.register_protected_fact(
            fact_type="constraint", value="用 Python 写。", source_event_id=e1.event_id,
        ).data["fact_id"]
        e2 = session.append(USER_MESSAGE, {"content": "改用 TypeScript 写。"})
        session.register_protected_fact(
            fact_type="constraint",
            value="改用 TypeScript 写。",
            source_event_id=e2.event_id,
            supersedes_fact_id=old,
        )
        resp = client.post(f"/api/sessions/{sid}/protected-facts/{old}/revoke")
        assert resp.status_code == 409, resp.text

    def test_revoke_bad_fact_id_422(self, tmp_path: Path):
        client, sid, _ = _seed(tmp_path)
        resp = client.post(f"/api/sessions/{sid}/protected-facts/bad%20id!/revoke")
        assert resp.status_code == 422, resp.text

    def test_unknown_session_404(self, tmp_path: Path):
        _, client = _web(tmp_path)
        resp = client.post("/api/sessions/does-not-exist-123/protected-facts/pf-x/revoke")
        assert resp.status_code == 404, resp.text

    def test_cross_origin_403(self, tmp_path: Path):
        client, sid, fact_id = _seed(tmp_path)
        resp = client.post(
            f"/api/sessions/{sid}/protected-facts/{fact_id}/revoke",
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403, resp.text

    @pytest.mark.asyncio
    async def test_concurrent_revoke_appends_single_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """并发撤销同一 fact → 只落一条 `task/fact_revocation`（读-判-写原子）。

        两个 `SessionService` 实例（模拟 Web 层每请求各建一个）**共享同一 RunManager**，
        故会话锁把它们串行化：第二个请求在锁内重读到 `revoked` 走幂等分支，不重复落事件。

        ⚠ 必须给读阶段**注入一个 await 让点**：真实的 `get_events` 在本环境不产生
        挂起点，`asyncio.gather` 的两个协程会一前一后跑完（race 窗口不出现）——那样的
        断言恒真、测不到锁。这里把 `get_events` 包一层 `sleep(0)`，让两个请求都先读到
        `active` 再各自写：**去掉锁这条会红**（已验证）。
        """
        app, _client = _web(tmp_path)
        state = app.state.agent
        sid = "s-race"
        session = Session.start(state.store, session_id=sid)
        e1 = session.append(USER_MESSAGE, {"content": "回复时先给结论。"})
        fact_id = session.register_protected_fact(
            fact_type="constraint", value="回复时先给结论。", source_event_id=e1.event_id,
        ).data["fact_id"]

        real_get_events = SessionService.get_events

        async def _slow_get_events(self: SessionService, session_id: str, *args: object, **kwargs: object):
            await asyncio.sleep(0)  # 撑开读-判-写窗口
            return await real_get_events(self, session_id, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(SessionService, "get_events", _slow_get_events)

        results = await asyncio.gather(
            session_service(state).revoke_protected_fact(sid, fact_id, revoked_by="local"),
            session_service(state).revoke_protected_fact(sid, fact_id, revoked_by="local"),
        )
        assert all(r["status"] == "revoked" for r in results)
        events = await real_get_events(session_service(state), sid)
        assert sum(1 for e in events if e.type == TASK_FACT_REVOCATION) == 1
