"""`scripts/check_exec_bit.py` 的守卫（P0-2：索引可执行位 ⇔ shebang）。

## 它守什么

Gate-0 的服务端跑在 Unix，`ruff` 的 `EXE001`（**Shebang is present but file is not executable**）
只在那里生效；本机 Windows 上 `core.filemode=false` ⇒ **工作树权限的改动 git 根本不记录**，
于是本地 `ruff check .` **永远看不见**这一条。两次真实事故都是它的产物（「本地绿 → CI 红」往返）：

- `99a744fe` —— 在 `main` 上补 7 个脚本的可执行位；
- `2d3761c` —— `scripts/live_gate.py` 索引 `100644` → `100755`（该笔另复跑了一次 Gate-0）。

`check_exec_bit.py` 把那条上游规则搬到**本地可跑**的形态上：读**索引模式**与**blob 头两字节**，
两者都不受 `core.filemode` / `core.autocrlf` / 本地未提交编辑影响。本文件守它自己不跑偏。

## 三层控制样本（缺一层就有假绿的缝）

1. **纯函数正控 / 反控 / 越界控 / 缺读控**（下面 A 组）：判据本体是纯函数，控制样本可直接构造。
   - 正控：blob 以 `#!` 开头 + 索引 `100644` ⇒ 必须判违例；
   - 反控：`#!`+`100755` / 无 `#!`+`100644` / 无 `#!`+`100755` ⇒ 一条都不许报
     （一个 `return []` 的实现过得了反控、过不了正控；一个恒报的实现反过来 —— 两个都要有）；
   - 缺读控：blob 头取不到 ⇒ 必须**判违例**（fail-closed），不是"跳过"。
2. **端到端真仓库控**（B 组）：在真 git 仓库里造出事故形状、跑 CLI，断言**只抓坏的那个**。
   纯函数控证明不了 git 那半截（`ls-files -s` 的模式列、`cat-file --batch` 的字节对齐）——
   只有真跑才证明"读索引模式"这条链路通。
3. **反空转控**（B 组）：断言本仓**确实存在**带 shebang 的 Python 文件、也确实存在 `100755` 的。
   少了它，"0 违例"可能只是"一个都没检查"的假绿。

## 它**不**守什么

- **反方向**（`100755` 而无 shebang）：本仓 0 例、上游口径未核对 ⇒ 刻意不纳入（见脚本 docstring）。
- **`.sh` / `.ps1` / `.mjs`**：`ruff` 不 lint 它们，纳入会立刻产生 11 条本仓现状无害的误报
  （`dev.sh`、`docs/integration/verify-before-merge.sh` 等）⇒ 刻意排除（见脚本 docstring）。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO / "scripts" / "check_exec_bit.py"
GATE0_PATH = REPO / "scripts" / "gate0.py"

#: 控制样本用的 sha（`read_blob_heads` 的键）。值本身无意义，只需要是**稳定且互不相同**的字符串，
#: 因为纯函数只按 `heads[sha]` 取头两字节。
SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40

SHEBANG = b"#!"
NO_SHEBANG = b"im"      # `import x` 的头两字节


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"加载不了 {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    assert SCRIPT_PATH.is_file(), f"判据脚本不存在：{SCRIPT_PATH}"
    return _load(SCRIPT_PATH, "_exec_bit_under_test")


def _maybe_gate0():
    """按需加载 gate0（只在接线那一组用，省一次模块加载）。"""
    return _load(GATE0_PATH, "_gate0_under_test")


# =========================================================================== #
# A. 纯函数判据本体（正控 / 反控 / 越界控 / 缺读控）
# =========================================================================== #

def test_positive_control_shebang_without_exec_bit_is_flagged(mod):
    """**正控**：事故形状（blob 以 `#!` 开头、索引 `100644`）⇒ 必须判违例，且报的是"不可执行"那条。"""
    problems = mod.find_violations([("scripts/x.py", "100644", SHA_A)], {SHA_A: SHEBANG})
    assert problems == [("scripts/x.py", "100644", mod.REASON_NOT_EXECUTABLE)], problems


def test_negative_controls_report_nothing(mod):
    """**反控**：三种正常形态一条都不许报 —— 恒报的假实现过不了这一条。"""
    entries = [
        ("exec.py", "100755", SHA_A),            # 带 shebang 且已可执行
        ("plain.py", "100644", SHA_B),           # 普通模块（无 shebang、不可执行）
        ("exec_no_shebang.py", "100755", SHA_C),  # 可执行但无 shebang（反方向，刻意不判）
    ]
    heads = {SHA_A: SHEBANG, SHA_B: NO_SHEBANG, SHA_C: NO_SHEBANG}
    assert mod.find_violations(entries, heads) == []


def test_fail_closed_when_the_blob_head_is_unreadable(mod):
    """**缺读控**：blob 头取不到 ⇒ 必须**判违例**（"核对不了就不放行"），不是跳过。

    两种缺失都要覆盖 —— 显式 `None`（读失败已登记）与**键完全不在** `heads` 里
    （`--batch` 输出提前结束那一支）。后者若被写成"默认通过"，就是一条静默放行。
    """
    entries = [("a.py", "100644", SHA_A), ("b.py", "100644", SHA_B)]
    problems = mod.find_violations(entries, {SHA_A: None})
    assert {p[0] for p in problems} == {"a.py", "b.py"}, problems
    assert {p[2] for p in problems} == {mod.REASON_UNREADABLE}, problems
    # 缺读的成因与「带 shebang 但不可执行」是两回事 ⇒ 两个文案必须不同（否则输出会误导修法）。
    assert mod.REASON_UNREADABLE != mod.REASON_NOT_EXECUTABLE


def test_scope_is_python_only(mod):
    """**越界控**：判据只覆盖 `ruff` 会 lint 的 `*.py` / `*.pyi`。

    `.sh` / `.ps1` / `.mjs` 带 shebang 而模式 `100644` 是本仓**现状且无害**的形状
    （它们一律经解释器调用）⇒ 纳入会立刻产生 11 条误报，把真命中一起训练成噪音。
    """
    entries = [
        ("dev.sh", "100644", SHA_A),
        ("scripts/p.ps1", "100644", SHA_B),
        ("web/scripts/preflight-port.mjs", "100644", SHA_C),
    ]
    heads = {s: SHEBANG for s in (SHA_A, SHA_B, SHA_C)}
    assert mod.find_violations(entries, heads) == [], "非 Python 后缀不在 ruff 口径内，不得报"
    # 但 `*.pyi` 在口径内（它同样是 ruff 的输入面）。
    assert mod.find_violations([("stubs/x.pyi", "100644", SHA_A)], {SHA_A: SHEBANG}), \
        "*.pyi 属 ruff 输入面，带 shebang 而不执行同样要报"
    # 边界：`#!` 必须**在字节 0**。头两字节不是 `#!` 的一律不算（比如 UTF-8 BOM 开头的文件）。
    assert mod.find_violations([("bom.py", "100644", SHA_A)], {SHA_A: b"\xef\xbb"}) == []


def test_exec_mode_constant_is_the_index_bit(mod):
    """钉住判据用到的那两个常量：模式位是 `100755`、shebang 前缀是 `#!`。

    常量若被"顺手"改成 `100644`（或把前缀放宽成 `#`），护栏就整条失效，而输出看起来一切正常。
    """
    assert mod.EXEC_MODE == "100755"
    assert mod.SHEBANG == b"#!"
    assert mod.PY_SUFFIXES == (".py", ".pyi")
    # 修法必须指向**索引**：关掉 CI 那条 ruff 规则正是本票要消灭的假绿灯形态。
    assert "git update-index --chmod=+x" in mod.FIX_HINT, mod.FIX_HINT


# =========================================================================== #
# B. 真跑：本仓（反空转）与临时仓库（端到端）
# =========================================================================== #

@pytest.fixture(scope="module")
def index_snapshot(mod) -> tuple[list[tuple[str, str, str]], dict[str, bytes | None]]:
    """本仓索引快照 `(entries, heads)` —— 下面三个用例共用一次 git 走查。

    刻意走**判据自己的**读取函数（`read_index_entries` / `read_blob_heads`）：它同时是"git 那半截
    链路通不通"的证据（另起一份实现来读就证明不了这一点）。`core.filemode=false` 那类坑全在这半截。
    """
    entries = mod.read_index_entries(str(REPO))
    heads = mod.read_blob_heads([sha for _, _, sha in entries], str(REPO))
    return entries, heads


def test_repo_is_green_and_the_scan_is_not_vacuous(mod, index_snapshot):
    """本仓必须**绿**，且自报的文件数必须等于**独立重读**的索引条目数（两侧不得各说各话）。

    只断言"通过了"是不够的：一个"扫到 0 个文件"的实现同样报通过。所以这里钉的是**计数相等**。

    ⚠ 本条走**进程内** `main()`、不走子进程：CLI 的可执行性（`__main__` + `sys.argv` 解析 + 退出码）
    由下面那条临时仓库用例以**真子进程**证明。本条每次 `guards` 车道都要跑，不该为"证明解释器
    起得来"多付一次解释器启动钱（本机实测 venv 冷启动 **1.55s**，那是本文件最大的单项成本）。
    """
    entries, _heads = index_snapshot
    out_buf, err_buf = io.StringIO(), io.StringIO()
    # 两个流都换成 `StringIO`：`main()` 开头的 `_utf8_stdio()` 便无事可做（StringIO 没有
    # `reconfigure`），不会去动 pytest 自己的捕获对象。
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = mod.main(["--repo", str(REPO)])
    out, err = out_buf.getvalue(), err_buf.getvalue()
    assert rc == 0, f"本仓应无违例：\n{out}\n{err}"
    assert len(entries) > 400, f"索引里的 Python 文件只有 {len(entries)} 个？"
    assert f"{len(entries)} 个 Python 文件" in out, (
        f"自报的文件数与独立重读不符（差一个就说明有条路没扫）：\n{out}")


def test_repo_actually_exercises_both_sides_of_the_rule(mod, index_snapshot):
    """**反空转**：本仓必须真的**有**带 shebang 的 Python 文件、也真的**有** `100755` 的。

    若两边任一为空，"本仓绿"就只是"没什么可检查"—— 那与"检查通过"必须区分开。
    最后一条同时就是**被守卫的性质本身**（带 shebang ⇒ 必须可执行），是真实的端到端断言。
    """
    entries, heads = index_snapshot
    shebang_files = [p for p, _mode, sha in entries if heads.get(sha) == SHEBANG]
    exec_files = [p for p, mode, _sha in entries if mode == mod.EXEC_MODE]
    assert shebang_files, "本仓没有任何带 shebang 的 Python 文件 ⇒ 这条检查是空转的"
    assert exec_files, "本仓没有任何索引模式为 100755 的 Python 文件 ⇒ 模式列取不到真值"
    assert set(shebang_files) <= set(exec_files), (
        f"带 shebang 却不可执行的 Python 文件：{sorted(set(shebang_files) - set(exec_files))}")


def test_read_blob_heads_marks_a_missing_object_as_unreadable(mod, index_snapshot):
    """**解析器对齐控**：`cat-file --batch` 里一条 `missing` 不得把后续 blob 的头读串位。

    这一条是 A 组那个纯函数缺读控**证明不了**的部分 —— 它吃的是 git 的真实输出流：
    缺失对象回的是 `<sha> missing`（两个字段、没有 size），若实现按 size 步进就会整体错位。
    """
    entries, heads = index_snapshot
    assert entries, "本仓索引里应有 Python 文件"
    real_sha = entries[0][2]
    missing_sha = "deadbeef" * 5      # 40 位 hex，但本仓必然不存在
    mixed = mod.read_blob_heads([real_sha, missing_sha], str(REPO))
    assert mixed[missing_sha] is None, f"缺失对象必须记 None，实得 {mixed[missing_sha]!r}"
    assert mixed[real_sha] == heads[real_sha], (
        "missing 行后面那个 blob 的头读串位了 —— 两次独立读取必须给出同一个头")
    assert len(heads[real_sha] or b"") == 2, f"头必须是 2 字节，实得 {heads[real_sha]!r}"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", check=False)
    assert proc.returncode == 0, f"git {' '.join(args)} 失败：{proc.stderr}"
    return proc.stdout


def test_cli_flags_the_incident_shape_in_a_real_repo(tmp_path):
    """**端到端控制样本**：在真仓库里造出 `99a744fe` / `2d3761c` 的形状 ⇒ CLI 只抓坏的那个。

    四种文件同场竞争（这才是"有鉴别力"的形状）：
      · `bad.py`   —— 带 shebang + `100644` ⇒ **必须被报**（事故原样）；
      · `good.py`  —— 带 shebang + `100755` ⇒ 不得报；
      · `plain.py` —— 无 shebang + `100644` ⇒ 不得报；
      · `tool.sh`  —— 带 shebang + `100644`，但在**.sh 越界面** ⇒ 不得报。

    所以本测试的判据是"报的集合**恰好等于** `{bad.py}`"，而不是"rc 非零"。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", ".")
    (repo / "bad.py").write_text("#!/usr/bin/env python3\nprint(1)\n", encoding="utf-8")
    (repo / "good.py").write_text("#!/usr/bin/env python3\nprint(2)\n", encoding="utf-8")
    (repo / "plain.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "tool.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--chmod=+x", "good.py")
    # ⚠ 前置探针（一次 `ls-files -s` 同时钉两件事，省一次进程）：
    #   ① `bad.py` 仍是 `100644` —— 证明"事故形状"确实是 `git add` 的**默认**产物（本机就能复现），
    #      而不是需要什么特殊环境才凑得出来的东西；
    #   ② `good.py` 已被 `update-index --chmod=+x` 写成 `100755` —— 若这一步在本机不生效，
    #      它也会被判违例，本控制样本就退化成"两个都该报"（没有鉴别力）。
    modes = {l.split("\t")[1]: l.split()[0]
             for l in _git(repo, "ls-files", "-s").splitlines() if l}
    assert modes["bad.py"] == "100644", f"`git add` 未给出 100644：{modes}"
    assert modes["good.py"] == "100755", (
        f"`update-index --chmod=+x` 没生效 ⇒ 本控制样本的前提不成立：{modes}")

    proc = subprocess.run([sys.executable, str(SCRIPT_PATH), "--repo", str(repo)],
                          capture_output=True, text=True, encoding="utf-8", check=False)
    assert proc.returncode == 1, f"事故形状必须判违例：\n{proc.stdout}\n{proc.stderr}"
    assert "bad.py" in proc.stdout, proc.stdout
    for ok in ("good.py", "plain.py", "tool.sh"):
        assert ok not in proc.stdout, f"{ok} 不该被报（误报）：\n{proc.stdout}"
    # 计数只算 Python（3 个）—— `.sh` 不进口径，否则 11 条误报会从这里长出来。
    assert "3 个 Python 文件" in proc.stdout, proc.stdout


# =========================================================================== #
# C. 与 Gate-0 的接线（不然本守卫只是个没人执行的装饰）
# =========================================================================== #

def test_gate0_guards_lane_runs_this_guard():
    """守卫必须被 Gate-0 的 `guards` 车道真的跑起来，且写进 `GUARD_TESTS` 的路径都在盘上。"""
    g0 = _maybe_gate0()
    name = "tests/" + Path(__file__).name
    assert name in g0.GUARD_TESTS, f"GUARD_TESTS 缺少 {name}：{g0.GUARD_TESTS}"
    for target in g0.GUARD_TESTS:
        assert (REPO / target).is_file(), f"GUARD_TESTS 指向不存在的文件：{target}"
    guards = [lane for lane in g0.build_lanes("") if lane.name == "guards"]
    assert len(guards) == 1, f"guards 车道应恰好一条，实得 {[ln.name for ln in g0.build_lanes('')]}"
    argv = guards[0].argv or []
    assert name in argv, f"guards 车道的 argv 没带上 {name}：{argv}"


def test_guards_lane_desc_count_matches_the_real_list():
    """车道描述里的「（N 文件）」必须等于 `GUARD_TESTS` 的**真实**长度 —— 防文档与实现漂移。

    这个计数在 `docs/agents/verification.md` §2 ⑦ 里也有一份（人工同步），所以它一旦能悄悄写错，
    人读到的那份就跟着错。这里把**代码侧**那一份钉死。
    """
    g0 = _maybe_gate0()
    guards = next(lane for lane in g0.build_lanes("") if lane.name == "guards")
    match = re.search(r"（(\d+) 文件）", guards.desc)
    assert match, f"guards 车道描述里应写明「（N 文件）」：{guards.desc!r}"
    assert int(match.group(1)) == len(g0.GUARD_TESTS), (
        f"描述写「{match.group(1)} 文件」但 GUARD_TESTS 有 {len(g0.GUARD_TESTS)} 个：{guards.desc!r}")
