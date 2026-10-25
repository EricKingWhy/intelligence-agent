"""覆盖率基线留痕 + 变更行棘轮（Issue #1499）的守卫。

判据的**唯一实现**是 `scripts/check_line_coverage_ratchet.py`；本文件只钉它对外承诺的语义，
用**真 CLI 子进程 + 真临时 git 仓库**跑（不 mock `git`、不 mock XML：mock 掉这两样，判据就
只能在"我以为的输入形状"上成立）。

覆盖票面 AC 的六条关键语义。带 `mutation:` 说明的用例是"删掉对应守卫就必然变红"的那几条
（每轮修复的变异矩阵读数与实际失败集合记在 PR 描述里，不在本文件里复制数字）：

1. 变更可执行行**恰 60% 通过**、**59% 失败**（整数比较，不经浮点）；
2. **未改行不进分母**（基线里早就未覆盖的行不影响判定）；
3. **白名单行不进分母**，且白名单拒绝通配 / 目录前缀 / 空理由（"整个安全域排除"过不了闸）；
4. **缺失测量 / 旧报告 / git 面读不出**判 `2`（不可判），不假绿：报告不存在、基线不存在、
   报告与基线不是同一份、基线不是当前树的、改了 `src/` 但报告里没有该文件、`git diff` 非零，
   六种都试；
5. **零可执行变更** = 显式空集合通过（打印 `RATCHET EMPTY`，退出 0），不是静默绿；
6. **纯移动**：没有 `--move-only-note` ⇒ 照常计入分母（可能变红）；有说明且 git `-M` 判为
   **`R100`（逐字相同）** ⇒ 跳过；`R<100`（移动 + 修改）照常计入。
   另附白名单/移动的正控与负控，以及 `baseline` 子命令的留痕字段齐全性。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_line_coverage_ratchet.py"

COVERAGE_XML = """<?xml version="1.0" ?>
<coverage version="7.16.2" lines-valid="{valid}" lines-covered="{covered}">
  <sources><source>{source}</source></sources>
  <packages><package name="."><classes>
{classes}
  </classes></package></packages>
</coverage>
"""


def _xml(repo: Path, files: dict[str, list[tuple[int, int]]]) -> str:
    """造一份 coverage.py 形状的 Cobertura XML：`{相对路径: [(行号, 命中数)]}`。"""
    classes = "\n".join(
        f"<class name={quoteattr(Path(rel).name)} filename={quoteattr(rel)}>"
        + "".join(f'<line number="{n}" hits="{h}"/>' for n, h in lines)
        + "</class>"
        for rel, lines in files.items()
    )
    covered = sum(1 for lines in files.values() for _, hits in lines if hits > 0)
    valid = sum(len(lines) for lines in files.values())
    return COVERAGE_XML.format(valid=valid, covered=covered,
                               source=(repo / "src").as_posix(), classes=classes)


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(repo), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          env=env, check=True)
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """一个真 git 仓库：`src/pkg/mod.py` 为 20 行的基线文件，main 上一笔提交。"""
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "docs" / "agents").mkdir(parents=True)
    _git(root, "init", "--quiet", "-b", "main")
    (root / "src" / "pkg" / "mod.py").write_text(
        "".join(f"line{i} = {i}\n" for i in range(1, 21)), encoding="utf-8")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "base")
    _git(root, "branch", "baseline")
    return root


def _write_report(root: Path, files: dict[str, list[tuple[int, int]]], name: str = "coverage.xml") -> Path:
    path = root / "artifacts" / name
    path.parent.mkdir(exist_ok=True)
    path.parent.joinpath(name).write_text(_xml(root, files), encoding="utf-8")
    return path


def _append_lines(root: Path, count: int, start: int = 21) -> None:
    """在 `src/pkg/mod.py` 末尾追加 `count` 行（行号 start 起）。"""
    path = root / "src" / "pkg" / "mod.py"
    path.write_text(path.read_text(encoding="utf-8")
                    + "".join(f"added{i} = {i}\n" for i in range(start, start + count)),
                    encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", f"add {count} lines")


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, COVERAGE_RATCHET_REPO=str(root))
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=root, capture_output=True,
                          text=True, env=env, check=False)


def _baseline(root: Path, report: Path) -> Path:
    out = root / "artifacts" / "baseline.json"
    proc = _run(root, "baseline", "--report", str(report), "--out", str(out),
                "--scope", "tests + --cov=src/agent_harness")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return out


def _ratchet(root: Path, report: Path, baseline: Path, *extra: str) -> subprocess.CompletedProcess:
    return _run(root, "ratchet", "--report", str(report), "--baseline", str(baseline),
                "--base", "baseline", *extra)


def _mod_lines(count: int, hits: int, start: int = 21) -> list[tuple[int, int]]:
    return [(line, 1 if index < hits else 0) for index, line in enumerate(range(start, start + count))]


# ── AC2 ①：恰好 60% 通过、59% 失败（整数比较） ──────────────────────────────


def test_exactly_60_percent_passes(repo: Path) -> None:
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 3)})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "变更可执行行：3/5 = 60.00%" in proc.stdout
    assert proc.stdout.rstrip().endswith("RATCHET PASS")


def test_59_percent_fails(repo: Path) -> None:
    """mutation: 把判定改成 `>` 或改成浮点四舍五入，4/7=57.14 与 3/5=60 的边界会一起漂。"""
    _append_lines(repo, 7)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(7, 4)})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "4/7 = 57.14%" in proc.stdout
    assert proc.stdout.rstrip().endswith("RATCHET FAIL")


def test_sub_one_percent_rounding_does_not_flip_the_verdict(repo: Path) -> None:
    """59.5% 这类"四舍五入看着像 60"的值必须失败：判据是整数乘法，不是显示值。"""
    _append_lines(repo, 200)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(200, 119)})  # 59.5%
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "119/200 = 59.50%" in proc.stdout


# ── AC2 ②：未改行不进分母 ──────────────────────────────────────────────────


def test_unchanged_uncovered_lines_stay_out_of_the_denominator(repo: Path) -> None:
    """基线的 20 行**一行都没被测到**；本次只加 4 行且全测到 ⇒ 仍 100%，4/4 而不是 4/24。"""
    _append_lines(repo, 4)
    files = {"src/pkg/mod.py": [(n, 0) for n in range(1, 21)] + _mod_lines(4, 4)}
    report = _write_report(repo, files)
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "变更可执行行：4/4 = 100.00%" in proc.stdout
    # 全仓总率只记录、不判定：它照样把 24 行里只覆盖 4 行这件事打印出来。
    assert "全仓总率（只记录、不判定）：4/24" in proc.stdout


def test_non_executable_added_lines_are_excluded(repo: Path) -> None:
    """新增行里不是可执行行的（注释 / 空行 —— 报告里根本没有该行号）不进分母。"""
    _append_lines(repo, 3)
    # 报告里只有第 21 行是可执行的，22/23 不可执行 ⇒ 分母 = 1。
    report = _write_report(repo, {"src/pkg/mod.py": [(21, 1)]})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "变更可执行行：1/1 = 100.00%" in proc.stdout


# ── AC3 ②：白名单不进分母；不许整域排除 ────────────────────────────────────


def _whitelist(repo: Path, entries: list[dict]) -> Path:
    path = repo / "docs" / "agents" / "coverage-ratchet.whitelist.json"
    path.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")
    return path


def test_whitelisted_file_changed_lines_leave_the_denominator(repo: Path) -> None:
    """mutation: 把白名单命中改成"进分母但算命中"，本用例的红转绿。"""
    (repo / "src" / "pkg" / "win.py").write_text("".join(f"w{i} = {i}\n" for i in range(1, 11)),
                                                 encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add win module")
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 3),
                                  "src/pkg/win.py": [(n, 0) for n in range(1, 11)]})
    _whitelist(repo, [{"path": "src/pkg/win.py", "reason": "Win32 only", "platform": "windows"}])
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    # 5/5 而不是 3/15：白名单文件的 10 行被整段拿掉，且理由/平台逐条打印。
    assert "变更可执行行：3/5 = 60.00%" in proc.stdout
    assert "src/pkg/win.py（10 行；platform=windows；理由=Win32 only）" in proc.stdout


@pytest.mark.parametrize("bad", [
    {"path": "src/agent_harness/security/", "reason": "整域排除", "platform": "all"},
    {"path": "src/pkg/*.py", "reason": "通配", "platform": "all"},
    {"path": "src/pkg/win.py", "reason": "  ", "platform": "windows"},
    {"path": "src/pkg/win.py", "reason": "Win32 only"},
])
def test_whitelist_rejects_directory_glob_and_empty_fields(repo: Path, bad: dict) -> None:
    """AC3 的"不把整个安全域排除来过闸"必须是**解析期拒绝**，不是文档约定。"""
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 5)})
    _whitelist(repo, [bad])
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "白名单不合法" in proc.stdout


# ── AC1：缺失测量 / 旧报告 ⇒ 不可判（不许假绿） ────────────────────────────


def test_missing_report_is_not_decidable(repo: Path) -> None:
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 5)})
    base = _baseline(repo, report)
    report.unlink()

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "棘轮不可判" in proc.stdout and "找不到覆盖率报告" in proc.stdout


def test_missing_baseline_is_not_decidable(repo: Path) -> None:
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 5)})

    proc = _ratchet(repo, report, repo / "artifacts" / "nope.json")

    assert proc.returncode == 2
    assert "找不到基线记录" in proc.stdout


def test_stale_report_from_another_run_is_not_decidable(repo: Path) -> None:
    """mutation: 去掉 sha256 对账 ⇒ 旧报告（同路径、不同内容）就会被当成这次的测量。"""
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 3)})
    base = _baseline(repo, report)
    # 换了内容但路径不变：正是"读取旧报告假绿"的形状。
    _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 5)})

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "报告与基线不是同一份文件" in proc.stdout


def test_baseline_from_another_tree_is_not_decidable(repo: Path) -> None:
    """基线记的是另一棵树 ⇒ 这份测量不对应当前代码，判定无依据。

    mutation: 去掉 tree 判据 ⇒ 本用例转绿（此时报告的 sha256 依然对得上，只有树能拦住它）。
    """
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 5)})
    base = _baseline(repo, report)
    # 只改一个**不参与判定**的文件：报告内容因此仍然"看起来对"，但树已经不是被测量那棵。
    (repo / "README.md").write_text("hi\nchanged\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "advance tree")

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "≠ 当前" in proc.stdout


def test_empty_commit_does_not_invalidate_a_baseline(repo: Path) -> None:
    """树没变 ⇒ 读数可传递（协议 §8.1）：只有 docs 提交 / 空提交不该把判定打成不可判。"""
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 5)})
    base = _baseline(repo, report)
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", "empty")

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "变更可执行行：5/5" in proc.stdout


def test_changed_file_missing_from_report_is_not_decidable(repo: Path) -> None:
    """改了 `src/` 下**测量面之外**的文件（`--cov=` 目标没覆盖到的那棵子树）⇒ 不可判。

    形状刻意做成"报告就在这次 HEAD 的树上产出的"（基线先记、再改/提交），于是它先过了
    sha256 与树两道前置 —— 只剩"这个文件没被测量"这一条 ⇒ 不许当成"没有可执行行"放行。
    """
    (repo / "src" / "outside").mkdir()
    (repo / "src" / "outside" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    report = _write_report(repo, {"src/pkg/mod.py": [(n, 1) for n in range(1, 21)]})
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add outside module")
    base = _baseline(repo, report)
    (repo / "src" / "outside" / "thing.py").write_text("x = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "touch outside module")
    # 报告在改动后的树上重产（内容不变、路径不变）⇒ sha256 与树都对得上。
    report = _write_report(repo, {"src/pkg/mod.py": [(n, 1) for n in range(1, 21)]})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "src/outside/thing.py" in proc.stdout


# ── AC2 ④：零可执行变更的显式空集合语义 ───────────────────────────────────


def test_docs_only_change_is_an_explicit_empty_pass(repo: Path) -> None:
    (repo / "README.md").write_text("hi\nmore\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "docs")
    report = _write_report(repo, {"src/pkg/mod.py": [(n, 1) for n in range(1, 21)]})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RATCHET EMPTY" in proc.stdout


def test_comment_only_change_inside_src_is_also_empty(repo: Path) -> None:
    """`src/` 里只加注释：行号变了但报告里这些行不是可执行行 ⇒ 分母为空、显式打印。"""
    path = repo / "src" / "pkg" / "mod.py"
    path.write_text("# a comment\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "comment")
    report = _write_report(repo, {"src/pkg/mod.py": [(n, 1) for n in range(2, 22)]})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RATCHET EMPTY" in proc.stdout


# ── AC3 ①：纯移动重构仅在显式说明后跳过 ────────────────────────────────────


def _move_module(repo: Path) -> None:
    """文件先落在基线里、再在分支上移动 —— 这样 `diff base...HEAD` 才看得见 rename。

    （先把文件放到基线分支上：`git -M` 判 rename 要求**同一对路径**在 diff 两侧都在，
    否则它只是"加了一个新文件"。）
    """
    (repo / "src" / "pkg" / "moved.py").write_text(
        "".join(f"m{i} = {i}\n" for i in range(1, 11)), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add module to move")
    _git(repo, "branch", "-f", "baseline", "HEAD")
    _git(repo, "mv", "src/pkg/moved.py", "src/pkg/moved2.py")
    _git(repo, "commit", "--quiet", "-m", "move module")


def test_move_without_note_is_counted_and_can_fail(repo: Path) -> None:
    """没有显式说明 ⇒ 移动按「新增 10 行、0 命中」计入分母 ⇒ 失败。禁止静默跳过。"""
    _move_module(repo)
    report = _write_report(repo, {"src/pkg/moved2.py": [(n, 0) for n in range(1, 11)]})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "变更可执行行：0/10 = 0.00%" in proc.stdout


def test_move_with_note_is_skipped_when_git_agrees_it_is_a_rename(repo: Path) -> None:
    _move_module(repo)
    report = _write_report(repo, {"src/pkg/moved2.py": [(n, 0) for n in range(1, 11)]})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base, "--move-only-note", "纯移动：moved.py → moved2.py")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "纯移动跳过 1 个文件" in proc.stdout
    assert "RATCHET EMPTY" in proc.stdout


def test_move_note_does_not_excuse_a_non_rename(repo: Path) -> None:
    """说明是"声明"，git 的 `-M` 是"事实"：不是 rename 的文件，贴了说明也照常计入分母。"""
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 0)})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base, "--move-only-note", "谎称纯移动")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "git -M 没判出任何 R100" in proc.stdout
    assert "变更可执行行：0/5 = 0.00%" in proc.stdout


# ── AC1：baseline 子命令的留痕字段 ─────────────────────────────────────────


def test_baseline_records_sha_tree_platform_scope_exclusions_and_report(repo: Path) -> None:
    report = _write_report(repo, {"src/pkg/mod.py": [(n, 1) for n in range(1, 21)]})
    out = _baseline(repo, report)

    record = json.loads(out.read_text(encoding="utf-8"))
    head = _git(repo, "rev-parse", "HEAD")
    assert record["schema"] == "coverage-baseline/1"
    assert record["sha"] == head
    assert record["tree"] == _git(repo, "rev-parse", f"{head}^{{tree}}")
    assert record["platform"] and record["python"]
    assert record["measurement_scope"] == "tests + --cov=src/agent_harness"
    assert record["exclusions"]["whitelist"].endswith("coverage-ratchet.whitelist.json")
    # 拿**盘上文件**独立算一遍：把自己和自己比是恒真断言，钉不住任何东西。
    assert record["report"] == {"path": str(report),
                                "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
    # 全仓总率只落在 `totals_recorded_only`：字段名本身就是"不判定"的承诺。
    assert record["totals_recorded_only"]["lines_valid"] == 20


def test_baseline_refuses_when_the_report_is_missing(repo: Path) -> None:
    proc = _run(repo, "baseline", "--report", str(repo / "artifacts" / "nope.xml"),
                "--out", str(repo / "artifacts" / "b.json"), "--scope", "x")

    assert proc.returncode == 2
    assert "找不到覆盖率报告" in proc.stdout


# ── 复核修复轮新增的守卫（每条都对应一个真实漏口，见 executor-notes 的 findings 表） ──


def test_whitelist_excuses_a_file_absent_from_the_report(repo: Path) -> None:
    """**白名单的主用途**：容器内 / 平台专有的文件在报告里根本没有 ⇒ 仍要能显式登记后放行。

    mutation: 把白名单判定移回 `unmeasured` 之后 ⇒ 本用例退回 2（正是修前的漏口）。
    """
    (repo / "src" / "pkg" / "win.py").write_text("".join(f"w{i} = {i}\n" for i in range(1, 11)),
                                                 encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add win module")
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 5)})
    _whitelist(repo, [{"path": "src/pkg/win.py", "reason": "Win32 only", "platform": "windows"}])
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "变更可执行行：5/5 = 100.00%" in proc.stdout
    assert "src/pkg/win.py（报告里没有该文件（盲区）；platform=windows；理由=Win32 only）" in proc.stdout


def test_whitelist_absent_from_report_still_needs_reason_and_platform(repo: Path) -> None:
    """放行盲区的前提仍是"精确文件 + 理由 + 平台"：不能让这条通道变成不写理由的豁免。"""
    (repo / "src" / "pkg" / "win.py").write_text("w = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add win module")
    report = _write_report(repo, {"src/pkg/mod.py": [(n, 1) for n in range(1, 21)]})
    _whitelist(repo, [{"path": "src/pkg/win.py", "reason": "Win32 only"}])
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "白名单不合法" in proc.stdout


def test_move_and_modify_is_not_excused_by_the_note(repo: Path) -> None:
    """`R077`（移动 + 新加代码）不是"纯移动"：新增行必须照常进分母。

    mutation: 把 `renamed_paths` 放宽成 `status.startswith("R")` ⇒ 13 行新增被整片豁免 ⇒ 转绿。
    """
    (repo / "src" / "pkg" / "moved.py").write_text(
        "".join(f"m{i} = {i}\n" for i in range(1, 11)), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add module to move")
    _git(repo, "branch", "-f", "baseline", "HEAD")
    _git(repo, "mv", "src/pkg/moved.py", "src/pkg/moved2.py")
    (repo / "src" / "pkg" / "moved2.py").write_text(
        (repo / "src" / "pkg" / "moved2.py").read_text(encoding="utf-8")
        + "".join(f"n{i} = {i}\n" for i in range(1, 4)), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "move and add 3 lines")
    report = _write_report(repo, {"src/pkg/moved2.py": [(n, 0) for n in range(1, 14)]})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base, "--move-only-note", "谎称纯移动：moved.py → moved2.py")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "变更可执行行：0/13 = 0.00%" in proc.stdout
    assert "git -M 没判出任何 R100" in proc.stdout


def test_undecidable_git_diff_does_not_become_an_empty_pass(repo: Path) -> None:
    """`--base` 与 HEAD 没有共同祖先（`git diff` rc=128）⇒ 退 2，不许洗成 RATCHET EMPTY。

    mutation: 让 `changed_lines` 在 rc≠0 时 `return {}` ⇒ 本用例变 0 绿（修前的形状）。
    """
    report = _write_report(repo, {"src/pkg/mod.py": [(n, 1) for n in range(1, 21)]})
    _git(repo, "checkout", "--quiet", "--orphan", "orphan")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "orphan root")
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "棘轮不可判" in proc.stdout
    assert "RATCHET EMPTY" not in proc.stdout


def test_non_python_file_under_src_is_not_a_denominator_and_not_undecidable(repo: Path) -> None:
    """`src/` 下的快照/数据文件没有"可执行行"：它既不该进分母，也不该把棘轮打成不可判。

    mutation: 扫描面回到裸 `startswith("src/")` ⇒ 本用例退回 2（修前的形状）。
    """
    snapshot = repo / "src" / "pkg" / "types.snapshot.txt"
    snapshot.write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add snapshot")
    snapshot.write_text("a\nb\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "change snapshot")
    report = _write_report(repo, {"src/pkg/mod.py": [(n, 1) for n in range(1, 21)]})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RATCHET EMPTY" in proc.stdout


def test_added_line_beginning_with_two_pluses_is_not_a_file_header(repo: Path) -> None:
    """内容 `++ x` 的新增行在 patch 里长成 `+++ x`，与文件头一字不差 —— 不许吃掉后续 hunk。

    形状必须**跨两个 hunk**：假文件头出现在第一个 hunk 之后，第二个 hunk 的新增行才有机会被吃掉。
    """
    path = repo / "src" / "pkg" / "mod.py"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = "++ x = 2"  # 第 2 行改成以 `++ ` 开头
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "plus content")
    _append_lines(repo, 3)  # 第二个 hunk：纯新增 3 行
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(3, 0)})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "变更可执行行：0/3 = 0.00%" in proc.stdout


def test_deleted_line_beginning_with_two_dashes_is_not_a_file_header(repo: Path) -> None:
    """内容 `-- x` 的**删除**行在 patch 里长成 `--- x`，同样会冒充文件头。

    这条是复核指出的**修后新引入**的假绿：删掉一行 `-- x` 之后，后续 hunk 的新增行全被吃掉
    ⇒ 分母归零 ⇒ 输出 `RATCHET EMPTY` 退 0，而正确结论是 FAIL 退 1。
    """
    path = repo / "src" / "pkg" / "mod.py"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = "-- 这是内容，不是文件头"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add sql-ish comment line")
    _git(repo, "branch", "-f", "baseline", "HEAD")
    # 再删掉那一行，并追加 2 行未覆盖代码
    kept = [line for i, line in enumerate(lines) if i != 1]
    path.write_text("\n".join(kept) + "\n" + "new1 = 1\nnew2 = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "delete dash line and add 2")
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(2, 0)})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    # 关键不是具体计数，而是**新增的那个 hunk 没被吃掉**：分母非空、且结论是 FAIL 不是 EMPTY。
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "RATCHET EMPTY" not in proc.stdout
    assert "变更可执行行：0/1 = 0.00%" in proc.stdout


def test_path_with_a_quote_is_unquoted_correctly(repo: Path) -> None:
    """含 `"` 的路径即使 `core.quotePath=false` 也会被 git 加引号 + 反斜杠转义。

    mutation: 让 `_unquote` 直通 ⇒ 键变成 `"b/src/we\"ird.py"`，与报告对不上 ⇒ 判"未测量"退 2。
    """
    weird = repo / "src" / "pkg" / 'we"ird.py'
    weird.write_text("".join(f"q{i} = {i}\n" for i in range(1, 6)), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add quoted-path module")
    report = _write_report(repo, {'src/pkg/we"ird.py': _mod_lines(5, 5, start=1)})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "变更可执行行：5/5 = 100.00%" in proc.stdout


def test_non_ascii_and_space_paths_are_matched(repo: Path) -> None:
    """git 对特殊路径会加引号；棘轮要能把它还原成仓库相对路径（否则整份报告落错键）。"""
    weird = repo / "src" / "pkg" / "中文 模块.py"
    weird.write_text("".join(f"z{i} = {i}\n" for i in range(1, 11)), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "add non-ascii module")
    report = _write_report(repo, {"src/pkg/中文 模块.py": _mod_lines(10, 10, start=1)})
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    # 匹配不上时该文件会落进"未测量"或干脆不出现 ⇒ 断言必须钉住"它被认出来了且进了分母"。
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "变更可执行行：10/10 = 100.00%" in proc.stdout


def test_report_from_another_checkout_is_counted_and_warned(repo: Path) -> None:
    """`<source>` 全在仓库外（报告来自别的 checkout）⇒ 每个文件都被计数并打印告警。

    静默丢弃会让"报告不是这棵树的"看不见：文件会凭空消失，剩下的行照样算出个比率。
    """
    _append_lines(repo, 5)
    report = repo / "artifacts" / "coverage.xml"
    report.parent.mkdir(exist_ok=True)
    report.write_text(
        '<coverage version="7.16.2" lines-valid="5" lines-covered="5">'
        "<sources><source>/elsewhere/checkout/src</source></sources>"
        '<packages><package name="."><classes>'
        '<class name="mod.py" filename="pkg/mod.py">'
        + "".join(f'<line number="{n}" hits="1"/>'
                  for n in range(21, 26))
        + "</class></classes></package></packages></coverage>",
        encoding="utf-8")
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    # 报告认不出这个文件 ⇒ 判"未被测量"（不是"没有可执行行"），且外部源要被计数。
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "1 个文件的 <source> 不在本仓库内" in proc.stdout
    assert "src/pkg/mod.py" in proc.stdout


@pytest.mark.parametrize("bad_path", [
    "./src/pkg/win.py",
    " src/pkg/win.py",
    "/src/pkg/win.py",
])
def test_whitelist_rejects_non_canonical_paths(repo: Path, bad_path: str) -> None:
    """写进去却永远匹配不上的条目等于失效声明 ⇒ 解析期拒绝（而不是静默无关）。"""
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 5)})
    _whitelist(repo, [{"path": bad_path, "reason": "r", "platform": "linux"}])
    base = _baseline(repo, report)

    proc = _ratchet(repo, report, base)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "规范化的仓库相对路径" in proc.stdout


def test_malformed_baseline_fields_are_undecidable_not_a_traceback(repo: Path) -> None:
    """基线满足 schema 但缺 `sha` ⇒ 判不可判（退 2），不是 KeyError 崩栈。"""
    _append_lines(repo, 5)
    report = _write_report(repo, {"src/pkg/mod.py": _mod_lines(5, 5)})
    out = repo / "artifacts" / "baseline.json"
    out.write_text(json.dumps({"schema": "coverage-baseline/1",
                               "report": {"path": str(report),
                                          "sha256": __import__("hashlib")
                                          .sha256(report.read_bytes()).hexdigest()}}),
                   encoding="utf-8")

    proc = _ratchet(repo, report, out)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr and "Traceback" not in proc.stdout
    assert "基线记录缺 sha" in proc.stdout
