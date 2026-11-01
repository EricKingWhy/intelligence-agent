"""#1662：`_RECEIPT_BOOTSTRAP_SHA` 必须是**本仓 HEAD 的祖先**。

为什么这条值得一条用例：锚点的失效形态是**静默 fail-open**。`receipt_present_check` 对
`merge-base --is-ancestor <bootstrap> <c>` 失败（退出码 ≠ 0）的提交直接 `continue`，注释写的是
「早于 bootstrap ⇒ 范围外」。锚点若写成不相干的 sha（或笔误的 sha），check 逻辑本身分辨不出
「这个锚点与本范围无关」，于是**范围内全部提交都被机械豁免**——判据看起来还在、实际被挖空。
2026-11-01 #1662 选锚时实测过这个形状：origin/main tip `2b04fa767` 不是本分支 HEAD 的祖先。
锚点不在仓里（拼错 / 分支未 fetch）是同一族的更强形态，一并钉住。

判别力：把常量改回一个非祖先 sha ⇒ 两条断言都转红。
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE0_PATH = REPO / "scripts" / "gate0.py"


def _load_gate0():
    spec = importlib.util.spec_from_file_location("_gate0_i1662", GATE0_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate0 = _load_gate0()


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=False)


def test_bootstrap_anchor_exists_and_is_ancestor_of_head():
    anchor = gate0.receipt_bootstrap_sha()
    assert anchor and len(anchor) == 40, anchor

    resolved = _git("rev-parse", "--verify", f"{anchor}^{{commit}}")
    assert resolved.returncode == 0, f"锚点 {anchor} 在仓里解析不出：{resolved.stderr.strip()}"

    ancestor = _git("merge-base", "--is-ancestor", anchor, "HEAD")
    assert ancestor.returncode == 0, (
        f"锚点 {anchor} 不是 HEAD 的祖先 ⇒ receipt-present 会把范围内全部提交机械豁免"
        f"（判据被挖空，fail-open）：{ancestor.stderr.strip()}"
    )
