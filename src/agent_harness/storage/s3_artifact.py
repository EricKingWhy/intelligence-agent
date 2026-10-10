"""S3 ArtifactStore（七牛云 Kodo S3 兼容）；SDK 仅在构造 Provider 时加载。

#933 M-01：**字节路径不再按会话命名空间寻址**——字节对象落全局内容寻址根，
本会话另存一份回执对象作为归属事实（详见本文件字节路径段注释）。文本路径
（`{session_id}/{artifact_id}`）语义一字未动，仍是会话命名空间。
"""

import hashlib
import importlib
import json
import logging
from datetime import UTC, datetime

from agent_harness.config import Settings
from agent_harness.storage.artifact import (
    ARTIFACT_ID_PATTERN,
    BYTE_ARTIFACT_ID_PATTERN,
    SESSION_KEY_PATTERN,
    Artifact,
    ArtifactSlice,
    ArtifactStore,
    BlobArtifact,
    compute_artifact_id,
    compute_byte_artifact_id,
    slice_artifact,
)
from agent_harness.storage.attachment_layout import (
    GLOBAL_BYTE_KEY_PREFIX,
    SESSION_ATTACHMENTS_DIRNAME,
)

logger = logging.getLogger("agent_harness.storage.s3_artifact")


class S3ArtifactStore(ArtifactStore):
    """绑定 Session 命名空间，无需内存索引即可恢复 artifact_id 的 key。

    **文本** artifact 的 key 是 `{session_id}/{artifact_id}`（会话命名空间，未变）；
    **字节** artifact 走全局内容寻址 + 会话回执（#933 M-01，见字节路径段）。
    """

    def __init__(self, settings: Settings, *, session_id: str) -> None:
        # 键段形态用共享定义（`storage/artifact.py`）：这条规则三个 Provider 与
        # session 层共用一份，各写一个正则字面量迟早漂移（#192 审查发现这里曾是第二份）。
        if not SESSION_KEY_PATTERN.fullmatch(session_id):
            raise ValueError("session_id must be a single safe key segment")
        # SecretStr('') 为 falsy（truthiness 基于密钥值长度）："是否已配置"
        # 判断可直接用字段本身（与 str 时代语义一致）。
        access_key = settings.artifact_store_access_key.get_secret_value()
        secret_key = settings.artifact_store_secret_key.get_secret_value()
        if not all((settings.artifact_store_endpoint, settings.artifact_store_bucket,
                    access_key, secret_key,
                    settings.artifact_store_region)):
            raise ValueError("S3ArtifactStore requires artifact_store_* configuration")
        try:
            sdk = importlib.import_module("aioboto3")
        except ModuleNotFoundError as error:
            raise RuntimeError("S3ArtifactStore requires pip install 'intelligence-agent[artifact]'") from error
        self._sdk_session = sdk.Session()
        self._client_error = importlib.import_module("botocore.exceptions").ClientError
        config_type = importlib.import_module("botocore.config").Config
        self._session_id = session_id
        self._bucket = settings.artifact_store_bucket
        self._client_kwargs = {
            "endpoint_url": settings.artifact_store_endpoint,
            "region_name": settings.artifact_store_region,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "config": config_type(signature_version="s3v4", s3={"addressing_style": "path"}),
        }

    async def save(
        self, session_id: str, content: str, *, mime_type: str,
        source_tool: str, tool_call_id: str,
    ) -> Artifact:
        if session_id != self._session_id:
            raise ValueError("save session_id must match the store namespace")
        body = content.encode("utf-8")
        artifact = Artifact(
            artifact_id=compute_artifact_id(content), session_id=session_id,
            size=len(body), mime_type=mime_type, source_tool=source_tool,
            tool_call_id=tool_call_id,
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )
        async with self._sdk_session.client("s3", **self._client_kwargs) as client:
            await client.put_object(
                Bucket=self._bucket, Key=f"{session_id}/{artifact.artifact_id}",
                Body=body, ContentType=mime_type,
                Metadata={"artifact": json.dumps(artifact.model_dump(exclude={"content"}),
                                                  ensure_ascii=True)},
            )
        return artifact

    async def load(self, artifact_id: str) -> Artifact:
        if not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise KeyError(f"Artifact '{artifact_id}' does not exist")
        async with self._sdk_session.client("s3", **self._client_kwargs) as client:
            try:
                response = await client.get_object(
                    Bucket=self._bucket, Key=f"{self._session_id}/{artifact_id}",
                )
            except self._client_error as error:
                if error.response.get("Error", {}).get("Code") == "NoSuchKey":
                    raise KeyError(f"Artifact '{artifact_id}' does not exist") from error
                raise
            async with response["Body"] as stream:
                body = await stream.read()
        # 先验哈希后解码：损坏对象可能在 decode 前就被识破（UnicodeDecodeError
        # 不外泄——错误契约统一是 KeyError，与 id 非法/对象缺失一致）。
        artifact = Artifact.model_validate_json(response["Metadata"]["artifact"])
        try:
            content = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise KeyError(
                f"Artifact '{artifact_id}' is not valid UTF-8 "
                "(object modified or corrupted out-of-band)"
            ) from error
        # 内容寻址自验证：artifact_id 就是 sha256(content)[:16]，读回时重算比对。
        # S3 对象可能被带外覆盖/截断——静默把错误内容当 artifact 交给模型违背
        # "内容寻址 = id 可验证"的承诺（不验证的 hash 只是摆设）。
        if compute_artifact_id(content) != artifact_id:
            raise KeyError(
                f"Artifact '{artifact_id}' content hash mismatch "
                "(object modified or corrupted out-of-band)"
            )
        return artifact.model_copy(update={"content": content})

    async def inspect(
        self, artifact_id: str, *, start_line: int | None = None,
        end_line: int | None = None, keyword: str | None = None, max_lines: int = 200,
        max_chars_per_line: int = 2000,
    ) -> ArtifactSlice:
        artifact = await self.load(artifact_id)
        assert artifact.content is not None
        return slice_artifact(
            artifact_id,
            artifact.content,
            start_line=start_line,
            end_line=end_line,
            keyword=keyword,
            max_lines=max_lines,
            max_chars_per_line=max_chars_per_line,
        )

    # ── 字节路径（#822 MM-01；#933 M-01 全局寻址 + 会话回执）───────────────
    #
    # 对象存储没有 hardlink，所以"全局对象 + 每会话回执"是**两份 key、同一个 body**：
    #     <bucket>/{GLOBAL_BYTE_KEY_PREFIX}/<sha[:2]>/<sha>   全局内容寻址对象
    #     <bucket>/{session_id}/attachments/<sha>             本会话回执（归属事实）
    #
    # **发布顺序：回执先、对象后**。对象存储没有跨键原子性（单 S3 API 无事务），所以
    # 必须挑一个"半途失败也无害"的顺序：回执先落 ⇒ 失败面只可能是"有回执、没对象"，
    # 而本实现的读回候选里**回执 key 就是旧的会话内对象位置**（全局化之前字节就落那儿），
    # `load_bytes` 会在全局未命中时回落到它 ⇒ 那种半成品**仍然读得回字节**、发送侧归属
    # 也照常成立。反过来（对象先落、回执后落）失败面是"有对象、没回执"：**别的会话**读得
    # 到字节、**本会话自己**发不了——归属静默失守，是最不该出现的失败面。
    #
    # 旧布局（`{session_id}/attachments/<sha>`）被双读回落兼容、**不回填**：与 Local 的
    # #830 D1 同策略（方案文档 §7），升级时不动任何既有对象。
    #
    # 来源: DSH `5badb150` `packages/attachment/attachment-local/src/store.ts:44-54`
    # （对象根 = 每用户全局 `DSH_HOME/attachments/v1`，路径无 session 段）、oh-my-pi
    # `a507b623` `packages/coding-agent/src/session/blob-store.ts:1-68`（扁平全局根，
    # docstring 逐字 "automatic deduplication across sessions"）。

    def _receipt_key(self, sha256: str) -> str:
        """本会话回执 key = 全局化之前的旧字节 key（升级兼容因此不需要额外分支）。"""
        return f"{self._session_id}/{SESSION_ATTACHMENTS_DIRNAME}/{sha256}"

    def _global_key(self, sha256: str) -> str:
        return f"{GLOBAL_BYTE_KEY_PREFIX}/{sha256[:2]}/{sha256}"

    def _byte_key_candidates(self, sha256: str) -> tuple[str, ...]:
        """读回候选 key，**顺序即优先级**（#933 M-16：布局演化只改**这一处**）。

        1. 全局内容寻址根 —— 现行布局、唯一写入落点（跨会话/fork 可寻址靠它）；
        2. 本会话回执 key —— 同时是回执位与**升级前旧对象**的落点。**本票不删**：
           删掉就会把升级前的附件全部 brick（删除前提是旧对象已迁移，见 #933 M-16 的
           审计结论与 Local 同款候选的 docstring）。

        对象存储侧没有旁挂元数据文件（ContentType 随对象走），所以候选只有一个消费点
        （`load_bytes`）；与 Local 一样，加一条候选 = 在这里加一个 key。
        """
        return (self._global_key(sha256), self._receipt_key(sha256))

    async def save_bytes(
        self, session_id: str, data: bytes, *, mime_type: str
    ) -> BlobArtifact:
        if session_id != self._session_id:
            raise ValueError("save_bytes session_id must match the store namespace")
        artifact_id = compute_byte_artifact_id(data)
        sha256 = artifact_id.split(":", 1)[1]
        blob = BlobArtifact(
            artifact_id=artifact_id,
            session_id=session_id,
            size=len(data),
            mime_type=mime_type,
        )
        async with self._sdk_session.client("s3", **self._client_kwargs) as client:
            # 顺序见本节顶注：回执先（失败面只剩"有回执、没对象"，而回执 key 本身
            # 就在读回候选里 ⇒ 那种半成品仍读得回字节）。
            await client.put_object(
                Bucket=self._bucket,
                Key=self._receipt_key(sha256),
                Body=data,
                ContentType=mime_type,
            )
            await client.put_object(
                Bucket=self._bucket,
                Key=self._global_key(sha256),
                Body=data,
                ContentType=mime_type,
            )
        return blob

    async def load_bytes(self, artifact_id: str) -> BlobArtifact:
        if not BYTE_ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise KeyError(f"Blob artifact '{artifact_id}' does not exist")
        sha256 = artifact_id.split(":", 1)[1]
        async with self._sdk_session.client("s3", **self._client_kwargs) as client:
            for key in self._byte_key_candidates(sha256):
                try:
                    response = await client.get_object(Bucket=self._bucket, Key=key)
                except self._client_error as error:
                    if error.response.get("Error", {}).get("Code") == "NoSuchKey":
                        continue
                    raise
                async with response["Body"] as stream:
                    body = await stream.read()
                if hashlib.sha256(body).hexdigest() != sha256:
                    # "存在但自证失败"（带外篡改）与"不存在"同样算该候选未命中：继续回落
                    # （本会话回执通常是同一份字节的完好副本），全部失败才 KeyError。
                    logger.warning(
                        "blob candidate %s failed content-address verification "
                        "(artifact %s)",
                        key,
                        artifact_id,
                    )
                    continue
                return BlobArtifact(
                    artifact_id=artifact_id,
                    session_id=self._session_id,
                    size=len(body),
                    mime_type=response.get("ContentType", "application/octet-stream"),
                    content=body,
                )
        raise KeyError(f"Blob artifact '{artifact_id}' does not exist")

    async def load_uploaded_bytes(self, artifact_id: str) -> BlobArtifact:
        """按字节 id 读回**本会话上传过**的字节（#933 M-01：发送侧归属判据）。

        全局化之后"读得到"不再等于"属于本会话"（别的会话/fork 子会话也读得到全局对象），
        所以归属必须另立事实：**本会话回执对象**（`save_bytes` 写的那份 key）。
        没上传过（含从未上传、属于别的会话）统一 `KeyError` → 422，与 Local 同契约。
        旧布局下这份 key 就是上传时对象的落点 ⇒ 升级前的旧附件仍是本会话的字节。
        """
        if not BYTE_ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise KeyError(f"Blob artifact '{artifact_id}' does not exist")
        sha256 = artifact_id.split(":", 1)[1]
        async with self._sdk_session.client("s3", **self._client_kwargs) as client:
            try:
                response = await client.get_object(
                    Bucket=self._bucket, Key=self._receipt_key(sha256)
                )
            except self._client_error as error:
                if error.response.get("Error", {}).get("Code") == "NoSuchKey":
                    raise KeyError(
                        f"Blob artifact '{artifact_id}' does not exist"
                    ) from error
                raise
            async with response["Body"] as stream:
                body = await stream.read()
        if hashlib.sha256(body).hexdigest() != sha256:
            raise KeyError(
                f"Blob artifact '{artifact_id}' content hash mismatch "
                "(object modified or corrupted out-of-band)"
            )
        return BlobArtifact(
            artifact_id=artifact_id,
            session_id=self._session_id,
            size=len(body),
            mime_type=response.get("ContentType", "application/octet-stream"),
            content=body,
        )
