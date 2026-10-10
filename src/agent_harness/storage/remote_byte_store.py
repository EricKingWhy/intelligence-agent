"""远端 Provider 的**字节路径**共享实现（#933 M-01；审查 P3 收口）。

## 为什么单独成模块

`S3ArtifactStore` 与 `MinioArtifactStore` 的字节路径**契约必须逐字相同**（spec 06 §9
"MinIO Provider 可替换 Local Provider"，回归面由 `tests/attachments/test_byte_store_providers.py`
的参数化组同时跑两遍钉住）。改动前这段逻辑在两个文件里各写一份（140 行逐字节相同），
违反"同一条规则只写一处"——本仓已因此吃过两次亏（`SESSION_KEY_PATTERN` 曾是第二份正则、
`artifact.py` 的 `SESSION_KEY_PATTERN`（"按 session 隔离 key"）是第二份过期口径）。

两个 Provider 的差异只有五个钩子（`_session_id` / `_sdk_session` / `_client_kwargs` / `_bucket` /
`_client_error`），全部由各自 `__init__` 提供 ⇒ 其余（key 形状、候选顺序、发布顺序、
自证、归属）收口到这里一份。

## 布局与不变量（**正本在此**；两个 Provider 各以简短注释指向本模块：s3 3 处、minio 2 处）

    全局对象（内容寻址，跨会话/fork 可寻址）: <bucket>/{GLOBAL_BYTE_KEY_PREFIX}/<sha[:2]>/<sha>
    本会话回执（归属事实）:                    <bucket>/{session_id}/attachments/<sha>

**发布顺序：回执先、对象后**。对象存储没有跨键原子性（单 S3 API 无事务），必须挑一个
"半途失败也无害"的顺序：回执先落 ⇒ 失败面只可能是"有回执、没对象"，而回执 key **就是**
全局化之前的旧对象位置（在读回候选里）⇒ 那种半成品**仍读得回字节**、发送侧归属也成立。
反过来（对象先、回执后）失败面是"有对象、没回执"：**别的会话**读得到字节、**本会话自己**
发不了——归属静默失守，是最不该出现的失败面。不变量 =「**有回执 ⇒ 有对象**」。

旧布局被双读回落兼容、**不回填**：与 Local 的 #830 D1 同策略（方案文档 §7）。

来源: DSH `5badb150` `packages/attachment/attachment-local/src/store.ts:44-54`（对象根 =
每用户全局 `DSH_HOME/attachments/v1`，路径无 session 段）、oh-my-pi `a507b623`
`packages/coding-agent/src/session/blob-store.ts:1-68`（扁平全局根，docstring 逐字
"automatic deduplication across sessions"）。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from agent_harness.storage.artifact import (
    BYTE_ARTIFACT_ID_PATTERN,
    BlobArtifact,
    compute_byte_artifact_id,
)
from agent_harness.storage.attachment_layout import (
    GLOBAL_BYTE_KEY_PREFIX,
    SESSION_ATTACHMENTS_DIRNAME,
)

logger = logging.getLogger("agent_harness.storage.remote_byte_store")


class RemoteByteStoreMixin:
    """字节路径的共享实现；宿主类提供 `_session_id` / `_sdk_session` / `_client_kwargs`
    / `_bucket` / `_client_error` 五个钩子（见模块 docstring）。"""

    #: 宿主类（`S3ArtifactStore` / `MinioArtifactStore`）在 `__init__` 里赋值的五个钩子。
    _session_id: str
    _sdk_session: Any
    _client_kwargs: dict[str, Any]
    _bucket: str
    _client_error: Any

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

    async def save_bytes(self, session_id: str, data: bytes, *, mime_type: str) -> BlobArtifact:
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
            # 顺序见模块顶注：回执先（失败面只剩"有回执、没对象"，而回执 key 本身
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
                        "blob candidate %s failed content-address verification (artifact %s)",
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
                response = await client.get_object(Bucket=self._bucket, Key=self._receipt_key(sha256))
            except self._client_error as error:
                if error.response.get("Error", {}).get("Code") == "NoSuchKey":
                    raise KeyError(f"Blob artifact '{artifact_id}' does not exist") from error
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


__all__ = ["RemoteByteStoreMixin"]
