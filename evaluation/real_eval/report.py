"""real_eval 报告生成（issue #1005）：JSONL + manifest -> 报告 JSON。

复用：
- `evaluation.smoke.regression_metadata`（spec 12 §8 版本语境，REUSE）；
- `evaluation.live_gate.secrets.scan_payload`（凭证扫描/掩码，REUSE）——
  报告落盘前整份过一遍，保证**零凭证**。

输出：`evaluation/reports/real_eval_<baseline>_<stamp>.json`。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation.live_gate.secrets import scan_payload
from evaluation.real_eval.dataset import load_manifest
from evaluation.real_eval.runner import (
    REPORTS_DIR,
    RunConfig,
    config_hash,
    read_records,
)
from evaluation.real_eval.stats import classify_failure, summarize
from evaluation.smoke import regression_metadata


def _failures(records: list[dict[str, Any]], overrides: dict[str, str]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for record in records:
        if record.get("ok"):
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
        })
    return failures


def build_report(
    records: list[dict[str, Any]],
    *,
    config: RunConfig | None = None,
    manifest: dict[str, Any] | None = None,
    regression: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 JSONL 记录 + manifest 汇总成报告 payload（已过凭证扫描/掩码）。"""
    manifest = manifest or {}
    overrides = manifest.get("failure_class_overrides") or {}
    baseline = config.baseline if config is not None else (
        str(records[0].get("baseline")) if records else "unknown"
    )
    payload: dict[str, Any] = {
        "report": "real_eval",
        "baseline": baseline,
        "ran_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "config": config.model_dump() if config is not None else None,
        "config_hash": config_hash(config) if config is not None else (
            records[0].get("config_hash") if records else None
        ),
        "regression_metadata": regression if regression is not None else regression_metadata(),
        "stats": summarize(records, overrides),
        "failures": _failures(records, overrides),
        "manifest_summary": {
            "seed": manifest.get("seed"),
            "source": manifest.get("source"),
            "deviations": manifest.get("deviations", []),
        },
    }
    masked, findings = scan_payload(payload)
    masked["credential_scan"] = {
        "clean": not findings,
        "findings": [finding.line() for finding in findings],
    }
    return masked


def write_report(
    records: list[dict[str, Any]],
    *,
    config: RunConfig | None = None,
    manifest: dict[str, Any] | None = None,
    reports_dir: str | Path | None = None,
) -> Path:
    """生成报告并落盘；返回写入路径。"""
    payload = build_report(records, config=config, manifest=manifest)
    target_dir = Path(reports_dir) if reports_dir is not None else REPORTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = target_dir / f"real_eval_{payload['baseline']}_{stamp}.json"
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
    parser.add_argument("--reports-dir", default=None)
    args = parser.parse_args(argv)
    records = read_records(args.jsonl)
    manifest = load_manifest(args.manifest)
    path = write_report(records, manifest=manifest, reports_dir=args.reports_dir)
    print(json.dumps({"report": str(path)}, ensure_ascii=False))
    return path


if __name__ == "__main__":  # pragma: no cover - CLI 薄壳
    main()
