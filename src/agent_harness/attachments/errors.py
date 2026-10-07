"""附件领域失败词汇表。

来源: DeepSeek Harness `5badb150` `packages/attachment/attachment/src/error.ts`
（MIT）。只保留 MM-01 入站接纳实际会用的稳定码；`isAttachmentError` 那套跨包
成员判定（`instanceof` + code 集合）在本仓单进程内不需要，故不移植。

码分两族：

- **调用方可纠正的接纳失败**（4xx）：形状/类型/上限不符合契约，客户端改一下就能过；
- **存储故障**（5xx）：写盘失败等，不是调用方的错。
"""

from __future__ import annotations

#: 调用方可纠正的图片接纳失败码（来源: DSH `IMAGE_ADMISSION_ERROR_CODES` 的相关子集）。
IMAGE_ADMISSION_ERROR_CODES = frozenset(
    {
        "UNSUPPORTED_IMAGE_TYPE",  # 字节不是受支持的图片格式 / 不在允许 media types 内
        "INVALID_IMAGE",  # 声称是图片但头部损坏 / 为空
        "IMAGE_TYPE_MISMATCH",  # 声明的 media type / 扩展名与字节判定不符
        "IMAGE_TOO_LARGE",  # 超过单张字节上限
        "IMAGE_TOO_MANY_PIXELS",  # 超过像素上限
        "IMAGE_DIMENSION_TOO_LARGE",  # 超过边长上限
    }
)

#: 存储故障码（来源: DSH `ATTACHMENT_WRITE_FAILED`）。
STORAGE_ERROR_CODES = frozenset({"ATTACHMENT_WRITE_FAILED"})

ATTACHMENT_ERROR_CODES = IMAGE_ADMISSION_ERROR_CODES | STORAGE_ERROR_CODES

AttachmentErrorCode = str


class AttachmentError(Exception):
    """附件领域的稳定失败（来源: DSH `AttachmentError`，仅保留 code 语义）。

    `code` 是机读路由键：HTTP 状态码由 `attachment_http_status` 单点映射，
    调用方（web 层）不各自 if 级联。
    """

    def __init__(self, message: str, code: AttachmentErrorCode) -> None:
        super().__init__(message)
        self.code = code


def is_image_admission_error(error: object) -> bool:
    """调用方可纠正的接纳失败（对应 4xx）。"""
    return isinstance(error, AttachmentError) and error.code in IMAGE_ADMISSION_ERROR_CODES


def attachment_http_status(code: AttachmentErrorCode) -> int:
    """稳定码 → HTTP 状态码（单点映射，web 层不重复判定）。

    - `IMAGE_TOO_LARGE` → 413（HTTP `Content Too Large`，与既有 body 体积配额同码）；
    - 其余接纳失败 → 422（形状 / 内容不合契约，可纠正）；
    - 存储故障 → 500（不是调用方的错，如实上报）。
    """
    if code == "IMAGE_TOO_LARGE":
        return 413
    if code in IMAGE_ADMISSION_ERROR_CODES:
        return 422
    return 500
