"""#1607 F4：dev.sh TUI node 版本闸回归。

`tui/package.json` engines 要求 node >= 22.1；旧闸只比较 major（`(( major < 22 ))`），
22.0.x 会被放行，随后在 npm install / 运行期才失败（红证与消融读数见 #1607 PR 描述）。

测试从 dev.sh 提取 `tui_node_ok` 的**真实文本**执行（测脚本字节本身，非副本），
用 bash 函数遮蔽注入受控 node 版本：不依赖真实 node、不触发 install/build。
定位 bash 沿用 `test_integration_scripts.py` 的 `_find_bash` 习气（跳过 System32 的 WSL bash）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEV_SH = REPO / "dev.sh"

_FUNC_RE = re.compile(r"^tui_node_ok\(\) \{.*?^\}", re.MULTILINE | re.DOTALL)
_START_TUI_RE = re.compile(r"^start_tui\(\) \{.*?^\}", re.MULTILINE | re.DOTALL)


def _find_bash() -> str | None:
    """定位 bash：Windows 上 `shutil.which` 可能解析到 System32 的 **WSL bash**
    （对 Windows cwd / 非 ASCII 路径直接失败），须跳过并落到 Git Bash 的固定安装位；
    Linux/CI 上 which 的结果即 /bin/bash，直接用。"""
    cands: list[str] = []
    w = shutil.which("bash")
    if w:
        cands.append(w)
    if os.name == "nt":
        cands += [r"C:\Program Files\Git\bin\bash.exe",
                  r"C:\Program Files\Git\usr\bin\bash.exe",
                  r"C:\Program Files (x86)\Git\bin\bash.exe"]
    for c in cands:
        if "system32" in os.path.normcase(c):
            continue
        if os.path.isfile(c):
            return c
    return None


BASH = _find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="需要非 WSL 的 bash（Git Bash / /bin/bash）")


def _bash_path(p: Path) -> str:
    """bash 侧统一用正斜杠路径（Windows 反斜杠在 bash 引号里会被当转义）。"""
    s = str(p)
    return s.replace("\\", "/") if os.name == "nt" else s


def _gate_rc(node_version: str, tmp_path: Path) -> int:
    """在受控 node 版本下执行 dev.sh 里的 tui_node_ok，返回其退出码（0=放行，1=拒绝）。"""
    match = _FUNC_RE.search(DEV_SH.read_text(encoding="utf-8"))
    assert match, "dev.sh 缺少 tui_node_ok()——版本闸被移走或改名，测试需同步"
    major, minor, *_ = node_version.split(".")
    script = "\n".join(
        [
            match.group(0),
            "node() { case \"$2\" in"
            f" *'[0]'*) echo '{major}' ;;"
            f" *'[1]'*) echo '{minor}' ;;"
            " *) echo '' ;; esac; }",
            "tui_node_ok",
        ]
    )
    script_file = tmp_path / "gate.sh"
    script_file.write_text(script, encoding="utf-8", newline="\n")
    proc = subprocess.run([BASH, _bash_path(script_file)], capture_output=True, text=True)
    return proc.returncode


@pytest.mark.parametrize(
    ("node_version", "expected_rc"),
    [
        ("21.9.0", 1),  # major 不够，必须拒绝
        ("22.0.0", 1),  # 旧闸放行的缺口：engines >=22.1 不含 22.0.x
        ("22.1.0", 0),  # 下边界恰好满足
        ("23.0.0", 0),  # 更高 major 放行
    ],
)
def test_tui_node_gate_verdicts(node_version: str, expected_rc: int, tmp_path: Path) -> None:
    assert _gate_rc(node_version, tmp_path) == expected_rc


def test_start_tui_calls_the_gate() -> None:
    """闸函数必须被 start_tui 实际调用，防止其变成无人引用的死代码。"""
    start_tui = _START_TUI_RE.search(DEV_SH.read_text(encoding="utf-8"))
    assert start_tui, "dev.sh 缺少 start_tui()"
    assert "tui_node_ok" in start_tui.group(0), "start_tui 未调用 tui_node_ok"
