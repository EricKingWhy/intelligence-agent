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

__all__ = [
    "dominant_newline",
    "line_ending_mismatch",
    "not_found_hint",
    "replace_with_line_ending_tolerance",
]


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


def _kinds(text: str) -> list[str]:
    """按 CRLF / 裸 CR / LF 三类枚举 text 里**实际存在**的行尾记号。

    对文件与 old_string 走同一套枚举，输出的两个集合才可比（#851 P2）。
    """
    crlf = text.count("\r\n")
    present = (
        ("CRLF（\\r\\n）", crlf),
        ("裸 CR（\\r）", text.count("\r") - crlf),
        ("LF（\\n）", text.count("\n") - crlf),
    )
    return [label for label, n in present if n]


def line_ending_mismatch(content: str, old_string: str) -> tuple[str, str] | None:
    """内容与 old_string 只在行尾上不同时，返回 (文件行尾, old_string 行尾)。

    返回 None ⇒ 差异不止行尾（就是上下文抄错），调用方**不得**提行尾，
    否则模型会去修一个不存在的行尾问题。

    只在调用方已经确认「字节精确 + 主导行尾归一化都没命中」之后才调用（#851）。
    判据：把 CRLF 与裸 CR 都折成 LF 后能命中，即差异只在行尾编码上。
    混行尾文件在主导行尾归一化下会落空，但这里仍能命中，正是本条存在的意义。

    两侧行尾都按实际种类枚举（#851 P2）：只取「主导/最后一个」行尾会在混杂时
    拼出自相矛盾的句子（文件是 LF、old 也是 LF 却称「两者只在行尾上不同」）。
    若枚举后两侧种类集合**完全相同**（正常不会发生：折平能命中而字节不命中，
    只可能是行尾编码差异），降级为 None——不误报。

    `old_string` 里**一个 `\\n` 都没有**（例如整段由裸 CR 分隔）时也返回 None：
    无法命名它的「行尾差异」，保守起见不提行尾。裸 CR 分隔的内容在 read 的展示
    文本里通常带不出裸 CR，据此把它判成「行尾差异」更可能掩盖真正的抄错。
    """
    canonical_content = content.replace("\r\n", "\n").replace("\r", "\n")
    canonical_old = old_string.replace("\r\n", "\n").replace("\r", "\n")
    if canonical_old not in canonical_content:
        return None
    if "\n" not in old_string:
        return None
    file_kinds = _kinds(content)
    old_kinds = _kinds(old_string)
    if not file_kinds or not old_kinds or file_kinds == old_kinds:
        return None
    return (" / ".join(file_kinds), " / ".join(old_kinds))


def not_found_hint(content: str, old_string: str) -> str:
    """未命中时的可执行后缀；差异不止行尾时返回空串（#851 验收 2 的判别力）。

    混行尾的文件（#851 P1）不套统一后缀：对这种文件「按该文件的行尾改写
    old_string」是不可执行的指令（段与段行尾不同，照做仍命中不了），
    只给唯一可行的动作——用 write 整文件重写。
    """
    mismatch = line_ending_mismatch(content, old_string)
    if mismatch is None:
        return ""
    file_newline, old_newline = mismatch
    if " / " in file_newline:
        return (
            f"该文件的行尾是混用的（{file_newline}），段与段的行尾并不一致；"
            f"old_string 的行尾是 {old_newline}。"
            f"这种文件无法靠改写 old_string 的行尾命中，请改用 write 整文件重写。"
        )
    return (
        f"该文件的行尾是 {file_newline}，old_string 的是 {old_newline}，"
        f"两者只在行尾上不同。请按该文件的行尾改写 old_string 后重试，"
        f"或改用 write 整文件重写。"
    )


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
