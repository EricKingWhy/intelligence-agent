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


# ── usage 维缺席（C6）：run 级与 session 级必须**同判** ─────────────────


def test_dim_missing_in_later_response_keeps_two_ledgers_consistent(tmp_path):
    """C6：第 2 轮的 `total_tokens` **不可采信（被丢弃）** ⇒ 两本账值级一致（都未知）。

    形状：多步 run（第 1 轮 tool_call）+ 第 2 轮 `total_tokens` 越界被丢。
    `AIMessage.usage_metadata` 三字段在 langchain 层必填，所以"某维缺席"在模型侧
    的真实入口是**值不可采信**（越界 / 负数 ⇒ `_usage_from_response` 省略该维，
    与既有 `test_negative_usage_values_are_dropped_not_aggregated` 同形）。
    session 侧早就是「该维缺席 ⇒ `NULL`」；改前 run 级 `usage_total.total_tokens`
    停在第 1 轮的旧值 110（少算的假精确数）——两本账值级分裂。改后两处同为未知。
    """
    _, client = _web(tmp_path)
    session_id = _create_idle_session(client)
    probe = _ScriptedProbe([
        # 第 1 轮**必须带正文**：`ScriptedModel.astream` 在纯 tool_call 轮（content 空）
        # 不附带 usage_metadata ⇒ 那一轮的 usage 整个缺席，本用例就退化成"从未跟踪
        # 过 total_tokens"，测不到"缺席 ⇒ 转未知"。真实 provider 的 content 与
        # tool_calls 也常并存。
        AIMessage(
            content="先看一眼文件。",
            response_metadata={"model_name": "qwen-plus-0911"},
            usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            tool_calls=[{
                "id": "tc-c6-1", "name": "read",
                "args": {"path": "does-not-exist.txt"}, "type": "tool_call",
            }],
        ),
        AIMessage(
            content="完成",
            usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": OVERSIZE},
        ),
    ])

    with probe:
        resp = client.post(f"/api/sessions/{session_id}/messages", json={"content": "hi"})
        assert resp.status_code == 200, resp.text

    usage_total = _one(_events(client, session_id), "run/completed")["data"]["usage_total"]
    assert usage_total["total_tokens"] is None, (
        f"run 级该维必须转未知（改前停在第 1 轮旧值 110）：{usage_total}"
    )
    assert usage_total["prompt_tokens"] == 107       # 100 + 7：本轮可采信的维照常累加
    assert usage_total["completion_tokens"] == 13    # 10 + 3

    consumed = _budget(client, session_id)["session"]["consumed"]
    assert consumed["total_tokens"] is None, "session 账同一步已转 NULL——两本账必须同判"


# ── C3（用户 2026-10-03 裁决）：`strict` 语义不变，只把拒绝文案说清 ──────────


#: 各模型的整数 ceiling 字段全集（`_ceiling_must_be_integer` 的挂载面）。
#: 漂移防护按**字段维**参数化：任何字段被从校验器挂载列表里挪走 ⇒ 此处红。
_INT_CEILING_FIELDS: dict[type, tuple[str, ...]] = {
    RunBudgetRequest: ("max_agent_turns_total", "max_model_requests", "max_total_tokens"),
    SessionBudgetRequest: (
        "max_agent_turns_total",
        "max_model_requests",
        "max_total_tokens",
        "max_delegations",
    ),
}


def _int_ceiling_cases() -> list:
    cases = []
    for model, fields in _INT_CEILING_FIELDS.items():
        for field in fields:
            for bad in (100.0, 1.5, "100", True):
                cases.append(
                    pytest.param(
                        model, field, bad, id=f"{model.__name__}-{field}-{bad!r}"
                    )
                )
    return cases


@pytest.mark.parametrize(("model", "field", "bad"), _int_ceiling_cases())
def test_non_integer_ceiling_error_message_is_actionable(model, field, bad):
    """F4/C3：**全部 7 个** ceiling 字段 × 四类非法输入都说清「要求整数」。

    判据三段：
    ① ``type`` 仍是 ``int_type`` —— wire 契约的机器可读位逐字不变（本票只改 ``msg``）；
    ② ``msg`` 明说要整数（改前是英文 ``Input should be a valid integer``）；
    ③ ``msg`` 不带 pydantic 的 ``"Value error, "`` 前缀（否则文案被污染）。

    字段维全覆盖（修后重审 P2）：只钉 ``max_total_tokens`` 一个字段的版本测不到
    「某个字段被从校验器挂载列表里挪走」的漂移——那会让该字段退回 lax 强转或
    英文默认文案，而其余用例全绿。
    """
    with pytest.raises(ValidationError) as exc:
        model.model_validate({field: bad})
    (err,) = exc.value.errors()
    assert err["type"] == "int_type", err
    assert "整数" in err["msg"], err
    assert not err["msg"].startswith("Value error"), err


def test_non_integer_ceiling_422_body_carries_actionable_message(tmp_path):
    """端到端：`POST /api/sessions` 传浮点 ceiling ⇒ 422 且响应体里带「整数」文案。

    `strict=True` 从未放松（浮点依旧被拒），变的只是**这句话说不说得清**。
    """
    _, client = _web(tmp_path)
    resp = client.post(
        "/api/sessions",
        params={"launch": "false"},
        json={"budget": {"session": {"max_total_tokens": 100.0}}},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list), detail
    assert any("整数" in item.get("msg", "") for item in detail), detail
