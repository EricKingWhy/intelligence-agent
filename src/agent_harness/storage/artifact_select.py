"""artifact store 的**唯一**选择器：写路径与读路径都从这里取 store（#192）。

**为什么必须有这个模块**：写路径（`assembly.build_runtime` 的外置链路）和读路径
（`web/artifacts.py` 的只读 store）必须指向**同一个** store。只要"选哪个 Provider、
按什么优先级"写两遍，就会出现"东西写进了 A、从 B 读"的静默错配——界面永远空着，
而日志里一切正常。审查（#192 批 1）正是按这条把原来两处各自的 if 级联收成一份。

**指针与读回工具成对返回**：三个 Provider 的键约定与接口相同，但**模型侧读回工具**
历史上分成两个（`InspectArtifactTool` 面向 S3 的通用存储，`ReadArtifactTool` 面向 T5
外置链路）。这个配对关系也是一种"必须一致"的知识，所以和选 store 放在一起——
否则各处重新判断"这类 store 配哪个工具"，同一个 store 又会有两种读法。

**优先级 S3 → MinIO → Local**：显式配置的对象存储永远优先；Local 是兜底
（spec 06 §3 把 Local filesystem 定为"开发/小型部署"的默认 Provider）。
`None` = 这个部署确实没有可用的 store（读接口据此如实回 503）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from agent_harness.config import Settings
from agent_harness.storage.artifact import ArtifactStore, SESSION_KEY_PATTERN
from agent_harness.storage.local_artifact import LocalArtifactStore
from agent_harness.storage.minio_artifact import MinioArtifactStore
from agent_harness.storage.s3_artifact import S3ArtifactStore
from agent_harness.tooling import Tool
from agent_harness.tools.inspect_artifact import InspectArtifactTool
from agent_harness.tools.read_artifact import ReadArtifactTool

__all__ = [
    "ArtifactStorePlan",
    "ArtifactStoreSelection",
    "plan_artifact_store",
    "select_artifact_store",
]

logger = logging.getLogger("agent_harness.storage.artifact_select")

#: 构造 store 时可预期的失败类型——**都表示"这个部署没有可用 store"**，不是 bug：
#: - `ValueError`：配置不完整（半配置对象存储、`artifact_dir` 为空）；
#: - `OSError`：本地路径在本平台非法（win32 非法字符）/ 不可创建；
#: - `RuntimeError`：**可选依赖缺失**（`pip install 'intelligence-agent[artifact]'`）。
#:   这一条曾漏掉，而它恰恰是最要紧的：把对象存储配好了但没装 `[artifact]` extra 的部署，
#:   构造期会抛 `RuntimeError`，沿 `build_runtime` 冒到建会话 → **每次建会话 500**，
#:   读接口也会 500 而不是 503。那违反不变量 21（可选能力故障不许拖垮 Core）——
#:   而这个函数的存在理由之一就是兜住它。其它异常类型一律照旧上抛（真 bug 不该被吞）。
_UNUSABLE_STORE = (ValueError, OSError, RuntimeError)


@dataclass(frozen=True)
class ArtifactStoreSelection:
    """选中的 store + 与它配对（且与它同域）的模型侧读回工具。"""

    store: ArtifactStore
    #: 构造读回工具：调用方拿到 store 后 `read_tool(store)` 注册即可。
    read_tool: Callable[[ArtifactStore], Tool]
    #: 供日志/诊断与测试断言用的 Provider 名（"s3" / "minio" / "local"）。
    provider: str


@dataclass(frozen=True)
class ArtifactStorePlan:
    """Pure provider and read-tool choice; construction happens separately."""

    store_type: type[ArtifactStore]
    read_tool: type[Tool]
    provider: str


def _s3_configured(settings: Settings) -> bool:
    return any((
        settings.artifact_store_endpoint,
        settings.artifact_store_bucket,
        settings.artifact_store_access_key.get_secret_value(),
        settings.artifact_store_secret_key.get_secret_value(),
        settings.artifact_store_region,
    ))


def _minio_configured(settings: Settings) -> bool:
    return any((
        settings.minio_endpoint,
        settings.minio_bucket,
        settings.minio_access_key.get_secret_value(),
        settings.minio_secret_key.get_secret_value(),
    ))


def plan_artifact_store(settings: Settings, session_id: str) -> ArtifactStorePlan | None:
    """Choose a configured artifact provider without constructing resources or doing I/O."""
    if not SESSION_KEY_PATTERN.fullmatch(session_id):
        return None
    if _s3_configured(settings):
        if not all((
            settings.artifact_store_endpoint,
            settings.artifact_store_bucket,
            settings.artifact_store_access_key.get_secret_value(),
            settings.artifact_store_secret_key.get_secret_value(),
            settings.artifact_store_region,
        )):
            return None
        return ArtifactStorePlan(S3ArtifactStore, InspectArtifactTool, "s3")
    if _minio_configured(settings):
        if not all((
            settings.minio_endpoint,
            settings.minio_bucket,
            settings.minio_access_key.get_secret_value(),
            settings.minio_secret_key.get_secret_value(),
        )):
            return None
        return ArtifactStorePlan(MinioArtifactStore, ReadArtifactTool, "minio")
    if not settings.artifact_dir.strip():
        return None
    return ArtifactStorePlan(LocalArtifactStore, ReadArtifactTool, "local")


def select_artifact_store(settings: Settings, session_id: str) -> ArtifactStoreSelection | None:
    """按优先级选 store 并配好读回工具；选不出可用 store 时返回 `None`。

    **半配置的对象存储一律返回 `None`，不降级到下一级**：配了 endpoint 却没配 bucket
    是部署配置错误。静默改用 Local 会让运维以为产物进了对象存储——那比"读不到"更糟
    （读不到至少是诚实的）。

    A pure plan owns provider precedence and read-tool pairing. This function only
    constructs that plan and converts expected provider failures into `None`.
    """
    plan = plan_artifact_store(settings, session_id)
    if plan is None:
        if _s3_configured(settings):
            logger.warning("artifact store 's3' 配置不可用，本会话不外置")
        elif _minio_configured(settings):
            logger.warning("artifact store 'minio' 配置不可用，本会话不外置")
        elif not settings.artifact_dir.strip():
            logger.debug("本地 artifact 落盘已关闭（artifact_dir 为空）")
        return None
    try:
        return ArtifactStoreSelection(
            store=plan.store_type(settings, session_id=session_id),
            read_tool=plan.read_tool,
            provider=plan.provider,
        )
    except _UNUSABLE_STORE as error:
        # `artifact_dir` 为空 = 显式关掉本地落盘；`OSError` 覆盖 `Path.resolve()` 在
        # 非法路径上的失败（win32 的非法字符）。两者都表示"这个部署没有可用 store"。
        # 空 `artifact_dir` 是**合法配置**（写了就是"别落盘"），不该每次建会话都刷一条
        # warning——那会把真正的部署故障淹掉；只有"配了 dir 却用不了"才值得留痕。
        if (
            plan.provider == "local"
            and isinstance(error, ValueError)
            and not settings.artifact_dir.strip()
        ):
            logger.debug("本地 artifact 落盘已关闭（artifact_dir 为空）")
        else:
            logger.warning(
                "artifact store '%s' 不可用，本会话不外置：%s", plan.provider, error,
            )
        return None
