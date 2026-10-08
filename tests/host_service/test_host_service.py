"""W-11（#355）S1：服务端点发现状态文件 + token 限权通道 + 附着核验分类。

探针客户端逻辑用标准库 ``http.server`` 桩验证——本切片只测
``agent_harness.host_service`` 的发现/核验面，真实 FastAPI 服务面在 S2+ 的
serve 子进程测试里闭环（票面验收：真实子进程 + loopback 套接字，非单纯 mock）。
"""

from __future__ import annotations

import hmac
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_harness.host_service import (
    HOST_PROTOCOL_VERSION,
    HOST_SKILLS_NONCE_HEADER,
    HOST_SKILLS_PROOF_HEADER,
    HOST_SKILLS_RESPONSE_PROOF_HEADER,
    AttachStatus,
    HostEndpointInfo,
    HostTokenStore,
    attach_probe,
    clear_endpoint,
    host_skills_request_proof,
    host_skills_response_proof,
    host_token_username,
    publish_endpoint,
    query_attached_skill_catalog,
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

    def __init__(
        self,
        *,
        auth_ok: bool,
        protocol_version: int = HOST_PROTOCOL_VERSION,
        oversized_health: bool = False,
        host_token: str | None = None,
        response_token: str | None = None,
        skill_catalog: dict[str, object] | None = None,
    ) -> None:
        self.protocol_version = protocol_version
        self.auth_headers: list[str | None] = []
        captured_auth_headers = self.auth_headers

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # 静音测试输出
                pass

            def _reply(
                self,
                status: int,
                payload: object,
                extra_headers: dict[str, str] | None = None,
            ) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (extra_headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/api/health":
                    payload: dict[str, object] = {
                        "status": "ok",
                        "protocol_version": handler_protocol[0],
                    }
                    if oversized_health:
                        payload["padding"] = "x" * (8 * 1024 * 1024 + 1)
                    self._reply(200, payload)
                elif self.path.startswith("/api/sessions"):
                    self._reply(200 if auth_ok else 401, [] if auth_ok else {"detail": "Invalid identity token"})
                elif self.path == "/api/skills" and skill_catalog is not None:
                    request_nonce = self.headers.get(HOST_SKILLS_NONCE_HEADER, "")
                    request_proof = self.headers.get(HOST_SKILLS_PROOF_HEADER, "")
                    captured_auth_headers.append(self.headers.get("Authorization"))
                    if (
                        host_token is None
                        or not request_nonce
                        or not hmac.compare_digest(
                            request_proof,
                            host_skills_request_proof(host_token, request_nonce),
                        )
                    ):
                        self._reply(401, {"detail": "Invalid host proof"})
                    else:
                        body = json.dumps(skill_catalog).encode("utf-8")
                        self._reply(
                            200,
                            skill_catalog,
                            {
                                HOST_SKILLS_RESPONSE_PROOF_HEADER: host_skills_response_proof(
                                    response_token or host_token,
                                    request_nonce,
                                    200,
                                    body,
                                )
                            },
                        )
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


def test_attach_probe_rejects_json_response_over_limit(tmp_path: Path) -> None:
    service = _StubService(auth_ok=True, oversized_health=True)
    try:
        root = tmp_path / "ws"
        publish_endpoint(root, HostEndpointInfo(pid=1, port=service.port))

        result = attach_probe(root, credentials=MemoryCredentialStore())

        assert result.status is AttachStatus.STALE
        assert "ValueError" in result.detail
    finally:
        service.stop()


@pytest.mark.parametrize(
    ("client_token", "response_token", "expected_catalog"),
    [
        ("shared-token", None, True),
        ("wrong-token", None, False),
        ("shared-token", "forged-token", False),
    ],
)
def test_live_skill_catalog_uses_challenge_proof_without_bearer_header(
    tmp_path: Path,
    client_token: str,
    response_token: str | None,
    expected_catalog: bool,
) -> None:
    root = tmp_path / "workspace"
    credentials = MemoryCredentialStore()
    HostTokenStore(credentials).set_token(root, client_token)
    catalog = {"skills": [{"name": "sample", "source": "/workspace/skills/sample/SKILL.md"}]}
    service = _StubService(
        auth_ok=True,
        host_token="shared-token",
        response_token=response_token,
        skill_catalog=catalog,
    )
    try:
        publish_endpoint(root, HostEndpointInfo(pid=1, port=service.port))

        result = query_attached_skill_catalog(root, credentials=credentials)

        assert (result == catalog) is expected_catalog
        assert service.auth_headers == [None]
    finally:
        service.stop()


# ── serve_once 优雅停机（审查修复轮 P2-1：此前 finally 清理只被崩溃路径间接覆盖） ──


def test_serve_once_graceful_shutdown_cleans_endpoint_token_lock(tmp_path: Path) -> None:
    """进程内走一次完整「就绪 → 停机」闭环（`stop_when` 接缝），钉住 finally 三件
    清理：端点文件、host token、实例锁。此前没有任何测试覆盖优雅停机路径——
    子进程测试全部以 kill 收尾（非优雅路径），删掉 finally 里的清理依旧全绿。"""
    from agent_harness.config import Settings
    from agent_harness.host_service import serve_once
    from agent_harness.instance_lock import InstanceLock, InstanceLockError

    root = tmp_path / "ws"
    root.mkdir()
    backend = MemoryCredentialStore()
    settings = Settings(
        _env_file=None, workspace_dir=str(root), model_api_key="sk-test"
    )
    stop = threading.Event()
    outcomes: list[object] = []
    errors: list[Exception] = []

    def _run() -> None:
        try:
            outcomes.append(
                serve_once(settings, credentials=backend, stop_when=stop.is_set)
            )
        except Exception as error:  # noqa: BLE001 — 线程边界：失败带回主线程断言，不静默
            errors.append(error)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if errors or outcomes:
                pytest.fail("服务线程在达到 ATTACHABLE 前就结束了（见 errors/outcomes）")
            if not thread.is_alive():
                pytest.fail("服务线程在未产出结论时退出且未留下异常（见 stderr）")
            probe = attach_probe(root, credentials=backend)
            if probe.status is AttachStatus.ATTACHABLE:
                break
            time.sleep(0.1)
        else:
            pytest.fail("60s 内未达到 ATTACHABLE")

        # 就绪面：端点已发布、token 已入限权通道。
        assert read_endpoint(root) is not None
        assert HostTokenStore(backend).get_token(root)

        stop.set()
    finally:
        thread.join(timeout=30)

    assert not errors, f"serve_once 在停机路径上抛错：{errors!r}"
    assert not thread.is_alive(), "stop_when 触发后 30s 未完成停机"
    assert outcomes and outcomes[0].served is True

    # finally 三件清理：端点、token、锁。
    assert read_endpoint(root) is None
    assert HostTokenStore(backend).get_token(root) is None
    try:
        InstanceLock(root).acquire().release()
    except InstanceLockError as error:  # 拿不到锁 = 服务没放锁（M3 逃脱的回归红）
        pytest.fail(f"服务停机后实例锁仍被占用（finally 未放锁）：{error}")
