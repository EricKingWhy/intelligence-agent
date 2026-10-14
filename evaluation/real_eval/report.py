"""real_eval 报告生成（issue #1005）：JSONL + manifest -> 报告 JSON。

复用：
- `evaluation.smoke.regression_metadata`（spec 12 §8 版本语境，REUSE）——
  带 `dataset_path` 产出 `eval_dataset_version`；
- `evaluation.live_gate.secrets.scan_payload`（凭证扫描/掩码，REUSE）——
  报告落盘前整份过一遍，**形状层 + 精确值层**（与 live_gate 生产同口径），
  保证零凭证。

只聚合 `config_hash` 匹配的记录（`config` 给定则按当前配置的 hash 过滤，
否则按记录首条 hash 过滤），避免把不同配置/数据集的记录混进同一份报告。

输出：`evaluation/reports/real_eval_<baseline>_<stamp>.json`。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation.live_gate.secrets import scan_payload
from evaluation.real_eval.dataset import DATASETS_DIR, load_manifest
from evaluation.real_eval.runner import (
    REPORTS_DIR,
    RunConfig,
    config_hash,
    credential_scan_values,
    read_records,
)
from evaluation.real_eval.stats import classify_failure, summarize
from evaluation.smoke import regression_metadata


def _failures(records: list[dict[str, Any]], overrides: dict[str, str]) -> list[dict[str, Any]]:
    """失败明细：**只列 train**（Test 只出计数）；逐条带 judge 上下文（votes/reason）。"""
    failures: list[dict[str, Any]] = []
    for record in records:
        if record.get("ok"):
            continue
        if str(record.get("split", "train")) != "train":
            continue
        failures.append({
            "case_name": record.get("case_name"),
            "rep": record.get("rep"),
            "split": record.get("split"),
            "assertion_kind": record.get("assertion_kind"),
            "case_type": record.get("case_type"),
            "outcome": record.get("outcome"),
            "error": record.get("error"),
            "failure_class": classify_failure(record, overrides),
            # J 类失败：带上逐票 verdict/reason，直接从报告定位「判错」还是「答错」
            "judge": record.get("judge"),
        })
    return failures


def _test_failure_count(records: list[dict[str, Any]]) -> int:
    """Test 侧失败**只出计数**（不列题名/rep，守住 holdout 纪律）。"""
    return sum(
        1
        for record in records
        if not record.get("ok") and str(record.get("split", "train")) != "train"
    )


def _filter_by_config_hash(
    records: list[dict[str, Any]], config: RunConfig | None, dataset_dir: str | Path | None
) -> tuple[list[dict[str, Any]], str | None]:
    """只保留与目标 `config_hash` 一致的记录；返回 (过滤后记录, hash)。"""
    if config is not None:
        target = config_hash(config, dataset_dir=dataset_dir)
    elif records:
        target = records[0].get("config_hash")
    else:
        return [], None
    return [r for r in records if r.get("config_hash") == target], target


def build_report(
    records: list[dict[str, Any]],
    *,
    config: RunConfig | None = None,
    manifest: dict[str, Any] | None = None,
    regression: dict[str, Any] | None = None,
    dataset_dir: str | Path | None = None,
    planned_total: int | None = None,
    values: Iterable[str] | None = None,
) -> dict[str, Any]:
    """把 JSONL 记录 + manifest 汇总成报告 payload（已过凭证扫描/掩码）。"""
    manifest = manifest or {}
    overrides = manifest.get("failure_class_overrides") or {}
    records, target_hash = _filter_by_config_hash(records, config, dataset_dir)
    baseline = config.baseline if config is not None else (
        str(records[0].get("baseline")) if records else "unknown"
    )
    if regression is None:
        ds_root = Path(dataset_dir) if dataset_dir is not None else DATASETS_DIR
        regression = regression_metadata(dataset_path=ds_root / "train.jsonl")
    completed = len({
        (r.get("config_hash"), r.get("case_name"), r.get("rep")) for r in records
    })
    payload: dict[str, Any] = {
        "report": "real_eval",
        "baseline": baseline,
        "ran_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "config": config.model_dump() if config is not None else None,
        "config_hash": target_hash,
        "planned_total": planned_total,
        "completed": completed,
        "completion_ratio": (
            completed / planned_total if planned_total else None
        ),
        "regression_metadata": regression,
        "stats": summarize(records, overrides),
        "failures": _failures(records, overrides),
        "test_failure_count": _test_failure_count(records),
        "manifest_summary": {
            "seed": manifest.get("seed"),
            "source": manifest.get("source"),
            "deviations": manifest.get("deviations", []),
        },
    }
    scan_values = tuple(values) if values is not None else credential_scan_values()
    masked, findings = scan_payload(payload, values=scan_values)
    masked["credential_scan"] = {
        "clean": not findings,
        # 与 live_gate 同口径：精确值层是否可用（无配置凭证时如实标 unavailable）
        "exact_value_scan": "ran" if scan_values else "unavailable",
        "findings": [finding.line() for finding in findings],
    }
    return masked


def _report_stamp(now: datetime) -> str:
    """报告文件名时间戳：到毫秒（同秒两次调用不再互相覆盖）。"""
    return now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}"


def write_report(
    records: list[dict[str, Any]],
    *,
    config: RunConfig | None = None,
    manifest: dict[str, Any] | None = None,
    dataset_dir: str | Path | None = None,
    planned_total: int | None = None,
    values: Iterable[str] | None = None,
    reports_dir: str | Path | None = None,
) -> Path:
    """生成报告并落盘；返回写入路径（文件名防同秒覆盖）。"""
    payload = build_report(
        records, config=config, manifest=manifest, dataset_dir=dataset_dir,
        planned_total=planned_total, values=values,
    )
    target_dir = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = _report_stamp(datetime.now(UTC))
    path = target_dir / f"real_eval_{payload['baseline']}_{stamp}.json"
    suffix_index = 1
    while path.exists():  # 极端同毫秒再兜一层计数后缀
        path = target_dir / f"real_eval_{payload['baseline']}_{stamp}-{suffix_index}.json"
        suffix_index += 1
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.real_eval.report",
        description="从未 JSONL 生成 real_eval 报告",
    )
    parser.add_argument("--jsonl", required=True)
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--dataset-dir", default=None)
    parser.add_argument("--reports-dir", default=None)
    args = parser.parse_args(argv)
    records = read_records(args.jsonl)
    manifest = load_manifest(args.manifest)
    path = write_report(
        records, manifest=manifest, dataset_dir=args.dataset_dir,
        reports_dir=args.reports_dir,
    )
    print(json.dumps({"report": str(path)}, ensure_ascii=False))
    return path


if __name__ == "__main__":  # pragma: no cover - CLI 薄壳
    main()
