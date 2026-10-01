"""覆盖闸门第四处加固：range 端点的不可变引用校验（431 真实事故 + 用户批准方案 a）。

## 它守什么

`check_review_coverage.main()` 在解析台账时对每行 range 的 base/tip 做形状校验：
端点必须是**不可变引用**——纯十六进制 SHA（完整 40/64 位或短缩写），可跟 `~N`/`^N`
代际修饰符；**活动引用**（`HEAD`、`head~1`、分支名、`refs/…`、tag 语义名）一律
`die`（fail-closed，不是 warn）。

### 事故原样（431 行）

`docs/review_ledger.d/431-mem-v2-301-heavy-lanes.tsv`（PR #470 线入库）的 range 列
写作 `60bc15d0..HEAD`——tip 是**活动引用**。检查器在运行时把 HEAD resolve 到
"当前的 HEAD"，该行客观放行其从未审查的后续提交（量化影响：`60bc15d0..cc1bb4b5`
共 12 提交）。`resolve_many` 对活动引用**也退非 None**（它确实存在）⇒ 修复前的
闸门没有任何报警：这正是 in-toto statement 的 subject "MUST have digest set" /
SLSA provenance 记录 pinned digest（resolvedDependencies）要防的失效形态——
**证明绑定到可变引用而静默失效**。

### 边界（为什么允许短 SHA 与 `~N`）

- 短 SHA 是同一对象的缩写、依然不可变（GitHub Actions "pin to full-length SHA"
  的理由是防**可变引用被改指**，不是防缩写）；存量台账大量行用短 SHA，全量升级
  属范围外。
- `~N`/`^N` 代际修饰符是提交图上的固定点（`089524a~1` 不随时间改变），存量 32 行
  用此形态。

### 验收（票面）

- 正控：431 原样（`..HEAD`）必须红；分支名/`refs/`/`HEAD~1` 必须红。
- 反控：短 SHA、修饰符形态、钉死后的 431 行（`60bc15d0..d6b3c6e4…`）不红。
- 现有合法台账（真实仓库）不红：闸门在真树上 exit 0。
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GATE_PATH = Path(os.environ.get("WBI_GATE_UNDER_TEST") or (REPO / "scripts" / "check_review_coverage.py"))

#: 431 行事故原样的 range（tip = 活动引用 HEAD）。
#: ⚠ 刻意硬编码而不是从磁盘读：磁盘上那行已被修复笔钉死成完整 SHA，
#: 从磁盘读会在修复入库后**静默变成永真断言**（与 REAL_TRUNCATED_ROW 同纪律）。
RANGE_WITH_HEAD_TIP = "60bc15d0..HEAD"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"加载不了 {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@contextmanager
def _capture_stdout(sink: dict):
    """捕获 sys.stdout（die() 写 stdout 后 sys.exit）。"""
    import io
    import sys

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield
    finally:
        sys.stdout = old
        sink["out"] = buf.getvalue()


@pytest.fixture(scope="module")
def gate():
    assert GATE_PATH.is_file(), f"闸门脚本不存在：{GATE_PATH}"
    return _load(GATE_PATH, "_coverage_gate_immutable_ref")


# --- 判据本体：main() 里没有独立的可导入函数，形状校验以内联 die 实现 ⇒
#     用例直接复刻同一正则并断言其**来源**（闸门脚本源码里的正则与判据存在性），
#     再用独立的 main()-级黑盒（变异钩子 WBI_GATE_UNDER_TEST）由回归覆盖。
#     这里断言三件事：①判据存在（源码含不可变引用校验）；②正则与本文档钉住的
#     语义一致；③431 原样被同一正则打回。

def test_gate_has_the_immutable_ref_check(gate):
    """判据必须存在：闸门脚本源码含 range 端点校验，且错误文案点名「活动引用」。

    ⚠ 断言的是**存在性 + 文案语义**（不是行号）：将来重构位置不该红，
    删掉判据必须红（那正是「悄悄拔掉这条护栏」的形状）。
    """
    src = GATE_PATH.read_text(encoding="utf-8")
    assert "台账 range 端点必须是不可变 SHA" in src, "判据文案必须存在"
    assert "活动引用" in src and "die(" in src, "必须是 fail-closed（die），不是 warn"


def test_the_pinned_regex_rejects_the_real_accident_shape():
    """**正控**：431 事故原样（`..HEAD`）必须被判据打回。

    这一条是整组里最重要的：它证明校验**真的抓得住历史上真实发生过的那类损坏**。
    判据 = 闸门脚本里钉死的同一正则字面量（源码包含性断言）+ 本用例复刻的
    `re.fullmatch` 行为——两处不一致（有人改了闸门却没改这里）会立即红。
    """
    src = GATE_PATH.read_text(encoding="utf-8")
    pattern = r"[0-9a-fA-F]{7,64}(~\d+|\^\d*|)?"
    assert f'"{pattern}"' in src, "闸门源码里的端点正则与本文档钉住的字面量不一致（判据被改形状？）"
    # 431 原样：HEAD / 分支名 / refs / 短代号带活动语义 —— 全部必须不匹配。
    for bad in ("HEAD", "head~1", "main", "origin/main", "refs/heads/main", "HEAD^", "@"):
        assert not re.fullmatch(pattern, bad), f"活动引用 {bad!r} 必须被正则打回"
    # 反控：不可变形态必须匹配（短 SHA / 完整 SHA / 修饰符）。
    for good in ("60bc15d0", "d6b3c6e4", "d6b3c6e40ceddb474ab8f41df738bd6ab9a17bcf",
                 "089524a~1", "9ab85ea^", "9ab85ea^1", "f" * 64):
        assert re.fullmatch(pattern, good), f"不可变引用 {good!r} 不得被打回"


def test_real_ledger_passes_after_the_431_fix():
    """**反控（整闸门级）**：真实台账在真树上 exit 0——修复笔钉死 431 行后，闸门必须绿。

    钉死前这行是 `..HEAD`（活动引用），加固后闸门会当场红；钉死后必须绿，
    否则加固就是「把存量全打红」的过度收紧。
    """
    proc = _load(GATE_PATH, "_coverage_gate_run").main([])
    assert proc == 0, f"真实台账必须绿（431 已钉死），实得 exit {proc}"


def test_gate_dies_on_a_ledger_row_with_head_tip(tmp_path, monkeypatch):
    """**黑盒正控（M1 缺口的牙齿）**：拿一个真 SHA 指到 `..HEAD` 端点的临时台账跑
    `main()` ⇒ 必须 exit 1，且**错误文案点名「活动引用」**（判据路径，不是别的 die）。

    ⚠ 上一窗口 M1 变异（把形状校验短路成 `if False`）**零失败**——两处原因都踩过：
    ①源码存在性/复刻正则两条不执行闸门；②`read_all` 是**双读**（legacy + `LEDGER_DIR`
    目录并集），只设 `LEDGER` 时真台账目录 313 行照常并入，其既有 `089524a~1..HEAD` 行
    走「tip 不存在」的旧路径先 die ⇒ 文案断言保证死在**本判据**上而不是凑巧同向。
    台账用真实 HEAD 的 SHA 作 base（合法端点）+ tip=HEAD（活动引用）：修复前
    `resolve_many` 解得出 HEAD ⇒ 闸门放行；修复后形状校验当场 die。
    """
    mod = _load(GATE_PATH, "_coverage_gate_head_tip")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.strip()
    # base 用真 HEAD 的 SHA（存在且是祖先）；die 在解析循环里，先于任何判定。
    ledger = tmp_path / "head_tip.tsv"
    ledger.write_text(
        f"2026-10-01\t#471 收尾记账（台账自身更新自动放行）\t{head}..{head}\r\n"
        f"2026-10-01\t431 事故原样（tip 是活动引用 HEAD）\t{head}..HEAD\r\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEDGER", str(ledger))
    # 双读机制：目录并集也要指到空目录，否则真台账的 313 行混进解析循环。
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "empty_dir"))
    # `--list` 也必须红：只读用法同样不能放过活动引用（否则任何只查 $? 的自动化用法失去闸门）。
    captured = {}
    with pytest.raises(SystemExit) as ei, _capture_stdout(captured):
        mod.main(["--list"])
    assert ei.value.code == 1, f"活动引用 tip 必须 die，实得 {ei.value.code}"
    # 死在**本判据**上（M1 缺口的牙齿）：文案必须点名「活动引用」——真台账混入时的
    # 旧路径（tip 不存在 / 解不出 HEAD 等）不能凑巧让本用例假绿。
    assert "活动引用" in captured.get("out", ""), (
        f"die 文案必须来自 range 端点校验（活动引用），实得：{captured.get('out', '')[:200]!r}")
