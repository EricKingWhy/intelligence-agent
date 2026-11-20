"""#1048 AC1/AC2：后端策略——windows_psec 淡出为 legacy、不再可新建。

红阶段（TDD）：本文件钉住的行为当前全部未成立：
- `windows_psec` 仍在 `SUPPORTED_BACKENDS`（capabilities.py:26）；
- 请求层放行 `sandbox_backend="windows_psec"`（web/app.py:221-228）；
- `_build_mapping` 对 `windows_psec` 无拒绝分支（registry.py:99-106）。

受影响工作区改用每会话私有 Docker named volume；PSEC 仅作 legacy 恢复路径。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.sandbox.capabilities import SUPPORTED_BACKENDS
from agent_harness.sandbox.registry import WorkspaceRegistry


class TestNewSessionBackendPolicy:
    def test_windows_psec_is_not_a_selectable_new_session_backend(self) -> None:
        """AC2：新会话可请求的后端集合不再含 windows_psec。"""
        assert "windows_psec" not in SUPPORTED_BACKENDS

    def test_legacy_backends_constant_declares_windows_psec(self) -> None:
        """AC2：PSEC 降级为 legacy-only——需要显式的 LEGACY_BACKENDS 声明。"""
        from agent_harness.sandbox.capabilities import LEGACY_BACKENDS

        assert LEGACY_BACKENDS == ("windows_psec",)
        assert not set(LEGACY_BACKENDS) & set(SUPPORTED_BACKENDS)

    def test_create_request_rejects_windows_psec_backend(self) -> None:
        """AC2：请求层不得再接受 windows_psec（响亮 422/ValidationError）。"""
        from pydantic import ValidationError

        from agent_harness.web.app import CreateSessionRequest

        with pytest.raises(ValidationError):
            CreateSessionRequest(task="t", sandbox_backend="windows_psec")

    def test_registry_create_rejects_windows_psec_for_new_session(
        self, tmp_path: Path
    ) -> None:
        """AC2：registry 新建路径对 windows_psec 响亮 ValueError，不构造 PSEC。"""
        registry = WorkspaceRegistry(root=tmp_path, backend="local")
        with pytest.raises(ValueError):
            registry.create("sess_psec_new", backend="windows_psec")
        assert not registry.exists("sess_psec_new")

    def test_windows_platform_default_backend_is_local(self) -> None:
        """#1644（翻转 #1048 的默认）：Windows 平台默认 local，不是 docker。

        2026-10-28 用户裁决：Windows 默认改 local（#1048 硬链接口子重开，风险由
        用户明示接受），docker 改为显式可选。本用例钉住该默认——它的前身
        `test_windows_platform_default_backend_is_docker` 断言 `== "docker"`，
        本票落地即红。
        """
        from agent_harness.sandbox.capabilities import default_backend_for_platform

        assert default_backend_for_platform("nt") == "local"

    def test_posix_platform_default_backend_stays_local(self) -> None:
        """#1644 AC3：非 Windows 默认仍 local（不随本票改变）。"""
        from agent_harness.sandbox.capabilities import default_backend_for_platform

        assert default_backend_for_platform("posix") == "local"
