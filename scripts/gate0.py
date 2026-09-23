#!/usr/bin/env python3
"""gate0.py —— 推送前的快速门禁（Gate-0）。预算目标：**墙钟 ≤60 秒**（目标，非硬约束；超了只告警，不把「慢」伪装成「失败」）。

## 它解决什么

SDD 的完整门禁（后端全量 `pytest` + 前端 `tsc/vitest/oxlint/playwright/build`）一次是
**分钟到十几分钟级**。问题是"改一个简单小功能也要等一小时"——其中大量时间花在**与本次改动
无关**的检查上。Gate-0 不是要取代完整门禁，而是把**廉价、机械、必判**的那部分提前到**推送前**：

与"整棵冻结树全量测试"无关，只跑能在几十秒内跑完、且**结果可机械判定**的车道（见 `LANES`）。

## 它**不是**什么（读这段，别误解）

- **不是安全边界**：pre-push hook 本地可 `--no-verify` 绕过，也不影响别的 clone（启用靠本地
  `git config core.hooksPath .githooks`，该配置**不随仓库分发**）。它挡的是"忘了跑"，不是"故意绕过"。
- **不取代** `AGENTS.md` §14.10 的集成前完整门禁，也不取代两轴独立审查。
- **默认不做按路径跳过**：不带 `--affected` 时恒跑全部 6 车道（实测约 **20–22s（热）/ 36–45s（冷）**，
  2026-09-22 读数），已满足预算；改动面只作**信息展示**，不影响跑什么。按改动路径跳过某条车道属于**放松**
  （跨层影响难以穷举）⇒ 所以它是一个**显式 opt-in**、且**不用于推送前**（见下一条）。
- **`--affected <rev>`（issue #292）**：读 `docs/agents/verification.map.tsv`（代码面 ↔ 必跑车道的**机械映射**），
  由 `rev..HEAD` 的改动面算出**受影响集合**，只跑集合内的车道 + 受影响 focused 用例。
  **边界（与 `docs/agents/verification.md` §4 一致）**：它**只**用于**失败后的增量重跑**；
  **不得**替代**推送前全量 Gate-0**（pre-push 恒不带 `--affected`），也**不得**替代**集成前完整门禁**
  （协议 §8.8.4 第 1 行）。改动面里出现**未映射路径**时 **fail-closed**：退回全量并打印出来。
  输出会标出每层在 `blast-radius` 确定性阶梯上的级别；**< 4 的一律标 `unproven`**。
- **产生读数（issue #293）**：默认把本次读数落盘到 `docs/gate/<head sha>.json`（`sha` +
  `git rev-parse <sha>^{tree}` + 每车道结论 + 墙钟 + 工具版本）。**它是门禁读数的唯一来源** ——
  集成前的读数一律引用该文件，**不从终端输出手抄任何数字**（#213 的事故成因就是手抄 fixed point
  错一格 ⇒ 静默豁免一票，没有任何东西会报错）。
  · 只想看一眼、不想动工作树 ⇒ `--no-record`；`.githooks/pre-push` **恒带** `--no-record`
    （否则推送那一刻会在工作树里留下一个未提交的读数文件，把刚收干净的树弄脏）。
  · **只有裸全量运行落盘**：带 `--since` / `--only` / `--affected` 的一律不落盘 —— 它们是
    「推送范围 / 单车道 / 受影响面」的**局部**读数，写进同一个 `<sha>.json` 会把该树的**全量**
    结论覆写成只剩一条车道（`--replay` 还得依赖那个 `<rev>` 仍然存在）。
  · **落盘失败 = FAIL**（fail-closed）：读数的唯一来源写不出来 ⇒ 这条"通过"不可引用。
  · **工作树偏离 `HEAD` = 拒绝落盘并 FAIL**：6 条车道是在**工作树**上跑的，而读数只能记 `HEAD` 的
    `sha` / `^{tree}`；两者不一致时写出去，这份读数就指到了一棵**没被测过的树**（§8.7 第 3 条）。
    判据**不看单一 `git status` 的脸色** —— 它受本地 config 影响、且对 `assume-unchanged` 完全失明
    （两种绕过都被 R1 实测复现过）。三条独立判据：① 追踪文件偏离（显式带 `--untracked-files=all`）；
    ② `assume-unchanged` / `skip-worktree` 位（单独查 `git ls-files -v`）；③ 未跟踪文件里**后缀命中
    车道输入**的（`LANE_INPUT_SUFFIXES`）。其余未跟踪文件（本仓稳态就有 `?? .zcodeignore`）**如实
    记进读数的 `worktree.untracked`**、不据以拒绝 —— 否则一个与车道无关的未跟踪文件会把「读数的
    唯一来源」永久卡死。只想看结果、不想先提交 ⇒ 加 `--no-record`。
  · `--replay <file>` 按落盘里的 `argv` / `cwd` / `env` **原样重跑**并比对判定（墙钟不参与），
    让"落盘 JSON 可独立复核"成为机械判据而不是人眼比对。
  ⚠ 它只覆盖本脚本的**这 6 条机械车道**，**不是** `AGENTS.md` §14.10 的完整门禁 ——
  JSON 的 `scope.does_not_cover` 如实列出没跑的重车道。

## 用法 / 退出码

    python scripts/gate0.py                    # 跑全部车道（默认）；0 = 全绿，1 = 有失败或工具缺失
    python scripts/gate0.py --since <rev>      # 额外检查 <rev>..HEAD 的空白/冲突标记，并报告改动面
    python scripts/gate0.py --affected <rev>   # 只跑 <rev>..HEAD 改动面**受影响**的车道 + focused 用例
                                               # （`<rev>` 也可写成范围 `A..B`；仅用于失败后的增量重跑；
                                               #  未映射路径 ⇒ fail-closed 全量）
    python scripts/gate0.py --only ruff        # 只重跑一条（失败后增量验证用，别整条流水线重跑）
    python scripts/gate0.py --no-record        # 不落盘读数（只看一眼；pre-push 恒带它）
                                               #   带 --since/--only/--affected 时本来就不落盘
    python scripts/gate0.py --replay <file>    # 独立复核：原样重跑落盘里的命令，比对判定
    python scripts/gate0.py --list             # 列车道名

**fail-closed**：任何车道因"工具缺失 / 超时 / 无法执行"而没能得到结论，一律算**失败**，
不算"跳过"，不算通过（沿用 `check_review_coverage.sh` 的"核对不了就不放行"口径）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(REPO_ROOT, "web")

#: 单条车道的墙上限。不是预算（预算见 `GATE0_BUDGET`），而是"卡死也要有个结论"的兜底：
#: pre-push hook 里挂死会让人推不动代码，超时按失败处理并打印出来。
LANE_TIMEOUT = 300.0

#: 预算目标（墙钟秒）。**目标而非硬约束**：超了只打印告警，仍按各车道的真实退出码判定。
#: 原因：把「超预算」等于「失败」会把环境负载导致的慢误判成代码问题，与本项目「不做假绿灯」同理。
GATE0_BUDGET = 60.0

#: 生成物同步守卫：`src/.../event.py`（词汇唯一真值）↔ `web/src/generated/event-types.ts`
#: ↔ `docs/EVENT_VOCABULARY.md`。共 2 文件 6 例。
#: 另加**验证映射守卫** `tests/test_verification_map.py`（issue #292）：断言
#: `docs/agents/verification.map.tsv` 覆盖**全部被跟踪文件**、车道 id 合法、focused 路径存在，
#: 且**每一行都承重**（删掉任一行都会有文件的受影响集合发生变化）⇒ map 不能悄悄腐烂。
GUARD_TESTS = (
    "tests/test_event_types_generated.py",
    "tests/test_event_vocabulary_generated.py",
    "tests/test_verification_map.py",
)

#: 代码面 ↔ 必跑车道的机械映射（issue #292）。`--affected` 读它；`tests/test_verification_map.py` 守它。
MAP_PATH = os.path.join("docs", "agents", "verification.map.tsv")

#: 门禁读数的落盘目录（issue #293）：`docs/gate/<sha>.json`。
#: **集成前读数的唯一来源**（协议 §7 第 8 条 / `AGENTS.md` §14.10 指向这里）⇒ 必须落进版本控制。
GATE_DIR = os.path.join("docs", "gate")

#: 映射表的**列名（顺序即列序）**。`MapRow` 按位置解包，`parse_map` 用它校验列数。
#: 前四列里的后四者逐一对齐 `docs/agents/skills/create-verification-skill` 的 feature 四要素
#: （`Sub-features` / `How to get to it (user POV)` / `Driving it with <harness>` / `Gotchas`），
#: `surface` / `layer` / `neg_tier` 是给脚本消费的机械列（详见表头注释）。
COLUMNS = ("surface", "layer", "sub_features", "how_to_get_to_it",
           "lanes", "focused", "neg_tier", "gotchas")

#: 车道 id 词表（`docs/agents/verification.md` §2）：Gate-0 能跑的 6 条 + 重车道。
#: ⚠ 这是**唯一真值**，`parse_map` 用它做 fail-closed 校验（map 里写错一个车道 id ⇒ 解析期就报错，
#: 而不是等到 `--affected` 静默少跑一条）。`tests/test_verification_map.py` 另存一份**独立副本**——
#: 那是刻意的交叉对账（与它那份独立匹配器同理），不是可以合并的重复。
GATE0_LANE_IDS = ("diff-check", "ruff", "oxlint", "tsc", "guards", "coverage")
HEAVY_LANE_IDS = ("pytest-full", "pytest-clean", "vitest", "build", "e2e", "live")
LANE_VOCAB = (*GATE0_LANE_IDS, *HEAVY_LANE_IDS)

#: **无条件车道**：`docs/agents/verification.md` §1 决策表第 1 行「任何文件」都要跑这两条。
#: 映射表**每一行**都必须列它们（守卫会打红）——否则 `--affected` 会在那个面上把它们**静默跳过**，
#: 那不是"增量"，那是放松。记成常量是为了让这里也能自证意图。
UNCONDITIONAL_LANES = ("diff-check", "coverage")

TAIL_LINES = 30  # 失败时打印的输出尾部行数（够看清红在哪，又不淹没结论）


def _venv_file(*candidates: str) -> str | None:
    for rel in candidates:
        path = os.path.join(REPO_ROOT, rel)
        if os.path.isfile(path):
            return path
    return None


def venv_python() -> str | None:
    return _venv_file(".venv/Scripts/python.exe", ".venv/bin/python")


def venv_exe(name: str) -> str | None:
    """优先仓库 `.venv`（项目自己的工具链），退回 PATH。"""
    found = _venv_file(f".venv/Scripts/{name}.exe", f".venv/bin/{name}")
    return found or shutil.which(name)


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )


class Lane:
    """一条车道：名字 + 说明 + 怎么跑。`argv is None` 表示"跑不了"（fail-closed）。"""

    def __init__(self, name: str, desc: str, argv: list[str] | None, cwd: str,
                 env: dict | None = None, blocked: str = "") -> None:
        self.name, self.desc, self.argv, self.cwd = name, desc, argv, cwd
        self.env, self.blocked = env, blocked


def _split_cell(cell: str) -> list[str]:
    return [x for x in cell.split("|") if x]


class MapRow:
    """`verification.map.tsv` 的一行（列顺序 = 表头，见该文件顶部注释）。"""

    # 注意：元组顺序按 ruff RUF023 的字母序（属性名与列序无关，`__init__` 按名字赋值）。
    __slots__ = ("focused", "gotchas", "how_to_get_to_it", "lanes", "layer",
                 "neg_tier", "sub_features", "surface")

    def __init__(self, cells: list[str]) -> None:
        (self.surface, self.layer, self.sub_features, self.how_to_get_to_it,
         lanes, focused, tier, self.gotchas) = cells
        self.lanes, self.focused, self.neg_tier = _split_cell(lanes), _split_cell(focused), int(tier)

    @property
    def patterns(self) -> list[str]:
        return _split_cell(self.surface)


def parse_map(path: str | None = None) -> list[MapRow]:
    """读验证映射表。`#` 注释行与表头行跳过；用通用换行 ⇒ CRLF / LF 都吃（本文件是 CRLF）。"""
    p = path or os.path.join(REPO_ROOT, MAP_PATH)
    # 报错要指到**实际读的那个文件**：硬写 `MAP_PATH` 在解析临时文件时会把标签指向仓库那份 map，
    # 让人以为是仓库表坏了（2026-09-22 自己踩过：pytest 临时文件报出 docs/agents/verification.map.tsv）。
    label = MAP_PATH if os.path.abspath(p) == os.path.abspath(os.path.join(REPO_ROOT, MAP_PATH)) else p
    rows: list[MapRow] = []
    with open(p, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\r\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cells = line.split("\t")
            if tuple(cells) == COLUMNS:      # 表头
                continue
            if len(cells) != len(COLUMNS):
                raise ValueError(f"{label}:{lineno} 列数 {len(cells)} != {len(COLUMNS)}")
            row = MapRow(cells)
            stray = [x for x in row.lanes if x not in LANE_VOCAB]
            if stray:
                raise ValueError(
                    f"{label}:{lineno} 车道 id 不在词表内：{stray}"
                    "（词表见 docs/agents/verification.md §2；写错一个 id 就等于少跑一条车道）")
            # `UNCONDITIONAL_LANES` 在这里**真的被用上**（2026-09-22 审查指出它原先是死常量）：
            # 缺无条件车道 ⇒ **解析期**就报错，而不是等 `--affected` 在那个面上把它们**静默跳过**
            # （静默少跑一条 = 放松）。守卫里另有一份独立副本，那是刻意的交叉对账、不是重复。
            missing = [x for x in UNCONDITIONAL_LANES if x not in row.lanes]
            if missing:
                raise ValueError(
                    f"{label}:{lineno} 缺无条件车道：{missing}"
                    "（verification.md §1 决策表第 1 行「任何文件」都要跑这两条）")
            rows.append(row)
    if not rows:
        raise ValueError(f"{label} 没有数据行")
    return rows


def map_matches(pattern: str, path: str) -> bool:
    """只允许**路径前缀**（以 `/` 结尾）与**精确路径**两种形态；不做通配、不做 catch-all。

    ⚠ 必须与 `tests/test_verification_map.py::_matches` **逐字等价**——那份独立实现是对账的另一半，
    两边不等价时"两条路径对每个文件逐项相同"的结论就没有意义（2026-09-22 审查发现少了下面
    `rstrip("/")` 那一支）。目录形态对两侧尾斜杠的容忍在真实输入上是惰性的：`git ls-files` 从不
    产出以 `/` 结尾的路径；留着是因为守卫那份有，而等价是硬要求。
    """
    if pattern.endswith("/"):
        return path.startswith(pattern) or path.rstrip("/") == pattern.rstrip("/")
    return path == pattern


def affected_summary(rows: list[MapRow], files: list[str]) -> dict:
    """由改动面算「受影响集合」。未命中任何行的路径进 `unmapped`（调用方须 fail-closed）。"""
    lanes: set[str] = set()
    focused: set[str] = set()
    layers: list[str] = []
    unproven: set[str] = set()
    unmapped: list[str] = []
    for path in files:
        hit = [r for r in rows if any(map_matches(p, path) for p in r.patterns)]
        if not hit:
            unmapped.append(path)
            continue
        for r in hit:
            lanes.update(r.lanes)
            focused.update(r.focused)
            if r.layer not in layers:
                layers.append(r.layer)
            if r.neg_tier < 4:      # blast-radius 阶梯：到不了「跑了真代码」一律 unproven
                unproven.add(r.layer)
    return {"lanes": sorted(lanes), "focused": sorted(focused), "layers": layers,
            "unmapped": sorted(unmapped), "unproven": sorted(unproven)}


def _as_range(rev: str) -> str:
    """`<rev>` 或已经是范围 `A..B` ⇒ 一律归一成范围。

    **只有这一处**能把 rev 变成范围：改动面报告、车道①（`git diff --check`）、`changed_files` 三边
    必须同口径，否则会出现"受影响集合用 A、空白检查用 B"的分裂（这正是 `--affected` 最容易走偏的地方）。
    """
    return rev if ".." in rev else f"{rev}..HEAD"


def changed_files(rev: str) -> list[str] | None:
    """`<rev>..HEAD` 的改动文件；`rev` 也可以直接写成范围（`A..B`）——为了能在**真实历史**上
    评估某个单类改动集（三态红证用），不必克隆仓库。"""
    rng = _as_range(rev)
    proc = git("diff", "--name-only", rng)
    if proc.returncode != 0:
        return None
    return [ln.strip().replace("\\", "/") for ln in proc.stdout.split("\n") if ln.strip()]


#: focused 单元格里的路径一律**相对仓库根**（守卫据此对盘校验），但 runner 的 cwd 未必是仓库根。
#: ⇒ 「谁跑它」必须**显式**决定。把仓库相对路径"顺手"交给 cwd 不是仓库根的 runner 会拿到**假红**，
#: 本文件为此留了两条血证（2026-09-22，issue #292 的三态红证 B-web-src）：
#:
#:   血证 1（坐标系混用）：把 map 里的 `web/src` 原样交给 cwd=`web/` 的 vitest ⇒
#:     `No test files found, exiting with code 1` ⇒ 车道 FAIL / 整体 rc=1。
#:     而那一笔改动只碰了 `web/src/components/StepDetail.test.tsx` 一类前端文件，
#:     **红与改动无关**，是纯粹的假红。
#:   血证 2（潮水线以下的红）：修好过滤器之后实测，**同一类干净树跑两次结果不同**——一次 67 文件
#:     1067 例里 1 例超时（`web/src/components/StepDetail.window.test.tsx`，6224ms，即 map 的
#:     `frontend-src` 行 gotchas 里登记的 B-29 已知 flake），一次 **67 文件 1067 例全绿**（20.0s）。
#:     ⚠ 所以这里的红是**非确定性**的（不是"恒红"）——2026-09-22 首版把它写成了恒常事实，已更正。
#:     ⇒ 结论反而**更强**：同一棵树既红又绿 ⇒ 前端 vitest 的红**结构上无法归因**到本次改动。
#:
#: 取舍（与 Gate-0 自身的划分一致：快车道跑 / 重车道列出）：
#:   · **只内联 pytest 子集**——`pytest-full` 是重车道，而**子集**才是"失败后的增量重跑"真正便宜的面。
#:   · 前端 / 浏览器类 focused（vitest / e2e / live）**一律不内联**，只列进"需按 docs/agents/verification.md
#:     §2 命令人工跑"的清单，并附上"只看失败集合差集"的提醒。
#:   · 加新面时若想让某个 focused 内联，必须同时改 `focused_runner` 的判据与
#:     `tests/test_verification_map.py::test_focused_only_inlines_pytest_subsets` 的登记表——
#:     那是一道"要求你明确表态"的锁，不是可以绕过的装饰。
INLINE_RUNNER_PREFIXES = ("tests/",)


def focused_runner(path: str) -> str | None:
    """focused 路径由谁**内联**跑；`None` = 不内联（重车道 / 已知 flake ⇒ 人工跑）。

    **判据只看路径形态**，不做"就近挑一个 runner"：给 `web/**` 挑 vitest 正是上面两条血证的形状。
    """
    if path.startswith(INLINE_RUNNER_PREFIXES) or path.endswith(".py"):
        return "pytest"
    return None


def focused_manual_reason(path: str) -> str:
    """为什么这条 focused **不内联**。写进 `--affected` 的输出，免得被读成"map 漏配了"。

    三条都不是"忘了接"：`tests` 是整个套件（= 重车道 `pytest-full` 本身，子集才便宜）；
    `web/**` 两类重车道另有血证（见 `focused_runner`）；其余形态一律不猜 runner。
    """
    if path.rstrip("/") == "tests":
        return "整个测试目录 = 重车道 pytest-full 本身，按 §2 命令跑"
    if path.startswith("web/"):
        return "前端/浏览器重车道，按 §2 命令跑；vitest 在同类干净树上的读数非确定（有红有绿）⇒ 只看失败集合差集"
    return "非 pytest focused，没有可信的内联 runner，按 §2 命令人工跑"


def focused_lanes(focused: list[str]) -> tuple[list[Lane], list[str]]:
    """把 focused 路径变成可执行车道；返回 `(内联车道, 需人工跑的 focused)`。

    工具缺失一律 `argv=None`（fail-closed）。`None` 只来自 `focused_runner`，即"按设计不内联"，
    与"工具缺失"是两回事——后者会以 FAIL 现身，前者只会列进清单。
    """
    out: list[Lane] = []
    manual: list[str] = []
    py = venv_python()
    for target in focused:
        if focused_runner(target) != "pytest":
            manual.append(f"{target}（{focused_manual_reason(target)}）")
            continue
        out.append(Lane(
            f"focused-pytest:{target}", f"pytest {target}（受影响 focused 用例）",
            [py, "-m", "pytest", target, "-q", "-p", "no:randomly", "-p", "no:cacheprovider"]
            if py else None,
            REPO_ROOT, env=dict(os.environ, PYTHONUTF8="1", PYTHONPATH=""),
            blocked="找不到 .venv 解释器"))
    return out, manual


def build_lanes(since: str) -> list[Lane]:
    lanes: list[Lane] = []

    # ① 空白 / 冲突标记：AGENTS.md §14.10 的固定项。给了 --since 就查提交范围，
    #    否则查工作树（未提交改动）。`since` 允许就是范围形态（`A..B`）。
    rng = _as_range(since)
    diff_args = ["diff", "--check"] + ([rng] if since else [])
    lanes.append(Lane("diff-check", f"git diff --check {' '.join(diff_args[2:])}".strip(),
                      ["git", *diff_args], REPO_ROOT))

    # ② 后端静态检查：仓库既有的门禁命令就是 `ruff check .`（协议 §7 第 6 条）。
    ruff = venv_exe("ruff")
    lanes.append(Lane("ruff", "ruff check .", [ruff, "check", "."] if ruff else None,
                      REPO_ROOT, blocked="找不到 ruff（先 `uv sync` / 装依赖）"))

    # ③④ 前端静态检查：直接调包的 .js 入口，不经过 `npm`/`npx`/`.bin/*.cmd`——
    #     本机沙箱把 `cmd.exe` 拉黑（`.cmd` 秒退且报错字节是 GBK 的"拒绝访问。"），
    #     而 `.bin/oxlint` 是 POSIX sh 脚本、会用到 `dirname`/`sed`（shim 的 **PATH 里没有**
    #     coreutils——不是不存在，它们在 Git 自带目录里，见审计 §5.2 的更正块）。
    node = shutil.which("node")
    oxlint_js = os.path.join("node_modules", "oxlint", "bin", "oxlint")
    tsc_js = os.path.join("node_modules", "typescript", "bin", "tsc")
    have_js = os.path.isdir(os.path.join(WEB_DIR, "node_modules"))
    blocker = "" if have_js else "找不到 web/node_modules（先 `cd web && pnpm install`）"
    lanes.append(Lane("oxlint", "oxlint（web/）",
                      [node, oxlint_js] if (node and have_js) else None, WEB_DIR,
                      blocked=blocker or "找不到 node"))
    lanes.append(Lane("tsc", "tsc -b（web/，类型检查；不含 vite build）",
                      [node, tsc_js, "-b"] if (node and have_js) else None, WEB_DIR,
                      blocked=blocker or "找不到 node"))

    # ⑤ 生成物同步守卫：跨 `src/` 与 `web/` 的漂移只有这一条能抓，所以**与改动面无关**、恒定跑。
    py = venv_python()
    guard_env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH="")
    lanes.append(Lane(
        "guards", "pytest 生成物同步守卫 + 验证映射守卫（3 文件）",
        [py, "-m", "pytest", *GUARD_TESTS, "-q", "-p", "no:randomly", "-p", "no:cacheprovider"]
        if py else None,
        REPO_ROOT, env=guard_env,
        blocked="找不到 .venv 解释器",
    ))

    # ⑥ 覆盖闸门：唯一机械闸门。python 版是本机唯一跑得动的那版（`.sh` 依赖 coreutils）。
    lanes.append(Lane("coverage", "覆盖闸门 check_review_coverage.py",
                      [py, os.path.join("scripts", "check_review_coverage.py")] if py else None,
                      REPO_ROOT, blocked="找不到 .venv 解释器"))
    return lanes


def run_lane(lane: Lane) -> tuple[int, float, str]:
    started = time.time()
    try:
        proc = subprocess.run(
            lane.argv, cwd=lane.cwd, env=lane.env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=LANE_TIMEOUT, check=False,
        )
        return proc.returncode, time.time() - started, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, time.time() - started, f"⚠ 超时：超过 {LANE_TIMEOUT:.0f}s 仍未返回"
    except OSError as exc:  # 解释器/可执行文件本身起不来
        return 127, time.time() - started, f"⚠ 无法执行：{exc}"


def surface_report(since: str) -> str:
    """改动面（**信息展示**，不影响跑什么）。

    `--since` 缺省时退化为工作树未提交改动。此时车道 ①（diff-check）也**只覆盖工作树**，不检查「已提交但未推送」的提交。
    这是一个**降级**，不能靠读使用者自己想起来——所以写进返回值，而不只写在注释里。
    """
    if since:
        rng = _as_range(since)
        proc = git("diff", "--name-only", rng)
        scope = rng
    else:
        proc = git("status", "--porcelain")
        scope = "工作树（未提交）"
    # ⚠ **不许先 `strip()` 再切**：`git status --porcelain` 的前两列是状态码，第 3 列起才是路径，
    # 而 ` M x` 的**前导空格本身就是状态列的一部分**。先 strip 会吃一个字符——实测把
    # `scripts/…` 显示成 `cripts/…`（本车道 2026-09-22 自己打出来暴露的，见审计 §8.6）。
    lines = [ln for ln in proc.stdout.split("\n") if ln.strip()]
    files = [ln[3:].strip() for ln in lines] if not since else [ln.strip() for ln in lines]
    # 未给 --since 时只覆盖工作树（车道 ① 同理）⇒ 把降级写进返回值，别让人误读成「推送范围已查」。
    caveat = "" if since else ("；⚠ 未给 --since ⇒ 车道 ① 只覆盖工作树，不含已提交未推送的提交")
    if proc.returncode != 0:
        return f"改动面：?（{scope} 取不到）{caveat}"
    buckets: dict[str, int] = {}
    for path in files:
        top = path.split("/")[0]
        buckets[top] = buckets.get(top, 0) + 1
    if not buckets:
        return f"改动面：0 文件（{scope}）{caveat}"
    shown = "  ".join(f"{k} {v}" for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]))
    return f"改动面：{len(files)} 文件（{scope}）— {shown}{caveat}"


# --------------------------------------------------------------------------- #
# 读数落盘与独立复核（issue #293）
# --------------------------------------------------------------------------- #

def _rel(path: str) -> str:
    """仓库相对路径（正斜杠）——打印与 JSON 里统一用这个形态。"""
    return os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")


def _portable_argv(argv: list[str]) -> list[str]:
    """把 argv 里的**机器绝对路径**折成可携带形态。

    落盘文件进**版本控制**（而且是公开仓库），不应该把本机路径写进去：
    `REPO_ROOT` 前缀 ⇒ `.`；用户主目录前缀 ⇒ `~`；其余原样；分隔符统一正斜杠。
    `--replay` 用 `_expand_argv` 对称还原 —— 两边必须同时改，否则"原样重跑"就破了。
    """
    pairs = ((REPO_ROOT.replace("\\", "/"), "."), (os.path.expanduser("~").replace("\\", "/"), "~"))
    out: list[str] = []
    for el in argv:
        norm = el.replace("\\", "/")
        for prefix, mark in pairs:
            if norm == prefix:
                norm = mark
                break
            if norm.startswith(prefix + "/"):
                norm = mark + norm[len(prefix):]
                break
        out.append(norm)
    return out


def _expand_argv(argv: list[str]) -> list[str]:
    """`_portable_argv` 的逆（`--replay` 用）：`.` ⇒ `REPO_ROOT`，`~` ⇒ 用户主目录。"""
    out: list[str] = []
    for el in argv:
        if el == ".":
            out.append(REPO_ROOT)
        elif el.startswith("./"):
            out.append(os.path.join(REPO_ROOT, el[2:]))
        elif el == "~" or el.startswith("~/"):
            out.append(os.path.expanduser(el))
        else:
            out.append(el)
    return out


def _probe_version(argv: list[str] | None) -> str:
    """取某个工具自报的版本（取首行）。取不到就如实写 `unknown（…）`，**不编**。"""
    if not argv:
        return "missing"
    try:
        proc = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20, check=False)
    except subprocess.TimeoutExpired:
        return "unknown（超时）"
    except OSError as exc:
        return f"unknown（{type(exc).__name__}）"
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    first = text.split("\n")[0].strip() if text else ""
    if proc.returncode != 0 or not first:
        return "unknown（取不到）"
    return first


def tool_versions() -> dict[str, str]:
    """本次读数涉及的工具版本（票面 #293 明列的字段之一）。缺失一律写 `missing`。"""
    out: dict[str, str] = {"git": _probe_version(["git", "--version"])}
    py = venv_python()
    out["python"] = _probe_version([py, "--version"]) if py else "missing"
    ruff = venv_exe("ruff")
    out["ruff"] = _probe_version([ruff, "--version"]) if ruff else "missing"
    node = shutil.which("node")
    out["node"] = _probe_version([node, "--version"]) if node else "missing"
    # 前端两条走包的 .js 入口（与车道 ③④ 同一形态），不经过 npm / .bin/*.cmd。
    for name, rel in (("oxlint", os.path.join("node_modules", "oxlint", "bin", "oxlint")),
                      ("tsc", os.path.join("node_modules", "typescript", "bin", "tsc"))):
        js = os.path.join(WEB_DIR, rel)
        out[name] = _probe_version([node, js, "--version"]) if (node and os.path.isfile(js)) else "missing"
    return out


def _env_overrides(lane: Lane) -> dict[str, str]:
    """车道显式设过的环境变量（**只记与当前进程不同的键**）。

    复核时必须原样重放：`pytest` 那两条车道设了 `PYTHONUTF8=1` / `PYTHONPATH=`，
    不重放就会拿到不同的读数 —— "同一命令"包括它的环境。
    """
    if not lane.env:
        return {}
    return {k: v for k, v in lane.env.items() if os.environ.get(k) != v}


def reading_doc(*, head: str, tree: str, argv: list[str], wall: float,
                results: list[tuple[Lane, int, float, str]],
                affected: dict | None, changed: list[str],
                worktree: dict | None = None) -> dict:
    """拼出落盘的读数文档。

    **票面 #293 的字段要求**（`sha` + `^{tree}` + 每车道结论 + 墙钟 + 工具版本）由
    `sha` / `tree` / `lanes[].status` / `wall_seconds` / `tool_versions` 承载；
    `argv` + `cwd` + `env` 让 `--replay` 能**原样重跑**，否则"可独立复核"就只剩人眼比对。
    失败车道的输出尾部一并落下（红在哪要能直接看见）；通过车道不记输出（免得 JSON 变成日志）。
    """
    lanes: list[dict] = []
    for lane, rc, secs, out in results:
        entry: dict = {
            "name": lane.name,
            "desc": lane.desc,
            "status": "PASS" if rc == 0 else "FAIL",
            "rc": rc,
            "seconds": round(secs, 2),
            "cwd": _rel(lane.cwd),
        }
        if lane.argv is None:
            entry["argv"] = None
            entry["command"] = None
            entry["blocked"] = lane.blocked
        else:
            portable = _portable_argv(lane.argv)
            entry["argv"] = portable
            entry["command"] = " ".join(portable)
            overrides = _env_overrides(lane)
            if overrides:
                entry["env"] = overrides
        if rc != 0:
            tail = [ln for ln in out.split("\n") if ln.strip()][-8:]
            if tail:
                entry["output_tail"] = tail
        lanes.append(entry)
    failed = [e["name"] for e in lanes if e["status"] != "PASS"]
    return {
        "schema": 1,
        "gate": "gate0",
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sha": head,
        "tree": tree,
        "argv": list(argv),
        "result": "FAIL" if failed else "PASS",
        "passed": len(lanes) - len(failed),
        "total": len(lanes),
        "failed": failed,
        "wall_seconds": round(wall, 2),
        "budget_seconds": GATE0_BUDGET,
        "lane_timeout_seconds": LANE_TIMEOUT,
        "tool_versions": tool_versions(),
        "scope": {
            "covers": "gate0.py 的机械车道（见 lanes）",
            "does_not_cover": [
                "AGENTS.md §14.10 的集成前**完整**门禁重车道：pytest-full / pytest-clean / vitest / build / e2e / live",
                "两轴独立审查与 Runtime Verification 的人工判据",
            ],
        },
        "affected": (dict(affected, changed_files=len(changed)) if affected is not None else None),
        # 读数**自证**它跑在哪棵树上：落盘前已断言追踪文件与 HEAD 一致、且无 assume-unchanged /
        # skip-worktree 位（否则根本走不到这里）。未跟踪文件里**不适配车道输入**的那些如实列出，
        # 免得读者以为「工作树完全等于 HEAD」（那是更强的、我们**没有**证明的断言）。
        "worktree": {
            "tracked_matches_head": True,
            "untracked": [ln[3:].strip() for ln in (worktree or {}).get("untracked", [])][:20],
            "untracked_total": len((worktree or {}).get("untracked", [])),
        },
        "lanes": lanes,
    }


#: 未被跟踪、但**可能被某条车道读进来**的后缀 ⇒ 出现就拒绝落盘（fail-closed）。
#: ruff 读 py/pyi/ipynb；oxlint / tsc 读 ts/tsx/js/jsx/mjs/cjs/vue；guards / coverage 读
#: tsv（映射表、台账）与 json/toml/cfg/ini/yaml 一类配置。
LANE_INPUT_SUFFIXES = (
    ".py", ".pyi", ".ipynb",
    ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".vue",
    ".json", ".toml", ".cfg", ".ini", ".tsv", ".yaml", ".yml",
)

#: 本脚本**自己的输出**目录：它不是任何车道的输入（没有车道读 `docs/gate/**`），而落盘必然把它
#: 造出来 ⇒ 必须从「车道输入」判据里排除，否则「同一棵树重跑」永远走不到（第二次一定被守卫拒）。
GATE_DIR_PREFIX = GATE_DIR.replace(os.sep, "/") + "/"


def worktree_divergence() -> dict:
    """工作树对 `HEAD` 的偏离（**不依赖本地 config，也不盲信 `git status`**）。

    车道是在**工作树**上跑的，而读数只能记 `HEAD` 的 `sha` / `^{tree}` ⇒ 两者一旦不一致，
    写出去的读数就指到了一棵**没被测过的树**（协议 §8.7 第 3 条）。返回四类：

      · `tracked`   —— 追踪文件的内容/类型偏离。**显式**带 `--untracked-files=all`：
                        `git config status.showUntrackedFiles no` 挡不住它（R1 实测过这个绕过）。
      · `hidden`    —— `assume-unchanged` / `skip-worktree` 位。⚠ 这两个位让 `git status`
                        **彻底看不见**该文件的改动（R1 实测：打上 assume-unchanged 后改文件，
                        `git status --porcelain` 仍为空、落盘照样成功）⇒ 必须单独查 `ls-files -v`。
      · `untracked` —— 其余未跟踪文件（**如实记录，不据以拒绝**）。
      · `risky`     —— 未跟踪文件里**后缀命中 `LANE_INPUT_SUFFIXES`** 的那些（据以拒绝）。

    `tracked` / `hidden` / `risky` 非空 ⇒ 拒绝落盘并 FAIL。**不**把「有任何未跟踪文件」当拒绝
    理由（本仓稳态就有 `?? .zcodeignore`）—— 那会把「读数的唯一来源」永久卡死；也不把
    `docs/gate/` 自己的产物算成「车道输入」（它不是任何车道的输入，见 `GATE_DIR_PREFIX`）。
    """
    tracked: list[str] = []
    untracked: list[str] = []
    # `-c core.quotepath=false`：让 `git status` 以**原始字节**输出非 ASCII 路径，而不是 C-quote
    # （`?? "ZZ_\344\270\255..."`）。否则 `endswith(LANE_INPUT_SUFFIXES)` 对带引号/八进制转义的
    # 路径恒为假 ⇒ 非 ASCII 的未跟踪车道输入被守卫**放行**（R2 P1-b 实测复现）。
    stat = git("-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=all").stdout
    for line in stat.splitlines():
        if not line.strip():
            continue
        (untracked if line.startswith("??") else tracked).append(line)
    # 除 `H`（正常缓存）以外的任何位都算偏离：`S` = skip-worktree、小写 = assume-unchanged。
    hidden = [ln for ln in git("ls-files", "-v").stdout.splitlines() if ln[:1] and ln[:1] != "H"]
    risky = [
        u for u in untracked
        if u[3:].strip().lower().endswith(LANE_INPUT_SUFFIXES)
        and not u[3:].strip().replace(os.sep, "/").startswith(GATE_DIR_PREFIX)
    ]
    return {"tracked": tracked, "hidden": hidden, "untracked": untracked, "risky": risky}


def write_reading(*, head: str, tree: str, argv: list[str], wall: float,
                  results: list[tuple[Lane, int, float, str]],
                  affected: dict | None, changed: list[str],
                  worktree: dict | None = None) -> str:
    """落盘到 `docs/gate/<head sha>.json`，返回绝对路径。

    文件名用**全 40 位 sha**：短 sha 的宽度是环境属性（同一提交 7 位 / 8 位都实测过），当键会撞。
    键是 `sha` 而**不是** `^{tree}`：共享同一棵树的多个提交（纯 docs 提交）各自产出一份，
    `--replay` 各认各的。**同一个 sha 重跑会覆写同名文件**（读数以最后一次为准）—— 有意的。
    ⚠ 但「重跑」有个前置：落盘一定把 `docs/gate/<sha>.json` 造出来，**只要它还没被提交**，守卫就
    放行（它被 `GATE_DIR_PREFIX` 排除在「车道输入」之外）；**一旦它进了某个提交**，再跑就是
    「追踪文件被改写」⇒ 守卫拒绝。想在那个 sha 上再取一次读数：要么先把这份读数提交掉、再在别处
    checkout 该 sha 跑，要么本次只是「看一眼」就加 `--no-record`。
    ⇒ 这段是本机制**固有的自指**（树里的文件无法认证它自己），如实写在这里，别读成 bug。
    """
    doc = reading_doc(head=head, tree=tree, argv=argv, wall=wall, results=results,
                      affected=affected, changed=changed, worktree=worktree)
    out_dir = os.path.join(REPO_ROOT, GATE_DIR)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{head}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def replay_reading(path: str) -> int:
    """独立复核（票面 #293 的 AC）：按落盘里的 `argv` / `cwd` / `env` **原样重跑**，比对判定。

    比的是**判定**（每条车道的 PASS/FAIL），**不是墙钟** —— 票面明写"墙钟允许不同"。

    **它证明什么、不证明什么**（R1 findings 后收紧，2026-09-22）：
    · 证明："落盘里这些命令，在**当前工作树**上也给出同样的判定"。
    · **不**证明"落盘那棵树被复核了" —— 落盘文件必然落在其目标 sha 的**子提交**里，所以复核时的
      `HEAD` 天然不等于落盘 sha。两者不同时**打印出来**，别读成同一棵。
    · 因此还做四道机械校验，任一不过一律 FAIL（缺一即不可引用）：
      ① 落盘 `sha` / `tree` 必须是 40 位 hex；且 `sha` 在本仓**真实存在**、它的 `^{tree}` 必须
         **等于**落盘写的 `tree`（伪造的 sha、或 sha 与 tree 不自洽 ⇒ 直接拒；`deadbeef…` 这类
         在 R1 里被实测过能骗过旧版复核）；
      ② 落盘里的**车道名集合**必须与当前脚本的车道集合完全一致 —— 脚本演化后旧 JSON 不得静默
         "复核通过"而新车道从未被跑；
      ③ 有车道 `argv` 缺失（工具缺失/被拦）⇒ **FAIL，不是 SKIP**（"核对不了就不放行"）；
      ④ 当前工作树不得偏离 `HEAD`（同 `worktree_divergence()`），否则"判定相同"毫无意义。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"❌ 读不到 / 解析不了读数：{exc}")
        return 1
    lanes = doc.get("lanes") or []
    if not lanes:
        print("❌ 读数里没有 lanes")
        return 1
    recorded_sha = str(doc.get("sha") or "")
    recorded_tree = str(doc.get("tree") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", recorded_sha) or not re.fullmatch(r"[0-9a-f]{40}", recorded_tree):
        print(f"❌ 落盘里的 sha / tree 不是 40 位 hex：sha={recorded_sha!r} tree={recorded_tree!r}")
        return 1
    real_tree = git("rev-parse", "--verify", "--quiet", recorded_sha + "^{tree}").stdout.strip()
    if real_tree != recorded_tree:
        print(f"❌ 落盘署名不自洽：sha={recorded_sha[:12]} 在本仓解析出的 tree="
              f"{real_tree[:12] or '（该 sha 不存在）'}，而落盘写的是 {recorded_tree[:12]}"
              " ⇒ 这份读数不可引用（篡改过 / 不是本仓的读数）。")
        return 1
    divergence = worktree_divergence()
    blocking = divergence["tracked"] + divergence["hidden"] + divergence["risky"]
    if blocking:
        print(f"❌ 复核前工作树已偏离 HEAD（{len(blocking)} 条）⇒ “判定相同”证明不了任何东西。"
              " 先提交/摘掉这些改动再来。")
        return 1
    current = [ln.name for ln in build_lanes("")]
    recorded_names = [ln.get("name") for ln in lanes]
    if recorded_names != current:
        print("❌ 车道集合不一致 —— 脚本已演化，这份落盘的复核结论**不成立**：")
        print(f"     落盘：{recorded_names}")
        print(f"     当前：{current}")
        return 1
    print(f"复核 {_rel(os.path.abspath(path))}：落盘 sha={recorded_sha[:12]} "
          f"tree={recorded_tree[:12]} 落盘判定={doc.get('result')}")
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != recorded_sha:
        print(f"⚠ 当前 HEAD={head[:12]} ≠ 落盘 sha={recorded_sha[:12]}（落盘文件必然在其目标 sha 的"
              "子提交里）⇒ 本复核只证明「这些命令在当前工作树上也给出同样判定」，**不**等于复核了落盘那棵树。")
    print("─" * 72)
    bad: list[str] = []
    for lane in lanes:
        name = lane.get("name") or "?"
        recorded = lane.get("argv")
        if not recorded:
            bad.append(name)
            print(f"  {name:11s} FAIL  落盘里没有 argv（{lane.get('blocked') or '未记录原因'}）"
                  " ⇒ 复核不了就不放行")
            continue
        lane_argv = _expand_argv(recorded)
        env = dict(os.environ, **(lane.get("env") or {}))
        cwd = os.path.join(REPO_ROOT, lane.get("cwd") or ".")
        try:
            proc = subprocess.run(lane_argv, cwd=cwd, env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=LANE_TIMEOUT, check=False)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = 124
        except OSError:
            rc = 127
        status = "PASS" if rc == 0 else "FAIL"
        same = status == lane.get("status")
        if not same:
            bad.append(name)
        verdict = "一致" if same else f"不一致（落盘是 {lane.get('status')}）"
        print(f"  {name:11s} {status:4s} {verdict}")
    print("─" * 72)
    if bad:
        print(f"❌ 复核不一致：{', '.join(bad)} —— 该落盘读数**不可引用**")
        return 1
    print("✅ 复核一致：落盘里每条车道重跑得到相同判定（墙钟不参与比对）。")
    return 0


def usage() -> int:
    print(__doc__.strip())
    return 0


def main(argv: list[str]) -> int:
    _utf8_stdio()
    since, only, affected_rev, replay_path = "", "", "", ""
    record = True
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--since":
            since = rest.pop(0) if rest else ""
        elif arg == "--affected":
            affected_rev = rest.pop(0) if rest else ""
        elif arg == "--only":
            only = rest.pop(0) if rest else ""
        elif arg in ("-h", "--help"):
            return usage()
        elif arg == "--list":
            for lane in build_lanes(""):
                print(f"  {lane.name:11s} {lane.desc}")
            return 0
        elif arg == "--replay":
            replay_path = rest.pop(0) if rest else ""
        elif arg == "--no-record":
            record = False
        else:
            print(f"未知参数：{arg}\n")
            return usage() or 1

    if replay_path:
        return replay_reading(replay_path)

    if affected_rev:
        # 车道①（diff-check）与改动面报告都用同一区间，避免"受影响集合用 A、空白检查用 B"两套口径。
        # ⚠ 同时给了 `--since` 时也必须**统一到 `--affected` 的范围**：此前写成
        # `if affected_rev and not since:`，实测同一份输出里会同时出现 `受影响面（--affected A）`
        # 与 `改动面：…（B）` 两个范围（2026-09-22 审查实测）—— 正是本处要消灭的那种分裂。
        if since and _as_range(since) != _as_range(affected_rev):
            print(f"⚠ --since（{_as_range(since)}）与 --affected（{_as_range(affected_rev)}）范围不同："
                  "一律以 --affected 的范围为准（否则空白检查与受影响集合会变成两套口径）。")
        since = affected_rev

    lanes = build_lanes(since)

    # `--affected`：按机械映射把车道收敛到受影响集合。**默认路径完全不受影响**（不带该参数即全量）。
    affected: dict | None = None
    changed: list[str] = []
    if affected_rev:
        try:
            rows = parse_map()
        except (OSError, ValueError) as exc:
            print(f"❌ 读不到 / 解析不了 {MAP_PATH}：{exc}")
            return 1
        got = changed_files(affected_rev)
        if got is None:
            print(f"❌ 取不到改动面：git diff --name-only {affected_rev}..HEAD 失败")
            return 1
        changed = got
        affected = affected_summary(rows, changed)
        # 空改动面 ⇒ **fail-closed，不收敛**：否则 `--affected <刚提交的 sha>`（此时
        # `git diff --name-only <sha>..HEAD` 恰为空）会打印出 `Gate-0 PASS：0/0 通过` ——
        # 一个与真 PASS **不可区分**的绿（2026-09-22 审查实测）。那不是"没有受影响面"，
        # 是"这个范围里根本没有改动"，属于用错参数，必须响亮退回全量。
        if not affected["unmapped"] and changed:
            keep = set(affected["lanes"])
            lanes = [ln for ln in lanes if ln.name in keep]
        # else：fail-closed —— 有路径映射不到就**不动车道集合**（= 保持全量），只再加 focused。
        # 反过来写（if unmapped: pass / else: 收敛）是等价但更难读的写法，一次就够。
        focused_todo, manual_focused = focused_lanes(affected["focused"])
        lanes = lanes + focused_todo

    if only:
        lanes = [ln for ln in lanes if ln.name == only]
        if not lanes:
            print(f"未知车道：{only}")
            return 1

    head = git("rev-parse", "HEAD").stdout.strip()
    tree = git("rev-parse", "HEAD^{tree}").stdout.strip()
    print(f"Gate-0（推送前快速门禁）  tip={head[:12]}  tree={tree[:12]}  预算目标={GATE0_BUDGET:.0f}s")
    # 协议要求任何门禁读数都必须能指到"跑在哪棵树上（sha + ^{tree}）"，所以上面两行都在。
    print("─" * 72)

    if affected is not None:
        print(f"受影响面（--affected {affected_rev}）：改动 {len(changed)} 文件 → 命中 {len(affected['layers'])} 层")
        print(f"  · 受影响车道：{', '.join(affected['lanes']) or '（无）'}")
        inline_focused = [t for t in affected["focused"] if focused_runner(t) == "pytest"]
        print(f"  · 受影响 focused（本脚本内联跑 pytest 子集）：{', '.join(inline_focused) or '（无）'}")
        if manual_focused:
            print("  · ⚠ 受影响 focused **本脚本不内联**，需按 §2 命令人工跑（原因随项注明）：")
            for item in manual_focused:
                print(f"       - {item}")
        heavy = [x for x in affected["lanes"] if x in HEAVY_LANE_IDS]
        if heavy:
            print(f"  · ⚠ 受影响但**本脚本不跑**的重车道：{', '.join(heavy)}"
                  "（按 docs/agents/verification.md §2 的命令跑）")
        if affected["unproven"]:
            print(f"  · ⚠ unproven（blast-radius 阶梯 < 4：未跑真代码证明「其余车道不受影响」）："
                  f"{', '.join(affected['unproven'])}")
        if not changed:
            print("  · ⚠ 改动面为空（该范围没有任何改动文件）⇒ **fail-closed：跑全部车道**"
                  "（不收敛成 0 条 —— 否则会打印出与真 PASS 不可区分的「0/0 通过」）")
        if affected["unmapped"]:
            print(f"  · ❌ 未映射路径 {len(affected['unmapped'])} 个 ⇒ **fail-closed：跑全部车道**（请补 map）")
            for u in affected["unmapped"][:10]:
                print(f"       {u}")
        print("  · 边界：--affected 只用于**失败后的增量重跑**；不得替代推送前全量 Gate-0，"
              "也不得替代集成前完整门禁（协议 §8.8.4 第 1 行）。")
        print("─" * 72)

    results: list[tuple[Lane, int, float, str]] = []
    started = time.time()
    for idx, lane in enumerate(lanes, 1):
        if lane.argv is None:
            print(f"[{idx}/{len(lanes)}] {lane.name:11s} FAIL  工具缺失：{lane.blocked}")
            results.append((lane, 1, 0.0, f"工具缺失：{lane.blocked}"))
            continue
        rc, secs, out = run_lane(lane)
        print(f"[{idx}/{len(lanes)}] {lane.name:11s} {'PASS' if rc == 0 else 'FAIL'}  {secs:6.2f}s")
        results.append((lane, rc, secs, out))

    wall = time.time() - started
    for lane, rc, _secs, out in results:
        if rc == 0:
            continue
        print("─" * 72)
        print(f"▼ {lane.name} 失败（{lane.desc}）——输出尾部 {TAIL_LINES} 行：")
        lines = [ln for ln in out.split("\n") if ln.strip()]
        for line in lines[-TAIL_LINES:]:
            print(f"  {line}")
        if not lines:
            print("  (无输出)")

    print("─" * 72)
    print(surface_report(since))
    # ── 读数落盘（issue #293）：门禁读数的**唯一来源** ─────────────────────────────
    # 只在**裸全量**运行时落盘：`--since` / `--only` / `--affected` 都是局部读数（推送范围 / 单车道 /
    # 受影响面），写进同一个 `<sha>.json` 会把该树的全量结论覆写掉。落盘前还要求工作树干净（见下）。
    record_here = record and not only and not affected_rev and not since
    if record and not record_here:
        print("读数：本次是局部运行（--since / --only / --affected）"
              "⇒ **不落盘**（那不是全量门禁读数，避免覆写该树的全量结论）")
    if record_here:
        divergence = worktree_divergence()
        blocking = divergence["tracked"] + divergence["hidden"] + divergence["risky"]
        if blocking:
            # fail-closed：车道跑在**工作树**上，读数却只能记 `HEAD` 的 `sha` / `^{tree}`。
            print("❌ 读数落盘被拒：工作树与 HEAD 的偏离会**影响车道输入** ⇒ 车道是在**工作树**上"
                  "跑的，而读数只能记 HEAD 的 `sha` + `^{tree}`；")
            print("   写出去就成了「指到一棵没被测过的树」的读数（协议 §8.7 第 3 条）。")
            for label, key in (("追踪文件偏离", "tracked"),
                               ("assume-unchanged / skip-worktree 位", "hidden"),
                               ("未跟踪的**车道输入**（后缀命中）", "risky")):
                items = divergence[key]
                if not items:
                    continue
                print(f"   · {label}：{len(items)} 条")
                for ln in items[:6]:
                    print(f"       {ln}")
                if len(items) > 6:
                    print(f"       …（共 {len(items)} 条）")
            if divergence["untracked"]:
                print(f"   （另有 {len(divergence['untracked'])} 个未跟踪文件，后缀不命中车道输入"
                      " ⇒ 不阻断；它们会如实记进读数的 `worktree.untracked`）")
            print("   处置：先把这些改动落成 commit（**不要** `git stash`；`assume-unchanged` 位用"
                  " `git update-index --no-assume-unchanged <path>` 摘掉）再重跑；"
                  "只想看一眼、不落盘就加 `--no-record`。")
            return 1
        try:
            path = write_reading(head=head, tree=tree, argv=list(argv), wall=wall,
                                 results=results, affected=affected, changed=changed,
                                 worktree=divergence)
            print(f"读数已落盘：{_rel(path)}")
        except OSError as exc:
            # fail-closed：读数的唯一来源写不出来 ⇒ 这条"通过"不可引用（与"核对不了就不放行"同向）。
            print(f"❌ 读数落盘失败：{exc}")
            return 1
    if wall > GATE0_BUDGET:
        print(f"⚠ 超过预算目标：墙钟 {wall:.1f}s > {GATE0_BUDGET:.0f}s（预算只是目标，不改变上面的判定）")
    failed = [lane.name for lane, rc, _s, _o in results if rc != 0]
    if failed:
        print(f"Gate-0 FAIL：{len(results) - len(failed)}/{len(results)} 通过，墙钟 {wall:.1f}s")
        print(f"失败车道：{'、'.join(failed)}")
        print(f"增量验证：修好后只重跑那一条即可，例如  python scripts/gate0.py --only {failed[0]}")
        return 1
    print(f"Gate-0 PASS：{len(results)}/{len(results)} 通过，墙钟 {wall:.1f}s")
    print("提醒：Gate-0 只覆盖机械可判项；集成前仍须跑完整门禁（AGENTS.md §14.10）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
