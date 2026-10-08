"""附件领域失败词汇表。

来源: DeepSeek Harness `5badb150` `packages/attachment/attachment/src/error.ts:1-87`
（MIT）。只保留 MM-01 入站接纳会**实际抛出**的稳定码——全部是调用方可纠正的接纳失败
（4xx）。DSH 里那族存储故障码（`ATTACHMENT_WRITE_FAILED`）在本仓没有抛出点
（`save_bytes` 失败抛 `OSError` 而非 `AttachmentError`），故不移植；`isAttachmentError`
那套跨包成员判定（`instanceof` + code 集合）在单进程内也不需要。
"""

from __future__ import annotations

from typing import Literal

#: 调用方可纠正的图片接纳失败码（来源: DSH `IMAGE_ADMISSION_ERROR_CODES` 的相关子集）。
#: 用 `Literal`（而不是 `str` 别名）当**类型即集合**：拼错一个码在静态期就暴露，
#: 而不是运行期落到某个未映射分支。
AttachmentErrorCode = Literal[
    "UNSUPPORTED_IMAGE_TYPE",  # 字节不是受支持的图片格式 / 不在允许 media types 内
    "INVALID_IMAGE",  # 声称是图片但头部损坏 / 为空
    "IMAGE_TYPE_MISMATCH",  # 声明的 media type / 扩展名与字节判定不符
    "IMAGE_TOO_LARGE",  # 超过单张字节上限
    "IMAGE_TOO_MANY_PIXELS",  # 超过像素上限
    "IMAGE_DIMENSION_TOO_LARGE",  # 超过边长上限
]


class AttachmentError(Exception):
    """附件领域的稳定失败（来源: DSH `AttachmentError`，仅保留 code 语义）。

    `code` 是机读路由键：HTTP 状态码由 `attachment_http_status` 单点映射，
    调用方（web 层）不各自 if 级联。
    """

    def __init__(self, message: str, code: AttachmentErrorCode) -> None:
        super().__init__(message)
        self.code = code


def attachment_http_status(code: AttachmentErrorCode) -> int:
    """稳定码 → HTTP 状态码（单点映射，web 层不重复判定）。

    全部码都是调用方可纠正的接纳失败（4xx）：

    - `IMAGE_TOO_LARGE` → 413（HTTP `Content Too Large`，与既有 body 体积配额同码）；
    - 其余接纳失败 → 422（形状 / 内容不合契约，可纠正）。
    """
    return 413 if code == "IMAGE_TOO_LARGE" else 422


__all__ = ["AttachmentError", "AttachmentErrorCode", "attachment_http_status"]
