"""`#552` HTTP / wire 边界：整数预算 ceiling 与 provider usage 的 int64 值域。

BUG-R4-02 / BUG-R4-03 同根：外部 / 请求侧数值未经「与存储层能力（SQLite INTEGER）
对齐」的校验就直进账本。本文件的验收面（`recon552` §C）：

- **ceiling**：`2**63-1` 合法（锚）；`2**63` / `10**30` → **422 + 零副作用**
  （不是 500、不是 OverflowError）；两条入口（`POST /api/sessions?launch=false` 与
  `POST /api/sessions/{id}/messages`）都要；`/messages` 拒绝时**不得先写 budget**；
- **run/session 同口径**：`budget.run.max_total_tokens=10**30` 也必须 422（当前 200）；
- **usage 越界**：模型自报 `total_tokens=10**30` ⇒ 该维转 **unknown**（不 clamp、
  不记 0）；`run/failed.reason` 不是 `"OverflowError"`；run 级 `usage_total` 与
  `GET /budget.session.consumed.total_tokens` 账目一致；
- **计数一致**：越界 usage 下 run 级 `model/request` 条数 == session
  `consumed.model_requests`。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from agent_harness.web.app import RunBudgetRequest, SessionBudgetRequest
from tests.web.test_budget_local_fuse_api import _create_idle_session, _web
from tests.web.test_run_pause_resume_api import (
    _events,
    _one,
    _ScriptedProbe,
)

INT64_MAX = 2**63 - 1
OVERSIZE = 10**30


def _session_row(app, session_id: str):  # type: ignore[no-untyped-def]
    """从 web 装配里拿 durable 账行（服务已 `_ensure_stores`，表必在）。"""
    ledger = app.state.agent.stores.delegation_tree_ledger
    return asyncio.run(ledger.get_session_budget(session_id))


def _budget(client: TestClient, session_id: str) -> dict:
    resp = client.get(f"/api/sessions/{session_id}/budget")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── wire 层值域（F2/F4，C16）────────────────────────────────────────────


@pytest.mark.parametrize("model", [RunBudgetRequest, SessionBudgetRequest])
def test_wire_rejects_above_int64_ceiling(model):
    """超过 `2**63-1` 的 ceiling 在解析期即拒（422 出口，不落给 SQLite 绑定）。"""
    for bad in (2**63, OVERSIZE):
        with pytest.raises(ValidationError):
            model.model_validate({"max_total_tokens": bad})
    assert model.model_validate({"max_total_tokens": INT64_MAX}).max_total_tokens == INT64_MAX


@pytest.mark.parametrize("model", [RunBudgetRequest, SessionBudgetRequest])
@pytest.mark.parametrize("bad", [True, "100", 1.5])
def test_wire_rejects_non_integer_ceiling_types(model, bad):
    """F4：`bool` / 字符串数字 / 浮点都非法——不得被 lax 强转成整数。"""
    with pytest.raises(ValidationError):
        model.model_validate({"max_total_tokens": bad})


def test_wire_rejects_above_int64_max_delegations():
    """F3：`max_delegations` 与其余整数 ceiling 同一上界。"""
    with pytest.raises(ValidationError):
        SessionBudgetRequest.model_validate({"max_delegations": OVERSIZE})


# ── ceiling：两条入口 422 + 零副作用（C12/C13/C14）─────────────────────


def test_messages_rejects_oversize_session_ceiling_422_no_side_effects(tmp_path):
    """C12+C13：`/messages` 带 10**30 session ceiling ⇒ 422，且**不先写 budget**。

    拒绝时事件流逐条与请求前相同（无 user/message、无 session/resumed），
    账行也不得存在（`ledger.get_session_budget(sid) is None`）。
    """
    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    before = _events(client, session_id)
    probe = _ScriptedProbe()

    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "hi", "budget": {"session": {"max_total_tokens": OVERSIZE}}},
        )

    assert resp.status_code == 422, resp.text
    assert probe.calls == [], "被拒请求不得构造模型"
    assert _events(client, session_id) == before, "被拒请求不得改动会话历史"
    assert _session_row(app, session_id) is None, "拒绝不得先写 budget"


def test_create_session_rejects_oversize_session_ceiling_422_no_orphan(tmp_path):
    """C14：`POST /api/sessions?launch=false` 带 10**30 ⇒ 422，且不新增会话（无孤儿）。"""
    _, client = _web(tmp_path)
    before = client.get("/api/sessions").json()

    resp = client.post(
        "/api/sessions", params={"launch": "false"},
        json={"budget": {"session": {"max_total_tokens": OVERSIZE}}},
    )

    assert resp.status_code == 422, resp.text
    assert client.get("/api/sessions").json() == before, "被拒请求不得留下孤儿会话"


def test_messages_rejects_oversize_run_ceiling_422(tmp_path):
    """C17（F3）：run 作用域与 session 同口径——`budget.run.max_total_tokens=10**30` 也 422。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe()

    with probe:
        resp = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "hi", "budget": {"run": {"max_total_tokens": OVERSIZE}}},
        )

    assert resp.status_code == 422, resp.text
    assert probe.calls == []


# ── ceiling：`2**63-1` 合法（锚，C15）─────────────────────────────────


def test_create_session_accepts_int64_max_session_ceiling(tmp_path):
    """C15：`2**63-1` 恰好是存储上限，必须接受并原样读回。"""
    app, client = _web(tmp_path)
    resp = client.post(
        "/api/sessions", params={"launch": "false"},
        json={"budget": {"session": {"max_total_tokens": INT64_MAX}}},
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    row = _session_row(app, session_id)
    assert row.limits.max_total_tokens == INT64_MAX


# ── usage 越界：两本账一致 + 计数一致（C9/C10/C11）─────────────────────


def test_oversize_provider_usage_keeps_two_ledgers_consistent(tmp_path):
    """C9+C10：provider 自报 10**30 ⇒ 该维转未知，两本账一致，run 不因裸 OverflowError 炸。

    改前 RED：run 级 `usage_total.total_tokens=10**30` vs session 级 `0`（永久分裂），
    且 `run/failed.reason == "OverflowError"`。
    """
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([AIMessage(
        content="完成",
        response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": OVERSIZE},
    )])

    with probe:
        resp = client.post(f"/api/sessions/{session_id}/messages", json={"content": "hi"})
        assert resp.status_code == 200, resp.text

    events = _events(client, session_id)
    types = [e["type"] for e in events]
    assert "run/failed" not in types, f"越界 usage 不得让 run 失败：{types}"
    terminal = _one(events, "run/completed")
    assert terminal["data"].get("reason") != "OverflowError"
    usage_total = terminal["data"].get("usage_total") or {}
    assert "total_tokens" not in usage_total, f"run 级不得出现越界 total_tokens：{usage_total}"
    assert OVERSIZE not in usage_total.values()

    # session 级：该维未知（None），既不是 10**30 也不是被编造的 0——两本账同为未知
    consumed = _budget(client, session_id)["session"]["consumed"]
    assert consumed["total_tokens"] is None


def test_model_request_counts_agree_under_oversize_usage(tmp_path):
    """C11：越界 usage 下，run 级 `model/request` 条数 == session `consumed.model_requests`。"""
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([AIMessage(
        content="完成",
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": OVERSIZE},
    )])

    with probe:
        resp = client.post(f"/api/sessions/{session_id}/messages", json={"content": "hi"})
        assert resp.status_code == 200, resp.text

    run_requests = sum(1 for e in _events(client, session_id) if e["type"] == "model/request")
    consumed = _budget(client, session_id)["session"]["consumed"]
    assert run_requests == 1
    assert consumed["model_requests"] == run_requests


# ── WS 第四入口：同一值域必须同判（不能绕过）───────────────────────────


def test_ws_rejects_oversize_session_ceiling_without_budget_write(tmp_path):
    """WS `send_message` 帧带 10**30 session ceiling ⇒ 错误帧，且不产生账行。"""
    import json

    app, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    before = _events(client, session_id)

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({
            "type": "send_message",
            "session_id": session_id,
            "content": "继续",
            "budget": {"session": {"max_total_tokens": OVERSIZE}},
        }))
        payload = json.loads(ws.receive_text())

    assert payload["type"] == "error", payload
    assert _events(client, session_id) == before
    assert _session_row(app, session_id) is None
