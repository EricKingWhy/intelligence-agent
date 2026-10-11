"""#963：测试套件对宿主 HTTP(S)_PROXY 免疫——loopback 请求永不进代理。

现象：宿主设了 `HTTP_PROXY`/`HTTPS_PROXY` 却没有给 loopback 豁免时，httpx/httpx2
默认 `trust_env=True`，会把发往 `127.0.0.1` 的请求送进代理 → 一切走真实本机 HTTP
服务的用例假红（issue #963 点名的 `test_http_transport_round_trip` 等）。

修法（成熟语义，REUSE）：`tests/conftest.py` 的 autouse 夹具
`_ensure_loopback_proxy_exemption` 在每个用例把 loopback 补进 `NO_PROXY`/`no_proxy`，
用例结束后恢复原值。豁免是 urllib → requests → httpx 一脉的既定语义（来源：httpx
官方 `docs/environment_variables.md`；httpx2 源码
`httpx2/_utils.py::get_environment_proxies` 同构），不造新机制。

方括号陷阱（返工 F1/F2）：`NO_PROXY` 里 loopback 必须写**裸** `::1`，不能写方括号
`[::1]`——`get_environment_proxies` 对 `[::1]` 走 `all://*[::1]` 分支，`URLPattern`
构造期即炸 `InvalidURL: Invalid port: ':1]'`（构造期，不是请求期）。宿主本机
`NO_PROXY` 预含 `[::1]` 与 `[fd8b:4f84:7d32:99::1]` 等**非 loopback 方括号条目**，
故夹具在补 loopback 前会先剔除/规范化这些方括号条目；本文件用例在**任何**宿主 env
下都成立（不依赖宿主已清代理变量）。

大小写两个键（`NO_PROXY`/`no_proxy`）都写：httpx2 经 `urllib.request.getproxies()`
读代理 env，`getproxies_environment` 对变量名做 case-fold 并按"优先小写"归并——两个
拼写都映射到同一个 `no` 键。不是"urllib 对大写敏感"。

运行约定：跑本文件前清掉宿主代理变量（大小写都清）
（`env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY -u no_proxy
-u http_proxy -u https_proxy`），
让本机 loopback 豁免只由夹具/用例精确控制；返工另用**未清**的宿主真实 env 实证夹具
免疫（F1/F2 证据，见 dispatch/963-run.log）。
"""

from __future__ import annotations

import http.server
import os
import threading

import httpx2
import pytest

from tests.conftest import ensure_loopback_proxy_exemption


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

    本用例**只**写 `HTTP(S)_PROXY`，不碰 `NO_PROXY`——豁免由 autouse 夹具提供。
    ABLATION（停用夹具、宿主 `NO_PROXY` 不含 loopback 时）：假代理成为唯一出口 →
    `httpx2.ConnectError`（连 `127.0.0.1:1` 被拒）→ 红；夹具生效后直连本机服务 → 绿。
    假代理刻意放在 loopback（`127.0.0.1:1`）不构成巧合：NO_PROXY 匹配的是**请求 URL
    的 host**、不是代理 host（`URLPattern.matches(request.url)`），未豁免时请求会连
    代理 `127.0.0.1:1`（端口 1 无人监听）→ 拒绝，与"直连本机服务 → 200"判然可分。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")

    with httpx2.Client(timeout=5.0, trust_env=True) as client:
        resp = client.get(f"{loopback_server}/")

    assert resp.status_code == 200, (resp.status_code, resp.text)
    assert resp.content == b"pong", resp.content


def test_no_proxy_spelling_guards_loopback_hosts(
    _ensure_loopback_proxy_exemption: None,
) -> None:
    """夹具生效后 `NO_PROXY`/`no_proxy` 必须含三项 loopback，且不含任何方括号条目。

    显式声明夹具参数（不隐式依赖 autouse）：本用例直接读 `os.environ`，需要夹具先跑。
    这条把 httpx2 `_utils.py` 实证编码成回归钉（裸 `::1` → `all://[::1]` 正确豁免；
    `[::1]` → `all://*[::1]` → `InvalidURL`）。
    """
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ[key]
        entries = [part.strip() for part in value.split(",")]
        assert "127.0.0.1" in entries, (key, value)
        assert "localhost" in entries, (key, value)
        assert "::1" in entries, (key, value)
        assert "[::1]" not in entries, (key, value)
        assert "[" not in value and "]" not in value, (key, value)


def test_client_construction_survives_host_no_proxy(
    _ensure_loopback_proxy_exemption: None,
) -> None:
    """夹具生效后，httpx2 客户端在宿主真实 `NO_PROXY` 下也能构造（方括号条目已剔除）。

    直击构造期崩溃：宿主 `NO_PROXY` 预含 `[::1]` 时 `httpx2.Client(trust_env=True)`
    抛 `InvalidURL: Invalid port: ':1]'`（`get_environment_proxies` 把 `[::1]` 解析成
    `all://*[::1]`，`URLPattern` 构造即炸）。纯构造、无网络。
    """
    with httpx2.Client(timeout=5.0, trust_env=True):
        pass


def test_ensure_loopback_exemption_preserves_existing_entries() -> None:
    """就地改写：保留既有条目、loopback 三项各恰出现一次（去重不重复追加）。"""
    env = {"NO_PROXY": "crop.internal"}
    ensure_loopback_proxy_exemption(env)

    entries = env["NO_PROXY"].split(",")
    assert "crop.internal" in entries, env["NO_PROXY"]
    assert entries.count("127.0.0.1") == 1, env["NO_PROXY"]
    assert entries.count("localhost") == 1, env["NO_PROXY"]
    assert entries.count("::1") == 1, env["NO_PROXY"]
    # 另一个大小写键缺失 → 直接设为三项（httpx2 会读大写，但小写也一并兜住）
    assert env["no_proxy"] == "127.0.0.1,localhost,::1", env["no_proxy"]


def test_ensure_loopback_exemption_wildcard_untouched() -> None:
    """就地改写：值为 `*`（全豁免）时保持原样，不追加、不改写。"""
    env = {"NO_PROXY": "*", "no_proxy": "*"}
    ensure_loopback_proxy_exemption(env)
    assert env["NO_PROXY"] == "*", env["NO_PROXY"]
    assert env["no_proxy"] == "*", env["no_proxy"]


def test_ensure_loopback_exemption_sets_when_empty() -> None:
    """就地改写：空值/缺失 → 直接设为三项（固定顺序，便于断言与阅读）。"""
    env: dict[str, str] = {"NO_PROXY": ""}
    ensure_loopback_proxy_exemption(env)
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1", env["NO_PROXY"]
    assert env["no_proxy"] == "127.0.0.1,localhost,::1", env["no_proxy"]


def test_ensure_loopback_exemption_normalizes_bracketed_ipv6() -> None:
    """就地改写：方括号 IPv6 条目（`[::1]`、`[fd8b:...]`）被规范化成裸形式。

    覆盖 loopback 与非 loopback 方括号条目；顺带确认条目已 `strip()`（去空白）。
    """
    env = {
        "NO_PROXY": "crop.internal , [::1] ,[fd8b:4f84:7d32:99::1]",
        "no_proxy": "[fd8b:4f84:7d32:99::2]",
    }
    ensure_loopback_proxy_exemption(env)

    for key in ("NO_PROXY", "no_proxy"):
        value = env[key]
        entries = value.split(",")
        assert "[" not in value and "]" not in value, (key, value)
        assert all(entry == entry.strip() for entry in entries), (key, value)

    upper_entries = env["NO_PROXY"].split(",")
    assert "crop.internal" in upper_entries, env["NO_PROXY"]
    assert "::1" in upper_entries, env["NO_PROXY"]
    assert "fd8b:4f84:7d32:99::1" in upper_entries, env["NO_PROXY"]
    assert "127.0.0.1" in upper_entries, env["NO_PROXY"]  # 缺失的 loopback 被补进
    lower_entries = env["no_proxy"].split(",")
    assert "fd8b:4f84:7d32:99::2" in lower_entries, env["no_proxy"]
    assert "localhost" in lower_entries, env["no_proxy"]


def test_ensure_loopback_exemption_dedups_case_insensitively() -> None:
    """就地改写：去重大小写不敏感——httpx2 主机匹配大小写不敏感（URL host 转小写）。"""
    env = {"NO_PROXY": "LOCALHOST,127.0.0.1,::1"}
    ensure_loopback_proxy_exemption(env)

    entries = env["NO_PROXY"].split(",")
    folded = [entry.casefold() for entry in entries]
    assert folded.count("localhost") == 1, env["NO_PROXY"]
    assert "LOCALHOST" in entries, env["NO_PROXY"]  # 保留首次出现的原拼写
