"""#668：gate0 车道子进程必须剥离继承的 GIT_* 环境。

pre-push hook 上下文里 git 会注入 GIT_DIR（linked worktree push 时是绝对路径）等
GIT_* 变量；guards/focused 等车道里的嵌套 git 测试继承后会把 tmp 仓库操作劫持到
外层仓库（2026-10-05 实证：guards 伪红 + 共享 config 被翻 core.bare + 分支被提交
垃圾 commit）。车道子进程的 git 一律靠 cwd 解析仓库 ⇒ 继承的 GIT_* 全部无必要，
run_lane（spawn 汇聚点）与 validate 复核路径统一经 `_git_env_clean` 剥离。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE0_PATH = REPO / "scripts" / "gate0.py"


def _load_gate0():
    spec = importlib.util.spec_from_file_location("_gate0_i668", GATE0_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate0 = _load_gate0()


def test_git_env_clean_strips_git_keys_and_keeps_rest():
    dirty = {
        "GIT_DIR": "D:/elsewhere/.git",
        "GIT_QUARANTINE_PATH": "D:/q",
        "GIT_CONFIG_PARAMETERS": "'core.hooksPath'='x'",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUTF8": "1",
    }
    cleaned = gate0._git_env_clean(dirty)
    assert not [k for k in cleaned if k.startswith("GIT_")]
    assert cleaned["PYTHONUTF8"] == "1"
    assert cleaned["PATH"] == dirty["PATH"]


def test_git_env_clean_does_not_mutate_input():
    dirty = {"GIT_DIR": "x", "KEEP": "1"}
    snapshot = dict(dirty)
    gate0._git_env_clean(dirty)
    assert dirty == snapshot


def test_git_env_clean_none_uses_process_env(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "D:/hook/.git")
    monkeypatch.setenv("PYTHONUTF8", "1")
    cleaned = gate0._git_env_clean(None)
    assert "GIT_DIR" not in cleaned
    assert cleaned["PYTHONUTF8"] == "1"


def test_run_lane_child_sees_no_git_env(monkeypatch):
    """行为钉：车道子进程在 GIT_DIR 污染的父环境下运行也必须看不到任何 GIT_*。"""
    monkeypatch.setenv("GIT_DIR", "D:/hook/.git")
    probe = (
        "import os,sys;"
        "hits=[k for k in os.environ if k.startswith('GIT_')];"
        "print(len(hits))"
    )
    lane = gate0.Lane(
        "git-env-probe", "GIT_* 泄漏探针", [sys.executable, "-c", probe], gate0.REPO_ROOT
    )
    rc, _elapsed, output = gate0.run_lane(lane)
    assert rc == 0, output
    assert output.strip().endswith("0"), f"车道子进程仍看到 GIT_*：{output}"


def test_receipt_lane_env_strips_git_keys():
    """validate 复核路径与 run_lane 同源：落盘 env ∪ 父环境后仍要剥离 GIT_*。"""
    merged = gate0._receipt_lane_env({"env": {"PYTHONUTF8": "1"}})
    assert "GIT_DIR" not in merged
    assert merged["PYTHONUTF8"] == "1"
