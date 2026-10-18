"""#1303 §3.3 + §3.4：列表行的看板四字段与「列表契约不回退」（AC-12）。

三件事必须同时为真：

1. **四字段真值**：`product_state` / `product_state_changed_at` /
   `attention_kind` / `attention_summary` 从 `BoardProjectionStore` 回填，
   与 `GET /api/sessions/{id}/task` 同一份派生语义（不复制第二份）；
2. **列表路径零重放**（AC-12）：sidecar 命中时 `read_events` 调用次数为 **0**；
   每行一次 `read_session_summary`（批量入口内部逐条）；
3. **自愈**：删掉 sidecar 后一次重放 + 回填，结果逐字段不变；第二次请求命中缓存
   （`read_events` 仍为 1 次）。

判定「缓存命中」必须同时清掉进程级单例的内存缓存（`board_projection._STORES`）：
只删 sidecar 而内存仍热时，`read_events` 为 0 是内存命中，证不了 sidecar 契约。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_harness.config import Settings
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session import board_projection as bp
from agent_harness.session.event import (
    MODEL_COMPLETED,
    RUN_STARTED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
)
from agent_harness.session.task import apply_task_definition
from agent_harness.web.app import SessionSummary, create_app


def _app(tmp_path: Path):
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return create_app(settings, enable_cors=False)


def _drop_memory_cache(store: JsonlSessionStore) -> None:
    """清掉该 root 的进程级单例——模拟「进程重启 / 冷启动」。

    单例按 `Path(store._root).resolve()` 分片（`board_projection.get_board_projection_store`），
    只弹自己这一个 key，不碰别的测试的 root。
    """
    bp._STORES.pop(str(Path(store._root).resolve()), None)


def _start_session(store: JsonlSessionStore, *, cwd: Path) -> Session:
    session = Session.start(store, cwd=str(cwd))
    outcome = apply_task_definition(
        session,
        task_text="看板地基",
        criteria=[{"text": "能投影", "origin": "user"}],
    )
    assert outcome.ok, outcome.reason
    return session


def _rows(client: TestClient) -> list[dict]:
    resp = client.get("/api/sessions")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── §3.3：四字段映射（断言**值**，不只断言键在）────────────────────────────


def test_rows_carry_board_fields_from_the_projection(tmp_path: Path) -> None:
    """已定义 + 在跑 + 有待回答提问：四字段必须逐字段等于投影真值。"""
    app = _app(tmp_path)
    store = app.state.agent.store
    session = _start_session(store, cwd=tmp_path)
    run_event = session.append(RUN_STARTED, {}, run_id="r1")
    session.append(
        USER_INPUT_REQUESTED,
        {
            "request_id": "q1",
            "kind": "ask_user",
            "run_id": "r1",
            "questions": [{"header": "选 A 还是 B", "question": "q?"}],
        },
        run_id="r1",
    )

    rows = {row["session_id"]: row for row in _rows(TestClient(app))}
    row = rows[session.session_id]
    assert row["product_state"] == "executing"
    assert row["product_state_changed_at"] == run_event.time
    assert row["attention_kind"] == "input"
    assert row["attention_summary"] == "待回答：选 A 还是 B"


def test_rows_carry_last_messages_from_the_projection(tmp_path: Path) -> None:
    """#1308 AC-19：行上的最后一条 user / agent 消息逐字段等于投影真值。"""
    app = _app(tmp_path)
    store = app.state.agent.store
    session = _start_session(store, cwd=tmp_path)
    session.append(USER_MESSAGE, {"content": "改一下标题"})
    session.append(MODEL_COMPLETED, {"content": "已改好"})

    rows = {row["session_id"]: row for row in _rows(TestClient(app))}
    row = rows[session.session_id]
    assert row["last_user_message"] == "改一下标题"
    assert row["last_agent_message"] == "已改好"


def test_last_messages_are_truncated_to_128_chars_in_the_row(tmp_path: Path) -> None:
    """#1308 AC-19 ④：行上两字段是写入侧截断后的值。"""
    app = _app(tmp_path)
    store = app.state.agent.store
    session = _start_session(store, cwd=tmp_path)
    session.append(USER_MESSAGE, {"content": "u" * 200})
    session.append(MODEL_COMPLETED, {"content": "a" * 200})

    row = {row["session_id"]: row for row in _rows(TestClient(app))}[session.session_id]
    assert row["last_user_message"] == "u" * 128
    assert row["last_agent_message"] == "a" * 128


def test_undefined_task_row_is_backlog_with_null_board_fields(tmp_path: Path) -> None:
    """从未 `task/defined`：`product_state == ""`，其余三字段为 null（不伪造）。"""
    app = _app(tmp_path)
    store = app.state.agent.store
    session = Session.start(store, cwd=str(tmp_path))

    rows = {row["session_id"]: row for row in _rows(TestClient(app))}
    row = rows[session.session_id]
    assert row["product_state"] == ""
    assert row["product_state_changed_at"] is None
    assert row["attention_kind"] is None
    assert row["attention_summary"] is None
    assert row["last_user_message"] is None
    assert row["last_agent_message"] is None


def test_response_model_requires_the_last_message_fields() -> None:
    """#1308：两字段在响应模型里同样**必填**（漏映射不得静默渲染成"没有消息"）。"""
    with pytest.raises(ValidationError):
        SessionSummary(
            session_id="s",
            event_count=1,
            first_event_time=None,
            last_event_time=None,
            first_user_message=None,
            workspace=None,
            archived=False,
            corrupted=False,
            product_state="",
            product_state_changed_at=None,
            attention_kind=None,
            attention_summary=None,
        )


def test_response_model_requires_the_board_fields() -> None:
    """四字段在响应模型里**必填**（同 `workspace`/`archived` 的刻意不给默认值）：
    漏映射必须响亮失败，而不是静默把看板渲染成 backlog。"""
    with pytest.raises(ValidationError):
        SessionSummary(
            session_id="s",
            event_count=1,
            first_event_time=None,
            last_event_time=None,
            first_user_message=None,
            workspace=None,
            archived=False,
            corrupted=False,
        )


# ── §3.4 / AC-12：列表路径零重放 + 自愈 ──────────────────────────────────


def test_list_hits_sidecar_without_read_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-12 前半：sidecar 命中 → `read_events` 0 次；每行一次 `read_session_summary`。"""
    app = _app(tmp_path)
    store = app.state.agent.store
    _start_session(store, cwd=tmp_path)
    _start_session(store, cwd=tmp_path)

    # 先清内存缓存：此时若仍零重放，证明命中来自 sidecar 而不是热内存。
    _drop_memory_cache(store)

    read_events_calls = 0
    original_read_events = store.read_events

    def counting_read_events(session_id: str):
        nonlocal read_events_calls
        read_events_calls += 1
        return original_read_events(session_id)

    summary_calls = 0
    original_summary = store.read_session_summary

    def counting_summary(session_id: str):
        nonlocal summary_calls
        summary_calls += 1
        return original_summary(session_id)

    monkeypatch.setattr(store, "read_events", counting_read_events)
    monkeypatch.setattr(store, "read_session_summary", counting_summary)

    rows = _rows(TestClient(app))
    assert len(rows) == 2
    assert read_events_calls == 0, "sidecar 命中时列表路径不得 read_events"
    assert summary_calls == len(rows), "每行恰好一次摘要扫描"


def test_deleted_sidecar_self_heals_and_second_request_hits_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-12 后半：删 sidecar → 一次重放并回填，结果逐字段相等；再请求仍 1 次。"""
    app = _app(tmp_path)
    store = app.state.agent.store
    session = _start_session(store, cwd=tmp_path)
    before = {row["session_id"]: row for row in _rows(TestClient(app))}
    sidecar = store.board_state_path(session.session_id)
    assert sidecar.exists()

    sidecar.unlink()
    _drop_memory_cache(store)

    read_events_calls = 0
    original_read_events = store.read_events

    def counting_read_events(session_id: str):
        nonlocal read_events_calls
        read_events_calls += 1
        return original_read_events(session_id)

    monkeypatch.setattr(store, "read_events", counting_read_events)

    client = TestClient(app)
    healed = {row["session_id"]: row for row in _rows(client)}
    assert healed == before, "删掉 sidecar 后列表行逐字段不变（自愈）"
    assert read_events_calls == 1, "缺 sidecar 允许一次重放回填"
    assert sidecar.exists(), "重放后必须回填 sidecar"

    again = {row["session_id"]: row for row in _rows(client)}
    assert again == before
    assert read_events_calls == 1, "第二次请求必须命中缓存，不得再重放"


def test_list_rejects_and_heals_a_tampered_sidecar(tmp_path: Path) -> None:
    """#1310 验收：坏 sidecar 在 `GET /api/sessions` 入口也被拒并自愈回填。

    列表路径必须经 `BoardProjectionStore.get()` 的三级读取校验；若 service 层
    将来绕过它直接读 sidecar，这条用例会红。
    """
    app = _app(tmp_path)
    store = app.state.agent.store
    session = _start_session(store, cwd=tmp_path)
    sid = session.session_id
    client = TestClient(app)
    before = {row["session_id"]: row for row in _rows(client)}
    assert before[sid]["product_state"] == "pending_verification"

    sidecar = store.board_state_path(sid)

    def rewrite(**changes) -> None:
        payload = json.loads(sidecar.read_text("utf-8"))
        payload.update(changes)
        sidecar.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    # 注入 1：非法枚举 —— 必须回落重放并回填真值。
    rewrite(product_state="banana")
    _drop_memory_cache(store)
    assert {row["session_id"]: row for row in _rows(client)} == before
    healed = json.loads(sidecar.read_text("utf-8"))
    assert healed["product_state"] == "pending_verification", "列表入口必须自愈回填"

    # 注入 2：戳匹配但水位落后于盘上尾 seq —— 同样必须被拒。
    tail = json.loads(store.events_path(sid).read_text("utf-8").splitlines()[-1])["seq"]
    assert tail > 0
    rewrite(projection_seq=tail - 1)
    _drop_memory_cache(store)
    assert {row["session_id"]: row for row in _rows(client)} == before
    healed = json.loads(sidecar.read_text("utf-8"))
    assert healed["projection_seq"] == tail
