"""real_eval 数据集与 manifest（issue #1005）。"""

from __future__ import annotations

from evaluation.real_eval.dataset import CASE_TYPES, load_cases, load_manifest


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
