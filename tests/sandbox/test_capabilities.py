"""#363 [W-19] Sandbox 能力探针与显式选择：TDD 红测。

铁律：选 Docker 但不可用时**绝不静默降级成本机**——必须抛结构化
SandboxUnavailableError（抄 DSH `SANDBOX_UNAVAILABLE` fail-closed）。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.sandbox.capabilities import (
    SandboxCapabilities,
    SandboxUnavailableError,
    probe_all_capabilities,
    probe_docker_capabilities,
    probe_local_capabilities,
    probe_windows_psec_capabilities,
)
from agent_harness.sandbox.registry import WorkspaceRegistry


class TestSandboxBackendSelection:
    def test_request_accepts_sandbox_backend(self):
        """#363：CreateSessionRequest 接受 sandbox_backend 字段。"""
        from agent_harness.web.app import CreateSessionRequest

        req = CreateSessionRequest(task="t", sandbox_backend="docker")
        assert req.sandbox_backend == "docker"

    def test_request_default_is_none(self):
        """#363：缺省 None（部署默认，向后兼容），不是静默 local。"""
        from agent_harness.web.app import CreateSessionRequest

        req = CreateSessionRequest(task="t")
        assert req.sandbox_backend is None

    def test_request_rejects_unknown_backend_422(self):
        """#363：未知后端名在请求层响亮 422（不漏到 registry 才炸）。"""
        from pydantic import ValidationError

        from agent_harness.web.app import CreateSessionRequest

        with pytest.raises(ValidationError) as exc_info:
            CreateSessionRequest(task="t", sandbox_backend="windows-sandbox")
        assert "sandbox_backend must be one of" in str(exc_info.value)

    def test_request_rejects_unknown_field_still(self):
        """#363：extra=forbid 不变，未知字段仍响亮 422。"""
        from pydantic import ValidationError

        from agent_harness.web.app import CreateSessionRequest

        with pytest.raises(ValidationError):
            CreateSessionRequest(task="t", sandbox_backen="docker")  # typo


class TestProbeLocal:
    def test_local_always_available(self):
        """本机后端永远可用（零外部依赖）。"""
        caps = probe_local_capabilities()
        assert isinstance(caps, SandboxCapabilities)
        assert caps.backend == "local"
        assert caps.available is True
        assert caps.reason is None


class TestProbeDocker:
    def test_docker_sdk_missing(self):
        """docker SDK 未安装 → 不可用，原因指明安装方式。"""
        with (
            patch.dict("sys.modules", {"docker": None}),
            patch(
                "agent_harness.sandbox.capabilities._import_docker",
                side_effect=ModuleNotFoundError("No module named 'docker'"),
            ),
        ):
            caps = probe_docker_capabilities()
        assert caps.backend == "docker"
        assert caps.available is False
        assert caps.reason is not None and "SDK" in caps.reason
        assert "pip install docker" in caps.details.get("remediation", "")

    def test_docker_daemon_unreachable(self):
        """daemon 不可达 → 不可用，原因指明 daemon。"""
        import agent_harness.sandbox.capabilities as cap_mod

        class FakeErrors:
            class APIError(Exception):
                pass

        class FakeClient:
            errors = FakeErrors()

            def ping(self):
                raise FakeErrors.APIError("Connection refused")

        with patch.object(cap_mod, "_import_docker") as mock_import:
            mock_import.return_value.errors = FakeErrors
            with patch.object(cap_mod, "_docker_client", return_value=FakeClient()):
                caps = probe_docker_capabilities()
        assert caps.available is False
        assert caps.reason is not None and "daemon" in caps.reason.lower()

    def test_probe_all_returns_available_backend_families(self):
        """#1048 AC2：probe_all 只列**新会话可选**后端（local/docker）。

        `windows_psec` 已降级为 legacy-only，不再进选择器。
        """
        caps = probe_all_capabilities()
        backends = {c.backend for c in caps}
        assert backends == {"local", "docker"}
        assert all(isinstance(c, SandboxCapabilities) for c in caps)


class TestProbeWindowsPsec:
    def test_probe_requires_exact_policy_before_reporting_available(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import agent_harness.sandbox.capabilities as cap_mod

        monkeypatch.setattr(
            cap_mod,
            "os",
            SimpleNamespace(name="nt", environ={}),
        )
        calls: list[list[str]] = []
        probe = {
            "tier": "base-container",
            "needsDaclAugmentation": False,
            "probes": {"baseContainerApiPresent": True},
        }

        caps = probe_windows_psec_capabilities(
            executable=str(tmp_path / "wxc-exec.exe"),
            run=lambda args, **_kwargs: (
                calls.append(args)
                or SimpleNamespace(returncode=0, stdout=json.dumps(probe), stderr="")
            ),
        )

        assert caps.available is False
        assert caps.reason is not None and "policy" in caps.reason.lower()
        assert calls == []

    # #1048 AC2：`probe_all_capabilities` 不再探测 / 列出 windows_psec（它已降级为
    # legacy-only，仅用于恢复既有映射）。原先经由 probe_all 走 PSEC workspace 策略
    # 报文校验的两条 Windows-only 用例随之退役；`probe_windows_psec_capabilities`
    # 本身仍由本类下列用例直接覆盖。

    def test_probe_accepts_only_basecontainer_for_the_requested_policy(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        import agent_harness.sandbox.capabilities as cap_mod

        executable = str(tmp_path / "wxc-exec.exe")
        policy = {
            "version": "1.0.0",
            "containment": "processcontainer",
            "filesystem": {
                "readwritePaths": [str(tmp_path)],
                "deniedPaths": [str(tmp_path / "protected")],
            },
            "fallback": {"allowDaclMutation": False},
        }
        probe = {
            "tier": "base-container",
            "needsDaclAugmentation": False,
            "probes": {"baseContainerApiPresent": True},
        }
        monkeypatch.setattr(
            cap_mod,
            "os",
            SimpleNamespace(name="nt", environ={}),
        )
        calls: list[tuple[list[str], dict[str, object]]] = []

        def run(args: list[str], **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(probe),
                stderr="",
            )

        caps = probe_windows_psec_capabilities(
            executable=executable,
            config=policy,
            run=run,
        )

        assert caps.available is True
        assert caps.backend == "windows_psec"
        assert caps.details["tier"] == "base-container"
        assert calls[0][0][:2] == [executable, "--probe"]
        encoded = calls[0][0][3]
        assert json.loads(base64.b64decode(encoded)) == policy

    @pytest.mark.parametrize(
        "probe",
        [
            {"tier": "appcontainer-bfs", "needsDaclAugmentation": False},
            {"tier": "base-container", "needsDaclAugmentation": True},
        ],
    )
    def test_probe_rejects_fallback_tiers_and_dacl_mutation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        probe: dict,
    ) -> None:
        import agent_harness.sandbox.capabilities as cap_mod

        monkeypatch.setattr(
            cap_mod,
            "os",
            SimpleNamespace(name="nt", environ={}),
        )
        caps = probe_windows_psec_capabilities(
            executable=str(tmp_path / "wxc-exec.exe"),
            config={"fallback": {"allowDaclMutation": False}},
            run=lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=json.dumps(probe),
                stderr="",
            ),
        )

        assert caps.available is False
        assert caps.backend == "windows_psec"
        assert caps.reason


class TestRegistryBackendSelection:
    @pytest.fixture
    def registry(self, tmp_path: Path) -> WorkspaceRegistry:
        return WorkspaceRegistry(root=tmp_path, backend="local")

    def test_default_stays_local(self, registry: WorkspaceRegistry):
        """不显式选择时默认 local（向后兼容）。"""
        sandbox = registry.create("sess_1")
        assert isinstance(sandbox, LocalSubprocessSandbox)

    def test_explicit_local(self, registry: WorkspaceRegistry):
        """显式选 local 正常工作。"""
        sandbox = registry.create("sess_2", backend="local")
        assert isinstance(sandbox, LocalSubprocessSandbox)

    def test_unknown_backend_fails_loud(self, registry: WorkspaceRegistry):
        """未知后端名响亮失败（抄 DSH：typo 不静默改策略）。"""
        with pytest.raises(ValueError, match="未知"):
            registry.create("sess_3", backend="k8s")

    def test_docker_unavailable_raises_no_silent_fallback(
        self, registry: WorkspaceRegistry
    ):
        """选 docker 但不可用 → 抛 SandboxUnavailableError，绝不静默回落 local。"""
        unavailable = SandboxCapabilities(
            backend="docker", available=False, reason="daemon 不可达", details={}
        )
        with (
            patch(
                "agent_harness.sandbox.registry.probe_docker_capabilities",
                return_value=unavailable,
            ),
            pytest.raises(SandboxUnavailableError) as exc_info,
        ):
            registry.create("sess_4", backend="docker")
        # 关键断言：不是 LocalSubprocessSandbox，没有静默降级
        assert "docker" in str(exc_info.value).lower()
        assert not registry.exists("sess_4")


class TestSessionStartedAuditFact:
    def test_explicit_backend_recorded_in_session_started(self, tmp_path: Path):
        """显式选择的后端记进 session/started（审计事实，抄 DSH sandbox/mode）。"""
        from agent_harness.session.session import Session
        from agent_harness.session.store import JsonlSessionStore

        store = JsonlSessionStore(tmp_path / "sessions.jsonl")
        registry = WorkspaceRegistry(root=tmp_path / "wr", backend="local")
        session = Session.start(
            store, workspace_registry=registry, sandbox_backend="local"
        )
        events = list(store.read_events(session.session_id))
        started = next(e for e in events if e.type == "session/started")
        assert started.data.get("sandbox_backend") == "local"

    def test_no_explicit_choice_writes_nothing(self, tmp_path: Path):
        """未显式选择不写字段（explicit 与 default 是两档，不混淆）。"""
        from agent_harness.session.session import Session
        from agent_harness.session.store import JsonlSessionStore

        store = JsonlSessionStore(tmp_path / "sessions.jsonl")
        registry = WorkspaceRegistry(root=tmp_path / "wr", backend="local")
        session = Session.start(store, workspace_registry=registry)
        events = list(store.read_events(session.session_id))
        started = next(e for e in events if e.type == "session/started")
        assert "sandbox_backend" not in started.data
