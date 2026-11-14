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
import sys
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
        """被测树 sha 的合法收据：文件名=sha，tree 与 `rev-parse` 核得上，PASS 且 passed==total。

        #1555 合并后收据验收要求 `lanes` 清单与被测树 `scripts/gate0.py` 的
        `GATE0_LANE_IDS` 一致（车道清单固定）；夹具源不含绑定 marker ⇒ 走 legacy
        兼容路径（无 review_ledger 也接受）。
        """
        tree = self.git("rev-parse", f"{tested_sha}^{{tree}}")
        lanes = [{"name": name, "status": "PASS", "rc": 0}
                 for name in gate0.GATE0_LANE_IDS]
        body = {"schema": 1, "gate": "gate0", "sha": tested_sha, "tree": tree,
                "lanes": lanes, "wall_seconds": 1.0, "tool_versions": {"pytest": "1"},
                "result": "PASS", "passed": len(lanes), "total": len(lanes)}
        return {f"docs/gate/{tested_sha}.json": json.dumps(body)}

    def is_ancestor(self, a: str, b: str) -> bool:
        env = _env()
        proc = subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=self.root, env=env, capture_output=True, text=True,
                              check=False)
        assert proc.returncode in (0, 1), f"git merge-base rc={proc.returncode}: {proc.stderr}"
        return proc.returncode == 0


def _base_with_origin(repo: Repo) -> str:
    # #1555 车道清单固定：被测树需要一份可解析 `GATE0_LANE_IDS` 且不含绑定 marker 的
    # gate0.py，收据才走 legacy 兼容验收（真实仓库每棵树都有该文件，这里补夹具等价物）。
    gate0_source = f"GATE0_LANE_IDS = {gate0.GATE0_LANE_IDS!r}\n"
    base = repo.commit({"README.md": "base\n",
                        "scripts/gate0.py": gate0_source}, "base")
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
        result = gate0.receipt_present_check(repo_root=str(repo.root), bootstrap_sha=base)
    # #1555 把返回值从 (ok, detail, warn_only) 三元组升级为 ReceiptCheckResult（多一个
    # status 字段）；本文件的历史用例只消费前三项，这里按旧契约折回三元组，断言不变。
    return result.ok, result.detail, result.warn_only


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


# ── #1598：warn-only 违规行行内前缀（单行自证不计数） ──


def _commit_recent(repo: Repo, files: dict[str, str], msg: str) -> str:
    """日期不钉旧的提交：让 bootstrap 落进 7 天 warn-only 窗内（#1598 两臂之 warn 臂）。"""
    for rel, content in files.items():
        p = repo.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    proc = subprocess.run(["git", "add", "-A"], cwd=repo.root, env=_env(),
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    proc = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo.root, env=_env(),
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return repo.git("rev-parse", "HEAD")


def test_warnonly_lines_carry_inline_prefix(tmp_path):
    """#1598：warn-only 窗内违规行带 `warn-only ` 行内前缀；转硬口径后前缀消失。

    判别力：去掉前缀拼接（或硬口径误带前缀）⇒ 两臂任一转红。
    """
    repo = Repo(tmp_path / "warn")
    base = _commit_recent(repo, {"README.md": "base\n"}, "base")
    repo.git("update-ref", "refs/remotes/origin/main", base)
    _fresh_code_branch(repo, base)

    ok, detail, warn = _check(repo, base)
    assert warn is True, detail
    assert ok is False, detail
    assert "warn-only missing receipt for" in detail, detail

    hard = Repo(tmp_path / "hard")
    hbase = _base_with_origin(hard)  # 日期钉旧 ⇒ 转硬口径
    _fresh_code_branch(hard, hbase)
    _, hdetail, hwarn = _check(hard, hbase)
    assert hwarn is False, hdetail
    assert "warn-only missing receipt for" not in hdetail, hdetail
    assert "missing receipt for" in hdetail, hdetail


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
    """本地落盘型全量跑（will_record_receipt=True + 全绿 7 车道）：attested 路径不改写
    guards 车道 + 响亮提示收据须随代码提交。

    判别力：把 attested 分支删掉（回到无条件改写）⇒ 本断言转红。#1555 合并后 self_cover
    以 attested 形式落地：要求**全绿全量**（7 车道齐且 rc=0）才自举，且新收据绑定树与台账。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    _fresh_code_branch(repo, base)
    with mock.patch.object(gate0, "REPO_ROOT", str(repo.root)), \
            mock.patch.object(gate0, "receipt_bootstrap_sha", lambda repo_root=None: base), _clean_env():
        names = [lane.name for lane in gate0.build_lanes("")]
        results = [(gate0.Lane(n, "desc", [], str(repo.root)), 0, 0.0, "") for n in names]
        gate0._apply_receipt_check(results, will_record_receipt=True)
    out = capsys.readouterr().out
    assert results[0][1] == 0, out
    assert "self-cover receipt-present (#1602 attested" in out
    assert "必须随代码一起提交" in out


def test_self_cover_off_rewrites_guards_in_ci(tmp_path, capsys):
    """CI/局部跑（will_record_receipt=False）：缺收据照旧把 guards 改写为失败（既有行为钉住）。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    _fresh_code_branch(repo, base)
    with mock.patch.object(gate0, "REPO_ROOT", str(repo.root)), \
            mock.patch.object(gate0, "receipt_bootstrap_sha", lambda repo_root=None: base), _clean_env():
        results = [(gate0.Lane("guards", "desc", [], str(repo.root)), 0, 0.0, "")]
        gate0._apply_receipt_check(results, will_record_receipt=False)
    assert results[0][1] == 1
    assert "missing receipt for" in results[0][3]


# ── #1663：≥2 笔无收据代码提交的分支，首张 PASS 收据如何出生 ──
#
# 死锁（issue #1663）：attested 分支要求**每一笔**违规提交的树都等于 HEAD 树（或其后仅 docs）⇒
# 只有最后一笔代码提交满足；≥2 笔时首张 PASS 收据永远生不出来。修复的鉴别点：
#   · AC1 = HEAD 树里**没有任何覆盖本分支区间的有效收据** ⇒ 本次全绿全量的 HEAD 收据可按形状④
#     覆盖全部违规（它们都是 HEAD 的祖先、尾判为空），允许自举；
#   · #1663 AC5(ii) = 分支里**已有**有效收据、其后又进代码提交 ⇒ 仍判红（真正钉住该语义的是
#     `..._receipt_is_tampered` 的 before 态 + 改范围后的 binding 用例 (a)(b)(d) 三段；
#     旧 binding 断言已按 #1692 AC3① 改范围）。


def _full_lane_results(repo: Repo, base: str) -> list:
    """裸全量跑的 7 条车道结果（coverage 输出供 `write_reading` 取台账 base / 计数）。"""
    coverage_output = (
        f"覆盖区间: {base}..HEAD\n"
        "提交总数 2 / 已审查 2 / 待判定 0\n"
    )
    return [
        (gate0.Lane(lane.name, "desc", [], str(repo.root)), 0, 0.1,
         coverage_output if lane.name == "coverage" else "")
        for lane in gate0.build_lanes("")
    ]


def test_multi_code_commit_branch_bootstraps_its_first_receipt_with_zero_receipts(
    tmp_path,
):
    """AC1：≥2 笔无收据代码提交 + HEAD 树零收据 + 全绿全量跑 ⇒ attested，且收据真的能落成有效。

    修复前红：`receipt_present_check(..., _allow_current_full_run_receipt=True)` 返回
    `violations`，`_apply_receipt_check` 把 guards 改写为 rc=1（死锁本体）。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    head = repo.git("rev-parse", "HEAD")
    with mock.patch.object(gate0, "REPO_ROOT", str(repo.root)), \
            mock.patch.object(gate0, "receipt_bootstrap_sha", lambda repo_root=None: base), _clean_env():
        # 前提自证：构造的正是「≥2 笔 + 树内零收据 + 转硬口径」。
        assert gate0.head_tree_receipts(str(repo.root)) == {}
        check = gate0.receipt_present_check(
            str(repo.root), bootstrap_sha=base, _allow_current_full_run_receipt=True,
        )
        assert check.warn_only is False, check.detail
        assert check.ok is True, check.detail
        assert check.status == "attested", check.detail

        results = _full_lane_results(repo, base)
        gate0._apply_receipt_check(results, will_record_receipt=True)
        assert all(rc == 0 for _lane, rc, _s, _o in results), results

        path = gate0.write_reading(
            head=head, tree=repo.git("rev-parse", f"{head}^{{tree}}"), argv=[], wall=0.5,
            results=results, affected=None, changed=[], worktree={"untracked": []},
        )
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    assert doc["result"] == "PASS"
    repo.commit({f"docs/gate/{head}.json": Path(path).read_text(encoding="utf-8")},
                "docs(gate): first receipt for the branch")

    # 被测逻辑一律在干净 env 里调（#668：GIT_DIR 优先级高于 `-C <tmp>`，见 `_check` 的 docstring）。
    with _clean_env():
        receipts = gate0.head_tree_receipts(str(repo.root))
        assert receipts[head]["valid"], receipts[head]["reason"]
        after = gate0.receipt_present_check(str(repo.root), bootstrap_sha=base)
    assert after.ok is True, after.detail
    assert after.status == "clear", after.detail


def test_multi_code_commit_bootstrap_ignores_receipts_from_before_the_branch_base(tmp_path):
    """AC1 的真实形态：HEAD 树里有**区间外**的历史收据，不影响首张收据自举。

    真实仓库 HEAD 树里有一千多张历史收据（都早于 merge-base）。判据必须只看
    `merge-base..HEAD` 里的收据；把 `in_range_receipts` 写成 `receipts` ⇒ 本用例红、
    且真仓上永远自举不了。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit(repo.receipt(base), "docs(gate): historical receipt, before the branch base")
    origin = repo.git("rev-parse", "HEAD")
    repo.git("update-ref", "refs/remotes/origin/main", origin)
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")

    with mock.patch.object(gate0, "REPO_ROOT", str(repo.root)), \
            mock.patch.object(gate0, "receipt_bootstrap_sha", lambda repo_root=None: base), _clean_env():
        assert list(gate0.head_tree_receipts(str(repo.root))) == [base]  # 前提：树里确实有收据
        assert base not in repo.git("rev-list", f"{origin}..HEAD").split()  # 且它在区间外
        check = gate0.receipt_present_check(
            str(repo.root), bootstrap_sha=base, _allow_current_full_run_receipt=True,
        )
    assert check.warn_only is False, check.detail
    assert check.ok is True, check.detail
    assert check.status == "attested", check.detail


def test_multi_code_commit_bootstrap_requires_full_successful_recording_run(
    tmp_path,
):
    """AC2/AC3：零收据分支也不能靠不完整 / 不绿 / 不落盘的跑自举（fail-closed 不放松）。

    判别力：若把自举写成无条件（不看 `will_record_receipt` / 全绿），CI 臂会转绿 ⇒ 本用例红。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")

    with mock.patch.object(gate0, "REPO_ROOT", str(repo.root)), \
            mock.patch.object(gate0, "receipt_bootstrap_sha", lambda repo_root=None: base), _clean_env():
        # 本地跑完已把收据落到工作树、但**没随代码提交** ⇒ CI 侧（will_record_receipt=False）
        # 仍按已提交收据执法：`head_tree_receipts` 读 HEAD 树，看不见未跟踪文件。
        gate0.write_reading(
            head=repo.git("rev-parse", "HEAD"),
            tree=repo.git("rev-parse", "HEAD^{tree}"), argv=[], wall=0.5,
            results=_full_lane_results(repo, base), affected=None, changed=[],
            worktree={"untracked": []},
        )
        assert (repo.root / "docs" / "gate" / f"{repo.git('rev-parse', 'HEAD')}.json").is_file()
        assert gate0.head_tree_receipts(str(repo.root)) == {}

        # CI / 局部跑：不落盘 ⇒ 无法自举。
        ci_results = _full_lane_results(repo, base)
        gate0._apply_receipt_check(ci_results, will_record_receipt=False)
        guards = next(r for r in ci_results if r[0].name == "guards")
        assert guards[1] == 1
        assert "missing receipt for" in guards[3]

        # 全量但有一条车道不绿（pytest 本体红 / 其它车道红）⇒ 本次跑落不出 PASS 收据。
        failed_results = _full_lane_results(repo, base)
        failed_results = [
            (lane, 1 if lane.name == "ruff" else rc, secs, out)
            for lane, rc, secs, out in failed_results
        ]
        gate0._apply_receipt_check(failed_results, will_record_receipt=True)
        guards = next(r for r in failed_results if r[0].name == "guards")
        assert guards[1] == 1
        assert "missing receipt for" in guards[3]

        # 只跑局部车道（--only guards）⇒ 不是全量、不会落盘 ⇒ 不能自举。
        only_guards = [
            (gate0.Lane("guards", "desc", [], str(repo.root)), 0, 0.1, ""),
        ]
        gate0._apply_receipt_check(only_guards, will_record_receipt=True)
        assert only_guards[0][1] == 1
        assert "missing receipt for" in only_guards[0][3]


# ── #1663 修复轮：自举前置不得由分支自己制造（P1-1 / P1-2）


def _branch_with_one_receipt_and_two_code_commits(tmp_path) -> tuple[Repo, str, str]:
    """#1663 AC5(ii) 同形现场：1 张覆盖 c1 的有效收据 + 其后 2 笔无收据代码提交 ⇒ 判红。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3")
    return repo, base, c1


def _bootstrapping_check(repo: Repo, base: str):
    with mock.patch.object(gate0, "REPO_ROOT", str(repo.root)), \
            mock.patch.object(gate0, "receipt_bootstrap_sha", lambda repo_root=None: base), \
            _clean_env():
        return gate0.receipt_present_check(
            str(repo.root), bootstrap_sha=base, _allow_current_full_run_receipt=True,
        )


def test_bootstrap_stays_red_after_the_in_range_receipt_is_deleted(tmp_path):
    """P1-1：删掉区间内那张收据**不能**把 #1663 AC5(ii) 的红变成绿。

    判据必须落在"区间内是否落过收据"（落地历史）而不是"HEAD 树里现存多少张有效收据"——
    后者由分支自己说了算：`git rm docs/gate/<sha>.json` 一笔就能清零。
    修复前：删除后 `attested`（把"陈旧覆盖"放行）。修复后：仍 `violations`。
    """
    repo, base, c1 = _branch_with_one_receipt_and_two_code_commits(tmp_path)
    before = _bootstrapping_check(repo, base)
    assert before.ok is False and before.status == "violations", before.detail

    repo.remove(f"docs/gate/{c1}.json", "docs(gate): drop the receipt")

    after = _bootstrapping_check(repo, base)
    assert after.warn_only is False, after.detail
    assert after.ok is False, after.detail
    assert after.status == "violations", after.detail
    assert "missing receipt for" in after.detail, after.detail


def test_bootstrap_stays_red_after_the_in_range_receipt_is_tampered(tmp_path):
    """P1-1（改坏版）：把区间内收据改成 `passed != total` 使其 invalid 也不得放行自举。

    修复前：改坏后该收据从 `in_range_receipts` 里消失 ⇒ `attested`。修复后：落地历史仍在
    ⇒ `violations`。
    """
    repo, base, c1 = _branch_with_one_receipt_and_two_code_commits(tmp_path)
    assert _bootstrapping_check(repo, base).status == "violations"

    path = repo.root / "docs" / "gate" / f"{c1}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["passed"] = doc["total"] - 1  # 不再是全绿 ⇒ 该收据 invalid
    repo.commit({f"docs/gate/{c1}.json": json.dumps(doc)}, "docs(gate): tamper with the receipt")

    after = _bootstrapping_check(repo, base)
    assert after.warn_only is False, after.detail
    assert after.ok is False, after.detail
    assert after.status == "violations", after.detail


def test_bootstrap_stays_red_when_the_receipt_scan_fails(tmp_path):
    """P1-2：收据扫描读失败 ≠ 树里没有收据，不得据此自举（fail-closed）。

    `head_tree_receipts()` 拿不到 `ls-tree ... docs/gate/` 时返回 `{}`（契约：当它不存在）；
    新消费点若把该 `{}` 读成"零收据 ⇒ 可以出生第一张"，一次瞬时读失败就能让一条真有违规的
    分支变绿并落 PASS 收据。修复前：`attested`；修复后：`violations`（且 detail 说明扫描失败）。
    """
    repo, base, _c1 = _branch_with_one_receipt_and_two_code_commits(tmp_path)
    assert _bootstrapping_check(repo, base).status == "violations"

    real = gate0._git_at_bytes

    def failing(repo_root, *args, **kwargs):
        if args[:1] == ("ls-tree",) and any("docs/gate" in str(a) for a in args):
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: simulated ls-tree failure")
        return real(repo_root, *args, **kwargs)

    with mock.patch.object(gate0, "_git_at_bytes", failing):
        after = _bootstrapping_check(repo, base)
    assert after.warn_only is False, after.detail
    assert after.ok is False, after.detail
    assert after.status == "violations", after.detail
    assert "收据扫描失败" in after.detail, after.detail


# ── #1663 Fix Round 2：落地历史的 git 口径（merge 落盘 / 改名落盘）与自举前置的覆盖面 ──
#
# 第一轮修复把判据从「HEAD 树里现存且有效」换到「区间内落过收据」，但落地历史本身用
# `git log --name-only --diff-filter=A` 取：默认 `git log` **不对 merge 提交做 diff**，且默认
# 开改名检测把 `staged.json → <sha>.json` 判成 `R100`。两条形态因此都看不见，删掉/改坏后
# 自举重开 ⇒ 能落出 PASS 收据（第二轮复审两轴各自端到端跑到）。修法 = `-m` + `--no-renames`
# **并且**只收 `t ∈ base..head`（否则例行 sync merge 从 main 带进来的别人收据会顶掉首张自举）。


def _branch_with_a_receipt_landed_in_a_merge(tmp_path) -> tuple[Repo, str, str]:
    """收据**只在 merge 提交里首次出现**（冲突解决式落盘 + 其后 1 笔代码提交）。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.git("checkout", "-q", "-b", "side")
    repo.commit({"src/s.py": "s = 1\n"}, "side work")
    repo.git("checkout", "-q", "main")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.git("merge", "--no-ff", "--no-commit", "side")
    repo.commit(repo.receipt(c1), "merge side, landing the receipt in the merge commit")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3")
    return repo, base, c1


def test_bootstrap_stays_red_after_a_receipt_landed_in_a_merge_is_deleted(tmp_path):
    """P1-1(a)：收据由 **merge 提交**首次带进来（相对第一父是 A），删掉后不得重开自举。

    `git log` 默认不对 merge 做 diff ⇒ 这笔落地对落地历史隐形。修复前：删除后 `attested`
    （且真落出 PASS 收据）；修复后：`violations`。判据是 `git log -m`。
    """
    repo, base, c1 = _branch_with_a_receipt_landed_in_a_merge(tmp_path)
    before = _bootstrapping_check(repo, base)
    assert before.warn_only is False, before.detail
    assert before.ok is False and before.status == "violations", before.detail

    repo.remove(f"docs/gate/{c1}.json", "docs(gate): drop the merge-landed receipt")

    after = _bootstrapping_check(repo, base)
    assert after.warn_only is False, after.detail
    assert after.ok is False, after.detail
    assert after.status == "violations", after.detail
    assert "missing receipt for" in after.detail, after.detail


def _branch_with_a_receipt_renamed_into_place(tmp_path) -> tuple[Repo, str, str]:
    """收据以非 canonical 名提交，再 `git mv` 到 `docs/gate/<sha>.json`，其后 2 笔代码提交。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    staged = next(iter(repo.receipt(c1).values()))
    repo.commit({"docs/gate/staged.json": staged}, "stage the receipt under a non-receipt name")
    repo.git("mv", "docs/gate/staged.json", f"docs/gate/{c1}.json")
    repo.git("commit", "-q", "-m", "rename the staged receipt into place")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3")
    return repo, base, c1


def test_bootstrap_stays_red_after_a_receipt_renamed_into_place_is_tampered(tmp_path):
    """P1-1(b)：收据以 `R100` 进 canonical 名（改名检测把它判成 R、既不 A 也不匹配收据名），
    改坏后不得重开自举。

    修复前：`attested`（并落出 PASS 收据）；修复后：`violations`。判据是 `--no-renames`
    （改名按"删 + 增"输出，新路径成为 A）。
    """
    repo, base, c1 = _branch_with_a_receipt_renamed_into_place(tmp_path)
    before = _bootstrapping_check(repo, base)
    assert before.warn_only is False, before.detail
    assert before.ok is False and before.status == "violations", before.detail

    path = repo.root / "docs" / "gate" / f"{c1}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["passed"] = doc["total"] - 1  # 不再是全绿 ⇒ 该收据 invalid
    repo.commit({f"docs/gate/{c1}.json": json.dumps(doc)}, "docs(gate): tamper with the renamed receipt")

    after = _bootstrapping_check(repo, base)
    assert after.warn_only is False, after.detail
    assert after.ok is False, after.detail
    assert after.status == "violations", after.detail


def test_bootstrap_survives_a_routine_sync_merge_that_brings_in_other_receipts(tmp_path):
    """P1-1 的**反向**守卫：例行 sync merge 从 main 带进别人的收据，本分支仍能自举首张收据。

    判据不能只看"`-m` 让 merge 出 diff"——那会把 main 侧收据（文件名指向的提交不在
    `merge-base..HEAD` 里）也算成"本分支落过收据" ⇒ 首张收据永远生不出来 = gen-1 死锁复发。
    所以 `landed` 必须过滤到 `t ∈ base..head`。本用例正是钉这条过滤。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    # main 侧：别的票落了代码 + 收据（真实仓里成百上千张）
    repo.git("checkout", "-q", "-b", "other-work")
    other = repo.commit({"src/o.py": "o = 1\n"}, "other ticket code")
    repo.commit(repo.receipt(other), "other ticket receipt on main")
    repo.git("update-ref", "refs/remotes/origin/main", repo.git("rev-parse", "HEAD"))
    # 本票分支：从 base 出发，两笔无收据代码提交
    repo.git("checkout", "-q", "-b", "ticket", base)
    repo.commit({"src/t1.py": "t = 1\n"}, "ticket code 1")
    repo.commit({"src/t2.py": "t = 2\n"}, "ticket code 2")
    repo.git("merge", "--no-ff", "-q", "-m", "merge origin/main", "origin/main")

    with _clean_env():
        # 前提自证：HEAD 树里确实有 main 侧带来的收据，且它不在本分支区间内。
        assert other in gate0.head_tree_receipts(str(repo.root))
        mb = repo.git("merge-base", "origin/main", "HEAD")
        assert other not in repo.git("rev-list", f"{mb}..HEAD").split()
    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is True, check.detail
    assert check.status == "attested", check.detail


def test_bootstrap_stays_red_when_the_receipt_scan_fails_on_a_zero_receipt_branch(tmp_path):
    """P3-2：**零收据**分支 + 收据扫描失败 ⇒ 仍不得自举（fail-closed）。

    既有那条扫描失败用例（`test_bootstrap_stays_red_when_the_receipt_scan_fails`）走的是
    "区间内有收据"路径，`in_range_receipts` 非空就已挡住自举 ⇒ 把 `receipts_scanned` 这一项
    从前置里删掉（MUT-B）时它照样绿。本用例走零收据路径，直接钉住 `receipts_scanned`。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    with _clean_env():
        assert gate0.head_tree_receipts(str(repo.root)) == {}  # 前提自证：树里真的零收据

    real = gate0._git_at_bytes

    def failing(repo_root, *args, **kwargs):
        if args[:1] == ("ls-tree",) and any("docs/gate" in str(a) for a in args):
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: simulated ls-tree failure")
        return real(repo_root, *args, **kwargs)

    with mock.patch.object(gate0, "_git_at_bytes", failing):
        after = _bootstrapping_check(repo, base)
    assert after.warn_only is False, after.detail
    assert after.ok is False, after.detail
    assert after.status == "violations", after.detail
    assert "收据扫描失败" in after.detail, after.detail

def test_bootstrap_stays_red_when_the_landing_history_read_fails(tmp_path):
    """P1-1（新增的 rev-list 那半边）：落地历史读不出来 ⇒ 不得自举（fail-closed）。

    本轮给 `_in_range_receipt_landings` 加了 `git rev-list base..head` 建集合；它读失败同样抛
    `OSError`、由调用方转成红。没有这条用例，把那次 `returncode` 检查删掉（或把它写成
    "失败即空集"）不会有任何测试变红——正是 P1-2 那族"读失败当没有"的形态。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    with _clean_env():
        assert gate0.head_tree_receipts(str(repo.root)) == {}  # 前提自证：零收据分支

    real = gate0._git_at

    def failing(repo_root, *args, **kwargs):
        # 只打 `_in_range_receipt_landings` 那次（右端是具体 sha）；判据自身的
        # `rev-list base..HEAD` 必须放行，否则测的就不是落地历史这一支。
        if (args[:1] == ("rev-list",) and len(args) == 2 and ".." in str(args[1])
                and not str(args[1]).endswith("HEAD")):
            return subprocess.CompletedProcess(args, 128, "", "fatal: simulated rev-list failure")
        return real(repo_root, *args, **kwargs)

    with mock.patch.object(gate0, "_git_at", failing):
        after = _bootstrapping_check(repo, base)
    assert after.warn_only is False, after.detail
    assert after.ok is False, after.detail
    assert after.status == "violations", after.detail


# ── #1692：第二代死锁（收据落地后台账继续漂移，其后又进无收据代码提交） ──
#
# 与上面 7 条 stays_red 用例的鉴别点：那 7 条的现场是「收据新鲜、台账未动」⇒ 违规只有
# missing、没有 stale；第二代死锁的现场（#1686/#1681/#1678 + AC1）都含**台账漂移**：
# 收据绑定的台账指纹随其后新增的台账行漂移 ⇒ 那笔判 `stale ledger fingerprint`。新自举
# 只在这个可机械判定的窄条件上放行（票面方向 1 的「至少一张在场收据」字面过宽，会把
# stays_red 的 before 态一并翻绿）。


def _branch_with_a_receipt_then_ledger_drift_and_code(tmp_path) -> tuple[Repo, str, str]:
    """AC1/AC2 现场：code 1 有 PASS 收据 ⇒ 台账再漂移 ⇒ 其后 1 笔无收据代码提交。

    修复前：违规 = `stale ledger fingerprint`（code 1 的收据绑定的台账已被新增行改掉）
    + `missing receipt`（code 2）；`per_commit_transfer`（code 1 的树 ≠ HEAD 树）与
    `first_receipt`（区间内已落过收据）都不成立 ⇒ 全绿全量跑照样落不出 PASS 收据。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    repo.commit({"docs/review_ledger.d/1692.tsv":
                 "date\tdesc\trange\n2026-10-02\tevidence\tbase..HEAD\n"},
                "docs(ledger): append a review-ledger row")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2 without a receipt")
    return repo, base, c1


def test_second_receipt_bootstrap_after_ledger_drift(tmp_path):
    """AC1/AC2：台账漂移后的第二代死锁 ⇒ 本次全绿全量跑的 HEAD 收据自举（修复前 6/7）。

    修复前：`receipt_present_check(..., _allow_current_full_run_receipt=True)` 返回
    violations，`_apply_receipt_check(will_record_receipt=True)` 把 guards 改写为 rc=1
    （7 条车道里恰 1 条红 = 6/7 的形态）。修复后：attested 且 7/7，落盘收据
    `head_tree_receipts()` 判 valid；CI 硬口径（`_allow_current_full_run_receipt=False`）
    在新收据落盘前后分别是 violations / clear —— 覆盖凭证是这份新收据，不是旧收据。
    """
    repo, base, c1 = _branch_with_a_receipt_then_ledger_drift_and_code(tmp_path)
    head = repo.git("rev-parse", "HEAD")

    with mock.patch.object(gate0, "REPO_ROOT", str(repo.root)), \
            mock.patch.object(gate0, "receipt_bootstrap_sha", lambda repo_root=None: base), \
            _clean_env():
        # 前提自证：区间内那张收据扫描有效（它自己的被测树台账与收据绑定一致）。
        assert gate0.head_tree_receipts(str(repo.root))[c1]["valid"]

        # CI 硬口径（不落盘）：同一状态仍按已提交收据执法 ⇒ 判红。
        hard = gate0.receipt_present_check(
            str(repo.root), bootstrap_sha=base, _allow_current_full_run_receipt=False,
        )
        assert hard.ok is False and hard.status == "violations", hard.detail
        assert "stale ledger fingerprint" in hard.detail, hard.detail
        assert "missing receipt" in hard.detail, hard.detail

        check = gate0.receipt_present_check(
            str(repo.root), bootstrap_sha=base, _allow_current_full_run_receipt=True,
        )
        assert check.warn_only is False, check.detail
        assert check.ok is True, check.detail
        assert check.status == "attested", check.detail

        results = _full_lane_results(repo, base)
        gate0._apply_receipt_check(results, will_record_receipt=True)
        failed = [lane.name for lane, rc, _s, _o in results if rc != 0]
        assert failed == [], f"修复后应 7/7，实际红车道 {failed}"

        path = gate0.write_reading(
            head=head, tree=repo.git("rev-parse", f"{head}^{{tree}}"), argv=[], wall=0.5,
            results=results, affected=None, changed=[], worktree={"untracked": []},
        )
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    assert doc["result"] == "PASS"
    assert doc["passed"] == doc["total"] == len(gate0.GATE0_LANE_IDS)
    repo.commit({f"docs/gate/{head}.json": Path(path).read_text(encoding="utf-8")},
                "docs(gate): record the refreshed HEAD receipt")

    with _clean_env():
        receipts = gate0.head_tree_receipts(str(repo.root))
        assert receipts[head]["valid"], receipts[head]["reason"]
        refreshed = gate0.receipt_present_check(str(repo.root), bootstrap_sha=base)
    assert refreshed.ok is True, refreshed.detail
    assert refreshed.status == "clear", refreshed.detail


def test_second_receipt_bootstrap_stays_red_when_the_landing_history_read_fails(tmp_path):
    """#1692 的第二代自举同样要求落地历史可读（fail-closed）。

    零收据那一支已由 `test_bootstrap_stays_red_when_the_landing_history_read_fails` 钉住；
    本条走**有收据 + 台账漂移**这一支：把 `_in_range_receipt_landings` 的读失败写成
    "空集 ⇒ 当作落过收据"不会让任何旧用例变红（那一支的 `rev-list` 只在零收据路径上被调用），
    本条专门钉它。
    """
    repo, base, _c1 = _branch_with_a_receipt_then_ledger_drift_and_code(tmp_path)
    assert _bootstrapping_check(repo, base).status == "attested"

    real = gate0._git_at

    def failing(repo_root, *args, **kwargs):
        if (args[:1] == ("rev-list",) and len(args) == 2 and ".." in str(args[1])
                and not str(args[1]).endswith("HEAD")):
            return subprocess.CompletedProcess(args, 128, "", "fatal: simulated rev-list failure")
        return real(repo_root, *args, **kwargs)

    with mock.patch.object(gate0, "_git_at", failing):
        after = _bootstrapping_check(repo, base)
    assert after.warn_only is False, after.detail
    assert after.ok is False, after.detail
    assert after.status == "violations", after.detail


def test_second_receipt_bootstrap_requires_an_in_range_valid_receipt(tmp_path):
    """#1692 的第二代自举要求**区间内**至少一张扫描有效的在场收据。

    构造：区间内那张收据（c1）事后被删，HEAD 树里只剩一张**区间外**（base）的历史收据；
    base 是空提交 e 的第一父 ⇒ e 走形状②拿到 base 这个 stale 候选（台账漂移过后指纹不符）。
    此时「有 stale 违规」成立，但区间内没有任何有效收据 ⇒ 不得自举（那正是「落过收据又删掉
    ⇒ 陈旧覆盖」的形状）。把 `in_range_receipts` 从谓词里删掉 ⇒ 本用例转红。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.empty_commit("empty commit right after base")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    repo.commit({"docs/review_ledger.d/1692.tsv":
                 "date\tdesc\trange\n2026-10-02\tevidence\tbase..HEAD\n"},
                "docs(ledger): append a review-ledger row")
    repo.commit(repo.receipt(base), "docs(gate): receipt for the out-of-range base")
    repo.remove(f"docs/gate/{c1}.json", "docs(gate): drop the in-range receipt")

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail


def _tamper_receipt(repo: Repo, sha: str) -> None:
    """原地改坏一张收据（`passed != total`，文件仍在树里）。"""
    path = repo.root / "docs" / "gate" / f"{sha}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["passed"] = doc["total"] - 1
    repo.commit({f"docs/gate/{sha}.json": json.dumps(doc)}, "docs(gate): tamper with the receipt")


def _stale_from_out_of_range_receipt(tmp_path) -> tuple[Repo, str, str]:
    """区间外那张（base）收据在台账漂移后成为 stale 候选；返回 (repo, base, c1)。

    只做到 `code 1` 与台账行——区间内那张收据由调用方按臂自行落地。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.empty_commit("empty commit right after base")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit({"docs/review_ledger.d/1692.tsv":
                 "date\tdesc\trange\n2026-10-02\tevidence\tbase..HEAD\n"},
                "docs(ledger): append a review-ledger row")
    repo.commit(repo.receipt(base), "docs(gate): receipt for the out-of-range base")
    return repo, base, c1


def test_second_receipt_bootstrap_requires_an_in_range_valid_receipt_on_tamper(tmp_path):
    """#1692：区间内收据**改坏但仍在 HEAD 树里**、区间外收据制造 stale 违规 ⇒ 仍不得自举。

    本条是**纵深防御**：`in_range_receipts` / 内容项 / 路径项任一条单独在位都判红
    （M2 / M9 / M10 单删皆绿，三者全删才红）；`in_range_receipts` 的同族另一半见
    `..._on_birth_invalid`。
    """
    repo, base, c1 = _stale_from_out_of_range_receipt(tmp_path)
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    _tamper_receipt(repo, c1)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_requires_an_in_range_valid_receipt_on_birth_invalid(
    tmp_path,
):
    """#1692：区间内那张收据**首次落地就是 FAIL**（从未被编辑）、区间外收据制造 stale ⇒ 判红。

    这条钉的是 `in_range_receipts` 里「**有效**」那半边：内容合取项（从未被编辑）成立，
    挡住自举的只能是「区间内至少一张扫描**有效**的收据」。删掉 `in_range_receipts` ⇒
    本用例转红（`..._on_tamper` 钉另一半）。
    """
    repo, base, c1 = _stale_from_out_of_range_receipt(tmp_path)
    fail = json.loads(next(iter(repo.receipt(c1).values())))
    fail["result"] = "FAIL"
    fail["passed"] = fail["total"] - 1
    repo.commit({f"docs/gate/{c1}.json": json.dumps(fail)},
                "docs(gate): land the first receipt as FAIL")
    with _clean_env():
        receipts = gate0.head_tree_receipts(str(repo.root))
        assert not receipts[c1]["valid"], receipts[c1]  # 前提自证：区间内零有效收据

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_requires_a_trustworthy_receipt_scan(tmp_path):
    """#1692 的第二代自举要求收据扫描可信（部分读失败 = 状态未知 ⇒ 判红）。

    与零收据那一支同理（#1663 P1-2），但走**有 stale 违规**这一支：把真实的
    `_head_tree_receipts_scan` 返回的 `scan_ok` 翻成 False，模拟"读到了部分收据、但读不全"。
    从谓词里删掉 `receipts_scanned` ⇒ 本用例转红。
    """
    repo, base, _c1 = _branch_with_a_receipt_then_ledger_drift_and_code(tmp_path)
    assert _bootstrapping_check(repo, base).status == "attested"

    real_scan = gate0._head_tree_receipts_scan

    def partial_scan(repo_root=None):
        receipts, _ok = real_scan(repo_root)
        return receipts, False

    with mock.patch.object(gate0, "_head_tree_receipts_scan", partial_scan):
        after = _bootstrapping_check(repo, base)
    assert after.warn_only is False, after.detail
    assert after.ok is False, after.detail
    assert after.status == "violations", after.detail
    assert "收据扫描失败" in after.detail, after.detail


# ── #1692 修轮：组合形态（stale 违规与完整性角并存）与有效范围边界 ──
#
# 第一轮实现只要求「至少一笔违规是 stale-ledger」，但 attested 是**整跑级**结论：一次放行
# 覆盖同一次跑的全部违规。于是「另有一张收据在场制造 stale」+「删掉 / 藏掉自己落过的收据」
# 的组合会被一并放行（审查 Standards P1-1 的三组复现）。修法（经三轮）= 内容合取项（首次落地
# 内容）+ 路径历史合取项（第一父链上无 D/M），只判「没被动过」、不判是否仍有效——stale 收据本来
# 就不是有效收据，拿 `in_range_receipts` 当包含域会把三单本身判红、修复失效（见 Spec P2-1 ①）。
# r1 的 key 子集判据（`landed - set(receipts)`）经 M6 实测冗余，已于 r3 删除。


_LEDGER_ROW = {"docs/review_ledger.d/ticket.tsv":
               "date\tdesc\trange\n2026-10-02\tevidence\tbase..HEAD\n"}


def test_second_receipt_bootstrap_stays_red_when_a_landed_receipt_was_deleted(tmp_path):
    """Standards P1-1(a)：删掉一张**落过的**收据、另一张仍在场制造 stale ⇒ 仍判红。

    修复前：`attested`（删收据那一笔的 missing 违规被同一次跑的 stale 违规一并洗白）。
    修复后：「落过就必须还在」不成立 ⇒ `violations`。这条是本轮修复的回归钉子。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    c2 = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.commit(repo.receipt(c2), "docs(gate): receipt for code 2")
    repo.remove(f"docs/gate/{c1}.json", "docs(gate): drop the receipt for code 1")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    # 前提自证：stale 违规确实在场（否则本用例测不到「一次放行覆盖全部违规」）。
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert "missing receipt for" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_committed_fail_receipt_is_hidden(tmp_path):
    """Standards P1-1(b)：藏掉已提交的 FAIL 收据、另一张仍在场制造 stale ⇒ 7 车道不得翻绿。

    修复前：`attested` ⇒ 删掉自己提交过的 FAIL 收据 + 一行日常台账，6/7 现成变成 7/7 并落
    PASS 收据。修复后：落过的 FAIL 收据不在 HEAD 树里 ⇒ `violations`（6/7 保持）。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    c2 = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    fail = json.loads(next(iter(repo.receipt(c2).values())))
    fail["result"] = "FAIL"
    fail["passed"] = fail["total"] - 1
    repo.commit({f"docs/gate/{c2}.json": json.dumps(fail)},
                "docs(gate): commit the FAIL receipt for code 2")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3")
    repo.remove(f"docs/gate/{c2}.json", "docs(gate): drop the FAIL receipt")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail

    with mock.patch.object(gate0, "REPO_ROOT", str(repo.root)), \
            mock.patch.object(gate0, "receipt_bootstrap_sha", lambda repo_root=None: base), \
            _clean_env():
        results = _full_lane_results(repo, base)
        gate0._apply_receipt_check(results, will_record_receipt=True)
    assert [lane.name for lane, rc, _s, _o in results if rc != 0] == ["guards"]


def test_second_receipt_bootstrap_stays_red_when_a_landed_receipt_was_tampered(tmp_path):
    """r2 P1-1：把一张**落过的**收据原地改坏（文件仍在）、另一张仍在场制造 stale ⇒ 仍判红。

    修复轮 1 的 `landed ⊆ set(receipts)` 按 key 判「在不在」：改坏的收据 key 仍在 ⇒ 该合取项
    成立，整跑 `attested`（6/7 洗成 7/7 + PASS 收据）。r2 时代本条是内容合取项的钉子（当时
    M9 单删即红）；r3 加路径项后本条要「内容项 + 路径项」**都删**才转红（M9 / M10 单删皆绿，
    M9+M10 红）——这一格是双重覆盖，本条保留以钉住「改坏 ⇒ `violations`」这一行为。
    内容项的独立鉴别力由侧分支两条钉承担（见本文件 `..._side_branch_*`，M9 单删即红）。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    c2 = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.commit(repo.receipt(c2), "docs(gate): receipt for code 2")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3 without a receipt")
    _tamper_receipt(repo, c1)
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    # 前提自证：改坏的收据仍在 HEAD 树里（只是 invalid），且 stale 违规在场。
    with _clean_env():
        assert c1 in gate0.head_tree_receipts(str(repo.root))
        assert not gate0.head_tree_receipts(str(repo.root))[c1]["valid"]
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_deleted_receipt_is_readded_verbatim(
    tmp_path,
):
    """r3 P1-1(g)：删掉落过的收据、下一笔又用**原字节**加回、另一张仍在场制造 stale ⇒ 判红。

    内容合取项（blob == 首次落地 blob）在恢复原字节后成立，key 合取项也只问在不在 ⇒ 修复轮 2
    仍可整跑洗白（base `violations`、tip `attested`）。补「收据路径在第一父链上未被 D 或 M
    触碰」后 ⇒ `violations`：中间那笔删除仍在路径历史上。（改名按 `--no-renames` 拆成 D+A，
    由 D 覆盖；`R` 在该口径下不产出，本判据不依赖它。`_in_range_receipt_landings` 走
    `--diff-filter=A`，改名的新路径作为 `A` 收下，不受影响。）
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    original = next(iter(repo.receipt(c1).values()))
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    c2 = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.commit(repo.receipt(c2), "docs(gate): receipt for code 2")
    repo.remove(f"docs/gate/{c1}.json", "docs(gate): drop the receipt for code 1")
    repo.commit({f"docs/gate/{c1}.json": original},
                "docs(gate): re-add the identical receipt")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3 without a receipt")

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    # 前提自证：收据内容确实回到首次落地的字节（内容合取项成立），stale 违规在场。
    with _clean_env():
        receipts = gate0.head_tree_receipts(str(repo.root))
        assert receipts[c1]["valid"], receipts[c1]
        assert gate0._receipt_matches_first_landing(str(repo.root), c1)
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_tampered_receipt_is_reverted_verbatim(
    tmp_path,
):
    """r3 P1-1(h)：改坏一张落过的收据、下一笔又写回**原字节**、另一张仍在场制造 stale ⇒ 判红。

    与上一条同理：恢复原字节后内容合取项成立，只有路径历史（M 事件）能看见中间那笔改坏。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    original = next(iter(repo.receipt(c1).values()))
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    c2 = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.commit(repo.receipt(c2), "docs(gate): receipt for code 2")
    _tamper_receipt(repo, c1)
    repo.commit({f"docs/gate/{c1}.json": original},
                "docs(gate): restore the original receipt bytes")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3 without a receipt")

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    with _clean_env():
        receipts = gate0.head_tree_receipts(str(repo.root))
        assert receipts[c1]["valid"], receipts[c1]
        assert gate0._receipt_matches_first_landing(str(repo.root), c1)
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_sees_a_receipt_first_landed_in_a_merge(tmp_path):
    """r3 P2-1：唯一区间内收据由 **merge 提交**带入的合法 gen-2 形状 ⇒ `attested`。

    `_receipt_matches_first_landing` 不带 `-m` 时 `--diff-filter=A` 看不见 merge 落地 ⇒
    恒 False ⇒ 这条形状被判红（r2 相对 r1 的假红，等于该形状重新自锁）。gen-2 调用点改传
    `include_merges=True` 后恢复 `attested`；既有调用点仍走默认 `False`。
    """
    repo, base, c1 = _branch_with_a_receipt_landed_in_a_merge(tmp_path)
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")

    with _clean_env():
        # 前提自证：不带 `-m` 时看不见 merge 落地（这正是被修的盲区）。
        assert gate0._receipt_matches_first_landing(str(repo.root), c1) is False

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is True, check.detail
    assert check.status == "attested", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_merge_landed_receipt_is_tampered(
    tmp_path,
):
    """r3 P2-1 反向守卫：merge 落地后**又被改坏**仍是 `violations`（`-m` 不误伤 fail-closed）。

    现场：c1 收据正常落地（后因例行台账行变 stale）+ c2 收据在 merge 里首次落地后再被改坏。
    改坏 = 一笔 `M` ⇒ 路径合取项与内容合取项**任一在位**都能挡住；本条是纵深防御的组合钉，
    `-m` 的必要性由 `..._sees_a_receipt_first_landed_in_a_merge`（M11 ⇒ 单独转红）钉住。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    c2 = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    receipt_c2 = next(iter(repo.receipt(c2).values()))
    repo.git("checkout", "-q", "-b", "side")
    repo.commit({"src/s.py": "s = 1\n"}, "side work")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "--no-ff", "--no-commit", "side")
    repo.commit({f"docs/gate/{c2}.json": receipt_c2},
                "merge side, landing the receipt in the merge commit")
    _tamper_receipt(repo, c2)
    repo.commit({"src/c.py": "c = 3\n"}, "code 3 without a receipt")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    # 前提自证：stale 违规在场（c1 收据仍在场但指纹过期）。
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_attests_after_a_routine_ledger_row(tmp_path):
    """Spec P2-1 边界（登记语义）：收据**仍在场但 stale**（例行台账行改了指纹）+ 其后 2 笔
    无收据代码提交 ⇒ `attested`。

    与「删掉落过的收据」的差别：收据还在（只是指纹过期）⇒「落过就必须还在」满足 ⇒ 放行。
    这条口径同时是披露面：一旦例行归属行落地，第二代自举实际只要求「本次完整全绿全量跑」，
    不再区分「收据新鲜、其后又进代码提交」的完整性形状（见 `receipt_present_check` docstring）。
    """
    repo, base, _c1 = _branch_with_one_receipt_and_two_code_commits(tmp_path)
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    # 前提自证：该收据仍在 HEAD 树里，但指纹已被台账漂移改掉。
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is True, check.detail
    assert check.status == "attested", check.detail


def _branch_with_a_sync_merge_keeping_its_own_receipt_bytes(tmp_path) -> tuple[Repo, str, str]:
    """分支落收据 A → 台账行 → 无收据代码；origin/main 在同一路径重跑落字节 B；`-s ours` 合并。

    `wall_seconds` 不同 ⇒ 同路径不同字节 ⇒ 真实的多跑一次全量的现场。分支从没动过那张收据：
    HEAD blob == 首次落地 blob，第一父链上零 D/M。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "branch")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    receipt_a = next(iter(repo.receipt(c1).values()))
    repo.commit({f"docs/gate/{c1}.json": receipt_a}, "docs(gate): land the receipt on branch")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2 without a receipt")
    repo.git("checkout", "-q", "main")
    rerun = json.loads(receipt_a)
    rerun["wall_seconds"] = 61.0
    repo.commit({f"docs/gate/{c1}.json": json.dumps(rerun)},
                "docs(gate): origin/main re-ran gate0 on the same tree")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    repo.git("checkout", "-q", "branch")
    repo.git("merge", "--no-ff", "--no-commit", "-s", "ours", "origin/main")
    repo.commit({}, "Merge remote-tracking branch 'origin/main' into branch")
    return repo, base, c1


def test_second_receipt_bootstrap_survives_a_sync_merge_keeping_its_own_receipt(tmp_path):
    """r4 P1-1：sync merge 保留本侧收据字节的合法 gen-2 形状 ⇒ `attested`（跨全部父会误判）。

    `git log -m` 会让 merge 相对第二父也出一份 diff，`-s ours` 保留本侧字节时那正是
    `M docs/gate/<sha>.json` ⇒ 只看「跨父」会把没动过的收据判成动过（r2 `attested` →
    r3 `violations` 的退化）。加 `--first-parent` 后只看本分支交付面 ⇒ 恢复 `attested`。
    """
    repo, base, c1 = _branch_with_a_sync_merge_keeping_its_own_receipt_bytes(tmp_path)

    with _clean_env():
        # 前提自证：HEAD blob 仍是首次落地的字节，内容项也认为没动过。
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is True, check.detail
    assert check.status == "attested", check.detail


def _branch_tampered_inside_the_merge_commit(tmp_path) -> tuple[Repo, str, str]:
    """merge 提交**自身**把落过的收据写成坏字节（第一父 diff 就是 `M`，侧分支历史干净）。"""
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    original = next(iter(repo.receipt(c1).values()))
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    c2 = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.commit(repo.receipt(c2), "docs(gate): receipt for code 2")
    repo.git("checkout", "-q", "-b", "side")
    repo.commit({"src/s.py": "s = 1\n"}, "side work")
    repo.git("checkout", "-q", "main")
    doc = json.loads(original)
    doc["passed"] = doc["total"] - 1
    repo.git("merge", "--no-ff", "--no-commit", "side")
    repo.commit({f"docs/gate/{c1}.json": json.dumps(doc)},
                "merge side, resolving the receipt to tampered bytes")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3 without a receipt")
    return repo, base, c1


def test_second_receipt_bootstrap_stays_red_when_the_merge_commit_tampers_the_receipt(
    tmp_path,
):
    """r5 P1-1 反向守卫：merge 提交自身把落过的收据改坏 ⇒ 仍 `violations`（第一父查询承重）。

    `--first-parent` 让 `git log` 对 merge 出「相对第一父」的 diff ⇒ 这笔 `M` 看得见；少了
    `--first-parent`（或不带 `-m` 的跨父查询）merge 提交根本不产出 diff ⇒ 该形状失守。
    侧查询是 `--full-history --diff-merges=first-parent --no-patch --not origin/main`，它同样
    看得见这笔 merge ⇒ 本格两路都会命中；第一父查询是语义上的第一道闸，侧查询是它的兜底
    （Standards r9 P3-1：r5 时代那句「只有第一父能判」在 r6 换实现后已不成立）。
    """
    repo, base, c1 = _branch_tampered_inside_the_merge_commit(tmp_path)

    with _clean_env():
        # 前提自证：内容项也认为被改过（blob 与首次落地不等），与路径项方向一致。
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True) is False

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_the_path_history_read_fails(tmp_path):
    """r4 P2-1 / r5 P2-2：路径历史的第一父查询读不出来 ⇒ 不得自举（fail-closed）。

    现场用 sync-merge 那一格（本应 `attested`），把第一父那次 `git log` 打成 rc=128 ⇒ 必须转红。
    把 `returncode != 0` 丢掉（fail-open）后它会落回侧分支查询的空结果 ⇒ `attested` ⇒ 本钉转红，
    所以它不是真空断言。定位条件按路径参数 + `--first-parent`，不按 `DMR` 字面串（`DMR`→`DM` 不漏）。
    """
    # 用 sync-merge 那一格：第一父链干净、侧分支历史也干净、内容项为真 ⇒ 正常情况下 `attested`。
    # 于是这条钉子只可能在「第一父查询读失败却没按 fail-closed 处理」时转红，不是真空断言。
    repo, base, _c1 = _branch_with_a_sync_merge_keeping_its_own_receipt_bytes(tmp_path)
    _assert_path_history_read_failure_is_closed(repo, base, "--first-parent")


def test_second_receipt_bootstrap_stays_red_when_the_side_history_read_fails(tmp_path):
    """r5 P1-1 / P2-2 同族：**分支侧历史**那一路读不出来 ⇒ 同样不得自举（fail-closed）。

    现场同 `..._a_side_branch_tamper_is_discarded`（第一父链干净、只有侧分支历史有 `M`）⇒
    这一格只有 `--full-history` 那一路能判，把它的 rc 打成 128 后必须判红，而不是当成「没动过」。
    """
    repo, base, _c1 = _branch_whose_side_edit_is_discarded_by_the_merge(tmp_path, "tamper")
    _assert_path_history_read_failure_is_closed(repo, base, "--full-history")


def _assert_path_history_read_failure_is_closed(repo: Repo, base: str, marker: str) -> None:
    """把 `_receipt_path_was_touched` 的**某一路**查询打成 rc=128 ⇒ 不得自举。

    定位条件按**本函数的路径参数**（`docs/gate/*.json`）＋行为标记（`--first-parent` /
    `--full-history`），**不按会变的过滤字母**——`DMR` 改 `DM` 是等价改法，按字面串定位会让钉子
    真空（r5 Standards P2-2）。要求带路径参数同时避免打中 `_in_range_receipt_landings` 的
    `git log`（它走同一个 `_git_at`，但不按单张收据路径过滤）。

    `marker` 是**必填**的：两个调用点各打一路，合起来才覆盖两路。r6 之前它曾是可选参数并宣称
    「`marker=None` ⇒ 两路都打」——实测为假（`_receipt_matches_first_landing` 的 `--diff-filter=A`
    查询排在前面，一打就短路，两路路径查询根本执行不到），该说法已删（r6 Spec 条件性 P4）。
    """
    real = gate0._git_at

    def failing(repo_root, *args, **kwargs):
        hits_this_path = any(
            str(a).startswith("docs/gate/") and str(a).endswith(".json") for a in args
        )
        if args[:1] == ("log",) and marker in args and hits_this_path:
            return subprocess.CompletedProcess(args, 128, "", "fatal: simulated log failure")
        return real(repo_root, *args, **kwargs)

    with mock.patch.object(gate0, "_git_at", failing):
        check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def _branch_whose_side_edit_is_discarded_by_the_merge(tmp_path, how: str) -> tuple[Repo, str, str]:
    """侧分支碰过落地收据（`how` = tamper / delete / edit_back），merge 冲突解决回**原字节**。

    交付树里那张收据是首次落地的字节、第一父链上零 D/M ⇒ 内容项与「跨父」的直觉都认为没动过；
    被丢弃的是「有人动过这张收据」这个事实，只有侧分支历史看得见。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    original = next(iter(repo.receipt(c1).values()))
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    repo.git("checkout", "-q", "-b", "side")
    if how == "tamper":
        _tamper_receipt(repo, c1)
    elif how == "delete":
        repo.remove(f"docs/gate/{c1}.json", "side: delete the landed receipt")
    else:  # edit_back
        doc = json.loads(original)
        doc["wall_seconds"] = 9.0
        repo.commit({f"docs/gate/{c1}.json": json.dumps(doc)}, "side: edit the receipt")
        repo.commit({f"docs/gate/{c1}.json": original},
                    "side: edit it back to the original bytes")
    repo.git("checkout", "-q", "main")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    repo.git("merge", "--no-ff", "--no-commit", "side")
    repo.commit({f"docs/gate/{c1}.json": original},
                "merge side, discarding its receipt edit")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3 without a receipt")
    return repo, base, c1


def _branch_whose_side_merge_edits_the_receipt(tmp_path, how: str, outer: str):
    """侧分支**在自己的 merge 提交里**碰落地收据（`how` = tamper / delete / fail），交付线丢弃。

    与 `..._side_edit_is_discarded_by_the_merge` 的区别只在编辑发生的**位置**：那里是侧分支的
    普通提交，这里是侧分支 `git merge --no-ff --no-commit` 一个兄弟分支后，在冲突解决里把收据
    写成坏字节 / 删掉 / 写成 FAIL。合法作业里这比普通提交更容易撞上（侧分支 sync 后解冲突）。

    `outer` = conflict ⇒ 交付线 merge 时把收据解回**原字节**；ours ⇒ `git merge -s ours`。
    两种都让交付树回到首次落地的字节，于是内容项与第一父查询都认为没动过。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    original = next(iter(repo.receipt(c1).values()))
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    repo.git("checkout", "-q", "-b", "side")
    repo.git("checkout", "-q", "-b", "sub")
    repo.commit({"src/s.py": "s = 1\n"}, "sub: unrelated work")
    repo.git("checkout", "-q", "side")
    repo.git("merge", "--no-ff", "--no-commit", "sub")
    if how == "tamper":
        doc = json.loads(original)
        doc["passed"] = doc["total"] - 1
        repo.commit({f"docs/gate/{c1}.json": json.dumps(doc)},
                    "side merge: resolve the receipt to tampered bytes")
    elif how == "delete":
        (repo.root / "docs" / "gate" / f"{c1}.json").unlink()
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "side merge: resolve the receipt away")
    else:  # fail
        doc = json.loads(original)
        doc["result"] = "FAIL"
        doc["passed"] = doc["total"] - 1
        repo.commit({f"docs/gate/{c1}.json": json.dumps(doc)},
                    "side merge: resolve the receipt to FAIL bytes")
    repo.git("checkout", "-q", "main")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    if outer == "ours":
        repo.git("merge", "--no-ff", "-s", "ours", "side")
    else:
        repo.git("merge", "--no-ff", "--no-commit", "side")
        repo.commit({f"docs/gate/{c1}.json": original},
                    "merge side, resolving the receipt back to the original bytes")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3 without a receipt")
    return repo, base, c1


def test_second_receipt_bootstrap_stays_red_when_a_side_merge_tampers_the_receipt(
    tmp_path,
):
    """r6 P1-1（钉 1）：侧分支**在 merge 提交里**改坏落过的收据、交付线解回原字节 ⇒ `violations`。

    r5 的侧查询带 `--no-merges`，于是「编辑发生在侧分支的 merge 提交里」两路同时看不见：
    第一父链干净、侧历史被 `--no-merges` 整段滤掉、内容项因交付树回到原字节而成立 ⇒ tip
    `attested`（7/7 + 一张 PASS 收据）。修法：侧查询改 `--diff-merges=first-parent --no-patch`。
    本钉在 r6 修法前**红**（`attested`），修后绿。
    """
    repo, base, c1 = _branch_whose_side_merge_edits_the_receipt(tmp_path, "tamper", "conflict")

    with _clean_env():
        # 前提自证：交付树里是原字节、内容项认为没动过 ⇒ 挡住它的只能是路径项。
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_side_merge_deletes_the_receipt(
    tmp_path,
):
    """r6 P1-1（钉 2）：侧分支 merge 提交里删掉落过的收据、交付线 `-s ours` 丢弃 ⇒ `violations`。"""
    repo, base, c1 = _branch_whose_side_merge_edits_the_receipt(tmp_path, "delete", "ours")

    with _clean_env():
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_side_merge_writes_fail_receipt(
    tmp_path,
):
    """r6 P1-1（钉 3）：侧分支 merge 提交里把落过的收据写成 FAIL、交付线丢弃 ⇒ `violations`。

    这一格同时覆盖「侧分支自己写坏字节」与「交付树回到 PASS 原字节」两个面：内容项成立
    （HEAD blob == 首次落地 blob），判红只能靠侧历史看得见 merge 提交。
    """
    repo, base, c1 = _branch_whose_side_merge_edits_the_receipt(tmp_path, "fail", "conflict")

    with _clean_env():
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_side_branch_edit_back_is_discarded(
    tmp_path,
):
    """r5 P3-1 / r6 Spec P3-1：侧分支「改成别的字节再改回」后交付线丢弃 ⇒ `violations`。

    这条钉消费 `_branch_whose_side_edit_is_discarded_by_the_merge` 的 `edit_back` 分支——r5 加了
    该分支却没有任何调用者（死代码），且 (p3) 形状当时没有专属钉。
    """
    repo, base, c1 = _branch_whose_side_edit_is_discarded_by_the_merge(tmp_path, "edit_back")

    with _clean_env():
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_survives_a_side_merge_that_adopts_origin_mains_receipt(
    tmp_path,
):
    """r6 P1-1 误伤控制：侧分支 sync main 只是**收下 main 写的字节** ⇒ 仍 `attested`。

    侧历史会把侧分支的 merge 提交报成 `M`（它相对第一父把该路径改成了 main 的字节）。那不是
    本分支的编辑 ⇒ 命中提交若在该路径上的 blob 等于 `origin/main` 的 blob 就跳过。删掉这一步
    会让这条合法形状回退成 `violations`（本钉就是它的牙齿）。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "branch")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    original = next(iter(repo.receipt(c1).values()))
    repo.commit({f"docs/gate/{c1}.json": original}, "docs(gate): land the receipt on branch")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2 without a receipt")
    repo.git("checkout", "-q", "main")
    rerun = json.loads(original)
    rerun["wall_seconds"] = 61.0
    repo.commit({f"docs/gate/{c1}.json": json.dumps(rerun)},
                "docs(gate): origin/main re-ran gate0 on the same tree")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    repo.git("checkout", "-q", "-b", "side", "branch")
    repo.git("merge", "--no-ff", "--no-commit", "-X", "theirs", "origin/main")
    repo.commit({f"docs/gate/{c1}.json": json.dumps(rerun)},
                "side: merge origin/main, adopting its receipt bytes")
    repo.git("checkout", "-q", "branch")
    repo.commit({"src/c.py": "c = 3\n"}, "branch: more code")
    repo.git("merge", "--no-ff", "--no-commit", "side")
    repo.commit({f"docs/gate/{c1}.json": original},
                "merge side, keeping our own receipt bytes")
    repo.commit({"src/d.py": "d = 4\n"}, "code 4 without a receipt")

    with _clean_env():
        # 前提自证：侧历史确实看见了那笔 merge（否则本钉测不到跳过分支）。
        side = gate0._git_at(str(repo.root), "log", "--full-history",
                             "--diff-merges=first-parent", "--no-patch", "--no-renames",
                             "--diff-filter=DMR", "--format=%H", f"{base}..HEAD",
                             "--not", "origin/main", "--", f"docs/gate/{c1}.json")
        assert side.stdout.strip(), "fixture must make the side query hit the merge commit"

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is True, check.detail
    assert check.status == "attested", check.detail


def test_second_receipt_bootstrap_survives_a_side_merge_that_adopts_an_older_origin_mains_receipt(
    tmp_path,
):
    """r7 P2-1（长生命周期分支）：侧 merge 收下的是 main **当时**的字节 ⇒ 仍 `attested`。

    main 对同一张收据原地重跑两次（bytes B、C），分支每次 sync 都收下；此后 main 再跑一次
    （bytes D，分支未 sync）⇒ `origin/main` 当前字节已经不等于任何一笔旧 adoption 的字节。
    跳过步若只问「== 当前 `origin/main` 的 blob」，那两笔纯 adoption 会被判成动过 ⇒ 合法分支
    翻红，且**不单调**（HEAD 不动，main 前进一次结论就变）。基准必须是「merge 时那侧交给它的
    字节」：命中提交的 blob == 其第二父的 blob 也算收下。
    本钉同时覆盖 Spec P3-2 的多轮形状（同一 fixture 反复 sync），不另补钉。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "branch")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    original = next(iter(repo.receipt(c1).values()))
    repo.commit({f"docs/gate/{c1}.json": original}, "docs(gate): land the receipt on branch")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2 without a receipt")

    def rerun_on_main(wall: float, msg: str) -> str:
        doc = json.loads(original)
        doc["wall_seconds"] = wall
        body = json.dumps(doc)
        repo.git("checkout", "-q", "main")
        repo.commit({f"docs/gate/{c1}.json": body}, msg)
        repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        return body

    def sync_side(wall: float, body: str) -> str:
        repo.git("merge", "--no-ff", "--no-commit", "-X", "theirs", "origin/main")
        return repo.commit({f"docs/gate/{c1}.json": body},
                           f"side: sync, adopting main bytes {wall}")

    b = rerun_on_main(61.0, "docs(gate): origin/main re-ran gate0 (bytes B)")
    repo.git("checkout", "-q", "-b", "side", "branch")
    sync1 = sync_side(61.0, b)
    c = rerun_on_main(99.0, "docs(gate): origin/main re-ran gate0 again (bytes C)")
    repo.git("checkout", "-q", "side")
    sync2 = sync_side(99.0, c)
    rerun_on_main(123.0, "docs(gate): origin/main re-ran gate0 once more (bytes D)")

    repo.git("checkout", "-q", "branch")
    repo.commit({"src/c.py": "c = 3\n"}, "branch: more code")
    repo.git("merge", "--no-ff", "--no-commit", "side")
    repo.commit({f"docs/gate/{c1}.json": original},
                "merge side, keeping our own receipt bytes")
    repo.commit({"src/d.py": "d = 4\n"}, "code 4 without a receipt")

    path = f"docs/gate/{c1}.json"
    with _clean_env():
        side = gate0._git_at(str(repo.root), "log", "--full-history",
                             "--diff-merges=first-parent", "--no-patch", "--no-renames",
                             "--diff-filter=DMR", "--format=%H", f"{base}..HEAD",
                             "--not", "origin/main", "--", path)
        hits = [h for h in side.stdout.split() if h]
        assert {sync1, sync2} <= set(hits), "fixture must make the side query hit both sync merges"
        # 前提自证：主基准（当前 origin/main 的字节）已不覆盖这两笔 adoption，只有第二父覆盖。
        upstream = gate0._git_at(str(repo.root), "rev-parse", f"origin/main:{path}").stdout.strip()
        for hit in (sync1, sync2):
            blob = gate0._git_at(str(repo.root), "rev-parse", f"{hit}:{path}").stdout.strip()
            assert blob != upstream, "fixture must make the live origin/main blob stale"
            parents = gate0._git_at(str(repo.root), "rev-list", "--parents", "-n", "1",
                                    hit).stdout.split()[1:]
            assert len(parents) >= 2, "sync merge must have two parents"
            second = gate0._git_at(str(repo.root), "rev-parse",
                                   f"{parents[1]}:{path}").stdout.strip()
            assert blob == second, "the second parent must hold the adopted bytes"

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is True, check.detail
    assert check.status == "attested", check.detail


def _branch_whose_side_merge_adopts_another_branchs_receipt_bytes(tmp_path, hops: int):
    """侧分支的 merge **逐字收下兄弟分支改坏的收据字节**，交付线再解回原字节。

    `hops` = 坏字节转手级数：1 ⇒ rogue 直接写坏；2 ⇒ rogue2 写坏 → rogue1 收下 → side 再收下
    rogue1（多级 adoption 链，链条底部的引入笔仍在区间内）。

    形状要点（也是本夹具的鉴别力所在）：命中 merge 的 blob == 其**第二父**的 blob（r7 的跳过步
    据此放行），但该字节**从未**由 `origin/main` 出版过（`main` 在该路径根本没有这个文件）。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "branch")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    original = next(iter(repo.receipt(c1).values()))
    receipt = f"docs/gate/{c1}.json"
    repo.commit({receipt: original}, "docs(gate): land the receipt on branch")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2 without a receipt")

    bad = json.loads(original)
    if hops == 1:
        bad["passed"] = bad["total"] - 1
        bad["result"] = "FAIL"
    else:
        bad["wall_seconds"] = 7.0
    bad_body = json.dumps(bad)

    repo.git("checkout", "-q", "-b", "rogue", base)
    repo.commit({receipt: bad_body}, "rogue: land edited bytes for code 1")
    if hops == 1:
        adopter, src_branch = "rogue", "rogue"
    else:
        repo.git("checkout", "-q", "-b", "rogue1", "branch")
        repo.git("merge", "--no-ff", "--no-commit", "-X", "theirs", "rogue")
        repo.commit({receipt: bad_body}, "rogue1: adopt rogue's bytes")
        adopter, src_branch = "rogue1", "rogue1"
    repo.git("checkout", "-q", "-b", "side", "branch")
    repo.git("merge", "--no-ff", "--no-commit", "-X", "theirs", src_branch)
    sync = repo.commit({receipt: bad_body}, f"side: merge {adopter}, adopting its bytes")

    repo.git("checkout", "-q", "main")
    repo.commit({"src/m.py": "m = 0\n"}, "main: unrelated code")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")

    repo.git("checkout", "-q", "branch")
    repo.commit({"src/c.py": "c = 3\n"}, "branch: more code")
    repo.git("merge", "--no-ff", "--no-commit", "side")
    repo.commit({receipt: original}, "merge side, resolving the receipt back to our bytes")
    repo.commit({"src/d.py": "d = 4\n"}, "code 4 without a receipt")
    return repo, base, c1, receipt, sync


def _assert_sibling_adoption_stays_red(repo, base, c1, receipt, sync):
    """公共断言：夹具前提自证 + 期望 `violations`。"""
    with _clean_env():
        # ① 侧查询确实命中那笔 adoption merge（否则测不到跳过分支）。
        side_hits = gate0._git_at(str(repo.root), "log", "--full-history",
                                  "--diff-merges=first-parent", "--no-patch", "--no-renames",
                                  "--diff-filter=DMR", "--format=%H", f"{base}..HEAD",
                                  "--not", "origin/main", "--", receipt).stdout.split()
        assert sync in side_hits, "fixture must make the side query hit the adopting merge"
        # ② 该 merge 的 blob == 其第二父的 blob（r7 跳过步正是据此放行）。
        blob = gate0._git_at(str(repo.root), "rev-parse", f"{sync}:{receipt}").stdout.strip()
        parents = gate0._git_at(str(repo.root), "rev-list", "--parents", "-n", "1",
                               sync).stdout.split()[1:]
        assert len(parents) >= 2, "the adopting commit must be a merge"
        second = gate0._git_at(str(repo.root), "rev-parse",
                               f"{parents[1]}:{receipt}").stdout.strip()
        assert blob == second, "the merge must adopt its second parent's bytes verbatim"
        # ③ 该字节**从未**由 origin/main 出版过（跳过步不该被这一笔命中）。
        published = set()
        for main_commit in gate0._git_at(str(repo.root), "log", "--format=%H",
                                         "origin/main", "--", receipt).stdout.split():
            got = gate0._git_at(str(repo.root), "rev-parse", f"{main_commit}:{receipt}")
            if got.returncode == 0:
                published.add(got.stdout.strip())
        assert blob not in published, "the adopted bytes must never have been published by main"
        # ④ 交付树里是原字节，内容项认为没动过。
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_side_merge_adopts_a_sibling_branchs_bytes(
    tmp_path,
):
    """r8 Spec P1-1（洗白格 A）：侧 merge 逐字收下**兄弟分支**改坏的字节 ⇒ 必须 `violations`。

    rogue 从 base（收据落地之前）起分支，自己提交一张改坏的收据；side 在**自己的 merge 提交**里
    逐字收下它（`-X theirs`，merge 的 blob == 第二父 blob）；main 只前进一笔代码（收据路径不动，
    `origin/main` 在该路径**没有**该文件）；交付线 merge side 时又把收据解回原字节。其余各钉都
    满足（交付树回到首次落地字节、第一父链干净），判红只能靠侧路跳过步**拒绝**这笔命中。

    r7 把「== 该提交第二父的 blob」当成「收下 main 的字节」⇒ 放行（tip `attested`）；r6 只认
    `origin/main` 当前字节 ⇒ 判红（本钉的 before 态）。r8 换成「main 在该路径出版过的全部字节」
    后，第二父那笔不在集合里 ⇒ 恢复判红。
    """
    repo, base, c1, receipt, sync = _branch_whose_side_merge_adopts_another_branchs_receipt_bytes(
        tmp_path, hops=1
    )
    _assert_sibling_adoption_stays_red(repo, base, c1, receipt, sync)


def test_second_receipt_bootstrap_stays_red_when_a_sibling_adoption_chain_repeats(
    tmp_path,
):
    """r8 Spec P1-1（洗白格 B，两级链）：坏字节 `rogue → rogue1 → side` 逐级逐字转手 ⇒ `violations`。

    比单级多一层「收下的也是兄弟分支的字节」的 merge；若跳过步按第二父放行，整条链会被洗白。
    """
    repo, base, c1, receipt, sync = _branch_whose_side_merge_adopts_another_branchs_receipt_bytes(
        tmp_path, hops=2
    )
    _assert_sibling_adoption_stays_red(repo, base, c1, receipt, sync)


def _branch_whose_side_plain_commit_rewrites_to_historical_main_bytes(
    tmp_path, *, invalid_bytes: bool
):
    """侧分支用**普通提交**（非 merge）把收据写成 main **曾出版、现已替换掉**的字节，交付线解回原字节。

    与 r8 那条兄弟分支钉的区别：这里的字节**确实**由 `origin/main` 出版过 ⇒ r8 的「main 出版过的
    字节集合」会放行它（r9 Spec P1 的洗白格）。r7 的第二父候选从不覆盖它（侧提交没有第二父）。

    `invalid_bytes=True` ⇒ main 先出版一张 FAIL 收据（`passed != total` / `result=FAIL`），再改成
    合法 PASS 字节；侧分支写回那张 FAIL 字节 —— 即「曾被改成非法」被洗成合法。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "branch")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    receipt = f"docs/gate/{c1}.json"
    original = next(iter(repo.receipt(c1).values()))
    repo.commit({receipt: original}, "docs(gate): land the receipt on branch")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2 without a receipt")

    historical = json.loads(original)
    historical["wall_seconds"] = 7.0
    if invalid_bytes:
        historical["passed"] = historical["total"] - 1
        historical["result"] = "FAIL"
    historical_body = json.dumps(historical)

    current = json.loads(original)
    current["wall_seconds"] = 61.0
    current_body = json.dumps(current)

    repo.git("checkout", "-q", "main")
    repo.commit({receipt: historical_body}, "main: publish bytes that will be replaced")
    repo.commit({receipt: current_body}, "main: publish the current bytes")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")

    repo.git("checkout", "-q", "-b", "side", "branch")
    side = repo.commit({receipt: historical_body},
                       "side: rewrite the receipt with a plain commit")

    repo.git("checkout", "-q", "branch")
    repo.commit({"src/c.py": "c = 3\n"}, "branch: more code")
    repo.git("merge", "--no-ff", "--no-commit", "side")
    repo.commit({receipt: original}, "merge side, resolving the receipt back to our bytes")
    repo.commit({"src/d.py": "d = 4\n"}, "code 4 without a receipt")
    return repo, base, c1, receipt, side


def _assert_historical_bytes_rewrite_stays_red(repo, base, c1, receipt, side):
    """前提自证 + 期望 `violations`。"""
    with _clean_env():
        # ① 侧查询确实命中那笔普通提交（它是 D/M 命中，且**没有第二父**）。
        hits = gate0._git_at(str(repo.root), "log", "--full-history",
                             "--diff-merges=first-parent", "--no-patch", "--no-renames",
                             "--diff-filter=DMR", "--format=%H", f"{base}..HEAD",
                             "--not", "origin/main", "--", receipt).stdout.split()
        assert side in hits, "the side query must hit the side branch's plain rewrite"
        parents = gate0._git_at(str(repo.root), "rev-list", "--parents", "-n", "1",
                                side).stdout.split()[1:]
        assert len(parents) == 1, "the rewriting commit must be a plain (non-merge) commit"
        # ② 该字节**确实**由 origin/main 出版过（否则退化成本仓已钉住的「兄弟字节」形状）。
        blob = gate0._git_at(str(repo.root), "rev-parse", f"{side}:{receipt}").stdout.strip()
        published = set()
        for main_commit in gate0._git_at(str(repo.root), "log", "--full-history",
                                         "--format=%H", "origin/main", "--",
                                         receipt).stdout.split():
            got = gate0._git_at(str(repo.root), "rev-parse", f"{main_commit}:{receipt}")
            if got.returncode == 0:
                published.add(got.stdout.strip())
        assert blob in published, "the rewritten bytes must have been published by main"
        # ③ 它不是 main **当前**字节 ⇒ 那个显式例外覆盖不到它。
        upstream = gate0._git_at(str(repo.root), "rev-parse",
                                 f"origin/main:{receipt}").stdout.strip()
        assert blob != upstream, "the rewritten bytes must be a replaced, not current, blob"
        # ④ 交付树回到首次落地字节，内容项认为没动过。
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_side_plain_commit_rewrites_to_replaced_main_bytes(
    tmp_path,
):
    """r9 Spec P1（洗白格）：侧分支普通提交写回 main **曾出版、现已替换**的字节 ⇒ 必须 `violations`。

    r8 的跳过步只问「这串字节是否曾被 main 出版」，不问**来源**：分支自己用普通提交写出的历史
    字节因而被当成 adoption 放行（交付线再解回原字节，其余各钉全满足）。r9 起加 donor 身份判定
    ——该字节必须由一个 main 侧的父逐字交来，普通提交没有这样的父 ⇒ 判红。
    """
    repo, base, c1, receipt, side = (
        _branch_whose_side_plain_commit_rewrites_to_historical_main_bytes(tmp_path, invalid_bytes=False)
    )
    _assert_historical_bytes_rewrite_stays_red(repo, base, c1, receipt, side)


def test_second_receipt_bootstrap_stays_red_when_a_side_plain_commit_restores_replaced_fail_bytes(
    tmp_path,
):
    """r9 Spec P1（FAIL 变体）：main 曾出版一张 FAIL 收据、后改成 PASS；侧分支普通提交写回那张
    FAIL 字节 ⇒ 必须 `violations`（「曾被改成非法」不得被洗成合法）。
    """
    repo, base, c1, receipt, side = (
        _branch_whose_side_plain_commit_rewrites_to_historical_main_bytes(tmp_path, invalid_bytes=True)
    )
    _assert_historical_bytes_rewrite_stays_red(repo, base, c1, receipt, side)


def test_second_receipt_bootstrap_survives_a_side_branch_plain_commit_copying_origin_mains_receipt(
    tmp_path,
):
    """侧分支用**普通提交**拷走 `origin/main` **当前**字节、交付线保留本侧字节 ⇒ `attested`。

    Standards r8 P2-1 / r9 P4-2 指出的形状：没有第二父，只能靠「main 当前 blob」这条身份例外放行
    （donor 判定对它为 False —— 它的父不持有该字节）。r8 起没有钉覆盖；本钉把它机械钉住。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "branch")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    receipt = f"docs/gate/{c1}.json"
    original = next(iter(repo.receipt(c1).values()))
    repo.commit({receipt: original}, "docs(gate): land the receipt on branch")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2 without a receipt")

    rerun = json.loads(original)
    rerun["wall_seconds"] = 61.0
    current_body = json.dumps(rerun)
    repo.git("checkout", "-q", "main")
    repo.commit({receipt: current_body}, "main: re-ran gate0 (current bytes)")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")

    repo.git("checkout", "-q", "-b", "side", "branch")
    copy_commit = repo.commit({receipt: current_body},
                              "side: copy main's receipt bytes with a plain commit")
    repo.git("checkout", "-q", "branch")
    repo.commit({"src/c.py": "c = 3\n"}, "branch: more code")
    repo.git("merge", "--no-ff", "--no-commit", "-s", "ours", "side")
    repo.commit({}, "merge side, keeping our own bytes")
    repo.commit({"src/d.py": "d = 4\n"}, "code 4 without a receipt")

    with _clean_env():
        blob = gate0._git_at(str(repo.root), "rev-parse",
                            f"{copy_commit}:{receipt}").stdout.strip()
        upstream = gate0._git_at(str(repo.root), "rev-parse",
                                 f"origin/main:{receipt}").stdout.strip()
        assert blob == upstream, "the copied bytes must be origin/main's current blob"
        parents = gate0._git_at(str(repo.root), "rev-list", "--parents", "-n", "1",
                                copy_commit).stdout.split()[1:]
        assert len(parents) == 1, "the copying commit must be a plain (non-merge) commit"
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is True, check.detail
    assert check.status == "attested", check.detail


def test_second_receipt_bootstrap_stays_attested_as_origin_main_prunes_the_adopted_bytes(
    tmp_path,
):
    """r9 Spec P2：main 用一笔「结果 == 第二父」的 merge 把旧收据字节剪掉后，合法 sync 仍 `attested`。

    `git log <rev> -- <path>` 默认做路径历史简化：与某个父 TREESAME 的 merge 被剪掉、其祖先也可能
    整段消失。r8 的候选集合没带 `--full-history` ⇒ main 把旧字节换成新字节后，分支**合法**收下的
    那串旧字节不再是候选 ⇒ 判红。关键断言是**单调性**：同一 HEAD 在 main 前进前后都必须 `attested`
    （r7 在同样形状上 attested→attested；r8 是 attested→violations）。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "branch")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    receipt = f"docs/gate/{c1}.json"
    original = next(iter(repo.receipt(c1).values()))
    repo.commit({receipt: original}, "docs(gate): land the receipt on branch")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2 without a receipt")

    adopted = json.loads(original)
    adopted["wall_seconds"] = 61.0
    adopted_body = json.dumps(adopted)
    repo.git("checkout", "-q", "main")
    main_b = repo.commit({receipt: adopted_body},
                         "main: publish B (the bytes the branch will adopt)")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    main_b_parent = repo.git("rev-parse", "HEAD^")

    repo.git("checkout", "-q", "-b", "side", "branch")
    repo.git("merge", "--no-ff", "--no-commit", "-X", "theirs", "origin/main")
    repo.commit({receipt: adopted_body}, "side: sync, adopting main's bytes")

    repo.git("checkout", "-q", "branch")
    repo.commit({"src/c.py": "c = 3\n"}, "branch: more code")
    repo.git("merge", "--no-ff", "--no-commit", "side")
    repo.commit({receipt: original}, "merge side, resolving the receipt back to our bytes")
    repo.commit({"src/d.py": "d = 4\n"}, "code 4 without a receipt")
    head = repo.git("rev-parse", "HEAD")

    # stage 1: main 顶端正是分支收下的那串字节 ⇒ 合法 adoption。
    first = _bootstrapping_check(repo, base)
    assert first.warn_only is False, first.detail
    assert first.status == "attested", first.detail

    # main 用一笔「结果 == 第二父」的 merge 换成别的字节 ⇒ 旧字节被默认路径简化剪掉。
    lane_bytes = json.loads(original)
    lane_bytes["wall_seconds"] = 99.0
    repo.git("checkout", "-q", "-b", "lane", main_b_parent)
    repo.commit({receipt: json.dumps(lane_bytes)}, "lane: receipt with different bytes")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "--no-ff", "--no-commit", "-X", "theirs", "lane")
    repo.commit({}, "main: merge lane (result is the second parent's, dropping B)")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    repo.git("checkout", "-q", head)

    with _clean_env():
        # 前提自证：分支 HEAD 没动；被收下的那串字节被默认列表剪掉、但 `--full-history` 仍看得见。
        assert repo.git("rev-parse", "HEAD") == head, "the branch HEAD must not move"
        adopted_blob = gate0._git_at(str(repo.root), "rev-parse",
                                     f"{main_b}:{receipt}").stdout.strip()

        def _published(*extra) -> set[str]:
            out = gate0._git_at(str(repo.root), "log", *extra, "--format=%H",
                                "origin/main", "--", receipt).stdout.split()
            blobs = set()
            for commit in out:
                got = gate0._git_at(str(repo.root), "rev-parse", f"{commit}:{receipt}")
                if got.returncode == 0:
                    blobs.add(got.stdout.strip())
            return blobs

        assert adopted_blob not in _published(), (
            "fixture must make default path simplification prune the adopted bytes"
        )
        assert adopted_blob in _published("--full-history"), (
            "fixture must keep the adopted bytes visible under --full-history"
        )

    second = _bootstrapping_check(repo, base)
    assert second.warn_only is False, second.detail
    assert second.status == "attested", second.detail


def test_second_receipt_bootstrap_stays_red_when_a_side_branch_tamper_is_discarded(
    tmp_path,
):
    """r5 P1-1（钉 1）：侧分支改坏落过的收据、merge 写回原字节 ⇒ 仍 `violations`。

    `--first-parent` 只看交付线 ⇒ 侧分支那笔 `M` 藏在第二父后面，交付树回到原字节，内容项也成立
    （HEAD blob == 首次落地 blob）⇒ r4 起整跑 `attested`（7/7 + PASS 收据）。侧分支是本分支自己的
    提交，被 merge 丢弃不等于没发生过 ⇒ 路径项必须另看分支侧历史（`--full-history
    --diff-merges=first-parent --no-patch --not origin/main`；r5 的 `--no-merges` 已不再使用）。
    """
    repo, base, c1 = _branch_whose_side_edit_is_discarded_by_the_merge(tmp_path, "tamper")

    with _clean_env():
        # 前提自证：交付树里是原字节，内容项认为没动过；第一父链也干净。
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_side_branch_delete_is_discarded(
    tmp_path,
):
    """r5 P1-1（钉 2）：侧分支删掉落过的收据、merge 写回原字节 ⇒ 仍 `violations`（同上）。"""
    repo, base, c1 = _branch_whose_side_edit_is_discarded_by_the_merge(tmp_path, "delete")

    with _clean_env():
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_side_branch_fail_receipt_is_discarded(
    tmp_path,
):
    """r5 P1-1（钉 3）：侧分支提交某代码提交的 **FAIL 收据**、merge 丢弃 ⇒ 仍 `violations`。

    挡住它的是**内容项**（`HEAD:path` 缺失 ⇒ False），不是路径项——路径项看不到从未进交付树的
    收据。所以这条钉是内容项的独立鉴别力钉子：M9（单删内容项）下它必须转红。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    c2 = repo.commit({"src/b.py": "b = 2\n"}, "code 2")
    fail = json.loads(next(iter(repo.receipt(c2).values())))
    fail["result"] = "FAIL"
    fail["passed"] = fail["total"] - 1
    repo.git("checkout", "-q", "-b", "side")
    repo.commit({f"docs/gate/{c2}.json": json.dumps(fail)},
                "side: land a FAIL receipt for code 2")
    repo.git("checkout", "-q", "main")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3 without a receipt")
    repo.git("merge", "--no-ff", "-s", "ours", "side")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_a_side_branch_receipt_is_discarded(
    tmp_path,
):
    """r5 P1-1（钉 3 的同族）：侧分支给**自己的代码提交**落了收据、merge 丢弃 ⇒ 仍 `violations`。

    与上一条同属「内容项承重」的一格：那张收据在区间内落过（进 `landed`），但交付树里没有
    `HEAD:path` ⇒ 内容项为 False。路径项看得到它：侧查询带 `--diff-merges=first-parent`，
    侧分支 merge 提交里的编辑会出 diff（r5 的 `--no-merges` 已不再使用）。
    两条一起把内容项的独立贡献钉住（M9 单删 ⇒ 两条都转红）。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit(repo.receipt(c1), "docs(gate): receipt for code 1")
    repo.git("checkout", "-q", "-b", "side")
    c2 = repo.commit({"src/b.py": "b = 2\n"}, "side: code 2")
    repo.commit(repo.receipt(c2), "side: land the receipt for code 2")
    repo.git("checkout", "-q", "main")
    repo.commit({"src/c.py": "c = 3\n"}, "code 3 without a receipt")
    repo.git("merge", "--no-ff", "-s", "ours", "side")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def _branch_whose_side_merge_adopts_a_discarded_topic_blob(tmp_path, *, conflict: bool):
    """topic 写「坏收据字节」→ main 把那笔 merge 的**内容整段丢弃** → 侧分支逐字收下坏字节。

    与其余钉的区别（r10 Spec P1）：这里的字节**曾经出现在 `origin/main` 的历史里**（topic 提交
    被 merge 过、因而是 main 的祖先），但**从未进入 main 的第一父线**——`-s ours` 整棵丢弃，或
    `-X ours` 把冲突解成 main 侧字节。

    `--full-history` 枚举会把它算成「main 出版过」（默认路径简化恰好剪掉它），于是「topic 写在
    main 之前、侧分支 merge 收下」这条形状在 r9 被判 `attested`。候选集合以 `--first-parent` 为界
    后恢复 `violations`（r6 / r8 也是 `violations`，只有 r7 / r9 放行）。

    `conflict=True` ⇒ 走 `-X ours` 变体（main 在分叉后又推进一次同一路径，制造 modify/modify 冲突）。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "branch")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    receipt = f"docs/gate/{c1}.json"
    original = next(iter(repo.receipt(c1).values()))
    repo.commit({receipt: original}, "docs(gate): land the receipt on branch")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2 without a receipt")

    bad = json.loads(original)
    bad["passed"] = bad["total"] - 1
    bad["result"] = "FAIL"
    bad_body = json.dumps(bad)

    repo.git("checkout", "-q", "main")
    if conflict:
        # main 先在这个路径写自己的字节，分叉后再推进一次 ⇒ 与 topic 的改动形成真冲突。
        other = json.loads(original)
        other["wall_seconds"] = 71.0
        repo.commit({receipt: json.dumps(other)}, "main: publish its own receipt")
        fork = repo.git("rev-parse", "HEAD")
    else:
        fork = repo.git("rev-parse", "main")

    repo.git("checkout", "-q", "-b", "topic", fork)
    topic_commit = repo.commit({receipt: bad_body}, "topic: write FAIL bytes")

    repo.git("checkout", "-q", "main")
    if conflict:
        later = json.loads(original)
        later["wall_seconds"] = 83.0
        repo.commit({receipt: json.dumps(later)},
                    "main: advance its own bytes after the fork")
        repo.git("merge", "--no-ff", "--no-commit", "-X", "ours", "topic")
        repo.commit({}, "main: merge topic with -X ours (keep main's bytes)")
    else:
        repo.git("merge", "--no-ff", "--no-commit", "-s", "ours", "topic")
        repo.commit({}, "main: merge topic with -s ours (discard its tree)")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")

    # 侧分支用 merge 从 topic 逐字收下坏字节（donor 身份 = topic 这个 main 侧祖先）。
    # 用普通提交写同样字节是 r9 已钉住的另一种洗白格，不是本形状。
    repo.git("checkout", "-q", "-b", "side", "branch")
    repo.git("merge", "--no-ff", "--no-commit", "-X", "theirs", "topic")
    adoption = repo.commit({}, "side: merge topic, adopting the FAIL bytes verbatim")

    repo.git("checkout", "-q", "branch")
    repo.commit({"src/c.py": "c = 3\n"}, "branch: more code")
    repo.git("merge", "--no-ff", "--no-commit", "-s", "ours", "side")
    repo.commit({}, "merge side with -s ours, keeping our own bytes")
    repo.commit({"src/d.py": "d = 4\n"}, "code 4 without a receipt")
    return repo, base, c1, receipt, bad_body, topic_commit, adoption


def _assert_discarded_topic_blob_stays_red(repo, base, c1, receipt, bad_body, topic_commit):
    """前提自证（四条）+ 期望 `violations`。"""
    with _clean_env():
        # ① 侧查询确实命中那笔 adoption merge。
        hits = gate0._git_at(str(repo.root), "log", "--full-history",
                             "--diff-merges=first-parent", "--no-patch", "--no-renames",
                             "--diff-filter=DMR", "--format=%H", f"{base}..HEAD",
                             "--not", "origin/main", "--", receipt).stdout.split()
        assert hits, "the side query must hit the side branch's adoption merge"
        adoption = hits[0]
        parents = gate0._git_at(str(repo.root), "rev-list", "--parents", "-n", "1",
                                adoption).stdout.split()[1:]
        assert len(parents) >= 2, "the adopting commit must be a merge"
        blob = gate0._git_at(str(repo.root), "rev-parse",
                             f"{adoption}:{receipt}").stdout.strip()

        def listed(*extra):
            blobs = set()
            for c in gate0._git_at(str(repo.root), "log", *extra, "--format=%H",
                                   "origin/main", "--", receipt).stdout.split():
                got = gate0._git_at(str(repo.root), "rev-parse", f"{c}:{receipt}")
                if got.returncode == 0:
                    blobs.add(got.stdout.strip())
            return blobs

        # ② 该 blob 在 --full-history 候选集里，但**不在** origin/main 的第一父线上。
        assert blob in listed("--full-history"), "the bytes must appear in --full-history"
        assert blob not in listed("--first-parent", "--full-history"), (
            "the bytes must NOT have been published on origin/main's first-parent line")
        # ③ topic 提交确实是 origin/main 的祖先（否则退化成本仓已钉住的兄弟分支形状）。
        assert gate0._git_at(str(repo.root), "merge-base", "--is-ancestor",
                             topic_commit, "origin/main").returncode == 0, (
            "the topic commit must be an ancestor of origin/main")
        # ④ 交付树回到首次落地字节，内容项认为没动过。
        assert gate0._receipt_matches_first_landing(str(repo.root), c1, include_merges=True)

    check = _bootstrapping_check(repo, base)
    assert check.warn_only is False, check.detail
    assert "stale ledger fingerprint" in check.detail, check.detail
    assert check.ok is False, check.detail
    assert check.status == "violations", check.detail


def test_second_receipt_bootstrap_stays_red_when_main_discards_a_topics_fail_bytes(tmp_path):
    """r10 Spec P1（k1）：topic 写 FAIL 字节 → main `-s ours` 整棵丢弃 → 侧分支 merge 收下 ⇒ `violations`。

    r9 的候选集合枚举缺 `--first-parent`：topic 提交因「被 main merge 过」而进了 `--full-history`
    列表，于是这段**从未在主线出版过**的坏字节被当成「main 出版过」⇒ 放行。r6 / r8 判 `violations`，
    r7 / r9 放行（r7 的第二父候选也覆盖这条形状）。
    """
    repo, base, c1, receipt, bad_body, topic, _ = (
        _branch_whose_side_merge_adopts_a_discarded_topic_blob(tmp_path, conflict=False)
    )
    _assert_discarded_topic_blob_stays_red(repo, base, c1, receipt, bad_body, topic)


def test_second_receipt_bootstrap_stays_red_when_main_resolves_conflict_against_a_topics_fail_bytes(
    tmp_path,
):
    """r10 Spec P1（k4）：main 已出版自己的字节 → topic 写 FAIL → main `-X ours` 解冲突丢弃 ⇒ `violations`。

    与 k1 同机制，只是丢弃发生在**冲突解法**里而不是 `-s ours`。两条一起把「第一父线是出版的
    唯一口径」钉住。
    """
    repo, base, c1, receipt, bad_body, topic, _ = (
        _branch_whose_side_merge_adopts_a_discarded_topic_blob(tmp_path, conflict=True)
    )
    _assert_discarded_topic_blob_stays_red(repo, base, c1, receipt, bad_body, topic)


def test_second_receipt_bootstrap_plain_copy_of_main_current_bytes_turns_red_after_origin_main_advances_known_boundary_5(
    tmp_path,
):
    """已知边界⑤（**不是**回归钉）：侧分支普通提交拷走 `origin/main` 当前字节，main 原地改写同一张
    收据后，同一 HEAD 由 `attested` 翻 `violations`。

    这条钉把**已知的不单调**机械钉住（r10 P2/S-1，处置取 (a)）：`current_main_blob` 例外判的是
    「main **当前** 的 blob」，main 一动它就失效；而普通提交没有持有该字节的父 ⇒ donor 判定恒为
    False ⇒ 没有任何东西再接住这条路径。对照：同一形状改用 **merge** 收下（donor=True）则
    `attested → attested`，不受影响（r9 的 `..._stays_attested_as_origin_main_prunes_the_adopted_bytes`
    覆盖的正是那条）。

    现状为 r6/r7 同病、r8 单调（集合无身份约束）、r9 起恢复翻转。**真修复需要时间维判定**（拷贝
    时刻该字节是否仍是 main 当前 blob），超出 #1692 的最小改动范围 ⇒ 另开票。本钉只保证「它是
    已知的」可判定，不声称现状正确。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "branch")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    receipt = f"docs/gate/{c1}.json"
    original = next(iter(repo.receipt(c1).values()))
    repo.commit({receipt: original}, "docs(gate): land the receipt on branch")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2 without a receipt")

    b = json.loads(original)
    b["wall_seconds"] = 61.0
    b_body = json.dumps(b)
    c = json.loads(original)
    c["wall_seconds"] = 99.0
    c_body = json.dumps(c)

    repo.git("checkout", "-q", "main")
    repo.commit({receipt: b_body}, "main: re-ran gate0 (bytes B)")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")

    repo.git("checkout", "-q", "-b", "side", "branch")
    copy_commit = repo.commit({receipt: b_body},
                              "side: copy main's current bytes with a plain commit")
    repo.git("checkout", "-q", "branch")
    repo.commit({"src/c.py": "c = 3\n"}, "branch: more code")
    repo.git("merge", "--no-ff", "--no-commit", "-s", "ours", "side")
    repo.commit({}, "merge side, keeping our own bytes")
    repo.commit({"src/d.py": "d = 4\n"}, "code 4 without a receipt")
    head = repo.git("rev-parse", "HEAD")

    with _clean_env():
        parents = gate0._git_at(str(repo.root), "rev-list", "--parents", "-n", "1",
                                copy_commit).stdout.split()[1:]
        assert len(parents) == 1, "the copying commit must be a plain (non-merge) commit"
        upstream = gate0._git_at(str(repo.root), "rev-parse",
                                 f"origin/main:{receipt}").stdout.strip()
        blob = gate0._git_at(str(repo.root), "rev-parse",
                             f"{copy_commit}:{receipt}").stdout.strip()
        assert blob == upstream, "stage 1 copies origin/main's *current* blob"

    stage1 = _bootstrapping_check(repo, base)
    assert stage1.warn_only is False, stage1.detail
    assert stage1.ok is True, stage1.detail
    assert stage1.status == "attested", stage1.detail

    # main 原地重写同一张收据（B → C）；HEAD **不动**。
    repo.git("checkout", "-q", "main")
    repo.commit({receipt: c_body}, "main: rewrite the same receipt (B -> C)")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    repo.git("checkout", "-q", head)
    assert repo.git("rev-parse", "HEAD") == head, "HEAD must not move between the stages"

    stage2 = _bootstrapping_check(repo, base)
    assert stage2.warn_only is False, stage2.detail
    assert stage2.ok is False, stage2.detail
    assert stage2.status == "violations", stage2.detail


def test_donor_depth_cap_fails_closed_instead_of_raising_recursion_error(tmp_path):
    """r10 必修 4：donor 转手链超过 `_DONOR_MAX_DEPTH` 跳 ⇒ **判动过**，不抛 `RecursionError`。

    形状：main 第一父线出版 B（后原地改成 C ⇒ `current_main_blob != B`，短路例外兜不住）、
    侧支一串 adoption merge 每跳都持 B、链根接在 main 侧持 B 的提交上 ⇒ donor 必须递归到
    链底才能回答 True。链长 > 上限时报「不成立」（False）⇒ 调用方不跳过 ⇒ 顶层判 `violations`。

    这条钉**反向锁死返回值的符号**：`_handed_over_by_main` 的 True 语义是「main 交来的 ⇒
    放行」，深度超限必须是 False。r10 一度写成 True，1000 跳探针里 r9 抛 `RecursionError`、
    写反的 fix10 直接判 `attested`，两个都不对。
    """
    repo = Repo(tmp_path / "r")
    base = _base_with_origin(repo)
    repo.git("checkout", "-q", "-b", "branch")
    c1 = repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    receipt = f"docs/gate/{c1}.json"
    original = next(iter(repo.receipt(c1).values()))
    repo.commit({receipt: original}, "docs(gate): land the receipt on branch")
    repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")

    historical = json.loads(original)
    historical["wall_seconds"] = 7.0
    historical_body = json.dumps(historical)
    current = json.loads(original)
    current["wall_seconds"] = 61.0
    current_body = json.dumps(current)

    repo.git("checkout", "-q", "main")
    main_holds_b = repo.commit({receipt: historical_body}, "main: publish bytes B")
    repo.commit({receipt: current_body}, "main: rewrite to C")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")

    # 先红口径：r9 树**没有**这个上限常量 ⇒ 本钉在 r9 上以显式断言失败（不是靠碰巧的
    # AttributeError），在 tip 上才走到真正的行为验证。
    cap = getattr(gate0, "_DONOR_MAX_DEPTH", None)
    assert isinstance(cap, int) and cap > 0, "r10 必修 4：必须有显式的 donor 深度上限常量"
    hops = cap + 5
    repo.git("checkout", "-q", "-b", "side", "branch")
    # 每跳都持 main 出版过的 B（相对第一父 branch 的原始字节是 M ⇒ 侧查询命中）
    b_tree = repo.git("rev-parse", f"{main_holds_b}^{{tree}}")
    hop = main_holds_b
    for i in range(hops):
        hop = repo.git("commit-tree", b_tree, "-p", "branch", "-p", hop,
                       "-m", f"side: adopt hop {i}")
    repo.git("update-ref", "refs/heads/chain", hop)
    repo.git("checkout", "-q", "branch")
    repo.git("merge", "--no-ff", "--no-commit", "chain")
    repo.commit({receipt: original}, "merge the chain, resolving back to our bytes")
    # 尾部再落一笔**没有收据的代码提交**：否则违规提交与 HEAD 的差异只剩收据路径
    # （纯 docs）⇒ 逐笔传递把红洗绿，测不到路径项。
    repo.commit({"src/d.py": "d = 4\n"}, "code 4 without a receipt")

    with _clean_env():
        before = sys.getrecursionlimit()
        # ① 超过上限（= 上限 + 5 跳）⇒ 判「动过」，且**不抛** `RecursionError`。
        assert gate0._receipt_path_was_touched(str(repo.root), base, "HEAD", c1) is True, \
            "超过深度上限必须判「动过」（fail-closed），而不是放行或抛异常"
        assert sys.getrecursionlimit() == before
        # ② 同一棵树、同一条链，只把上限抬高到链长之上 ⇒ 回到「没动过」。
        #    这一步证明 ① 是**上限**在起作用，而不是别的路径碰巧判红。
        with mock.patch.object(gate0, "_DONOR_MAX_DEPTH", hops + 10):
            assert gate0._receipt_path_was_touched(
                str(repo.root), base, "HEAD", c1) is False
        # ③ 顶层判红（该函数是 `second_receipt` 的合取项之一）。
        check = _bootstrapping_check(repo, base)
        assert check.ok is False, check.detail
        assert check.status == "violations", check.detail
