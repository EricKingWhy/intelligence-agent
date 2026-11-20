"""#1644：service 层部署默认（`sandbox_backend=None`）落 local，零探针。

Watchtower 的触发路径就是这里的形状：`rule.sandbox_backend=None` ⇒
`create_and_launch(sandbox_backend=None, launch=False)` ⇒ registry 部署默认。
Windows（`default_backend_for_platform("nt")`）改默认后该路径必须是 local 且
不探 docker——规则为 None 的值班会话在无 docker 的机器上也得建得起来。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agent_harness.sandbox.registry as sandbox_registry
from agent_harness.config import Settings
from agent_harness.sandbox.capabilities import (
    SandboxCapabilities,
    default_backend_for_platform,
)
from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session.event import SESSION_STARTED
from agent_harness.session.store import JsonlSessionStore
from agent_harness.web.app import session_service


def _fake_build_runtime(registry: WorkspaceRegistry):
    """替身 `build_runtime`，但保留真实顺序：它内部 `registry.create(...)`。

    真实现（`assembly.build_runtime`）就是用同一组 kwargs 调 registry.create，
    映射文件由那一步写出；替身只省掉模型/工具装配，不改变 sandbox 语义——
    显式 docker 的 SandboxUnavailableError 仍由 registry 原样抛出。

    边界：替身只透传 `session_id` / `workspace` / `sandbox_backend` 三个 kwarg，
    其余装配参数（permission / approval / budget / tools …）一律丢弃——
    本文件的用例**不得**断言这些参数被传递（要断言请改用真 build_runtime 或另造替身）。
    """

    async def build(**kwargs):
        registry.create(
            kwargs["session_id"],
            workspace_root=kwargs["workspace"],
            backend=kwargs["sandbox_backend"],
        )
        return MagicMock()

    return build


def _state(tmp_path: Path, registry: WorkspaceRegistry) -> MagicMock:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_provider="deepseek",
        model_name="deepseek-chat",
    )
    state = MagicMock()
    state.settings = settings
    state.store = JsonlSessionStore(root=tmp_path / "sessions")
    state.workspaces_root = tmp_path
    state.workspace_registry = registry
    state.workspace_index = None
    state.run_manager = MagicMock()
    state.run_manager.get_active = MagicMock(return_value=None)
    state.run_manager.launch = MagicMock(return_value=(MagicMock(), MagicMock()))
    state.get_wiring = AsyncMock(return_value=(MagicMock(), MagicMock()))
    state.ensure_stores = AsyncMock()
    state.stores = MagicMock()
    state.stores.delegation_tree_ledger.get_session_budget = AsyncMock(return_value=None)
    return state


def test_service_default_backend_is_local_on_windows_and_never_probes(
    tmp_path, monkeypatch
):
    """`sandbox_backend=None`（Watchtower 规则形状）⇒ 映射 local、不探 docker。"""
    calls: list[str] = []

    def probe(*_a, **_k):
        calls.append("probe")
        return SandboxCapabilities(backend="docker", available=False, reason="无 daemon")

    monkeypatch.setattr(sandbox_registry, "probe_docker_capabilities", probe)
    registry = WorkspaceRegistry(
        root=tmp_path, backend=default_backend_for_platform("nt")
    )
    assert default_backend_for_platform("nt") == "local"
    state = _state(tmp_path, registry)

    with patch(
        "agent_harness.session.service.build_runtime", _fake_build_runtime(registry)
    ):
        result = asyncio.run(
            session_service(state).create_and_launch(task="值班检查", launch=False)
        )

    session_id = result.session.session_id
    mapping = json.loads(
        (tmp_path / "workspaces" / f"{session_id}.json").read_text(encoding="utf-8")
    )
    assert mapping["backend"] == "local"
    assert calls == [], "默认路径不该探 docker"
    started = state.store.read_events(session_id)[0]
    assert started.type == SESSION_STARTED
    assert "sandbox_backend" not in started.data


def test_service_explicit_docker_still_fails_closed(tmp_path, monkeypatch):
    """显式 docker + 不可用 ⇒ SandboxUnavailableError（不静默回落 local）。"""
    from agent_harness.sandbox.capabilities import SandboxUnavailableError

    monkeypatch.setattr(
        sandbox_registry,
        "probe_docker_capabilities",
        lambda *a, **k: SandboxCapabilities(
            backend="docker", available=False, reason="Docker Python SDK 未安装",
            details={"remediation": "pip install docker"},
        ),
    )
    registry = WorkspaceRegistry(root=tmp_path, backend="local")
    state = _state(tmp_path, registry)
    with (
        patch(
            "agent_harness.session.service.build_runtime",
            _fake_build_runtime(registry),
        ),
        pytest.raises(SandboxUnavailableError),
    ):
        asyncio.run(
            session_service(state).create_and_launch(
                task="t", launch=False, sandbox_backend="docker"
            )
        )
    assert list((tmp_path / "workspaces").glob("*.json")) == []
