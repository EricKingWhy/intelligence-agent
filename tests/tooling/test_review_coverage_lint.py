"""覆盖闸门的三处加固（issue #295 / 缺陷 7 + 缺陷 5 + 缺陷 1）。

## 它守什么

三组互不重叠、且**各自可证伪**的判据。每组都有正控与反控（票面验收 1 / 1b）。

### A. 描述字段 lint（缺陷 7）

B-43 真实事故：用 `python -c "…"` 把含**反引号**的中文写进台账白名单行 ⇒ Git Bash 在双引号内
对反引号做**命令替换**，两处文件名被静默吞掉；更糟的是第二个反引号区间的文本被当成脚本执行，
**在仓库根创建了 6 个 0 字节垃圾文件**。闸门当时**完全没报警**，因为归属只看 sha 前缀，
描述字段是自由文本、不参与判定。

`check_review_coverage.lint_description()` 给描述字段加轻量体检。**默认 warn，`--strict` 升 fail**：
历史台账里有**已知**的截断行（下面 `REAL_TRUNCATED_ROW` 就是仓库里现存的一条），立刻改成 fail
会让闸门在存量上红 ——「新行从严、存量登记」是惯用做法。

### B. docs-only 按路径机械自动归属（缺陷 5，方案 C）

一个提交若其**全部改动路径**命中 `DOC_PATTERN` ⇒ 闸门自动归属并**逐条打印**，
不再需要手写白名单行。归属证据从「作者声明」升级为「路径客观事实」。
前置依赖：A 的 lint 必须先能跑（否则自动归属会把截断行照放）。

### C. 语义等价判据（缺陷 1）

`#294` 在协议 §8.1 第 3 条写死了口径（纯注释 / 纯 docstring 的 `src/**` diff ⇒ 语义等价），
本票把它变成机器可执行（`scripts/check_semantic_equiv.py`）。**它必须自证可证伪**：
正控「只改 docstring 措辞 ⇒ 等价」、负控「在注释里改字面量 ⇒ 不等价」、
反控「删一行真语句 ⇒ 不等价」。**最后一个才是关键** —— 没有它，
一个 `return True` 的假实现也能过前两个。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
#: 变异测试用：允许把闸门指到一份**打了变异的副本**上（issue #295 验收 2）。
#: 默认就是真文件 —— 这个钩子**只在**变异测试里被设置，正常跑没有任何影响。
GATE_PATH = Path(os.environ.get("WBI_GATE_UNDER_TEST") or (REPO / "scripts" / "check_review_coverage.py"))
EQUIV_PATH = Path(os.environ.get("WBI_EQUIV_UNDER_TEST") or (REPO / "scripts" / "check_semantic_equiv.py"))

#: 仓库里**现存**的真实截断行（`docs/review_ledger.d/062-7f6c0fd-9ab85ea.tsv`）。
#: 它的形状正是 `python -c` + 反引号命令替换的产物：反引号区间被整段吞掉，留下孤立的 `（）`。
#: ⚠ 这里刻意**硬编码**而不是从磁盘读：lint 的正控必须钉住一个**已知损坏的字节序列**，
#: 从磁盘读会在有人「顺手修好」该行之后**静默变成永真断言**。
REAL_TRUNCATED_ROW = (
    "2026-09-19\tP1-B4 两轴独立审查（实测把「加载更早」点到全量（）⇒ 该 finding 撤销）"
    "\t7f6c0fd..9ab85ea"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"加载不了 {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gate():
    assert GATE_PATH.is_file(), f"闸门脚本不存在：{GATE_PATH}"
    return _load(GATE_PATH, "_coverage_gate_under_test")


@pytest.fixture(scope="module")
def equiv():
    assert EQUIV_PATH.is_file(), f"等价判据脚本不存在：{EQUIV_PATH}（缺陷 1 要求新增）"
    return _load(EQUIV_PATH, "_semantic_equiv_under_test")


# =========================================================================== #
# A. 描述字段 lint（缺陷 7）
# =========================================================================== #

def test_lint_has_every_rule_the_ticket_names(gate):
    """票面点名的五条规则必须都在 —— 少一条就等于少一个机械护栏。

    ⚠ 断言的是**规则 id 的集合**（不是数量）：将来加规则不该让本测试红，
    但**删 / 改名**现有规则必须红（那正是「悄悄去掉一条护栏」的形状）。
    """
    required = {"unbalanced_backtick", "empty_parens", "control_chars", "overlong", "unbalanced_bold"}
    assert required <= set(gate.LINT_RULES), (
        f"lint 规则缺项：{sorted(required - set(gate.LINT_RULES))}（现值 {sorted(gate.LINT_RULES)}）")


def test_lint_flags_the_real_truncation_sample(gate):
    """**正控**：仓库里现存的真实截断行 ⇒ 必须报警，且报的规则能指回事故成因。

    这一条是整组里最重要的：它证明 lint **真的抓得住历史上真实发生过的那类损坏**，
    而不是一个「看起来像在做体检」的空转断言。
    """
    hits = gate.lint_description(REAL_TRUNCATED_ROW)
    assert hits, "真实截断样本必须报警（这是缺陷 7 的事故原样）"
    rules = {h["rule"] for h in hits}
    assert "empty_parens" in rules, f"截断行必命中「空括号」规则，实得 {sorted(rules)}"
    for h in hits:
        assert h["why"], f"每条命中都必须带原因（否则输出退化成一行无头无尾的规则名）：{h}"


def test_lint_does_not_flag_a_legitimate_row(gate):
    """**反控**：一条结构正常的审查行 ⇒ 一条规则都不许命中（不误报）。

    反控比正控更容易被写坏：一个「什么输入都报警」的实现能过正控，只有反控能拦住它。
    """
    good = (
        "2026-09-23\t#295 两轴独立审查（Standards + Correctness 各一独立只读子代理；"
        "范围 `a1b2c3d..d4e5f6a`，findings 全数处置）\t`a1b2c3d..d4e5f6a`"
    )
    hits = gate.lint_description(good)
    assert not hits, f"合法行不得被误报，实得 {hits}"


def test_lint_accepts_balanced_backticks_even_when_pairing_is_oddly_sized(gate):
    """**反控（边界）**：反引号**成对**就算合法，不要求「每两个相邻」。

    这条防的是一个很自然的错实现：在 `` r"`([^`]*)`" `` 之外再叠一个「必须偶数个相邻反引号」
    的判据。描述字段里出现 `` `a` 和 `b` `` 完全正常。
    """
    row = "2026-09-23\t审查了 `scripts/gate0.py` 与 `scripts/check_review_coverage.py`\ta..b"
    assert not gate.lint_description(row), "成对反引号（彼此不相邻）不得被误报"


def test_lint_flags_glob_stars_are_not_bold_hits(gate):
    """**反控**：`src/**` / `tests/**` 这类 **glob** 里的 `**` 不是 markdown 粗体，不得报。

    2026-09-23 实测：历史台账里 4 条行的 `**` 计数为奇数，逐个核对发现其中
    `099-37e8c4d-d165740.tsv` 的奇数**全部**来自 `src/**` / `tests/**` glob。
    若不做豁免，这条规则会给出**纯假阳性** —— 一个只会制造噪音的护栏比没有更糟：
    它会把真命中一起训练成噪音。
    """
    # ⚠ 两种形态都要覆盖，缺一不可（2026-09-23 变异测试实测：
    #   只写反引号内那种 ⇒ 「去掉 glob 豁免」的变异**零失败**，即该性质没被钉住 —— 真缺口）。
    #   ① 反引号内：被 _INLINE_CODE_RE 摘掉（另一种豁免路径）
    #   ② **裸的**：只能靠 _GLOB_STARS_RE 豁免 ⇒ 它才是 glob 豁免的鉴别力来源
    assert not gate.lint_description("2026-09-23\t零 `src/**` / `tests/**` 改动（glob，不是粗体）\ta..b"),         "反引号内的 glob ** 不得触发粗体奇偶检查"
    #   ③ **裸 glob + 成对粗体**：裸计 3 颗 `**`（奇 ⇒ 会报），豁免那 1 颗 glob 后剩 2 颗（偶 ⇒ 不报）。
    #      ⚠ 这才是真正**有鉴别力**的形状 —— 前两种（反引号内 / 纯 glob 偶数颗）豁免与否结论都一样，
    #      所以打掉 glob 豁免它们都不红（2026-09-23 变异测试实测：M3 零失败，就是这么被抓出来的）。
    assert not gate.lint_description(
        "2026-09-23\t改动面 src/** 无代码变更，**真粗体**已闭合\ta..b"
    ), "裸 glob + 成对粗体：豁免 glob 后 ** 为偶数，不得报（这条是 glob 豁免唯一的鉴别力来源）"


def test_lint_flags_unbalanced_bold_in_real_text(gate):
    """**正控**：真正的**粗体**不闭合 ⇒ 必须报（与上一条构成一对，证明豁免不是「整条规则关掉」）。"""
    row = "2026-09-23\t**这半截粗体没闭合（后面没有对应的两颗星）\ta..b"
    hits = gate.lint_description(row)
    assert any(h["rule"] == "unbalanced_bold" for h in hits), (
        f"真粗体不闭合必须报，实得 {[h['rule'] for h in hits]}")


def test_lint_flags_overlong_row_at_the_protocol_limit(gate):
    """**正控**：超长行报警 —— 阈值 = 协议 §8.5 的**硬上限 800**（票面原话）。

    刻意钉住常量本身：阈值若被「顺手调大」，护栏就失去意义，而输出看起来一切正常。
    """
    assert gate.LINT_LINE_LIMIT == 800, "阈值必须等于协议 §8.5 的硬上限，别顺手改"
    hits = gate.lint_description("x" * 801)
    assert any(h["rule"] == "overlong" for h in hits), "801 字符必须报超长"
    assert not gate.lint_description("x" * 800), "恰好 800 属上限内，不得报"


def test_lint_flags_control_and_private_use_chars(gate):
    """**正控**：NUL 与 Unicode 私用区字符报警（这两类是「文本被二进制污染」的可靠指纹）。"""
    assert any(h["rule"] == "control_chars" for h in gate.lint_description("正常文本\x00后面有 NUL")), "NUL 必须报"
    assert any(h["rule"] == "control_chars" for h in gate.lint_description("私用区 \ue000 字符")), "私用区必须报"


def test_lint_is_pure_and_total(gate):
    """lint 必须是**纯函数**且**全定义**：任何输入都不许抛异常。

    理由：它跑在闸门的判定循环里，一个会抛的实现等于「遇到畸形输入就崩」，
    而畸形输入恰恰是它存在的理由。
    """
    for probe in ["", "\t", "[whitelist]", "```", "\x00" * 3, "a" * 5000, "（）（）", "\r\n"]:
        gate.lint_description(probe)
    assert isinstance(gate.lint_description("x"), list)


# =========================================================================== #
# B. docs-only 按路径机械自动归属（缺陷 5 / 方案 C）
# =========================================================================== #

def test_docs_only_commit_is_auto_attributed_by_path(gate):
    """**正控**：全部改动路径都命中 `DOC_PATTERN` ⇒ 自动归属（不需要白名单行）。"""
    files = ["docs/PHASE_STATUS.md", "docs/phase_status/2026-09.md", "docs/review_ledger.tsv"]
    assert gate.is_docs_only(files), f"全 docs 的改动必须判 True：{files}"


def test_docs_only_rejects_any_non_doc_path(gate):
    """**反控（不放松的核心）**：夹带**任何一个**非文档路径 ⇒ 不得自动归属。

    这是「方案 C 没有放松闸门」的机械证明：自动归属的输入是**路径客观事实**，
    而 `scripts/**`、`.zcodeignore`、`web/**` 一律命中不了 `DOC_PATTERN`。
    """
    for bad in ["scripts/check_review_coverage.py", "src/agent_harness/agent/runtime.py",
                ".zcodeignore", "web/src/App.tsx", "tests/test_verification_map.py",
                "pyproject.toml"]:
        assert not gate.is_docs_only(["docs/PHASE_STATUS.md", bad]), (
            f"{bad} 不是文档路径，不得被自动归属连坐放行")


def test_docs_only_rejects_empty_file_list(gate):
    """**反控**：改动表**为空**（merge 提交 `--name-only` 的默认输出）⇒ 必须 fail-closed。

    空表有两种成因：真 merge（核对不了）与枚举失败。**两者都必须拒绝** ——
    「核对不了」不是「没问题」（参考实现 `.sh` 对此 fail-closed，本判据沿用）。
    """
    assert not gate.is_docs_only([]), "空改动表必须 fail-closed，不得当成「没有非文档文件」"


def test_auto_attribution_uses_the_same_doc_pattern_as_whitelist(gate):
    """自动归属**必须复用** `DOC_PATTERN`，不许另造一个更宽的模式。

    这是「两条判据不是两套真相」的锁：自动归属若自带一个更松的模式（比如顺手认了 `*.py`），
    闸门就在**看不见的地方**被放松了。这里逐例对账两者的一致性。
    """
    probes = ["docs/a.md", "docs/a.py", "docs/x/y.json", "README.md", "AGENTS.md", "CLAUDE.md",
              "CONTEXT.md", "scripts/a.py", "docs/review_ledger.d/001-a-b.tsv", "notmd", "a.txt"]
    for p in probes:
        assert gate.is_docs_only([p]) == bool(gate.DOC_RE.match(p)), (
            f"{p}: 自动归属与 DOC_PATTERN 结论不一致（自动归属={gate.is_docs_only([p])}，"
            f"DOC_PATTERN={bool(gate.DOC_RE.match(p))}）")


def test_auto_attribution_prints_every_path_for_audit(gate):
    """自动归属必须**逐条打印**（票面要求「保持可审计」）。

    断言的是**输出里含每一个路径** —— 归属从「作者声明」升级为「路径客观事实」，
    那这个客观事实就必须留在输出里，否则审计者只能看到一句「自动放行」。
    """
    files = ["docs/PHASE_STATUS.md", "docs/SDD_TICKET_TRACKER.md"]
    text = gate.format_auto_attribution("abc1234", "docs(#295): 落点", files)
    assert "abc1234" in text, "必须打印短 sha"
    for f in files:
        assert f in text, f"必须逐条打印路径，缺 {f}（实得 {text!r}）"


# =========================================================================== #
# C. 语义等价判据（缺陷 1）—— 三个控制样本，缺一不可
# =========================================================================== #

_NEG_SRC_OLD = (
    "VALUE = 1\n\n\ndef f():\n"
    "    # 这里的字面量是 1\n"
    "    return \"one\"\n"
)

_NEG_SRC_NEW = (
    "VALUE = 2\n\n\ndef f():\n"
    "    # 这里的字面量是 2\n"
    "    return \"two\"\n"
)

_POS_SRC_OLD = (
    "\"\"\"模块 docstring 的旧措辞。\"\"\"\n\n\ndef f():\n"
    "    \"\"\"函数的旧措辞。\"\"\"\n"
    "    return 1\n"
)

_POS_SRC_NEW = (
    "\"\"\"模块 docstring 的新措辞（改写，语义不变）。\"\"\"\n\n\ndef f():\n"
    "    \"\"\"函数的新措辞，说得更清楚一点。\"\"\"\n"
    "    # 顺便加一行注释，也只该判等价\n"
    "    return 1\n"
)

_DEL_SRC_OLD = "def f():\n    pass\n\n\ndef g():\n    return 2\n"

_DEL_SRC_NEW = "def f():\n    pass\n"


def test_negative_control_literal_changed_only_in_comments(equiv):
    """**负控**：在**注释里**改字面量（同时真值也变了）⇒ 判据必须判**不等价**。

    防的是把「注释 / docstring」当垃圾桶夹带真实改动。注意这里**代码本身也变了**
    （`VALUE = 1` → `VALUE = 2`）—— 这是故意的：若只改注释，两者本就等价，
    这条控制样本就变成永真断言、没有鉴别力。它要钉的是「真改动不许被注释豁免掩盖」。
    """
    assert equiv.is_semantically_equivalent(_NEG_SRC_OLD, _NEG_SRC_NEW) is False, (
        "真值 / 返回值变了，必须判不等价")


def test_positive_control_docstring_wording_only(equiv):
    """**正控**：只改 docstring 措辞（+ 纯注释）⇒ 判据必须判**等价**。

    `_POS_SRC_NEW` 比 OLD 多了一行纯注释 —— 这是刻意的第二层：证明判据剥离的是
    **全部注释**，不只是 docstring。
    """
    assert equiv.is_semantically_equivalent(_POS_SRC_OLD, _POS_SRC_NEW) is True, (
        "只改 docstring / 注释措辞，必须判等价")


def test_reverse_control_deleting_a_real_statement_is_not_equivalent(equiv):
    """**反控（最关键的一条）**：删掉一个**真语句**（整个 `g()`）⇒ 必须判**不等价**。

    没有这一条，一个 `def is_semantically_equivalent(a, b): return True` 的假实现
    能同时过正控与「只改措辞」类断言。**删真语句**才是判据的牙齿。
    """
    assert equiv.is_semantically_equivalent(_DEL_SRC_OLD, _DEL_SRC_NEW) is False, (
        "删掉一个函数是真改动，必须判不等价")


def test_deleting_only_a_pass_statement_is_not_equivalent(equiv):
    """**反控（最小剂量）**：`pass` → `...` 也必须判不等价。

    票面原话是「哪怕只是一行 `pass`」。这条把「最小真改动」钉死。
    """
    old = "def f():\n    pass\n"
    new = "def f():\n    ...\n"
    assert equiv.is_semantically_equivalent(old, new) is False, "`pass` → `...` 是表达式替换，必须不等价"


def test_equivalence_reports_a_normalized_digest(equiv):
    """判据必须同时给出**归一化摘要**（票面要求「等价 / 不等价 + 归一化摘要」）。

    摘要是「判据真的做了剥离」的可观测证据：两个只差措辞的 blob 必须给出**同一个**摘要；
    真改动过的必须不同。只看布尔值的话，一个永远返回 True 的实现和一个正确实现无法区分。
    """
    assert equiv.normalized_digest(_POS_SRC_OLD) == equiv.normalized_digest(_POS_SRC_NEW), (
        "只差 docstring / 注释措辞 ⇒ 归一化摘要必须相同")
    assert equiv.normalized_digest(_DEL_SRC_OLD) != equiv.normalized_digest(_DEL_SRC_NEW), (
        "删了真语句 ⇒ 归一化摘要必须不同")


def test_equivalence_fails_closed_on_syntax_error(equiv):
    """**反控**：语法都不合法的输入 ⇒ **不得**判等价（fail-closed，不许静默放行）。

    成因很现实：`src/**` 的半截 diff、或者拿错了 blob 类型。判据在「看不懂」时的正确反应是
    拒绝等价（那会让调用者回落正常审查），不是「看着差不多就算等价」。
    """
    assert equiv.is_semantically_equivalent("def f(:\n", "def f():\n    pass\n") is False
    assert equiv.is_semantically_equivalent("def f():\n    pass\n", "def f(:\n") is False


def test_equivalence_reads_blobs_from_git(equiv):
    """判据要能直接吃 `git rev-parse <rev>:<path>` 给出的 blob（票面给的输入口径）。

    用本仓真实历史取证：同一个 blob 与它自己必须判等价 —— 后者是这一组的正控，
    它同时证明了「读 blob」这条路是通的（若读失败 / 读空，自比也会不等于）。
    """
    proc = subprocess.run(["git", "rev-parse", "HEAD:scripts/check_review_coverage.py"],
                          cwd=REPO, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"解不出 blob：{proc.stderr}"
    blob = proc.stdout.strip()
    a = equiv.read_blob(blob, repo=REPO)
    b = equiv.read_blob(blob, repo=REPO)
    assert a and a == b, "同一个 blob 读两次必须相同且非空"
    assert equiv.is_semantically_equivalent(a, b) is True, "blob 与自身必须判等价"


def test_equivalence_tool_has_a_cli(equiv, tmp_path):
    """判据要能当**独立只读小工具**用（票面：输入两个 blob，输出结论 + 摘要）。

    跑子进程而不是调函数：CLI 存在性只有真跑才能证明（一个 `__main__` 写坏的模块
    在函数级测试里完全看不出来）。
    """
    blob_a = _blob_of(_POS_SRC_OLD)
    blob_b = _blob_of(_POS_SRC_NEW)
    proc = subprocess.run(
        [sys.executable, str(EQUIV_PATH), "--a", blob_a, "--b", blob_b, "--json"],
        cwd=REPO, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"CLI 失败：rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["equivalent"] is True, f"payload={payload}"


def _blob_of(content: str) -> str:
    """把内容写成 blob 并返回 sha（`git hash-object -w`，不碰工作树）。"""
    proc = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=REPO, input=content,
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"hash-object 失败：{proc.stderr}"
    return proc.stdout.strip()
