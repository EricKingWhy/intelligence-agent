"""#963：测试套件对宿主 HTTP(S)_PROXY 免疫——loopback 请求永不进代理。

现象：宿主设了 `HTTP_PROXY`/`HTTPS_PROXY` 却没有给 loopback 豁免时，httpx/httpx2
默认 `trust_env=True`，会把发往 `127.0.0.1` 的请求送进代理 → 一切走真实本机 HTTP
服务的用例假红（issue #963 点名的 `test_http_transport_round_trip` 等）。

修法（成熟语义，REUSE）：`tests/conftest.py` 的 autouse 夹具
`_seal_loopback_proxy_exemption` 在每个用例把 loopback 追加进 `NO_PROXY`/`no_proxy`，
用例结束后恢复原值。环境变量豁免是 urllib → requests → httpx 一脉的既定语义
（来源：httpx 官方 `docs/environment_variables.md`；httpx2 源码
`httpx2/_utils.py::get_environment_proxies` 同构），不造新机制。

拼写陷阱：`NO_PROXY` 里 loopback 必须写**裸** `::1`，不能写方括号 `[::1]`——
`get_environment_proxies` 对 `[::1]` 走 `all://*[::1]` 分支，`URLPattern` 构造
即炸 `InvalidURL: Invalid port: ':1]'`（构造期炸，不是请求期）。

运行约定：跑本文件前清掉宿主代理变量
（`env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY -u no_proxy`），
让本机 loopback 豁免只由夹具/用例精确控制，不依赖宿主环境。
"""

from __future__ import annotations

import http.server
import os
import threading

import httpx2
import pytest


class _PongHandler(http.server.BaseHTTPRequestHandler):
    """最小本机 HTTP 服务：任何 GET 回 200 + b"pong"。"""

    def do_GET(self) -> None:  # 驼峰方法名由 stdlib BaseHTTPRequestHandler 约定
        body = b"pong"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音 stdlib 访问日志
        return


@pytest.fixture
def loopback_server():
    """真实本机 HTTP 服务（线程，bind 127.0.0.1、port 0），yield 出基址。"""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PongHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_loopback_bypasses_proxy_env(monkeypatch: pytest.MonkeyPatch, loopback_server: str):
    """验收第 1 条：设了不可达假代理时，发往 loopback 的请求必须绕开代理成功。

    无夹具（RED）：宿主 `NO_PROXY` 已被运行命令清空，假代理成为唯一出口，
    `httpx2.Client()` 把请求送进 `127.0.0.1:1` → 连接失败 → 红。
    有夹具（GREEN）：夹具已把 loopback 追加进 `NO_PROXY`，请求直连本机服务 → 绿。
    本用例**只**写 `HTTP(S)_PROXY`，不碰 `NO_PROXY`——豁免由夹具提供。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")

    with httpx2.Client(timeout=5.0, trust_env=True) as client:
        resp = client.get(f"{loopback_server}/")

    assert resp.status_code == 200
    assert resp.content == b"pong"


def test_no_proxy_spelling_guards_ipv6():
    """loopback 豁免用裸 `::1`，绝不用方括号 `[::1]`（构造期炸的已知坑）。

    夹具生效后 `NO_PROXY` 与 `no_proxy` 必须同时含三项 loopback，且不含 `[::1]`。
    这条把 httpx2 `_utils.py` 实证（裸 `::1` → `all://[::1]` 正确豁免；
    `[::1]` → `all://*[::1]` → `InvalidURL`）编码成回归钉。
    """
    for key in ("NO_PROXY", "no_proxy"):
        entries = [part.strip() for part in os.environ[key].split(",")]
        assert "127.0.0.1" in entries, (key, os.environ[key])
        assert "localhost" in entries, (key, os.environ[key])
        assert "::1" in entries, (key, os.environ[key])
        assert "[::1]" not in entries, (key, os.environ[key])


def test_ensure_loopback_exemption_preserves_existing_entries():
    """纯函数：保留既有条目、loopback 三项各恰出现一次（去重不重复追加）。"""
    from tests.conftest import ensure_loopback_proxy_exemption

    env = {"NO_PROXY": "crop.internal"}
    ensure_loopback_proxy_exemption(env)

    entries = env["NO_PROXY"].split(",")
    assert "crop.internal" in entries
    assert entries.count("127.0.0.1") == 1
    assert entries.count("localhost") == 1
    assert entries.count("::1") == 1
    # 另一个大小写键缺失 → 直接设为三项（httpx2 会读大写，但小写也一并兜住）
    assert env["no_proxy"] == "127.0.0.1,localhost,::1"


def test_ensure_loopback_exemption_wildcard_untouched():
    """纯函数：值为 `*`（全豁免）时保持原样，不追加、不改写。"""
    from tests.conftest import ensure_loopback_proxy_exemption

    env = {"NO_PROXY": "*", "no_proxy": "*"}
    ensure_loopback_proxy_exemption(env)
    assert env["NO_PROXY"] == "*"
    assert env["no_proxy"] == "*"


def test_ensure_loopback_exemption_sets_when_empty():
    """纯函数：空值/缺失 → 直接设为三项（固定顺序，便于断言与阅读）。"""
    from tests.conftest import ensure_loopback_proxy_exemption

    env: dict[str, str] = {"NO_PROXY": ""}
    ensure_loopback_proxy_exemption(env)
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert env["no_proxy"] == "127.0.0.1,localhost,::1"
