"""#952 行尾归一化语义收口的一致性用例。

模块 `agent_harness.tools._line_endings` 收口后提供两个行尾归一化函数，各自
服务一条路径，本文件把这两个语义与它们的交互锁进可执行断言：

1. `_canonical(text)`：CRLF 与裸 CR **都**折成 LF（Python io universal newlines
   语义，三类行尾全折）。**诊断路径**（`line_ending_mismatch` / `not_found_hint`）
   的折平判据用它——判据问的是「两侧折成同一行尾后能不能命中」，裸 CR 也是行尾
   差异，必须一起折。
2. `_crlf_to_lf(text)`：只把 CRLF 折成 LF，裸 CR **原样保留**。**匹配路径**
   （`replace_with_line_ending_tolerance`）的归一化用它，与 `_from_lf` 配对：
   往返必须逐字节无损，而裸 CR 分隔的文件不在归一化路径的启用范围内，故不能折。
3. 两条路径的一致性边界：**不含裸 CR** 时两者结果相同；**含裸 CR** 时两者必须
   不同（`_crlf_to_lf` 保留 `"\\r"`）。

矩阵部分把「窄归一化命中（T1）」与「诊断折平对不上则窄必不命中（T2）」两条
不变式在 文件行尾模式 × old 模板 × old 行尾模式 × replace_all 上全量验证。

注：`_crlf_to_lf` 在收口实现落地前尚不存在，本文件引用它现在会红，这是预期的
TDD 红。
"""

from __future__ import annotations

import io

from agent_harness.tools._line_endings import (
    _canonical,
    _crlf_to_lf,
    replace_with_line_ending_tolerance,
)

# --- 标准库一手语义 ---------------------------------------------------------


def _universal_newlines(s: str) -> str:
    """io universal newlines（newline=None）对 s 的折平结果（标准库一手语义）。"""
    # newline=None：CRLF / 裸 CR / LF 三类输入行尾都先折成 LF 再返回。
    wrapper = io.TextIOWrapper(
        io.BytesIO(s.encode("utf-8")), encoding="utf-8", newline=None
    )
    try:
        return wrapper.read()
    finally:
        wrapper.close()


# 覆盖 CRLF / LF / 裸 CR / 混行尾 / 无行尾的样本。
_MIXED_SAMPLES = [
    "alpha\r\nbeta\r\ngamma",
    "alpha\nbeta\ngamma",
    "alpha\rbeta\rgamma",
    "alpha\r\nbeta\ngamma\r",
    "a\r\nb\rc\nd",
    "a\rb",
    "\r",
    "\r\n",
    "alphabetagamma",
    "",
]


def test_canonical_is_universal_newlines():
    """`_canonical` 的三类折平必须与标准库 universal newlines 逐字符一致。"""
    for s in _MIXED_SAMPLES:
        assert _canonical(s) == _universal_newlines(s), (
            f"canonical disagrees with io universal newlines for {s!r}"
        )


def test_crlf_to_lf_folds_only_crlf():
    """`_crlf_to_lf` 只折 CRLF；裸 CR 必须原样保留（与 _from_lf 配对无损往返）。"""
    assert _crlf_to_lf("a\r\nb\rc\nd") == "a\nb\rc\nd"
    assert _crlf_to_lf("a\r\nb\r\n") == "a\nb\n"
    # 纯裸 CR：一个字节都不动。
    assert _crlf_to_lf("a\rb\rc") == "a\rb\rc"


def test_normalizers_agree_without_bare_cr():
    """不含裸 CR 时两归一化一致；含裸 CR 时必须不同且 `_crlf_to_lf` 保留 CR。"""
    without_bare_cr = [
        "alpha\r\nbeta\r\ngamma",  # 纯 CRLF
        "alpha\nbeta\ngamma",  # 纯 LF
        "alpha\r\nbeta\ngamma",  # 混 CRLF + LF
        "alphabetagamma",  # 无行尾
        "",
    ]
    for s in without_bare_cr:
        assert _canonical(s) == _crlf_to_lf(s), (
            f"normalizers must agree without bare CR for {s!r}"
        )

    with_bare_cr = [
        "a\rb",
        "a\r\nb\rc\nd",
        "alpha\rbeta\rgamma",
        "\r",
    ]
    for s in with_bare_cr:
        assert "\r" in _crlf_to_lf(s), (
            f"_crlf_to_lf must preserve bare CR for {s!r}"
        )
        assert _canonical(s) != _crlf_to_lf(s), (
            f"normalizers must differ with bare CR for {s!r}"
        )


# --- 全矩阵一致性 -----------------------------------------------------------

# 文件行尾模式 → 文件内容。mixed 用 CRLF + LF + 裸 CR 三种齐全（末位的裸 CR
# 让「alpha/beta/gamma」三行也承载第三种行尾）；none 是无行尾的单行。
_FILE_CONTENT = {
    "all-crlf": "alpha\r\nbeta\r\ngamma",
    "all-lf": "alpha\nbeta\ngamma",
    "all-cr": "alpha\rbeta\rgamma",
    "mixed": "alpha\r\nbeta\ngamma\r",
    "none": "alphabetagamma",
}

# old 模板 → 该模板取自文件的哪些逻辑行（文本不同的那组把 beta 改成 BETA）。
_OLD_TEMPLATE_LINES = {
    "middle": ["beta"],
    "first-two": ["alpha", "beta"],
    "whole": ["alpha", "beta", "gamma"],
    "different": ["alpha", "BETA"],
}

# old 行尾模式 → 拼接各逻辑行时循环使用的分隔符序列。empty ⇒ 直接相连（无行尾）。
_OLD_SEPARATORS = {
    "all-crlf": ["\r\n"],
    "all-lf": ["\n"],
    "all-cr": ["\r"],
    "mixed": ["\r\n", "\n"],
    "none": [],
}


def _render_old(lines: list[str], mode: str) -> str:
    """按 old 行尾模式拼接逻辑行（分隔符按序列循环取用）。"""
    seps = _OLD_SEPARATORS[mode]
    if not seps:
        return "".join(lines)
    out = lines[0]
    for i, line in enumerate(lines[1:]):
        out += seps[i % len(seps)] + line
    return out


def test_matrix_normalization_consistency():
    """文件模式 × old 模板 × old 模式 × replace_all 的全矩阵 T1/T2 一致性。

    对每一组 (content, old_string) 在 replace_all 取 False / True 下断言：

    - T1：窄归一化命中 ⟹ 诊断折平判据必通过
      （若 count > 0 且 exact == 0，则 `_canonical(old) in _canonical(content)`）。
    - T2：诊断折平对不上 ⟹ 匹配必不命中
      （若 `_canonical(old) not in _canonical(content)`，则 count == 0）。
    """
    for file_mode, content in _FILE_CONTENT.items():
        for template, lines in _OLD_TEMPLATE_LINES.items():
            for old_mode in _OLD_SEPARATORS:
                old_string = _render_old(lines, old_mode)
                new_string = _render_old(["REPLACED"], old_mode)
                for replace_all in (False, True):
                    ctx = (file_mode, template, old_mode, replace_all)
                    exact = content.count(old_string)
                    count, _ = replace_with_line_ending_tolerance(
                        content, old_string, new_string, replace_all=replace_all
                    )
                    # T1：窄归一化命中（exact 不中而 count 中）⟹ 折平判据必过。
                    if count > 0 and exact == 0:
                        assert _canonical(old_string) in _canonical(content), (
                            f"T1 violated {ctx!r}: narrow normalize hit but "
                            "canonical fold missed"
                        )
                    # T2：折平对不上 ⟹ 窄归一化（含字节精确）必不命中。
                    if _canonical(old_string) not in _canonical(content):
                        assert count == 0, (
                            f"T2 violated {ctx!r}: canonical fold missed but "
                            "match hit"
                        )
