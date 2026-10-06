"""Sandbox：Coding Tool 的隔离执行环境。

对外暴露抽象契约（Sandbox + ExecResult）和具体后端。
其他实现细节不对外导出。

DockerSandbox 的 docker SDK 依赖在其实例化时才懒加载，
模块导入本身不需要 docker 已安装，因此放在包级导出安全。
"""

from agent_harness.sandbox.base import (
    ExecResult,
    Sandbox,
    ShellEnvironment,
    ShellFamily,
)
from agent_harness.sandbox.capabilities import (
    SUPPORTED_BACKENDS,
    SandboxCapabilities,
    SandboxUnavailableError,
    probe_all_capabilities,
    probe_docker_capabilities,
    probe_local_capabilities,
)
from agent_harness.sandbox.docker import DockerSandbox
from agent_harness.sandbox.local import LocalSubprocessSandbox
from agent_harness.sandbox.paths import canonical_workspace_path
from agent_harness.sandbox.registry import WorkspaceBindingError, WorkspaceRegistry

__all__ = [
    "SUPPORTED_BACKENDS",
    "DockerSandbox",
    "ExecResult",
    "LocalSubprocessSandbox",
    "Sandbox",
    "SandboxCapabilities",
    "SandboxUnavailableError",
    "ShellEnvironment",
    "ShellFamily",
    "WorkspaceBindingError",
    "WorkspaceRegistry",
    "canonical_workspace_path",
    "probe_all_capabilities",
    "probe_docker_capabilities",
    "probe_local_capabilities",
]
