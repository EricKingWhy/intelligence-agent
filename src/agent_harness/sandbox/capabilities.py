"""Sandbox 后端能力探针（#363 / W-19）。

显式选择的前置：创建 Task 前先问"谁可用、为什么不可用"，而不是选完才炸。
设计抄 DeepSeek Harness：
- `SANDBOX_UNAVAILABLE` 结构化错误 + fail-closed（"silent unconfined passthrough
  is forbidden"，`docs/subsystems/sandbox.md:172`）；
- 配置 typo 响亮失败，不静默改策略。

本模块是纯查询（无副作用）：探针不创建容器、不拉镜像。
"""

from __future__ import annotations

import base64
import json
import logging
import ntpath
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field

logger = logging.getLogger("agent_harness.sandbox.capabilities")

#: 支持的后端名（DSH：非法值在加载时响亮拒绝，不静默改策略）。
#: #1048：`windows_psec` 已降级为 **legacy-only**——NTFS hard link 的对象身份边界
#: 靠 ACL/路径授权解决不了，受影响工作区改用每会话私有 Docker named volume。
#: 它不再可作为新会话后端，只保留用于解析**既有** mapping 的恢复路径。
SUPPORTED_BACKENDS = ("local", "docker")

#: 仅用于恢复既有映射、不可用于新建会话的后端名（#1048）。
LEGACY_BACKENDS = ("windows_psec",)


def default_backend_for_platform(name: str = os.name) -> str:
    """平台默认后端（#1048）。

    受影响（Windows/NTFS）平台上工作区的对象身份边界只能靠换文件系统切断，
    故平台默认落 `docker`（不可用即 fail-closed，绝不回落 local/PSEC）。
    其余平台默认 `local`（无 NTFS hard link 对象身份问题）。
    """
    return "docker" if name == "nt" else "local"


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


def probe_windows_psec_capabilities(
    *,
    executable: str | None = None,
    config: Mapping[str, object] | None = None,
    run=None,
) -> SandboxCapabilities:
    """Probe MXC's exact policy and accept only its native BaseContainer tier."""
    if os.name != "nt":
        return SandboxCapabilities(
            backend="windows_psec",
            available=False,
            reason="Windows PSEC sandbox requires Windows",
        )
    path = executable or os.environ.get("IA_MXC_EXECUTABLE")
    if not path:
        return SandboxCapabilities(
            backend="windows_psec",
            available=False,
            reason="MXC wxc-exec.exe is not configured",
            details={
                "remediation": "install the desktop runtime or set IA_MXC_EXECUTABLE",
            },
        )
    if config is None:
        return SandboxCapabilities(
            backend="windows_psec",
            available=False,
            reason="An exact PSEC policy is required to verify workspace isolation",
            details={
                "workspace_policy_required": "true",
                "remediation": "probe the policy that will be used to create the sandbox",
            },
        )

    args = [path, "--probe"]
    payload = json.dumps(config, separators=(",", ":")).encode("utf-8")
    args.extend(["--config-base64", base64.b64encode(payload).decode("ascii")])
    try:
        result = (run or subprocess.run)(
            args,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxCapabilities(
            backend="windows_psec",
            available=False,
            reason=f"MXC capability probe failed: {exc}",
        )
    if result.returncode != 0:
        detail = str(result.stderr or result.stdout or "").strip()
        return SandboxCapabilities(
            backend="windows_psec",
            available=False,
            reason=f"MXC capability probe exited {result.returncode}: {detail}",
        )
    try:
        report = json.loads(result.stdout)
        probes = report["probes"]
        tier = report["tier"]
        dacl_augmentation = report["needsDaclAugmentation"]
        base_container_present = probes["baseContainerApiPresent"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return SandboxCapabilities(
            backend="windows_psec",
            available=False,
            reason=f"MXC capability probe returned invalid JSON: {exc}",
        )

    details = {
        "tier": str(tier),
        "needs_dacl_augmentation": str(dacl_augmentation).lower(),
        "base_container_api": str(base_container_present).lower(),
    }
    if (
        tier != "base-container"
        or dacl_augmentation is not False
        or base_container_present is not True
    ):
        return SandboxCapabilities(
            backend="windows_psec",
            available=False,
            reason=(
                "MXC did not select BaseContainer/PSEC without DACL mutation "
                f"(tier={tier}, needsDaclAugmentation={dacl_augmentation})"
            ),
            details=details,
        )
    return SandboxCapabilities(backend="windows_psec", available=True, details=details)


def probe_all_capabilities(
    image: str = "python:3-slim",
    *,
    windows_psec_workspace: str | None = None,
) -> list[SandboxCapabilities]:
    """探针全部后端（给选择器 UI 用：展示能力 + 缺依赖原因）。"""
    if windows_psec_workspace is None or os.name != "nt":
        psec = probe_windows_psec_capabilities()
    else:
        workspace = ntpath.normpath(windows_psec_workspace)
        if not ntpath.isabs(workspace):
            psec = SandboxCapabilities(
                backend="windows_psec",
                available=False,
                reason="PSEC requires an absolute path to an existing workspace",
            )
        else:
            try:
                from agent_harness.sandbox.windows_psec import WindowsPsecSandbox

                config = WindowsPsecSandbox.probe_configuration_for_workspace(workspace)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                psec = SandboxCapabilities(
                    backend="windows_psec",
                    available=False,
                    reason=f"Could not build the workspace PSEC policy: {exc}",
                )
            else:
                psec = probe_windows_psec_capabilities(config=config)
    return [
        probe_local_capabilities(),
        probe_docker_capabilities(image=image),
        psec,
    ]


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
