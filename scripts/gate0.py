#!/usr/bin/env python3
"""gate0.py —— 推送前的快速门禁（Gate-0）。预算：**墙钟 ≤60 秒**。

## 它解决什么

SDD 的完整门禁（后端全量 `pytest` + 前端 `tsc/vitest/oxlint/playwright/build`）一次是
**分钟到十几分钟级**。问题是"改一个简单小功能也要等一小时"——其中大量时间花在**与本次改动
无关**的检查上。Gate-0 不是要取代完整门禁，而是把**廉价、机械、必判**的那部分提前到**推送前**：

与"整棵冻结树全量测试"无关，只跑能在几十秒内跑完、且**结果可机械判定**的车道（见 `LANES`）。

## 它**不是**什么（读这段，别误解）

- **不是安全边界**：pre-push hook 本地可 `--no-verify` 绕过，也不影响别的 clone（启用靠本地
  `git config core.hooksPath .githooks`，该配置**不随仓库分发**）。它挡的是"忘了跑"，不是"故意绕过"。
- **不取代** `AGENTS.md` §14.10 的集成前完整门禁，也不取代两轴独立审查。
- **不做按路径跳过**：全量 6 车道实测 ≈36–45s（2026-09-22 读数），已满足预算；按改动路径跳过某条
  车道属于**放松**（跨层影响难以穷举），机制上没有必要。改动面只作**信息展示**，不影响跑什么。
- **不产生"读数"**：本脚本只回退出码与人的可读输出，**不写任何文件**（机器产出的门禁读数
  `docs/gate/<sha>.json` 计划在批 3）。

## 用法 / 退出码

    python scripts/gate0.py                  # 跑全部车道；0 = 全绿，1 = 有失败或工具缺失
    python scripts/gate0.py --since <rev>    # 额外检查 <rev>..HEAD 的空白/冲突标记，并报告改动面
    python scripts/gate0.py --only ruff       # 只重跑一条（失败后增量验证用，别整条流水线重跑）
    python scripts/gate0.py --list           # 列车道名

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

#: 单条车道的墙上限。不是预算（预算是 60s），而是"卡死也要有个结论"的兜底：
#: pre-push hook 里挂死会让人推不动代码，超时按失败处理并打印出来。
LANE_TIMEOUT = 300.0

#: 生成物同步守卫：`src/.../event.py`（词汇唯一真值）↔ `web/src/generated/event-types.ts`
#: ↔ `docs/EVENT_VOCABULARY.md`。共 2 文件 6 例。
GUARD_TESTS = (
    "tests/test_event_types_generated.py",
    "tests/test_event_vocabulary_generated.py",
)

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


def node_exe() -> str | None:
    return shutil.which("node")


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


def build_lanes(since: str) -> list[Lane]:
    lanes: list[Lane] = []

    # ① 空白 / 冲突标记：AGENTS.md §14.10 的固定项。给了 --since 就查提交范围，
    #    否则查工作树（未提交改动）。
    diff_args = ["diff", "--check"] + ([f"{since}..HEAD"] if since else [])
    lanes.append(Lane("diff-check", f"git diff --check {' '.join(diff_args[2:])}".strip(),
                      ["git", *diff_args], REPO_ROOT))

    # ② 后端静态检查：仓库既有的门禁命令就是 `ruff check .`（协议 §7 第 6 条）。
    ruff = venv_exe("ruff")
    lanes.append(Lane("ruff", "ruff check .", [ruff, "check", "."] if ruff else None,
                      REPO_ROOT, blocked="找不到 ruff（先 `uv sync` / 装依赖）"))

    # ③④ 前端静态检查：直接调包的 .js 入口，不经过 `npm`/`npx`/`.bin/*.cmd`——
    #     本机沙箱把 `cmd.exe` 拉黑（`.cmd` 秒退且报错字节是 GBK 的"拒绝访问。"），
    #     而 `.bin/oxlint` 是 POSIX sh 脚本、会用到 `dirname`/`sed`（shim 缺 coreutils）。
    node = node_exe()
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
        "guards", "pytest 生成物同步守卫（2 文件 6 例）",
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
    """改动面（**信息展示**，不影响跑什么）。`--since` 缺省时退化为工作树未提交改动。"""
    if since:
        proc = git("diff", "--name-only", f"{since}..HEAD")
        scope = f"{since}..HEAD"
    else:
        proc = git("status", "--porcelain")
        scope = "工作树（未提交）"
    files = [ln.strip() for ln in proc.stdout.split("\n") if ln.strip()]
    if proc.returncode != 0:
        return f"改动面：?（{scope} 取不到）"
    if not since:
        files = [f[3:] if len(f) > 3 else f for f in files]  # 去掉 porcelain 状态两列
    buckets: dict[str, int] = {}
    for path in files:
        top = path.split("/")[0]
        buckets[top] = buckets.get(top, 0) + 1
    if not buckets:
        return f"改动面：0 文件（{scope}）"
    shown = "  ".join(f"{k} {v}" for k, v in sorted(buckets.items(), key=lambda kv: -kv[1]))
    return f"改动面：{len(files)} 文件（{scope}）— {shown}"


def usage() -> int:
    print(__doc__.strip())
    return 0


def main(argv: list[str]) -> int:
    _utf8_stdio()
    since, only = "", ""
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--since":
            since = rest.pop(0) if rest else ""
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

    lanes = build_lanes(since)
    if only:
        lanes = [ln for ln in lanes if ln.name == only]
        if not lanes:
            print(f"未知车道：{only}")
            return 1

    head = git("rev-parse", "HEAD").stdout.strip()
    tree = git("rev-parse", "HEAD^{tree}").stdout.strip()
    print(f"Gate-0（推送前快速门禁）  tip={head[:12]}  tree={tree[:12]}  预算=60s")
    # 协议要求任何门禁读数都必须能指到"跑在哪棵树上（sha + ^{tree}）"，所以上面两行都在。
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
