"""Sandbox 后端能力探针（#363 / W-19）。

显式选择的前置：创建 Task 前先问"谁可用、为什么不可用"，而不是选完才炸。
设计抄 DeepSeek Harness：
- `SANDBOX_UNAVAILABLE` 结构化错误 + fail-closed（"silent unconfined passthrough
  is forbidden"，`docs/subsystems/sandbox.md:172`）；
- 配置 typo 响亮失败，不静默改策略。

本模块是纯查询（无副作用）：探针不创建容器、不拉镜像。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("agent_harness.sandbox.capabilities")

#: 支持的后端名（DSH：非法值在加载时响亮拒绝，不静默改策略）。
SUPPORTED_BACKENDS = ("local", "docker")


@dataclass(frozen=True)
class SandboxCapabilities:
    """单个后端的能力探针结果。"""

    backend: str
    available: bool
    #: 不可用原因（人类可读，含修复提示）；可用时为 None。
    reason: str | None = None
    #: 结构化细节（daemon 版本、镜像是否存在等），给诊断 UI 用。
    details: dict[str, str] = field(default_factory=dict)


class SandboxUnavailableError(RuntimeError):
    """选定的 sandbox 后端不可用——结构化错误，绝不静默降级。

    抄 DSH `SANDBOX_UNAVAILABLE`：fail-closed，调用方把错误直接呈现给用户
    （诊断 + 可选切换动作），而不是把命令放裸跑。
    """

    def __init__(
        self,
        backend: str,
        reason: str,
        *,
        details: dict[str, str] | None = None,
        remediation: str | None = None,
    ) -> None:
        self.backend = backend
        self.reason = reason
        self.details = dict(details or {})
        self.remediation = remediation
        message = f"Sandbox 后端 '{backend}' 不可用：{reason}"
        if remediation:
            message += f"；修复：{remediation}"
        super().__init__(message)


def _import_docker():
    """延迟导入 docker SDK（可选依赖，不在模块顶层 import）。

    拆成小函数只为测试可 patch；生产路径与 DockerSandbox 一致。
    """
    import importlib

    return importlib.import_module("docker")


def _docker_client():
    """建一个 docker client（不 ping，只构造）。"""
    docker = _import_docker()
    return docker.from_env(use_context=False)


def probe_local_capabilities() -> SandboxCapabilities:
    """本机后端：零外部依赖，永远可用。"""
    return SandboxCapabilities(backend="local", available=True)


def probe_docker_capabilities(image: str = "python:3-slim") -> SandboxCapabilities:
    """Docker 后端能力探针：SDK → daemon → 镜像，逐项 fail-closed。

    任一环节失败即返回 available=False（不抛），调用方决定是阻断还是提示。
    """
    try:
        _import_docker()
    except ModuleNotFoundError:
        return SandboxCapabilities(
            backend="docker",
            available=False,
            reason="Docker Python SDK 未安装",
            details={"remediation": "pip install docker"},
        )
    try:
        client = _docker_client()
    except Exception as exc:  # noqa: BLE001 — 任何构造失败都视为不可用
        return SandboxCapabilities(
            backend="docker",
            available=False,
            reason=f"Docker client 构造失败：{exc}",
            details={},
        )
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001 — daemon 不可达是最常见的不可用
        logger.info("Docker daemon 不可达: %s", exc)
        return SandboxCapabilities(
            backend="docker",
            available=False,
            reason=f"Docker daemon 不可达：{exc}",
            details={"remediation": "启动 Docker Desktop / dockerd 后重试"},
        )
    try:
        client.images.get(image)
        image_present = True
    except Exception:  # noqa: BLE001 — 镜像不存在（或无权读）都算缺依赖
        image_present = False
    if not image_present:
        return SandboxCapabilities(
            backend="docker",
            available=False,
            reason=f"镜像 '{image}' 不存在",
            details={"remediation": f"docker pull {image}"},
        )
    version: dict = {}
    try:
        version = client.version() or {}
    except Exception:
        logger.debug("Docker 版本读取失败，忽略", exc_info=True)
    details = {"image": image, "image_present": "true"}
    server_version = version.get("Version")
    if server_version:
        details["daemon_version"] = str(server_version)
    return SandboxCapabilities(backend="docker", available=True, details=details)


def probe_all_capabilities(image: str = "python:3-slim") -> list[SandboxCapabilities]:
    """探针全部后端（给选择器 UI 用：展示能力 + 缺依赖原因）。"""
    return [probe_local_capabilities(), probe_docker_capabilities(image=image)]


def require_available(
    caps: SandboxCapabilities, *, remediation: str | None = None
) -> None:
    """断言后端可用；不可用则抛结构化 SandboxUnavailableError（不降级）。

    抄 DSH fail-closed：调用方（registry.create）在此处阻断，绝不静默回落 local。
    """
    if caps.available:
        return
    raise SandboxUnavailableError(
        caps.backend,
        caps.reason or "未知原因",
        details=caps.details,
        remediation=remediation or caps.details.get("remediation"),
    )
