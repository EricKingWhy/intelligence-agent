"""终端主题：颜色角色 + glyph 预设 + 终端能力检测（P0-1）。

来源（MIT，port 保留本注释）：
- Pi theme.ts ThemeColor slot 设计 → pi-mono/.../theme/theme.ts L57
- OMP symbols.ts ASCII_SYMBOLS → oh-my-pi/.../theme/symbols.ts L1168；sep.dot L455/L1233
- 品牌色：本仓库 DESIGN.md --accent #f1b3ca（One Voice Rule）

颜色与字形是两个正交轴（2026-10-06 parent 裁决）：`color` ∈ {truecolor, color256,
ansi16, nocolor}（四档对齐 lipgloss TrueColor/ANSI256/ANSI/Ascii；原 3 档被
termenv/rich/supports-color 证伪，2026-10-06 challenge 修订），`glyph_set` ∈
{unicode, ascii}，独立判定。检测逻辑的成熟产品出处见各分支旁标注；整体组装为
本仓库设计（上游 TUI 渲染层本身无颜色/符号降级链）。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

#: 角色集合（固定 8 个，本票不增）：accent=品牌强调 / text=正文默认 /
#: muted=次要 / faint=最弱 / ok=成功 / warn=警告 / err=错误 / info=信息性高光
#: （slot 设计来源：Pi theme.ts L57-106 的 ThemeColor，V1 只取 8 个）
ROLES = ("accent", "text", "muted", "faint", "ok", "warn", "err", "info")

#: 256 色档取值（语义三色 OK 114 / WARN 220 / ERR 167 / MUTED 246 来自四产品
#: 抓屏 ANSI 实测收敛；accent 218 ≈ #f1b3ca）
_C256 = {"accent": 218, "muted": 246, "faint": 239,
         "ok": 114, "warn": 220, "err": 167, "info": 153}

#: 真彩档取值（accent = #f1b3ca，自家 DESIGN.md Pale Blossom）
_RGB = {"accent": (241, 179, 202),   # = #f1b3ca，自家 DESIGN.md
        "muted": (148, 148, 148), "faint": (78, 78, 78),
        "ok": (135, 215, 135), "warn": (255, 215, 0),
        "err": (215, 95, 95), "info": (175, 215, 255)}

#: 16 色档 role→ANSI code 映射（accent→95 bright magenta 最接近 #f1b3ca；
#: muted/faint 同取 90 是 16 色下的已知坍缩）
_ANSI16 = {"accent": 95, "muted": 90, "faint": 90,
           "ok": 32, "warn": 33, "err": 31, "info": 36}

_GLYPHS_UNICODE = {"pending": "●", "ok": "●", "fail": "●", "warn": "●", "info": "●",
                   "attach": "└", "dot": "·", "prompt": ">", "ellipsis": "…"}
_GLYPHS_ASCII = {"pending": "[*]", "ok": "[ok]", "fail": "[!!]", "warn": "[!]",
                 "info": "[i]", "attach": "'--", "dot": "-", "prompt": ">",
                 "ellipsis": "..."}
# 注：ascii 映射逐字抄 OMP ASCII_SYMBOLS（symbols.ts L1169-1180）；
# attach 取 OMP tree.last "'--"；dot 取 OMP ascii sep.dot " - "（L1233）→ 存 "-"，
# 两侧由 sep() 补空格。GBK 安全：●/└/─ 等可编码，⎿❯✔✘ 会炸，不用（DESIGN.md 字形集）。


#: Windows VT 激活的每进程一次 guard（模块级：避免每次构造 Theme 都打一次 console）。
_VT_ENABLED = False


@dataclass(frozen=True)
class Theme:
    """颜色与字形是两个正交轴（单轴 mode 无法表达"ascii 字形 + 彩色"，且
    NO_COLOR + ASCII 编码若共用一个轴会抛 UnicodeEncodeError）。

    color ∈ {"truecolor", "color256", "ansi16", "nocolor"}：ANSI 着色能力。
    glyph_set ∈ {"unicode", "ascii"}：字形集，与 color 独立判定。
    """

    color: str
    glyph_set: str = "unicode"

    def paint(self, role: str, text: str) -> str:
        """按角色给 text 包 ANSI；text 角色与 nocolor 档原样返回（fail-closed：
        非法 role 抛 ValueError，不静默吞）。"""
        if role not in ROLES:
            raise ValueError(f"unknown color role: {role!r}")
        if role == "text" or self.color == "nocolor":
            return text
        if self.color == "truecolor":
            r, g, b = _RGB[role]
            return f"\x1b[38;2;{r};{g};{b}m{text}\x1b[0m"
        if self.color == "color256":
            return f"\x1b[38;5;{_C256[role]}m{text}\x1b[0m"
        if self.color == "ansi16":
            return f"\x1b[{_ANSI16[role]}m{text}\x1b[0m"
        raise ValueError(f"unknown color mode: {self.color!r}")

    def glyph(self, name: str) -> str:
        """按 glyph_set 取字形（与 color 无关）。"""
        table = _GLYPHS_ASCII if self.glyph_set == "ascii" else _GLYPHS_UNICODE
        return table[name]

    def sep(self) -> str:
        """分隔符：unicode 下 " · "，ascii 下 " - "（OMP sep.dot L455/L1233）。"""
        return " - " if self.glyph_set == "ascii" else " · "


def _detect_glyph_set(stdout) -> str:
    """A 轴 · 字形集：只看编码，与颜色无关，永远执行。

    探针（2026-10-06 challenge 裁定"一字不改"；成熟先例 rich ascii_only
    console.py:141-144，探针比 rich 的 startswith("utf") 启发式更精确——GBK 实测）：
    "●".encode(stdout.encoding or "ascii", errors="strict") 成功 → unicode；
    失败（UnicodeEncodeError/LookupError/TypeError，或 encoding 为 None）→ ascii。
    """
    encoding = getattr(stdout, "encoding", None)
    try:
        "●".encode(encoding or "ascii", errors="strict")
    except (UnicodeEncodeError, LookupError, TypeError):
        return "ascii"
    return "unicode"


def _enable_windows_vt() -> None:
    """Windows VT 激活：每进程一次的最佳努力（termenv termenv_windows.go:100-140
    语义——检测 ≠ 启用，两者刻意分离）。os.system("") 返回值忽略、异常吞掉；
    检测不依赖其成功。"""
    global _VT_ENABLED
    if _VT_ENABLED:
        return
    _VT_ENABLED = True
    try:
        os.system("")  # 返回值忽略；仅为触发 console 初始化
    except Exception:  # noqa: BLE001, S110 —— 票面要求：异常吞掉，激活是尽力而为，检测不依赖其成功
        pass


def _grade_color(stdout) -> str:
    """B 轴 · 颜色分级（优先级从高到低；查表结构照抄 colorprofile/termenv/rich）。

    规则 1 的 CLICOLOR_FORCE 保底在 detect_theme 里包在本函数结果外
    （termenv termenv.go:104-106：FORCE 只把 Ascii 抬到 ANSI，不无中生有造真彩）。
    """
    # 规则 2：NO_COLOR 非空 → nocolor（no-color.org 现行规范"present and not an
    # empty string"；rich console.py:728；gh go-gh env.go:162）
    if os.environ.get("NO_COLOR"):
        return "nocolor"
    # 规则 3：CLICOLOR=0 → nocolor（termenv termenv.go:69；gh env.go:161-163）
    if os.environ.get("CLICOLOR") == "0":
        return "nocolor"
    # 规则 4：非 TTY（或无 isatty）→ nocolor（保住 spec 11 §3 管道纯文本可解析）
    isatty = getattr(stdout, "isatty", None)
    if not callable(isatty) or not isatty():
        return "nocolor"
    # 规则 5：TERM 缺失/空，或 dumb/unknown → nocolor
    # （colorprofile env.go:76-78；kubectl terminal.go:71；rich console.py:986-987）
    term = os.environ.get("TERM") or ""
    term_l = term.lower()
    if not term or term_l in ("dumb", "unknown"):
        return "nocolor"
    # 规则 6：Windows 按 build 号分档（termenv termenv_windows.go:24,39；
    # supports-color index.js:100-112；WT_SESSION 见 kubectl terminal.go:82；
    # getwindowsversion 不可用时按最低档处理）
    if os.name == "nt":
        _enable_windows_vt()
        if os.environ.get("WT_SESSION"):
            return "truecolor"
        try:
            build = sys.getwindowsversion().build
        except (AttributeError, OSError):
            return "nocolor"
        if build >= 14931:
            return "truecolor"
        if build >= 10586:
            return "color256"
        return "nocolor"
    # 规则 7：Unix TERM/COLORTERM 查表（colorprofile env.go:157-199 +
    # termenv termenv_unix.go:52-73 + rich _TERM_COLORS console.py:96-100；
    # 未知 TERM 默认 16 色，不是 truecolor——colorprofile env.go:143-155）
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("24bit", "truecolor"):
        return "truecolor"
    if os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "ghostty"):
        return "truecolor"
    if term_l in ("alacritty", "contour", "foot", "ghostty", "kitty", "rio",
                  "st", "wezterm", "xterm-ghostty", "xterm-kitty"):
        return "truecolor"
    if ("256color" in term_l or term_l.endswith("-256color")
            or colorterm in ("yes", "true")
            or term_l.startswith(("screen", "tmux"))):
        return "color256"
    return "ansi16"


def detect_theme(stdout=None) -> Theme:
    """检测终端能力，返回 Theme（两轴独立判定，无歧义）。

    检测对象是 `sys.stdout`（进程的真实标准输出），不是注入的 `write` 可调用对象
    （write 只是采集器，颜色能力取决于最终落盘/落屏的流）。每次调用现算，不缓存
    成模块全局（避免测试间 stdout 替换导致串扰）。
    """
    stream = sys.stdout if stdout is None else stdout
    return Theme(
        color=_grade_with_force(stream),
        glyph_set=_detect_glyph_set(stream),
    )


def _grade_with_force(stdout) -> str:
    """规则 1：按规则 2–7 算出 grade，若 CLICOLOR_FORCE 非空且 != "0" 且 grade 为
    nocolor → ansi16（termenv termenv.go:104-106；gh go-gh env.go:55,166-168 的
    保守子集）。"""
    grade = _grade_color(stdout)
    force = os.environ.get("CLICOLOR_FORCE")
    if force and force != "0" and grade == "nocolor":
        return "ansi16"
    return grade
