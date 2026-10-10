"""#822 / MM-01：附件入站 REST 传输面（独立 router）。

两个职责，语义单源在 `attachments/` 与 `storage/`：

- `POST /api/sessions/{session_id}/attachments`（octet-stream 流式上传）——
  原始字节进得来、按字节判 MIME、内容寻址落盘，回不透明 `sha256:<hex>` id；
- `GET /api/sessions/{session_id}/attachments/{attachment_id}/content`——
  受控读回原始字节 + `Content-Type`；只认**本会话事件流引用过**的 id，别的会话 /
  未上传过的 id 一律 404（不泄露存在性）。#830 D1 起字节对象全局内容寻址，隔离**不再**
  由 store 的会话命名空间提供，这里的引用闸门是唯一屏障（见下方路由体内不变量注释）。

**授权口径（MM-02 收紧，闭合 MM-01 的已知缺口）**：读回要求该 `attachment_id`
被**本 Session 的 `user/message` 事件真实引用**（PRD D5 / DSH
`ATTACHMENT_NOT_REFERENCED` 语义）。MM-01 落地时授权单位只是"上传即归属本会话"的
命名空间归属，因为当时还没有"事件引用附件"的机制；MM-02 让 `user/message` 带上
`attachments` 引用数组后，此处补回事件引用闸门：**未引用（含上传后从未发送）→ 404**，
且与"从未上传 / 属于别的会话"**不可区分**（不泄露存在性）。

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
    STORAGE_UNAVAILABLE_MESSAGE,
    AttachmentError,
    attachment_http_status,
    check_declared_image_matches,
    detect_image,
    file_leaf_name,
    resolve_image_limits,
    single_image_too_large_message,
)
from agent_harness.session.derive import assert_attachment_referenced
from agent_harness.session.errors import (
    AttachmentNotReferenced,
    InvalidSessionId,
    SessionNotFound,
)
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
                single_image_too_large_message(max_bytes), "IMAGE_TOO_LARGE"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _declared_image_media_type(request: Request) -> str | None:
    """请求 `Content-Type` 声明的图片类型；非 `image/*`（含缺省）→ None。

    判据是**前缀** `image/` 而不是"∈ 支持集"：声明成 `image/svg+xml` 等非支持类型时
    也必须与字节判定比对——否则 `Content-Type: image/svg+xml` + PNG 字节会被静默
    接受成 PNG（契约要求"与声明类型不符即拒绝"，不能只覆盖声明为受支持类型的子集）。
    """
    header = request.headers.get("content-type", "")
    declared = header.split(";", 1)[0].strip().lower()
    return declared if declared.startswith("image/") else None


def _check_declared_matches(
    request: Request, name: str | None, detected_media_type: str
) -> None:
    """声明（`Content-Type` / 文件名扩展名）与字节判定不符 → 拒绝。

    判定本身是 `attachments.admission.check_declared_image_matches`（与 CLI `--image`
    **同一份**，两入口结论一致）；本函数只负责从请求里取出 `Content-Type` 声明。
    """
    check_declared_image_matches(
        declared_media_type=_declared_image_media_type(request),
        name=name,
        detected_media_type=detected_media_type,
    )


def _unavailable_storage_error() -> HTTPException:
    # detail 用 dict（`code` + `message`）而不是裸 str：与本仓 #227 的
    # `artifact_store_unavailable` 503 同一形状（见 `web/app.py` 与 ADR-0035）——
    # "存储不可用"这一族错误给前端一个机读码，不让它按 503 猜原因。这是既有后端
    # 错误契约惯例；本 router 其余 4xx 的 detail 仍是 str（那些是领域异常文案）。
    return HTTPException(
        status_code=503,
        detail={
            "code": ATTACHMENT_STORAGE_UNAVAILABLE,
            "message": STORAGE_UNAVAILABLE_MESSAGE,
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

        **授权闸门（#823 / MM-02，收紧）**：id 必须被本会话某条 `user/message` 事件
        真实引用（PRD D5 / DSH `ATTACHMENT_NOT_REFERENCED` 语义）。未引用（含上传后
        从未发送、别的会话）一律 404，与"从未上传"**不可区分**（不泄露存在性）。
        422 只留给 id 形态非法。
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
        service = session_service(state)
        if not await service.has_session(session_id):
            raise http_error(SessionNotFound(f"session '{session_id}' not found"))

        # #823 / MM-02（A8）：授权判据是「id 被本会话某条 user/message 引用」，本实现
        # 每次 GET 都 `read_events_report` 全量解析 + 扫描事件日志（O(事件数)/图）。
        # 权衡已登记：**正确性优先**——事件日志是唯一权威来源，且这是受控读回入口
        # （非热路径）；大会话 + 多图场景的索引/缓存优化（如按会话缓存被引用 id 集合、
        # 或落附件引用索引）留待后续票，不在此引入易与事件流漂移的旁路状态。
        #
        # **#830 D1 起，本闸门是唯一屏障**：字节对象已改全局内容寻址
        # （`<root>/.attachments/objects/…`），store 侧的会话命名空间**不再**提供隔离
        # （第二屏障已不存在，纵深由两层降为一层）——"别的会话拿不到字节"完全由下面这一行
        # 「本会话事件流是否引用该 id」保证。为守住这条唯一屏障，以下不变量必须成立：
        #   **任何新增写入 `user/message.attachments` 的路径，都必须先经发送闸门
        #   `session/service.py::_resolve_attachment_refs`（形态校验 + 本会话上传回执归属
        #   校验）**，否则该路径会直接变成跨会话字节读取。当前唯一写入者就是它
        #   （`POST /sessions/{id}/messages`；队列 / steer 复用的是已解析引用，
        #   `ResumeRequest` 无 `attachments` 字段，也没有原始事件追加端点）。
        #
        # #934 M-06：判断不住在传输层——`assert_attachment_referenced` 是
        # `session/derive.py` 里的唯一授权入口（谓词 + 判断同模块），这里只做
        # 领域异常 → HTTP 404 的翻译（不可区分口径见 `_attachment_not_found`），
        # 不手写 `not in` 判断。
        events = await service.get_events(session_id)
        try:
            assert_attachment_referenced(events, attachment_id)
        except AttachmentNotReferenced as error:
            raise _attachment_not_found(session_id, attachment_id) from error

        store = _build_attachment_store(state.settings, session_id)
        if store is None:
            raise _unavailable_storage_error()
        try:
            blob = await store.load_bytes(attachment_id)
        except KeyError as error:
            # 事件引用了但字节读不回：违反 persist-before-event 前提（不应发生），
            # 仍按同形 404 如实回，不泄露内部不一致。
            raise _attachment_not_found(session_id, attachment_id) from error
        # content 一定被 load_bytes 填充（契约）；None 只可能来自未 load 的元数据形状。
        return Response(
            content=blob.content or b"", media_type=blob.mime_type
        )


def _attachment_not_found(session_id: str, attachment_id: str) -> HTTPException:
    """统一的 404（"不存在 / 属于别的会话 / 未被本会话事件引用"三种情形同形）。"""
    return HTTPException(
        status_code=404,
        detail=(
            f"attachment {attachment_id!r} 未被会话 {session_id!r} 的事件引用"
            "（不存在，或属于别的会话）"
        ),
    )
