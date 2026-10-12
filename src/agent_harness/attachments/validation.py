"""配置面的**校验规则**（不依赖 `Settings`，故可被 `config` 直接 import）。

`#939 M-15`：`Settings` 需要在**构造期**（= 启动期）校验 `attachment_allowed_media_types`，
校验规则却被 `attachments.types` 与 `Settings` 的字段一起持有——`types.resolve_image_limits`
运行期读 `Settings`，`Settings` 的 validator 又要调 `types` 的解析函数，两边概念上互指
（实际靠函数体内延迟 import 躲开 import 环，而那道保护只写在注释里、无机械守卫）。

把**纯规则**放进本模块后方向变成单向（箭头按**实际 import 边**画，`#988 F-2` 订正——此前
这里画成 `validation ──import──▶ types`，与代码相反）：

    config ──import──▶ attachments.validation ◀──import── attachments.types
      （validator 调规则）   （规则 + 词汇表，真身）      （resolve_image_limits 读 Settings）

- 本模块**不 import 包内任何模块**（连 `config` 也不 import，甚至不需要 `TYPE_CHECKING`：
  规则只吃字符串）⇒ 它是上面这张图的**汇点**，两个箭头都指向它；
- `config` 的 validator 直接 `from agent_harness.attachments.validation import ...`，
  不再有"躲环用的延迟 import"；
- `attachments.types` 只**内部调用** `parse_allowed_media_types`（不进它的 `__all__`）；
  需要这几个名字的调用方一律从**本模块**取用（`#988 F-2` 收窄了 `types` 的再导出面
  ——单一真身，不是第二份定义）。
"""

from __future__ import annotations

from typing import Literal

#: v1 接受的栅格图片 media types（来源: DSH `attachment/src/types.ts` `ImageMediaType`）。
ImageMediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]

SUPPORTED_IMAGE_MEDIA_TYPES: tuple[ImageMediaType, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)

#: 扩展名 → media type（用于"声明文件名与字节不符"的判定；大小写不敏感）。
EXTENSION_MEDIA_TYPES: dict[str, ImageMediaType] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def parse_allowed_media_types(raw: str) -> tuple[str, ...]:
    """解析逗号分隔的允许图片类型；未知类型 / 空 → `ValueError`（响亮失败）。

    **单一解析点**：`Settings` 的字段校验（**启动期**，见 `config.Settings`）与
    `resolve_image_limits`（请求期）共用它，避免两处规则漂移。未知类型静默忽略会让
    部署者以为某格式已放行、实际被悄悄拒绝（fail-closed 的反面）。
    """
    media_types: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        if value not in SUPPORTED_IMAGE_MEDIA_TYPES:
            raise ValueError(
                "attachment_allowed_media_types 含不支持的图片类型"
                f"（允许: {', '.join(SUPPORTED_IMAGE_MEDIA_TYPES)}）：{value!r}"
            )
        if value not in media_types:
            media_types.append(value)
    if not media_types:
        raise ValueError("attachment_allowed_media_types 不能为空")
    return tuple(media_types)


__all__ = [
    "EXTENSION_MEDIA_TYPES",
    "SUPPORTED_IMAGE_MEDIA_TYPES",
    "ImageMediaType",
    "parse_allowed_media_types",
]
