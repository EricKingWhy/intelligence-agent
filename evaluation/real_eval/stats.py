"""real_eval 统计（issue #1005）：全部纯函数，输入是 JSONL 记录列表。

记录是 `runner.py` 追加的一行（见那里 schema）。统计口径：

- **infra 不计入通过率分母**（provider 连接/5xx/限流等环境故障不归咎模型），
  infra 率单独报告；
- per-question / 分组通过率的分母同为「非 infra 的重复次数」；
- 所有随机量（bootstrap）固定 seed，保证可复现。
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from typing import Any

Record = dict[str, Any]

#: 失败分类固定四种（manifest 可覆盖自动启发式）。
FAILURE_CLASSES = frozenset(
    {"capability_gap", "judge_misgrade", "task_ambiguity", "environment"}
)

#: bootstrap 默认重采样次数与固定 seed。
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261011


def _outcome(record: Record) -> str:
    return str(record.get("outcome", "fail"))


def _is_infra(record: Record) -> bool:
    return _outcome(record) == "infra"


def _ok(record: Record) -> bool:
    return bool(record.get("ok"))


def pass_rate(records: Iterable[Record]) -> float:
    """通过率 = ok / 非 infra 计数（无有效样本返回 0.0）。"""
    scored = [r for r in records if not _is_infra(r)]
    if not scored:
        return 0.0
    return sum(1 for r in scored if _ok(r)) / len(scored)


def infra_rate(records: Sequence[Record]) -> float:
    """infra 率 = infra 记录 / 全部记录（无记录返回 0.0）。"""
    if not records:
        return 0.0
    return sum(1 for r in records if _is_infra(r)) / len(records)


def per_question_pass(records: Sequence[Record]) -> dict[str, float | None]:
    """每题通过率（分母 = 该题非 infra 的重复次数；全 infra → None）。"""
    grouped: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("case_name"))].append(record)
    result: dict[str, float | None] = {}
    for name, group in grouped.items():
        scored = [r for r in group if not _is_infra(r)]
        result[name] = (
            sum(1 for r in scored if _ok(r)) / len(scored) if scored else None
        )
    return result


def bootstrap_ci(
    values: Sequence[float],
    *,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """对一组标量做 bootstrap 95% CI（默认 10k 重采样、固定 seed）。

    返回 `(lo, hi)`；空输入返回 `(0.0, 0.0)`。
    """
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    means: list[float] = []
    for _ in range(n_resamples):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    alpha = (1.0 - confidence) / 2.0
    lo_index = max(0, min(n_resamples - 1, math.floor(alpha * n_resamples)))
    hi_index = max(0, min(n_resamples - 1, math.ceil((1 - alpha) * n_resamples) - 1))
    return (means[lo_index], means[hi_index])


def question_pass_ci(
    records: Sequence[Record], **kwargs: Any
) -> dict[str, Any]:
    """按题 bootstrap：对 per-question 通过率做重采样，返回 mean + 95% CI。"""
    rates = [v for v in per_question_pass(records).values() if v is not None]
    lo, hi = bootstrap_ci(rates, **kwargs)
    mean = sum(rates) / len(rates) if rates else 0.0
    return {"mean": mean, "ci95_low": lo, "ci95_high": hi, "n_questions": len(rates)}


def group_pass_rate(
    records: Sequence[Record], key: Callable[[Record], Iterable[str] | None]
) -> dict[str, float]:
    """按 key 抽取的分组标签分别算通过率（无标签的记录跳过）。"""
    grouped: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        labels = key(record)
        if not labels:
            continue
        for label in labels:
            grouped[label].append(record)
    return {label: pass_rate(group) for label, group in grouped.items()}


def group_pass_rates(records: Sequence[Record]) -> dict[str, float]:
    """按 case_type 与 tags 两个维度分别算分组通过率（键带维度前缀）。"""

    def by_case_type(record: Record) -> Iterable[str]:
        case_type = record.get("case_type")
        return [f"case_type:{case_type}"] if case_type else []

    def by_tag(record: Record) -> Iterable[str]:
        return [f"tag:{tag}" for tag in record.get("tags", [])]

    groups = group_pass_rate(records, by_case_type)
    groups.update(group_pass_rate(records, by_tag))
    return groups


def repeat_sd(records: Sequence[Record]) -> float:
    """各题重复通过率的总体标准差（population SD，分母 N）。无题返回 0.0。"""
    rates = [v for v in per_question_pass(records).values() if v is not None]
    if not rates:
        return 0.0
    mean = sum(rates) / len(rates)
    variance = sum((rate - mean) ** 2 for rate in rates) / len(rates)
    return math.sqrt(variance)


def mde(records: Sequence[Record]) -> float:
    """最小可检测效应 MDE = 2 × 重复 SD。"""
    return 2.0 * repeat_sd(records)


def train_vs_test(records: Sequence[Record]) -> dict[str, float]:
    """按 split 分层通过率。"""
    grouped: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("split", "train"))].append(record)
    return {split: pass_rate(group) for split, group in grouped.items()}


def _total_tokens(record: Record) -> int:
    usage = record.get("usage") or {}
    value = usage.get("total_tokens") if isinstance(usage, dict) else None
    return int(value) if isinstance(value, (int, float)) else 0


def tokens_per_question(records: Sequence[Record]) -> float:
    """tokens / 题：非 infra 记录的总 tokens 除以题目数（无题返回 0.0）。"""
    scored = [r for r in records if not _is_infra(r)]
    names = {str(r.get("case_name")) for r in scored}
    if not names:
        return 0.0
    return sum(_total_tokens(r) for r in scored) / len(names)


def percentile(values: Sequence[float], p: float) -> float:
    """线性插值分位数（p ∈ [0,100]）；空输入返回 0.0。"""
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def latency_percentiles(records: Sequence[Record]) -> dict[str, float]:
    """延迟 p50 / p95（毫秒）。"""
    latencies = [
        float(r.get("latency_ms", 0)) for r in records if not _is_infra(r)
    ]
    return {"p50_ms": percentile(latencies, 50), "p95_ms": percentile(latencies, 95)}


def judge_flip_rate(records: Sequence[Record]) -> float:
    """Judge 翻票率：3 票非一致的 J 记录 / 有有效投票的 J 记录。"""
    judged = [
        r for r in records
        if r.get("assertion_kind") == "J"
        and isinstance(r.get("judge"), dict)
        and r["judge"].get("valid_votes", 0) > 0
    ]
    if not judged:
        return 0.0
    return sum(1 for r in judged if r["judge"].get("flip")) / len(judged)


def classify_failure(record: Record, overrides: dict[str, str] | None = None) -> str:
    """单条失败记录的分类（四种固定类别之一）。

    自动启发式：infra → environment；D 失败 → capability_gap；
    J 失败 → judge_misgrade。manifest 的 `failure_class_overrides` 优先。
    """
    if _is_infra(record):
        return "environment"
    kind = str(record.get("assertion_kind", "D"))
    heuristic = "capability_gap" if kind == "D" else "judge_misgrade"
    name = str(record.get("case_name"))
    if overrides and name in overrides:
        candidate = overrides[name]
        if candidate in FAILURE_CLASSES:
            return candidate
    return heuristic


def failure_breakdown(
    records: Sequence[Record], overrides: dict[str, str] | None = None
) -> dict[str, int]:
    """失败记录按分类计数（通过记录不计入）。"""
    counts: dict[str, int] = {name: 0 for name in sorted(FAILURE_CLASSES)}
    for record in records:
        if _ok(record):
            continue
        counts[classify_failure(record, overrides)] += 1
    return counts


def summarize(
    records: Sequence[Record], overrides: dict[str, str] | None = None
) -> dict[str, Any]:
    """汇总全部指标（report.py 的统计段）。"""
    latencies = latency_percentiles(records)
    return {
        "n_records": len(records),
        "n_questions": len({str(r.get("case_name")) for r in records}),
        "overall_pass_rate": pass_rate(records),
        "infra_rate": infra_rate(records),
        "per_question_pass": per_question_pass(records),
        "question_pass_ci95": question_pass_ci(records),
        "group_pass_rate": group_pass_rates(records),
        "repeat_sd": repeat_sd(records),
        "mde": mde(records),
        "train_vs_test": train_vs_test(records),
        "tokens_per_question": tokens_per_question(records),
        "latency_p50_ms": latencies["p50_ms"],
        "latency_p95_ms": latencies["p95_ms"],
        "judge_flip_rate": judge_flip_rate(records),
        "failure_breakdown": failure_breakdown(records, overrides),
    }
