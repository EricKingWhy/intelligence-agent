"""#1553：`receipt_present_check` 形状④（分支冻结 tip 覆盖）的判别力与 ①②③ 回归。

形状④（用户 2026-10-23 亲裁 A 案）：提交 c 被覆盖，若 HEAD 树中存在有效收据，其被测树 t 满足
  ① t 在本检查区间内（`merge-base(origin/main, HEAD)..HEAD`）；
  ② c 是 t 的祖先；
  ③ t 之后至 HEAD 的**每一笔**提交都是纯 docs 或纯收据落盘（复用 `is_pure_docs_commit` /
     `is_pure_receipt_landing`；任一笔不满足 ⇒ ④ 对该 t 不成立，fail-closed）。

依据：被测的是**最终合入的那棵树**。tip 树实跑过全量、其后无代码改动 ⇒ 中间提交的代码已被
包含在被测树内（与 PR #1336「7 笔代码提交、1 枚冻结 tip 读数」及 SDD §8.1 读数传递同源）。

本文件用真 git 仓库（tmp_path）构造检查区间，直接调 `receipt_present_check()`；不跑门禁车道。
bootstrap 一律显式传 base、且把 base 的提交时间固定在 2026-10-01（远早于今天，> 7 天）
⇒ `warn_only=False`（转硬口径）。
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
GATE0_PATH = REPO / "scripts" / "gate0.py"

#: 固定在过去（> 7 天）：保证 `bootstrap + 7 天 < now` ⇒ 转硬口径（宽限窗已过）。
_OLD_DATE = "2026-10-01T00:00:00+00:00"


def _load_gate0():
    spec = importlib.util.spec_from_file_location("_gate0_i1553", GATE0_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate0 = _load_gate0()


def _env() -> dict:
    """剥掉继承的 `GIT_*` 并**屏蔽全局/系统 config**。

    前者：#668（hook 注入的 GIT_DIR 会把 tmp 仓库操作劫持到外层仓库）。
    后者：`core.hooksPath` / `autocrlf` / `commit.gpgsign` 等本机设置会让测试结果依赖跑测试
    的那台机器（同仓先例：`tests/tooling/test_review_coverage_zero_content.py::_git`）。
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1"})
    return env


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

    def is_ancestor(self, a: str, b: str) -> bool:
        env = _env()
        proc = subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=self.root, env=env, capture_output=True, text=True,
                              check=False)
        assert proc.returncode in (0, 1), f"git merge-base rc={proc.returncode}: {proc.stderr}"
        return proc.returncode == 0


def _base_with_origin(repo: Repo) -> str:
    base = repo.commit({"README.md": "base\n"}, "base")
    repo.git("update-ref", "refs/remotes/origin/main", base)
    return base


def _check(repo: Repo, base: str) -> tuple[bool, str, bool]:
    """在**干净 env** 里调被测逻辑。

    `_env()` 只喂 fixture 自己起的 git 子进程；被测逻辑走 `gate0.git()`（`scripts/gate0.py:162`），
    它不传 env ⇒ 继承 `os.environ`，而 `GIT_DIR` 的优先级**高于** `-C <tmp>` ⇒ 检查会跑去查外层
    仓库（r3 实测：`GIT_DIR=$PWD/.git` 下 5 failed / 3 passed，`test_shape1/2` 是**假绿**——detail
    显示 `被查 0 笔`，什么都没检）。这里把整段调用期间的 `os.environ` 换成 `_env()` 的结果，
    退出即逐字恢复。不改生产 `git()`：它继承环境在真实门禁里是对的，缺的是测试侧的隔离。
    """
    with _clean_env():
        return gate0.receipt_present_check(repo_root=str(repo.root), bootstrap_sha=base)


@contextlib.contextmanager
def _clean_env():
    """被测逻辑调 `gate0.*` 时用的干净 env（见 `_check` 的 docstring）。"""
    with mock.patch.dict(os.environ, _env(), clear=True):
        yield


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
    """条件③ fail-closed：t 之后删除 docs 文件（状态 D）也不算「纯 docs」⇒ ④ 对该 t 不成立。

    承重点：`code 1` 只可能靠 ④ 被覆盖（收据在它之后那笔上），tail 一旦被判不纯就必须进违规。
    把 `tail_is_docs_or_receipts` 改成恒 True ⇒ 本断言转红。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"docs/old.md": "old\n"}, "docs 1")
    early = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    tip = repo.commit({"src/b.py": "b = 2\n"}, "code 2 (receipt carrier)")
    repo.commit(repo.receipt(tip), "docs(gate): receipt for tip")
    doomed = repo.remove("docs/old.md", "docs: delete a file after receipt")

    ok, detail, warn = _check(repo, base)

    assert warn is False, detail
    assert ok is False, detail
    # tip 有自身收据（形状①）；early 只能靠 ④ 被覆盖 ⇒ tail 一旦判纯，early 就消失。
    # 违规恰为 2 笔（early + doomed）：M1（tail 恒真）或 M6（接受 D 状态）下都只剩 1 笔 ⇒ 转红。
    assert f"missing receipt for {early[:12]}" in detail, detail
    assert f"missing receipt for {doomed[:12]}" in detail, detail
    assert f"missing receipt for {tip[:12]}" not in detail, detail
    assert "违规 2 笔" in detail, detail


def test_shape4_tail_code_is_not_washed_out_by_a_later_delete(tmp_path):
    """逐笔判的必要性：t 之后「先加代码、后删代码」在 `t..HEAD` 整体 diff 里是空改动面，
    只有逐笔判才看得见 ⇒ ④ 不成立（fail-closed）。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    tip = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.commit(repo.receipt(tip), "docs(gate): receipt for tip")
    repo.commit({"src/tmp.py": "x = 0\n"}, "code 3 (scratch)")
    repo.remove("src/tmp.py", "code 3 reverted")

    ok, detail, warn = _check(repo, base)

    assert warn is False, detail
    assert ok is False, detail
    assert "违规 3 笔" in detail, detail


def test_shape4_condition1_t_must_be_inside_the_range(tmp_path):
    """④ 条件①（`t in range_set`）的判别力：区间外的收据**不得**覆盖区间内的 c。

    构造（r3 P2 指条件①零判别力，此例即其反例）：t 存在一条**未合入**的 side 分支上，
    c 是 t 的祖先，且 `t..HEAD` 逐笔纯 docs ⇒ ②③ 都成立，只有 ① 不成立。
    删掉 `t not in range_set` ⇒ ④ 认下这枚区间外收据、c 被放行 ⇒ 本用例转红。

    注意 m 笔（`-s ours` 合并）的树与第一父相同 ⇒ ③ 也不会认它（③ 要求 c 与 t 同树，
    而 c 的树 ≠ base 的树），c 只可能靠 ④ 被覆盖。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c = repo.commit({"src/a.py": "a = 1\n"}, "code c")
    repo.git("checkout", "-q", "-b", "side")
    out_of_range = repo.commit({"src/side.py": "s = 1\n"}, "side code t")
    repo.git("checkout", "-q", "main")
    repo.commit({"docs/notes.md": "notes\n"}, "docs tail")
    repo.commit(repo.receipt(out_of_range), "docs(gate): receipt for the out-of-range tip")

    ok, detail, warn = _check(repo, base)

    assert warn is False, detail
    # 前提自证：t 确实在区间外、且 c 确是 t 的祖先（否则本用例没有命中条件①）。
    assert out_of_range not in repo.git("rev-list", f"{base}..HEAD").split(), \
        "构造失败：t 落进了检查区间"
    assert repo.is_ancestor(c, out_of_range), "构造失败：c 不是 t 的祖先"
    assert ok is False, detail
    assert f"missing receipt for {c[:12]}" in detail, detail
    assert "违规 1 笔" in detail, detail


# ── ④ 条件②（`c` 是 `t` 的祖先）: 不补用例，理由如下（r3 P2 的第二半） ──


def test_shape4_condition2_is_implied_by_conditions_1_and_3(tmp_path):
    """条件② 无判别力用例，因为它**由 ①+③ 蕴含**，删掉不改变任何判定结果。

    论证（本用例用一组真实提交把前提打实，不是纯口述）：设 c ∈ scope（⇒ c 在区间内且非纯
    docs），t 满足 ①（t 在区间内）但**不**满足 ②（c 不是 t 的祖先）。则 c 可从 HEAD 到达、
    从 t 不可到达 ⇒ **c ∈ `t..HEAD`**；而 c 非纯 docs ⇒ `tail_is_docs_or_receipts(t)` 必为
    False ⇒ ④ 对该 t 不成立。即「②不成立」的那些 t 全部被 ③ 挡掉，② 只是短路优化。
    这里断言的是这条链子的三个环节，而不是「跑一遍 8 passed」。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c = repo.commit({"src/a.py": "a = 1\n"}, "code c")
    repo.git("checkout", "-q", "-b", "side", base)
    t = repo.commit({"src/s.py": "s = 1\n"}, "side code t")
    repo.git("checkout", "-q", "main")
    repo.commit({"docs/notes.md": "notes\n"}, "docs tail")
    repo.git("merge", "-q", "--no-ff", "side", "-m", "merge side")

    # ① t 在区间内。
    assert t in repo.git("rev-list", f"{base}..HEAD").split()
    # ② 前提：c 不是 t 的祖先（这正是要论证的情形）。
    assert not repo.is_ancestor(c, t)
    # 关键环节：c 落在 t..HEAD ⇒ ③ 必然判 False（c 非纯 docs）。
    assert c in repo.git("rev-list", f"{t}..HEAD").split()
    with _clean_env():
        assert gate0.is_pure_docs_commit(gate0.commit_file_changes(str(repo.root), c)) is False
        assert gate0.tail_is_docs_or_receipts(str(repo.root), t) is False


def test_tail_memo_does_not_serve_a_stale_answer_after_head_moves(tmp_path):
    """记忆化的正确性：HEAD 前进后再问同一个 `t`，必须重算而不是给旧答案。

    `tail_is_docs_or_receipts` 按 `(repo_root, t, HEAD)` 记忆化 —— 少了 HEAD 这一项就会
    fail-open：先问一次拿到 True（当时 `t..HEAD` 全纯 docs），之后**在 t 之后落一笔代码提交**，
    再问同一个 t 仍命中旧缓存 True ⇒ 新提交被静默放行。本用例先直接调一次把缓存填上，
    再进代码提交，然后要求同一调用返回 False。
    """
    repo = Repo(tmp_path / "r")
    _base_with_origin(repo)
    tip = repo.commit({"src/b.py": "b = 2\n"}, "code at tip")

    # 第一次：t = tip 自己 ⇒ tail 为空 ⇒ True（缓存被填）。
    with _clean_env():
        assert gate0.tail_is_docs_or_receipts(str(repo.root), tip) is True
        # HEAD 前进一笔代码提交。
        late = repo.commit({"src/c.py": "c = 3\n"}, "code after tip")
        assert late != tip
        # 同一 (repo_root, t) 再问 ⇒ 必须看见新提交（False），不能吃缓存。
        assert gate0.tail_is_docs_or_receipts(str(repo.root), tip) is False


def test_tail_rejects_an_empty_commit(tmp_path):
    """空提交（`--allow-empty`）不算「纯 docs / 纯收据落盘」⇒ ④ 对该 t 不成立（fail-closed）。

    票面字面是「每一笔提交都是纯 docs 或纯收据落盘」；空提交的改动面是空集，既不满足
    `is_pure_docs_commit`（要求 `bool(changes)`）也不满足 `is_pure_receipt_landing`。
    把那个 `bool(changes) and` 放宽掉 ⇒ 本用例转红。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    tip = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.commit(repo.receipt(tip), "docs(gate): receipt for tip")
    empty = repo.empty_commit("empty commit after the receipt")

    ok, detail, warn = _check(repo, base)

    assert warn is False, detail
    with _clean_env():
        assert gate0.is_pure_docs_commit(gate0.commit_file_changes(str(repo.root), empty)) is False
    assert ok is False, detail
    # tip 有自身收据；code 1 与那笔空提交都没有 ⇒ 违规 2 笔。
    assert "违规 2 笔" in detail, detail
    assert f"missing receipt for {empty[:12]}" in detail, detail


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
    """形状②：t = 第一父 p，且 p..c 的差异 docs-only。

    构造要点：`is_pure_docs_commit` 要求 `bool(changes)`，所以**带 docs 改动的提交根本进不了
    scope**——② 在 scope 里只对「第一父 diff 为空」的提交有事可做（空提交 / `-s ours` 合并）。
    要让本用例**只**由 ② 放行：让第一父 base 落在检查区间之外（`range_set` 不含 base）⇒ ③/④
    都拿不到 t = base；给 base 落一枚有效收据（收据提交是纯收据落盘、不进 scope）。
    把 `and transfer_diff_docs_only(...)` 改成 `and False` ⇒ 本断言转红。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "side")
    repo.commit({"docs/side.md": "side\n"}, "docs on side")
    repo.git("checkout", "-q", "main")
    # -s ours：树与第一父相同 ⇒ `git diff base c` 为空 ⇒ 传递口径成立（docs-only 的空集）。
    repo.git("merge", "-q", "--no-ff", "-s", "ours", "side", "-m", "merge side (ours)")
    merged = repo.git("rev-parse", "HEAD")
    repo.commit(repo.receipt(base), "docs(gate): receipt for base")

    ok, detail, _ = _check(repo, base)

    assert merged != base
    assert f"missing receipt for {merged[:12]}" not in detail, detail
    assert ok is True, detail


def test_shape3_same_tree_descendant_receipt(tmp_path):
    """形状③：t 为区间内后代且与 c **同一棵树**。

    尾部再放一笔删除 docs 的提交（状态 D，不纯）⇒ ④ 对该 t 不成立，只有 ③ 能放行 c。
    把 ③ 的 `c_tree == t_tree` 判据删掉 ⇒ 本断言转红。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"docs/old.md": "old\n"}, "docs 1")
    c = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    t = repo.empty_commit("same tree, later commit")
    repo.commit(repo.receipt(t), "docs(gate): receipt")
    doomed = repo.remove("docs/old.md", "docs: delete after receipt")

    ok, detail, _ = _check(repo, base)

    assert c != t
    assert f"missing receipt for {c[:12]}" not in detail, detail
    assert f"missing receipt for {doomed[:12]}" in detail, detail
    assert ok is False, detail  # 那笔 D 提交自身没有收据


# ── #1602：self-cover 自举（本地落盘型全量跑不因缺收据自红；CI/局部跑照旧硬口径） ──


def test_receipt_self_cover_truth_table():
    """`receipt_self_cover`：record 且非局部且非 CI 才允许自举（#1602）。"""
    assert gate0.receipt_self_cover(record=True, partial=False, ci_env=None) is True
    assert gate0.receipt_self_cover(record=True, partial=False, ci_env="true") is False
    assert gate0.receipt_self_cover(record=True, partial=True, ci_env=None) is False
    assert gate0.receipt_self_cover(record=False, partial=False, ci_env=None) is False


def _fresh_code_branch(repo: Repo, base: str) -> None:
    """无收据的代码分支：receipt-present 必 not ok 且非 warn（转硬口径）。"""
    repo.commit({"docs/n.md": "n\n"}, "docs 1")
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")


def test_self_cover_keeps_guards_green_on_local_full_run(tmp_path, capsys):
    """本地落盘型全量跑（self_cover=True）：缺收据不改写 guards 车道 + 响亮提示。

    判别力：把 `elif self_cover` 分支删掉（回到无条件改写）⇒ 本断言转红。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    _fresh_code_branch(repo, base)
    with mock.patch.object(gate0, "REPO_ROOT", str(repo.root)), \
            mock.patch.object(gate0, "receipt_bootstrap_sha", lambda repo_root=None: base), _clean_env():
        results = [(gate0.Lane("guards", "desc", [], str(repo.root)), 0, 0.0, "")]
        gate0._apply_receipt_check(results, self_cover=True)
    out = capsys.readouterr().out
    assert results[0][1] == 0, out
    assert "self-cover receipt-present (#1602)" in out
    assert "必须随代码一起提交" in out


def test_self_cover_off_rewrites_guards_in_ci(tmp_path, capsys):
    """CI/局部跑（self_cover=False）：缺收据照旧把 guards 改写为失败（既有行为钉住）。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    _fresh_code_branch(repo, base)
    with mock.patch.object(gate0, "REPO_ROOT", str(repo.root)), \
            mock.patch.object(gate0, "receipt_bootstrap_sha", lambda repo_root=None: base), _clean_env():
        results = [(gate0.Lane("guards", "desc", [], str(repo.root)), 0, 0.0, "")]
        gate0._apply_receipt_check(results, self_cover=False)
    assert results[0][1] == 1
    assert "missing receipt for" in results[0][3]
