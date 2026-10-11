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

折进 Skill 报告的 MCP 段是**共享叶子**：`skills` 的安装闸门与 `plugins` 的包生命周期
都要读它。把它放到一个**既不属于 skills 也不属于 plugins** 的中性模块
（`agent_harness.mcp_precheck`，与既有顶层的 `agent_harness.plugin_inspection` 同构），
依赖方向重新变成单向 `plugins → skills`，`skills` 不再反向依赖 `plugins`。
这正是 `attachments` 用中性模块破 `config ↔ attachments` 环的同款做法
（`tests/attachments/test_import_direction.py`），不是「靠延迟 import 躲环」。

## 判定口径

本用例在**子进程**里 import（避免被测试进程已加载的模块掩蔽），断言
① `import agent_harness.skills.package_manager` 不连带加载 `agent_harness.plugins`；
② `skills/` 的源码里没有任何指向 `agent_harness.plugins` 的 import 语句（机械守卫）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_ROOT / "src")
_SKILLS_DIR = _ROOT / "src" / "agent_harness" / "skills"


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=_SRC,
        env={"PYTHONPATH": _SRC, "PATH": "/usr/bin:/bin"},
        timeout=60,
        check=False,
    )


def test_importing_the_skills_package_manager_does_not_pull_in_plugins() -> None:
    """`skills` 不得在 import 期反向依赖 `plugins`（`plugins → skills` 单向）。"""
    proc = _run(
        "import sys, agent_harness.skills.package_manager;"
        "print('agent_harness.plugins' in sys.modules)"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False", (
        "import `agent_harness.skills.package_manager` 时拉进了 `agent_harness.plugins`——"
        "skills 反向依赖 plugins，`skills → plugins → skills` 成环（#974）"
    )


def test_skills_sources_never_import_the_plugins_package() -> None:
    """`skills/` 的源码里不得出现指向 `agent_harness.plugins` 的 import 语句。"""
    offenders: list[str] = []
    for path in sorted(_SKILLS_DIR.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if "agent_harness.plugins" in stripped or "agent_harness import plugins" in stripped:
                offenders.append(f"{path.relative_to(_ROOT)}:{lineno}: {stripped}")
    assert not offenders, (
        "`skills/` 出现反向 import `plugins/` 的语句，包边界偏离（#974）：\n"
        + "\n".join(offenders)
    )
