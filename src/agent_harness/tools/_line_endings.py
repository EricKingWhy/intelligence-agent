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

例外：`not_found_hint` 的提示不受上面这条限制——它可以在**混行尾文件**上、
也可以在**整段由裸 CR 分隔的单一行尾文件**上（归一化路径对裸 CR 关闭）建议
「把 old_string 改写成对应段落的行尾后重试」。那次重试走的是**字节精确匹配**
（`count(old)`），不是上面的归一化路径，两者不矛盾：归一化的不启用只限制自动
改写，不限制提示。

历次修回的逐条判据与反例见 #851。
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "dominant_newline",
    "line_ending_mismatch",
    "not_found_hint",
    "replace_with_line_ending_tolerance",
]

# 行尾记号的三个种类（逻辑值）；展示文案另置 _KIND_LABELS，避免逻辑与呈现耦合。
_Kind = Literal["crlf", "cr", "lf"]

_KIND_LABELS: dict[_Kind, str] = {
    "crlf": "CRLF（\\r\\n）",
    "cr": "裸 CR（\\r）",
    "lf": "LF（\\n）",
}

# 「改用 write」这条动作的固定表述与统一尾注（四处提示共用，避免逐处抄写漂移）。
_REWRITE_ACTION = (
    "可尝试逐处核对对应段落的行尾、把 old_string 改写为该段落的行尾后重试；"
    "若仍不命中，改用 write 整文件重写"
)
_WRITE_HINT_TAIL = "（必须照抄原文件逐字节行尾，勿统一成 LF）。"


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


def _canonical(text: str) -> str:
    """行尾归一的规范形：CRLF 与裸 CR 都折成 LF。

    语义来源：Python `io` 的 universal newlines（`open(..., newline=None)`）——
    `\\r`、`\\r\\n`、`\\n` 统一译为 `\\n`。三类全折是标准库确立的语义，本函数照此实现。

    适用场景：诊断路径（`line_ending_mismatch` / `not_found_hint`）的折平判据专用。
    判据问的是「两侧折成同一行尾后能不能命中」，裸 CR 也是行尾差异，必须一起折。

    为什么匹配路径不用它：匹配路径归一化后还要经 `_from_lf` 转回主导行尾写回；若用
    三类全折，会把 old_string / new_string 里的裸 CR 静默改写成主导行尾，改变用户可见
    行为。故匹配路径用 `_crlf_to_lf`。两个函数语义不同、名字不同、适用场景不同，见
    `_crlf_to_lf` 的 docstring。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _crlf_to_lf(text: str) -> str:
    """把 CRLF 折成 LF；裸 CR 原样保留。

    语义：只折 `\\r\\n` → `\\n`，单独的 `\\r`（裸 CR）**不动**——与 `_canonical` 不同，
    后者三类全折。

    适用场景：匹配路径 `replace_with_line_ending_tolerance` 的归一化专用，与
    `_from_lf` 配对——先把文件与 old/new 归一化成 LF 匹配，再把结果经 `_from_lf`
    转回该文件的主导行尾写回。

    为什么裸 CR 不折：归一化路径只在 `dominant_newline(content)` 非 None（文件行尾纯
    CRLF 或纯 LF）时启用，此时文件侧没有裸 CR；old_string 含裸 CR 时归一化匹配自然
    落空（count=0），诊断路径（`not_found_hint`）会另行给出可执行的「改写行尾后重试」
    提示。自动改写保守、诊断提示宽松，是 #851 有意确立的分工，不得把裸 CR 的折叠塞进
    本函数。
    """
    return text.replace("\r\n", "\n")


def _kinds(text: str) -> list[_Kind]:
    """按 CRLF / 裸 CR / LF 三类枚举 text 里**实际存在**的行尾记号。

    对文件与 old_string 走同一套枚举，输出的两个集合才可比。
    """
    crlf = text.count("\r\n")
    present: list[tuple[_Kind, int]] = [
        ("crlf", crlf),
        ("cr", text.count("\r") - crlf),
        ("lf", text.count("\n") - crlf),
    ]
    return [kind for kind, n in present if n]


def _join_kinds(kinds: list[_Kind]) -> str:
    return " / ".join(_KIND_LABELS[kind] for kind in kinds)


def line_ending_mismatch(
    content: str, old_string: str
) -> tuple[list[_Kind], list[_Kind]] | None:
    """内容与 old_string 只在行尾上不同时，返回 (文件行尾种类, old_string 行尾种类)。

    返回 None ⇒ 差异不止行尾（就是上下文抄错），调用方**不得**提行尾，
    否则模型会去修一个不存在的行尾问题。

    前置条件由折平判据承担：内容与 old_string 折成同一行尾后必须能命中，即两者
    只差行尾编码。混行尾文件在主导行尾归一化下会落空，但这里仍能命中，正是本条
    存在的意义。

    两侧行尾都按**实际种类**枚举：只取「主导/最后一个」行尾会在混杂时拼出自相
    矛盾的句子（文件是 LF、old 也是 LF 却称「两者只在行尾上不同」）。返回**种类
    列表**而不是拼好的字符串——调用方要判「混行尾」（`len(file_kinds) > 1`）与
    「old 是文件的子集」（`set(old_kinds) <= set(file_kinds)`），靠 `" / " in text`
    嗅探字符串是脆的。

    两侧种类集合相同时**不**降级：折平能命中而字节不命中，在混行尾文件里完全
    可能——old_string 自带 CRLF 与 LF 各一处、只是与文件的段落位置对不上（反例
    old `"a = 1\\r\\nb = 2\\nc = 3\\r\\n"` vs 文件 `"a = 1\\r\\nb = 2\\nc = 3\\n"`）。
    把它当「不可能发生」防御性返回 None 会把真实的行尾问题吞成「上下文抄错」。

    `old_string` 里**一个 `\\n` 都没有**（例如整段由裸 CR 分隔）时同样照报：它就是
    要提的那类行尾差异（折平后命中、字节不命中）。只有两侧**一方的行尾记号种类
    为空**（单行、无任何行尾记号）才返回 None——那种情况没有任何可命名的行尾差异。
    """
    if _canonical(old_string) not in _canonical(content):
        return None
    file_kinds = _kinds(content)
    old_kinds = _kinds(old_string)
    if not file_kinds or not old_kinds:
        return None
    return (file_kinds, old_kinds)


def not_found_hint(
    content: str, old_string: str, *, search_base: str | None = None
) -> str:
    """未命中时的可执行后缀；差异不止行尾时返回空串（#851 验收 2 的判别力）。

    `content` 是**描述基准**（模型读到的那个文件）；`search_base` 是工具**实际
    搜索的基准**，缺省即 `content`（edit 的单发替换）。apply_patch 逐 hunk 在内存
    态 `current` 上搜索，前块消费/改写掉 old_string 时，末块的失配与行尾无关——
    此时任何「改写行尾后重试」都是死路（反例 1：`content="a = 1\\nb = 2\\n"` + 两块
    `"b = 2\\n"`；反例 2：纯 CRLF 文件 `"A\\r\\nB\\r\\n"`，前块先改掉 `A\\r\\n`，末块
    再找 `A\\n`，把它改写成 `A\\r\\n` 后在内存态里仍然 `count()==0`）。所以入口先按
    `search_base` 落前置守卫：折平后仍对不上 ⇒ 返回空串。该守卫同时把
    `line_ending_mismatch` 的前置契约从注释变成**可执行**判据。

    **不得**断言「无法靠改写 old_string 的行尾命中」：混行尾文件里 old 改写成
    **对应段落**的行尾后 `count()==1`，绝对断言会让模型放弃一条走得通的路。

    old 含有文件里没有的行尾种类时同样给两条路：把那个记号改写成对应段落的行尾
    后重试**可能**命中（反例：文件 `"a\\r\\nb\\nc\\n"` + old `"a\\rb\\n"`，把裸 CR
    改成 CRLF 后 `count()==1`），所以不得把它封成「只能改用 write」的唯一路径。

    两侧行尾种类相同时（各段落位置不同）也给谨慎提示，不吞掉：折平命中而字节
    不命中在混行尾文件里完全可能。

    四条分支的**事实描述**不同（种类相同 / 文件混用且 old 是子集 / old 含文件里
    没有的种类 / 文件行尾单一），**动作句**是同一条，统一在 `_REWRITE_ACTION`
    一处拼接。

    返回值**不带**与前导句之间的分隔符（无前导空格）：分隔符属于**拼接方**，由调用方
    按自己的前导句标点决定。两处 caller 的前导句都以全角句号收尾，中文排印里句号后
    不接空格，故两者都直接拼接、不加分隔符。把分隔符放进被复用的返回值，等于要求
    每个调用方都记住「我的前导句得配一个空格」——任一处文案改动都会静默产出
    「。」+ 空格的异常断句（#851 八轮修回 P2）。
    """
    base = content if search_base is None else search_base
    # 前置契约（P3）：调用方声明的基准里 old_string **字节精确存在** ⟹ 本次失败与
    # 行尾无关（在 apply_patch 里是前块把它消费/改写掉了），不得提行尾。这条同时
    # 保证下游 `line_ending_mismatch` 声明的「已确认字节精确未命中」由代码而非注释承担。
    # 第二条件针对搜索基准：old 在搜索基准上折平后仍对不上 ⇒ 行尾诊断不可执行
    # （反例 2：前块把 `A\r\n` 换成 `Z\r\n` 后，old `A\n` 改写成 `A\r\n` 仍 `count()==0`）。
    if content.count(old_string) or _canonical(old_string) not in _canonical(base):
        return ""
    mismatch = line_ending_mismatch(content, old_string)
    if mismatch is None:
        return ""
    file_kinds, old_kinds = mismatch
    file_newline = _join_kinds(file_kinds)
    old_newline = _join_kinds(old_kinds)
    if set(file_kinds) == set(old_kinds):
        fact = (
            f"该文件与 old_string 的行尾种类相同（{file_newline}），"
            f"但各处行尾出现的位置或次数可能不同"
        )
    elif len(file_kinds) > 1 and set(old_kinds) <= set(file_kinds):
        fact = (
            f"该文件的行尾是混用的（{file_newline}），段与段的行尾并不一致；"
            f"old_string 的行尾是 {old_newline}"
        )
    elif len(file_kinds) > 1:
        fact = (
            f"该文件的行尾是混用的（{file_newline}），段与段的行尾并不一致；"
            f"old_string 的行尾是 {old_newline}，含有该文件里不存在的行尾种类"
        )
    else:
        fact = (
            f"该文件的行尾是 {file_newline}，old_string 的是 {old_newline}，"
            f"两者只在行尾上不同"
        )
    return f"{fact}。{_REWRITE_ACTION}{_WRITE_HINT_TAIL}"


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

    normalized = _crlf_to_lf(content)
    normalized_old = _crlf_to_lf(old_string)
    count = normalized.count(normalized_old)
    if count == 0:
        return 0, content

    if replace_all:
        replaced = normalized.replace(normalized_old, _crlf_to_lf(new_string))
    else:
        replaced = normalized.replace(normalized_old, _crlf_to_lf(new_string), 1)
    return count, _from_lf(replaced, newline)
