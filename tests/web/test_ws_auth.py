"""#890 WS 鉴权接缝：`/api/ws` 与 HTTP 面同一口径。

三条契约（票面验收 1–3 逐条对应）：

1. **配置 `jwt_secret` = fail-closed**：无 Bearer / 坏 Bearer / 过期 Bearer 的握手
   一律在 `websocket.accept()` **之前**被拒；带合法 Bearer 才建连。
2. **未配置 `jwt_secret` = 本地信任模式 + 来源闸**：无凭据照常可用（开发形态语义不变），
   但带 `Origin` 且非本机 hostname 的握手被拒——这正是浏览器 drive-by 的形状
   （WS 握手不受 CORS 约束，服务端不判 Origin 就等于允许任意网页连上来读写会话）。
3. **有凭据行为不变**：接缝放行的连接继续走既有 `handle_websocket`（本文件只锚握手层，
   下行帧契约由 `tests/web/test_web_ws_relay.py` 等既有用例钉住）。

来源（方案依据，机制照搬不逐字复制）：
- Django Channels `531894e5b2a825168e16fd82f1766f748f71faf4`
  `channels/security/websocket.py`：`OriginValidator.__call__` 在把 scope 交给 application
  **之前**判定，失败走 `WebsocketDenier`（`connect()` 里 `await self.close()`）——即
  "拒 = accept 之前 close"这条形状。
- Phoenix `v1.8.15` `lib/phoenix/socket/transport.ex:341-400` `check_origin`：
  `is_nil(origin)`（非浏览器发起）⇒ **放行**；否则比对来源，不匹配 ⇒ 握手前 403。
- Socket.IO 4.8.4 官方文档 `docs/v4/middlewares/`：鉴权在**连接建立之前**完成，
  失败即拒绝连接（不是建立后再发错误帧）。

判别力说明（为什么这些用例能区分"拒"与"放行"）：被拒的握手由 uvicorn 回 HTTP 403，
客户端在 `__enter__` 阶段就抛 `WebSocketUpgradeError`（`response.status_code == 403`）；
放行的握手拿到 `websocket.accept`，客户端正常进入协议循环。断言里**同时**要求
"抛的是升级失败"且"状态码 403"，因此"先 accept 再关"的实现（handler 里主动 close）
会被区分出来——那种实现拿到的是已建立的连接。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_harness.config import Settings
from agent_harness.web.app import create_app

_SECRET = "ws-auth-test-signing-secret-at-least-32-chars"


def _token(secret: str = _SECRET, *, expires_in: int | None = 600) -> str:
    payload: dict[str, object] = {
        "tenant_id": "acme", "user_id": "alice", "scopes": ["user", "session"],
    }
    if expires_in is not None:
        payload["exp"] = int((datetime.now(UTC) + timedelta(seconds=expires_in)).timestamp())
    return jwt.encode(payload, secret, algorithm="HS256")


def _app(tmp_path: Path, *, jwt_secret: str | None = None):
    return create_app(Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        jwt_secret=jwt_secret, enable_cors=False,
    ))


def _headers(**headers: str) -> dict[str, str] | None:
    # 空 headers 必须**省略**这个 kwarg：TestClient 收到显式 `None` 会在迭代时报错。
    return headers or None


def _ping_pong(ws) -> str:
    """放行的连接必须是既有 `handle_websocket` 的协议行为（不是被换成了桩）。"""
    ws.send_text(json.dumps({"type": "ping"}))
    return json.loads(ws.receive_text())["type"]


# ── 配置 jwt_secret：fail-closed（验收 1）──────────────────────────────


def test_refuses_bearerless_handshake_when_secret_configured(tmp_path: Path) -> None:
    """无凭据 ⇒ 握手被拒。

    鉴别力锚点：拒发生在 `accept()` **之前** ⇒ 客户端 `__enter__` 就抛
    `WebSocketDisconnect`（starlette testclient 的 `_raise_on_close`）。
    若实现改成"先 accept 再关"，这里拿到的是已建立的连接、不会抛。
    """
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect("/api/ws"):
        pass


@pytest.mark.parametrize("authorization", [
    "Bearer not-a-jwt",
    f"Bearer {_token('another-signing-secret-32-chars-long!!')}",  # 签名不对
    f"Bearer {_token(expires_in=-60)}",                            # 已过期
    f"Bearer {_token(expires_in=None)}",                           # 无 exp（强制过期语义）
    f"Bearer {_token()}extra",                                     # 形状不合
])
def test_refuses_bad_bearer_when_secret_configured(tmp_path: Path, authorization: str) -> None:
    """坏 token 与漏 token 同一结果：拒绝。逐条对应 HTTP 面 `AuthSeamMiddleware`
    已有的四种 401 形状（无 exp / 过期 / 伪造签名 / 非 Bearer）。"""
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect("/api/ws", headers={"Authorization": authorization}):
        pass


@pytest.mark.parametrize("authorization", [
    "Basic YWxpY2U6cHc=",  # 非 Bearer scheme
    _token(),              # 裸 token（漏了 scheme）
    "Bearer",              # 只有 scheme
])
def test_refuses_bearer_that_is_not_bearer_scheme(tmp_path: Path, authorization: str) -> None:
    """`Basic ...` / 裸 token 一律拒——HTTP 面同款（scheme 必须是 bearer）。"""
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect("/api/ws", headers={"Authorization": authorization}):
        pass


def test_accepts_valid_bearer_when_secret_configured(tmp_path: Path) -> None:
    """合法凭据 ⇒ 放行（验收 2 的握手侧半边）。

    must-not-over-fix 锚点：放行后必须是既有 `handle_websocket` 的协议行为——
    客户端 `ping` 拿到 `pong`。若接缝把连接关在别的分支上（例如无条件 close），
    这里拿不到 pong。
    """
    bearer = {"Authorization": f"Bearer {_token()}"}
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            client.websocket_connect("/api/ws", headers=bearer) as ws:
        assert _ping_pong(ws) == "pong"


def test_valid_bearer_does_not_make_origin_a_gate(tmp_path: Path) -> None:
    """凭据齐备时不看 Origin——本机任意进程都能伪造 Origin，token 才是边界。

    与 HTTP 面同口径（`projects.require_trusted_origin`：`jwt_secret` 已配 ⇒ 直接
    返回、不查 Origin）。这条同时是"过度收紧"的反锚：把 Origin 升成第二道硬门禁
    会让页面 origin 不是 loopback 的桌面形态整体连不上。
    """
    headers = {"Authorization": f"Bearer {_token()}", "Origin": "https://evil.example.com"}
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            client.websocket_connect("/api/ws", headers=headers) as ws:
        assert _ping_pong(ws) == "pong"


# ── 未配置 jwt_secret：本地信任模式 + 来源闸（验收 2 / 3）─────────────


def test_unconfigured_keeps_local_dev_shape(tmp_path: Path) -> None:
    """未配置密钥 = 本地信任模式：无凭据照常可用（开发形态语义不变）。"""
    with TestClient(_app(tmp_path)) as client, \
            client.websocket_connect("/api/ws") as ws:
        assert _ping_pong(ws) == "pong"


def test_unconfigured_trusts_originless_non_browser_client(tmp_path: Path) -> None:
    """无 Origin = 非浏览器发起（CLI / curl / 本机 WS 客户端）⇒ 放行。

    与 Phoenix 的 `is_nil(origin) -> conn` 同一判据：第三方网页**无法**构造不带
    Origin 的浏览器握手，故这条放行不构成 drive-by 面。
    """
    headers = {"User-Agent": "curl/8"}
    with TestClient(_app(tmp_path)) as client, \
            client.websocket_connect("/api/ws", headers=headers) as ws:
        assert _ping_pong(ws) == "pong"


@pytest.mark.parametrize("origin", [
    "https://evil.example.com",
    "http://evil.example.com",
    "null",  # sandboxed iframe / file:// —— hostname 为 None
    "http://127.0.0.1.evil.example.com",  # 后缀伪装
])
def test_unconfigured_refuses_cross_origin_browser_handshake(
    tmp_path: Path, origin: str,
) -> None:
    """跨源网页握手 ⇒ 拒（#890 影响面第 2 条：drive-by）。

    `null` 与后缀伪装两条与 HTTP 面 `require_trusted_origin` 的既有判据同形
    （`urlparse(origin).hostname` 不在 `{localhost,127.0.0.1,::1}` 即拒）。
    """
    with TestClient(_app(tmp_path)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect("/api/ws", headers={"Origin": origin}):
        pass


@pytest.mark.parametrize("origin", [
    "http://localhost:5173",   # Vite dev server
    "http://127.0.0.1:8000",   # 直连后端
    "http://[::1]:8000",       # IPv6 回环
    "http://LOCALHOST:5173",   # 大小写
])
def test_unconfigured_accepts_local_browser_origins(tmp_path: Path, origin: str) -> None:
    """本机来源的浏览器握手照常放行——CLI / Web 开发形态零影响。"""
    with TestClient(_app(tmp_path)) as client, \
            client.websocket_connect("/api/ws", headers={"Origin": origin}) as ws:
        assert _ping_pong(ws) == "pong"


# ── 鉴权发生在业务之前：被拒的连接不得建立任何会话面（验收 1 的读写面）──


async def _serve(app) -> tuple[object, asyncio.Task, int]:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="error", lifespan="on",
    ))
    serve_task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn 未起来"
    return server, serve_task, server.servers[0].sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_unauthorized_handshake_starts_no_session_work(tmp_path: Path) -> None:
    """被拒的握手**没有**进入 `handle_websocket`：没有订阅、没有可驱动的连接。

    这是"不能读到任何事件、不能驱动会话"的结构性证据——不是靠"帧没收到"这种
    行为观察（后者可能被超时掩盖）。用真实 uvicorn + httpx2 客户端
    （与 `tests/web/test_web_ws_relay.py` 同款接缝）。

    同时锚住"拒的是**这一条连接**"：同一服务上带凭据的连接照常 ping/pong，
    `/api/health` 照常 200——实现若把判定写成把进程搞崩，这里会红。
    """
    import httpx2
    from httpx2.websockets import HTTPXWSException

    server, serve_task, port = await _serve(_app(tmp_path, jwt_secret=_SECRET))
    try:
        async with httpx2.AsyncClient(timeout=10) as client:
            # ① 无凭据：握手失败（uvicorn 对握手前的 `websocket.close` 回 403）
            with pytest.raises(HTTPXWSException) as refusal:
                async with client.websocket(f"ws://127.0.0.1:{port}/api/ws"):
                    pass
            response = refusal.value.response
            assert response is not None, "拒必须是握手层的 HTTP 拒绝，不是建立后断开"
            assert response.status_code == 403
            # ② 服务仍活着：拒的是连接，不是把进程搞崩
            assert (await client.get(
                f"http://127.0.0.1:{port}/api/health"
            )).status_code == 200
            # ③ 带凭据：建连成功且既有协议行为不变
            async with client.websocket(
                f"ws://127.0.0.1:{port}/api/ws",
                headers={"Authorization": f"Bearer {_token()}"},
            ) as ws:
                await ws.send_text(json.dumps({"type": "ping"}))
                assert json.loads(await ws.receive_text())["type"] == "pong"
    finally:
        server.should_exit = True
        serve_task.cancel()
        try:
            await serve_task
        except asyncio.CancelledError:
            pass
