"""#890 WS 鉴权接缝：`/api/ws` 与 HTTP 面同一口径。

三条契约（票面验收 1–3 逐条对应）：

1. **配置 `jwt_secret` = fail-closed**：无凭据 / 坏凭据 / 过期凭据的握手一律在
   `websocket.accept()` **之前**被拒；凭据齐备才建连。凭据有**两条等价来源**：
   `Authorization: Bearer`（桌面外壳 loopback 代理注入的那条）与
   `Sec-WebSocket-Protocol` 子协议（浏览器唯一能用的那条，k8s 同款形状）——
   任一通过即放行。
2. **未配置 `jwt_secret` = 本地信任模式 + 来源闸**：无凭据照常可用，但带 `Origin`
   且非本机 hostname 的握手被拒——这正是浏览器 drive-by 的形状（WS 握手不受 CORS
   约束，服务端不判 Origin 就等于允许任意网页连上来读写会话）。
   ⚠ **这条相对改动前是行为变更**（跨源握手由放行改为拒绝），不是"零影响"：
   受影响的是从 `file://` / sandboxed iframe（`Origin: null`）打开本服务的旧用法。
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
- Kubernetes `714f97d7baf4975ad3aa47735a868a81a984d1f0`
  `staging/.../authentication/request/websocket/protocol.go`：子协议
  `base64url.bearer.authorization.k8s.io.<base64url 无 padding 的 token>` 承载 Bearer，
  校验成功后**剥离该子协议不回显**（防泄漏）。

判别力说明（为什么这些用例能区分"拒"与"放行"）：本文件共 **35 例**，走**两条不同的
测试接缝**，断言强度不同，别混为一谈。

**接缝一：真实 uvicorn + httpx2**（另外 2 例，即文件末尾那两个 `async` 用例）。被拒的握手由 uvicorn 回 HTTP 403，
客户端在 `client.websocket(...)` 的 `__enter__` 抛 `HTTPXWSException`，其 `.response`
带 `status_code == 403`——**只有这两例能断言状态码**。它们覆盖的正是"拒发生在协议层、
连会话面都没进"这个结构性事实（订阅 / 写入都到不了业务侧，且同一服务上带凭据的连接
照常工作——拒的是连接，不是把进程搞崩）。

**接缝二：starlette `TestClient`**（其余 **33 例**，含 parametrize 展开）。`__enter__`
收到 accept 之前的 close 就抛 `WebSocketDisconnect`，**拿不到 status_code**（它只在
`websocket.http.response.start` 那条路上才带状态码，见 `starlette/testclient.py` 的
`_raise_on_close`）。这里锚的是"升级阶段就失败"这个事实本身——**先 accept 再关**的实现
不会在 `__enter__` 抛（那时拿到的是已建立的连接），所以两种实现仍能被区分。

接缝二的 33 例不是一个模子：**拒**侧（凭据来源 × 有无 Origin × 本机/跨源）锚的是判据矩阵，
**放行**侧锚 over-fix（"没被过度收紧"）的共 **10 个用例函数**——其中 8 个走 `_ping_pong`
（证明放行的是既有 `handle_websocket` 协议行为，连同 `test_unconfigured_accepts_local_browser_origins`
的 4 项 parametrize 展开共 11 例）、2 个断言 `ws.accepted_subprotocol`（证明协商值）。
它与"拒"侧同等重要，别被"判据矩阵"四个字盖过去。
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_harness.config import Settings
from agent_harness.web.app import (
    WS_BEARER_SUBPROTOCOL_PREFIX,
    WS_BUSINESS_SUBPROTOCOL,
    create_app,
)

_SECRET = "ws-auth-test-signing-secret-at-least-32-chars"


def _token(secret: str = _SECRET, *, expires_in: int | None = 600) -> str:
    payload: dict[str, object] = {
        "tenant_id": "acme", "user_id": "alice", "scopes": ["user", "session"],
    }
    if expires_in is not None:
        payload["exp"] = int((datetime.now(UTC) + timedelta(seconds=expires_in)).timestamp())
    return jwt.encode(payload, secret, algorithm="HS256")


def _bearer_subprotocol(token: str) -> str:
    """按 k8s 同款形状把 token 编成子协议值（`base64url` 无 padding）。"""
    encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
    return f"{WS_BEARER_SUBPROTOCOL_PREFIX}{encoded}"


def _app(tmp_path: Path, *, jwt_secret: str | None = None):
    return create_app(Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        jwt_secret=jwt_secret, enable_cors=False,
    ))


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


# ids 固定：token 的 `exp` 取自挂钟（秒级），交给 pytest 自动生成 id 会让 id 随收集时刻
# 变化——xdist 各 worker 在不同秒收集时 id 不一致，`-n 4` 报 "Different tests were collected"
# （#931）。值本身仍按真实时间构造，只把标签钉死。
@pytest.mark.parametrize("authorization", [
    "Bearer not-a-jwt",
    f"Bearer {_token('another-signing-secret-32-chars-long!!')}",  # 签名不对
    f"Bearer {_token(expires_in=-60)}",                            # 已过期
    f"Bearer {_token(expires_in=None)}",                           # 无 exp（强制过期语义）
    f"Bearer {_token()}extra",                                     # 形状不合
], ids=["not-a-jwt", "wrong-signature", "expired", "no-exp", "trailing-garbage"])
def test_refuses_bad_bearer_when_secret_configured(tmp_path: Path, authorization: str) -> None:
    """坏 token 与漏 token 同一结果：拒绝。这 5 项对应 HTTP 面既有的坏凭据形状
    （非 JWT / 签名不对 / 已过期 / 无 exp / 形状不合）；**非 Bearer scheme**
    另见 `test_refuses_bearer_that_is_not_bearer_scheme`。"""
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect("/api/ws", headers={"Authorization": authorization}):
        pass


@pytest.mark.parametrize("authorization", [
    "Basic YWxpY2U6cHc=",  # 非 Bearer scheme
    _token(),              # 裸 token（漏了 scheme）
    "Bearer",              # 只有 scheme
], ids=["basic-scheme", "bare-token-no-scheme", "scheme-only"])
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


# ── 未配置 jwt_secret：本地信任模式 + 来源闸（验收 3）──────────────────
# ⚠ 相对改动前是**行为变更**：跨源浏览器握手由放行改为拒绝（受影响旧用法 =
#   从 `file://` / sandboxed iframe 打开本服务）。本地信任模式本身仍可用。


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
    """本机来源的浏览器握手照常放行；无凭据的本地开发形态仍可用。

    ⚠ **不是"零影响"**：跨源握手这一侧确实由放行改成了拒绝（见上方分节标题），
    受影响的是从 `file://` / sandboxed iframe 打开本服务的旧用法。
    """
    with TestClient(_app(tmp_path)) as client, \
            client.websocket_connect("/api/ws", headers={"Origin": origin}) as ws:
        assert _ping_pong(ws) == "pong"


def test_unconfigured_ignores_malformed_origin_instead_of_500(tmp_path: Path) -> None:
    """畸形 `Origin`（失配方括号）判**拒**，不是 500（#890 P3-5）。

    `urlsplit("http://[::1")` 抛 `ValueError`；不接住的话它会冒泡到 starlette 的
    `ServerErrorMiddleware`，把一次设计的 403 变成 500——判据方向没被绕过（仍
    fail-closed），但错误分类与日志被污染，且给了任意客户端一个选失败模式的手段。
    """
    with TestClient(_app(tmp_path)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect("/api/ws", headers={"Origin": "http://[::1"}):
        pass


# ── 浏览器凭据通道：Sec-WebSocket-Protocol 子协议（#890 P1-1）──────────
# 浏览器 `WebSocket` 构造器不能设 `Authorization` 头，子协议是它唯一的凭据通道。
# 这组用例锚的正是"反代 + JWT"这个官方认可形态下浏览器能不能连上。


def test_accepts_bearer_via_subprotocol_when_secret_configured(tmp_path: Path) -> None:
    """配了密钥 + 只带子协议凭据（无 `Authorization` 头）⇒ 放行且能驱动会话。

    这是 P1-1 的正面锚：仓内真实浏览器客户端只有这条路，用例若只走 `Authorization`
    头就覆盖不到它（P1-1 正是藏在"用头注入凭据"的形态背后）。
    `handle_websocket` 的 ping/pong 证明放行的是既有协议行为。
    """
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            client.websocket_connect(
                "/api/ws", subprotocols=[
                    WS_BUSINESS_SUBPROTOCOL, _bearer_subprotocol(_token()),
                ]) as ws:
        assert _ping_pong(ws) == "pong"


def test_subprotocol_credential_is_not_echoed(tmp_path: Path) -> None:
    """承载 token 的子协议**不回显**（防泄漏，k8s 同款），业务子协议照常协商。"""
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            client.websocket_connect(
                "/api/ws", subprotocols=[
                    WS_BUSINESS_SUBPROTOCOL, _bearer_subprotocol(_token()),
                ]) as ws:
        assert ws.accepted_subprotocol == WS_BUSINESS_SUBPROTOCOL


def test_refuses_unknown_subprotocol_without_credential(tmp_path: Path) -> None:
    """客户端自带一个第三方子协议、**无凭据** ⇒ 仍被拒（白名单化没把闸门一起放松）。

    ⚠ **本用例对白名单零判别力**，别把它读成回显面的锚：无凭据在第一层就被
    `_authenticate_websocket` 拒掉（`accept()` 之前），协商根本没跑到，回显值读不到。
    它的价值只有一个——钉住"改了回显策略 ≠ 放松了认证"，新旧实现上它都绿。
    量回显的真读数在 `test_unknown_subprotocol_is_not_echoed_even_when_credential_is_valid`。
    """
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect(
                "/api/ws", subprotocols=["other.product.v9"]):
        pass


def test_unknown_subprotocol_is_not_echoed_even_when_credential_is_valid(
    tmp_path: Path,
) -> None:
    """凭据有效但客户端只带第三方子协议 ⇒ 放行，且回显值**不是**它（白名单的真读数）。

    上一条在拒绝路径上量不到回显（`__enter__` 已抛），这条走放行路径才拿得到
    `accepted_subprotocol`：`other.product.v9` 从未被请求回显 ⇒ 它是 `None`。
    原实现会在这里回显 `other.product.v9`。
    """
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            client.websocket_connect(
                "/api/ws",
                headers={"Authorization": f"Bearer {_token()}"},
                subprotocols=["other.product.v9"],
            ) as ws:
        assert ws.accepted_subprotocol is None


def test_refuses_browser_shaped_client_without_credential(tmp_path: Path) -> None:
    """浏览器形客户端**完全不带**凭据（裸 `new WebSocket(url)`）⇒ 被拒。

    P1-1 点名要补的鉴别力缺口：AC2 原来只知道"塞了 `Authorization` 头的客户端能过 /
    没头就拒"，而浏览器**根本设不了那个头**——它发的就是这条形状。这里刻意带上
    真实浏览器的全套头（`Origin` + `User-Agent` + `Upgrade`），锚一个反直觉的推论：
    **`Origin` 不能救一条无凭据的握手**（配了密钥时 Origin 不参与判定，与
    `test_valid_bearer_does_not_make_origin_a_gate` 是同一条策略的两面）。
    """
    headers = {
        "Origin": "http://localhost:5173",
        "User-Agent": "Mozilla/5.0",
        "Upgrade": "websocket",
    }
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect("/api/ws", headers=headers):
        pass


def test_refuses_browser_shaped_client_with_only_business_subprotocol(tmp_path: Path) -> None:
    """浏览器形客户端只带业务子协议（有子协议、没 token）⇒ 被拒。

    这条是仓内前端真实发出的形状（`wsSubprotocols()` 无 token 时正好只发业务子协议），
    与上一条（完全不发子协议）是两个不同的请求形状，各锚一半。
    """
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect(
                "/api/ws", subprotocols=[WS_BUSINESS_SUBPROTOCOL]):
        pass


@pytest.mark.parametrize("token", [
    "not-a-jwt",
    jwt.encode({"tenant_id": "acme", "user_id": "alice",
                "exp": int((datetime.now(UTC) + timedelta(seconds=600)).timestamp())},
               "another-signing-secret-32-chars-long!!", algorithm="HS256"),
], ids=["not-a-jwt", "wrong-signature"])
def test_refuses_bad_token_via_subprotocol(tmp_path: Path, token: str) -> None:
    """子协议里的坏 token（非 JWT / 签名不对）⇒ 被拒——与头通道同一出口。"""
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect(
                "/api/ws", subprotocols=[
                    WS_BUSINESS_SUBPROTOCOL, _bearer_subprotocol(token),
                ]):
        pass


def test_subprotocol_prefix_without_token_is_refused(tmp_path: Path) -> None:
    """只有前缀、没有编码体 ⇒ 当作"没给凭据" ⇒ 拒（不静默降级成放行）。"""
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            pytest.raises(WebSocketDisconnect), \
            client.websocket_connect(
                "/api/ws", subprotocols=[
                    WS_BUSINESS_SUBPROTOCOL, WS_BEARER_SUBPROTOCOL_PREFIX,
                ]):
        pass


def test_bad_header_does_not_shadow_valid_subprotocol_credential(tmp_path: Path) -> None:
    """两个来源**等价**：头坏了不该把子协议里的好凭据挤掉（任一通过即放行）。

    桌面外壳代理注入头、浏览器只能发子协议；两种部署各用一条。这条锚住"任一条
    有效就放行"，避免实现被写成"先看头、头不合法就拒"。
    """
    with TestClient(_app(tmp_path, jwt_secret=_SECRET)) as client, \
            client.websocket_connect(
                "/api/ws",
                headers={"Authorization": "Bearer not-a-jwt"},
                subprotocols=[WS_BUSINESS_SUBPROTOCOL, _bearer_subprotocol(_token())],
            ) as ws:
        assert _ping_pong(ws) == "pong"


def test_unconfigured_local_shape_works_with_subprotocols(tmp_path: Path) -> None:
    """未配置密钥时，浏览器形客户端（只发业务子协议）照常连上。

    ⚠ 只说**子协议这条通道**不构成影响：该形态下后端不看凭据。**跨源握手那一侧仍是
    行为变更**（由放行改为拒绝），见上方分节标题——两条别混。
    """
    with TestClient(_app(tmp_path)) as client, \
            client.websocket_connect(
                "/api/ws", subprotocols=[WS_BUSINESS_SUBPROTOCOL]) as ws:
        assert _ping_pong(ws) == "pong"


# ── 鉴权发生在业务之前：被拒的连接不得建立任何会话面（验收 1 的读写面）──


async def _start_server(tmp_path: Path, *, jwt_secret: str | None = None):
    """启动真实 uvicorn，返回 `(server, serve_task, port)`。

    与 `tests/web/test_web_ws_relay.py:86` 同款接缝（那一个的签名多了 `monkeypatch`
    与模型工厂 `model_factory`，本文件只注入 JWT 设置、不需要造模型，**实现不复用**）；
    **`_shutdown` 直接复用** `tests/web/test_web_ws_relay.py:118` 那一份，见两处
    async 用例的 `from tests.web.test_web_ws_relay import _shutdown`。
    """
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(
        _app(tmp_path, jwt_secret=jwt_secret),
        host="127.0.0.1", port=0, log_level="error", lifespan="on",
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

    from tests.web.test_web_ws_relay import _shutdown

    server, serve_task, port = await _start_server(tmp_path, jwt_secret=_SECRET)
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
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_unauthorized_handshake_cannot_read_nor_drive_a_real_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收 1 的两条**可观测**面：被拒的握手读不到事件、也驱不动会话。

    上一条锚的是"没进 `handle_websocket`"这个**结构性**事实；这一条锚的是票面
    验收 1 的原话（"不能读到任何事件、不能驱动会话"）——用真实会话做对照：

      · 读：先建一个真会话（走 HTTP，带 Bearer），拿到它的 `session_id`；
        无凭据的 WS 无法 `subscribe`（握手就被拒）⇒ 事件一条也拿不到。
      · 写：无凭据的 WS 无法 `send_message` ⇒ 事件条数**一条不增**
        （真正的判别点是"没变"，不是"没收到回执"——静默丢弃也会让回执缺席）。

    对照侧同一条连接形态带凭据跑一遍：`subscribe` 拿到含真实事件的快照、
    `send_message` 起新 run ⇒ 证明上面的"拿不到"是鉴权造成的，不是端点坏了。
    """
    import httpx2
    from httpx2.websockets import HTTPXWSException

    from tests.web.test_web_ws_relay import _OneTurnModel, _recv_until, _shutdown

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: _OneTurnModel(),
    )
    server, serve_task, port = await _start_server(tmp_path, jwt_secret=_SECRET)
    auth = {"Authorization": f"Bearer {_token()}"}
    base = f"http://127.0.0.1:{port}"
    ws_url = f"ws://127.0.0.1:{port}/api/ws"

    try:
        async with httpx2.AsyncClient(timeout=30) as client:
            # ① 造一个真会话（HTTP 面凭据齐全）：读到 run/completed 收流。
            frames: list[dict] = []
            async with client.stream(
                "POST", f"{base}/api/sessions", json={"task": "建会话"}, headers=auth,
            ) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        frame = json.loads(line.removeprefix("data:").strip())
                        frames.append(frame)
                        if frame.get("type") == "run/completed":
                            break
            session_id = frames[0]["session_id"]

            async def event_count() -> int:
                got = await client.get(f"{base}/api/sessions/{session_id}/events", headers=auth)
                assert got.status_code == 200
                return len(got.json())

            before = await event_count()
            assert before > 0, "对照会话必须真有事件，否则下面的『一条不增』没有判别力"

            # ② 无凭据的 WS：握手就被拒 ⇒ 订阅与写入都到不了业务面。
            with pytest.raises(HTTPXWSException):
                async with client.websocket(ws_url) as ws:
                    await ws.send_text(json.dumps({
                        "type": "subscribe", "session_id": session_id,
                    }))
                    await ws.send_text(json.dumps({
                        "type": "send_message", "session_id": session_id,
                        "content": "无凭据也想驱动会话",
                    }))

            assert await event_count() == before, "被拒的连接不得驱动会话（事件一条都不该增）"

            # ③ 同一形态带凭据：读得到（快照含真实事件）、写得动（新 run 起得来）。
            async with client.websocket(ws_url, headers=auth) as ws:
                await ws.send_text(json.dumps({
                    "type": "subscribe", "session_id": session_id,
                }))
                frames = await _recv_until(ws, lambda fs: any(
                    f.get("type") == "snapshot" for f in fs))
                snapshot = next(f for f in frames if f.get("type") == "snapshot")
                assert snapshot["events"], "带凭据的订阅必须拿到事件（读面通）"

                await ws.send_text(json.dumps({
                    "type": "send_message", "session_id": session_id,
                    "content": "带凭据可以驱动",
                }))
                frames = await _recv_until(ws, lambda fs: any(
                    f.get("type") == "launched" for f in fs))
                assert any(f.get("type") == "launched" for f in frames), \
                    "带凭据的写入必须真的起 run（写面通）"

            assert await event_count() > before, "对照：带凭据的写入确实增了事件"
    finally:
        await _shutdown(server, serve_task)
