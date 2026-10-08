"""行尾容忍匹配（#851）。

背景：Windows 上 `core.autocrlf=true` 检出即 CRLF，而模型生成的字符串几乎总是 LF。
`Sandbox.read_text` 是字节透传（`newline=""`，不做 universal-newlines 折叠），
于是「从 read 结果里逐字抄下来的一段」在 CRLF 文件里 `count()` 得 0 ⇒ 工具报
「未找到匹配的字符串」，把「行尾不同」与「上下文抄错」混成一类，模型只能盲试。

策略（只在**行尾单一**的文件上启用；混行尾文件一律退回字节精确匹配）：

1. 先按字节精确匹配，命中即用原路径 ⇒ LF 文件与既有行为逐字节一致；
2. 精确匹配落空、且文件行尾单一时，在归一化（CRLF → LF）的文本上匹配，
   命中后把 `new_string` 的行尾转回该文件的主导行尾再写回。

第 2 步安全：行尾单一的文件整体 CRLF → LF → CRLF 往返逐字节无损，所以只有
`old_string` 命中的那一段会变，不会产生整文件 diff。混行尾文件不做归一化——
宁可报错，也不替用户改写整文件行尾。
"""

from __future__ import annotations

__all__ = ["dominant_newline", "replace_with_line_ending_tolerance"]


def dominant_newline(content: str) -> str | None:
    """返回 content 的主导行尾（CRLF 或 LF）。

    混行尾、或完全没有换行时返回 None——调用方据此退回字节精确匹配。
    """
    crlf = content.count("\r\n")
    bare_lf = content.count("\n") - crlf
    if crlf and bare_lf:
        return None
    if crlf:
        return "\r\n"
    if bare_lf:
        return "\n"
    return None


def _to_lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def _from_lf(text: str, newline: str) -> str:
    if newline == "\n":
        return text
    return text.replace("\n", newline)


def replace_with_line_ending_tolerance(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool,
) -> tuple[int, str]:
    """行尾容忍地替换，返回 (命中数, 新内容)。

    命中数为 0 时新内容**就是** content（逐字节相同），调用方据此报「未找到」。
    `replace_all=False` 时只替换第一处，但命中数如实返回全部匹配数，以便调用方
    保留「>1 处需 replace_all」的三态语义。
    """
    exact = content.count(old_string)
    if exact:
        if replace_all:
            return exact, content.replace(old_string, new_string)
        return exact, content.replace(old_string, new_string, 1)

    newline = dominant_newline(content)
    if newline is None:
        return 0, content

    normalized = _to_lf(content)
    normalized_old = _to_lf(old_string)
    count = normalized.count(normalized_old)
    if count == 0:
        return 0, content

    if replace_all:
        replaced = normalized.replace(normalized_old, _to_lf(new_string))
    else:
        replaced = normalized.replace(normalized_old, _to_lf(new_string), 1)
    return count, _from_lf(replaced, newline)
