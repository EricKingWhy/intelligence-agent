"""`docs/agents/verification.map.tsv` 的守卫（issue #292 / 批次 B-41）。

## 它守什么

这张表是 `scripts/gate0.py --affected <rev>` 与协议 §8.8.3「环内可以跳过」两处的**唯一机械依据**。
表一旦腐烂（漏掉一个新出现的代码面、写错车道 id、指向不存在的 focused 用例、或者某一行变成没人需要的
装饰），`--affected` 就会**静默少跑**——那正是"放松"的形状。所以本测试把七件事钉死
（编号与协议 **§8.8.9**、`docs/agents/verification.md` **§2 ⑭** 同源，改一处必须三处同改）：

1. **覆盖面**：`git ls-files` 里的**每一个**被跟踪文件都必须被 ≥1 行匹配（无 catch-all 可依赖）。
   ⇒ 新增一个顶层代码面而不补映射，本测试会红。
2. **结构合法**：**8 列**、layer 唯一、`lanes` 在词表内、`focused` 路径在盘上存在、`neg_tier` ∈ 1..5、
   `surface` 只允许「路径前缀（以 `/` 结尾）」与「精确路径」两种形态（禁通配、禁 catch-all）。
3. **每行承重**：删掉**任何**一行，都会有至少一个文件的「受影响集合（车道 ∪ focused）」发生变化。
   ⇒ 装饰性的行活不下来（等价于「删掉一条映射 ⇒ 守卫打红」的变异证明）。
4. **focused 真的跑得动**：`focused` 指向 pytest 目标时，必须**真能收集到用例**（`tests/**` 目录下
   存在 `test_*.py`）；且哪些 focused 属于"按设计不内联"（前端/浏览器类）必须**逐个登记**在
   `test_focused_only_inlines_pytest_subsets` 里。只用"路径存在"当断言挡不住
   `No test files found` 那一类**假 FAIL**。
5. **每行都列无条件车道**：`diff-check` + `coverage`（决策表第 1 行「任何文件」）。漏列会让
   `--affected` 在那个面上把它们**静默跳过**，而输出看起来"一切正常"——那不是增量，是放松。
6. **`How to get to it (user POV)` 的形状可判**：要么写明「无用户入口」，要么引一个**真实存在**的
   仓库路径。这一列是散文，脚本判不了真伪，但能挡住"引一个不存在的入口"这种无法核实的空话。
7. **两份实现逐项相同**：本文件自带**独立**的匹配器与**独立**的求值器，对每个被跟踪文件与
   `scripts/gate0.py` 的 `affected_summary()` 对账——`map` 与 `--affected` **不是两套真相**。

## 它**不**守什么

- 不证明「受影响集合里的车道**足够**」：那是 `blast-radius` 阶梯上的 tier 3（走过失败路径），
  `neg_tier < 4` 的行由 `--affected` 标 `unproven`。本测试只把**覆盖面**这一条抬到 tier 4（真的跑了代码）。
- 不改任何产品行为；本文件只读表与 `git ls-files`。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MAP_PATH = REPO / "docs" / "agents" / "verification.map.tsv"
GATE0_PATH = REPO / "scripts" / "gate0.py"

COLUMNS = ("surface", "layer", "sub_features", "how_to_get_to_it",
           "lanes", "focused", "neg_tier", "gotchas")

# 列序 = map 的表头。**用具名索引而不是裸数字**：列序一变，裸数字会**静默指错列**——那种错不会报错，
# 只会让断言悄悄去查别的东西。2026-09-22 加 `how_to_get_to_it` 时正是靠这层避免整体错位。
I_SURFACE, I_LAYER, I_SUB, I_POV, I_LANES, I_FOCUSED, I_TIER, I_GOTCHAS = range(len(COLUMNS))

#: 无条件车道（`docs/agents/verification.md` §1 决策表第 1 行「任何文件」）。**每一行**都必须列它们。
UNCONDITIONAL_LANES = ("diff-check", "coverage")
LANE_VOCAB = {
    "diff-check", "ruff", "oxlint", "tsc", "guards", "coverage",          # Gate-0 可跑的 6 条
    "pytest-full", "pytest-clean", "vitest", "build", "e2e", "live",      # 重车道（Gate-0 之外）
}


def _split(cell: str) -> list[str]:
    return [x for x in cell.split("|") if x]


def _read_rows() -> list[list[str]]:
    """读表（`#` 注释与表头跳过）。**自写解析器**，故意不调 gate0——这样第 4 条才是真交叉对账。"""
    rows: list[list[str]] = []
    with open(MAP_PATH, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cells = line.split("\t")
            if tuple(cells) == COLUMNS:
                continue
            assert len(cells) == len(COLUMNS), f"{MAP_PATH.name}:{lineno} 列数 {len(cells)} != {len(COLUMNS)}"
            rows.append(cells)
    assert rows, f"{MAP_PATH.name} 没有数据行"
    return rows


ROWS = _read_rows()


def _matches(pattern: str, path: str) -> bool:
    """独立实现（刻意与 gate0 的写法不同，但语义必须等价）。"""
    if pattern.endswith("/"):
        return path.startswith(pattern) or path.rstrip("/") == pattern.rstrip("/")
    return path == pattern


def _row_hits(row: list[str], path: str) -> bool:
    return any(_matches(p, path) for p in _split(row[0]))


def _mine_eval(path: str) -> tuple[list[str], list[str], bool]:
    """本文件的独立求值：返回 (车道, focused, 是否未映射)。"""
    lanes, focused, hit = set(), set(), False
    for row in ROWS:
        if _row_hits(row, path):
            hit = True
            lanes |= set(_split(row[I_LANES]))
            focused |= set(_split(row[I_FOCUSED]))
    return sorted(lanes), sorted(focused), not hit


def _gate0_module():
    spec = importlib.util.spec_from_file_location("_gate0_under_test", GATE0_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tracked_files() -> list[str]:
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=False)
    assert proc.returncode == 0, "git ls-files 失败"
    files = [f.decode("utf-8").replace("\\", "/") for f in proc.stdout.split(b"\0") if f]
    assert len(files) > 500, f"被跟踪文件只有 {len(files)} 个？"
    return files


# ------------------------------------------------------------------ 1/2. 结构与词表

def test_map_has_uniform_line_endings_and_tabs():
    """行尾必须**统一**（半 CRLF 半 LF 不行），但**不**断言"必须是 CRLF"。

    为什么不断言具体口径：本仓 `core.autocrlf=true`，而 HEAD 里的 blob 是**纯 LF**（2026-09-22 实测
    `git show HEAD:docs/review_ledger.tsv`：CRLF=0 / LF=242；`PHASE_STATUS.md` / `verification.md` /
    `gate0.py` 同为 0）。也就是说工作树里的 CRLF 是**检出的产物**，在 `autocrlf=false` 的克隆里就是 LF
    ⇒ 断言"必须 CRLF"会在那边**假红**。真正的不变量是**统一性**：半转换的文件会在最后一格留下游离 `\r`。
    """
    raw = MAP_PATH.read_bytes()
    assert raw.count(b"\t") > 0, "map 必须是用 tab 分隔的 TSV"
    assert raw.endswith(b"\n"), "map 应以换行结尾"
    lone_cr = raw.count(b"\r") - raw.count(b"\r\n")
    lone_lf = raw.count(b"\n") - raw.count(b"\r\n")
    assert lone_cr == 0 and lone_lf == 0, (
        f"行尾不统一：游离 CR={lone_cr}、游离 LF={lone_lf}（半转换会污染最后一列；"
        "本仓 autocrlf=true ⇒ 只查统一性，不查具体口径）")


def test_layers_are_unique_and_columns_match_the_header():
    layers = [r[I_LAYER] for r in ROWS]
    dupes = sorted({x for x in layers if layers.count(x) > 1})
    assert not dupes, f"layer 必须唯一，重复：{dupes}"
    assert all(len(r) == len(COLUMNS) for r in ROWS)


def test_every_lane_id_is_in_the_vocabulary():
    bad = sorted({l for r in ROWS for l in _split(r[I_LANES]) if l not in LANE_VOCAB})
    assert not bad, f"车道 id 不在词表内（见 docs/agents/verification.md §2）：{bad}"


def test_every_row_lists_at_least_one_lane_and_a_valid_tier():
    for r in ROWS:
        assert _split(r[I_LANES]), f"{r[I_LAYER]}: 必须有 ≥1 条车道"
        assert int(r[I_TIER]) in (1, 2, 3, 4, 5), f"{r[I_LAYER]}: neg_tier 越界 = {r[I_TIER]}"


def test_surfaces_are_prefix_or_exact_only_no_wildcards_no_catch_all():
    """只允许「以 `/` 结尾的路径前缀」与「精确路径」；禁通配、禁 catch-all（否则守卫没牙齿）。"""
    banned = {"*", "**", "**/*", "/", "", ".", "./"}
    for r in ROWS:
        pats = _split(r[I_SURFACE])
        assert pats, f"{r[I_LAYER]}: surface 为空"
        for p in pats:
            assert p not in banned, f"{r[I_LAYER]}: catch-all 形态 {p!r} 不允许"
            assert not any(ch in p for ch in "*?["), f"{r[I_LAYER]}: 不允许通配 {p!r}"
            if "/" not in p:
                # 顶层裸名只允许是**文件**：目录必须写成 `dir/`，否则前缀语义不明（`logs` vs `logs/`）。
                assert not (REPO / p).is_dir(), f"{r[I_LAYER]}: 顶层目录必须以 '/' 结尾：{p!r}"


def test_every_focused_target_exists_on_disk():
    missing = sorted({t for r in ROWS for t in _split(r[I_FOCUSED]) if not (REPO / t).exists()})
    assert not missing, f"focused 指向不存在的路径：{missing}"


# ------------------------------------------------------------------ 3. 覆盖面 + 每行承重

def test_every_tracked_file_is_covered_by_at_least_one_row(tracked_files):
    """覆盖面：**每一个**被跟踪文件都要被匹配（fail-closed 的形状——漏了就会少跑）。"""
    unmatched = [f for f in tracked_files if _mine_eval(f)[2]]
    assert not unmatched, (
        f"有 {len(unmatched)} 个被跟踪文件没有任何映射行覆盖（新增代码面后要补 map）："
        f"\n  " + "\n  ".join(unmatched[:20]))


def test_every_row_is_load_bearing(tracked_files):
    """每行承重：删掉任一行 ⇒ 至少一个文件的（车道 ∪ focused）集合发生变化。

    这就是票面要求的「变异证明 map 有牙」的**自动化版本**：不需要手工删行，本断言等价于
    「每一行都删得动」——反过来说，任何一行被删掉都会有文件受影响，所以删掉即打红。
    """
    needed: set[int] = set()
    for path in tracked_files:
        idxs = [i for i, r in enumerate(ROWS) if _row_hits(r, path)]
        if not idxs:
            continue
        lane_c: Counter = Counter()
        foc_c: Counter = Counter()
        for i in idxs:
            lane_c.update(_split(ROWS[i][I_LANES]))
            foc_c.update(_split(ROWS[i][I_FOCUSED]))
        for i in idxs:
            unique = any(lane_c[l] == 1 for l in _split(ROWS[i][I_LANES])) or \
                     any(foc_c[t] == 1 for t in _split(ROWS[i][I_FOCUSED]))
            if unique:
                needed.add(i)
    lazy = [ROWS[i][I_LAYER] for i in range(len(ROWS)) if i not in needed]
    assert not lazy, f"这些行删掉不会有任何文件受影响（装饰性映射，必须删或改）：{lazy}"


# ------------------------------------------------------------------ 4. 与 gate0 交叉对账

def test_mine_and_gate0_agree_on_every_tracked_file(tracked_files):
    """`map` 求值 与 `gate0.affected_summary()` 求值必须**逐项相同**（两者不是两套真相）。"""
    g0 = _gate0_module()
    rows = g0.parse_map(str(MAP_PATH))
    mismatches = []
    for path in tracked_files:
        mine_lanes, mine_foc, mine_unmapped = _mine_eval(path)
        got = g0.affected_summary(rows, [path])
        if mine_unmapped:
            if got["unmapped"] != [path]:
                mismatches.append((path, "unmapped 判定不一致"))
            continue
        if (got["lanes"], got["focused"]) != (mine_lanes, mine_foc):
            mismatches.append((path, f"map={mine_lanes}/{mine_foc} gate0={got['lanes']}/{got['focused']}"))
    assert not mismatches, f"{len(mismatches)} 个文件两份实现结论不同：\n  " + "\n  ".join(
        f"{p} :: {why}" for p, why in mismatches[:20])


# ------------------------------------------------------------------ 票面「红证三态」的语义锁

def test_three_states_semantics_backend_session():
    """① 改 `src/agent_harness/session/**` ⇒ 选中后端相关项，**不得**选中前端车道。"""
    lanes, focused, unmapped = _mine_eval("src/agent_harness/session/runmanager.py")
    assert not unmapped
    assert {"ruff", "pytest-full", "coverage", "diff-check"} <= set(lanes)
    assert "tests/session" in focused
    assert not ({"oxlint", "tsc", "vitest", "build", "e2e"} & set(lanes)), lanes


def test_three_states_semantics_frontend_src():
    """② 改 `web/src/**` ⇒ 选中前端车道，**不得**选中后端全量 pytest。"""
    lanes, focused, unmapped = _mine_eval("web/src/App.tsx")
    assert not unmapped
    assert {"oxlint", "tsc", "vitest", "build", "coverage", "diff-check"} <= set(lanes)
    assert not ({"ruff", "pytest-full"} & set(lanes)), lanes
    assert focused == ["web/src"]


def test_three_states_semantics_docs_only():
    """③ 纯 `docs/**` ⇒ 只剩「任何文件都要跑」的那两条，**不得**选中任何代码车道。"""
    lanes, focused, unmapped = _mine_eval("docs/PHASE_STATUS.md")
    assert not unmapped
    assert lanes == ["coverage", "diff-check"], lanes
    assert focused == []


def test_event_vocabulary_surface_pulls_in_the_generated_artifacts_guard():
    """`session/event.py` 是词汇唯一真值 ⇒ 必须连带选中生成物守卫（这一条只有那一行能提供）。"""
    lanes, focused, _ = _mine_eval("src/agent_harness/session/event.py")
    assert "guards" in lanes
    assert "tests/test_event_types_generated.py" in focused
    assert "tests/test_event_vocabulary_generated.py" in focused
    # 生成物本体（手改必被守卫打红）
    gen_lanes, _, _ = _mine_eval("web/src/generated/event-types.ts")
    assert "guards" in gen_lanes


def test_gate_change_pulls_in_the_coverage_gate_itself():
    """改闸门自身 ⇒ 必须选中覆盖闸门车道（改闸门要真实审查行，不能走白名单）。"""
    lanes, _, _ = _mine_eval("scripts/check_review_coverage.py")
    assert {"coverage", "diff-check"} <= set(lanes)
    assert "ruff" in lanes


# ------------------------------------------------------------------ gate0 的 map 解析器自身

def test_gate0_parser_rejects_bad_input(tmp_path):
    """列数不符 / 车道 id 不在词表内，都必须**解析期**报错——不许静默按位置乱解、更不许少跑一条车道。"""
    g0 = _gate0_module()
    header = "\t".join(COLUMNS)
    # 行内字段顺序即 COLUMNS：surface / layer / sub_features / how_to_get_to_it /
    #                        lanes / focused / neg_tier / gotchas
    good = tmp_path / "good.tsv"
    good.write_text(header + "\n" + "a/\tb\tc\td\tdiff-check\tf\t3\tg" + "\n", encoding="utf-8")
    assert len(g0.parse_map(str(good))) == 1, "正控：合法的 8 列行必须能解析"
    short = tmp_path / "short.tsv"
    short.write_text("a/\tb\tc\n", encoding="utf-8")
    with pytest.raises(ValueError):
        g0.parse_map(str(short))
    # 第二种坏输入：列数对，但 `lanes` 里有个刻意的词表外 id —— 必须同样报错
    # （写错一个车道 id 就等于少跑一条车道，绝不能静默通过）。
    stray = tmp_path / "stray.tsv"
    stray.write_text(header + "\n" + "a/\tb\tc\td\tnot-a-lane\tf\t3\tg" + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        g0.parse_map(str(stray))


def test_gate0_affected_summary_fails_closed_on_unmapped_path():
    g0 = _gate0_module()
    rows = g0.parse_map(str(MAP_PATH))
    got = g0.affected_summary(rows, ["README.md", "no/such/surface.py"])
    assert got["unmapped"] == ["no/such/surface.py"]
    assert "diff-check" in got["lanes"]      # README.md 命中 governance 行

# ------------------------------------------------------------------ 5. focused 真的跑得动

#: 按设计**不内联**的 focused（前端 / 浏览器类）。加一条新的非 pytest focused 必须同时改这里——
#: 这是一道"要求你明确表态"的锁。原因见 `scripts/gate0.py::focused_runner` 的两条血证：
#: ① vitest 的 cwd 是 `web/`，仓库相对路径当 filter 会 `No test files found`（假 FAIL）；
#: ② 干净 HEAD 上 `vitest run src` 本身就红（B-29 已知 flake）⇒ 前端红无法归因。
MANUAL_FOCUSED = [
    "tests",      # 整个测试目录 = 重车道 pytest-full 本身（子集才便宜 ⇒ 全量不内联）
    "web/e2e",    # playwright 重车道
    "web/src",    # vitest：cwd 是 web/ 且干净 HEAD 上就有红（两条血证见 focused_runner）
]


def test_focused_only_inlines_pytest_subsets():
    """非 pytest 的 focused **一律不内联**，且必须逐个登记（防止有人"顺手"把它们接上内联车道）。"""
    g0 = _gate0_module()
    manual = sorted({t for r in ROWS for t in _split(r[I_FOCUSED]) if g0.focused_runner(t) is None})
    assert manual == MANUAL_FOCUSED, (
        "非 pytest 的 focused 集合变了。若这是有意的，请先读 `scripts/gate0.py::focused_runner` 的"
        f"两条血证再更新登记表。现值={manual} 期望={MANUAL_FOCUSED}")
    lanes, rest = g0.focused_lanes(["web/src", "tests/session"])
    # 血证 1 的形状：绝不能出现 `focused-vitest:*`（那是把仓库相对路径喂给 cwd=`web/` 的产物）。
    assert not [ln for ln in lanes if "vitest" in ln.name], f"不得产出 vitest 内联车道：{[ln.name for ln in lanes]}"
    assert [ln.name for ln in lanes] == ["focused-pytest:tests/session"]
    # ⚠ 这一条只锁**分类**，不锁 argv 是否存在：argv=None 有两种成因——① map 分类错（要红）；
    # ② 本机没有 `.venv`（那是 fail-closed 的**正确**行为，属环境）。2026-09-22 在隔离克隆里
    # 被这条断言假红过一次（克隆不含 .venv）⇒ 必须把两者分开，否则守卫自己就成了假红源。
    assert lanes[0].name == "focused-pytest:tests/session", "pytest 目标的分类必须稳定"
    if lanes[0].argv is None:
        assert "解释器" in lanes[0].blocked, (
            f"argv 缺失必须只因工具缺失（fail-closed）；实得 blocked={lanes[0].blocked!r}")
    # 不内联的项必须**逐条带原因**（否则输出会退化成"一条没头没尾的路径"）。
    paths = [item.split("（")[0] for item in rest]
    assert paths == ["web/src"], f"不内联清单的路径部分应只有 web/src，实得 {paths}"
    assert all(g0.focused_manual_reason(p) for p in paths), "每条不内联的项都必须能给出原因"
    assert all("（" in item and "）" in item for item in rest), f"不内联项必须带原因：{rest}"


def test_every_inlined_pytest_target_actually_collects_tests():
    """内联的 pytest 目标必须**真能收到用例**。

    `pytest <空目录>` 会以 exit code 5（no tests ran）返回——对调用者而言是**假 FAIL**，形状与
    vitest 的 `No test files found` 完全一样。只断言"路径在盘上存在"挡不住它，所以这里按 pytest
    自己的命名约定（`test_*.py` / `*_test.py`）对盘核对。
    """
    g0 = _gate0_module()
    bad = []
    for r in ROWS:
        for target in _split(r[I_FOCUSED]):
            if g0.focused_runner(target) != "pytest":
                continue
            path = REPO / target
            if path.is_file():
                if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
                    bad.append((target, "是 .py 但不符 pytest 命名，跑不起来"))
                continue
            found = list(path.rglob("test_*.py")) + list(path.rglob("*_test.py"))
            if not found:
                bad.append((target, "目录下没有任何 pytest 用例（会退化成假 FAIL）"))
    assert not bad, f"内联 focused 目标收不到用例：{bad}"

def test_every_row_lists_the_unconditional_lanes():
    """每一行都必须列 `diff-check` + `coverage` —— 这两条对**任何文件**都跑（决策表第 1 行）。

    漏列的后果是**静默缩小**：`--affected` 只保留受影响行列出的车道 ⇒ 那一面上这两条会被跳过，
    而输出看起来"一切正常"。2026-09-22 两轴审查发现 5 行漏列（`web/e2e/`、`web/scripts/`、
    `web/public/`、`frontend-config`、`demo/…`），所以它必须是**结构性**的，不能靠人记得。
    """
    miss = {r[I_LAYER]: sorted(set(UNCONDITIONAL_LANES) - set(_split(r[I_LANES]))) for r in ROWS
            if set(UNCONDITIONAL_LANES) - set(_split(r[I_LANES]))}
    assert not miss, f"这些行漏了无条件车道（--affected 会在该面静默跳过它们）：{miss}"


def test_every_row_states_how_a_user_reaches_it():
    """`How to get to it (user POV)`（skill §3 四要素之一）必须逐行给出，且形状**可机械判定**。

    这一列是**散文**，脚本判不了真伪——所以只强制一条能判的形状：要么写明「无用户入口」，
    要么**至少引一个真实存在的仓库路径**。它能挡住的是"写成无法核实的空话"（引一个不存在的入口）。
    """
    bad = []
    for r in ROWS:
        cell = r[I_POV].strip()
        if not cell:
            bad.append((r[I_LAYER], "为空"))
        elif "无用户入口" in cell:
            continue
        elif not [c for c in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", cell)
                  if (REPO / c).exists()]:
            bad.append((r[I_LAYER], "既没写「无用户入口」，也没引任何真实存在的仓库路径"))
    assert not bad, f"user POV 列不合格：{bad}"
