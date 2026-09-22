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
- **不产生"读数"**：本脚本只回退出码与人的可读输出，**不写任何文件**（机器产出的门禁读数
  `docs/gate/<sha>.json` 计划在批 3）。

## 用法 / 退出码

    python scripts/gate0.py                    # 跑全部车道（默认）；0 = 全绿，1 = 有失败或工具缺失
    python scripts/gate0.py --since <rev>      # 额外检查 <rev>..HEAD 的空白/冲突标记，并报告改动面
    python scripts/gate0.py --affected <rev>   # 只跑 <rev>..HEAD 改动面**受影响**的车道 + focused 用例
                                               # （`<rev>` 也可写成范围 `A..B`；仅用于失败后的增量重跑；
                                               #  未映射路径 ⇒ fail-closed 全量）
    python scripts/gate0.py --only ruff        # 只重跑一条（失败后增量验证用，别整条流水线重跑）
    python scripts/gate0.py --list             # 列车道名

**fail-closed**：任何车道因"工具缺失 / 超时 / 无法执行"而没能得到结论，一律算**失败**，
不算"跳过"，不算通过（沿用 `check_review_coverage.sh` 的"核对不了就不放行"口径）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

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
        return "前端/浏览器重车道，按 §2 命令跑；vitest 在干净 HEAD 上就有红 ⇒ 只看失败集合差集"
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


def usage() -> int:
    print(__doc__.strip())
    return 0


def main(argv: list[str]) -> int:
    _utf8_stdio()
    since, only, affected_rev = "", "", ""
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
        else:
            print(f"未知参数：{arg}\n")
            return usage() or 1

    if affected_rev and not since:
        # 车道①（diff-check）与改动面报告都用同一区间，避免"受影响集合用 A、空白检查用 B"两套口径。
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
        if not affected["unmapped"]:
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
