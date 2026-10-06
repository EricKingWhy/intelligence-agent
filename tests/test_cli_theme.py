"""P0-1 终端主题模块测试：颜色角色 + glyph 预设 + 能力检测（票面 §2 验收标准）。

检测对象是 sys.stdout 能力（编码/TTY），不是业务状态（spec 11 §6 不违反）。
环境变量用 monkeypatch 隔离；颜色档断言锁死 2026-10-06 challenge 修订后的
查表行为（TERM=xterm → ansi16，不是 truecolor）。
"""

import os
import sys
from types import SimpleNamespace

import pytest

from agent_harness import cli_theme
from agent_harness.cli import StreamRenderer
from agent_harness.cli_theme import ROLES, Theme, detect_theme


class _FakeStdout:
    """假 stdout：encoding/isatty 可控；write 走严格编码（真实编码边界，
    unicode 字形漏进 ascii 流会立刻抛 UnicodeEncodeError）。"""

    def __init__(self, encoding: str = "utf-8", tty: bool = True) -> None:
        self.encoding = encoding
        self._tty = tty
        self.chunks: list[str] = []

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        self.chunks.append(text.encode(self.encoding, errors="strict").decode(self.encoding))
        return len(text)


@pytest.fixture
def clean_env(monkeypatch):
    """清掉本机环境里可能存在的检测变量，保证判定确定性。"""
    for var in ("NO_COLOR", "CLICOLOR", "CLICOLOR_FORCE", "COLORTERM",
                "TERM_PROGRAM", "WT_SESSION"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def posix_env(clean_env, monkeypatch):
    """钉 Unix 查表分支：os.name 必须钉——Windows 宿主上规则 6 按 build 号短路
    返回 truecolor/color256，规则 7 的 TERM/COLORTERM 查表不可达（与
    TestWindowsGrading.win_env 同理反向）。宿主 TERM（Git Bash 继承的
    xterm-256color）一并清除，防泄漏进未显式 setenv 的参数化用例。"""
    monkeypatch.setattr(os, "name", "posix")
    clean_env.delenv("TERM", raising=False)
    return clean_env


class TestNoColor:
    def test_no_color_set_yields_nocolor(self, clean_env):
        clean_env.setenv("NO_COLOR", "1")
        theme = detect_theme(_FakeStdout(tty=True))
        assert theme.color == "nocolor"
        assert Theme(color="nocolor").paint("err", "x") == "x"

    def test_no_color_empty_string_does_not_trigger(self, posix_env):
        posix_env.setenv("NO_COLOR", "")
        posix_env.setenv("TERM", "xterm")
        theme = detect_theme(_FakeStdout(tty=True))
        assert theme.color == "ansi16"

    def test_c6_regression_no_color_plus_ascii_encoding(self, clean_env):
        """NO_COLOR=1 + ascii 编码 → nocolor 且 ascii 字形，写入不抛
        UnicodeEncodeError（C6(1) hole 修复：两轴正交）。"""
        clean_env.setenv("NO_COLOR", "1")
        fake = _FakeStdout(encoding="ascii", tty=True)
        theme = detect_theme(fake)
        assert theme.color == "nocolor"
        assert theme.glyph_set == "ascii"
        assert theme.glyph("ok") == "[ok]"
        fake.write(theme.glyph("pending"))  # 不抛 UnicodeEncodeError


class TestGlyphSetAxis:
    def test_ascii_encoding_probes_to_ascii_glyphs(self, posix_env):
        posix_env.setenv("TERM", "xterm-256color")
        theme = detect_theme(_FakeStdout(encoding="ascii", tty=True))
        assert theme.glyph_set == "ascii"
        assert theme.glyph("fail") == "[!!]"
        assert theme.sep() == " - "
        assert theme.color == "color256"  # 颜色照 B 轴独立判定：ascii 字形 + 彩色

    def test_utf8_encoding_keeps_unicode_glyphs(self, clean_env):
        clean_env.setenv("NO_COLOR", "1")
        theme = detect_theme(_FakeStdout(encoding="utf-8", tty=True))
        assert theme.glyph_set == "unicode"
        assert theme.glyph("pending") == "●"
        assert theme.sep() == " · "


class TestColorAxisPriority:
    def test_non_tty_yields_nocolor(self, clean_env):
        clean_env.setenv("TERM", "xterm-256color")
        theme = detect_theme(_FakeStdout(tty=False))
        assert theme.color == "nocolor"

    def test_clicolor_zero_yields_nocolor(self, clean_env):
        clean_env.setenv("CLICOLOR", "0")
        clean_env.setenv("TERM", "xterm-256color")
        theme = detect_theme(_FakeStdout(tty=True))
        assert theme.color == "nocolor"

    @pytest.mark.parametrize("term", ["dumb", ""])
    def test_dumb_or_empty_term_yields_nocolor(self, posix_env, term):
        if term:
            posix_env.setenv("TERM", term)
        theme = detect_theme(_FakeStdout(tty=True))
        assert theme.color == "nocolor"

    def test_missing_term_yields_nocolor(self, clean_env):
        clean_env.delenv("TERM", raising=False)
        theme = detect_theme(_FakeStdout(tty=True))
        assert theme.color == "nocolor"


class TestTermTable:
    """TERM/COLORTERM 查表分级（防"一律真彩"回归，2026-10-06 challenge 新增）。"""

    def test_xterm_256color_yields_color256(self, posix_env):
        posix_env.setenv("TERM", "xterm-256color")
        assert detect_theme(_FakeStdout(tty=True)).color == "color256"

    def test_colorterm_truecolor_yields_truecolor(self, posix_env):
        posix_env.setenv("TERM", "xterm")
        posix_env.setenv("COLORTERM", "truecolor")
        assert detect_theme(_FakeStdout(tty=True)).color == "truecolor"

    def test_plain_xterm_yields_ansi16_not_truecolor(self, posix_env):
        """TERM=xterm → ansi16（旧规则会给出 truecolor，此断言锁死新行为）。"""
        posix_env.setenv("TERM", "xterm")
        assert detect_theme(_FakeStdout(tty=True)).color == "ansi16"

    def test_screen_256color_yields_color256(self, posix_env):
        posix_env.setenv("TERM", "screen-256color")
        assert detect_theme(_FakeStdout(tty=True)).color == "color256"


class TestClicolorForce:
    def test_force_lifts_no_color_to_ansi16(self, clean_env):
        """termenv 语义：FORCE 只在 nocolor 时保底 16 色（NO_COLOR 仍优先于分级）。"""
        clean_env.setenv("NO_COLOR", "1")
        clean_env.setenv("CLICOLOR_FORCE", "1")
        theme = detect_theme(_FakeStdout(tty=True))
        assert theme.color == "ansi16"

    def test_force_lifts_pipe_to_ansi16(self, clean_env):
        clean_env.setenv("CLICOLOR_FORCE", "1")
        theme = detect_theme(_FakeStdout(tty=False))
        assert theme.color == "ansi16"

    def test_force_zero_string_is_not_forced(self, clean_env):
        clean_env.setenv("TERM", "dumb")
        clean_env.setenv("CLICOLOR_FORCE", "0")
        assert detect_theme(_FakeStdout(tty=True)).color == "nocolor"


class TestWindowsGrading:
    """Windows 按 build 号分档（termenv termenv_windows.go:24,39）。"""

    @pytest.fixture
    def win_env(self, clean_env, monkeypatch):
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(cli_theme, "_VT_ENABLED", False)
        clean_env.setenv("TERM", "xterm")  # 过规则 5（TERM 缺失 → nocolor），才能走到规则 6
        return clean_env

    @pytest.mark.parametrize(("build", "expected"), [
        (18363, "truecolor"),
        (10586, "color256"),
        (10240, "nocolor"),
    ])
    def test_build_grading(self, win_env, monkeypatch, build, expected):
        monkeypatch.setattr(sys, "getwindowsversion",
                            lambda: SimpleNamespace(build=build), raising=False)
        theme = detect_theme(_FakeStdout(tty=True))
        assert theme.color == expected

    def test_os_system_called_at_most_once_across_calls(self, win_env, monkeypatch):
        monkeypatch.setattr(sys, "getwindowsversion",
                            lambda: SimpleNamespace(build=18363), raising=False)
        calls: list[str] = []
        monkeypatch.setattr(os, "system", lambda cmd: calls.append(cmd) or 0)
        for _ in range(3):
            detect_theme(_FakeStdout(tty=True))
        assert len(calls) <= 1  # 模块级 guard：每进程最多一次


class TestPaint:
    def test_ansi16_err_mapping(self):
        assert "\x1b[31m" in Theme(color="ansi16").paint("err", "x")

    def test_ansi16_accent_mapping(self):
        assert "\x1b[95m" in Theme(color="ansi16").paint("accent", "x")

    def test_ansi16_text_verbatim(self):
        assert Theme(color="ansi16").paint("text", "x") == "x"

    def test_text_role_verbatim_in_truecolor(self):
        assert Theme(color="truecolor").paint("text", "x") == "x"

    def test_invalid_role_raises(self):
        with pytest.raises(ValueError):
            Theme(color="truecolor").paint("nope", "x")

    def test_all_roles_colorized_in_truecolor_and_plain_in_nocolor(self):
        for role in ROLES:
            if role == "text":  # text 角色任何 color 档都原样返回（票面语义）
                continue
            assert "\x1b[" in Theme(color="truecolor").paint(role, "x")
            assert Theme(color="nocolor").paint(role, "x") == "x"

    def test_truecolor_accent_carries_pale_blossom_rgb(self):
        assert Theme(color="truecolor").paint("accent", "x") == \
            "\x1b[38;2;241;179;202mx\x1b[0m"  # #f1b3ca

    def test_color256_accent_is_218(self):
        assert Theme(color="color256").paint("accent", "x") == "\x1b[38;5;218mx\x1b[0m"


class TestStreamRendererInjection:
    def test_explicit_theme_used_unconditionally(self):
        out: list[str] = []
        theme = Theme(color="ansi16")
        renderer = StreamRenderer(out.append, theme=theme)
        assert renderer._theme is theme

    def test_default_theme_detected_at_construction(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        assert isinstance(renderer._theme, Theme)
