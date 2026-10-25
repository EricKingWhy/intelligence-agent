"""#1597：`scripts/integration/{update-pr,poll-pr}` 的最小冒烟测试。

不依赖真实 PR / 网络：用临时 bare origin + 工作 clone 构造「分支与 origin/main 分叉」的
集成现场，按退出码与输出断言各形态（沿用 `test_gate0_receipt_present.py` 的干净 git env
先例：剥 `GIT_*`、屏蔽全局/系统 config，#668 hook 劫持与本机差异都不进结果）。
覆盖闸门一律经 `--coverage-cmd` 注入假命令，隔离真实台账。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UPDATE_PR = REPO / "scripts" / "integration" / "update-pr"
POLL_PR = REPO / "scripts" / "integration" / "poll-pr"

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


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1"})
    return env


def _bash_path(p: Path) -> str:
    """bash 侧统一用正斜杠路径（Windows 反斜杠在 bash 引号里会被当转义）。"""
    s = str(p)
    return s.replace("\\", "/") if os.name == "nt" else s


def _run(script: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # bash 输出为 UTF-8；Windows 默认 locale 解码会炸 reader 线程
    return subprocess.run([BASH, _bash_path(script), *args], cwd=cwd, env=_env(),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False)


class Fixture:
    """bare origin + 工作 clone：main、feature 自 base 分叉后各自前进一笔。"""

    BASE_FILES = {
        "README.md": "base\n",
        "docs/PHASE_STATUS.md": "# 进度\n",
        "docs/SDD_TICKET_TRACKER.md": "# 票台账\n",
        "docs/phase_status/2026-10.md": "# 月度明细\n",
        "src/app.py": "print('base')\n",
    }

    def __init__(self, root: Path):
        self.root = root
        self.work = root / "work"
        self.work.mkdir(parents=True)
        self.g("init", "-q", "-b", "main")
        self.g("remote", "add", "origin", _bash_path(root / "origin.git"))
        self.g("config", "user.email", "t@example.com")
        self.g("config", "user.name", "t")
        self.g("config", "commit.gpgsign", "false")
        proc = subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", _bash_path(root / "origin.git")],
            env=_env(), capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr
        self.commit(self.BASE_FILES, "base", refs=["main"])

    def g(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=self.work, env=_env(),
                              capture_output=True, text=True, check=False)
        assert proc.returncode == 0, f"git {args} rc={proc.returncode}: {proc.stderr}"
        return proc.stdout.strip()

    def commit(self, files: dict[str, str], msg: str, branch: str | None = None,
               refs: list[str] | None = None) -> str:
        if branch:
            self.g("switch", "-q", branch)
        for rel, content in files.items():
            p = self.work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        self.g("add", "-A")
        self.g("commit", "-q", "-m", msg)
        sha = self.g("rev-parse", "HEAD")
        if branch:
            self.g("push", "-q", "origin", f"HEAD:{branch}")
        for r in refs or []:
            self.g("push", "-q", "origin", f"HEAD:{r}")
        return sha

    def remote_sha(self, branch: str) -> str:
        out = self.g("ls-remote", "origin", f"refs/heads/{branch}")
        return out.split("\t")[0] if out else ""

    def new_branch(self, name: str, files: dict[str, str], msg: str) -> str:
        self.g("switch", "-q", "-c", name)
        return self.commit(files, msg, branch=name)


def test_update_pr_help():
    proc = _run(UPDATE_PR, ["--help"], cwd=REPO)
    assert proc.returncode == 0
    assert "用法" in proc.stdout and "--coverage-cmd" in proc.stdout
    assert "退出码" in proc.stdout and "COVERAGE-RED" in proc.stdout


def test_poll_pr_help():
    proc = _run(POLL_PR, ["--help"], cwd=REPO)
    assert proc.returncode == 0
    assert "用法" in proc.stdout and "SUCCESS" in proc.stdout


def test_docs_union_coverage_green_push(tmp_path):
    """docs 三件套冲突自动并集、覆盖闸门绿、默认推送。"""
    fx = Fixture(tmp_path)
    fx.new_branch("feature", {"docs/PHASE_STATUS.md": "# 进度\n- feature 侧条目\n"}, "feat: 追加")
    fx.commit({"docs/PHASE_STATUS.md": "# 进度\n- main 侧条目\n"}, "main: 追加", branch="main")

    proc = _run(UPDATE_PR, ["1597", "feature", "--repo", _bash_path(fx.work),
                            "--coverage-cmd", "exit 0"], cwd=fx.work)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "UNION-RESOLVED: docs/PHASE_STATUS.md" in proc.stdout
    assert "coverage: exit=0" in proc.stdout and "pushed" in proc.stdout
    merged = (fx.work / "docs" / "PHASE_STATUS.md").read_text(encoding="utf-8")
    assert "- feature 侧条目" in merged and "- main 侧条目" in merged
    assert fx.remote_sha("feature") == fx.g("rev-parse", "HEAD")


def test_no_push_keeps_remote(tmp_path):
    fx = Fixture(tmp_path)
    feat = fx.new_branch("feature", {"docs/note.md": "feat\n"}, "feat: 文档")
    fx.commit({"docs/other.md": "main\n"}, "main: 文档", branch="main")

    proc = _run(UPDATE_PR, ["1597", "feature", "--repo", _bash_path(fx.work),
                            "--coverage-cmd", "exit 0", "--no-push"], cwd=fx.work)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "未推送" in proc.stdout
    assert fx.remote_sha("feature") == feat


def test_manual_conflict_exit3(tmp_path):
    """非 docs 进度文件冲突 ⇒ 停在现场（exit 3），不 push。"""
    fx = Fixture(tmp_path)
    feat = fx.new_branch("feature", {"src/app.py": "print('feature')\n"}, "feat: 改同line")
    fx.commit({"src/app.py": "print('main')\n"}, "main: 改同line", branch="main")

    proc = _run(UPDATE_PR, ["1597", "feature", "--repo", _bash_path(fx.work),
                            "--coverage-cmd", "exit 0"], cwd=fx.work)
    assert proc.returncode == 3
    assert "MANUAL-CONFLICT" in proc.stdout and "src/app.py" in proc.stdout
    assert fx.remote_sha("feature") == feat


def test_coverage_red_empty_cc_hint(tmp_path):
    """空 --cc 且 tree≠父 + 覆盖闸门红 ⇒ exit 4 + 「先落机械归属行」显式提示，不 push。"""
    fx = Fixture(tmp_path)
    feat = fx.new_branch("feature", {"docs/note.md": "feat\n"}, "feat: 文档")
    fx.commit({"src/new.py": "print('main')\n"}, "main: 代码", branch="main")

    proc = _run(UPDATE_PR, ["1597", "feature", "--repo", _bash_path(fx.work),
                            "--coverage-cmd", "exit 1"], cwd=fx.work)
    assert proc.returncode == 4
    assert "COVERAGE-RED" in proc.stdout
    assert "空 --cc 且 tree≠父" in proc.stdout
    assert "机械归属行" in proc.stdout and "review_ledger.d" in proc.stdout
    assert "rev-parse" in proc.stdout  # 反手补零教训进了提示
    assert fx.remote_sha("feature") == feat


def test_code_in_cc_exit5(tmp_path):
    """两侧各改 src/app.py 不同区域（干净自动合并）⇒ --cc 含代码 ⇒ exit 5 停在 push 前。"""
    fx = Fixture(tmp_path)
    feat = fx.new_branch("feature", {"src/app.py": "print('base')\n# feature 区\n"}, "feat: 尾部")
    fx.commit({"src/app.py": "# main 区\nprint('base')\n"}, "main: 头部", branch="main")

    proc = _run(UPDATE_PR, ["1597", "feature", "--repo", _bash_path(fx.work),
                            "--coverage-cmd", "exit 0"], cwd=fx.work)
    assert proc.returncode == 5
    assert "CODE-IN-CC" in proc.stdout and "src/app.py" in proc.stdout
    assert "STOPPED-BEFORE-PUSH" in proc.stdout
    assert fx.remote_sha("feature") == feat
