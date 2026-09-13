"""artifact 内容读取的 web 侧接线（#185）。

**为什么单独一个模块**：路由需要"按 session 构造一个只读 store"，而 store 的构造
口径（配了哪种对象存储、用哪个 session 前缀）原本只存在于 `assembly.build_runtime`
——那是**写路径**的装配点。把"读 store 怎么造"独立出来，路由才有一个可被测试替换的
接缝（与 `assembly.build_runtime` 的补丁口径一致）。

**store 选择口径**（按优先级，与写路径保持一致）：

1. 配了 `artifact_store_*`（S3 兼容）→ `S3ArtifactStore`。这是**唯一会收到外置写入的
   store**：`assembly.build_runtime` 只在这个分支里创建 `ArtifactOverflowHandler`。
2. 只配了 `minio_*` → `MinioArtifactStore`。历史上它只接在 `ReadArtifactTool`（读工具）
   上，没有对应的写路径——保留作读取回退，但**这不改变"没有写路径就没有内容"的事实**。
3. 都没配 → `None`。调用方据此**如实**返回 503，不得伪装成 404。

**artifact_id 是内容哈希**（`sha256(content)[:16]`），跨会话可重复：所以归属只能由
URL 里的 `session_id` 决定（provider 的 key 前缀是 `{session_id}/{artifact_id}`）。
"""

from __future__ import annotations

from agent_harness.config import Settings
from agent_harness.storage.artifact import ARTIFACT_ID_PATTERN, ArtifactStore

#: 单次响应的服务端上限：客户端给多大都会夹到这个范围内（响应体积可控）。
MAX_LINES_CAP = 1000
MAX_CHARS_PER_LINE_CAP = 2000

__all__ = [
    "ARTIFACT_ID_PATTERN",
    "MAX_CHARS_PER_LINE_CAP",
    "MAX_LINES_CAP",
    "build_read_artifact_store",
    "clamp_to_cap",
]


def build_read_artifact_store(settings: Settings, session_id: str) -> ArtifactStore | None:
    """按会话构造只读 artifact store；未配置（或**半配置**）对象存储时返回 None。

    "半配置"也返回 None：provider 的构造器要求**整组**字段齐全，而这里的判据是
    "任意一个字段非空"。若只填了 endpoint 没填 bucket，构造器会抛 ValueError——
    那是部署配置问题，应当以 503（"本部署没配好存储"）如实上报，而不是变成 500。
    """
    if any((
        settings.artifact_store_endpoint,
        settings.artifact_store_bucket,
        settings.artifact_store_access_key.get_secret_value(),
        settings.artifact_store_secret_key.get_secret_value(),
        settings.artifact_store_region,
    )):
        from agent_harness.storage.s3_artifact import S3ArtifactStore

        try:
            return S3ArtifactStore(settings, session_id=session_id)
        except ValueError:
            return None
    if any((
        settings.minio_endpoint,
        settings.minio_bucket,
        settings.minio_access_key.get_secret_value(),
        settings.minio_secret_key.get_secret_value(),
    )):
        from agent_harness.storage.minio_artifact import MinioArtifactStore

        try:
            return MinioArtifactStore(settings, session_id=session_id)
        except ValueError:
            return None
    return None


def clamp_to_cap(value: int, cap: int) -> int:
    """把客户端给的行数/字符数夹进 `[1, cap]`（0 或负数没有意义，同样夹到 1）。"""
    return max(1, min(value, cap))
