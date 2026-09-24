#!/usr/bin/env python3
"""`live_gate.py` —— Live Gate 的可复跑命令行（`#307`）。

## 用法 / 退出码

    python scripts/live_gate.py capabilities [--json]
        # 只探运行环境：凭证**键名**（不是值）+ 端点三态探测 → 不发 run、不落盘证据
    python scripts/live_gate.py list
        # 列已注册场景（id / 版本 / 说明）
    python scripts/live_gate.py run [--scenario ID] [--out-dir DIR] [--no-write]
                                   [--skip REASON] [--inject-failure attempt:N]
        # 跑一次 Live Gate（3 次真实尝试）→ 落盘 docs/live_gate/<stamp>-<sha12>-<场景>/evidence.json
    python scripts/live_gate.py validate <evidence.json> [--json]
        # 独立复核一份证据（不跑场景、不需要凭证）：重算判定 / 重读轨迹 / 比对 sha256 / 复扫凭证

    退出码：0 = 判定 PASS（`validate` 为复核通过）；1 = 未通过（FAIL / BLOCKED / SKIPPED / 复核有 FAIL）；
            2 = 用法错误或环境错误（未知场景、证据文件读不了、缺参数）

## 判定语义（详见 `evaluation/live_gate/schema.py`）

`PASS` 只有一条路径：计划的三次尝试**全部成功**且没有任何注入/替身参与。缺凭证 ⇒ `BLOCKED`
（**一个模型请求都不发**）；操作者显式跳过 ⇒ `SKIPPED`。两者**都不是通过**，退出码都是 1。

## 与 Gate-0 的次序（实测踩过，别踩第二遍）

证据是未跟踪的 `*.json` ⇒ 跑完 Live Gate 但**没提交证据**时，Gate-0 会拒绝落盘读数
（它把未跟踪的 `.json` 当"可能被车道读入"的输入，fail-closed）。次序：
**跑 Live Gate → 提交 `docs/live_gate/**` → 再跑 Gate-0**。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 直接 `python scripts/live_gate.py` 时仓库根不在 sys.path 上（sys.path[0] 是 scripts/），
# 故补一次 —— 与 `scripts/gen_event_vocabulary.py` 同一处置。
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXIT_OK = 0
EXIT_NOT_PASSED = 1
EXIT_USAGE = 2


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")


def _print_capability(capability, *, as_json: bool) -> None:
    payload = {
        "verdict": capability.verdict,
        "reason": capability.reason,
        "credential_names": capability.credential_names,
        "provider": capability.provider.model_dump() if capability.provider else None,
        "probes": [probe.model_dump() for probe in capability.probes],
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f"能力面判定: {capability.verdict}")
    print("凭证键名（值不打印）: " + (", ".join(capability.credential_names) or "(无)"))
    if capability.provider is not None:
        print(
            "模型: "
            f"{capability.provider.provider_id}/{capability.provider.model_name}"
            f" @ {capability.provider.base_url_host}"
        )
    for probe in capability.probes:
        print(f"  · {probe.label}: {'可用' if probe.ok else probe.message}")
    if capability.reason:
        print("原因: " + capability.reason)


async def _cmd_capabilities(args: argparse.Namespace) -> int:
    from agent_harness.config import Settings
    from evaluation.live_gate.capability import check_capability

    capability = await check_capability(Settings())
    _print_capability(capability, as_json=args.json)
    print(f"docker daemon（仅供将来场景选型，本票证据不使用）: {_docker_state()}")
    return EXIT_OK if capability.ready else EXIT_NOT_PASSED


def _docker_state() -> str:
    import importlib.util
    import subprocess

    if importlib.util.find_spec("docker") is None:
        return "不可用（未安装 docker SDK）"
    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return f"可用（server {result.stdout.strip()}）" if result.returncode == 0 else "不可用（daemon 未响应）"


def _cmd_list(args: argparse.Namespace) -> int:
    from evaluation.live_gate.scenarios import register_builtin_scenarios

    register_builtin_scenarios()
    from evaluation.live_gate.registry import list_scenarios

    for scenario in list_scenarios():
        print(f"{scenario.id}  v{scenario.version}  {scenario.description}")
    return EXIT_OK


async def _cmd_run(args: argparse.Namespace) -> int:
    from evaluation.live_gate.registry import get_scenario
    from evaluation.live_gate.runner import GateOptions, run_gate
    from evaluation.live_gate.scenarios import register_builtin_scenarios

    register_builtin_scenarios()
    try:
        get_scenario(args.scenario)
    except KeyError as error:
        print(f"用法错误：{error}", file=sys.stderr)
        return EXIT_USAGE
    options = GateOptions(
        scenario_id=args.scenario,
        out_dir=Path(args.out_dir) if args.out_dir else REPO_ROOT / "docs" / "live_gate",
        skip_reason=args.skip or "",
        injected_failure=args.inject_failure or "",
        write=not args.no_write,
    )
    result = await run_gate(options)
    _print_run_result(result)
    return EXIT_OK if result.passed else EXIT_NOT_PASSED


def _print_run_result(result) -> None:
    evidence = result.evidence
    print(f"判定: {evidence.verdict.value}")
    print(f"sha: {evidence.sha}  tree: {evidence.tree}")
    if evidence.provider.model_name:
        print(
            f"模型: {evidence.provider.provider_id}/{evidence.provider.model_name}"
            f" @ {evidence.provider.base_url_host}"
        )
    for attempt in evidence.attempts:
        print(
            f"  · attempt {attempt.index}: {attempt.status.value} "
            f"run_status={attempt.run_status or '-'} steps={attempt.steps} "
            f"tools={attempt.tool_calls} {attempt.duration_ms}ms"
            + (f" err={attempt.error}" if attempt.error else "")
        )
        for assertion in attempt.assertions:
            print(f"      - {assertion.name}: {'ok' if assertion.ok else 'FAIL'}（{assertion.detail}）")
    if evidence.missing_preconditions:
        print("未满足的前置：")
        for item in evidence.missing_preconditions:
            print(f"  · {item}")
    if evidence.reason:
        print("原因: " + evidence.reason)
    if result.findings:
        print("凭证扫描命中（已脱敏）：")
        for finding in result.findings:
            print(f"  · {finding.line()}")
    print(f"证据: {result.evidence_path}" if result.evidence_path else "证据: （--no-write，未落盘）")


def _cmd_validate(args: argparse.Namespace) -> int:
    from evaluation.live_gate.secrets import credential_values
    from evaluation.live_gate.validator import validate_evidence

    path = Path(args.evidence)
    if not path.exists():
        print(f"用法错误：证据文件不存在：{path}", file=sys.stderr)
        return EXIT_USAGE
    payload = json.loads(path.read_text(encoding="utf-8"))
    values: tuple[str, ...] = ()
    try:
        from agent_harness.config import Settings

        values = tuple(credential_values(Settings()))
    except Exception:  # noqa: BLE001 - 复核**不需要**凭证；读不到就只跑形状层（如实标注）
        values = ()
    report = validate_evidence(payload, evidence_path=path, secret_values=values)
    if args.json:
        print(json.dumps(
            {"ok": report.ok, "checks": [vars(check) for check in report.checks]},
            ensure_ascii=False, indent=2,
        ))
    else:
        for check in report.checks:
            mark = {"PASS": "✅", "FAIL": "❌", "UNAVAILABLE": "⚠️ "}.get(check.status, "?")
            print(f"{mark} {check.name}: {check.detail}")
        print(f"复核结论: {'通过' if report.ok else '未通过'}"
              f"（{len(report.failures)} 条 FAIL / 共 {len(report.checks)} 条）")
    return EXIT_OK if report.ok else EXIT_NOT_PASSED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="live_gate.py", description="Live Gate：真实模型 + 生产工具 + 一次性工作区（#307）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capabilities", help="只探能力面：凭证键名 + 端点状态（不发 run）")
    cap.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    sub.add_parser("list", help="列已注册场景")

    run = sub.add_parser("run", help="跑一次 Live Gate（3 次真实尝试）")
    run.add_argument("--scenario", default="smoke-production-tools", help="场景 id（默认内置 smoke）")
    run.add_argument("--out-dir", default="", help="证据目录（默认 docs/live_gate）")
    run.add_argument("--no-write", action="store_true", help="只看结论，不落盘证据")
    run.add_argument("--skip", default="", help="操作者显式跳过（判 SKIPPED，附理由）")
    run.add_argument(
        "--inject-failure", default="",
        help="验证专用：attempt:<n> 让第 n 次尝试受控失败（总判定必为 FAIL）",
    )

    validate = sub.add_parser("validate", help="独立复核一份证据")
    validate.add_argument("evidence", help="证据 JSON 路径")
    validate.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "capabilities":
        return asyncio.run(_cmd_capabilities(args))
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "run":
        return asyncio.run(_cmd_run(args))
    if args.command == "validate":
        return _cmd_validate(args)
    return EXIT_USAGE  # pragma: no cover - argparse 已挡住未知子命令


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
