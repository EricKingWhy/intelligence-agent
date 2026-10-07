"""文件名消毒（来源: DeepSeek Harness `5badb150` `packages/attachment/attachment-local/src/file-store.ts` 的 `fileLeafName`，MIT）。

两种分隔符都**手工**剥掉：POSIX 宿主把 `\\` 当普通字符，`os.path.basename` 会把
Windows 客户端的完整本地路径留下并泄漏进引用 / 会话日志。Windows 拒绝的文件名字符
统一换成 `_`，使同一个引用在两种宿主上都成立。含 Windows 保留设备名与 255 字节截断。
"""

from __future__ import annotations

import re

#: Windows 保留设备名（来源: DSH `WINDOWS_DEVICE_NAME`）。
_WINDOWS_DEVICE_NAME = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE)

#: Windows 文件名非法字符（来源: DSH `fileLeafName` 的 `[<>:"|?*]`）。
_ILLEGAL_NAME_CHARS = re.compile(r'[<>:"|?*]')

#: 控制字符（含 DEL）。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _is_windows_device_name(name: str) -> bool:
    dot = name.find(".")
    stem = (name if dot < 0 else name[:dot]).rstrip(". ")
    return bool(_WINDOWS_DEVICE_NAME.match(stem))


def _utf8_prefix(value: str, max_bytes: int) -> str:
    """按**字节**截断到不超过 `max_bytes`（不劈开多字节字符）。"""
    prefix: list[str] = []
    used = 0
    for ch in value:
        encoded = ch.encode("utf-8")
        if used + len(encoded) > max_bytes:
            break
        prefix.append(ch)
        used += len(encoded)
    return "".join(prefix)


def file_leaf_name(value: str | None) -> str:
    """把一个调用方展示名消毒成安全的叶子名。

    输入可以是一条完整的客户端路径；返回值**非空**且两种宿主上都能存。
    """
    if value is None:
        return "file"
    leaf = value[max(value.rfind("/"), value.rfind("\\")) + 1 :]
    clean = _CONTROL_CHARS.sub("", leaf)
    clean = _ILLEGAL_NAME_CHARS.sub("_", clean).strip().rstrip(". ")
    if _is_windows_device_name(clean):
        clean = f"_{clean}"
    clean = _utf8_prefix(clean, 255).rstrip(". ")
    if clean in ("", ".", ".."):
        return "file"
    return clean


__all__ = ["file_leaf_name"]
