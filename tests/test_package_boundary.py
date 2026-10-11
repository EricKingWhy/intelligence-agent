"""`skills/` **不得**反向 import `plugins/` 的包边界闸门（#974，源自 #875 收尾残余）。

## 它守什么

`plugins/__init__.py` 自述的族级分层是：每个车道自己的预检住在自己的车道里
（`agent_harness.skills` 的预检不搬迁），`plugins/` 放的是跨车道的**包生命周期**
（跨市场导入的安装/版本/启用，`plugins/mcp_package_manager.py`）。改动前
`skills/` 对 `plugins/` **零 import**；#875 修回把「MCP 缺口折进安装闸门」时，
`skills/package_manager.py` 直接 `from agent_harness.plugins.mcp_precheck import
attach_mcp_section`，于是 `skills → plugins` 与既有的 `plugins → skills` 成环，
包边界偏离（Standards 轴 P1）。

## 为什么是「抽公共模块」而不是延迟 import / 注入

折进 Skill 报告的 MCP 段是两车道**共享**的一层：`skills` 的安装闸门与 `plugins` 的包
生命周期都要读它。把它放到一个**既不属于 skills 也不属于 plugins** 的中性顶层模块
（`agent_harness.mcp_precheck`，与既有顶层的 `agent_harness.plugin_inspection` 同构），
依赖方向重新变成单向 `plugins → skills`，`skills` 不再反向依赖 `plugins`。这正是
`attachments` 用中性模块破 `config ↔ attachments` 环的同款做法
（`tests/attachments/test_import_direction.py`），不是「靠延迟 import 躲环」。

## 判定口径

本用例在**子进程**里 import（避免被测试进程已加载的模块掩蔽），断言
① `agent_harness.skills` 下的**每个**模块 import 期都不连带加载 `agent_harness.plugins`
（import 图本身是判据，故对绝对 / 相对 import 与 import 位置都成立）；
② `skills/` 源码里没有任何指向 `agent_harness.plugins` 的 import 语句——用 `ast`
把每条 import 解析成**绝对**点分模块名后再判（覆盖 `from ..plugins import ...`
这类相对形式；纯子串匹配会漏）。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_ROOT / "src")
_SKILLS_DIR = _ROOT / "src" / "agent_harness" / "skills"
_PLUGINS_PACKAGE = "agent_harness.plugins"


def _run(code: str) -> subprocess.CompletedProcess[str]:
    env = {"PYTHONPATH": _SRC, "PATH": "/usr/bin:/bin"}
    if os.name == "nt":
        env["SystemRoot"] = os.environ.get("SystemRoot", "")
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_SRC,
        env=env,
        timeout=60,
        check=False,
    )


def test_importing_every_skills_module_does_not_pull_in_plugins() -> None:
    """`skills` 不得在 import 期反向依赖 `plugins`（`plugins → skills` 单向）。

    import **整包**的所有子模块，而不是单个 `package_manager`：反向边可能被加进
    别的 skills 模块（或写成相对 import），只 import 一个模块会漏掉它。
    """
    proc = _run(
        "import importlib, pkgutil, sys;"
        "import agent_harness.skills as skills;"
        "[importlib.import_module(m.name) for m in "
        "pkgutil.iter_modules(skills.__path__, skills.__name__ + '.')];"
        "print('agent_harness.plugins' in sys.modules)"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", (
        "import `agent_harness.skills` 下的模块时拉进了 `agent_harness.plugins`——"
        "skills 反向依赖 plugins，`skills → plugins → skills` 成环（#974）"
    )


def _absolute_import_targets(path: Path) -> list[tuple[int, str]]:
    """`path` 里每条 import 解析出的**绝对**点分目标（from ... import name 也算 name）。

    相对 import 按模块自身所在包上推：`skills/` 下无子包，故包名恒为
    `agent_harness.skills`，`from ..plugins import x` ⇒ `agent_harness.plugins`。
    """
    package = ".".join(path.relative_to(_ROOT / "src").with_suffix("").parts[:-1])
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - (node.level - 1)] + ([base] if base else []))
            targets.append((node.lineno, base))
            targets += [
                (node.lineno, f"{base}.{alias.name}" if base else alias.name)
                for alias in node.names
            ]
    return targets


def test_skills_sources_never_import_the_plugins_package() -> None:
    """`skills/` 源码里不得出现任何指向 `agent_harness.plugins` 的 import（含相对形式）。"""
    offenders: list[str] = []
    for path in sorted(_SKILLS_DIR.rglob("*.py")):
        for lineno, target in _absolute_import_targets(path):
            if target == _PLUGINS_PACKAGE or target.startswith(_PLUGINS_PACKAGE + "."):
                offenders.append(f"{path.relative_to(_ROOT)}:{lineno}: import {target}")
    assert not offenders, (
        "`skills/` 出现反向 import `plugins/` 的语句，包边界偏离（#974）：\n"
        + "\n".join(offenders)
    )
