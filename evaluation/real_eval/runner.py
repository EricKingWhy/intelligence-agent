"""real_eval 可续跑 JSONL 运行器（issue #1005）。

形状来源：openai/evals（MIT）`evals/record.py` 的 LocalRecorder——JSONL 追加，
**写一条 flush 一条**（中断不丢）、首行元数据、之后每条一行。PORT DESIGN。

一条命令续跑：`python -m evaluation.real_eval.run --baseline A --out <jsonl>`。
启动时读已有 JSONL，按 `(case_name, rep)` 去重跳过已跑过的（含 infra 记录），
只补跑缺的记录。每次运行用全新 `tempfile.mkdtemp` 工作区；SUT 不注册 websearch、
不设 fallback；provider 异常（连接 / 5xx / 限流）记 `outcome=infra` 并最多重试 2 次。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
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
from evaluation.real_eval.dataset import RealEvalCase, load_cases
from evaluation.real_eval.judge import JudgeFn, majority_judge
from evaluation.support import AddTool, ModeledFailureAddTool

_REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = _REPO_ROOT / "evaluation" / "reports"

#: 每条 case 的记录里，确定性断言支持的 case_type。
_D_CASE_TYPES = frozenset({"tool_selection", "recovery", "kill_resume"})
#: 单条 case 的默认重试上限（infra 才重试）。
DEFAULT_MAX_RETRIES = 2

#: provider 侧 infra 分类：分类表命中即环境故障（不归咎模型）。
_INFRA_REASONS = frozenset(
    {"provider_account_unavailable", "provider_auth_failed", "provider_model_not_found"}
)
_INFRA_REASON_MARKERS = ("timeout", "connection", "connect", "transport")
_INFRA_MESSAGE_MARKERS = ("无法连接", "超时", "服务端错误", "供应商", "限流")


class RunConfig(BaseModel):
    """一次基线的运行配置；`config_hash` = 规范 JSON 的 sha256。"""

    baseline: str
    provider: str
    model: str
    base_url: str
    api_key_env: str
    temperature: float = 0.0
    max_tokens: int = 1024
    seed: int = 20261011
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


def config_hash(config: RunConfig) -> str:
    """配置内容哈希（sha256）：config 一变，基线不可比。"""
    return hashlib.sha256(canonical_json(config.model_dump()).encode("utf-8")).hexdigest()


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


def _find_event(events: list[Any], event_type: str) -> Any | None:
    return next((event for event in events if event.type == event_type), None)


def _final_text(events: list[Any]) -> str:
    completed = _find_event(events, RUN_COMPLETED)
    if completed is None:
        return ""
    return str(completed.data.get("final_text", ""))


def _extract_usage(events: list[Any]) -> dict[str, Any]:
    for event in events:
        if event.type == RUN_COMPLETED and event.data.get("usage_total"):
            return dict(event.data["usage_total"])
    return {}


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
    infra = error is not None or (failed is not None and _is_infra(failed.data))

    metrics: dict[str, Any] = {
        "completed": completed,
        "dangling_tool_calls": len(dangling_tool_call_ids(events)),
    }
    judge: dict[str, Any] | None = None
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
            )
            judge = result.model_dump()
            ok = result.ok
            if result.valid_votes == 0:
                infra = True

    outcome = "infra" if infra else ("ok" if ok else "fail")
    if infra:
        ok = False
    usage = case_result.usage_total or _extract_usage(events)
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
            result = {
                "ok": False,
                "outcome": "infra",
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
    run_id: str,
    case: RealEvalCase,
    rep: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "config_hash": config_hash(config),
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


def _append_record(out_path: Path, record: dict[str, Any]) -> None:
    """追加一行并 flush（openai/evals LocalRecorder 形状：写一条落一条）。"""
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """读 JSONL 记录（空行忽略）。"""
    target = Path(path)
    if not target.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _done_keys(records: list[dict[str, Any]]) -> set[tuple[str, int]]:
    return {(str(r.get("case_name")), int(r.get("rep", 0))) for r in records}


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
    """跑（或续跑）一次基线，逐条追加 JSONL，返回 summary。"""
    cases = load_cases(dataset_dir)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    existing = read_records(out)
    done = _done_keys(existing)
    skipped = len(done)

    # 仅在确有**未跑**的 J 题时才构建 judge（续跑一个已完成的 run 不该因缺 key 失败）。
    needs_judge = any(
        case.assertion_kind == "J"
        and any((case.name, rep) not in done for rep in range(1, config.reps + 1))
        for case in cases
    )
    if needs_judge and judge_fn is None:
        from evaluation.real_eval.judge import build_judge_fn

        judge_fn = build_judge_fn()
    factory: SutFactory = sut_factory or _default_sut_factory

    run_id = f"{config.baseline}-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    total_tokens = 0
    written = 0
    budget_stop = False

    for case in cases:
        for rep in range(1, config.reps + 1):
            if (case.name, rep) in done:
                continue
            elapsed = time.monotonic() - started
            if total_tokens > config.budget_tokens or elapsed > config.budget_seconds:
                budget_stop = True
                break
            result = asyncio.run(_run_record_async(
                case, rep, config,
                sut_factory=factory, judge_fn=judge_fn, max_retries=max_retries,
            ))
            record = _record(config, run_id, case, rep, result)
            _append_record(out, record)
            done.add((case.name, rep))
            total_tokens += _record_tokens(record)
            written += 1
        if budget_stop:
            break

    return {
        "baseline": config.baseline,
        "config_hash": config_hash(config),
        "out": str(out),
        "written": written,
        "skipped": skipped,
        "budget_stop": budget_stop,
        "total_tokens": total_tokens,
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
