"""#548（P0）+ #562（FUZZ-01）：wire 层 Unicode/序列化边界。

背景（2026-10-03 在当前 main 重新复现，基线 22cd4d64）——**票面根因被实测纠正**：

* 票面说「被接受的输入含 lone surrogate」经 ``POST /api/sessions`` 与
  ``POST /api/sessions/{id}/messages`` 稳定 500（SSE / store 的
  ``ensure_ascii=False`` 出口）。实测：这两条 HTTP 入口的 lone surrogate 在
  **pydantic 边界就被拒**（``type='string_unicode'``），根本没走到 SSE / store；
  可见的 500 来自 **422 错误处理器自己的响应渲染**
  （``jsonable_encoder(exc.errors())`` 保留原文 → ``JSONResponse.render`` 的
  ``json.dumps`` 编不出 surrogate）——与 #562 FUZZ-01 是**同一个**缺陷。
* 真正的「接受路径」是 **WS 第四入口**：``websocket.py`` 的 ``send_message`` 帧
  **不做任何 pydantic 校验**，``json.loads`` 收下 ``\\ud800`` 后直达
  ``SessionService`` → ``runtime.py:1279`` → ``session/store.py:304`` 的
  ``fh.write`` 抛 ``UnicodeEncodeError``，被 ``WS read loop`` 的
  ``except Exception: logger.debug`` 吞掉。现象比 500 更坏：客户端收到
  ``{"type":"launched","run_started":true}``，而事件流里**没有** ``user/message``
  ——输入静默丢失、会话处于「已 resume 无 user message」的中间态。

产品语义（用户 2026-10-03 裁决）：**入口拒绝 + 无副作用**。不做 U+FFFD 替换
（替换会丢用户数据），不做全仓 ``safe_dumps`` 重构；``ensure_ascii=False`` 对
合法中文/emoji 本身正确，不得作为消灭目标。

两种上线形态必须分开测（实测：形态 B 连传输层都不合法）：
  A. JSON 转义 ``"abc\\ud800def"``——body 是**合法 ASCII/UTF-8**，RFC 8259 允许，
     这是真实攻击面（``json.dumps`` 默认 ensure_ascii=True 的产物）；
  B. 原始非法 UTF-8 字节 ``ED A0 80``（``surrogatepass``）——Jiter 更早拒绝。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel

JSON_HDR = {"content-type": "application/json"}

#: 用 chr() 构造，源码里不出现 lone surrogate 字面转义——**实测坑**：非 raw 的
#: docstring/字面量含 ``\ud800`` 会让 ``compile()`` 阶段直接 UnicodeEncodeError
#: （``py_compile`` 只打一行 ``Sorry:``，无 traceback，极难归因）。
LONE_HIGH = "abc" + chr(0xD800) + "def"
LONE_LOW = "abc" + chr(0xDFFF) + "def"
LEGAL_PAIR = "abc" + "\U0001F600" + "def"  # 合法代理对（UTF-16 上 D83D DE00）
CJK_EMOJI = "中文 😀 emoji 测试"


class _OneTurnModel(ScriptedModel):
    """每轮吐一条最终 AIMessage（剧本耗尽即报错，单条足够）。"""

    def __init__(self) -> None:
        super().__init__([AIMessage(content="task done")])


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch | None = None) -> TestClient:
    if monkeypatch is not None:
        # 同 `test_projects_api.py` 的接缝：真 runtime + 替身模型。
        monkeypatch.setattr(
            "agent_harness.assembly.create_chat_model",
            lambda *a, **kw: _OneTurnModel(),
        )
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return TestClient(create_app(settings, enable_cors=False))


def _ascii(payload: dict) -> bytes:
    """形态 A：``json.dumps`` 默认 ensure_ascii=True（合法 ASCII body）。"""
    return json.dumps(payload).encode("utf-8")


def _surrogatepass(payload: dict) -> bytes:
    """形态 B：原始非法 UTF-8 字节。"""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8", "surrogatepass")


def _post_json(client: TestClient, path: str, raw: bytes):
    return client.post(path, content=raw, headers=JSON_HDR)


def _session_dirs(tmp_path: Path) -> set[str]:
    root = tmp_path / "sessions"
    if not root.exists():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir()}


def _seed(client: TestClient) -> str:
    resp = client.post("/api/sessions?launch=false", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


# ── 1. HTTP 入口：两种形态的 lone surrogate 都必须是 422，绝不 500 ──────────


@pytest.mark.parametrize("bad", [LONE_HIGH, LONE_LOW])
@pytest.mark.parametrize("form", ["ascii", "surrogatepass"])
def test_sessions_entry_lone_surrogate_is_422_not_500(
    tmp_path: Path, bad: str, form: str
) -> None:
    """``POST /api/sessions``（launch 默认 true）含 lone surrogate → 422。"""
    client = _client(tmp_path)
    raw = _ascii({"task": bad}) if form == "ascii" else _surrogatepass({"task": bad})
    resp = _post_json(client, "/api/sessions", raw)
    assert resp.status_code == 422, f"{resp.status_code} {resp.text[:200]}"
    body = resp.json()
    assert isinstance(body.get("detail"), list), body
    assert body["detail"], "422 必须给出可读的校验理由"


@pytest.mark.parametrize("bad", [LONE_HIGH, LONE_LOW])
def test_messages_entry_lone_surrogate_is_422_not_500(tmp_path: Path, bad: str) -> None:
    """``POST /api/sessions/{id}/messages`` 含 lone surrogate → 422。"""
    client = _client(tmp_path)
    sid = _seed(client)
    resp = _post_json(
        client, f"/api/sessions/{sid}/messages", _ascii({"content": bad})
    )
    assert resp.status_code == 422, f"{resp.status_code} {resp.text[:200]}"
    assert isinstance(resp.json().get("detail"), list)


def test_rejected_lone_surrogate_has_no_side_effects(tmp_path: Path) -> None:
    """被拒请求不得建会话目录 / 不得落任何事件（裁决：入口拒绝 **且无副作用**）。"""
    client = _client(tmp_path)
    before = _session_dirs(tmp_path)
    for path, payload in (
        ("/api/sessions", {"task": LONE_HIGH}),
        ("/api/projects", {"path": LONE_HIGH}),
    ):
        resp = _post_json(client, path, _ascii(payload))
        assert resp.status_code == 422, f"{path} {resp.status_code}"
    assert _session_dirs(tmp_path) == before


def test_rejected_payload_with_non_finite_number_is_422_not_500(tmp_path: Path) -> None:
    """FUZZ-01 同族：被拒字段里带 ``inf``（``1e999``）→ 422，不是 500。"""
    client = _client(tmp_path)
    resp = client.post(
        "/api/sessions?launch=false", content=b'{"task":"a","n":1e999}', headers=JSON_HDR
    )
    assert resp.status_code == 422, f"{resp.status_code} {resp.text[:200]}"
    assert isinstance(resp.json().get("detail"), list)


def test_rejected_422_body_does_not_echo_raw_input(tmp_path: Path) -> None:
    """错误响应不得回显原始输入（#562 audit：原始秘密 / 递归结构都不许带出去）。

    判据三重：原文子串不在体里、其转义形态也不在体里、detail 条目只剩
    ``{type, loc, msg}``——FastAPI 默认实现会原样带上 ``input``。
    """
    client = _client(tmp_path)
    resp = _post_json(client, "/api/sessions", _ascii({"task": "SECRET-" + LONE_HIGH}))
    assert resp.status_code == 422, resp.text[:200]
    body = resp.text
    assert "SECRET" not in body
    assert "\\ud800" not in body  # 连转义形态也不回显
    assert set(resp.json()["detail"][0]) == {"type", "loc", "msg"}


# ── 2. 不得过度拒绝：合法代理对 / 中文 emoji 必须放行 ─────────────────────


@pytest.mark.parametrize("good", [LEGAL_PAIR, CJK_EMOJI])
def test_legal_text_is_not_rejected_by_unicode_guard(tmp_path: Path, good: str) -> None:
    """合法文本必须穿过校验层。

    用 ``?launch=false`` + task 的**业务** 422（detail 是字符串「互斥」）作判据：
    拿到字符串信封就证明 pydantic 放行了这段文本——若被 Unicode 守卫误拒，
    detail 会是校验数组。
    """
    client = _client(tmp_path)
    resp = _post_json(client, "/api/sessions?launch=false", _ascii({"task": good}))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str), detail
    assert "互斥" in detail, detail


def test_legal_cjk_emoji_round_trips_through_json_response(tmp_path: Path) -> None:
    """``ensure_ascii=False`` 对合法中文/emoji 本身正确：响应必须逐字回读。"""
    client = _client(tmp_path)
    resp = client.post("/api/projects", json={"path": str(tmp_path), "title": CJK_EMOJI})
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["title"] == CJK_EMOJI


# ── 3. WS 第四入口：真正的「接受路径」 ─────────────────────────────────


def test_ws_send_message_lone_surrogate_is_refused_with_error_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WS 帧含 lone surrogate → 回 ``error`` 帧，**且会话事件流一字未动**。

    当前（缺陷态）：服务端回 ``launched`` / ``run_started: true``，随后后台 run
    在 ``store.py`` 的 ``fh.write`` 抛 ``UnicodeEncodeError`` 被日志吞掉——
    输入静默丢失。
    """
    client = _client(tmp_path, monkeypatch)
    sid = _seed(client)
    events = tmp_path / "sessions" / sid / "events.jsonl"
    before = events.read_bytes()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(
            json.dumps({"type": "send_message", "session_id": sid, "content": LONE_HIGH})
        )
        frame = json.loads(ws.receive_text())

    assert frame["type"] == "error", frame
    assert events.read_bytes() == before, "被拒帧不得留下任何副作用"


def test_ws_send_message_legal_cjk_emoji_still_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对照：合法中文/emoji 帧必须照常 ``launched``（不得过度拒绝）。"""
    client = _client(tmp_path, monkeypatch)
    sid = _seed(client)

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(
            json.dumps({"type": "send_message", "session_id": sid, "content": CJK_EMOJI})
        )
        frame = json.loads(ws.receive_text())

    assert frame["type"] != "error", frame
