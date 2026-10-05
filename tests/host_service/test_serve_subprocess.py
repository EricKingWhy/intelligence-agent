"""W-11（#355）S2–S4：`serve` 冷启动闭环 / 并发竞态 / STALE 恢复 / CLI 旧锁报错。

票面验收要求**真实子进程 + loopback 套接字**：服务子进程跑真实
`agent-harness serve`（uvicorn + FastAPI + lifespan），父子进程通过
`AGENT_HARNESS_HOST_CREDENTIALS=file:<path>` 测试接缝共享凭据后端，
端点发现走数据根里的 `.host-service.json`，全部 HTTP 走 127.0.0.1。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_harness.host_service import (
    AttachStatus,
    FileCredentialStore,
    HostTokenStore,
    attach_probe,
    read_endpoint,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STARTUP_TIMEOUT = 90.0
_POLL_INTERVAL = 0.1


def _serve_env(root: Path, tokfile: Path, **overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "WORKSPACE_DIR": str(root),
            "AGENT_HARNESS_HOST_CREDENTIALS": f"file:{tokfile}",
            "AGENT_HARNESS_ATTACH_WAIT_SECONDS": "15",
            "JWT_SECRET": "",  # 显式空 = 覆盖 .env，让 serve 走「生成随机密钥」路径
            # 同法中和 artifact store 配置面：Settings 的 env_file 锚在 _REPO_ROOT/.env，
            # 真实部署的 .env 若配了 ARTIFACT_STORE_* / MINIO_*，artifact_select 的
            # _s3_configured / _minio_configured 是 any() 判据，任一非空即选中远端
            # store ⇒ serve 读不到测试写进本地 artifact_dir 的 fixture ⇒ 404。
            # 显式空串让两条 any() 全 False ⇒ 落回与 save 侧同域的本地 store。
            "ARTIFACT_STORE_ENDPOINT": "",
            "ARTIFACT_STORE_BUCKET": "",
            "ARTIFACT_STORE_ACCESS_KEY": "",
            "ARTIFACT_STORE_SECRET_KEY": "",
            "ARTIFACT_STORE_REGION": "",
            "MINIO_ENDPOINT": "",
            "MINIO_BUCKET": "",
            "MINIO_ACCESS_KEY": "",
            "MINIO_SECRET_KEY": "",
            "PYTHONUTF8": "1",
        }
    )
    env.update(overrides)
    return env


def _spawn_serve(root: Path, tokfile: Path, **overrides: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "agent_harness.cli", "serve"],
        cwd=_REPO_ROOT,
        env=_serve_env(root, tokfile, **overrides),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _spawn_lock_holder(root: Path) -> subprocess.Popen[bytes]:
    """占锁但不发布端点的进程 = 在途 CLI 写者（无端点可附着）。"""
    script = (
        "import sys, time\n"
        "from agent_harness.instance_lock import InstanceLock\n"
        f"InstanceLock({str(root)!r}).acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(60)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


#: 与被测探针同一姿势：测试 HTTP 一律显式绕过系统代理（只打 loopback）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """树杀：Windows 上 venv 启动器有进程代际，Popen.kill 只杀壳不杀真服务。"""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    else:
        proc.kill()
    proc.wait(timeout=15)


def _http_get(
    url: str, *, token: str | None = None
) -> tuple[int, object]:
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with _OPENER.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


def _wait_endpoint(root: Path, timeout: float = _STARTUP_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        endpoint = read_endpoint(root)
        if endpoint is not None:
            return endpoint
        time.sleep(_POLL_INTERVAL)
    pytest.fail(f"{timeout:.0f}s 内未等到端点状态文件")


def _wait_attachable(root: Path, tokfile: Path, timeout: float = _STARTUP_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = attach_probe(root, credentials=FileCredentialStore(tokfile))
        if result.status is AttachStatus.ATTACHABLE:
            return result
        time.sleep(_POLL_INTERVAL)
    pytest.fail(f"{timeout:.0f}s 内服务未达到 ATTACHABLE")


def _wait_predicate(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_POLL_INTERVAL)
    pytest.fail(message)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


# ── S2：serve 子进程发布可附着端点 ───────────────────────────────────────────


def test_serve_publishes_attachable_endpoint(workspace: Path, tmp_path: Path) -> None:
    tokfile = tmp_path / "tokens.json"
    proc = _spawn_serve(workspace, tokfile)
    backend = FileCredentialStore(tokfile)
    try:
        endpoint = _wait_endpoint(workspace)
        # pid 仅供诊断（venv 启动器有进程代际），身份实证 = 新铸 token + 健康面 + 鉴权面。
        assert endpoint.pid > 0
        assert endpoint.auth_required is True
        assert endpoint.port > 0

        result = _wait_attachable(workspace, tokfile)
        assert result.endpoint is not None and result.endpoint.port == endpoint.port

        # 健康面：匿名可达、统一形状（health/version/capability，W-11 工作指令 3）、
        # 不含秘密字段。
        status, payload = _http_get(f"http://127.0.0.1:{endpoint.port}/api/health")
        assert status == 200
        assert payload["status"] == "ok"
        assert payload["protocol_version"] == 1
        assert payload["version"]
        assert isinstance(payload["capabilities"], list)
        assert payload["auth_required"] is True
        payload_text = json.dumps(payload).lower()
        assert "token" not in payload_text and "secret" not in payload_text

        # 已认证面：限权通道里的 token 可用；错 token 401；无 token 401。
        token = HostTokenStore(backend).get_token(workspace)
        assert token
        status, _ = _http_get(
            f"http://127.0.0.1:{endpoint.port}/api/sessions?limit=1", token=token
        )
        assert status == 200
        status, _ = _http_get(
            f"http://127.0.0.1:{endpoint.port}/api/sessions?limit=1", token="wrong-token"
        )
        assert status == 401
        status, _ = _http_get(f"http://127.0.0.1:{endpoint.port}/api/sessions?limit=1")
        assert status == 401
    finally:
        _terminate(proc)

    # 崩溃（kill）后端点残留 → 探针 STALE：下一次冷启动据此证明失效并竞争锁。
    assert read_endpoint(workspace) is not None
    assert attach_probe(workspace, credentials=backend).status is AttachStatus.STALE


# ── S3：并发冷启动只产出一个服务；STALE 恢复；活写者拒绝强抢 ─────────────────


def test_concurrent_cold_start_yields_single_service(workspace: Path, tmp_path: Path) -> None:
    tokfile = tmp_path / "tokens.json"
    first = _spawn_serve(workspace, tokfile)
    second = _spawn_serve(workspace, tokfile)
    try:
        _wait_endpoint(workspace)
        # 端点出现 ≠ 输家已退——它的二次检查还要轮询到赢家端点。等「恰好一个退出」。
        _wait_predicate(
            lambda: (first.poll() is None) != (second.poll() is None),
            timeout=60,
            message="并发冷启动未在窗口内收敛到恰好一个存活",
        )
        winner_alive = [p for p in (first, second) if p.poll() is None]
        assert len(winner_alive) == 1, (
            f"并发冷启动必须恰好一个存活：first={first.poll()} second={second.poll()}"
        )
        winner = winner_alive[0]

        # 输家走「竞争失败 → 二次检查发现赢家 → 附着成功退出 0」，不是报错堆栈。
        loser = second if winner is first else first
        _wait_predicate(
            lambda: loser.poll() is not None,
            timeout=60,
            message="竞态输家未在窗口内完成二次检查退出",
        )
        assert loser.returncode == 0

        assert _wait_attachable(workspace, tokfile).endpoint is not None
    finally:
        for proc in (first, second):
            _terminate(proc)


def test_stale_endpoint_then_cold_start_recovers(workspace: Path, tmp_path: Path) -> None:
    tokfile = tmp_path / "tokens.json"
    first = _spawn_serve(workspace, tokfile)
    try:
        first_endpoint = _wait_attachable(workspace, tokfile).endpoint
        assert first_endpoint is not None
    finally:
        _terminate(first)

    # 残留端点 + 新冷启动成功 = 「进程死但残留先证明失效，再竞争锁」。
    assert read_endpoint(workspace) is not None
    second = _spawn_serve(workspace, tokfile)
    try:
        endpoint = _wait_attachable(workspace, tokfile).endpoint
        # 新实例身份：service_uuid 已轮换（服务实例换代），端点端口可用。
        assert endpoint is not None
        assert endpoint.service_uuid != first_endpoint.service_uuid
    finally:
        _terminate(second)


def test_lock_held_by_live_writer_refuses_cold_start(workspace: Path, tmp_path: Path) -> None:
    holder = _spawn_lock_holder(workspace)
    try:
        holder.stdout.readline()  # 等占用者确认已持锁
        serve = _spawn_serve(workspace, tmp_path / "tokens.json", AGENT_HARNESS_ATTACH_WAIT_SECONDS="3")
        try:
            # 锁在活进程手里且无端点可附着 → 二次检查超时 → 响亮拒绝（exit 2），不强抢。
            assert serve.wait(timeout=60) == 2
            assert read_endpoint(workspace) is None
        finally:
            _terminate(serve)
    finally:
        _terminate(holder)


# ── S4：CLI 旧锁报错必须附着感知，不得诱发第二个实例 ─────────────────────────


def test_cli_lock_error_is_attach_aware(workspace: Path, tmp_path: Path) -> None:
    tokfile = tmp_path / "tokens.json"
    serve = _spawn_serve(workspace, tokfile)
    try:
        _wait_attachable(workspace, tokfile)
        result = subprocess.run(
            [sys.executable, "-m", "agent_harness.cli", "sessions"],
            cwd=_REPO_ROOT,
            env=_serve_env(workspace, tokfile),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        assert result.returncode == 2
        combined = result.stdout + result.stderr
        assert "127.0.0.1" in combined
        assert "已有本机服务" in combined
    finally:
        _terminate(serve)


# ── ensure_service 编排（桌面/TUI 冷启动入口消费的原语） ─────────────────────


def test_ensure_service_cold_start_and_attach(workspace: Path, tmp_path: Path) -> None:
    from agent_harness.host_service import ensure_service

    tokfile = tmp_path / "tokens.json"
    spawns: list[subprocess.Popen[bytes]] = []

    def _counting_spawn() -> subprocess.Popen[bytes]:
        proc = _spawn_serve(workspace, tokfile)
        spawns.append(proc)
        return proc

    endpoint = ensure_service(
        workspace,
        credentials=FileCredentialStore(tokfile),
        spawn=_counting_spawn,
        timeout=_STARTUP_TIMEOUT,
    )
    assert endpoint is not None and endpoint.port > 0
    # 已有服务时再次调用 = 只附着，不再冷启动（spawn 计数不变）。
    endpoint_again = ensure_service(
        workspace,
        credentials=FileCredentialStore(tokfile),
        spawn=_counting_spawn,
        timeout=_STARTUP_TIMEOUT,
    )
    assert endpoint_again is not None and endpoint_again.port == endpoint.port
    assert len(spawns) == 1
    _terminate(spawns[0])


# ── S5：默认只绑 loopback——LAN 地址连不上 ───────────────────────────────────


def _non_loopback_ipv4() -> list[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return sorted(
        {
            info[4][0]
            for info in infos
            if not info[4][0].startswith("127.")
        }
    )


def test_serve_binds_loopback_only(workspace: Path, tmp_path: Path) -> None:
    lan_addresses = _non_loopback_ipv4()
    if not lan_addresses:
        pytest.skip("本机无非回环 IPv4 地址，LAN 隔离无法实测")
    tokfile = tmp_path / "tokens.json"
    proc = _spawn_serve(workspace, tokfile)
    try:
        endpoint = _wait_attachable(workspace, tokfile).endpoint
        assert endpoint is not None
        # 服务绑定 127.0.0.1：从任意非回环本机地址连同一端口必须失败——
        # 个人桌面服务不暴露 LAN（W-11 目标行为）。
        for address in lan_addresses:
            with pytest.raises(OSError):
                socket.create_connection((address, endpoint.port), timeout=2).close()
    finally:
        _terminate(proc)


# ── S6：旧数据原位复用——无迁移、无删除（#303 禁删项） ────────────────────────


def _snapshot_files(root: Path) -> set[str]:
    return {
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    }


def test_serve_reuses_old_data_root_in_place(workspace: Path, tmp_path: Path) -> None:
    import asyncio
    from datetime import UTC, datetime

    import jwt as pyjwt
    from fastapi.testclient import TestClient

    from agent_harness.config import Settings
    from agent_harness.model.provider_store import MemoryCredentialStore, ProviderStore
    from agent_harness.storage.local_artifact import LocalArtifactStore
    from agent_harness.web.app import create_app

    # 供应商与 artifact 的存储位都显式指进 tmp（provider_store_path 默认锚用户
    # 主目录、artifact_dir 默认相对 CWD——都不能让测试污染真实用户目录）。
    provfile = tmp_path / "model-providers.json"
    artdir = tmp_path / "artifacts"
    secret = "test-signing-secret-at-least-32-characters"
    pre_settings = Settings(
        _env_file=None,
        workspace_dir=str(workspace),
        jwt_secret=secret,
        model_api_key="sk-test",
        provider_store_path=str(provfile),
        artifact_dir=str(artdir),
    )

    # 1) 服务未引入任何新面之前，用既有入口在该数据根上造「旧数据」：
    #    launch=false 会话（不跑 run、不碰模型）+ 自定义供应商 + artifact。
    app = create_app(pre_settings, enable_cors=False)
    with TestClient(app) as client:
        old_token = pyjwt.encode(
            {
                "tenant_id": "acme",
                "user_id": "alice",
                "exp": int(datetime.now(UTC).timestamp()) + 600,
            },
            secret,
        )
        created = client.post(
            "/api/sessions",
            json={},
            params={"launch": "false"},
            headers={"Authorization": f"Bearer {old_token}"},
        )
        assert created.status_code == 200, created.text
        old_session_id = created.json()["session_id"]

    provider = ProviderStore.for_settings(pre_settings, MemoryCredentialStore()).create(
        {
            "id": "old-prov",
            "label": "旧供应商",
            "base_url": "https://api.example.test/v1",
            "models": [{"model_id": "m-1"}],
        }
    )
    assert provider["id"] == "old-prov"
    old_artifact = asyncio.run(
        LocalArtifactStore(pre_settings, session_id=old_session_id).save(
            old_session_id,
            "旧数据大输出原文：服务换代后必须仍可逐字读回。",
            mime_type="text/plain",
            source_tool="bash",
            tool_call_id="call-old",
        )
    )

    before = _snapshot_files(workspace)
    assert any(p.startswith("sessions/") for p in before)

    # 2) serve 子进程在**同一数据根 + 同一供应商/artifact 存储**上启动：
    #    不迁移、不换根、不空白冒充。
    tokfile = tmp_path / "tokens.json"
    proc = _spawn_serve(
        workspace,
        tokfile,
        PROVIDER_STORE_PATH=str(provfile),
        ARTIFACT_DIR=str(artdir),
    )
    try:
        endpoint = _wait_attachable(workspace, tokfile).endpoint
        assert endpoint is not None
        token = HostTokenStore(FileCredentialStore(tokfile)).get_token(workspace)
        assert token
        base = f"http://127.0.0.1:{endpoint.port}"
        status, sessions = _http_get(f"{base}/api/sessions?limit=50", token=token)
        assert status == 200
        assert any(row["session_id"] == old_session_id for row in sessions)
        status, events = _http_get(
            f"{base}/api/sessions/{old_session_id}/events", token=token
        )
        assert status == 200
        assert events
        # 模型配置同样原位可读（#355 AC：服务沿用既有 provider 存储，不另立配置面）。
        status, providers = _http_get(f"{base}/api/model-providers", token=token)
        assert status == 200
        assert any(
            row["id"] == "old-prov" and row["label"] == "旧供应商"
            for row in providers["providers"]
        )
        # Artifact 原位可读（#355 AC + 不变量 #15 的读路径）。
        status, slice_payload = _http_get(
            f"{base}/api/sessions/{old_session_id}/artifacts/{old_artifact.artifact_id}",
            token=token,
        )
        assert status == 200
        assert any(
            "旧数据大输出原文" in str(line) for line in slice_payload["lines"]
        )
    finally:
        _terminate(proc)

    # 3) 服务往返后：旧文件一个不少（#303 禁删项的文件系统级证明）。
    after = _snapshot_files(workspace)
    missing = before - after
    assert not missing, f"服务启动/停机删除了既有数据：{sorted(missing)[:5]}"
