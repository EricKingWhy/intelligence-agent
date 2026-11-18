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

## 三条行为

1. **常驻集合永不消失**：无论改动面是什么，表里 `surface=ALWAYS` 的那些 spec 一定被选中。
2. **冷区要选中**：`web/src/**` 的路径落到某行 surface ⇒ 该行的 spec 进集合。
3. **热区漏配要红，但只对产品面**（复审第 1 轮 P2-1 裁决）：`web/src/**` 下**未映射的非测试
   文件** ⇒ 退出码 2 并点名（`unmapped_product`）。其余未映射的 `web/**`（`*.test.ts(x)`、
   文档、`public/`、`e2e-live/` 等）只记 warning（`unmapped_other`）、跑常驻 14、退出码 0。
   理由：实测 `git ls-files web/` 498 个跟踪文件里 396 个未映射（241 个非产品面 + 155 个
   产品面），若一律 fail-closed，改一个 vitest 用例或 `web/README.md` 都会让 `e2e-smoke` 常态
   变红 —— routine 的红会把真漏选的红一起淹掉。产品面（`web/src/**` 非测试）才代表
   「用户能看到的行为变了却没跑对应 spec」。

非 `web/**` 的改动（`src/**`、`tests/**`、`docs/**` …）不进 e2e 选测面：e2e 是前端浏览器车道，
后端/文档改动不改变它的受测树。纯 `docs/**` 的 PR 连工作流都不会触发（`paths-ignore` 在原位）。

## 用法

    python scripts/e2e_scope.py --changed-file web/src/components/KanbanPanel.tsx
    git diff --name-only origin/main...HEAD | python scripts/e2e_scope.py --changed-list -

输出一段人读的块 + 最后一行 `SPECS: <空格分隔的 spec 路径>`（工作流只消费这一行）。
**有 `unmapped_product` 时不打印 `SPECS:` 行**（否则忽略退出码的调用方会把「仅常驻」当成
选测结果 —— 复审 P4-4）。`--json` 改为输出机器可读对象（测试用）。

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

#: `web/src/**` 下的测试/夹具不算产品面：改它们不改变用户可见行为（复审 P2-1）。
_TEST_MARKERS = (".test.", ".spec.")


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


def _is_product_surface(path: str) -> bool:
    """`web/src/**` 的非测试文件 = 用户能感知的行为面 ⇒ 未映射时 fail-closed。"""
    if not path.startswith("web/src/"):
        return False
    name = path.rsplit("/", 1)[-1]
    return (
        not any(marker in name for marker in _TEST_MARKERS)
        and "/__tests__/" not in path
        and "/__fixtures__/" not in path
    )


def _uniq(items: list[str]) -> list[str]:
    """去重保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def evaluate(paths: list[str], rows: list[dict[str, list[str]]]) -> dict:
    """返回 {resident, selected, matched, unmapped_product, unmapped_other}，均去重保序。

    `unmapped_product` ⇒ 调用方退出 2；`unmapped_other` 只是 warning（复审 P2-1）。
    """
    resident: list[str] = []
    for row in rows:
        if ALWAYS in row["surface"]:
            resident.extend(_norm(s) for s in row["specs"])
    resident = _uniq(resident)

    # `ALL` 是保留字，不是 spec；展开时用它代表「表里出现过的每一支」。
    every: list[str] = []
    for row in rows:
        for spec in row["specs"]:
            if spec == ALL:
                continue
            every.append(_norm(spec))
    every = _uniq(every)

    matched: list[tuple[str, list[str]]] = []
    unmapped_product: list[str] = []
    unmapped_other: list[str] = []
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
        hit = _uniq(hit)
        if not hit:
            (unmapped_product if _is_product_surface(path) else unmapped_other).append(path)
            continue
        matched.append((path, hit))
        selected.extend(s for s in hit if s not in selected)

    return {
        "resident": resident,
        "selected": selected,
        "matched": [{"path": p, "specs": s} for p, s in matched],
        "unmapped_product": unmapped_product,
        "unmapped_other": unmapped_other,
    }


def _read_changed_list(value: str) -> list[str]:
    """`--changed-list` 的输入源；文件不可读时给人读报错（复审 P3-2）。"""
    if value == "-":
        return [ln.strip() for ln in sys.stdin if ln.strip()]
    try:
        text = Path(value).read_text(encoding="utf-8")
    except (MapError, OSError) as exc:
        raise MapError(f"改动清单不可用：{value}（{exc}）") from exc
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按改动面算出 PR 级 e2e 选测集合（#1500）")
    ap.add_argument("--map", default=str(DEFAULT_MAP), help="映射表路径")
    ap.add_argument("--changed-file", action="append", default=[], help="改动文件（可重复）")
    ap.add_argument("--changed-list", help="从文件读改动文件（`-` = stdin，一行一个）")
    ap.add_argument("--json", action="store_true", help="输出机器可读对象")
    args = ap.parse_args(argv)

    paths = list(args.changed_file)
    if args.changed_list:
        try:
            paths.extend(_read_changed_list(args.changed_list))
        except MapError as exc:
            print(f"e2e-scope: {exc}", file=sys.stderr)
            return EXIT_MAP_ERROR

    try:
        rows = parse_map(Path(args.map))
    except (MapError, OSError) as exc:
        print(f"e2e-scope: 映射表不可用：{exc}", file=sys.stderr)
        return EXIT_MAP_ERROR

    result = evaluate(paths, rows)
    result["changed"] = paths

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
        if result["unmapped_other"]:
            print("[warning] 以下 web/** 改动没有映射行，不进 e2e 选测面（只跑常驻）：")
            for p in result["unmapped_other"]:
                print(f"  ? {p}")
        if not result["unmapped_product"]:
            # `SPECS:` 是工作流唯一消费的一行；路径相对 `web/`（playwright 的 cwd），
            # 免得 workflow 里再做一轮字符串手术。**有未映射产品面时不打印**（复审 P4-4）。
            print("SPECS: " + " ".join(s.removeprefix("web/")
                                      for s in result["selected"]))

    if result["unmapped_product"]:
        print(
            "e2e-scope: 改动面里有 web/src/** 的产品路径没有任何映射行覆盖 ⇒ fail-closed"
            "（不静默少跑）：\n  "
            + "\n  ".join(result["unmapped_product"])
            + "\n  处置：在 docs/agents/e2e_scope_map.tsv 补一行 surface/specs（并先做那支 spec 的定性）。",
            file=sys.stderr,
        )
        return EXIT_UNMAPPED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
