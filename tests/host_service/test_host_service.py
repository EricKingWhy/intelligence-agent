"""W-11（#355）S1：服务端点发现状态文件 + token 限权通道 + 附着核验分类。

探针客户端逻辑用标准库 ``http.server`` 桩验证——本切片只测
``agent_harness.host_service`` 的发现/核验面，真实 FastAPI 服务面在 S2+ 的
serve 子进程测试里闭环（票面验收：真实子进程 + loopback 套接字，非单纯 mock）。
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_harness.host_service import (
    HOST_PROTOCOL_VERSION,
    AttachStatus,
    HostEndpointInfo,
    HostTokenStore,
    attach_probe,
    clear_endpoint,
    host_token_username,
    publish_endpoint,
    read_endpoint,
)
from agent_harness.model.provider_store import MemoryCredentialStore


def _free_loopback_port() -> int:
    """取一个当前空闲的 loopback 端口并**关闭**——构造「连接被拒」的 STALE 面。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _StubService:
    """最小 HTTP 桩：/api/health 永远 200；/api/sessions 按 `auth_ok` 返回 200/401。"""

    def __init__(self, *, auth_ok: bool, protocol_version: int = HOST_PROTOCOL_VERSION) -> None:
        self.protocol_version = protocol_version

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # 静音测试输出
                pass

            def _reply(self, status: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/api/health":
                    self._reply(200, {"status": "ok", "protocol_version": handler_protocol[0]})
                elif self.path.startswith("/api/sessions"):
                    self._reply(200 if auth_ok else 401, [] if auth_ok else {"detail": "Invalid identity token"})
                else:
                    self._reply(404, {"detail": "not found"})

        handler_protocol = [protocol_version]
        self._handler_protocol = handler_protocol
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def set_protocol_version(self, version: int) -> None:
        self._handler_protocol[0] = version

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture()
def stub_service(request: pytest.FixtureRequest):
    service = _StubService(auth_ok=getattr(request, "param", True))
    yield service
    service.stop()


def _endpoint(port: int) -> HostEndpointInfo:
    return HostEndpointInfo(pid=1234, port=port)


# ── 端点状态文件 ─────────────────────────────────────────────────────────────


def test_endpoint_roundtrip_clear_and_no_secret(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    assert read_endpoint(root) is None  # 从未发布过

    info = HostEndpointInfo(pid=4242, port=51820)
    publish_endpoint(root, info)
    loaded = read_endpoint(root)
    assert loaded is not None
    assert loaded.pid == 4242
    assert loaded.port == 51820
    assert loaded.protocol_version == HOST_PROTOCOL_VERSION
    assert loaded.auth_required is True

    # 协议字段不暴露 secret：状态文件里不得出现 token/secret 类键值（W-11 目标行为）。
    raw = (root / ".host-service.json").read_text(encoding="utf-8").lower()
    assert "token" not in raw
    assert "secret" not in raw

    clear_endpoint(root)
    assert read_endpoint(root) is None


def test_endpoint_read_corrupt_file_is_absent(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".host-service.json").write_text("{not json", encoding="utf-8")
    assert read_endpoint(root) is None


# ── token 限权通道（REUSE Credentials seam，测试注入内存后端） ────────────────


def test_host_token_store_roundtrip_and_namespace(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    store = HostTokenStore(MemoryCredentialStore())

    assert store.get_token(root_a) is None
    store.set_token(root_a, "tok-a")
    assert store.get_token(root_a) == "tok-a"
    assert store.get_token(root_b) is None  # 不同数据根互不可见（username 命名空间隔离）

    assert host_token_username(root_a) != host_token_username(root_b)
    assert host_token_username(root_a).startswith("host-service/")

    store.clear_token(root_a)
    assert store.get_token(root_a) is None
    store.clear_token(root_a)  # 幂等


def test_host_token_username_stable_across_path_spellings(tmp_path: Path) -> None:
    a = tmp_path / "ws"
    b = tmp_path / "." / "ws"  # 同一目录的不同拼写 → 同一 username（realpath 归一）
    assert host_token_username(a) == host_token_username(b)


# ── attach_probe 四态分类 ────────────────────────────────────────────────────


def test_attach_probe_absent(tmp_path: Path) -> None:
    result = attach_probe(tmp_path / "ws", credentials=MemoryCredentialStore())
    assert result.status is AttachStatus.ABSENT
    assert result.endpoint is None


def test_attach_probe_stale_when_nothing_listens(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    port = _free_loopback_port()
    publish_endpoint(root, HostEndpointInfo(pid=1, port=port))
    result = attach_probe(root, credentials=MemoryCredentialStore())
    assert result.status is AttachStatus.STALE


def test_attach_probe_stale_on_protocol_mismatch(tmp_path: Path, stub_service: _StubService) -> None:
    root = tmp_path / "ws"
    stub_service.set_protocol_version(HOST_PROTOCOL_VERSION + 1)
    publish_endpoint(root, HostEndpointInfo(pid=1, port=stub_service.port))
    result = attach_probe(root, credentials=MemoryCredentialStore())
    assert result.status is AttachStatus.STALE


def test_attach_probe_auth_failed(tmp_path: Path) -> None:
    service = _StubService(auth_ok=False)
    try:
        root = tmp_path / "ws"
        publish_endpoint(root, HostEndpointInfo(pid=1, port=service.port))
        # set 与 probe 必须共享同一后端实例（生产里两者走同一系统凭据管理器）。
        backend = MemoryCredentialStore()
        HostTokenStore(backend).set_token(root, "stale-token")
        result = attach_probe(root, credentials=backend)
        assert result.status is AttachStatus.AUTH_FAILED
        assert "401" in result.detail
    finally:
        service.stop()


def test_attach_probe_attachable(tmp_path: Path, stub_service: _StubService) -> None:
    root = tmp_path / "ws"
    publish_endpoint(root, HostEndpointInfo(pid=1, port=stub_service.port))
    backend = MemoryCredentialStore()
    HostTokenStore(backend).set_token(root, "tok")
    result = attach_probe(root, credentials=backend)
    assert result.status is AttachStatus.ATTACHABLE
    assert result.endpoint is not None
    assert result.endpoint.port == stub_service.port


def test_attach_probe_auth_required_without_token_is_auth_failed(
    tmp_path: Path, stub_service: _StubService
) -> None:
    # 服务活着但本机通道里没有 token（如另一 OS 用户启动了服务）——不得把它当
    # STALE 去冷启动强抢（锁在活进程手里），必须 AUTH_FAILED。
    root = tmp_path / "ws"
    publish_endpoint(root, HostEndpointInfo(pid=1, port=stub_service.port))
    result = attach_probe(root, credentials=MemoryCredentialStore())
    assert result.status is AttachStatus.AUTH_FAILED
