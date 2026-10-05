"""#616 HTTP 面：`POST /api/sessions/{id}/budget/purge-stale-tools`（黑盒）。

端点把陈旧账行名（`session_budgets.tool_call_limits` 里不在根 registry 的名字）清掉，
不动消耗事实、不动 SessionEvent 流，只写一条 `session_budget_events` 审计并 bump version。

**本文件钉的外部契约（#616 第一轮，用户已按推荐拍板）**：

- 路径 `POST /api/sessions/{id}/budget/purge-stale-tools`；
- 可选收窄参数是 **query** `?tool=<name>`（对应 CLI 的 `--tool`）：服务端仍按根 registry
  权威判 stale，点名**正常注册名 / 从未存在名**都是安全 no-op，只有点名**确实陈旧**的名字
  才真清；
- 响应体 = `SessionToolLimitsPurge` 四字段 `{purged, remaining, rows, version}`；
- 来源闸 `require_trusted_origin`；错误语义与 `archive` 端点同口径：
  404 = 会话不存在（`SessionNotFound`），422 = id 形态非法（`InvalidSessionId`）。

黑盒：只经 HTTP + 直接读 `harness.db` / `/events`，不 import 任何服务层内部符号。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel

#: 一个**真实注册**的工具名（装配层内置工具，见 CLI `--run-tool-limit glob=1` 用例）——
#: 它是"非陈旧"的判据样本。
_REGISTERED = "glob"
#: 不在根 registry 的陈旧名（#564 历史污染留下的坏名）。
_STALE = "ghost-tool"
_OTHER_STALE = "other-ghost"


def _app(tmp_path: Path, **overrides):
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test", **overrides
    )
    return create_app(settings, enable_cors=False)


def _client(tmp_path: Path, **overrides) -> TestClient:
    return TestClient(_app(tmp_path, **overrides))


def _create_session(client: TestClient, **payload: object) -> str:
    """建会话（真 runtime + 替身模型），返回 session_id（同归档端点测试的建会话口径）。"""
    base: dict[str, object] = {"task": "建立一个会话", "budget": {"local": {"max_agent_turns": 1}}}
    base.update(payload)
    with patch(
        "agent_harness.assembly.create_chat_model",
        return_value=ScriptedModel(responses=[AIMessage(content="ok")]),
    ):
        resp = client.post("/api/sessions", json=base)
    assert resp.status_code == 200, resp.text
    frames = [
        json.loads(line[len("data:") :].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]
    session_id = next((f["session_id"] for f in frames if f.get("session_id")), None)
    assert session_id, f"SSE 流里没有 session_id：{frames[:3]}"
    return str(session_id)


def _db(client: TestClient) -> Path:
    return Path(client.app.state.agent.harness_db)


def _seed_budget(
    client: TestClient,
    session_id: str,
    *,
    tool_call_limits: dict[str, int],
    calls: dict[str, int] | None = None,
    attempts: dict[str, int] | None = None,
) -> int:
    """把账行造出/改写成给定形状，返回当前 version。

    账行可能已被建会话过程懒补；`INSERT OR IGNORE` + `UPDATE` 两种情形都收敛到目标形状。
    """
    with sqlite3.connect(_db(client)) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO session_budgets (budget_key, root_session_id) VALUES (?, ?)",
            (session_id, session_id),
        )
        connection.execute(
            "UPDATE session_budgets SET tool_call_limits = ?, tool_calls_by_tool = ?, "
            "tool_attempts_by_tool = ? WHERE budget_key = ?",
            (
                json.dumps(tool_call_limits, sort_keys=True),
                json.dumps(calls or {}, sort_keys=True),
                json.dumps(attempts or {}, sort_keys=True),
                session_id,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT version FROM session_budgets WHERE budget_key = ?", (session_id,)
        ).fetchone()
    return int(row[0])


def _row(client: TestClient, session_id: str) -> dict:
    with sqlite3.connect(_db(client)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM session_budgets WHERE budget_key = ?", (session_id,)
        ).fetchone()
    assert row is not None, f"缺少 session_budgets 行：{session_id}"
    return dict(row)


def _limits(client: TestClient, session_id: str) -> dict[str, int]:
    return json.loads(_row(client, session_id)["tool_call_limits"])


def _calls(client: TestClient, session_id: str) -> tuple[dict, dict]:
    row = _row(client, session_id)
    return json.loads(row["tool_calls_by_tool"]), json.loads(row["tool_attempts_by_tool"])


def _budget_events(client: TestClient, session_id: str) -> list[tuple[str, int, str | None]]:
    with sqlite3.connect(_db(client)) as connection:
        return connection.execute(
            "SELECT kind, version, detail FROM session_budget_events "
            "WHERE budget_key = ? ORDER BY rowid",
            (session_id,),
        ).fetchall()


def _jsonl_events(client: TestClient, session_id: str) -> list[dict]:
    resp = client.get(f"/api/sessions/{session_id}/events")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _post(client: TestClient, session_id: str, **params: str):
    return client.post(
        f"/api/sessions/{session_id}/budget/purge-stale-tools", params=params or None
    )


# ── 核心：整会话重整清陈旧、留正常 ──────────────────────────────────────


def test_whole_session_purge_removes_stale_and_keeps_registered(tmp_path):
    client = _client(tmp_path)
    sid = _create_session(client)
    before_version = _seed_budget(
        client, sid, tool_call_limits={_STALE: 5, _REGISTERED: 9}
    )

    resp = _post(client, sid)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["purged"] == {_STALE: 5}
    assert body["remaining"] == {_REGISTERED: 9}
    assert body["version"] == before_version + 1

    assert _limits(client, sid) == {_REGISTERED: 9}, "陈旧名清掉、正常名留住"
    assert _row(client, sid)["version"] == before_version + 1

    kind, version, detail_raw = _budget_events(client, sid)[-1]
    assert kind == "tool_limits_purged"
    assert version == before_version + 1
    detail = json.loads(detail_raw)
    assert detail["purged"] == {_STALE: 5}
    assert detail["remaining"] == {_REGISTERED: 9}


# ── tool 收窄三态：陈旧 / 正常名 / 未知名 ───────────────────────────────


def test_tool_narrowing_stale_name_purges_only_it(tmp_path):
    client = _client(tmp_path)
    sid = _create_session(client)
    before_version = _seed_budget(
        client, sid, tool_call_limits={_STALE: 5, _OTHER_STALE: 7, _REGISTERED: 9}
    )

    resp = _post(client, sid, tool=_STALE)
    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] == {_STALE: 5}
    assert _limits(client, sid) == {_OTHER_STALE: 7, _REGISTERED: 9}
    assert _row(client, sid)["version"] == before_version + 1


def test_tool_narrowing_registered_name_is_a_noop(tmp_path):
    """点名一个**正常注册**的工具：它不是陈旧名 ⇒ 不摘（收窄只在陈旧集合里选）。"""
    client = _client(tmp_path)
    sid = _create_session(client)
    before_version = _seed_budget(
        client, sid, tool_call_limits={_STALE: 5, _REGISTERED: 9}
    )
    before_events = _budget_events(client, sid)

    resp = _post(client, sid, tool=_REGISTERED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] == {}
    assert resp.json()["rows"] == 0
    assert _limits(client, sid) == {_STALE: 5, _REGISTERED: 9}, "正常名不许被清"
    assert _row(client, sid)["version"] == before_version
    assert _budget_events(client, sid) == before_events, "no-op 不落事件"


def test_tool_narrowing_unknown_name_is_a_noop(tmp_path):
    """点名一个账里根本没有的名字 ⇒ 无键可清，幂等 no-op。"""
    client = _client(tmp_path)
    sid = _create_session(client)
    before_version = _seed_budget(client, sid, tool_call_limits={_REGISTERED: 9})
    before_events = _budget_events(client, sid)

    resp = _post(client, sid, tool="never-existed")
    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] == {}
    assert _limits(client, sid) == {_REGISTERED: 9}
    assert _row(client, sid)["version"] == before_version
    assert _budget_events(client, sid) == before_events


# ── 幂等 ───────────────────────────────────────────────────────────────


def test_repeated_purge_is_idempotent(tmp_path):
    client = _client(tmp_path)
    sid = _create_session(client)
    _seed_budget(client, sid, tool_call_limits={_STALE: 5, _REGISTERED: 9})

    assert _post(client, sid).status_code == 200
    version_after_first = _row(client, sid)["version"]
    events_after_first = _budget_events(client, sid)

    second = _post(client, sid)
    assert second.status_code == 200, second.text
    assert second.json()["purged"] == {}
    assert second.json()["rows"] == 0
    assert _row(client, sid)["version"] == version_after_first
    assert _budget_events(client, sid) == events_after_first
    assert _limits(client, sid) == {_REGISTERED: 9}


# ── 不动消耗事实、不动 SessionEvent 流 ──────────────────────────────────


def test_purge_keeps_consumption_facts_and_session_event_stream(tmp_path):
    client = _client(tmp_path)
    sid = _create_session(client)
    _seed_budget(
        client, sid,
        tool_call_limits={_STALE: 5, _REGISTERED: 9},
        calls={_STALE: 2, _REGISTERED: 1},
        attempts={_STALE: 3, _REGISTERED: 1},
    )
    jsonl_before = _jsonl_events(client, sid)

    assert _post(client, sid).status_code == 200

    calls, attempts = _calls(client, sid)
    assert calls == {_STALE: 2, _REGISTERED: 1}, "消耗事实（逻辑调用）不得被清除触碰"
    assert attempts == {_STALE: 3, _REGISTERED: 1}, "消耗事实（真实尝试）不得被清除触碰"
    assert _jsonl_events(client, sid) == jsonl_before, "清理不是会话真相，不进 SessionEvent 流"


# ── 错误语义：404 / 422（同 archive 端点）────────────────────────────────


def test_unknown_session_is_404(tmp_path):
    client = _client(tmp_path)
    resp = _post(client, "no-such-session")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "session 'no-such-session' not found"


def test_invalid_session_id_is_422(tmp_path):
    client = _client(tmp_path)
    resp = _post(client, "bad.id")
    assert resp.status_code == 422, resp.text


# ── 来源闸：非可信来源拒绝 ─────────────────────────────────────────────


def test_purge_requires_trusted_origin(tmp_path):
    """与 archive 同口径：宿主侧账行写操作只接受本机来源（ADR-0025 D1）。"""
    client = _client(tmp_path)
    sid = _create_session(client)
    _seed_budget(client, sid, tool_call_limits={_STALE: 5})
    resp = client.post(
        f"/api/sessions/{sid}/budget/purge-stale-tools",
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code in (403, 401), resp.text
    assert _limits(client, sid) == {_STALE: 5}, "被拒请求零副作用"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
