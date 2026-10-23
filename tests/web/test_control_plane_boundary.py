"""#1483：本机控制面的统一 Host / Origin / CORS 边界。

这些用例走真实 FastAPI/Starlette 请求与 WebSocket 接缝，不枚举路由装饰器；
额外挂载的路由也必须自动落入同一条中央边界。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import Depends
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_harness.config import Settings
from agent_harness.web.app import create_app
from agent_harness.web.projects import check_trusted_host, require_trusted_origin

_LOCAL_ORIGIN = "http://localhost:5173"
_LOCAL_HOST = "127.0.0.1:8000"
_PRIVATE_PATH = "/newly-mounted-private-api"

# P2-2 regression: async HTTP tests must use a real loopback Host instead of
# synthetic ``test``/``restart.test`` names, so this boundary test remains the
# canonical check that non-loopback synthetic Host values are rejected.


def _app(tmp_path: Path, *, enable_cors: bool = False):
    calls: list[str] = []
    app = create_app(
        Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"),
        enable_cors=enable_cors,
        allow_testserver=True,
    )

    @app.get(_PRIVATE_PATH)
    async def newly_mounted_private_api() -> dict[str, object]:
        calls.append("called")
        return {"private": True}

    return app, calls


def test_central_boundary_protects_a_newly_mounted_route(tmp_path: Path) -> None:
    app, calls = _app(tmp_path)
    with TestClient(app) as client:
        allowed = client.get(
            _PRIVATE_PATH,
            headers={"Origin": _LOCAL_ORIGIN, "Host": _LOCAL_HOST},
        )
        assert allowed.status_code == 200
        assert allowed.json() == {"private": True}

        before_denied = len(calls)
        for origin in (
            "https://evil.example",
            "http://localhost.evil.example",
            "http://127.0.0.1.evil.example",
            "http://evil-localhost:5173",
            "http://xlocalhost",
            "null",
            "file://localhost",
        ):
            denied = client.get(
                _PRIVATE_PATH,
                headers={"Origin": origin, "Host": _LOCAL_HOST},
            )
            assert denied.status_code == 403, (origin, denied.text)
        assert len(calls) == before_denied


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://localhost",
        "http://localhost/path",
        "http://localhost?query=1",
        "http://user@localhost",
        "http://localhost:0",
        "http://localhost:65536",
    ],
)
def test_malformed_origins_are_not_loopback_origins(
    tmp_path: Path, origin: str
) -> None:
    app, calls = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get(
            _PRIVATE_PATH,
            headers={"Origin": origin, "Host": _LOCAL_HOST},
        )
    assert response.status_code == 403
    assert calls == []


def test_host_is_checked_even_when_origin_is_missing(tmp_path: Path) -> None:
    app, calls = _app(tmp_path)
    with TestClient(app) as client:
        local_script = client.get(_PRIVATE_PATH, headers={"Host": _LOCAL_HOST})
        assert local_script.status_code == 200

        forged = client.get(_PRIVATE_PATH, headers={"Host": "evil.example"})
        assert forged.status_code == 403
        assert len(calls) == 1


def test_duplicate_origin_is_rejected_fail_closed(tmp_path: Path) -> None:
    app, calls = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get(
            _PRIVATE_PATH,
            headers=[
                ("Host", _LOCAL_HOST),
                ("Origin", _LOCAL_ORIGIN),
                ("Origin", _LOCAL_ORIGIN),
            ],
        )
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("host", ["localhost:8000", "[::1]:8000", "LOCALHOST:5173"])
def test_exact_loopback_hosts_are_allowed(tmp_path: Path, host: str) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get(
            _PRIVATE_PATH,
            headers={"Origin": _LOCAL_ORIGIN, "Host": host},
        )
    assert response.status_code == 200, response.text


def test_cors_is_explicit_and_rejects_cross_origin_preflight(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, enable_cors=True)
    with TestClient(app) as client:
        allowed = client.get(
            _PRIVATE_PATH,
            headers={"Origin": _LOCAL_ORIGIN, "Host": _LOCAL_HOST},
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == _LOCAL_ORIGIN
        assert allowed.headers["access-control-allow-origin"] != "*"

        preflight = client.options(
            _PRIVATE_PATH,
            headers={
                "Origin": _LOCAL_ORIGIN,
                "Host": _LOCAL_HOST,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert preflight.status_code == 200, preflight.text
        assert preflight.headers["access-control-allow-origin"] == _LOCAL_ORIGIN
        assert "POST" in preflight.headers["access-control-allow-methods"]
        assert (
            "authorization" in preflight.headers["access-control-allow-headers"].lower()
        )

        forged_host_preflight = client.options(
            _PRIVATE_PATH,
            headers={
                "Origin": _LOCAL_ORIGIN,
                "Host": "evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert forged_host_preflight.status_code == 403

        evil = client.options(
            _PRIVATE_PATH,
            headers={
                "Origin": "https://evil.example",
                "Host": _LOCAL_HOST,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert evil.status_code == 403
        assert "access-control-allow-origin" not in evil.headers


def test_extra_origin_and_host_require_explicit_configuration(tmp_path: Path) -> None:
    calls: list[str] = []
    app = create_app(
        Settings(
            _env_file=None,
            workspace_dir=str(tmp_path),
            model_api_key="sk-test",
            control_plane_allowed_origins="https://desktop.example",
            control_plane_allowed_hosts="control.example:8443",
        ),
        enable_cors=True,
        allow_testserver=True,
    )

    @app.get(_PRIVATE_PATH)
    async def configured_private_api() -> dict[str, object]:
        calls.append("called")
        return {"private": True}

    with TestClient(app) as client:
        allowed = client.get(
            _PRIVATE_PATH,
            headers={
                "Origin": "https://desktop.example",
                "Host": "control.example:8443",
            },
        )
        assert allowed.status_code == 200
        assert (
            allowed.headers["access-control-allow-origin"] == "https://desktop.example"
        )

        with client.websocket_connect(
            "/api/ws",
            headers={
                "Origin": "https://desktop.example",
                "Host": "control.example:8443",
            },
        ) as websocket:
            websocket.send_text(json.dumps({"type": "ping"}))
            assert json.loads(websocket.receive_text())["type"] == "pong"

        not_configured = client.get(
            _PRIVATE_PATH,
            headers={"Origin": "https://other.example", "Host": "control.example:8443"},
        )
        assert not_configured.status_code == 403
        assert len(calls) == 1


def test_wildcard_extra_origin_is_rejected_at_app_creation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CONTROL_PLANE_ALLOWED_ORIGINS"):
        create_app(
            Settings(
                _env_file=None,
                workspace_dir=str(tmp_path),
                control_plane_allowed_origins="*",
            ),
            allow_testserver=True,
        )


@pytest.mark.parametrize("value", ["*", "null", "*.example", "control*.example"])
def test_wildcard_or_null_extra_host_is_rejected_at_app_creation(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(ValueError, match="CONTROL_PLANE_ALLOWED_HOSTS"):
        create_app(
            Settings(
                _env_file=None,
                workspace_dir=str(tmp_path),
                control_plane_allowed_hosts=value,
            ),
            allow_testserver=True,
        )


def test_testserver_host_requires_explicit_test_injection() -> None:
    scope = {
        "headers": [(b"host", b"testserver")],
        "server": ["testserver", 80],
    }
    assert check_trusted_host(scope) is not None
    assert check_trusted_host(scope, allow_testserver=True) is None


def test_request_origin_uses_create_app_cache(tmp_path: Path, monkeypatch) -> None:
    app = create_app(
        Settings(
            _env_file=None,
            workspace_dir=str(tmp_path),
            control_plane_allowed_origins="https://desktop.example",
        ),
        enable_cors=False,
        allow_testserver=True,
    )

    @app.get("/cached-origin-probe")
    async def cached_origin_probe(_: None = Depends(require_trusted_origin)) -> dict[str, bool]:
        return {"ok": True}

    def _unexpected_reparse(raw: str):
        raise AssertionError(f"configured origins reparsed during request: {raw!r}")

    import agent_harness.web.projects as projects_module

    monkeypatch.setattr(
        projects_module, "configured_control_plane_origins", _unexpected_reparse
    )
    with TestClient(app) as client:
        response = client.get(
            "/cached-origin-probe",
            headers={"Origin": "https://desktop.example"},
        )
    assert response.status_code == 200, response.text


def test_websocket_central_boundary_rejects_forged_host(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                "/api/ws",
                headers={"Host": "evil.example", "Origin": _LOCAL_ORIGIN},
            ),
        ):
            pass

        with client.websocket_connect(
            "/api/ws",
            headers={"Host": _LOCAL_HOST, "Origin": _LOCAL_ORIGIN},
        ) as websocket:
            websocket.send_text(json.dumps({"type": "ping"}))
            assert json.loads(websocket.receive_text())["type"] == "pong"
