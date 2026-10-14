"""real_eval 报告（issue #1005）：聚合 + 凭证扫描（植入假 key 正控）。"""

from __future__ import annotations

import json
from pathlib import Path

from evaluation.live_gate.secrets import scan_payload
from evaluation.real_eval.report import build_report, write_report
from evaluation.real_eval.runner import RunConfig, config_hash

_FAKE_KEY = "sk-abcdefghijklmnop1234"


def _record(name: str, *, ok: bool, kind: str = "D", outcome: str = "ok") -> dict:
    return {
        "run_id": "run-1",
        "config_hash": config_hash(RunConfig.for_baseline("A")),
        "baseline": "A",
        "case_name": name,
        "rep": 1,
        "split": "train",
        "assertion_kind": kind,
        "case_type": "tool_selection",
        "tags": ["t"],
        "ok": ok,
        "outcome": outcome,
        "metrics": {},
        "judge": None,
        "usage": {"total_tokens": 10},
        "latency_ms": 100,
        "attempts": 1,
        "error": None,
        "timestamp": "2026-10-14T00:00:00.000Z",
    }


def test_build_report_structure_and_clean_scan():
    records = [_record("q1", ok=True), _record("q2", ok=False)]
    report = build_report(records, config=RunConfig.for_baseline("A"), manifest={"seed": 1})
    assert report["baseline"] == "A"
    assert report["config_hash"] == config_hash(RunConfig.for_baseline("A"))
    assert "regression_metadata" in report
    assert report["stats"]["n_questions"] == 2
    assert report["credential_scan"]["clean"] is True
    assert len(report["failures"]) == 1
    assert report["failures"][0]["failure_class"] == "capability_gap"


def test_credential_scan_positive_control():
    """植入假 key：形状扫描必须命中并掩掉（正控）。"""
    payload = {"note": f"leaked {_FAKE_KEY} in text"}
    masked, findings = scan_payload(payload)
    assert findings, "假 key 必须被形状扫描命中"
    assert _FAKE_KEY not in json.dumps(masked)
    assert "***" in json.dumps(masked)

    exact_masked, exact_findings = scan_payload(payload, values=[_FAKE_KEY])
    assert exact_findings
    assert _FAKE_KEY not in json.dumps(exact_masked)


def test_report_masks_leaked_key(tmp_path: Path):
    record = _record("q1", ok=False)
    record["error"] = f"provider echoed {_FAKE_KEY} back"
    report = build_report([record], config=RunConfig.for_baseline("A"))
    assert report["credential_scan"]["clean"] is False
    assert _FAKE_KEY not in json.dumps(report, ensure_ascii=False)


def test_write_report_creates_file_without_credentials(tmp_path: Path):
    record = _record("q1", ok=False)
    record["error"] = f"echo {_FAKE_KEY}"
    path = write_report(
        [record], config=RunConfig.for_baseline("A"),
        manifest={"seed": 1}, reports_dir=tmp_path,
    )
    assert path.exists()
    assert path.name.startswith("real_eval_A_") and path.suffix == ".json"
    text = path.read_text(encoding="utf-8")
    assert _FAKE_KEY not in text
    payload = json.loads(text)
    assert payload["baseline"] == "A"
