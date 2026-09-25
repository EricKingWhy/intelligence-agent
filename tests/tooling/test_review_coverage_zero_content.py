"""零新增内容（树 == 某父的树）自动归属 —— 覆盖闸门的第四条归属判据（2026-09-26）。

## 它守什么

`strict` + 禁 `rebase` ⇒ 只能先把 `main` 并进分支 ⇒ GitHub 合出来的 merge 与**父提交二（分支顶端）
逐文件相同** ⇒ `git show --cc --name-only` 空表 ⇒ `is_docs_only` 与白名单那支**都** fail-closed
⇒ **每个 PR 合入都在 `main` 顶端重造一个 ❌**。`c8e04cb9`（PR #321）与 `da7dfef1`（PR #322，
前者处置落地**当天**）各实测一次 ⇒ 结构性、非偶发。

判据 `zero_content_parent()` 把「**树对象相等**」变成机械可验的归属理由。它与「空文件表」**不是**
同一件事：空表是"核对不了"（枚举失败也是空表）⇒ 必须 fail-closed；树相等是**正向证明** ⇒ 可以放行。

## 为什么除了纯函数测试还要跑合成仓库

纯函数测试看得见判据、**看不见接线**：忘了在判定循环里调用它、或把它放在白名单之后（于是永远
到不了），纯函数测试全绿。所以本文件有两条端到端：

- `test_merge_with_parent_tree_is_attributed_end_to_end` —— 正控。合成仓库里造一个"树 == 父2 的树"
  的 merge ⇒ 闸门必须 `exit 0` 并打印 `✅ 零新增内容`。**修前这条是 `exit 1`**（`❌ 未审查且未声明`），
  所以它同时是"这个 bug 真的被修了"的证明。
- `test_interleaved_merge_is_still_fail_closed_end_to_end` —— **反控，最关键的一条**。文件表为空但
  树不等于任何父的 merge（两侧各改不同文件、结果取并集）**仍必须 `exit 1`**。没有它，一个
  「凡是空文件表就放行」的假实现也能过正控 —— 而那正是把 fail-closed 整条拔掉的形状。

端到端用**合成仓库**（把闸门脚本复制进去，于是它的 `REPO_ROOT` = 临时仓库），不碰本仓历史：
本仓那条真实 merge 迟早会被补上台账行、从而不再进入判定循环，硬依赖它的测试会**静默失效**。

## 变异实测（2026-09-26；`WBI_GATE_UNDER_TEST` 指向打了变异的副本，只跑本文件）

**被抓到的**（每条都被**恰好预期**的用例抓住）：

- **M1 拆掉接线**（判定循环里 `if False and sha in zero_parent:`）⇒ **只红正控那条端到端**，
  其余 8 条（7 条判据层 + 反控端到端）**全绿** ⇒ 「判据层测试看不见接线」不是推断，是实测。
- **M2 判据偷懒**（文件表为空就 `return parents[0]`，即"凡是空表就放行"）⇒ 红 4 条，
  含 `…_still_fail_closed_end_to_end` ⇒ 反控确实拦得住那条假实现。
- **M3 去掉 `target` 真值守卫**（`None == None` 判等）⇒ **只红** `…_tree_cannot_be_resolved`。
- **M15 去掉 `expect.match(line)` 形状校验**（`missing` 那行被当成 sha 取用）⇒ **只红**
  `test_resolve_trees_rejects_a_line_that_is_not_a_tree`；本条是补写的，此前**无任何用例**能抓它
  —— 那正是"注释声称 fail-closed、测试却没钉住"的唯一一处。

**未被抓到、但也不该被证伪（逐条定级，别读成漏网）**：

- **M4 删 `if not parents: return None`（根提交守卫）** ⇒ 行为**完全相同**（无父 ⇒ 循环零次 ⇒
  照样 `return None`）。这条守卫是**可读性**，不是分支；删了没人能证伪，因为没有可观察差异。
- **M5 命中多个父时返回最后一个而非第一个** ⇒ 只有"两个父的树恰好相同"（真存在，如把已并入的
  分支再 `--no-ff` 合一次）时标签不同，**两个都是正当归属** ⇒ 无判定差异，属良性。
- **M6 改成「树 == 任一祖先」**（而非仅父）⇒ 判据**放宽**、本文件不钉它，但**未被证伪**：
  它会把"整棵树恰好回到某个祖先状态"的提交也放行（如删除某文件后树回到祖父的样子）—— 那种
  提交的内容确实等于一份已存在的树，可"相对父的**增量**"并未被审查 ⇒ 属**真放松**。当前实现
  取**父**（更严），保留即安全；若将来有人想放宽，必须**另案**并补上这条反控。
- **M7 去掉 `cand` 预筛**（对全部待判定提交解树）⇒ 结论**完全相同**，只是多解几个 rev ⇒ 良性。
- **M9 把接线挪到 `is_ledger_only` 之后** ⇒ 只有"既是零新增内容、又恰好只改台账"的提交（几乎
  不存在）会**换一个 ✅ 标签**（`✅ 台账自身更新` ↔ `✅ 零新增内容`），**两边都是绿** ⇒ 良性。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
#: 变异测试用：允许把闸门指到一份**打了变异的副本**上（与 `test_review_coverage_lint.py` 同一钩子，
#: 同一次变异运行可同时证伪两边）。默认就是真文件 —— 正常跑没有任何影响。
#: 端到端那两条也走这条路径（把 `GATE_SRC` 复制进合成仓库），于是变异同样能证伪"接线"。
GATE_SRC = Path(os.environ.get("WBI_GATE_UNDER_TEST") or (REPO / "scripts" / "check_review_coverage.py"))


def _load_gate_module():
    spec = importlib.util.spec_from_file_location("_coverage_gate_zero_content", GATE_SRC)
    assert spec and spec.loader, f"加载不了 {GATE_SRC}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    assert GATE_SRC.is_file(), f"闸门脚本不存在：{GATE_SRC}"
    return _load_gate_module()


# =========================================================================== #
# 判据本身（纯函数）
# =========================================================================== #

def test_fires_when_the_tree_equals_a_parent_tree(gate):
    """**正控**：整棵树逐字等于父提交二的树 ⇒ 判据必须命中并返回那个父。"""
    trees = {"m": "T_branch", "p1": "T_main", "p2": "T_branch"}
    assert gate.zero_content_parent("m", ["p1", "p2"], trees) == "p2"


def test_fires_on_a_non_merge_commit_with_identical_tree(gate):
    """边界：非 merge 但树与父相同（`--allow-empty` 造出的空提交）⇒ 同样判零新增内容。

    判据的正当性（"整棵树逐字等于某个父 ⇒ 不可能携带未审查内容"）与父的**个数**无关；
    只在 merge 上成立反而是一条没有理由的特例。
    """
    assert gate.zero_content_parent("c", ["p"], {"c": "T", "p": "T"}) == "p"


def test_does_not_fire_when_no_parent_tree_matches(gate):
    """**反控（不放松的核心）**：树与**每个**父都不同 ⇒ 必须返回 None。

    这就是"两侧各改不同文件、结果取并集"的形状：`--cc` 是空的，但树既不等于父 1 也不等于父 2
    ⇒ 内容是两侧的并集，"零新增"这个断言对它**不成立**，必须回落 fail-closed。
    """
    trees = {"m": "T_union", "p1": "T_main", "p2": "T_branch"}
    assert gate.zero_content_parent("m", ["p1", "p2"], trees) is None


def test_fails_closed_when_a_tree_cannot_be_resolved(gate):
    """**反控**：树解不出来（`cat-file` 报 missing ⇒ None）⇒ 判据必须返回 None。

    解不出来**不是**"树相等"—— 它是"核对不了"，方向和空文件表一样。若实现写成
    `trees.get(p) == trees.get(sha)` 而不先要求 `trees[sha]` 为真，两个 None 会**判等** ⇒
    一个解不出树的提交立刻被放行（这正是"用 Python 的 `None == None` 悄悄拔掉 fail-closed"）。
    """
    assert gate.zero_content_parent("m", ["p"], {}) is None
    assert gate.zero_content_parent("m", ["p"], {"m": None, "p": None}) is None
    assert gate.zero_content_parent("m", ["p"], {"m": "T", "p": None}) is None


def test_fails_closed_on_a_root_commit(gate):
    """**反控**：根提交（无父）⇒ 必须返回 None —— 不是"零新增"，是"无从判定"。"""
    assert gate.zero_content_parent("root", [], {"root": "T"}) is None


def test_prints_full_shas_for_recomputation(gate):
    """归属行必须**打印树与父的完整 sha**（这条判据的全部价值是可复算）。

    审计者拿父的全 sha 跑一次 `git diff --name-only <父> <sha>` 就该得到空输出。只印短 sha 会让
    "别处的同名缩写"有机会冒充 —— 而短名冒充恰恰是这行唯一可能的失效方式。
    """
    tree, parent = "a" * 40, "b" * 40
    text = gate.format_zero_content("abc1234", "Merge pull request #322", parent, tree)
    assert "abc1234" in text, "必须打印短 sha"
    assert tree in text and parent in text, f"必须打印树与父的完整 sha（实得 {text!r}）"


def test_resolve_trees_rejects_a_line_that_is_not_a_tree(gate):
    """**反控**：`resolve_many` 必须按**行的形状**判成败，不能只看退出码 / 无条件取首段。

    为什么这是 `resolve_trees` 的**命门**：`git cat-file --batch-check` 对**不存在的对象也退 0**，
    只是把该行变成 `<input> missing`。若实现写成 `line.split(" ")[0]` 无条件取值，两次"解不出"会
    得到**两个 `missing` 字样**、彼此 `==` ⇒ 一个解不出树的父会被判成"与提交同树" ⇒ **fail-closed
    被悄悄拔掉**。所以这里既要钉"不存在的对象 ⇒ None"，也要钉两条正则**互不兼收**
    （`FULL_SHA_RE` 故意不收 `tree` 行：它还兼任 commit 行的形状判据，放宽它等于把哨兵行也认了）。
    """
    real = gate.git("rev-parse", "HEAD").stdout.strip()
    assert len(real) == 40, f"环境异常：拿不到 HEAD 的完整 sha（{real!r}）"
    bogus = "0" * 40

    trees = gate.resolve_trees([real, bogus])
    assert trees[real] is not None and len(trees[real]) == 40, f"真实 rev 必须解出树：{trees[real]!r}"
    assert trees[bogus] is None, f"不存在的对象必须映射为 None，实得 {trees[bogus]!r}"

    assert gate.TREE_SHA_RE.match(f"{trees[real]} tree 1")
    assert not gate.FULL_SHA_RE.match(f"{trees[real]} tree 1"), \
        "`FULL_SHA_RE` 不得兼收 tree 行（它还兼任 commit 行形状判据）"
    assert not gate.TREE_SHA_RE.match(f"{real} commit 1")
    assert gate.FULL_SHA_RE.match(f"{real} commit 1")


# =========================================================================== #
# 端到端（合成仓库 —— 跑**真闸门**，于是接线错误也逃不掉）
# =========================================================================== #

def _git(repo: Path, *args: str) -> str:
    """在合成仓库里跑 git。**屏蔽全局/系统 config**：否则 `core.hooksPath` / `autocrlf` /
    `commit.gpgsign` 这些本机设置会让测试结果依赖跑测试的那台机器。"""
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_DATE": "2026-09-26T00:00:00+08:00",
        "GIT_COMMITTER_DATE": "2026-09-26T00:00:00+08:00",
    })
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False, env=env)
    assert proc.returncode == 0, f"git {' '.join(args)} 失败（rc={proc.returncode}）：{proc.stderr}"
    return proc.stdout


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    full = repo / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8", newline="\n")
    _git(repo, "add", "--", path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _run_gate(repo: Path) -> subprocess.CompletedProcess:
    """跑复制进合成仓库的那份闸门（其 `REPO_ROOT` = 合成仓库）。"""
    script = repo / "scripts" / "check_review_coverage.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(GATE_SRC, script)
    return subprocess.run([sys.executable, str(script)], cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)


class _Synth:
    """合成仓库：`c0 -> c1 -> c2`（都在 main），再 `c3` 落在从 `c2` 开的分支上。

    台账只覆盖 `c0..c2`（`c2` 是 base 之后的那一笔），于是待判定集恰好是 `{c3, M}`
    —— `c3` 走 docs-only、`M` 走本文件要测的零新增内容。
    """

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "synth"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "gate-test")
        _git(self.repo, "config", "user.email", "gate-test@example.invalid")
        self.c0 = _commit(self.repo, "README.keep", "keep\n", "c0")
        self.c1 = _commit(self.repo, "docs/one.md", "one\n", "c1")
        self.c2 = _commit(self.repo, "docs/two.md", "two\n", "c2")
        # 台账：`<base>..<tip>` 的 base 是**排他**的，故要覆盖 c1/c2 必须写 `c0..c2`。
        (self.repo / "docs" / "review_ledger.tsv").write_text(
            f"2026-09-26\t合成台账行（覆盖 c1/c2）\t{self.c0}..{self.c2}\n",
            encoding="utf-8", newline="\n")

    def branch_commit(self, name: str, content: str, message: str) -> str:
        """从 `c2` 开分支并落一笔 —— 与 `main` **无分叉**（`strict` 满足时的形状）。

        ⚠ 调用后 HEAD 停在**该分支**上（`git checkout -b` 的语义）。
        """
        _git(self.repo, "checkout", "-q", "-b", name, self.c2)
        return _commit(self.repo, f"docs/{name}.md", content, message)

    def main_commit(self, name: str, content: str, message: str) -> str:
        """在 `main` 上另落一笔 —— 与已开出的分支**分叉**（造"取并集"的 merge 用）。"""
        _git(self.repo, "checkout", "-q", "main")
        return _commit(self.repo, f"docs/{name}.md", content, message)

    def merge_into_main(self, branch: str) -> str:
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)
        return _git(self.repo, "rev-parse", "HEAD").strip()


def test_merge_with_parent_tree_is_attributed_end_to_end(tmp_path):
    """**正控（跑真闸门）**：树 == 父提交二的树的 merge ⇒ `exit 0` 且打印 `✅ 零新增内容`。

    这条是"判据真的接进了判定循环"的唯一证明。它也钉住形状前提：先断言 `--cc` 文件表**为空**
    （否则测的就不是那个 fail-closed 形状），再断言闸门放行。
    """
    s = _Synth(tmp_path)
    branch_tip = s.branch_commit("feat", "three\n", "c3")
    merge = s.merge_into_main("feat")

    # 形状自证：`--cc` 空表（修前正是它让闸门 fail-closed 的），且 merge 的树 == 父2 的树。
    cc = subprocess.run(["git", "show", "--cc", "--no-renames", "--pretty=format:", "--name-only", merge],
                        cwd=s.repo, capture_output=True, text=True, check=False)
    assert cc.stdout.strip() == "", f"本条要测的就是空 `--cc` 形状，实得 {cc.stdout!r}"
    assert (subprocess.run(["git", "rev-parse", f"{merge}^{{tree}}"], cwd=s.repo, capture_output=True,
                           text=True, check=True).stdout
            == subprocess.run(["git", "rev-parse", f"{branch_tip}^{{tree}}"], cwd=s.repo,
                              capture_output=True, text=True, check=True).stdout), \
        "前提不成立：merge 的树必须等于父2（分支顶端）的树"

    proc = _run_gate(s.repo)
    assert proc.returncode == 0, (
        f"零新增内容的 merge 不得让闸门红：rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    assert "✅ 零新增内容" in proc.stdout, f"必须打印零新增内容归属行：\n{proc.stdout}"
    assert merge[:7] in proc.stdout, "归属行必须指向该 merge"
    assert "❌" not in proc.stdout, f"不得有 ❌：\n{proc.stdout}"


def test_interleaved_merge_is_still_fail_closed_end_to_end(tmp_path):
    """**反控（最关键）**：`--cc` 空表、但树**不等于任何父**的 merge ⇒ 仍必须 `exit 1`。

    形状：两侧各改**不同**文件 ⇒ 结果的每个文件都等于某个父的 blob ⇒ `--cc` 为空；而结果的整棵树
    是新并集 ⇒ 既不等于父 1 也不等于父 2。它是"空文件表 ≠ 零新增内容"的活样本 ——
    「凡是空文件表就放行」的假实现会在这条上红。
    """
    s = _Synth(tmp_path)
    s.branch_commit("side", "side\n", "c3-side")        # 分支：docs/side.md（HEAD 停在 side）
    s.main_commit("main-side", "main-side\n", "c3-main")   # main 另改一个文件 ⇒ 与分支分叉
    merge = s.merge_into_main("side")

    cc = subprocess.run(["git", "show", "--cc", "--no-renames", "--pretty=format:", "--name-only", merge],
                        cwd=s.repo, capture_output=True, text=True, check=False)
    assert cc.stdout.strip() == "", f"前提不成立：本条要求空 `--cc` 形状，实得 {cc.stdout!r}"

    trees = {p: subprocess.run(["git", "rev-parse", f"{p}^{{tree}}"], cwd=s.repo, capture_output=True,
                               text=True, check=True).stdout.strip()
             for p in _git(s.repo, "rev-list", "--parents", "-n", "1", merge).split()[1:]}
    merge_tree = subprocess.run(["git", "rev-parse", f"{merge}^{{tree}}"], cwd=s.repo, capture_output=True,
                                text=True, check=True).stdout.strip()
    assert merge_tree not in trees.values(), \
        "前提不成立：取并集的 merge 其树不得等于任何父的树（否则测的还是上一条）"

    proc = _run_gate(s.repo)
    assert proc.returncode == 1, (
        f"空文件表但树不等于任何父 ⇒ 必须 fail-closed，实得 rc={proc.returncode}\n{proc.stdout}")
    assert merge[:7] in proc.stdout and "❌" in proc.stdout, f"必须对该 merge 报 ❌：\n{proc.stdout}"
    assert "✅ 零新增内容" not in proc.stdout, f"不得把取并集的 merge 判成零新增内容：\n{proc.stdout}"
