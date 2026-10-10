"""MinIO-backed ArtifactStore for externalized large tool results.

Phase Multiturn T5 (#135): when a tool result exceeds the overflow threshold,
the raw content is externalized to MinIO (S3-compatible). The session keeps
only a truncated summary + artifact_ref. The model can read back local slices
via the ``read_artifact`` tool.

Design notes:
- Content-addressable: ``artifact_id = sha256(content)[:16]``.
- Verification on load: recomputes the hash and compares.
- Graceful degradation: if MinIO is unreachable during ``save``, the handler
  falls back to keeping the original (untruncated) tool result in-session.

Key 布局分两套（**文本**仍是会话命名空间，**字节**在 #933 M-01 之后不是）：

- 文本 artifact：``{session_id}/{artifact_id}``（会话命名空间，未变）；
- 字节 artifact：全局内容寻址对象 ``.attachments/objects/<sha[:2]>/<sha>``
  ＋ 每会话回执对象 ``{session_id}/attachments/<sha>``（归属事实）。
  见本文件字节路径段注释。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

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

logger = logging.getLogger("agent_harness.storage.minio_artifact")


class MinioArtifactStore(ArtifactStore):
    """S3-compatible store backed by MinIO for externalized large tool results.

    Unlike the generic ``S3ArtifactStore``, this store is purpose-built for
    the T5 overflow path: it stores raw tool output under a session-scoped
    key and verifies content integrity on load.
    """

    def __init__(self, settings: Settings, *, session_id: str) -> None:
        # 键段形态与 S3/Local 共用一份定义（`storage/artifact.py`）：三个 Provider 的键
        # 都是 `{session_id}/{artifact_id}`，"同一条规则"不该有一处不校验（#192 批 1 审查）。
        if not SESSION_KEY_PATTERN.fullmatch(session_id):
            raise ValueError("session_id must be a single safe key segment")
        access_key = settings.minio_access_key.get_secret_value()
        secret_key = settings.minio_secret_key.get_secret_value()
        if not all(
            (
                settings.minio_endpoint,
                settings.minio_bucket,
                access_key,
                secret_key,
            )
        ):
            raise ValueError("MinioArtifactStore requires minio_* configuration")
        try:
            sdk = __import__("aioboto3")
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "MinioArtifactStore requires pip install 'intelligence-agent[artifact]'"
            ) from error
        self._sdk_session = sdk.Session()
        self._client_error = __import__(
            "botocore.exceptions", fromlist=["ClientError"]
        ).ClientError
        config_type = __import__("botocore.config", fromlist=["Config"]).Config
        self._session_id = session_id
        self._bucket = settings.minio_bucket
        self._client_kwargs: dict[str, Any] = {
            "endpoint_url": settings.minio_endpoint,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "config": config_type(
                signature_version="s3v4", s3={"addressing_style": "path"}
            ),
        }

    async def save(
        self,
        session_id: str,
        content: str,
        *,
        mime_type: str,
        source_tool: str,
        tool_call_id: str,
    ) -> Artifact:
        if session_id != self._session_id:
            raise ValueError("save session_id must match the store namespace")
        body = content.encode("utf-8")
        artifact = Artifact(
            artifact_id=compute_artifact_id(content),
            session_id=session_id,
            size=len(body),
            mime_type=mime_type,
            source_tool=source_tool,
            tool_call_id=tool_call_id,
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )
        async with self._sdk_session.client("s3", **self._client_kwargs) as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=f"{session_id}/{artifact.artifact_id}",
                Body=body,
                ContentType=mime_type,
            )
        return artifact

    async def load(self, artifact_id: str) -> Artifact:
        # 形态校验必须与 S3ArtifactStore 一致（#185 AC3）：此前这里直接拿 id 拼 key
        # 去请求对象存储，畸形 id 会以 SDK 异常的形状外泄——错误契约不统一。
        # 与 S3 一致地先判形态，再走网络，统一以 KeyError 表示"不存在/不可读"。
        if not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise KeyError(f"Artifact '{artifact_id}' does not exist")
        async with self._sdk_session.client("s3", **self._client_kwargs) as client:
            try:
                response = await client.get_object(
                    Bucket=self._bucket,
                    Key=f"{self._session_id}/{artifact_id}",
                )
            except self._client_error as error:
                # 与 S3ArtifactStore 一致：对象不存在是**契约内的 not-found**，统一成
                # KeyError；否则它会以 SDK 异常的形状漏到 HTTP 层变成 500（#185 审查 P1）。
                if error.response.get("Error", {}).get("Code") == "NoSuchKey":
                    raise KeyError(f"Artifact '{artifact_id}' does not exist") from error
                raise
            async with response["Body"] as stream:
                body = await stream.read()
        try:
            content = body.decode("utf-8")
        except UnicodeDecodeError as error:
            # 与 S3 同口径：损坏/截断的对象不得以 UnicodeDecodeError 外泄（会变 500），
            # 也不得静默当成合法内容——统一 KeyError → 404。
            raise KeyError(
                f"Artifact '{artifact_id}' is not valid UTF-8 "
                "(object modified or corrupted out-of-band)"
            ) from error
        # Content-addressable verification: artifact_id is sha256(content)[:16].
        if compute_artifact_id(content) != artifact_id:
            raise KeyError(
                f"Artifact '{artifact_id}' content hash mismatch "
                "(object modified or corrupted out-of-band)"
            )
        return Artifact(
            artifact_id=artifact_id,
            session_id=self._session_id,
            size=len(body),
            mime_type=response.get("ContentType", "application/octet-stream"),
            # save 未持久化这三项（对象上只有内容和 ContentType），所以 load 无法恢复：
            # 如实给 None，而不是伪造 ""（#185 AC4）。
            source_tool=None,
            tool_call_id=None,
            created_at=None,
            content=content,
        )

    async def inspect(
        self,
        artifact_id: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
        keyword: str | None = None,
        max_lines: int = 200,
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
