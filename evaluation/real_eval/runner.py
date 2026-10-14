"""real_eval 可续跑 JSONL 运行器（issue #1005）。

形状来源：openai/evals（MIT）`evals/record.py` 的 LocalRecorder——JSONL 追加，
**写一条 flush 一条**（中断不丢）、首行元数据、之后每条一行。PORT DESIGN。

一条命令续跑：`python -m evaluation.real_eval.run --baseline A --out <jsonl>`。
启动时读已有 JSONL，按 `(config_hash, case_name, rep)` 去重跳过**已完成**的记录
（`outcome in {ok, fail}`；`infra` 视为未完成可重试），只补跑缺的记录。
文件内既有记录的 `config_hash` 与本次不一致时**响亮拒绝**（换配置/数据集 ⇒ 不可比）。
每次运行用全新 `tempfile.mkdtemp` 工作区；SUT 不注册 websearch、不设 fallback；
**provider/传输层异常**记 `outcome=infra` 并最多重试 2 次（其它异常记 `fail`、不重试）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent_harness.session import RUN_COMPLETED, RUN_FAILED
from evaluation.assertions import (
    dangling_tool_call_ids,
    kill_resume_ok,
    recovery_guard_ok,
    tool_selection_ok,
)
from evaluation.live_gate.secrets import scan_payload
from evaluation.real_eval.dataset import (
    RealEvalCase,
    dataset_digest,
    load_cases,
)
from evaluation.real_eval.judge import JudgeFn, majority_judge
from evaluation.support import AddTool, ModeledFailureAddTool

_REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = _REPO_ROOT / "evaluation" / "reports"

#: 单条 case 的默认重试上限（仅 infra 才重试）。
DEFAULT_MAX_RETRIES = 2

#: provider 侧 infra 分类：分类表命中即环境故障（不归咎模型）。
_INFRA_REASONS = frozenset(
    {"provider_account_unavailable", "provider_auth_failed", "provider_model_not_found"}
)
_INFRA_REASON_MARKERS = ("timeout", "connection", "connect", "transport")
_INFRA_MESSAGE_MARKERS = ("无法连接", "超时", "服务端错误", "供应商", "限流")
#: provider/传输层异常的类型名（按 MRO 类名，不硬 import SDK）。只有这些算 infra；
#: 其它异常（harness/断言/胶水层的 bug）记 `fail`，不重试、不排除出分母。
_INFRA_ERROR_TYPE_NAMES = frozenset({
    "ModelClientConstructionError", "ModelStallError",
    "APITimeoutError", "APIConnectionError",
    "TimeoutError", "ConnectionError",
    "TransportError", "ConnectError", "ReadTimeout", "WriteTimeout",
    "PoolTimeout", "ConnectTimeout", "RemoteProtocolError", "ProtocolError",
})


class RunConfig(BaseModel):
    """一次基线的运行配置；`config_hash` = 规范 JSON（含数据集版本）的 sha256。"""

    baseline: str
    provider: str
    model: str
    base_url: str
    api_key_env: str
    temperature: float = 0.0
    max_tokens: int = 1024
    reps: int = 5
    budget_tokens: int = 20_000_000
    budget_seconds: int = 14_400

    @classmethod
    def for_baseline(cls, name: str, **overrides: Any) -> RunConfig:
        profile = BASELINES.get(name)
        if profile is None:
            raise ValueError(
                f"未知 baseline: {name!r}，可选: {sorted(BASELINES)}"
            )
        return cls(baseline=name, **{**profile, **overrides})


#: 基线档案（SUT 端点 / 模型 / key 环境变量名）。key **只**从环境变量读，
#: 代码里不出现任何 key 字面量。新增基线只在此登记。
BASELINES: dict[str, dict[str, Any]] = {
    "A": {
        "provider": "kunyou",
        "model": "glm-5.3-flash",
        "base_url": "https://api.kunyou.asia/v1",
        "api_key_env": "KUNYOU_API_KEY",
    },
    "B": {
        "provider": "mimo",
        "model": "mimo-v2.6-flash",
        "base_url": "https://api.xiaomimimo.com/v1",
        "api_key_env": "MIMO_API_KEY",
    },
}

#: 注入 seam：按 (case, config) 返回一个 ChatModel（测试用 fake；生产 = 真实 provider）。
SutFactory = Callable[[RealEvalCase, RunConfig], Any]


def canonical_json(payload: Any) -> str:
    """规范 JSON（键排序、紧凑分隔、非 ASCII 原样）：哈希的稳定输入。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_hash(config: RunConfig, *, dataset_dir: str | Path | None = None) -> str:
    """配置内容哈希（sha256）：config 或数据集一变，基线不可比。

    纳入 `dataset_digest(dataset_dir)`（train.jsonl + test.jsonl 的内容哈希），
    使「题面/期望值改了但 RunConfig 没改」也判为不可比（第 2 问要的 dataset 版本）。
    `dataset_dir=None` 时取默认数据集目录。
    """
    payload = {
        "config": config.model_dump(),
        "dataset_version": dataset_digest(dataset_dir),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _build_model(config: RunConfig) -> Any:
    """真实 SUT：ModelConfig + create_chat_model（temperature 0、固定 max_tokens）。"""
    from agent_harness.model.config import ModelConfig
    from agent_harness.model.provider import create_chat_model

    api_key = os.environ.get(config.api_key_env)
    if not api_key:
        raise RuntimeError(f"SUT 需要环境变量 {config.api_key_env}（未设置）")
    model_config = ModelConfig(
        provider=config.provider,
        model_name=config.model,
        api_key=api_key,
        base_url=config.base_url,
        temperature=config.temperature,
    )
    chat = create_chat_model(model_config)
    chat.max_tokens = config.max_tokens  # 固定输出上限（可复跑）
    return chat


def _default_sut_factory(_case: RealEvalCase, config: RunConfig) -> Any:
    """生产 SUT factory：真实 provider（测试通过 `sut_factory` 注入 fake）。"""
    return _build_model(config)


def _registry_for(case_type: str) -> Any:
    """case_type → 只含评测用工具的 registry（**不注册 websearch**）。

    recovery 用 `ModeledFailureAddTool`（按参数指纹确定性、**不可重试**失败）：
    只有模型层面的失败才会暴露给模型、触发 `recovery_guard_ok` 的软护栏。
    （票面写的是 FlakyAddTool，但它的失败是 `retryable=True`，会被 ToolExecutor
    在**同一次调用内**重试到成功——事件流里从不出现失败，`recovery_guard_ok`
    永远为 False；详见 manifest 的 deviation 说明。）
    """
    from agent_harness.tooling import ToolRegistry

    registry = ToolRegistry()
    if case_type == "tool_selection":
        registry.register(AddTool())
    elif case_type == "recovery":
        registry.register(ModeledFailureAddTool())
    return registry


def _is_infra(data: dict[str, Any]) -> bool:
    """run/failed 的载荷是否属环境故障（连接 / 5xx / 限流 / 账户不可用）。"""
    status = data.get("http_status")
    if (
        isinstance(status, int)
        and not isinstance(status, bool)
        and (status == 429 or status >= 500)
    ):
        return True
    reason = str(data.get("reason") or "")
    if reason in _INFRA_REASONS:
        return True
    if any(marker in reason.lower() for marker in _INFRA_REASON_MARKERS):
        return True
    message = str(data.get("message") or "")
    return any(marker in message for marker in _INFRA_MESSAGE_MARKERS)


def _infra_from_error_text(error: str | None) -> bool:
    """异常字符串是否属 provider/传输层故障（`"{TypeName}: {msg}"` 形状）。

    只认白名单类型名 + 传输层文本标记：harness/断言/胶水层的异常
    （`KeyError` / `ValueError` / `RuntimeError("judge_fn 缺失")` 等）**不算** infra，
    记 `fail` 且不重试、不排除出分母（避免用异常兜底掩盖真回归）。
    """
    if not error:
        return False
    name = error.split(":", 1)[0].strip()
    if name in _INFRA_ERROR_TYPE_NAMES:
        return True
    lowered = error.lower()
    if any(marker in lowered for marker in _INFRA_REASON_MARKERS):
        return True
    return any(marker in error for marker in _INFRA_MESSAGE_MARKERS)


def _infra_from_exception(exc: BaseException) -> bool:
    """异常对象是否属 provider/传输层故障（按 MRO 类名 + 文本标记）。"""
    mro_names = {cls.__name__ for cls in type(exc).__mro__}
    if mro_names & _INFRA_ERROR_TYPE_NAMES:
        return True
    return _infra_from_error_text(f"{type(exc).__name__}: {exc}")


def _find_event(events: list[Any], event_type: str) -> Any | None:
    return next((event for event in events if event.type == event_type), None)


def _final_text(events: list[Any]) -> str:
    completed = _find_event(events, RUN_COMPLETED)
    if completed is None:
        return ""
    return str(completed.data.get("final_text", ""))


async def _run_case_async(
    case: RealEvalCase,
    *,
    session_root: Path,
    model: Any,
    judge_fn: JudgeFn | None,
) -> dict[str, Any]:
    """跑一条（case, rep）：真实 AgentRuntime + 断言/Judge，返回归一化结果。

    复用 `evaluation.runner.run_case_async`（含 kill_resume 的崩溃-恢复机器），
    注入一个只含评测工具的 runtime factory（无 fallback、无 websearch）。
    """
    from agent_harness.agent import AgentRuntime
    from agent_harness.tooling import ToolExecutor
    from evaluation.runner import EvalCase, run_case_async

    def factory(inner_case: EvalCase) -> AgentRuntime:
        registry = _registry_for(case.case_type)
        return AgentRuntime(model, registry, ToolExecutor(registry))

    eval_case = EvalCase(
        name=case.name,
        case_type=case.case_type,
        task=case.task,
        script=[],
        expected=case.expected,
        tags=case.tags,
    )
    case_result, events = await run_case_async(
        eval_case, session_root=session_root, runtime_factory=factory,
    )

    completed = _find_event(events, RUN_COMPLETED) is not None
    failed = _find_event(events, RUN_FAILED)
    error = case_result.error
    if error is not None:
        # 异常兜底：只把 provider/传输层异常判 infra（其余记 fail，见 _infra_from_error_text）
        infra = _infra_from_error_text(error)
    elif failed is not None:
        infra = _is_infra(failed.data)
    else:
        infra = False

    metrics: dict[str, Any] = {
        "completed": completed,
        "dangling_tool_calls": len(dangling_tool_call_ids(events)),
    }
    judge: dict[str, Any] | None = None
    judge_tokens = 0
    ok = False

    if not infra and completed:
        if case.case_type == "tool_selection":
            ok = tool_selection_ok(events, list(case.expected.get("tools", [])))
            metrics["tool_selected"] = ok
        elif case.case_type == "recovery":
            ok = recovery_guard_ok(events)
            metrics["recovered"] = ok
        elif case.case_type == "kill_resume":
            ok = kill_resume_ok(events)
            metrics["kill_resume_ok"] = ok
        elif case.case_type == "open_qa":
            fn = judge_fn
            if fn is None:
                raise RuntimeError("open_qa case 需要 judge_fn")
            result = majority_judge(
                task=case.task,
                criteria=case.judge_criteria or str(case.expected.get("criteria", "")),
                actual=_final_text(events),
                judge_fn=fn,
                votes=int(case.assertion.get("votes", 3)),
            )
            judge = result.model_dump()
            ok = result.ok
            judge_tokens = result.judge_tokens
            if result.valid_votes == 0:
                infra = True
        # base 语义（evaluation/runner.py）：dangling == 0 是 ok 的必要项
        ok = ok and metrics["dangling_tool_calls"] == 0

    outcome = "infra" if infra else ("ok" if ok else "fail")
    if infra:
        ok = False
    usage = dict(case_result.usage_total or {})
    if judge_tokens:
        usage["judge_tokens"] = int(usage.get("judge_tokens", 0)) + judge_tokens
        usage["total_tokens"] = int(usage.get("total_tokens", 0)) + judge_tokens
    return {
        "ok": ok,
        "outcome": outcome,
        "metrics": metrics,
        "judge": judge,
        "usage": usage,
        "latency_ms": case_result.duration_ms,
        "error": error,
    }


async def _run_record_async(
    case: RealEvalCase,
    rep: int,
    config: RunConfig,
    *,
    sut_factory: SutFactory,
    judge_fn: JudgeFn | None,
    max_retries: int,
) -> dict[str, Any]:
    """跑一条记录，infra 最多重试 `max_retries` 次（其余终止）。"""
    max_attempts = 1 + max_retries
    attempt = 0
    result: dict[str, Any]
    while True:
        attempt += 1
        workspace = Path(tempfile.mkdtemp(prefix=f"real_eval_{config.baseline}_"))
        try:
            model = sut_factory(case, config)
            result = await _run_case_async(
                case, session_root=workspace / "session", model=model, judge_fn=judge_fn,
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不得炸掉整个基线
            # provider/传输层异常记 infra（可重试）；其它异常记 fail（不重试、不排除）
            result = {
                "ok": False,
                "outcome": "infra" if _infra_from_exception(exc) else "fail",
                "metrics": {},
                "judge": None,
                "usage": {},
                "latency_ms": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            shutil.rmtree(workspace, ignore_errors=True)  # 运行后清理临时工作区
        result["attempts"] = attempt
        if result["outcome"] != "infra" or attempt >= max_attempts:
            return result


def _record(
    config: RunConfig,
    config_hash_value: str,
    run_id: str,
    case: RealEvalCase,
    rep: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "config_hash": config_hash_value,
        "baseline": config.baseline,
        "case_name": case.name,
        "rep": rep,
        "split": case.split,
        "assertion_kind": case.assertion_kind,
        "case_type": case.case_type,
        "tags": case.tags,
        "ok": bool(result["ok"]),
        "outcome": str(result["outcome"]),
        "metrics": result["metrics"],
        "judge": result["judge"],
        "usage": result["usage"],
        "latency_ms": result["latency_ms"],
        "attempts": result["attempts"],
        "error": result["error"],
        "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }


def credential_scan_values() -> tuple[str, ...]:
    """进程内配置的凭证值（仅内存，绝不打印/落盘）；未配置任何凭证时为空元组。

    与 `evaluation/live_gate/runner.py` 生产口径一致：把整份产物在**落盘前**用
    精确值层扫一遍，杜绝「形状不像 key 的自定义凭证」被回显进 JSONL/报告。
    """
    from agent_harness.config import Settings
    from evaluation.live_gate.secrets import credential_values

    return tuple(credential_values(Settings()))


def _write_header(
    out_path: Path,
    *,
    run_id: str,
    config: RunConfig,
    config_hash_value: str,
    dataset_version: str,
    planned_total: int,
) -> None:
    """写 JSONL 首行运行元数据（openai/evals LocalRecorder 形状）。"""
    header = {
        "record_type": "header",
        "run_id": run_id,
        "baseline": config.baseline,
        "config": config.model_dump(),
        "config_hash": config_hash_value,
        "dataset_version": dataset_version,
        "planned_total": planned_total,
        "created_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _append_record(
    out_path: Path, record: dict[str, Any], *, values: tuple[str, ...] = ()
) -> None:
    """追加一行并 `flush+fsync`；落盘前过一遍凭证扫描（零凭证承诺）。

    `flush` 只把数据交给 OS，掉电仍可能丢/留半截；`fsync` 才把数据落到磁盘。
    落盘前用精确值层扫描并掩码，避免模型回显的凭证进 JSONL。
    """
    masked, _findings = scan_payload(record, values=values)
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(masked, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """读 JSONL 记录：跳过首行元数据与空行；结尾半截行容忍；同键取最后一条。

    - **半截行容忍**：中断/掉电可能在文件尾留下被截断的一行（`_append_record`
      分多次 write 时尤甚）。仅当**最后一个非空行**解析失败时忽略它并向 stderr 告警
      （这正是「中断续跑」要防的场景）；中间行解析失败属真损坏，仍 raise。
    - **同键取最后**：`(config_hash, case_name, rep)` 相同的多行（infra 重试会追加）
      以最后一条为准，不把陈旧副本算进统计。
    """
    target = Path(path)
    if not target.exists():
        return []
    lines = target.read_text(encoding="utf-8").splitlines()
    last_nonempty = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
    records: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, int], int] = {}
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == last_nonempty:
                print(
                    f"警告：real_eval JSONL 结尾有不完整行，已忽略：{target}",
                    file=sys.stderr,
                )
                break
            raise
        if not isinstance(record, dict) or record.get("record_type") == "header":
            continue
        key = (
            str(record.get("config_hash")),
            str(record.get("case_name")),
            int(record.get("rep", 0)),
        )
        if key in by_key:
            records[by_key[key]] = record
        else:
            by_key[key] = len(records)
            records.append(record)
    return records


def _done_keys(records: list[dict[str, Any]]) -> set[tuple[str, str, int]]:
    """已完成键：`(config_hash, case_name, rep)`，且 `outcome in {ok, fail}`。

    `infra` 记录视为**未完成**（下次续跑会重试），不冻结进分母。
    """
    return {
        (str(r.get("config_hash")), str(r.get("case_name")), int(r.get("rep", 0)))
        for r in records
        if r.get("outcome") in ("ok", "fail")
    }


def _record_tokens(record: dict[str, Any]) -> int:
    usage = record.get("usage") or {}
    tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    return int(tokens) if isinstance(tokens, (int, float)) else 0


def run_baseline(
    config: RunConfig,
    *,
    out_path: str | Path,
    dataset_dir: str | Path | None = None,
    sut_factory: SutFactory | None = None,
    judge_fn: JudgeFn | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict[str, Any]:
    """跑（或续跑）一次基线，逐条追加 JSONL，返回 summary。

    文件内既有记录的 `config_hash` 与本次不一致（换了配置或数据集）⇒ 响亮拒绝，
    绝不静默混树——否则会得到一份「标着新配置、实为旧记录」的报告。
    """
    cases = load_cases(dataset_dir)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    ch = config_hash(config, dataset_dir=dataset_dir)
    existing = read_records(out)
    mismatched = sorted(
        {str(r.get("config_hash")) for r in existing if r.get("config_hash") != ch}
    )
    if mismatched:
        raise ValueError(
            f"输出文件 {out} 含不同 config_hash 的记录（{mismatched}）；"
            f"拒绝在同文件续跑（配置/数据集不可比）。请换 --out 或恢复原配置。"
        )
    done = _done_keys(existing)
    skipped = len(done)
    planned_total = len(cases) * config.reps

    # 仅在确有**未跑**的 J 题时才构建 judge（续跑一个已完成的 run 不该因缺 key 失败）。
    needs_judge = any(
        case.assertion_kind == "J"
        and any((ch, case.name, rep) not in done for rep in range(1, config.reps + 1))
        for case in cases
    )
    if needs_judge and judge_fn is None:
        from evaluation.real_eval.judge import build_judge_fn

        judge_fn = build_judge_fn()
    factory: SutFactory = sut_factory or _default_sut_factory

    run_id = f"{config.baseline}-{uuid.uuid4().hex[:8]}"
    if not out.exists() or out.stat().st_size == 0:
        _write_header(
            out, run_id=run_id, config=config, config_hash_value=ch,
            dataset_version=dataset_digest(dataset_dir), planned_total=planned_total,
        )

    values = credential_scan_values()
    started = time.monotonic()
    # 续跑：把既有记录的 tokens 一并计入累计（护栏不在换进程时归零）
    total_tokens = sum(_record_tokens(record) for record in existing)
    written = 0
    budget_stop = False

    for case in cases:
        for rep in range(1, config.reps + 1):
            if (ch, case.name, rep) in done:
                continue
            elapsed = time.monotonic() - started
            if total_tokens > config.budget_tokens or elapsed > config.budget_seconds:
                budget_stop = True
                break
            result = asyncio.run(_run_record_async(
                case, rep, config,
                sut_factory=factory, judge_fn=judge_fn, max_retries=max_retries,
            ))
            record = _record(config, ch, run_id, case, rep, result)
            _append_record(out, record, values=values)
            done.add((ch, case.name, rep))
            total_tokens += _record_tokens(record)
            written += 1
            # 记录**结束处**再查一次：单条超贵记录也能在写完后立刻刹住
            if (
                total_tokens > config.budget_tokens
                or (time.monotonic() - started) > config.budget_seconds
            ):
                budget_stop = True
                break
        if budget_stop:
            break

    return {
        "baseline": config.baseline,
        "config_hash": ch,
        "out": str(out),
        "written": written,
        "skipped": skipped,
        "budget_stop": budget_stop,
        "total_tokens": total_tokens,
        "planned_total": planned_total,
        "completion_ratio": (written + skipped) / planned_total if planned_total else 0.0,
    }


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.real_eval.run",
        description="真实模型基线（可续跑 JSONL）",
    )
    parser.add_argument("--baseline", required=True, help="基线名（见 BASELINES）")
    parser.add_argument("--out", required=True, help="输出 JSONL 路径")
    parser.add_argument("--dataset-dir", default=None, help="数据集目录")
    parser.add_argument("--reps", type=int, default=None)
    parser.add_argument("--budget-tokens", type=int, default=None)
    parser.add_argument("--budget-seconds", type=int, default=None)
    args = parser.parse_args(argv)

    overrides: dict[str, Any] = {}
    if args.reps is not None:
        overrides["reps"] = args.reps
    if args.budget_tokens is not None:
        overrides["budget_tokens"] = args.budget_tokens
    if args.budget_seconds is not None:
        overrides["budget_seconds"] = args.budget_seconds
    config = RunConfig.for_baseline(args.baseline, **overrides)
    summary = run_baseline(config, out_path=args.out, dataset_dir=args.dataset_dir)
    print(json.dumps(summary, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    main()
