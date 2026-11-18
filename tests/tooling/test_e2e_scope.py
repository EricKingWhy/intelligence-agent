"""`scripts/e2e_scope.py` + `docs/agents/e2e_scope_map.tsv` 的守卫（issue #1500）。

三个反向用例就是票面 AC3 要求「误选/漏选能变红」的那三条；其余几条守表与选择器的自洽
（表里点名的 spec 必须在盘上、常驻集不得被相关性逻辑吞掉、fail-closed 只对产品面生效）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "e2e_scope.py"
MAP = REPO / "docs" / "agents" / "e2e_scope_map.tsv"

RESIDENT_14 = {
    "task-review.spec.ts", "u-project-task.spec.ts", "control-row.spec.ts",
    "context-usage.spec.ts", "y-inspector-peek.spec.ts", "w-session-delete.spec.ts",
    "e-reconnect.spec.ts", "d-recover.spec.ts", "stream-fallback.spec.ts",
    "n-approval-card.spec.ts", "composer-stream-actions.spec.ts", "queue-flush.spec.ts",
    "multiturn-queue.spec.ts", "keep-text-on-reject.spec.ts",
}


def _run(*changed: str) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(SCRIPT)]
    for path in changed:
        args += ["--changed-file", path]
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=False)


def _specs(result: subprocess.CompletedProcess[str]) -> list[str]:
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("SPECS: "))
    return line[len("SPECS: "):].split()


def _json(*changed: str) -> dict:
    args = [sys.executable, str(SCRIPT), "--json"]
    for path in changed:
        args += ["--changed-file", path]
    result = subprocess.run(args, cwd=REPO, capture_output=True, text=True, check=False)
    return json.loads(result.stdout)


def test_docs_only_change_still_selects_resident_14() -> None:
    """反向用例 ②：纯 docs 改动 → 只跑常驻 14（workflow 的 paths-ignore 会让它连触发都没有）。"""
    result = _run("docs/PHASE_STATUS.md")
    assert result.returncode == 0, result.stderr
    assert {Path(s).name for s in _specs(result)} == RESIDENT_14


def test_cold_zone_change_selects_its_mapped_spec() -> None:
    """反向用例 ①：冷区（只被某一行映射）改动 → 该行的 spec 必须进集合。"""
    result = _run("web/src/components/KanbanPanel.tsx")
    assert result.returncode == 0, result.stderr
    names = {Path(s).name for s in _specs(result)}
    assert {"kanban.spec.ts", "kanban-ws.spec.ts"} <= names
    assert RESIDENT_14 <= names
    # 不相干的面不得被带进来（否则「按相关性扩围」退化成全量）。
    assert "recovery-w13.spec.ts" not in names


def test_hot_zone_without_a_map_row_fails_closed() -> None:
    """反向用例 ③：`web/src/**` 的产品文件没有映射行 ⇒ 退出 2（静默少跑比多跑贵）。"""
    result = _run("web/src/components/DefinitelyNotMapped.tsx")
    assert result.returncode == 2
    assert "DefinitelyNotMapped.tsx" in result.stderr
    # 「无 SPECS 行」本身就是失败信号（复审 P4-4）：忽略退出码的调用方也拿不到「仅常驻」。
    assert not any(ln.startswith("SPECS: ") for ln in result.stdout.splitlines())


def test_unmapped_web_src_test_file_only_warns() -> None:
    """`web/src/**` 的**测试**文件未映射 ⇒ 退出 0 + 常驻 14 + warning（复审第 1 轮 P2-1）。"""
    result = _run("web/src/components/LegacyWidget.test.tsx")
    assert result.returncode == 0, result.stderr
    assert {Path(s).name for s in _specs(result)} == RESIDENT_14
    assert "[warning]" in result.stdout
    assert "LegacyWidget.test.tsx" in result.stdout


def test_unmapped_web_public_and_docs_only_warn() -> None:
    """`web/public/**` 与 `web/*.md` 未映射 ⇒ 退出 0 + 常驻 14（不因静态资源/文档变红）。"""
    for path in ("web/public/logo-fixture.png", "web/README.md"):
        result = _run(path)
        assert result.returncode == 0, (path, result.stderr)
        assert {Path(s).name for s in _specs(result)} == RESIDENT_14, path


def test_unmapped_perf_probe_script_only_warns() -> None:
    """`web/scripts/perf-*.mjs` 是性能探针、无 e2e 引用 ⇒ 未映射只 warning，不选全量。"""
    payload = _json("web/scripts/perf-pulse-cost.mjs")
    assert payload["unmapped_product"] == []
    assert payload["unmapped_other"] == ["web/scripts/perf-pulse-cost.mjs"]
    assert {Path(s).name for s in payload["selected"]} == RESIDENT_14


def test_preflight_port_is_part_of_the_global_surface() -> None:
    """`web/scripts/preflight-port.mjs` 是 playwright 配置直接 import 的运行前置 ⇒ 选中全部。"""
    payload = _json("web/scripts/preflight-port.mjs")
    assert payload["unmapped_other"] == []
    assert len(payload["selected"]) > len(RESIDENT_14)


def test_spec_file_change_selects_every_mapped_spec() -> None:
    """改 spec 自己（`web/e2e/**`）⇒ 经全局面前缀选中表内全部 18 支（复审 P4-5a）。"""
    payload = _json("web/e2e/kanban.spec.ts")
    assert payload["unmapped_product"] == []
    assert {Path(s).name for s in payload["selected"]} >= RESIDENT_14 | {
        "kanban.spec.ts", "kanban-ws.spec.ts", "memory-management.spec.ts", "recovery-w13.spec.ts",
    }


def test_duplicate_specs_are_deduped_and_resident_never_dropped() -> None:
    """多个改动命中同一支 spec、且命中常驻集时，集合去重且常驻不丢。

    走 `--changed-list -`（stdin）——工作流消费的正是这条路径。
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "--changed-list", "-"],
        cwd=REPO, capture_output=True, text=True, check=False,
        input="web/src/App.tsx\nweb/src/index.css\nweb/src/components/MemoryPanel.tsx\n",
    )
    payload = json.loads(result.stdout)
    selected = payload["selected"]
    assert len(selected) == len(set(selected))
    assert {Path(s).name for s in selected} >= RESIDENT_14
    assert "web/e2e/memory-management.spec.ts" in selected


def test_every_spec_in_the_map_exists_on_disk() -> None:
    """表里点名的 spec 必须在 `web/e2e/` 里真实存在（防手误让 CI 跑空集）。"""
    selected = _json("web/src/App.tsx")["selected"]
    missing = [s for s in selected if not (REPO / s).is_file()]
    assert not missing, missing


def test_unreadable_map_is_an_error_not_an_empty_selection() -> None:
    """表坏了 → 退出 1；不许读成「没有受影响项」再静默跑 0 支。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--map", str(REPO / "docs" / "agents" / "nope.tsv")],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "映射表不可用" in result.stderr


def test_unreadable_changed_list_is_an_error_not_a_traceback() -> None:
    """`--changed-list` 指向不存在的文件 → 退出 1 + 人读报错，不许裸 traceback（复审 P3-2）。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--changed-list", str(REPO / "no-such-changed.txt")],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "e2e-scope: 改动清单不可用" in result.stderr
    assert "Traceback" not in result.stderr


def test_workflow_selects_instead_of_hardcoding_and_keeps_being_optional() -> None:
    """workflow 侧的三条硬口径：不写死 spec、保留 docs 跳过、仍是非必需检查。

    `paths-ignore` 与「非必需」是同一件事的两面（被跳过的必需检查会永久 pending），
    拆开任何一半都会卡死纯文档 PR。
    """
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "e2e-smoke.yml").read_text(encoding="utf-8"))
    # PyYAML 走 YAML 1.1：顶层键 `on:` 被解析成布尔 `True`，故这里用 `True` 取 `on.pull_request`。
    assert workflow[True]["pull_request"]["paths-ignore"] == ["docs/**"]
    run_lines = "\n".join(step.get("run", "") for step in workflow["jobs"]["e2e-smoke"]["steps"])
    assert "scripts/e2e_scope.py" in run_lines
    assert "steps.scope.outputs.specs" in run_lines
    # 常驻 14 支只能出现在映射表里，不能再写死在 run 行。
    assert not any(name in run_lines for name in RESIDENT_14)
