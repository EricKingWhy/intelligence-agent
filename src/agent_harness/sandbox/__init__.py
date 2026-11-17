"""Sandbox：Coding Tool 的隔离执行环境。

对外暴露抽象契约（Sandbox + ExecResult）和具体后端。
其他实现细节不对外导出。

`HARDENING_REASONS` 是该纪律的**刻意例外**：它是 `SandboxHardeningError.reason`
取值域的单一来源（`sandbox/docker.py` 里的唯一定义处），web 层响应白名单公开消费它
（`web/domain_errors.py`）。它是公开合同，不是实现细节外泄——导出它，web 层才不必
伸手进 `sandbox.docker` 拿私有名（#1539 跨包私有 import 守卫）。

DockerSandbox 的 docker SDK 依赖在其实例化时才懒加载，
模块导入本身不需要 docker 已安装，因此放在包级导出安全。
"""

from agent_harness.sandbox.base import (
    ExecResult,
    MultiLineCommandUnsupportedError,
    Sandbox,
    ShellEnvironment,
    ShellFamily,
)
from agent_harness.sandbox.capabilities import (
    SUPPORTED_BACKENDS,
    SandboxCapabilities,
    SandboxUnavailableError,
    default_backend_for_platform,
    probe_all_capabilities,
    probe_docker_capabilities,
    probe_local_capabilities,
)
from agent_harness.sandbox.docker import HARDENING_REASONS, DockerSandbox
from agent_harness.sandbox.local import LocalSubprocessSandbox
from agent_harness.sandbox.paths import canonical_workspace_path
from agent_harness.sandbox.registry import WorkspaceBindingError, WorkspaceRegistry

__all__ = [
    "HARDENING_REASONS",
    "SUPPORTED_BACKENDS",
    "DockerSandbox",
    "ExecResult",
    "LocalSubprocessSandbox",
    "MultiLineCommandUnsupportedError",
    "Sandbox",
    "SandboxCapabilities",
    "SandboxUnavailableError",
    "ShellEnvironment",
    "ShellFamily",
    "WorkspaceBindingError",
    "WorkspaceRegistry",
    "canonical_workspace_path",
    "default_backend_for_platform",
    "probe_all_capabilities",
    "probe_docker_capabilities",
    "probe_local_capabilities",
]
