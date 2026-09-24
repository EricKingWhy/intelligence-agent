"""CLI 面（`#307` AC「专用命令」）：退出码与"未知名场景 fail-fast"。

退出码是脚本化的唯一接口（人眼只看终端）：0 = PASS / 复核通过；1 = 未通过（含 BLOCKED /
SKIPPED）；2 = 用法或环境错误。把 BLOCKED 报成 0 就等于把"没跑"卖成"跑过了"。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from evaluation.live_gate import repo
from tests.live_gate._evidence_factory import make_evidence


def _load_cli():
    """按路径装载 `scripts/live_gate.py`（`scripts/` 不是包，与 `repo._gate0()` 同一手法）。"""
    path = repo.REPO_ROOT / "scripts" / "live_gate.py"
    spec = importlib.util.spec_from_file_location("_live_gate_cli_for_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_list_registers_the_builtin_smoke_scenario(capsys) -> None:
    cli = _load_cli()
    assert cli.main(["list"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "smoke-production-tools" in out
    assert "v1" in out


def test_unknown_scenario_is_a_usage_error_without_any_request(capsys) -> None:
    cli = _load_cli()
    assert cli.main(["run", "--scenario", "no-such-scenario"]) == cli.EXIT_USAGE
    assert "用法错误" in capsys.readouterr().err


def test_missing_evidence_file_is_a_usage_error(capsys, tmp_path: Path) -> None:
    cli = _load_cli()
    assert cli.main(["validate", str(tmp_path / "nope.json")]) == cli.EXIT_USAGE
    assert "用法错误" in capsys.readouterr().err


def test_validate_returns_zero_for_a_consistent_evidence(tmp_path: Path, capsys) -> None:
    """只用机制夹具（无轨迹引用 ⇒ 轨迹项不通过）——这里判的是退出码口径，不是证据质量。"""
    cli = _load_cli()
    evidence = make_evidence(attempts=[], verdict="SKIPPED", reason="机制测试：CLI 退出码口径")
    path = tmp_path / "evidence.json"
    path.write_text(evidence.to_json(), encoding="utf-8", newline="\n")
    assert cli.main(["validate", str(path)]) == cli.EXIT_OK
    assert "复核结论: 通过" in capsys.readouterr().out


def test_validate_json_output_is_machine_readable(tmp_path: Path, capsys) -> None:
    cli = _load_cli()
    evidence = make_evidence(attempts=[], verdict="SKIPPED", reason="机制测试")
    path = tmp_path / "evidence.json"
    path.write_text(evidence.to_json(), encoding="utf-8", newline="\n")
    assert cli.main(["validate", str(path), "--json"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {check["name"] for check in payload["checks"]} >= {"schema", "verdict_recomputed"}


def test_validate_returns_one_for_a_tampered_verdict(tmp_path: Path, capsys) -> None:
    cli = _load_cli()
    evidence = make_evidence()
    payload = json.loads(evidence.to_json())
    payload["verdict"] = "PASS"
    payload["attempts"][0]["status"] = "FAIL"  # 声明 3/3 其实只有 2 次成功
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")
    assert cli.main(["validate", str(path)]) == cli.EXIT_NOT_PASSED
    assert "未通过" in capsys.readouterr().out
