"""#1353 AC11：门禁读数必须机器落盘、可指到树，e2e 全量套件的输入必须真的在仓库里。

AC11 要求交付时给出 gate0 6/6、pytest 全量 `-n 4`、Playwright 全量的**实际数字**。
那些读数在交付时跑；本文件锁住读数赖以成立的**结构**，防止「数字是抄的 / 树对不上」：

1. Gate-0 的六条机械车道由 `scripts/gate0.py` 声明（不是人写在文档里的清单）；
2. 收据 schema 带 `sha` / `tree` / `lanes` / `wall_seconds` / `tool_versions`，
   且记录的 `tree` 等于 `git rev-parse <sha>^{tree}`（协议 §8.7 第 3 条：读数必须能指到树）；
3. e2e 车道的输入（playwright 配置 + 全量 spec + package 脚本）存在。

本文件只读仓库，不跑任何门禁。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE0 = REPO_ROOT / "scripts" / "gate0.py"
RECEIPTS = REPO_ROOT / "docs" / "gate"
WEB = REPO_ROOT / "web"

#: 协议 §14.10 / `docs/agents/verification.md` §2 的机械车道全集（顺序即 `build_lanes` 的构造顺序）。
EXPECTED_LANES = ("diff-check", "ruff", "oxlint", "tsc", "guards", "coverage")

_REQUIRED_RECEIPT_KEYS = ("sha", "tree", "lanes", "wall_seconds", "tool_versions")


def _load_gate0():
    spec = importlib.util.spec_from_file_location("_gate0_1353_probe", GATE0)
    assert spec is not None and spec.loader is not None, f"无法加载 {GATE0}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} rc={proc.returncode}: {proc.stderr}"
    return proc.stdout.strip()


def _receipts() -> list[Path]:
    return sorted(RECEIPTS.glob("*.json"))


def test_gate0_declares_exactly_the_six_mechanical_lanes():
    gate0 = _load_gate0()

    declared = tuple(gate0.GATE0_LANE_IDS)
    assert len(declared) == 6, f"Gate-0 应为六条机械车道，实际 {declared!r}"
    assert set(declared) == set(EXPECTED_LANES), f"车道集合漂移：{declared!r}"

    built = gate0.build_lanes("")
    assert [lane.name for lane in built] == list(EXPECTED_LANES), (
        f"build_lanes 与声明不一致：{[lane.name for lane in built]!r}"
    )
    for lane in built:
        assert lane.desc.strip(), f"{lane.name} 缺少说明"
        # 每条车道要么有可执行 argv，要么给出机械可读的 blocked 理由；
        # 两者皆空等于一条永远不会跑、也不会报错的「绿」。
        assert lane.argv is not None or lane.blocked.strip(), f"{lane.name} 既无 argv 也无 blocked 理由"


def test_gate_receipts_are_machine_recorded_and_point_at_their_tree():
    paths = _receipts()
    assert paths, f"{RECEIPTS} 下没有任何 Gate-0 收据：AC11 的读数将无处可指"

    for path in paths:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        missing = [key for key in _REQUIRED_RECEIPT_KEYS if key not in receipt]
        assert not missing, f"{path.name} 缺字段 {missing!r}"

        assert receipt["schema"] == 1, f"{path.name} schema={receipt['schema']!r}"
        assert receipt["gate"] == "gate0", f"{path.name} gate={receipt['gate']!r}"

        sha = str(receipt["sha"])
        tree = str(receipt["tree"])
        assert len(sha) == 40 and len(tree) == 40, f"{path.name} 的 sha/tree 不是完整对象 ID"
        assert path.stem == sha, f"{path.name} 的文件名与 sha 不一致"

        lanes = receipt["lanes"]
        assert isinstance(lanes, list) and lanes, f"{path.name} 没有车道读数"
        for lane in lanes:
            assert lane.get("name"), f"{path.name} 有车道缺 name"
            assert lane.get("status") in {"PASS", "FAIL"}, f"{path.name} 车道状态非法：{lane!r}"
            assert isinstance(lane.get("rc"), int), f"{path.name}/{lane.get('name')} 缺 rc"
            # 收据的契约：能跑的车道记 argv（`--replay` 靠它原样重跑），
            # 工具缺失的车道记 blocked 理由 —— 两者皆空等于「不可复核的读数」。
            if lane.get("argv") is None:
                assert lane.get("blocked"), f"{path.name}/{lane.get('name')} 无 argv 也无 blocked 理由"
            else:
                assert isinstance(lane["argv"], list), f"{path.name}/{lane.get('name')} 的 argv 形状非法"

        assert isinstance(receipt["wall_seconds"], (int, float))
        versions = receipt["tool_versions"]
        assert isinstance(versions, dict) and versions, f"{path.name} 缺工具版本"

    # 抽样核对「读数能指到树」：收据里记的 tree 必须等于该 sha 的真实 tree。
    # 只抽样（不跑 997 次 git）——格式与字段已在上面的全量循环里覆盖。
    sample = [paths[0], paths[len(paths) // 2], paths[-1]]
    for path in sample:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        actual = _git("rev-parse", f"{receipt['sha']}^{{tree}}")
        assert actual == receipt["tree"], (
            f"{path.name} 记录的 tree 与 {receipt['sha']} 的真实 tree 不一致："
            f"{receipt['tree']} != {actual}"
        )


def test_playwright_full_suite_inputs_exist():
    config = WEB / "playwright.config.ts"
    assert config.is_file(), f"缺 e2e 配置：{config}"
    config_text = config.read_text(encoding="utf-8")
    assert "testDir: './e2e'" in config_text, "playwright 配置的 testDir 不再是 ./e2e"

    specs = sorted((WEB / "e2e").glob("*.spec.ts"))
    assert len(specs) >= 60, f"e2e 全量套件只剩 {len(specs)} 个 spec（基线 66）"

    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    e2e_script = package.get("scripts", {}).get("test:e2e", "")
    assert "playwright test" in e2e_script, f"package.json 的 test:e2e 不再是 playwright test：{e2e_script!r}"
