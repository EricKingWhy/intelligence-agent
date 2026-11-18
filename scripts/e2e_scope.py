#!/usr/bin/env python3
"""e2e_scope.py —— 由改动面算出 PR 级 e2e 选测集合（issue #1500）。

## 它解决什么

`e2e-smoke.yml` 原先把 14 个 spec 文件名写死在 run 行：PR 只跑常驻 14，其余 60 支
只在夜间跑。本脚本把「路径 → spec」的映射从记忆变成可被 CI 消费的表
（`docs/agents/e2e_scope_map.tsv`），输出 = 常驻集合 ∪ 映射命中的集合（去重）。

## 为什么不是 Playwright 的 `--only-changed`

`--only-changed [ref]` 只取「HEAD 与 ref 之间被改的**测试文件**」——它不会把
「改了 `KanbanPanel.tsx`」翻译成「跑 `kanban.spec.ts`」。本仓要的是后者，映射仍由本仓持有
（与裁决 12 的 A 案一致）。官方语义见
https://playwright.dev/docs/test-cli （2026-11-18 联网核对原文）。

## 三条行为（与仓库既有 fail-closed 口径一致）

1. **常驻集合永不消失**：无论改动面是什么，表里 `surface=ALWAYS` 的那些 spec 一定被选中。
2. **冷区要选中**：`web/src/**` 的路径落到某行 surface ⇒ 该行的 spec 进集合。
3. **热区漏配要红**：改动面里出现 `web/**` 但**没有任何行命中**的路径 ⇒ 退出码 2，
   并点名该路径。静默少跑比多跑更贵 —— 与 `gate0.py --affected` 的未映射路径同一口径。

非 `web/**` 的改动（`src/**`、`tests/**`、`docs/**` …）不进 e2e 选测面：e2e 是前端浏览器车道，
后端/文档改动不改变它的受测树。纯 `docs/**` 的 PR 连工作流都不会触发（`paths-ignore` 在原位）。

## 用法

    python scripts/e2e_scope.py --changed-file web/src/components/KanbanPanel.tsx
    git diff --name-only origin/main...HEAD | python scripts/e2e_scope.py --changed-list -

输出一段人读的块 + 最后一行 `SPECS: <空格分隔的 spec 路径>`（工作流只消费这一行）。
`--json` 改为输出机器可读对象（测试用）。

## 表格式

`docs/agents/e2e_scope_map.tsv`：`surface` / `specs` / `why` / `triage_ref` / `baseline_ref`。
`surface` 为 `|` 分隔的**精确路径**或**以 `/` 结尾的路径前缀**（禁通配、禁 catch-all，与
`verification.map.tsv` 同形）；`ALWAYS` 是唯一的保留字，表示常驻集合。
`specs` 里可以写 `ALL`，表示表里出现过的每一支 spec（用于 fixtures / 配置这类全局面）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = REPO_ROOT / "docs" / "agents" / "e2e_scope_map.tsv"

COLUMNS = ("surface", "specs", "why", "triage_ref", "baseline_ref")
ALWAYS = "ALWAYS"
ALL = "ALL"

EXIT_OK = 0
EXIT_MAP_ERROR = 1
EXIT_UNMAPPED = 2


class MapError(Exception):
    """映射表结构不合法。fail-closed：调用方不得把这张表读成「没有受影响项」。"""


def _split(cell: str) -> list[str]:
    return [x.strip() for x in cell.split("|") if x.strip()]


def parse_map(path: Path) -> list[dict[str, list[str]]]:
    """读表。注释行（`#`）与表头跳过；列数不对直接抛错。"""
    rows: list[dict[str, list[str]]] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cells = line.split("\t")
            if tuple(cells) == COLUMNS:
                continue
            if len(cells) != len(COLUMNS):
                raise MapError(f"{path.name}:{lineno} 列数 {len(cells)} != {len(COLUMNS)}")
            rows.append({
                "surface": _split(cells[0]),
                "specs": _split(cells[1]),
                "why": [cells[2]],
                "triage_ref": [cells[3]],
                "baseline_ref": [cells[4]],
            })
    if not rows:
        raise MapError(f"{path.name} 没有数据行")
    return rows


def _norm(spec: str) -> str:
    """表里的 spec 可以写裸名（`kanban.spec.ts`），输出统一成 `web/e2e/<name>`。"""
    if "/" in spec:
        return spec
    return f"web/e2e/{spec}"


def _matches(surface: str, path: str) -> bool:
    """与 `scripts/gate0.py::map_matches` 同语义：前缀须以 `/` 结尾，其余按精确路径。"""
    if surface.endswith("/"):
        return path.startswith(surface) or path.rstrip("/") == surface.rstrip("/")
    return path == surface


def evaluate(paths: list[str], rows: list[dict[str, list[str]]]) -> dict:
    """返回 {resident, selected, matched, unmapped}；selected 已去重且保序。"""
    resident: list[str] = []
    for row in rows:
        if ALWAYS in row["surface"]:
            resident.extend(_norm(s) for s in row["specs"])

    # `ALL` 是保留字，不是 spec；展开时用它代表「表里出现过的每一支」。
    every = []
    for row in rows:
        for spec in row["specs"]:
            if spec == ALL:
                continue
            spec = _norm(spec)
            if spec not in every:
                every.append(spec)

    matched: list[tuple[str, list[str]]] = []
    unmapped: list[str] = []
    selected: list[str] = list(resident)

    for path in paths:
        if not path.startswith("web/"):
            continue
        # `ALL` 展开成表里出现过的每一支（含常驻）。
        hit: list[str] = []
        for row in rows:
            if any(_matches(s, path) for s in row["surface"] if s != ALWAYS):
                for spec in row["specs"]:
                    hit.extend(every if spec == ALL else [_norm(spec)])
        if not hit:
            unmapped.append(path)
            continue
        matched.append((path, hit))
        for spec in hit:
            if spec not in selected:
                selected.append(spec)

    return {
        "resident": resident,
        "selected": selected,
        "matched": [{"path": p, "specs": s} for p, s in matched],
        "unmapped": unmapped,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按改动面算出 PR 级 e2e 选测集合（#1500）")
    ap.add_argument("--map", default=str(DEFAULT_MAP), help="映射表路径")
    ap.add_argument("--changed-file", action="append", default=[], help="改动文件（可重复）")
    ap.add_argument("--changed-list", help="从文件读改动文件（`-` = stdin，一行一个）")
    ap.add_argument("--json", action="store_true", help="输出机器可读对象")
    args = ap.parse_args(argv)

    paths = list(args.changed_file)
    if args.changed_list:
        if args.changed_list == "-":
            paths.extend(ln.strip() for ln in sys.stdin if ln.strip())
        else:
            paths.extend(ln.strip() for ln in Path(args.changed_list).read_text(encoding="utf-8").splitlines()
                         if ln.strip())

    try:
        rows = parse_map(Path(args.map))
    except (MapError, OSError) as exc:
        print(f"e2e-scope: 映射表不可用：{exc}", file=sys.stderr)
        return EXIT_MAP_ERROR

    result = evaluate(paths, rows)
    result["changed"] = paths
    result["unmapped"] = result["unmapped"]

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"changed files ({len(paths)}):")
        for p in paths:
            print(f"  - {p}")
        print(f"resident ({len(result['resident'])}): {' '.join(result['resident'])}")
        for item in result["matched"]:
            print(f"matched  {item['path']} -> {' '.join(item['specs'])}")
        print(f"selected after dedup ({len(result['selected'])}):")
        for spec in result["selected"]:
            print(f"  * {spec}")
        # `SPECS:` 是工作流唯一消费的一行；路径相对 `web/`（playwright 的 cwd），
        # 免得 workflow 里再做一轮字符串手术。
        print("SPECS: " + " ".join(s.removeprefix("web/")
                                  for s in result["selected"]))

    if result["unmapped"]:
        print(
            "e2e-scope: 改动面里有 web/** 路径没有任何映射行覆盖 ⇒ fail-closed（不静默少跑）：\n  "
            + "\n  ".join(result["unmapped"])
            + "\n  处置：在 docs/agents/e2e_scope_map.tsv 补一行 surface/specs（并先做那支 spec 的定性）。",
            file=sys.stderr,
        )
        return EXIT_UNMAPPED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
