"""附件入站领域（#822 / MM-01）。

字节进得来、存得住、按授权取得回：上传端点、内容寻址存储、受控读回。
让模型看到图是 MM-02，本包**不做**归一化 / 投影 / Provider 载荷。

来源（AGENTS.md §6.1 / §9.1.1，判定见
`docs/research/2026-10-07-multimodal-image-input-research.md` §7.3-A）：

- 契约（`AttachmentId` / `ImageAttachmentRef` / `ImageAttachmentLimits`）与纯算法
  （文件名消毒、内容寻址原子发布）移植自 **DeepSeek Harness** `packages/attachment`
  （MIT，commit `5badb150`）：`attachment/src/types.ts`、`attachment/src/error.ts`、
  `attachment-local/src/file-store.ts`、`attachment-local/src/store.ts`。
- magic-bytes 图片探测对译自 **Pi** `packages/coding-agent/src/utils/mime.ts`
  （MIT，commit `1b347794`），尺寸解析换成本仓标准库实现（零新依赖，见 `probe.py`）。
- 只移植契约与纯算法，**不移植**任何 Cordis 绑定的类。
"""

from agent_harness.attachments.errors import (
    AttachmentError,
    AttachmentErrorCode,
    attachment_http_status,
)
from agent_harness.attachments.filenames import file_leaf_name
from agent_harness.attachments.probe import DetectedImage, detect_image
from agent_harness.attachments.types import (
    EXTENSION_MEDIA_TYPES,
    SUPPORTED_IMAGE_MEDIA_TYPES,
    ImageAttachmentLimits,
    ImageAttachmentRef,
    resolve_image_limits,
)

__all__ = [
    "EXTENSION_MEDIA_TYPES",
    "SUPPORTED_IMAGE_MEDIA_TYPES",
    "AttachmentError",
    "AttachmentErrorCode",
    "DetectedImage",
    "ImageAttachmentLimits",
    "ImageAttachmentRef",
    "attachment_http_status",
    "detect_image",
    "file_leaf_name",
    "resolve_image_limits",
]
