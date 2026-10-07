"""#822 / MM-01：附件入站 REST 传输面（独立 router）。

三个职责，语义单源在 `attachments/` 与 `storage/`：

- `POST /api/sessions/{session_id}/attachments`（octet-stream 流式上传）——
  原始字节进得来、按字节判 MIME、内容寻址落盘，回不透明 `sha256:<hex>` id；
- `GET /api/sessions/{session_id}/attachments/{attachment_id}/content`——
  受控读回原始字节 + `Content-Type`；只认**本会话**命名空间里的 id，别的会话 /
  未上传过的 id 一律 404（不泄露存在性）。

**授权口径（MM-01）**：读回要求 id 属于 URL 里的 session（store 按 session 构造、
provider 把 session 拼进键或路径）。DSH 的 `ATTACHMENT_NOT_REFERENCED` 是"被本
session 事件引用"，本仓把"引用"前移到了**上传即归属本会话**——MM-01 不往
`user/message` 写附件引用（那是 MM-02），故上传的会话归属就是本票的授权单位。

**绕开 1 MiB JSON body 上限**：上传请求体可达单张图上限（默认 20 MiB）。该配额
针对 JSON 端点，由 `BodyDepthGuardMiddleware` 全局强制；本模块导出
`ATTACHMENT_UPLOAD_PATH_RE`，`create_app` 把它交给中间件的**豁免**参数——只有
本上传路由绕过，其它 JSON 端点的 1 MiB / 深度行为逐字不变（回归测试钉住）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from pydantic import BaseModel
from starlette.responses import Response

from agent_harness.attachments import (
    EXTENSION_MEDIA_TYPES,
    SUPPORTED_IMAGE_MEDIA_TYPES,
    AttachmentError,
    attachment_http_status,
    detect_image,
    file_leaf_name,
    resolve_image_limits,
)
from agent_harness.session.errors import InvalidSessionId, SessionNotFound
from agent_harness.storage.artifact import BYTE_ARTIFACT_ID_PATTERN
from agent_harness.storage.artifact_select import select_artifact_store
from agent_harness.web.app import session_service
from agent_harness.web.domain_errors import http_error

if TYPE_CHECKING:
    from fastapi import FastAPI

    from agent_harness.config import Settings
    from agent_harness.storage.artifact import ArtifactStore

#: 上传路由路径模板（与 `register_attachment_routes` 里注册的路径同源）。
ATTACHMENT_UPLOAD_PATH = "/api/sessions/{session_id}/attachments"

#: 上传路由的**路径匹配**（给 BodyDepthGuardMiddleware 做 1 MiB / 深度豁免）。
#: `{session_id}` 只接受单个安全名字段（无 `/`），故用 `[^/]+` 精确匹配上传路径、
#: **不**匹配读回路径（`.../attachments/<id>/content`）。
ATTACHMENT_UPLOAD_PATH_RE = re.compile(r"^/api/sessions/[^/]+/attachments/?$")

#: 存储不可用时（`artifact_dir` 置空且无对象存储）的机读码（#227 同款形状）。
ATTACHMENT_STORAGE_UNAVAILABLE = "attachment_storage_unavailable"


class AttachmentUploadResponse(BaseModel):
    """上传成功回执（#822 AC 的固定形状）。"""

    attachment_id: str
    media_type: str
    bytes: int
    width: int
    height: int
    name: str | None = None


def _build_attachment_store(
    settings: Settings, session_id: str
) -> ArtifactStore | None:
    """按会话构造 store（与文本 artifact 共用**唯一**选择器，不另写 if 级联）。"""
    selection = select_artifact_store(settings, session_id)
    return None if selection is None else selection.store


def _attachment_http_error(error: AttachmentError) -> HTTPException:
    """附件接纳失败 → HTTPException（状态码由 `attachments.errors` 单点映射）。"""
    return HTTPException(
        status_code=attachment_http_status(error.code),
        detail=f"{error.code}: {error}",
    )


async def _read_body_bounded(request: Request, max_bytes: int) -> bytes:
    """流式读取请求体，超过单张字节上限即抛 `IMAGE_TOO_LARGE`（→ 413）。

    用累计字节而不是 `Content-Length`（可缺失 / 可撒谎），越界处**即时停读**，
    不把超大 body 整个缓冲完。
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise AttachmentError(
                f"图片超过单张字节上限（{max_bytes} 字节）。", "IMAGE_TOO_LARGE"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _declared_image_media_type(request: Request) -> str | None:
    """请求 `Content-Type` 里声明的图片类型；非图片类型 / 缺省 → None。"""
    header = request.headers.get("content-type", "")
    declared = header.split(";", 1)[0].strip().lower()
    return declared if declared in SUPPORTED_IMAGE_MEDIA_TYPES else None


def _check_declared_matches(
    request: Request, name: str | None, detected_media_type: str
) -> None:
    """声明（Content-Type / 文件名扩展名）与字节判定不符 → 拒绝。

    客户端声明不是权威（#822 AC）：不一致时以**字节**为准，且不是静默采用字节，
    而是明确拒绝（避免"我以为是 PNG"这类认知偏差被吞掉）。
    """
    declared = _declared_image_media_type(request)
    if declared is not None and declared != detected_media_type:
        raise AttachmentError(
            f"声明的类型 {declared!r} 与字节判定 {detected_media_type!r} 不符。",
            "IMAGE_TYPE_MISMATCH",
        )
    if name:
        leaf = file_leaf_name(name)
        dot = leaf.rfind(".")
        extension = leaf[dot:].lower() if dot >= 0 else ""
        extension_type = EXTENSION_MEDIA_TYPES.get(extension)
        if extension_type is not None and extension_type != detected_media_type:
            raise AttachmentError(
                f"文件名 {leaf!r} 的扩展名与字节判定"
                f" {detected_media_type!r} 不符。",
                "IMAGE_TYPE_MISMATCH",
            )


def _unavailable_storage_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": ATTACHMENT_STORAGE_UNAVAILABLE,
            "message": "本部署没有可用的附件存储（artifact_dir 为空，或对象存储只配了一半）",
        },
    )


def register_attachment_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把附件上传 / 读回路由挂到既有 app（app.py 侧一行调用的接入面）。"""

    @app.post(ATTACHMENT_UPLOAD_PATH, response_model=AttachmentUploadResponse)
    async def upload_attachment(
        session_id: str, request: Request, name: str | None = None
    ) -> AttachmentUploadResponse:
        """流式接收一张图片的字节，内容寻址落盘，回不透明 id。"""
        state = app.state.agent
        try:
            validate_session_id(session_id)
        except InvalidSessionId as error:
            raise http_error(error) from error
        if not await session_service(state).has_session(session_id):
            raise http_error(SessionNotFound(f"session '{session_id}' not found"))

        limits = resolve_image_limits(state.settings)
        try:
            data = await _read_body_bounded(request, limits.max_image_bytes)
            detected = detect_image(
                data,
                max_pixels=limits.max_image_pixels,
                max_dimension=limits.max_image_dimension,
                allowed_media_types=limits.media_types,
            )
            _check_declared_matches(request, name, detected.media_type)
        except AttachmentError as error:
            raise _attachment_http_error(error) from error

        store = _build_attachment_store(state.settings, session_id)
        if store is None:
            raise _unavailable_storage_error()
        blob = await store.save_bytes(
            session_id, data, mime_type=detected.media_type
        )
        return AttachmentUploadResponse(
            attachment_id=blob.artifact_id,
            media_type=detected.media_type,
            bytes=len(data),
            width=detected.width,
            height=detected.height,
            name=file_leaf_name(name) if name else None,
        )

    @app.get("/api/sessions/{session_id}/attachments/{attachment_id}/content")
    async def read_attachment_content(session_id: str, attachment_id: str) -> Response:
        """受控读回原始字节 + 正确 `Content-Type`。

        404 覆盖三种**不可区分**的情形（不泄露存在性）：从未上传过该 id、该 id 属于
        别的会话、id 形态合法但本会话命名空间里没有。422 只留给 id 形态非法。
        """
        state = app.state.agent
        try:
            validate_session_id(session_id)
        except InvalidSessionId as error:
            raise http_error(error) from error
        if not BYTE_ARTIFACT_ID_PATTERN.fullmatch(attachment_id):
            raise HTTPException(
                status_code=422,
                detail=(
                    "attachment_id 必须形如 sha256:<64 位小写十六进制>："
                    f"{attachment_id!r}"
                ),
            )
        if not await session_service(state).has_session(session_id):
            raise http_error(SessionNotFound(f"session '{session_id}' not found"))

        store = _build_attachment_store(state.settings, session_id)
        if store is None:
            raise _unavailable_storage_error()
        try:
            blob = await store.load_bytes(attachment_id)
        except KeyError as error:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"attachment {attachment_id!r} 不在会话 {session_id!r} 的"
                    "命名空间里（不存在，或属于别的会话）"
                ),
            ) from error
        # content 一定被 load_bytes 填充（契约）；None 只可能来自未 load 的元数据形状。
        return Response(
            content=blob.content or b"", media_type=blob.mime_type
        )
