"""#684 Phase 2 Web 面：`GET /api/approve-policy/rules` 与
`POST /api/approve-policy/rules/{id}/revoke`（黑盒 HTTP）。

钉住的契约（对标 #526 端点形态）：

- GET 返回本项目规则列表（`{"rules": [...]}`）；无规则 / 文件缺失 / 文件损坏都是
  `{"rules": []}`——空列表不是 404；
- POST revoke **幂等**：命中 → `revoked=True`；不存在 → 200 且 `revoked=False`；
- 422（`rule_id` 形态非法）先于任何读写；
- 来源闸 `require_trusted_origin`：跨源 GET/POST 一律 403（本地信任模式下）。

项目根 = 进程当前工作目录（与 `ToolExecutor` 回落口径一致），测试用
`monkeypatch.chdir(tmp_path)` 把它钉到临时目录；规则直接经 `ApprovePolicyStore`
预置，断言只读回文件与 HTTP 状态，不 import web 内部符号。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.tooling.approve_policy import (
    ApprovePolicyRule,
    ApprovePolicyStore,
    PolicyGranularity,
)
from agent_harness.tooling.contract import ToolPermission
from agent_harness.web.app import create_app

_RULES_PATH = "/api/approve-policy/rules"


def _rule(rule_id: str = "r1", *, key: str = "npm run build") -> ApprovePolicyRule:
    return ApprovePolicyRule(
        id=rule_id,
        tool="bash",
        key=key,
        granularity=PolicyGranularity.COMMAND,
        permission_at_approval=ToolPermission.DANGER,
        created_at="2026-10-05T12:00:00+00:00",
    )


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return TestClient(create_app(settings, enable_cors=False))


def _ids(tmp_path: Path) -> list[str]:
    return [rule.id for rule in ApprovePolicyStore(tmp_path).load()]


class TestListRules:
    def test_empty_returns_empty_list(self, tmp_path: Path, monkeypatch) -> None:
        client = _client(tmp_path, monkeypatch)
        resp = client.get(_RULES_PATH)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"rules": []}

    def test_returns_rules_with_all_fields(self, tmp_path: Path, monkeypatch) -> None:
        ApprovePolicyStore(tmp_path).save([_rule()])
        client = _client(tmp_path, monkeypatch)
        resp = client.get(_RULES_PATH)
        assert resp.status_code == 200, resp.text
        rules = resp.json()["rules"]
        assert len(rules) == 1
        assert rules[0] == {
            "id": "r1",
            "tool": "bash",
            "key": "npm run build",
            "granularity": "command",
            "permission_at_approval": "danger",
            "created_at": "2026-10-05T12:00:00+00:00",
        }

    def test_corrupt_file_fails_closed_to_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        target = tmp_path / ".agent-harness" / "approve-policy.json"
        target.parent.mkdir(parents=True)
        target.write_text("{ not json", encoding="utf-8")
        client = _client(tmp_path, monkeypatch)
        resp = client.get(_RULES_PATH)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"rules": []}

    def test_cross_origin_rejected(self, tmp_path: Path, monkeypatch) -> None:
        client = _client(tmp_path, monkeypatch)
        resp = client.get(_RULES_PATH, headers={"Origin": "http://evil.example"})
        assert resp.status_code == 403, resp.text


class TestRevokeRule:
    def test_revoke_existing_removes_and_returns_true(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        ApprovePolicyStore(tmp_path).save([_rule("r1"), _rule("r2")])
        client = _client(tmp_path, monkeypatch)

        resp = client.post(f"{_RULES_PATH}/r1/revoke")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"id": "r1", "revoked": True}
        assert _ids(tmp_path) == ["r2"]

    def test_revoke_missing_is_idempotent(self, tmp_path: Path, monkeypatch) -> None:
        client = _client(tmp_path, monkeypatch)
        resp = client.post(f"{_RULES_PATH}/does-not-exist/revoke")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"id": "does-not-exist", "revoked": False}

    def test_repeat_revoke_is_idempotent(self, tmp_path: Path, monkeypatch) -> None:
        ApprovePolicyStore(tmp_path).save([_rule("r1")])
        client = _client(tmp_path, monkeypatch)

        first = client.post(f"{_RULES_PATH}/r1/revoke")
        assert first.status_code == 200
        assert first.json()["revoked"] is True

        second = client.post(f"{_RULES_PATH}/r1/revoke")
        assert second.status_code == 200
        assert second.json() == {"id": "r1", "revoked": False}
        assert _ids(tmp_path) == []

    def test_malformed_id_422(self, tmp_path: Path, monkeypatch) -> None:
        client = _client(tmp_path, monkeypatch)
        resp = client.post(f"{_RULES_PATH}/bad.id/revoke")
        assert resp.status_code == 422, resp.text
        assert _ids(tmp_path) == []

    def test_cross_origin_rejected(self, tmp_path: Path, monkeypatch) -> None:
        ApprovePolicyStore(tmp_path).save([_rule("r1")])
        client = _client(tmp_path, monkeypatch)
        resp = client.post(
            f"{_RULES_PATH}/r1/revoke", headers={"Origin": "http://evil.example"}
        )
        assert resp.status_code == 403, resp.text
        assert _ids(tmp_path) == ["r1"], "跨源拒绝 ⇒ 规则一个字节都不许改"

    def test_list_reflects_revocation(self, tmp_path: Path, monkeypatch) -> None:
        ApprovePolicyStore(tmp_path).save([_rule("r1")])
        client = _client(tmp_path, monkeypatch)
        assert client.get(_RULES_PATH).json()["rules"][0]["id"] == "r1"
        client.post(f"{_RULES_PATH}/r1/revoke")
        assert client.get(_RULES_PATH).json() == {"rules": []}


@pytest.mark.parametrize(
    "path", [_RULES_PATH, f"{_RULES_PATH}/{{rule_id}}/revoke"]
)
def test_openapi_declares_endpoints(tmp_path: Path, monkeypatch, path: str) -> None:
    # 端点真的注册进了 schema（防"路由写错前缀"这类静默缺席）。
    client = _client(tmp_path, monkeypatch)
    schema = client.get("/openapi.json").json()
    assert path in schema["paths"]


# ── #684 P1-1：POST /approve 把 policy_granularity 透传进 ApprovalResponse ─────


class _RecordingQueue:
    """最小 live-resolver 替身：只实现 resolve_approval 用到的那一个方法。

    真实 `PendingApprovalQueue` 的 future 依赖运行中的事件循环，而 TestClient 的
    请求跑在自己的 portal 线程里——本测试只关心 HTTP→领域层的粒度透传，故用替身
    记录 `ApprovalResponse`（队列本身的语义另有 `#684` tooling/session 测试覆盖）。
    """

    def __init__(self) -> None:
        self.resolved: dict[str, object] = {}

    def resolve(self, approval_id: str, response: object) -> bool:
        self.resolved[approval_id] = response
        return True


def _seed_pending_policy_approval(client: TestClient):
    """在 app 的 store 里建 session + live resolver + 一条允许 approve_policy 的请求。

    返回 `(session_id, approval_id, queue)`——`queue.resolved` 是断言领域层真的收到
    粒度的唯一入口（HTTP 响应体不回声粒度）。
    """
    from agent_harness.session import Session
    from agent_harness.session.event import TOOL_APPROVAL_REQUESTED

    app_state = client.app.state.agent
    session = Session.start(app_state.store)
    queue = _RecordingQueue()
    approval_id = "ap-684"
    session.append(
        TOOL_APPROVAL_REQUESTED,
        {
            "approval_id": approval_id,
            "tool_name": "bash",
            "allowed_decisions": ["approve_once", "deny", "approve_policy"],
        },
    )
    app_state.approval_queues[session.session_id] = queue
    return session.session_id, approval_id, queue


def test_approve_policy_granularity_reaches_domain(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    sid, approval_id, queue = _seed_pending_policy_approval(client)

    resp = client.post(
        f"/api/sessions/{sid}/approve",
        json={
            "approval_id": approval_id,
            "decision": "approve_policy",
            "policy_granularity": "command",
        },
    )
    assert resp.status_code == 200, resp.text
    resolved = queue.resolved.get(approval_id)
    assert resolved is not None
    assert resolved.policy_granularity is PolicyGranularity.COMMAND


def test_approve_policy_invalid_granularity_422(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    sid, approval_id, _queue = _seed_pending_policy_approval(client)

    resp = client.post(
        f"/api/sessions/{sid}/approve",
        json={
            "approval_id": approval_id,
            "decision": "approve_policy",
            "policy_granularity": "fuzzy",
        },
    )
    assert resp.status_code == 422, resp.text
