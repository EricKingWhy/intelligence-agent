"""#562（RL-04 深度 / F6 reason 长度）：wire 层请求体输入加固——红测。

两个已拍板数值合同（用户 2026-10-03）：

* **请求体 JSON 嵌套深度上限 = 100**（FUZZ-01 同族的 RL-04：深嵌套 ``RecursionError``）。
* ``POST /api/sessions/{id}/approve`` 的 ``reason`` 上限 = **2000 字符**（不是字节）。

## 深度判据（与实现同一口径）

``depth`` = 最深路径上的容器（object + array）层数，**最外层 body 容器记 1**。
请求体 ``{"n": [[…]]}``（外层对象 1 + N 层数组）的 depth = ``N + 1``。上限 100 ⇒
depth ≤ 100 放行，depth ≥ 101 拒绝。构造见 ``_deep_body``。

## 为什么错误面必须是 422（本票与「裸 400」的核心区别）

当前树（无守卫）深 body 走 ``fastapi/routing.py`` 的 ``await request.json()``：
depth≳3000 时 ``json.loads`` 抛 ``RecursionError``，被 ``routing.py`` 的
``except Exception`` 归成**裸 400** ``There was an error parsing the body``
（本轮实测：depth 1500 尚能解析 ⇒ 422 ``extra_forbidden``；depth 3000 ⇒ 400）——
后者与「显式配额拒绝」语义不符（audit：拒绝而非崩溃）。守卫落在**解析之前**，
用固定字符串信封回 422。

## 与上一轮只读侦察（recon562.md §7）的偏差（以本轮实测为准）

recon 计划用 ``detail[0].type == "body_too_deep"``（数组形态）。本轮按施工单
B 节「``detail`` 保持字符串契约」实现，故断言**字符串信封**（``ErrorEnvelope``，
与 ``http_error`` 的业务 422 同形，OpenAPI 422 已声明 oneOf 覆盖两种形态）。
判别式因此改为：守卫命中 ⇒ ``detail`` 是**字符串**且等于 ``BODY_TOO_DEEP_DETAIL``；
放行 ⇒ ``detail`` 是**数组**（pydantic ``extra_forbidden``）。两种形态互斥，可判真伪。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app
from agent_harness.web.wire_safety import BODY_MAX_DEPTH

JSON_HDR = {"content-type": "application/json"}

#: 深度配额 422 的 ``detail`` **字符串契约**（字面量钉在这里，另由
#: ``test_quota_detail_string_is_stable`` 与实现常量对账，杜绝两侧漂移）。
_EXPECTED_QUOTA_DETAIL = "request body nesting too deep"

#: 合法最大嵌套：`budget.session.tool_call_limits`（depth=4，实测当前树 200）。
_LEGAL_MAX_DEPTH_BODY = {"budget": {"session": {"tool_call_limits": {"bash": 3}}}}


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return TestClient(create_app(settings, enable_cors=False))


def _deep_body(depth: int) -> bytes:
    """构造总容器深度恰为 ``depth`` 的 body：``{"n": [[…0…]]}``。

    depth = 1（外层对象）+ ``depth - 1`` 层数组。
    """
    assert depth >= 1
    inner = depth - 1
    return b'{"n": ' + b"[" * inner + b"0" + b"]" * inner + b"}"


def _post_raw(client: TestClient, path: str, raw: bytes):
    return client.post(path, content=raw, headers=JSON_HDR)


def _session_dirs(tmp_path: Path) -> set[str]:
    root = tmp_path / "sessions"
    if not root.exists():
        return set()
    return {p.name for p in root.iterdir() if p.is_dir()}


def _is_quota_422(resp) -> bool:
    """守卫命中的判别式：422 + ``detail`` 是等于配额的**字符串**信封。"""
    return resp.status_code == 422 and resp.json().get("detail") == _EXPECTED_QUOTA_DETAIL


def test_quota_detail_string_is_stable() -> None:
    """实现常量必须等于测试钉住的字符串契约（单边改名 ⇒ 此例红）。"""
    from agent_harness.web.wire_safety import BODY_TOO_DEEP_DETAIL

    assert BODY_TOO_DEEP_DETAIL == _EXPECTED_QUOTA_DETAIL


# ── 1. 深度：上限 100 ⇒ 98/99/100 放行，101 拒绝 ─────────────────────────


@pytest.mark.parametrize("depth", [98, 99, 100])
def test_depth_at_or_below_limit_passes_guard(tmp_path: Path, depth: int) -> None:
    """98/99/100 **必须通过守卫**：落到 pydantic，``detail`` 是数组（``extra_forbidden``），
    不是配额字符串信封。"""
    client = _client(tmp_path)
    resp = _post_raw(client, "/api/sessions?launch=false", _deep_body(depth))
    assert resp.status_code == 422, f"depth={depth} {resp.status_code} {resp.text[:200]}"
    detail = resp.json()["detail"]
    assert isinstance(detail, list), f"depth={depth} 守卫不应命中：{detail!r}"
    assert detail != _EXPECTED_QUOTA_DETAIL


def test_depth_101_is_refused_by_quota_guard(tmp_path: Path) -> None:
    """101（limit+1）**必须被拒**：422 且是配额字符串信封（解析前拦下）。"""
    client = _client(tmp_path)
    resp = _post_raw(client, "/api/sessions?launch=false", _deep_body(101))
    assert _is_quota_422(resp), f"{resp.status_code} {resp.text[:200]}"


@pytest.mark.parametrize("depth", [1500, 3000])
def test_far_over_depth_is_422_not_400(tmp_path: Path, depth: int) -> None:
    """1500/3000 必须 **422**（不是 400、不是 500）——本票与「裸 400」的核心区别。"""
    client = _client(tmp_path)
    resp = _post_raw(client, "/api/sessions?launch=false", _deep_body(depth))
    assert resp.status_code == 422, f"depth={depth} {resp.status_code} {resp.text[:200]}"
    assert _is_quota_422(resp), f"depth={depth} {resp.text[:200]}"


# ── 2. 守卫必须在解析前生效 ─────────────────────────────────────────────


def test_depth_guard_runs_before_json_parse(tmp_path: Path) -> None:
    """非法/非 JSON 且超深的 body 仍是配额 422 —— 证明守卫先于 ``request.json()``。

    判据：``b"[" * 3000 + b"not-json"`` 无法被 ``json.loads`` 解析。**解析后**的
    守卫（或任何依赖解析结果的判定）对不可解析输入结构上不可能产出配额标记——
    当前树走的正是 ``json.loads`` 的 ``RecursionError`` → ``routing.py`` 裸
    **400** ``There was an error parsing the body``。拿到固定字符串 422 即证明
    深度判定在解析之前独立完成。
    """
    client = _client(tmp_path)
    resp = _post_raw(client, "/api/sessions?launch=false", b"[" * 3000 + b"not-json")
    assert resp.status_code == 422, f"{resp.status_code} {resp.text[:200]}"
    assert _is_quota_422(resp), resp.text[:200]


# ── 3. 防过度拒绝 + 无副作用 ────────────────────────────────────────────


def test_legal_max_nesting_depth_request_still_ok(tmp_path: Path) -> None:
    """合法最大嵌套（depth=4）必须照常 200 —— 上限 100 不得伤到既有请求。"""
    client = _client(tmp_path)
    resp = client.post(
        "/api/sessions?launch=false", json=_LEGAL_MAX_DEPTH_BODY, headers=JSON_HDR
    )
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:200]}"
    assert "session_id" in resp.json()


def test_depth_rejection_has_no_side_effects(tmp_path: Path) -> None:
    """深 body 被拒 ⇒ 不得建会话目录 / 不得落任何事件（入口拒绝且无副作用）。"""
    client = _client(tmp_path)
    before = _session_dirs(tmp_path)
    for depth in (101, 1500, 3000):
        resp = _post_raw(client, "/api/sessions?launch=false", _deep_body(depth))
        assert _is_quota_422(resp), f"depth={depth} {resp.text[:200]}"
    assert _session_dirs(tmp_path) == before


# ── 4. reason 长度：字符口径，2000 通过 / 2001 拒绝 ─────────────────────


def _approve(client: TestClient, reason: str):
    # approval_id=None 是既有 seam 入口：校验通过 ⇒ 200 received（不触会话）。
    # 故 200 = 校验放行、422 = 校验拒绝，判别干净，无需起真交互式审批。
    return client.post(
        "/api/sessions/irrelevant/approve",
        json={"approval_id": None, "approved": True, "reason": reason},
    )


@pytest.mark.parametrize("size", [1999, 2000])
def test_reason_at_or_below_limit_accepted(tmp_path: Path, size: int) -> None:
    """1999/2000 字符必须放行（200 received）。"""
    client = _client(tmp_path)
    resp = _approve(client, "R" * size)
    assert resp.status_code == 200, f"size={size} {resp.status_code} {resp.text[:200]}"
    assert resp.json()["status"] == "received"


def test_reason_over_limit_is_422(tmp_path: Path) -> None:
    """2001 字符 ⇒ 422。"""
    client = _client(tmp_path)
    resp = _approve(client, "R" * 2001)
    assert resp.status_code == 422, f"{resp.status_code} {resp.text[:200]}"
    assert resp.json().get("detail")


def test_reason_limit_is_chars_not_utf8_bytes(tmp_path: Path) -> None:
    """上限按**字符**计：``"测"*2000`` = 6000 UTF-8 字节，必须放行；2001 字符 ⇒ 422。

    若误按字节计，6000 > 2000 会把合法中文直接拒掉——这条钉住口径。
    """
    client = _client(tmp_path)

    ok = _approve(client, "测" * 2000)
    assert ok.status_code == 200, f"2000 个中文（6000 字节）不得被拒：{ok.text[:200]}"

    bad = _approve(client, "测" * 2001)
    assert bad.status_code == 422, f"{bad.status_code} {bad.text[:200]}"


def test_reason_422_does_not_echo_raw_input(tmp_path: Path) -> None:
    """超限 422 响应体不得回显原始输入（audit：不得回显原始秘密）。"""
    client = _client(tmp_path)
    resp = _approve(client, "SECRET-" + "R" * 2001)
    assert resp.status_code == 422, resp.text[:200]
    assert "SECRET" not in resp.text, resp.text[:200]


# ── 5. 反向锚：既有合法请求不得被新守卫误伤 ─────────────────────────────


def test_legal_existing_requests_are_not_harmed(tmp_path: Path) -> None:
    """三端点既有合法请求的行为锚（守卫/reason 上限不得把它们变红）。"""
    client = _client(tmp_path)

    created = client.post("/api/sessions?launch=false", json={})
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]

    # messages：合法短 content（launch=false 不触发 run）
    queued = client.post(
        f"/api/sessions/{sid}/messages?launch=false", json={"content": "hello"}
    )
    assert queued.status_code in (200, 202), queued.text

    # approve：既有合法短 reason（approval_id=None seam ⇒ 200）
    approved = _approve(client, "approved by test")
    assert approved.status_code == 200, approved.text


# ── 6. WS 帧级深度守卫（#562 RL-04；用户 2026-10-03 拍板覆盖 WS）────────────
#
# 为什么 WS 也必须覆盖（与 HTTP body 守卫同源，但后果更坏）：
#   ``/api/ws`` 的 ``send_message`` 帧是本仓**唯一绕过 pydantic** 的写入口。
#   深帧在 ``websocket.py`` 的 ``json.loads(raw)`` 处抛 ``RecursionError``；该处
#   **只 catch ``json.JSONDecodeError``**，于是 ``RecursionError`` 逃到读循环末尾的
#   ``except Exception: logger.debug``——读循环静默死掉 ⇒ **客户端无任何帧、连接
#   静默断开**（比 HTTP 的裸 400 更难诊断）。
#
# 修法与 HTTP 同源：在 ``json.loads`` **之前**用 ``json_container_depth`` 判深度，
# 超限回 ``{"type": "error", "message": BODY_TOO_DEEP_DETAIL}`` 帧并 ``continue``
# ——与 #548 的 lone-surrogate 帧级拒绝同一形态（连接存活、零副作用）。
#
# 与 #548 守卫的**顺序**：深度守卫在 ``json.loads`` 之前（解析前、原始文本层）；
# #548 的 ``lone_surrogate_path`` 在解析后的 ``send_message`` 分支。深度问题会在
# ``json.loads`` 内部先爆栈，**结构上够不到**解析后的那道守卫，故必须让它更早。
#
# 判别式（两态互斥，可独立复核）：
#   命中 ⇒ ``{"type": "error", "message": == BODY_TOO_DEEP_DETAIL}``；
#   放行 ⇒ 到达原分支（pong / snapshot / cancelled / 业务 error），message ≠ 配额串。
#
# 深度口径与 HTTP 完全一致（最外层容器记 1）——``_ws_deep_frame`` 里由
# ``json_container_depth`` 亲自对账，避免手算漂移。


def _ws_deep_frame(
    depth: int,
    *,
    msg_type: str = "ping",
    session_id: str | None = None,
    content: str | None = None,
) -> str:
    """构造总容器深度恰为 ``depth`` 的 WS 文本帧。

    depth = 1（外层对象）+ ``depth - 1`` 层数组（放进 ``pad`` 字段）。手工拼串
    （而非先建深 Python 对象再 ``json.dumps``）——后者在深输入上会自己爆栈。
    """
    assert depth >= 1
    inner = depth - 1
    fields = ['"type": ' + json.dumps(msg_type)]
    if session_id is not None:
        fields.append('"session_id": ' + json.dumps(session_id))
    if content is not None:
        fields.append('"content": ' + json.dumps(content))
    fields.append('"pad": ' + "[" * inner + "0" + "]" * inner)
    frame = "{" + ", ".join(fields) + "}"
    # 自证：夹具产出的深度必须等于请求值（手算错了这里当场红，而非静默改判据）。
    from agent_harness.web.wire_safety import json_container_depth

    assert json_container_depth(frame.encode("utf-8")) == depth, frame[:80]
    return frame


def _recv_ws_frame(ws) -> dict:
    """收一帧业务帧，跳过服务端心跳 ``server_ping``（默认 2s 一次，正常不出现）。"""
    for _ in range(10):
        frame = json.loads(ws.receive_text())
        if frame.get("type") != "server_ping":
            return frame
    raise AssertionError("连续多帧都是 server_ping，未收到业务帧")


def _seed_session(client: TestClient) -> str:
    created = client.post("/api/sessions?launch=false", json={})
    assert created.status_code == 200, created.text
    return created.json()["session_id"]


def test_ws_deep_send_message_frame_refused_with_no_side_effects(tmp_path: Path) -> None:
    """depth=101 的 ``send_message`` 帧 ⇒ 配额 error 帧 + 零副作用 + 连接存活。

    ``content`` 故意留空：改前（无守卫）帧能穿过 ``json.loads``，落到
    ``send_message`` 分支的「missing session_id or content」——**不触发 run**，
    使改前读数可在不挂死的前提下取得（判别式落在 message 上，而非是否回帧）。
    有守卫后走的是配额分支，与业务分支互斥。
    """
    client = _client(tmp_path)
    sid = _seed_session(client)
    events = tmp_path / "sessions" / sid / "events.jsonl"
    before = events.read_bytes()

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(_ws_deep_frame(101, msg_type="send_message", session_id=sid))
        frame = _recv_ws_frame(ws)
        # 连接必须存活：随后一条合法 ping 仍能拿到 pong。
        ws.send_text(json.dumps({"type": "ping"}))
        alive = _recv_ws_frame(ws)

    assert frame.get("type") == "error", frame
    assert frame.get("message") == _EXPECTED_QUOTA_DETAIL, frame
    assert alive.get("type") == "pong", alive
    assert events.read_bytes() == before, "被拒深帧不得留下任何副作用（events.jsonl 逐字节不变）"


def test_ws_depth_boundary_limit_passes_plus_one_refused(tmp_path: Path) -> None:
    """边界：depth == ``BODY_MAX_DEPTH``（100）放行、``+1``（101）拒绝。"""
    client = _client(tmp_path)
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(_ws_deep_frame(BODY_MAX_DEPTH))
        allowed = _recv_ws_frame(ws)
        ws.send_text(_ws_deep_frame(BODY_MAX_DEPTH + 1))
        refused = _recv_ws_frame(ws)
        ws.send_text(json.dumps({"type": "ping"}))
        alive = _recv_ws_frame(ws)

    # 100 放行 ⇒ 走到 ping 分支 ⇒ pong（不是 error 帧）
    assert allowed.get("type") == "pong", allowed
    assert allowed.get("message") != _EXPECTED_QUOTA_DETAIL, allowed
    # 101 拒绝 ⇒ 配额 error 帧
    assert refused.get("type") == "error", refused
    assert refused.get("message") == _EXPECTED_QUOTA_DETAIL, refused
    assert alive.get("type") == "pong", alive


def test_ws_depth_guard_runs_before_json_parse(tmp_path: Path) -> None:
    """守卫必须**先于** ``json.loads(raw)``（判据可独立复核）。

    同一段**非法 JSON** 文本，只变容器深度：深（101）⇒ 配额 error；浅（3）⇒
    走原解析路径的 ``JSONDecodeError`` ⇒ ``"invalid JSON"``。既然浅的那条确实
    触发了 ``json.loads`` 的失败分支，而深的那条**没有**，唯一变量是深度 ⇒
    深度判定发生在解析之前。把守卫移到 ``json.loads`` 之后，本例如期变红
    （深的那条会变成 ``"invalid JSON"``）。

    深度取 101 而非数千：数千会让改前的 ``json.loads`` 抛 ``RecursionError``、
    读循环静默死掉 ⇒ 客户端无帧、本用例**挂死**（recon562 §8 实测坑），
    取 101 既越界又不触发解析器爆栈，改前读数稳定可得。
    """
    client = _client(tmp_path)
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text("[" * 101 + "not-json")
        deep = _recv_ws_frame(ws)
        ws.send_text("[" * 3 + "not-json")
        shallow = _recv_ws_frame(ws)

    assert deep.get("type") == "error", deep
    assert deep.get("message") == _EXPECTED_QUOTA_DETAIL, deep
    assert shallow.get("type") == "error", shallow
    assert shallow.get("message") == "invalid JSON", shallow


def test_ws_depth_guard_call_precedes_json_loads_in_source(tmp_path: Path) -> None:
    """接线守卫（结构证明）：源码里守卫调用位于 ``json.loads(raw)`` **之前**。

    与 ``test_web_ws_snapshot_offload`` 的接线守卫同法——深度守卫最可能被
    "顺手挪到解析之后"而静默失效（测试仍能过一部分）。直接读 handler 源码
    比对两个调用的偏移，任何人重排都能复核。
    """
    del tmp_path  # 纯源码断言，不需要夹具
    from agent_harness.web import websocket as wsmod

    source = inspect.getsource(wsmod.handle_websocket)
    assert "json_container_depth(" in source, "深度守卫调用不在 handler 里"
    assert "BODY_MAX_DEPTH" in source, "深度上限常量没被引用"
    guard_at = source.index("json_container_depth(")
    parse_at = source.index("json.loads(raw)")
    assert guard_at < parse_at, (
        "深度守卫必须落在 json.loads(raw) 之前，否则深帧会先在解析处爆栈（守卫够不到）"
    )


def test_ws_legal_frame_shapes_are_not_harmed(tmp_path: Path) -> None:
    """反向锚：四种既有合法帧的真实形状（深度 1–3）不得被深度守卫误伤。

    - ``ping`` ⇒ ``pong``；
    - ``subscribe``（真 session）⇒ ``snapshot``；
    - ``send_message``（不存在的 session，避免起真 run）⇒ 业务 error，非配额；
    - ``cancel``（真 session）⇒ ``cancelled`` / 业务 error，非配额。
    """
    client = _client(tmp_path)
    sid = _seed_session(client)
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(json.dumps({"type": "ping"}))
        pong = _recv_ws_frame(ws)

        ws.send_text(json.dumps({"type": "subscribe", "session_id": sid}))
        snapshot = _recv_ws_frame(ws)

        ws.send_text(json.dumps({
            "type": "send_message", "session_id": "no-such-session", "content": "hi",
        }))
        msg_err = _recv_ws_frame(ws)

        ws.send_text(json.dumps({"type": "cancel", "session_id": sid}))
        cancel = _recv_ws_frame(ws)

    assert pong.get("type") == "pong", pong
    assert snapshot.get("type") == "snapshot", snapshot
    assert snapshot.get("session_id") == sid, snapshot
    for got in (msg_err, cancel):
        # 放行的判据：拿到的是业务回声，而不是深度配额串（误伤会回配额串）。
        assert got.get("message") != _EXPECTED_QUOTA_DETAIL, got
