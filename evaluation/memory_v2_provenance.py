"""Bind Memory V2 evaluation evidence to one verified committed tree."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from scripts.gate0 import worktree_divergence

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SHA = re.compile(r"^[0-9a-f]{40}$")


def capture_code_identity() -> dict[str, str]:
    """Return HEAD identity only when tracked, hidden-index, and risky inputs are clean."""
    code_sha = _git("rev-parse", "HEAD")
    tree_sha = _git("rev-parse", f"{code_sha}^{{tree}}")
    divergence = worktree_divergence()
    if any(divergence[key] for key in ("tracked", "hidden", "risky")):
        raise RuntimeError("evaluation requires a verified committed worktree")
    if (code_sha != _git("rev-parse", "HEAD")
            or tree_sha != _git("rev-parse", "HEAD^{tree}")):
        raise RuntimeError("repository identity changed during evaluation setup")
    return {"code_sha": code_sha, "tree_sha": tree_sha}


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=_REPOSITORY_ROOT, capture_output=True,
        text=True, timeout=5, check=False,
    )
    value = result.stdout.strip()
    if result.returncode or not _SHA.fullmatch(value):
        raise RuntimeError("cannot determine committed evaluation identity")
    return value
