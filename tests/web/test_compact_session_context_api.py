"""#635 T5 HTTP 面：`POST /api/sessions/{id}/context/compact`（黑盒）。

手动触发一次上下文压缩：复用 `SessionService.compact_session_context`（唯一实现），
端点只做来源闸 + 状态码映射 + DTO 透传。

**本文件钉的外部契约（Task B）**：

- 路径 `POST /api/sessions/{id}/context/compact`；可选 query `?model=<name>`；
- 成功 200 `{bracket_id, source_seq_start, source_seq_end, tokens_before,
  tokens_after, compacted_turn_count, summary_model}`（低水位时 `bracket_id=null`、
  `compacted_turn_count=0`，仍 200 —— 零写入）；
- 在途 run / 压缩进行中 → 409 `ActiveRunConflict`（零事件写入）；
- 无会话 → 404；id 非法 → 422（先于 404，路径穿越防线）；
- 非法 `?model=` → 422（detail 原样上抛，零副作用前校验）；
- 来源闸 `require_trusted_origin`（与 archive / purge-stale-tools 同实现）。

黑盒：只经 HTTP + 直接读 `app.state.agent.store` 的事件流，不 import 服务层内部
符号（除用于模拟并发点击的 in-flight 标识，见对应用例注释）。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.session import MODEL_COMPLETED, USER_MESSAGE, Session
from agent_harness.session.runmanager import RunManager
from agent_harness.web.app import create_app, session_service
from tests.scripted_model import ScriptedModel

MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


def _client(tmp_path: Path, **overrides) -> TestClient:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test", **overrides
    )
    return TestClient(create_app(settings, enable_cors=False))


def _seed_history(client: TestClient) -> str:
    """造一个"有可压缩早期轮"的会话：user → model(大) → user(current)。"""
    store = client.app.state.agent.store
    session = Session.start(store)
    session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
    session.append(MODEL_COMPLETED, {"content": "历史分析 " * 800})
    session.append(USER_MESSAGE, {"content": "current request"})
    return session.session_id


def _events(client: TestClient, session_id: str) -> list[dict]:
    resp = client.get(f"/api/sessions/{session_id}/events")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _post(client: TestClient, session_id: str, **params: str):
    return client.post(
        f"/api/sessions/{session_id}/context/compact", params=params or None
    )


def _summary_model() -> ScriptedModel:
    model = ScriptedModel(responses=[AIMessage(content=MODEL_SECTIONS)])
    model.model_name = "main-model"
    return model


# ── 成功：200 + DTO + bracket 落盘 ──────────────────────────────────────


def test_compact_success_returns_dto_and_writes_bracket(tmp_path):
    client = _client(tmp_path)
    sid = _seed_history(client)

    with patch(
        "agent_harness.model.provider.create_chat_model",
        return_value=_summary_model(),
    ):
        resp = _post(client, sid)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bracket_id"]
    assert body["source_seq_start"] == 1
    assert body["source_seq_end"] == 2
    assert body["tokens_before"] > body["tokens_after"]
    assert body["compacted_turn_count"] == 1
    assert body["summary_model"] == "main-model"

    compacted = [
        e for e in _events(client, sid) if e["type"] == "context/compacted"
    ]
    assert len(compacted) == 1
    assert compacted[0]["data"]["schema"] == "eight_section"
    assert compacted[0]["data"]["bracket_id"] == body["bracket_id"]


def test_compact_requires_trusted_origin(tmp_path):
    client = _client(tmp_path)
    sid = _seed_history(client)
    before = _events(client, sid)
    resp = client.post(
        f"/api/sessions/{sid}/context/compact",
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code in (403, 401), resp.text
    assert _events(client, sid) == before, "被拒请求零副作用"


# ── 错误矩阵：404 / 422 ────────────────────────────────────────────────


def test_unknown_session_is_404(tmp_path):
    client = _client(tmp_path)
    resp = _post(client, "no-such-session")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "session 'no-such-session' not found"


def test_invalid_session_id_is_422_before_404(tmp_path):
    client = _client(tmp_path)
    resp = _post(client, "bad.id")
    assert resp.status_code == 422, resp.text


def test_invalid_model_is_422_with_zero_event_writes(tmp_path):
    client = _client(tmp_path)
    sid = _seed_history(client)
    before = _events(client, sid)

    resp = _post(client, sid, model="definitely-not-a-model")

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"], "detail 原样上抛"
    assert _events(client, sid) == before, "非法模型必须在零副作用前失败"


# ── 409：在途 run / 压缩已在进行中 ─────────────────────────────────────


def test_busy_run_is_409_with_zero_event_writes(tmp_path, monkeypatch):
    client = _client(tmp_path)
    sid = _seed_history(client)
    before = _events(client, sid)
    monkeypatch.setattr(RunManager, "is_busy", lambda self, _sid: True)

    resp = _post(client, sid)

    assert resp.status_code == 409, resp.text
    assert _events(client, sid) == before, "在途 run 拒绝必须零写入"


def test_second_call_while_compaction_in_flight_is_409(tmp_path):
    """模拟并发连点：per-session in-flight guard 已在途 → 第二次 409、零写入。

    直接置位服务实例的 guard 标识（同步 TestClient 无法真并发）；这是端点把
    "压缩已在进行中"这一 `ActiveRunConflict` 映射成 409 的确定性取证。
    """
    client = _client(tmp_path)
    sid = _seed_history(client)
    before = _events(client, sid)
    service = session_service(client.app.state.agent)
    service._compact_in_flight[sid] = True
    try:
        resp = _post(client, sid)
    finally:
        service._compact_in_flight.pop(sid, None)

    assert resp.status_code == 409, resp.text
    assert _events(client, sid) == before, "压缩进行中拒绝必须零写入"


# ── 写后复核失败：500 fail-closed（不谎报"未改动"）（F2 #635）─────────


def test_post_write_error_is_500_fail_closed(tmp_path, monkeypatch):
    """bracket 已写入但重投影复核失败 → 500（而非 200 + bracket_id=null）。"""
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
    )
    client = TestClient(
        create_app(settings, enable_cors=False), raise_server_exceptions=False,
    )
    sid = _seed_history(client)

    from agent_harness.context.builder import ContextBuilder

    monkeypatch.setattr(
        ContextBuilder, "_reproject",
        lambda self, _session: [AIMessage(content="divergent projection")],
    )
    with patch(
        "agent_harness.model.provider.create_chat_model",
        return_value=_summary_model(),
    ):
        resp = _post(client, sid)

    assert resp.status_code == 500, resp.text
    # 历史已多出 bracket：失败必须是响亮的 500，不能返回"未改动"。
    assert any(
        e["type"] == "context/compacted" for e in _events(client, sid)
    ), "post-bracket 失败时 bracket 三事件已确实落盘"


# ── 非严格幂等：重复调用追加新 bracket ─────────────────────────────────


def test_repeated_compact_appends_new_bracket(tmp_path):
    client = _client(tmp_path)
    sid = _seed_history(client)

    with patch(
        "agent_harness.model.provider.create_chat_model",
        return_value=_summary_model(),
    ):
        first = _post(client, sid)
    assert first.status_code == 200, first.text
    first_bracket = first.json()["bracket_id"]

    # 追加新一轮历史后再压：每次调用都是用户显式请求的一次**新**压缩。
    store = client.app.state.agent.store
    session = Session(sid, store, store.read_events(sid))
    session.append(USER_MESSAGE, {"content": "继续下一段工作。"})
    session.append(MODEL_COMPLETED, {"content": "后续历史 " * 800})
    session.append(USER_MESSAGE, {"content": "current request 2"})

    with patch(
        "agent_harness.model.provider.create_chat_model",
        return_value=_summary_model(),
    ):
        second = _post(client, sid)

    assert second.status_code == 200, second.text
    assert second.json()["bracket_id"] != first_bracket
    compacted = [
        e for e in _events(client, sid) if e["type"] == "context/compacted"
    ]
    assert len(compacted) == 2, "重复调用安全但非 no-op：追加新 bracket"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
