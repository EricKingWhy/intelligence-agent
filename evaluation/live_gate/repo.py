"""仓库事实：`sha` / `tree` / 工作树输入证明（`#307` Contracts 要求的前三项）。

## 判据只有一处实现

"工作树不得偏离 `HEAD`"这条判据的**实现**在 `scripts/gate0.py::worktree_divergence`（三条独立
检查：追踪文件偏离 / `assume-unchanged`·`skip-worktree` 位 / 未跟踪的车道输入后缀）。本模块
**直调它**，不另写一份 —— 判据分叉的后果是"Gate-0 说干净、Live Gate 说脏"这种无人能裁的僵局
（`AGENTS.md` §16.1：同一事实只在一处写全）。

`worktree.tracked_matches_head` 这个键名与 `docs/gate/<sha>.json` 里的同名字段**刻意一致**：
两份读数可以并排比对。语义 = 上述三条**一条都没触发**（含 `risky` 非空）。

本模块另有一条 gate0 没有的判据：**跑完之后**再取一次证明，与跑前对比 —— Live Gate 会在
开发仓库里执行真实工具，必须能证明"除了自己的证据目录，仓库没被这次运行改动"。自己的输出
目录（`docs/live_gate/`）与被排除的理由同 gate0 的 `GATE_DIR_PREFIX`（它也不是任何车道的输入）。
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

#: 仓库根（本文件位于 `<root>/evaluation/live_gate/repo.py`）。
REPO_ROOT = Path(__file__).resolve().parents[2]

#: 本 runner 自己的输出目录：跑前/跑后对比时排除（理由同 gate0 的 `GATE_DIR_PREFIX`）。
OUTPUT_DIR = Path("docs") / "live_gate"

#: gate0 的模块名（importlib 装载，避免把 `scripts/` 变成包、也避免改命令行工具的行为）。
_GATE0_MODULE_NAME = "_live_gate_gate0"


def git(*args: str) -> subprocess.CompletedProcess:
    """只读 git（**写操作一律不经本模块**）：cwd 锁定仓库根，UTF-8 解码。"""
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


def head_sha() -> str:
    return git("rev-parse", "HEAD").stdout.strip()


def tree_of(rev: str = "HEAD") -> str:
    return git("rev-parse", f"{rev}^{{tree}}").stdout.strip()


def _gate0() -> Any:
    """按路径装载 `scripts/gate0.py`（它没有包结构与导入副作用，见其文件尾的 `__main__` 守卫）。"""
    path = REPO_ROOT / "scripts" / "gate0.py"
    spec = importlib.util.spec_from_file_location(_GATE0_MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 环境损坏时的 fail-closed
        raise RuntimeError(f"无法装载 {path}（工作树判据的唯一实现）")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worktree_divergence() -> dict[str, list[str]]:
    """三条独立判据的原始读数（直调 gate0，不重新实现）。"""
    return _gate0().worktree_divergence()


def _rel(path: str) -> str:
    return path.replace("\\", "/")


def worktree_proof() -> dict[str, Any]:
    """工作树输入证明（跑前 / 跑后各取一次，见模块 docstring）。

    本 runner 自己的输出目录（`docs/live_gate/`）在取数时就**排除**：跑完必然多出证据文件，
    不排除的话"跑后比跑前"恒为不等（gate0 排除 `GATE_DIR` 是同一形状）。
    """
    divergence = worktree_divergence()
    prefix = _rel(str(OUTPUT_DIR)) + "/"

    def keep(lines: list[str]) -> list[str]:
        return [line for line in lines if not _rel(line[3:].strip()).startswith(prefix)]

    tracked, hidden = divergence["tracked"], divergence["hidden"]
    risky, untracked = keep(divergence["risky"]), keep(divergence["untracked"])
    return {
        "head_sha": head_sha(),
        "tree": tree_of(),
        "tracked_matches_head": not (tracked or hidden or risky),
        # 清单截断到 20 条是给人看的；**判定必须看全量**（截断后比对会让"第 21 个文件出现"
        # 变成看不见的变化）⇒ 完整计数另存 `_counts`，指纹只比它。
        "untracked": [_rel(line[3:].strip()) for line in untracked][:20],
        "hidden": [_rel(line) for line in hidden][:20],
        "risky": [_rel(line[3:].strip()) for line in risky][:20],
        "_counts": {
            "tracked": len(tracked), "hidden": len(hidden),
            "risky": len(risky), "untracked": len(untracked),
        },
    }


def _fingerprint(proof: dict[str, Any]) -> dict[str, Any]:
    """跑前/跑后可比对的指纹（只比完整量，不比给人看的截断清单）。"""
    return {
        "head_sha": proof["head_sha"],
        "tree": proof["tree"],
        "tracked_matches_head": proof["tracked_matches_head"],
        "_counts": proof["_counts"],
    }


def repo_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """跑前/跑后指纹是否一致（"开发仓库没有任务副作用"的可机械判定形式）。"""
    return _fingerprint(before) == _fingerprint(after)
