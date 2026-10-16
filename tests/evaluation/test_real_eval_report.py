"""real_eval 报告（issue #1005）：聚合 + 凭证扫描（植入假 key 正控）。"""

from __future__ import annotations

import json
from pathlib import Path

from evaluation.live_gate.secrets import scan_payload
from evaluation.real_eval.report import build_report, write_report
from evaluation.real_eval.runner import RunConfig, config_hash

_FAKE_KEY = "sk-abcdefghijklmnop1234"


def _record(name: str, *, ok: bool, kind: str = "D", outcome: str = "ok",
            split: str = "train", ch: str | None = None,
            judge: dict | None = None, case_type: str = "tool_selection") -> dict:
    return {
        "run_id": "run-1",
        "config_hash": ch if ch is not None else config_hash(RunConfig.for_baseline("A")),
        "baseline": "A",
        "case_name": name,
        "rep": 1,
        "split": split,
        "assertion_kind": kind,
        "case_type": case_type,
        "tags": ["t"],
        "ok": ok,
        "outcome": outcome,
        "metrics": {},
        "judge": judge,
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


def test_exact_value_scan_masks_custom_credential():
    """非 sk- 形状的自定义凭证：只有精确值层能抓（AC#3 安全红线）。"""
    custom = "kunyou-custom-secret-9f8e7d6c"  # 形状层识别不出
    record = _record("q1", ok=False)
    record["error"] = f"provider echoed {custom} back"
    report = build_report(
        [record], config=RunConfig.for_baseline("A"), values=[custom],
    )
    assert report["credential_scan"]["clean"] is False
    assert report["credential_scan"]["exact_value_scan"] == "ran"
    assert custom not in json.dumps(report, ensure_ascii=False)


def test_exact_value_scan_marked_unavailable_without_values():
    report = build_report(
        [_record("q1", ok=True)], config=RunConfig.for_baseline("A"), values=[],
    )
    assert report["credential_scan"]["exact_value_scan"] == "unavailable"


def test_report_aggregates_only_matching_config_hash():
    other = config_hash(RunConfig.for_baseline("B"))
    records = [
        _record("q1", ok=True),
        _record("qB", ok=False, ch=other),  # 不同配置，必须被过滤
    ]
    report = build_report(records, config=RunConfig.for_baseline("A"))
    assert report["stats"]["n_questions"] == 1
    assert "qB" not in report["stats"]["per_question_pass"]
    assert all(f["case_name"] != "qB" for f in report["failures"])


def test_report_test_details_only_counted_not_listed():
    records = [
        _record("q_train", ok=False, split="train"),
        _record("q_test", ok=False, split="test"),
    ]
    report = build_report(records, config=RunConfig.for_baseline("A"))
    assert [f["case_name"] for f in report["failures"]] == ["q_train"]
    assert report["test_failure_count"] == 1


def test_report_planned_total_and_completion_ratio():
    records = [_record("q1", ok=True), _record("q2", ok=True)]
    report = build_report(
        records, config=RunConfig.for_baseline("A"), planned_total=4,
    )
    assert report["planned_total"] == 4
    assert report["completed"] == 2
    assert report["completion_ratio"] == 0.5


def test_failure_entry_includes_judge_context():
    judge = {"valid_votes": 3, "flip": True,
             "votes": [{"verdict": False, "reason": "答非所问"}]}
    record = _record("qj", ok=False, kind="J", case_type="open_qa", judge=judge)
    report = build_report([record], config=RunConfig.for_baseline("A"))
    failure = report["failures"][0]
    assert failure["judge"] is not None
    assert failure["judge"]["votes"][0]["reason"] == "答非所问"


def test_write_report_avoids_same_stamp_collision(tmp_path: Path):
    records = [_record("q1", ok=True)]
    first = write_report(records, config=RunConfig.for_baseline("A"),
                         reports_dir=tmp_path)
    second = write_report(records, config=RunConfig.for_baseline("A"),
                          reports_dir=tmp_path)
    assert first != second
    assert second.exists()
