"""W-30（#384）增补判据的集成契约测试。

测的是「Spec/集成证据」这部分的机器可读契约，不是判据实现本身：
- `docs/live_gate/w30/criteria.json`：4 条增补判据的机器定义（backend 判定器消费它）；
- `docs/live_gate/w30/criteria.schema.json`：它的 JSON Schema；
- `goal/.../spec/12_OBSERVABILITY_EVALUATION.md` §9.2：人读规格与机器定义一致。

判据实现（真实模型跑出来的 PASS/FAIL）在 intelligence-agent-backend 仓库，不在本票。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

REPO = Path(__file__).resolve().parent.parent.parent
W30_DIR = REPO / "docs" / "live_gate" / "w30"
CRITERIA_JSON = W30_DIR / "criteria.json"
CRITERIA_SCHEMA_JSON = W30_DIR / "criteria.schema.json"
SPEC_12 = (
    REPO
    / "goal"
    / "Lightweight_Observable_Agent_Harness_Spec"
    / "docs"
    / "spec"
    / "12_OBSERVABILITY_EVALUATION.md"
)

EXPECTED_CHECK_IDS = ("W30-C1", "W30-C2", "W30-C3", "W30-C4")
EXPECTED_RULING_IDS = ("R1", "R2", "R3", "R4")
LIVE_VERDICTS = ("PASS", "FAIL", "BLOCKED", "SKIPPED")


@pytest.fixture(scope="module")
def criteria() -> dict:
    return json.loads(CRITERIA_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def criteria_schema() -> dict:
    return json.loads(CRITERIA_SCHEMA_JSON.read_text(encoding="utf-8"))


def test_criteria_files_exist() -> None:
    assert CRITERIA_JSON.is_file(), "缺 docs/live_gate/w30/criteria.json"
    assert CRITERIA_SCHEMA_JSON.is_file(), "缺 docs/live_gate/w30/criteria.schema.json"


def test_criteria_validates_against_schema(
    criteria: dict, criteria_schema: dict
) -> None:
    jsonschema.validate(instance=criteria, schema=criteria_schema)


def test_exactly_four_criteria_with_stable_ids(criteria: dict) -> None:
    check_ids = [c["check_id"] for c in criteria["criteria"]]
    assert check_ids == list(EXPECTED_CHECK_IDS), check_ids


def test_each_criterion_covers_ticket_contract(criteria: dict) -> None:
    """4 条判据恰好覆盖票面 4 条明确契约（顺序即票面顺序），不多少。"""
    contracts = [c["ticket_contract"] for c in criteria["criteria"]]
    assert "逐字一致" in contracts[0] and "重放验证" in contracts[0]
    assert "R-042" in contracts[1] and "行为证据" in contracts[1]
    assert "source_event_id" in contracts[2] and "不得重走" in contracts[2]
    assert "无闪烁" in contracts[3] and "W-20" in contracts[3]


def test_all_four_user_rulings_have_landing(criteria: dict) -> None:
    """用户拍板的 4 条口径（2026-10-11）每条至少落到一条判据上。"""
    ruling_ids = [r["id"] for r in criteria["user_rulings"]]
    assert ruling_ids == list(EXPECTED_RULING_IDS), ruling_ids
    landed: set[str] = set()
    for c in criteria["criteria"]:
        landed.update(c["user_rulings"])
    assert landed == set(EXPECTED_RULING_IDS), f"未落点口径: {set(EXPECTED_RULING_IDS) - landed}"


def test_harness_inputs_exist_in_repo(criteria: dict) -> None:
    """判据「消费 W-21 harness」必须是接地的：每个 harness_input 指到本仓真实路径。"""
    missing = [
        h["path"]
        for c in criteria["criteria"]
        for h in c["harness_inputs"]
        if not (REPO / h["path"]).exists()
    ]
    assert not missing, f"harness 输入路径不存在: {missing}"


def test_evidence_fields_bind_sha_and_tree(criteria: dict) -> None:
    """票面验收：证据文件绑定 commit sha 与 tree hash。"""
    for c in criteria["criteria"]:
        fields = c["evidence_fields"]
        assert "sha" in fields, f'{c["check_id"]} 缺 sha 绑定'
        assert "tree" in fields, f'{c["check_id"]} 缺 tree 绑定'


def test_verdict_values_match_live_evidence_contract(criteria: dict) -> None:
    """判定四态与 evaluation/live_gate/schema.py 的 Verdict 一致（PASS/FAIL/BLOCKED/SKIPPED）。"""
    for c in criteria["criteria"]:
        assert tuple(c["verdict_values"]) == LIVE_VERDICTS, c["check_id"]


def test_no_mock_or_fake_counted_as_pass(criteria: dict) -> None:
    """票面冻结：不得用 mock 成功或 UI 绿勾代替真实模型证据。"""
    for c in criteria["criteria"]:
        assert c["forbid_mock"] is True, c["check_id"]
        assert "mock" in c["evidence_rule"].lower(), c["check_id"]


def test_spec_section_lists_same_check_ids(criteria: dict) -> None:
    """人读规格（spec §9.2）与机器定义（criteria.json）的 check_id 集合一致，不漂移。"""
    spec_text = SPEC_12.read_text(encoding="utf-8")
    assert "## 9.2" in spec_text or "§9.2" in spec_text, "spec 缺 §9.2 W-30 节"
    for check_id in EXPECTED_CHECK_IDS:
        assert check_id in spec_text, f"spec §9.2 缺 {check_id}"
    for ruling_id in EXPECTED_RULING_IDS:
        assert ruling_id in spec_text, f"spec §9.2 缺口径 {ruling_id}"
