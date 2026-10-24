"""#1553：`receipt_present_check` 形状④（分支冻结 tip 覆盖）的判别力与 ①②③ 回归。

形状④（用户 2026-10-23 亲裁 A 案）：提交 c 被覆盖，若 HEAD 树中存在有效收据，其被测树 t 满足
  ① t 在本检查区间内（`merge-base(origin/main, HEAD)..HEAD`）；
  ② c 是 t 的祖先；
  ③ t 之后至 HEAD 的**每一笔**提交都是纯 docs 或纯收据落盘（复用 `is_pure_docs_commit` /
     `is_pure_receipt_landing`；任一笔不满足 ⇒ ④ 对该 t 不成立，fail-closed）。

依据：被测的是**最终合入的那棵树**。tip 树实跑过全量、其后无代码改动 ⇒ 中间提交的代码已被
包含在被测树内（与 PR #1336「7 笔代码提交、1 枚冻结 tip 读数」及 SDD §8.1 读数传递同源）。

本文件用真 git 仓库（tmp_path）构造检查区间，直接调 `receipt_present_check()`；不跑门禁车道。
bootstrap 一律显式传 base、且把 base 的提交时间推到 8 天前 ⇒ `warn_only=False`（转硬口径）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATE0_PATH = REPO / "scripts" / "gate0.py"

#: 8 天前：保证 `bootstrap + 7 天 < now` ⇒ 转硬口径（宽限窗已过）。
_OLD_DATE = "2026-10-01T00:00:00+00:00"


def _load_gate0():
    spec = importlib.util.spec_from_file_location("_gate0_i1553", GATE0_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate0 = _load_gate0()


def _env() -> dict:
    """剥掉继承的 GIT_*：否则测试钩子/CI 的 GIT_DIR 会把 tmp 仓库操作劫持到外层仓库（#668）。"""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class Repo:
    """tmp 仓库：`origin/main` 指向 base，提交日期固定在区间外（转硬口径）。"""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        env = _env()
        env.update({"GIT_AUTHOR_DATE": _OLD_DATE, "GIT_COMMITTER_DATE": _OLD_DATE})
        proc = subprocess.run(["git", *args], cwd=self.root, env=env,
                              capture_output=True, text=True, check=False)
        assert proc.returncode == 0, f"git {args} rc={proc.returncode}: {proc.stderr}"
        return proc.stdout.strip()

    def commit(self, files: dict[str, str], msg: str) -> str:
        for rel, content in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def empty_commit(self, msg: str) -> str:
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def remove(self, rel: str, msg: str) -> str:
        (self.root / rel).unlink()
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def receipt(self, tested_sha: str) -> dict[str, str]:
        """被测树 sha 的合法收据：文件名=sha，tree 与 `rev-parse` 核得上，PASS 且 passed==total。"""
        tree = self.git("rev-parse", f"{tested_sha}^{{tree}}")
        body = {"schema": 1, "gate": "gate0", "sha": tested_sha, "tree": tree,
                "lanes": [], "wall_seconds": 1.0, "tool_versions": {"pytest": "1"},
                "result": "PASS", "passed": 7, "total": 7}
        return {f"docs/gate/{tested_sha}.json": json.dumps(body)}


def _base_with_origin(repo: Repo) -> str:
    base = repo.commit({"README.md": "base\n"}, "base")
    repo.git("update-ref", "refs/remotes/origin/main", base)
    return base


def _check(repo: Repo, base: str) -> tuple[bool, str, bool]:
    return gate0.receipt_present_check(repo_root=str(repo.root), bootstrap_sha=base)


# ── 判别力四项 ──


def test_shape4_tip_receipt_covers_multi_commit_branch(tmp_path):
    """正例：多笔代码提交 + tip 收据 + 其后仅 docs/收据提交 ⇒ 转硬口径下 ok=True、零违规。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    tip = repo.commit({"src/c.py": "c = 3\n"}, "code 3")
    repo.commit(repo.receipt(tip), "docs(gate): receipt for tip")
    repo.commit({"docs/notes.md": "notes\n"}, "docs: after receipt")

    ok, detail, warn = _check(repo, base)

    assert warn is False, f"本用例要求转硬口径：{detail}"
    assert ok is True, detail
    assert "违规 0 笔" in detail, detail


def test_shape4_rejects_code_commit_after_tested_tree(tmp_path):
    """反例一：t 之后再进一笔代码提交 ⇒ 该笔（及失去 ④ 覆盖的整支）不被覆盖。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    tip = repo.commit({"src/c.py": "c = 3\n"}, "code 3")
    repo.commit(repo.receipt(tip), "docs(gate): receipt for tip")
    late = repo.commit({"src/d.py": "d = 4\n"}, "code 4 after receipt")

    ok, detail, warn = _check(repo, base)

    assert warn is False, detail
    assert ok is False, detail
    assert f"missing receipt for {late[:12]}" in detail, detail
    # 条件③ fail-closed：tip 收据不再覆盖任何更早的代码提交（t..HEAD 里有代码）。
    # 违规 = code1 / code2 / code4 三笔；code3 仍有自身收据（形状①），与④无关。
    assert "违规 3 笔" in detail, detail


def test_shape4_requires_a_receipt_at_all(tmp_path):
    """反例二：无 tip 收据 ⇒ violations 按原口径出现。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    first = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    second = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.commit({"docs/notes.md": "notes\n"}, "docs only")

    ok, detail, warn = _check(repo, base)

    assert warn is False, detail
    assert ok is False, detail
    assert f"missing receipt for {first[:12]}" in detail, detail
    assert f"missing receipt for {second[:12]}" in detail, detail
    assert "违规 2 笔" in detail, detail


def test_shape4_tail_must_be_docs_or_receipts_only(tmp_path):
    """条件③ fail-closed：t 之后删除 docs 文件（状态 D）也不算「纯 docs」。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"docs/old.md": "old\n"}, "docs 1")
    tip = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(tip), "docs(gate): receipt for tip")
    repo.remove("docs/old.md", "docs: delete a file after receipt")

    ok, detail, warn = _check(repo, base)

    assert warn is False, detail
    assert ok is False, detail
    assert "违规 1 笔" in detail, detail


# ── ①②③ 回归（此前无专用用例；本票只证明行为不变） ──


def test_shape1_receipt_for_the_commit_itself(tmp_path):
    """形状①：t = c 自身。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(c), "docs(gate): receipt")

    ok, detail, _ = _check(repo, base)

    assert ok is True, detail


def test_shape2_readings_transfer_from_first_parent(tmp_path):
    """形状②：t = 第一父，且 p..c 的差异是 docs-only。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    p = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(p), "docs(gate): receipt")
    repo.commit({"docs/after.md": "after\n"}, "docs after receipt")

    ok, detail, _ = _check(repo, base)

    assert ok is True, detail


def test_shape3_same_tree_descendant_receipt(tmp_path):
    """形状③：t 为区间内后代且与 c **同一棵树**。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    t = repo.empty_commit("same tree, later commit")
    repo.commit(repo.receipt(t), "docs(gate): receipt")

    ok, detail, _ = _check(repo, base)

    assert c != t
    assert ok is True, detail
