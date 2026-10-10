"""LocalArtifactStore：artifact 的本地文件系统 Provider（spec 06 §3 的**默认** Provider）。

规范原文（06 §3）："默认 Provider：Local filesystem：开发/小型部署；MinIO：正式大文件、
大量对象。" 本模块补上这句话里从未实现的那一半——此前只有 S3 / MinIO（远端）与 Fake
（内存），于是任何**没有**配置对象存储的部署都外置不出任何 artifact：溢出处理器
（`tooling/overflow.py`）是生产里唯一写入者，而它只在 S3 分支被创建
（`assembly.py`）——结果是读取接口（#185）永远回 503，界面（#186）看不到内容。

落盘形态
--------
    <artifact_dir>/<session_id>/<artifact_id>        内容（UTF-8）
    <artifact_dir>/<session_id>/<artifact_id>.json   元数据（旁挂，可缺）

三个 Key 决定：

1. **与对象存储的 key 约定逐段同构**（`{session_id}/{artifact_id}`）：三个 Provider
   因此同形，#185 的读取接口与 `web/artifacts.py` 的 store 选择保持 Provider 无关。
2. **session 段必须是单个名字段**（`SESSION_KEY_PATTERN`）：它会被拼进文件系统路径，
   不校验时 `..` / 反斜杠段能越出 artifact 根目录。与 session 层同一条规则。
3. **元数据旁挂而可缺**：对象存储上只存得下内容与 ContentType（所以 S3/MinIO 的 load
   只能如实返回 `None`）；本地没有这个限制，就把四项元数据存下来。但旁挂文件缺失时
   **不报错**——内容还在、id 自证，元数据如实为 `None`（#185 AC4 的"缺失即 None"）。
   旁挂文件是**内容之后**写的，所以崩溃只会留下"有内容、没元数据"，不会留下
   "有元数据、没内容"的假记录。

为什么不放 workspace 底下：ADR-0027 之后 workspace 可能指向**用户的真实仓库**，
artifact 绝不能落进去——那条路径会话硬删时不许碰（ADR-0029 D2）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
from collections.abc import Callable, Iterable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import anyio

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
    BYTE_OBJECTS_DIRNAME,
    GLOBAL_ATTACHMENT_DIRNAME,
    SESSION_ATTACHMENTS_DIRNAME,
)

logger = logging.getLogger("agent_harness.storage.local_artifact")

#: 旁挂元数据的后缀。用后缀而不是把元数据塞进内容头部：内容必须**逐字节**等于
#: 模型当初产出的大输出（`compute_artifact_id` 要对得上），塞头部就会改变内容。
_META_SUFFIX = ".json"

#: 内容寻址对象的摘要形状（与 `artifact.BYTE_ARTIFACT_ID_PATTERN` 的摘要段同形）。
#: 恢复只读位时用它逐段校验回执路径（#923）——路径里嵌的 sha 既是推导依据也是形状约束。
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

#: 回执相对会话目录的固定中段（与 `_session_blob_object_path` 同形）：
#: `<session_dir>/attachments/objects/<sha[:2]>/<sha>`。由共享布局常量拼出，不写字面量
#: （#933 M-16：布局常量只此一份，见 `storage/attachment_layout.py`）。
_RECEIPT_RELATIVE_PARTS = (SESSION_ATTACHMENTS_DIRNAME, BYTE_OBJECTS_DIRNAME)

#: 发布与恢复共用的对象权限。恢复必须与 `_publish_blob` 发布时逐位一致，否则"恢复"会写在
#: 一个与发布态不同的值上。
_OBJECT_MODE = 0o400


class LocalArtifactStore(ArtifactStore):
    """本地文件系统上的 ArtifactStore（默认 Provider，零外部依赖）。"""

    def __init__(self, settings: Settings, *, session_id: str) -> None:
        root = settings.artifact_dir.strip()
        if not root:
            raise ValueError("LocalArtifactStore requires artifact_dir")
        if not SESSION_KEY_PATTERN.fullmatch(session_id):
            # 不构造带越界段的实例：错误越早、越靠近来源越好（与 path 边界同一纪律）。
            raise ValueError(
                f"session_id must be a single safe name segment: {session_id!r}"
            )
        # resolve() 不要求路径存在（strict=False 默认）：根目录在 save 时才创建。
        self._root = Path(root).resolve()
        self._session_id = session_id
        self._dir = self._root / session_id

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
        # #275：整段同步工作（encode + sha256 + 两次原子落盘）**一次**下放线程——只搬
        # `_write_atomic` 不够（encode/sha256 会留在循环上）。阈值判定见
        # `docs/PERF_BASELINE.md` B7 节。
        # 契约逐字不变：原子纪律、内容先 / 元数据后、返回形状、异常语义。
        return await anyio.to_thread.run_sync(
            self._save_blocking, session_id, content, mime_type, source_tool,
            tool_call_id,
        )

    def _save_blocking(
        self,
        session_id: str,
        content: str,
        mime_type: str,
        source_tool: str,
        tool_call_id: str,
    ) -> Artifact:
        """`save` 的同步体（原样搬运，只把 `self` 显式化；#275 未改任何一行语义）。"""
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
        # 内容与元数据都是 temp + os.replace 原子落盘（与 SessionStore / 映射表同纪律）：
        # 半写的文件会被 `load` 的 hash 校验判成损坏 → 一个正常写入的 artifact 变成
        # 永久不可读（不自愈）。同目录 rename 在两种平台都原子。
        self._dir.mkdir(parents=True, exist_ok=True)
        self._write_atomic(self._content_path(artifact.artifact_id), body)
        # 元数据直接由 `Artifact` 序列化（去掉 content）：手抄一份字段表会在 `Artifact`
        # 加字段时静默漂移（#192 批 1 审查）。
        meta = artifact.model_dump(exclude={"content"})
        self._write_atomic(
            self._meta_path(artifact.artifact_id),
            json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        )
        return artifact

    async def load(self, artifact_id: str) -> Artifact:
        # 形态校验先做（与 S3/MinIO 一致，#185 AC3）：畸形 id 不得进入路径拼接。
        if not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise KeyError(f"Artifact '{artifact_id}' does not exist")
        # #275：读 + decode + hash 自证 + 旁挂元数据**一次**下放线程——只搬
        # `read_bytes` 不够（decode/sha256 才是大头）。阈值判定见
        # `docs/PERF_BASELINE.md` B7 节。
        # 形态校验留在循环（它不碰 IO，且畸形 id 不该进线程）。
        return await anyio.to_thread.run_sync(self._load_blocking, artifact_id)

    def _load_blocking(self, artifact_id: str) -> Artifact:
        """`load` 的同步体（原样搬运；异常映射逐字未改，#275 Scope lock）。"""
        path = self._content_path(artifact_id)
        try:
            body = path.read_bytes()
        except FileNotFoundError as error:
            # not-found 是契约内的结果，不是异常路径：统一 KeyError（三实现同契约）。
            # 只接 not-found——与远端 Provider 同形状（那里也只把 NoSuchKey 映射成
            # KeyError，其余 SDK 异常照常外泄成 500）。把 PermissionError 之类也吞成
            # 404 会把"服务端异常"谎报成"不存在"。
            raise KeyError(f"Artifact '{artifact_id}' does not exist") from error
        try:
            content = body.decode("utf-8")
        except UnicodeDecodeError as error:
            # 与 S3/MinIO 同口径：损坏/被外部改动的内容不得以 UnicodeDecodeError 外泄
            # （会变 500），也不得当成合法内容——统一 KeyError → 404。
            raise KeyError(
                f"Artifact '{artifact_id}' is not valid UTF-8 "
                "(file modified or corrupted out-of-band)"
            ) from error
        # content-addressable 自证：id 就是 sha256(content)[:16]，所以"内容是否还是
        # 当初那份"不需要额外校验字段。
        if compute_artifact_id(content) != artifact_id:
            raise KeyError(
                f"Artifact '{artifact_id}' content hash mismatch "
                "(file modified or corrupted out-of-band)"
            )
        meta = self._read_meta(artifact_id)
        return Artifact(
            artifact_id=artifact_id,
            session_id=self._session_id,
            size=len(body),
            mime_type=meta.get("mime_type") or "application/octet-stream",
            # 旁挂元数据可缺（崩溃窗口 / 外部清理）：如实给 None，不伪造 ""（#185 AC4）。
            source_tool=_optional_str(meta.get("source_tool")),
            tool_call_id=_optional_str(meta.get("tool_call_id")),
            created_at=_optional_str(meta.get("created_at")),
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

    # ── 字节路径（#822 MM-01：附件入站；#830 D1：对象根全局化）─────────────────
    #
    # 落盘算法移植自 DeepSeek Harness `5badb150`
    # `packages/attachment/attachment-local/src/store.ts:214-388,431-458`（MIT）：
    # staging → fsync →
    # 原子发布（`os.link` hardlink）→ 权限收紧（`_OBJECT_MODE`）→ 目录 fsync。与文本路径的
    # `temp + os.replace` 是两个纪律：字节路径要"半途失败不产生可被读到的半文件"，
    # 靠的是**先写完整 staging 文件并 fsync，再 hardlink 到目标**——目标在任何时刻
    # 要么不存在、要么是完整对象。
    #
    # 布局（**全局**内容寻址对象根 + 会话上传回执；#830 D1）：
    #     <root>/.attachments/objects/<sha[:2]>/<sha>       字节对象（跨会话去重）
    #     <root>/.attachments/objects/<sha[:2]>/<sha>.json  对象元数据（旁挂，可缺）
    #     <root>/.attachments/tmp/<uuid>                    staging
    #     <root>/<session_id>/attachments/objects/<sha[:2]>/<sha>  会话上传回执（hardlink）
    #
    # **为什么对象根必须是全局的**（#830 MM-08 D1）：`session/fork.py` 复制的是
    # workspace，**绝不**复制附件对象（其 docstring 原文："Artifact 是全局 store 的
    # 内容寻址 ref…绝不复制"）。对象若按会话命名空间落盘，fork 出的子会话就永远
    # 读不回它自己在种子事件里继承的引用（404 + 模型侧退化成占位符）。
    # 与上游两条独立实现同构：DeepSeek Harness `attachment-local/src/store.ts:47-54`
    # （对象根 `DSH_HOME/attachments/v1`，每用户全局）、oh-my-pi
    # `packages/coding-agent/src/session/blob-store.ts:40-68`（`~/.omp/agent/blobs`
    # 全局，"Content-addressing … provides automatic deduplication across sessions"）。
    #
    # **回执是"本会话拥有这些字节"的唯一事实**：发送侧归属判据（PRD D5"属于本
    # session 上下文"）不能再用"对象读得到"（全局之后它对人人都读得到），改看回执；
    # 读回授权则仍由调用方的事件引用闸门负责（`web/attachments.py`，
    # 与 DSH `commands.ts:405-410` 的 `ATTACHMENT_NOT_REFERENCED` 同构）。
    # 回执是全局对象的 hardlink（同一 inode）⇒ 不复制字节、去重不受影响；**升级场景例外**：
    # 该会话旧路径若已有同 sha 对象（升级前布局），`os.link` 撞 `FileExistsError` ⇒
    # 保留**旧 inode**（字节与该 sha 逐字节相同，功能等价，但与全局对象不是同一 inode）。
    # 会话硬删时回执随目录一起消失，全局对象留在原地（孤儿回收见 PRD D2"v1 不做自动清理"）。

    async def save_bytes(
        self, session_id: str, data: bytes, *, mime_type: str
    ) -> BlobArtifact:
        if session_id != self._session_id:
            raise ValueError("save_bytes session_id must match the store namespace")
        return await anyio.to_thread.run_sync(
            self._save_bytes_blocking, data, mime_type
        )

    def _save_bytes_blocking(self, data: bytes, mime_type: str) -> BlobArtifact:
        artifact_id = compute_byte_artifact_id(data)
        sha256 = artifact_id.split(":", 1)[1]
        self._publish_blob(data, sha256)
        # 会话回执（#830 D1）：本会话"上传过这些字节"的事实。对象先发布、回执后建 ⇒
        # 不变量是"有回执 ⇒ 有对象"，绝不会出现回执指向不存在的对象。
        self._link_session_receipt(sha256)
        blob = BlobArtifact(
            artifact_id=artifact_id,
            session_id=self._session_id,
            size=len(data),
            mime_type=mime_type,
        )
        # 元数据是便利（读回 Content-Type），内容才是事实：所以内容先发布、元数据后写。
        # 崩溃只会留下"有内容、没元数据"（load 退化为 octet-stream），绝不会留下
        # "有元数据、没内容"的假记录。
        self._write_blob_meta(sha256, blob)
        return blob

    async def load_bytes(self, artifact_id: str) -> BlobArtifact:
        if not BYTE_ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise KeyError(f"Blob artifact '{artifact_id}' does not exist")
        return await anyio.to_thread.run_sync(self._load_bytes_blocking, artifact_id)

    async def load_uploaded_bytes(self, artifact_id: str) -> BlobArtifact:
        """只认**本会话上传过**的字节（会话回执）；#830 D1 的发送侧归属判据。"""
        if not BYTE_ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise KeyError(f"Blob artifact '{artifact_id}' does not exist")
        return await anyio.to_thread.run_sync(self._load_uploaded_bytes_blocking, artifact_id)

    def _load_bytes_blocking(self, artifact_id: str) -> BlobArtifact:
        sha256 = artifact_id.split(":", 1)[1]
        # 全局对象根优先，未命中回落本会话旧路径（#830 D1 的**向后兼容**读法：升级前
        # 落盘的对象按旧布局还在原处，不因这次改动变成"读不到的孤儿"）。
        for path in self._blob_object_candidates(sha256):
            try:
                body = path.read_bytes()
            except FileNotFoundError:
                continue
            try:
                return self._blob_from_bytes(artifact_id, sha256, body)
            except KeyError:
                # **"存在但自证失败"同样算该候选未命中**（带外篡改 / 写坏）：记 warning 后
                # 继续回落，不遮蔽同一 sha 在另一候选（本会话旧路径）里的完好副本。回落语义
                # 覆盖"不存在"与"存在但损坏"两种未命中，全部候选都失败才 KeyError。
                logger.warning(
                    "blob candidate %s failed content-address verification "
                    "(artifact %s)",
                    path,
                    artifact_id,
                )
                continue
        # not-found 是契约内的结果（从未上传 / 本部署里查无此对象 / 全部候选都自证失败），
        # 统一 KeyError → 404；不把"服务端异常"谎报成"不存在"。**归属**不在这里判——那是
        # 调用方的事件引用闸门（读回）与会话回执（发送）的事，见本类字节路径段注释。
        raise KeyError(f"Blob artifact '{artifact_id}' does not exist")

    def _load_uploaded_bytes_blocking(self, artifact_id: str) -> BlobArtifact:
        sha256 = artifact_id.split(":", 1)[1]
        path = self._session_blob_object_path(sha256)
        try:
            body = path.read_bytes()
        except FileNotFoundError as error:
            # 没上传过（或属于别的会话）：统一 KeyError，与"从未上传"不可区分。
            raise KeyError(f"Blob artifact '{artifact_id}' does not exist") from error
        return self._blob_from_bytes(artifact_id, sha256, body)

    def _blob_from_bytes(self, artifact_id: str, sha256: str, body: bytes) -> BlobArtifact:
        """字节 → `BlobArtifact`（内容寻址自证 + 旁挂元数据；三条读路径共用一件）。"""
        if hashlib.sha256(body).hexdigest() != sha256:
            raise KeyError(
                f"Blob artifact '{artifact_id}' content hash mismatch "
                "(file modified or corrupted out-of-band)"
            )
        meta = self._read_blob_meta(sha256)
        return BlobArtifact(
            artifact_id=artifact_id,
            session_id=self._session_id,
            size=len(body),
            mime_type=meta.get("mime_type") or "application/octet-stream",
            content=body,
        )

    # 路径构造：全局对象根（新写入的唯一落点）与会话命名空间（回执 / 升级前旧对象）。

    def _global_blob_objects_dir(self) -> Path:
        return self._root / GLOBAL_ATTACHMENT_DIRNAME / BYTE_OBJECTS_DIRNAME

    def _global_blob_staging_dir(self) -> Path:
        return self._root / GLOBAL_ATTACHMENT_DIRNAME / "tmp"

    def _global_blob_object_path(self, sha256: str) -> Path:
        return self._global_blob_objects_dir() / sha256[:2] / sha256

    def _global_blob_meta_path(self, sha256: str) -> Path:
        """**写**侧旁挂路径（读侧走 `_blob_object_candidates` 派生的那份清单）。"""
        return self._global_blob_objects_dir() / sha256[:2] / f"{sha256}.json"

    def _session_blob_object_path(self, sha256: str) -> Path:
        """本会话命名空间里的对象路径 = 上传回执位 / 升级前旧对象的落点。"""
        return (
            self._dir
            / SESSION_ATTACHMENTS_DIRNAME
            / BYTE_OBJECTS_DIRNAME
            / sha256[:2]
            / sha256
        )

    def _blob_object_candidates(self, sha256: str) -> tuple[Path, ...]:
        """读回候选路径，**顺序即优先级**（#933 M-16：布局演化只改**这一处**）。

        候选是"同一 sha 可能出现在哪几个历史布局里"的**清单**，读回（`_load_bytes_blocking`）
        与旁挂元数据读（`_read_blob_meta`）都从它派生——此前两者各写一份路径元组，加一条
        候选就会出现"对象读得到、旁挂读不到"的静默错配（正是 M-16 说的不可扩展）。

        逐条：

        1. 全局内容寻址根（`_global_blob_object_path`，`GLOBAL_ATTACHMENT_DIRNAME` 之下）——
           **现行布局，唯一写入落点**（#830 D1）；
        2. 本会话命名空间（`_session_blob_object_path`）——**升级兼容包袱**：它同时是
           回执位与 #830 D1 之前旧对象的落点。**本票不删**：旧对象只在这里，删掉候选
           就会把升级前的附件全部 brick；删除前提是"旧附件已迁移"（迁移需要有回填/双读
           窗口的独立票，方案文档 §7/§8.3 已登记）。

        **扩展方式**（本方法的全部意义）：加一条候选 = 在返回元组里插一个路径，读回与
        旁挂元数据两侧同时生效，无需改动任何消费点。
        """
        return (self._global_blob_object_path(sha256), self._session_blob_object_path(sha256))

    def _read_blob_meta(self, sha256: str) -> dict:
        """读旁挂元数据：**候选与对象读同源**（#933 M-16），全局优先；都缺或损坏返回 `{}`。

        旁挂路径由候选路径派生（`.json` 后缀），不另列一份清单——两条清单会漂移，
        而"对象读得到、旁挂读不到"只会静默退化成 octet-stream（症状最轻、最难发现）。
        """
        for candidate in self._blob_object_candidates(sha256):
            path = candidate.with_name(f"{candidate.name}.json")
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(raw, dict):
                return raw
        return {}

    def _write_blob_meta(self, sha256: str, blob: BlobArtifact) -> None:
        """写**全局**对象旁挂元数据（按 sha 单一文件）。

        `session_id` **剥离**：全局对象跨会话去重、每个 sha 只有一份旁挂，写上传者会话 id
        只会被后上传者覆盖 ⇒ 该字段对全局对象无意义（会误导读者以为有单一归属）。消费侧
        （`_blob_from_bytes` / `_read_blob_meta`）只读 `mime_type`，而 mime 由内容派生
        ⇒ 同字节同值，剥离无行为影响。
        """
        target = self._global_blob_meta_path(sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(
            target,
            json.dumps(
                blob.model_dump(exclude={"content", "session_id"}), ensure_ascii=False
            ).encode("utf-8"),
        )

    def _link_session_receipt(self, sha256: str) -> None:
        """在本会话命名空间建一条回执 hardlink，指向全局对象（不复制字节）。幂等。

        **升级场景**：该会话旧路径下若已存在同 sha 的升级前旧对象，`os.link` 抛
        `FileExistsError`，此时**保留旧 inode**（不改动、不收敛）——旧对象的字节与该 sha
        逐字节相同，读/归属行为等价；差别只是它与全局对象不是同一 inode。故"回执就是全局
        对象的 hardlink"这条不变量**只在本次写入新建回执时成立**。
        """
        target = self._global_blob_object_path(sha256)
        receipt = self._session_blob_object_path(sha256)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(target, receipt)
        except FileExistsError:
            # 回执位已被占用：同会话重复上传同一字节（幂等返回），或升级前旧对象占位
            # （保留旧 inode，见 docstring）。两种情形都无需再动磁盘。
            return
        # 回执是**授权事实**，必须比 HTTP 回执先落稳：目录项也要 fsync。
        self._sync_blob_dirs(receipt.parent)

    def _publish_blob(self, data: bytes, sha256: str) -> None:
        """staging → fsync → **读回自证** → hardlink 原子发布（全局对象根）。

        自证的目的（#933 M-04）："**落盘字节** == 预期摘要"。所以校验必须读回**暂存文件**
        再算摘要（来源: DSH `attachment-local/src/store.ts:390-394` `digestFile`——它读文件
        算摘要，不是拿内存里那份再算一遍）。改动前这里是
        `hashlib.sha256(data).hexdigest() != sha256`：`sha256` 由同一份内存 `data` 派生
        （`_save_bytes_blocking` 的 `compute_byte_artifact_id(data)`），恒真——一道**从未
        工作过**的检查。删掉它不加读回检查，才是真的退步；两者是同一件事的"假版本/真版本"。

        失败面：平台把字节写坏（Windows MSVCRT 文本模式 LF→CRLF、部分写、被截断）时，
        自证在**发布前**抛 `OSError`，目标位置不会出现坏对象——而不是等读回时才发现
        `content hash mismatch`（那时对象已发布、噪声更大、也更难归因）。这也是
        `O_BINARY`（见下）的道具之外的第二道防线。
        """
        staging_dir = self._global_blob_staging_dir()
        staging_dir.mkdir(parents=True, exist_ok=True)
        target = self._global_blob_object_path(sha256)
        temporary = staging_dir / uuid4().hex
        handle: int | None = None
        try:
            # `O_BINARY` 只在 Windows 存在（POSIX 上 `getattr` 取 0，逐位或等价于无操作）：
            # 缺它时 MSVCRT 会把二进制流当文本处理——0x0A 撑成 0x0D 0x0A 使对象字节数变化 ⇒
            # content-addressable 自证 hash mismatch（#830 D2），0x1A 还会被当作提前 EOF。
            # 加它让落盘按字节精确。
            handle = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
            # `os.write` 不保证一次写完（大对象必然部分写）——循环写完。
            view = memoryview(data)
            written = 0
            while written < len(view):
                written += os.write(handle, view[written:])
            # **读回自证**（#933 M-04）：关句柄前先 flush 到内核再读回文件字节——`os.read`
            # 看到的就是后续 `os.link`/读者会拿到的那份字节（同 inode、无中间缓冲）。
            os.fsync(handle)
            with open(temporary, "rb") as staged:
                if hashlib.file_digest(staged, "sha256").hexdigest() != sha256:
                    raise OSError("staged bytes do not match their publication digest")
            os.close(handle)
            handle = None

            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(temporary, target)
            except FileExistsError:
                # 内容寻址去重：别的写入者已发布同一对象。校验已有对象完整即接受，
                # 否则拒绝（不把损坏对象当成"已存在"）。
                if hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
                    raise OSError(
                        "stored blob failed integrity verification"
                    ) from None
            finally:
                with suppress(OSError):
                    os.unlink(temporary)
            os.chmod(target, _OBJECT_MODE)
            self._sync_blob_dirs(target.parent)
        except BaseException:
            if handle is not None:
                with suppress(OSError):
                    os.close(handle)
            with suppress(OSError):
                os.unlink(temporary)
            raise

    def _sync_blob_dirs(self, start: Path) -> None:
        """从对象父目录向上 fsync 到 artifact 根：目录项也要落盘，重启才找得到。"""
        if os.name == "nt":  # Windows 打不开目录句柄，NTFS 元数据日志负责目录项
            return
        level = start
        stop = self._root
        while True:
            self._fsync_dir(level)
            if level == stop or level.parent == level:
                break
            level = level.parent

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # —— 内部方法 ——

    def _content_path(self, artifact_id: str) -> Path:
        return self._dir / artifact_id

    def _meta_path(self, artifact_id: str) -> Path:
        return self._dir / f"{artifact_id}{_META_SUFFIX}"

    def _read_meta(self, artifact_id: str) -> dict:
        """读旁挂元数据；缺失或损坏都返回 `{}`（内容才是事实，元数据是便利）。"""
        path = self._meta_path(artifact_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _write_atomic(path: Path, body: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
        try:
            tmp.write_bytes(body)
            os.replace(tmp, path)
        finally:
            with suppress(OSError):
                tmp.unlink()


def _optional_str(value: object) -> str | None:
    """元数据字段 → `str | None`：非字符串一律 None（不把类型错误伪装成空串）。"""
    return value if isinstance(value, str) and value else None


def _read_only_retry_handler(cleared_receipts: set[Path]) -> Callable[..., None]:
    """构造 `shutil.rmtree` 的错误回调：清只读位后重试一次删除（#916），并登记改过位的路径。

    注册方式按解释器版本分支，见 `_rmtree_clearing_read_only`：两个分支（`onexc` 收异常
    实例 / `onerror` 收 `sys.exc_info()` 三元组）**共用本工厂产出的同一个回调**——不读第三个
    参数的是内层 `_handler`（形参 `_exc_info`）；工厂本身没有第三个形参，所以版本差异不进
    这里，cleared_receipts 的记录逻辑也就不存在"只修了一条分支"。

    `_publish_blob` 发布的对象是只读的（`os.chmod(target, _OBJECT_MODE)`），而会话回执是它的
    hardlink ⇒ Windows 上回执与全局对象共享同一只读属性，`os.unlink` 抛
    `PermissionError`；改动前 `rmtree(..., ignore_errors=True)` 会把它静默吞掉，回执因此
    残留。POSIX 上 unlink 不受只读位约束（只看目录写权限），本回调不会被触发。

    只对 `os.unlink` / `os.rmdir` 重试：rmtree 对 `os.open` 等内部操作传的 func 需要更多
    参数，盲目 `func(path)` 会抛 `TypeError`；其余情况直接返回（不抛）。重试仍失败时静默
    放弃——保持改动前 best-effort 的"会话删除不因残留文件而崩溃"语义。

    清位用"原 mode **或上**写位"而不是 `stat.S_IWRITE`：后者把 mode 归一成 0o200（只写），
    在 POSIX 上会连**读**权限一起抹掉（hardlink 共享 inode ⇒ 全局对象也变不可读）。只加写位
    对 Windows 同样有效（`os.chmod` 只认写位来清 FILE_ATTRIBUTE_READONLY）。

    **副作用（Windows 固有，非本回调引入；本段是这一因果的权威叙述）**：hardlink 共享文件
    属性，清回执的只读位会同时清掉其全局对象的只读位——这是删除只读 hardlink 在 Windows 上
    无法回避的代价。**#923 起由调用方收尾**：回调把"确实清过位的只读**文件**回执"路径记进
    `cleared_receipts`，`discard_local_artifacts` 在删除结束后据此恢复对应全局对象的只读位。

    `cleared_receipts` 是**单次 discard 调用的局部状态**：不做全量扫描，也不跨调用累积。

    保留为工厂而非内联进 `_rmtree_clearing_read_only` 的唯一原因是**测试需要注入 spy**
    （`test_discard_callback_receives_version_appropriate_exc` monkeypatch 本符号）；生产调用
    点唯一。
    """

    def _handler(func: Callable[..., object], path: str, _exc_info: object) -> None:
        if func not in (os.unlink, os.rmdir):
            # 改位与重试**同域**：只对可单参重试的两个 func 清只读位。对 `os.open` 等其余
            # func 触发时不碰权限（它们需要更多参数、无法重试，改位会是纯粹的越界副作用）。
            return
        with suppress(OSError):
            os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
            if func is os.unlink:
                # 只登记 `os.unlink`：能共享对象只读属性的只有**文件** hardlink。`os.rmdir`
                # 分支上面那行 chmod 照样执行（否则清位后重试删不掉目录），但它清的是目录的
                # 写位，目录不是对象、不共享全局对象的属性 ⇒ 不登记（形状校验另有一道）。
                cleared_receipts.add(Path(path))
        with suppress(OSError):
            func(path)

    return _handler


def _global_object_for_receipt(receipt: Path, session_dir: Path, root: Path) -> Path | None:
    """回执路径 → 它指向的全局对象路径；形状不符或越界时返回 `None`（纵深防御）。

    回执路径里嵌着 sha256，所以可机械推导：`<session_dir>/attachments/objects/<sha[:2]>/<sha>`
    → `<root>/.attachments/objects/<sha[:2]>/<sha>`。逐段校验形状并 `relative_to`（拒绝 `..`），
    推导结果还必须仍在 `<root>/.attachments/objects/` 之下——这段校验是信任边界，不随"路径
    是 harness 自己拼的"而省略（同 `discard_local_artifacts` 重校验 session_id 的理由）。
    """
    try:
        relative = receipt.relative_to(session_dir)
    except ValueError:
        return None
    parts = relative.parts
    # 中段与 `_RECEIPT_RELATIVE_PARTS` 同源（#933 M-16）：把中段长度当偏移量推导，
    # 而不是写死 `parts[2], parts[3]`——改布局常量时这里跟着走，不会静默错位。
    offset = len(_RECEIPT_RELATIVE_PARTS)
    if len(parts) != offset + 2 or parts[:offset] != _RECEIPT_RELATIVE_PARTS:
        return None
    shard, sha256 = parts[offset], parts[offset + 1]
    if shard != sha256[:2] or not _SHA256_PATTERN.fullmatch(sha256):
        return None
    objects_dir = (root / GLOBAL_ATTACHMENT_DIRNAME / BYTE_OBJECTS_DIRNAME).resolve()
    derived = (objects_dir / shard / sha256).resolve()
    if not derived.is_relative_to(objects_dir):
        return None
    return derived


def _rmtree_clearing_read_only(path: Path, cleared_receipts: set[Path]) -> None:
    """删除目录树，只读文件清位后重试一次（#916）。

    `onerror` 在 Python 3.12 被 `onexc` 取代（CPython `Lib/shutil.py` 起把 `onerror`
    委托给 `onexc`），这里按版本选参数名，形状同 pip
    `src/pip/_internal/utils/misc.py:160-164`（`if sys.version_info >= (3, 12)`）。
    两分支共用 `_read_only_retry_handler` 产出的回调（内层 `_handler` 不读第三个参数）
    ⇒ 不存在逻辑分叉。

    版本判定读在**调用时**的 `sys.version_info`：import 时求值会写死在模块里，
    测试注入替身（以及将来冻进旧解释器的构建）都改不动。

    `cleared_receipts` 由调用方持有（见 `_read_only_retry_handler`）：回调在本轮删除中实际清过
    只读位的回执路径会登记进去，供删除结束后恢复全局对象的只读位（#923）。
    """
    handler = _read_only_retry_handler(cleared_receipts)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=handler)
    else:
        shutil.rmtree(path, onerror=handler)


def _restore_object_read_only(receipt: Path, session_dir: Path, root: Path) -> None:
    """把 `receipt` 指向的全局字节对象恢复成发布时的只读态（#923）。best-effort。

    只对**确实被清过只读位**的回执调用（调用方据 `cleared_receipts` 驱动），失败一律
    `suppress(OSError, RuntimeError)`——对象可能已被并发删除、权限不够、父目录不可写，以及
    `_global_object_for_receipt` 里 `.resolve()` 撞上符号链接环（抛 `RuntimeError`，**不是**
    `OSError`），都不许让会话硬删崩溃（保持 `discard_local_artifacts` 原有的 best-effort 语义）。
    清位副作用的因果见 `_read_only_retry_handler` docstring。

    **升级场景例外**（`_link_session_receipt` docstring）：旧布局下回执可能与全局对象**不是同一
    inode**（`os.link` 撞 `FileExistsError` 时保留了旧 inode）。此时清回执的只读位根本没波及对象，
    但按 sha 推导出的全局对象路径照常恢复——对已是只读的对象重复 `chmod` 无害，故不为旧 inode
    加特殊分支（Scope Lock）。
    """
    with suppress(OSError, RuntimeError):
        object_path = _global_object_for_receipt(receipt, session_dir, root)
        if object_path is None:
            return
        os.chmod(object_path, _OBJECT_MODE)


def discard_local_artifacts(settings: Settings, session_id: str) -> None:
    """会话硬删时丢弃该会话的**本地** artifact 目录。幂等。

    窄方法，沿用 `WorkspaceRegistry.discard_session_artifacts` 的既有模式
    （ADR-0029 D2）：**不读映射**、只删"用 setting + session_id 自己拼出来的路径"。
    这里的路径完全由 `settings.artifact_dir` 与 session_id 拼成，只有 harness 会往里写，
    所以删除判定是"写死的构造规则"，碰不到用户目录——这正是 ADR-0029 D2 要求的安全形状。

    **三条刻意不做的**（记录在案，不在本票范围）：
    - 配置了 S3/MinIO 时**远端对象不删**（那些 Provider 没有 delete）；
    - 不做任何自动 TTL / 体积清理（ADR-0004 明确不做自动 TTL；ADR-0029 Non-Goals 同款）。
      文本 artifact 仍满足旧口径"生命周期 = 会话生命周期：只要会话还在，它的引用就必然可解析"；
    - **#830 D1 起，附件字节对象的回收已从"会话删除"语义中剥离**：字节对象落在全局内容
      寻址根（`<root>/.attachments/objects/…`，跨会话去重），本函数只删 `<root>/<session_id>`
      （连带回执），**全局字节对象留在原地、不随会话删除回收**。这是相对 D1 之前的行为回归
      （旧布局下字节对象在会话目录内、随目录一起删），当前**刻意接受**（设计文档 §8.2 / PRD D2
      "v1 不做自动清理"）；后续 GC（引用计数 / 宽限期；DSH 有 `gc`、oh-my-pi 有 `omp gc`）
      是独立的 follow-up 票，不由本函数承担。别把这里的"幂等删除"读成"字节也被回收了"。

    Windows 上删除只读回执会连带清掉其全局对象的只读位（hardlink 共享属性 ⇒ 副作用原文见
    `_read_only_retry_handler` docstring），本函数在删除结束后把本轮实际清过位的回执恢复成
    发布时的只读态（#923，见 `_restore_object_read_only`）；恢复是 best-effort，失败不影响
    会话删除本身。
    """
    root = settings.artifact_dir.strip()
    if not root:
        return
    if not SESSION_KEY_PATTERN.fullmatch(session_id):
        # 硬删入口已校验过 id 形态（422），这里再挡一次：本函数只允许删自己拼得出的路径。
        return
    root_path = Path(root).resolve()
    session_dir = root_path / session_id
    if session_dir.is_dir():
        # 回执是只读对象的 hardlink：Windows 上直接 rmtree 删不掉（旧的 ignore_errors
        # 会静默吞掉、留下回执）。改用清只读位后重试的错误回调，
        # 见 `_rmtree_clearing_read_only` / `_read_only_retry_handler`。
        cleared_receipts: set[Path] = set()
        _rmtree_clearing_read_only(session_dir, cleared_receipts)
        # 收尾（#923）：本轮清过只读位的回执背后是**共享同一属性**的全局字节对象，删完把它们
        # 恢复成发布时的只读态（清位副作用的因果见 `_read_only_retry_handler` docstring）。
        # POSIX 上 unlink 不看只读位 ⇒ cleared_receipts 为空 ⇒ no-op。
        for receipt in cleared_receipts:
            _restore_object_read_only(receipt, session_dir, root_path)


def delete_local_artifacts(
    settings: Settings, session_id: str, artifact_ids: Iterable[str]
) -> dict[str, list[str]]:
    """按 id 精确删除该会话的**本地** artifact（内容 + 旁挂元数据）。幂等。

    与 `discard_local_artifacts` 同一条安全形状（ADR-0029 D2）：**不读映射**，只删
    "用 setting + session_id + artifact_id 自己拼出来的路径"。路径全由
    `settings.artifact_dir` 与两个已校验的名字段拼成，只有 harness 会往里写，所以删除
    判定是写死的构造规则，碰不到用户目录——`agent-progress/` 与工作目录源码绝不碰。

    返回 `{"deleted", "not_found", "invalid", "failed"}` 四个 list，保持输入顺序：

    - session_id 不合 `SESSION_KEY_PATTERN` → 所有 id 记 `invalid`，直接返回（不拼路径）；
    - 单个 id 不合 `ARTIFACT_ID_PATTERN` → `invalid`；
    - 内容文件不存在 → `not_found`（幂等：删第二次走这里）；
    - 存在 → 删内容 + 同名 `.json` 旁挂（旁挂缺失也接受，幂等），成功 → `deleted`；
      任一 `OSError` → `failed`，记下后继续处理下一个，不中断整批。

    **不在本函数范围**：配置了 S3/MinIO 时的远端对象（那些 Provider 没有 delete），
    另见 ADR-0029 D6 单独开票。
    """
    result: dict[str, list[str]] = {
        "deleted": [],
        "not_found": [],
        "invalid": [],
        "failed": [],
    }
    root = settings.artifact_dir.strip()
    if not root or not SESSION_KEY_PATTERN.fullmatch(session_id):
        # 拼不出安全路径就不动手：所有 id 如实记 invalid（纵深防御）。
        result["invalid"].extend(artifact_ids)
        return result
    session_dir = Path(root).resolve() / session_id
    for artifact_id in artifact_ids:
        if not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            result["invalid"].append(artifact_id)
            continue
        content_path = session_dir / artifact_id
        if not content_path.exists():
            result["not_found"].append(artifact_id)
            continue
        try:
            content_path.unlink()
        except FileNotFoundError:
            # 检查与删除之间的竞态：别人先删了 → 如实记 not_found（幂等）。
            result["not_found"].append(artifact_id)
            continue
        except OSError:
            # 单个失败不拖垮整批：记下继续（调用方据 failed 对账）。
            result["failed"].append(artifact_id)
            continue
        with suppress(FileNotFoundError):
            (session_dir / f"{artifact_id}{_META_SUFFIX}").unlink()
        result["deleted"].append(artifact_id)
    return result
