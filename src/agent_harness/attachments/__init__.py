"""附件入站领域（#822 / MM-01）。

字节进得来、存得住、按授权取得回：上传端点、内容寻址存储、受控读回。
让模型看到图是 MM-02，本包**不做**归一化 / 投影 / Provider 载荷。

来源（AGENTS.md §6.1 / §9.1.1，判定见
`docs/research/2026-10-07-multimodal-image-input-research.md` §7.3-A）：

- 契约（`AttachmentId` / `ImageAttachmentRef` / `ImageAttachmentLimits`）与纯算法
  （文件名消毒、内容寻址原子发布）移植自 **DeepSeek Harness** `packages/attachment`
  （MIT，commit `5badb150`）：`attachment/src/types.ts:1-164`、
  `attachment/src/error.ts:1-87`、`attachment-local/src/file-store.ts:45-56`、
  `attachment-local/src/store.ts:214-388,431-458`。
- magic-bytes 图片探测对译自 **Pi** `packages/coding-agent/src/utils/mime.ts:1-116`
  （MIT，commit `1b347794`），尺寸解析换成本仓标准库实现（零新依赖，见 `probe.py`）。
- 只移植契约与纯算法，**不移植**任何 Cordis 绑定的类。
"""

from agent_harness.attachments.admission import (
    STORAGE_UNAVAILABLE_MESSAGE,
    check_declared_image_matches,
    image_reference,
    message_images_too_large_message,
    read_image_file_bounded,
    single_image_too_large_message,
    too_many_images_message,
)
from agent_harness.attachments.errors import (
    AttachmentError,
    attachment_http_status,
)
from agent_harness.attachments.filenames import file_leaf_name
from agent_harness.attachments.probe import DetectedImage, detect_image
from agent_harness.attachments.types import (
    ImageAttachmentLimits,
    resolve_image_limits,
)

#: 包根的导出面（#939 M-30：**只留真的从包根 import 的名字**）。
#:
#: 此前这里再导出了 13 个名字（`KIND_IMAGE` / `ImageRef` / 投影纯函数 / 归一化 /
#: 词汇表等），它们**唯一定义点**在子模块里，而全仓没有任何调用方从包根取用它们
#: （`grep` 复核 + `tests/attachments/test_export_surface.py` 已机械化）。再导出的
#: 代价是"读代码的人以为包根是公开 API"，导出面因此只增不减。子模块仍可**直接** import——
#: `from agent_harness.attachments.projection import ImageRef`。
__all__ = [
    "STORAGE_UNAVAILABLE_MESSAGE",
    "AttachmentError",
    "DetectedImage",
    "ImageAttachmentLimits",
    "attachment_http_status",
    "check_declared_image_matches",
    "detect_image",
    "file_leaf_name",
    "image_reference",
    "message_images_too_large_message",
    "read_image_file_bounded",
    "resolve_image_limits",
    "single_image_too_large_message",
    "too_many_images_message",
]
