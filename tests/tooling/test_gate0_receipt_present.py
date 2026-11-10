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
#   · AC5(ii) = 分支里**已有**有效收据、其后又进代码提交 ⇒ 仍判红（真正钉住该语义的是
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
    """AC5(ii) 同形现场：1 张覆盖 c1 的有效收据 + 其后 2 笔无收据代码提交 ⇒ 判红。"""
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
    """P1-1：删掉区间内那张收据**不能**把 AC5(ii) 的红变成绿。

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


def test_second_receipt_bootstrap_requires_an_in_range_valid_receipt_on_tamper(tmp_path):
    """#1692 修轮：区间内收据**改坏但仍在 HEAD 树里**时，`in_range_receipts` 仍是必需的合取项。

    与 `..._requires_an_in_range_valid_receipt`（删收据）不同：这里收据文件还在，只是
    `passed != total`。「落过就必须还在」按 **key** 判、这一条仍成立，所以挡住自举的只能是
    `in_range_receipts`（现存且**有效**）。缺了它，区间外那张 base 收据制造的 stale 违规
    就会把改坏收据的现场放行。把 `in_range_receipts` 从谓词里删掉 ⇒ 本用例转红。
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
    _tamper_receipt(repo, c1)

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
# 的组合会被一并放行（审查 Standards P1-1 的三组复现）。修法 = 补「落过就必须还在」：
# `landed - set(receipts)` 为空才放行（只判在不在，不判是否仍有效——stale 收据本来就不是
# 有效收据，拿 `in_range_receipts` 当包含域会把三单本身判红、修复失效，见 Spec P2-1 ①）。


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


def _tamper_receipt(repo: Repo, sha: str) -> None:
    path = repo.root / "docs" / "gate" / f"{sha}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["passed"] = doc["total"] - 1
    repo.commit({f"docs/gate/{sha}.json": json.dumps(doc)}, "docs(gate): tamper with the receipt")


def _scan_failure_patch():
    real = gate0._git_at_bytes

    def failing(repo_root, *args, **kwargs):
        if args[:1] == ("ls-tree",) and any("docs/gate" in str(a) for a in args):
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: simulated ls-tree failure")
        return real(repo_root, *args, **kwargs)

    return mock.patch.object(gate0, "_git_at_bytes", failing)


def _landing_read_failure_patch():
    real = gate0._git_at

    def failing(repo_root, *args, **kwargs):
        if (args[:1] == ("rev-list",) and len(args) == 2 and ".." in str(args[1])
                and not str(args[1]).endswith("HEAD")):
            return subprocess.CompletedProcess(args, 128, "", "fatal: simulated rev-list failure")
        return real(repo_root, *args, **kwargs)

    return mock.patch.object(gate0, "_git_at", failing)


def _two_plain_code_commits(repo: Repo) -> None:
    repo.commit({"src/a.py": "a = 1\n"}, "code 1")
    repo.commit({"src/b.py": "b = 2\n"}, "code 2")


def _stays_red_scene(scene: str, root: Path):
    """重建 7 条 `test_bootstrap_stays_red_*` 的现场（不改那些用例本身），返回 (repo, base, patch)。"""
    patch = contextlib.nullcontext()
    if scene == "receipt_deleted":
        repo, base, c1 = _branch_with_one_receipt_and_two_code_commits(root)
        repo.remove(f"docs/gate/{c1}.json", "docs(gate): drop the receipt")
    elif scene == "receipt_tampered":
        repo, base, c1 = _branch_with_one_receipt_and_two_code_commits(root)
        _tamper_receipt(repo, c1)
    elif scene == "receipt_scan_fails":
        repo, base, _c1 = _branch_with_one_receipt_and_two_code_commits(root)
        patch = _scan_failure_patch()
    elif scene == "merge_landed_receipt_deleted":
        repo, base, c1 = _branch_with_a_receipt_landed_in_a_merge(root)
        repo.remove(f"docs/gate/{c1}.json", "docs(gate): drop the merge-landed receipt")
    elif scene == "renamed_receipt_tampered":
        repo, base, c1 = _branch_with_a_receipt_renamed_into_place(root)
        _tamper_receipt(repo, c1)
    elif scene == "zero_receipt_scan_fails":
        repo = Repo(root / "r")
        base = _base_with_origin(repo)
        _two_plain_code_commits(repo)
        patch = _scan_failure_patch()
    else:  # landing_history_read_fails
        repo = Repo(root / "r")
        base = _base_with_origin(repo)
        _two_plain_code_commits(repo)
        patch = _landing_read_failure_patch()
    return repo, base, patch


def test_bootstrap_stays_red_when_a_routine_ledger_row_is_added(tmp_path):
    """Standards P2-1：7 条 `test_bootstrap_stays_red_*` 的现场各追加一条例行台账行 ⇒ 仍判红。

    这一侧正是第一轮实现的缺口：「有 stale 的现场上放行一切」没有任何用例覆盖。台账行用
    `_LEDGER_ROW`（每张票都会做的归属行），不改 7 条既有用例本身（它们钉的是无台账行形态）。
    每个现场各用一个独立 tmp 子目录，失败信息带场景名。
    """
    for scene in ("receipt_deleted", "receipt_tampered", "receipt_scan_fails",
                  "merge_landed_receipt_deleted", "renamed_receipt_tampered",
                  "zero_receipt_scan_fails", "landing_history_read_fails"):
        repo, base, patch = _stays_red_scene(scene, tmp_path / scene)
        repo.commit(_LEDGER_ROW, "docs(ledger): append a review-ledger row")

        with patch:
            check = _bootstrapping_check(repo, base)
        assert check.warn_only is False, f"[{scene}] {check.detail}"
        assert check.ok is False, f"[{scene}] {check.detail}"
        assert check.status == "violations", f"[{scene}] {check.detail}"
