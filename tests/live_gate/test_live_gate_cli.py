"""CLI 面（`#307` AC「专用命令」）：退出码与"未知名场景 fail-fast"。

退出码是脚本化的唯一接口（人眼只看终端）：0 = PASS / 复核通过；1 = 未通过（含 BLOCKED /
SKIPPED）；2 = 用法或环境错误。把 BLOCKED 报成 0 就等于把"没跑"卖成"跑过了"。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from evaluation.live_gate import repo
from evaluation.live_gate.registry import get_scenario
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
    # 场景版本随票增长（T2 v1 → T5 v2 …），钉的是"版本以 vN 形式打出来"，
    # 不是某个具体 N —— 写死会让每张改场景的票都得改这里。
    assert f"v{get_scenario('smoke-production-tools').version}" in out


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
    out = capsys.readouterr().out
    assert "复核结论（证据自洽）: 通过" in out
    # 自洽 ≠ 判定通过：这份证据声明的是 SKIPPED，脚本化消费方必须看得见这一行
    assert "证据声明的判定: SKIPPED" in out
    assert "判定通过（声明 PASS 且证据自洽）: 否" in out


def test_validate_require_pass_rejects_a_self_consistent_non_pass(tmp_path: Path, capsys) -> None:
    """`--require-pass`：一份自洽的 SKIPPED 证据默认退出 0，加了闸门就必须是 1。

    `passed`（判定通过 = 声明 PASS 且自洽）**两种跑法下都是 false** —— 它不随开关变形，
    变的是退出码。这样机器面不会因为"没加开关"就把 SKIPPED 读成"判定通过"。
    """
    cli = _load_cli()
    evidence = make_evidence(attempts=[], verdict="SKIPPED", reason="机制测试")
    path = tmp_path / "evidence.json"
    path.write_text(evidence.to_json(), encoding="utf-8", newline="\n")
    assert cli.main(["validate", str(path), "--json", "--require-pass"]) == cli.EXIT_NOT_PASSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["passed"] is False
    assert payload["declared_verdict"] == "SKIPPED"
    assert payload["exit_ok"] is False
    # 不加闸门：退出码只反映"证据自洽"（这是默认口径，且输出里要说明）
    assert cli.main(["validate", str(path), "--require-pass"]) == cli.EXIT_NOT_PASSED
    capsys.readouterr()
    assert cli.main(["validate", str(path)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "判定通过（声明 PASS 且证据自洽）: 否" in out
    assert "--require-pass" in out, "退出码口径与判定口径的差别要写在输出里"


def test_validate_json_output_is_machine_readable(tmp_path: Path, capsys) -> None:
    cli = _load_cli()
    evidence = make_evidence(attempts=[], verdict="SKIPPED", reason="机制测试")
    path = tmp_path / "evidence.json"
    path.write_text(evidence.to_json(), encoding="utf-8", newline="\n")
    assert cli.main(["validate", str(path), "--json"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {check["name"] for check in payload["checks"]} >= {"schema", "verdict_recomputed"}


def test_capabilities_json_is_pure_json(tmp_path: Path, capsys, monkeypatch) -> None:
    """`--json` 的输出必须整体可 `json.loads`（docker 那行是给人看的，不能混进机器面）。

    能力面用替身（不探测真实端点）：本用例判的是**输出形态**，不是网络。
    """
    cli = _load_cli()
    from evaluation.live_gate.capability import ProviderCapability

    async def _fake_check(settings, *, timeout=None):
        return ProviderCapability(verdict="BLOCKED", reason="机制测试：不探测", credential_names=[])

    monkeypatch.setattr(
        "evaluation.live_gate.capability.check_capability", _fake_check, raising=True,
    )
    assert cli.main(["capabilities", "--json"]) == cli.EXIT_NOT_PASSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "BLOCKED"
    assert "docker" in payload


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
