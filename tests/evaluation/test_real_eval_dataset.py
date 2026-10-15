"""real_eval 数据集与 manifest（issue #1005）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.real_eval.dataset import (
    CASE_TYPES,
    RealEvalCase,
    dataset_digest,
    load_cases,
    load_manifest,
)


def test_dataset_shape_and_counts():
    cases = load_cases()
    assert len(cases) == 34
    assert len({case.name for case in cases}) == 34, "题名不得重复"
    assert sum(1 for c in cases if c.split == "train") == 22
    assert sum(1 for c in cases if c.split == "test") == 12
    assert sum(1 for c in cases if c.assertion_kind == "D") == 26
    assert sum(1 for c in cases if c.assertion_kind == "J") == 8


def test_every_case_is_well_formed():
    for case in load_cases():
        case.validate_row()
        assert case.case_type in CASE_TYPES
        assert case.task
        assert case.assertion_kind in {"D", "J"}
        assert case.source
        if case.assertion_kind == "J":
            assert case.judge_criteria, f"{case.name} J 类必须有 judge_criteria"


def test_manifest_counts_match_dataset():
    cases = load_cases()
    manifest = load_manifest()
    counts = manifest["counts"]
    assert counts["total"] == len(cases) == 34
    assert counts["train"] == 22 and counts["test"] == 12
    assert counts["D"] == 26 and counts["J"] == 8
    names = {entry["name"] for entry in manifest["cases"]}
    assert names == {case.name for case in cases}


def test_manifest_documents_deviation():
    manifest = load_manifest()
    assert manifest["excluded_or_replaced"], "必须记录替换/排除理由"
    assert any(
        "FlakyAddTool" in item["what"] for item in manifest["excluded_or_replaced"]
    )


def _write(dataset_dir: Path, rows: list[dict]) -> None:
    dataset_dir.mkdir(parents=True, exist_ok=True)
    (dataset_dir / "train.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_load_cases_rejects_duplicate_names(tmp_path: Path):
    row = {"name": "dup", "task": "t", "case_type": "tool_selection",
           "assertion": {"kind": "D"}, "expected": {}, "split": "train",
           "tags": [], "source": "s"}
    _write(tmp_path, [row, {**row, "task": "t2"}])
    with pytest.raises(ValueError, match="重名"):
        load_cases(tmp_path)


def test_validate_row_requires_kind(tmp_path: Path):
    # 缺 assertion.kind ⇒ 加载期 raise（不静默回落 D）
    row = {"name": "n", "task": "t", "case_type": "tool_selection",
           "expected": {}, "split": "train", "tags": [], "source": "s"}
    _write(tmp_path, [row])
    with pytest.raises(ValueError, match="assertion.kind"):
        load_cases(tmp_path)


def test_validate_row_rejects_kind_case_type_mismatch():
    # open_qa 必须 J；标成 D ⇒ 不匹配即 raise
    case = RealEvalCase(name="n", task="t", case_type="open_qa",
                        assertion={"kind": "D"}, split="train")
    with pytest.raises(ValueError, match="不匹配"):
        case.validate_row()
    # tool_selection 必须 D；标成 J ⇒ 同样 raise
    bad = RealEvalCase(name="m", task="t", case_type="tool_selection",
                       assertion={"kind": "J"}, split="train")
    with pytest.raises(ValueError, match="不匹配"):
        bad.validate_row()


def test_dataset_digest_changes_with_content(tmp_path: Path):
    row = {"name": "n", "task": "t", "case_type": "tool_selection",
           "assertion": {"kind": "D"}, "expected": {}, "split": "train",
           "tags": [], "source": "s"}
    _write(tmp_path, [row])
    first = dataset_digest(tmp_path)
    _write(tmp_path, [{**row, "task": "changed"}])
    assert dataset_digest(tmp_path) != first
