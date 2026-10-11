"""W-30（`#384`）增补判据的集成契约测试。

测的是「Spec/集成证据」这部分的机器可读契约，不是判据实现本身：
- `docs/live_gate/w30/criteria.json`：4 条增补判据的机器定义（backend 判定器消费它）；
- `docs/live_gate/w30/criteria.schema.json`：它的 JSON Schema；
- `goal/.../spec/12_OBSERVABILITY_EVALUATION.md` §9.2：人读规格与机器定义一致。

判据实现（真实模型跑出来的 PASS/FAIL）在 intelligence-agent-backend 仓库，不在本票。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from jsonschema import FormatChecker
from jsonschema.exceptions import ValidationError

REPO = Path(__file__).resolve().parent.parent.parent
W30_DIR = REPO / "docs" / "live_gate" / "w30"
CRITERIA_JSON = W30_DIR / "criteria.json"
CRITERIA_SCHEMA_JSON = W30_DIR / "criteria.schema.json"
CONTRACT_MD = W30_DIR / "00-integration-contract.md"
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


@pytest.fixture(scope="module")
def criteria_by_id(criteria: dict) -> dict:
    return {c["check_id"]: c for c in criteria["criteria"]}


def spec_section_92() -> str:
    """只取 spec 的 §9.2 文本块（### 9.2 到 ## 10 之间），不扫全 spec。"""
    lines = SPEC_12.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("### 9.2 "))
    end = next(i for i, line in enumerate(lines) if i > start and line.startswith("## 10"))
    assert start < end, "spec §9.2 边界异常"
    return "\n".join(lines[start:end])


def test_criteria_files_exist() -> None:
    assert CRITERIA_JSON.is_file(), "缺 docs/live_gate/w30/criteria.json"
    assert CRITERIA_SCHEMA_JSON.is_file(), "缺 docs/live_gate/w30/criteria.schema.json"


def test_criteria_validates_against_schema(
    criteria: dict, criteria_schema: dict
) -> None:
    # jsonschema 默认不启用 format checker；显式传入让 frozen_at / decided_at
    # 的 `format: date` 真被断言（否则日期约束形同注释）。
    jsonschema.validate(
        instance=criteria, schema=criteria_schema, format_checker=FormatChecker()
    )


def test_criteria_rejects_bad_dates(criteria: dict, criteria_schema: dict) -> None:
    """format checker 生效的反向证据：非法日期必须被 schema 拒掉。"""
    bad = copy.deepcopy(criteria)
    bad["frozen_at"] = "not-a-date"
    bad["user_rulings"][0]["decided_at"] = "2026-13-99"
    with pytest.raises(ValidationError):
        jsonschema.validate(
            instance=bad, schema=criteria_schema, format_checker=FormatChecker()
        )


def test_verdict_values_reject_duplicates(
    criteria: dict, criteria_schema: dict
) -> None:
    """schema 层 uniqueItems 生效：重复 verdict 值必须被拒掉。"""
    bad = copy.deepcopy(criteria)
    bad["criteria"][0]["verdict_values"] = ["PASS", "PASS", "PASS", "PASS"]
    with pytest.raises(ValidationError):
        jsonschema.validate(
            instance=bad, schema=criteria_schema, format_checker=FormatChecker()
        )


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
    """用户拍板的 4 条口径（2026-10-11）每条在语义上真实表达，不止标签出现。"""
    ruling_ids = [r["id"] for r in criteria["user_rulings"]]
    assert ruling_ids == list(EXPECTED_RULING_IDS), ruling_ids
    landed: set[str] = set()
    for c in criteria["criteria"]:
        landed.update(c["user_rulings"])
    assert landed == set(EXPECTED_RULING_IDS), f"未落点口径: {set(EXPECTED_RULING_IDS) - landed}"

    by_id = {c["check_id"]: c for c in criteria["criteria"]}

    def rules_of(c: dict) -> str:
        return c["decision_point"] + c["pass_rule"] + c["fail_rule"]

    # R4（清单一致限定）：在 C1 的 fail_rule / pass_rule 里被真实表达
    #（plan_updated_ref 作为区分"合法更新 vs 丢项"的输入字段）。
    c1 = by_id["W30-C1"]
    c1_rules = rules_of(c1)
    assert "plan" in c1_rules.lower(), "R4 未在 C1 规则中真实表达"
    assert "plan_updated_ref" in c1["evidence_fields"], "R4 缺 plan_updated 区分输入字段"

    # R3（失败重试限定）：在 C3 的规则里被真实表达（"新证据 / 修正输入"
    # 的证据字段 + "关键参数"的定义）。
    c3 = by_id["W30-C3"]
    c3_rules = rules_of(c3)
    assert "新证据" in c3_rules and "修正输入" in c3_rules, "R3 未在 C3 规则中真实表达"
    assert "关键参数" in c3["decision_point"], 'R3 未定义"关键参数"'
    assert {"new_evidence_refs", "input_diff_summary"} <= set(
        c3["evidence_fields"]
    ), "R3 缺新证据 / 修正输入捕获字段"

    # R2（依赖边含义）：每条判据的 harness_inputs 都消费 W-21 harness 产物
    #（artifact 措辞里显式出现），而不是只挂标签。
    for c in criteria["criteria"]:
        artifacts = " ".join(h["artifact"] for h in c["harness_inputs"])
        assert "harness" in artifacts.lower() or "W-2" in artifacts, (
            f'{c["check_id"]} harness_inputs 未真实表达 R2 依赖边'
        )

    # R1（判定器顺序）：流程口径，不落单条判据的 pass/fail；其真实落点在
    # 集成契约 §1 与 spec §9.2.2。
    contract_text = CONTRACT_MD.read_text(encoding="utf-8")
    assert "最终发布 Gate 执行前" in contract_text, "R1 未在集成契约 §1 真实表达"
    assert "判定器" in spec_section_92(), "R1 未在 spec §9.2 真实表达"


def test_c2_violation_boundary_is_stated(criteria_by_id: dict) -> None:
    """C2 的「违反」有判定边界：直接违反自动判，间接违反（代理链 / 脚本）
    如实声明不自动判定并留人工复核，不留任意解释空间。"""
    c2 = criteria_by_id["W30-C2"]
    rules = c2["decision_point"] + c2["pass_rule"] + c2["fail_rule"]
    assert "直接违反" in rules, "C2 未定义直接违反"
    assert "间接违反" in rules and "人工复核" in rules, "C2 未声明间接违反的判定边界"


def test_c4_sampling_window_fields(criteria_by_id: dict) -> None:
    """C4 的 fail_rule 要求检出中间态闪烁，证据字段必须有采样窗口定义 +
    窗口内项数最小值，否则两个端点计数结构上检不出（端点恒相等）。"""
    c4 = criteria_by_id["W30-C4"]
    fields = set(c4["evidence_fields"])
    assert {"sample_window_ms", "min_item_count_during_window"} <= fields, (
        f"C4 缺采样窗口 / 中间态证据字段: {sorted(fields)}"
    )
    assert "sample_window_ms" in c4["decision_point"], "C4 decision_point 未定义采样窗口"
    assert "min_item_count_during_window" in c4["fail_rule"], (
        "C4 fail_rule 未说明中间态的机械检出方式"
    )


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
    """票面冻结：不得用 mock 成功或 UI 绿勾代替真实模型证据。

    不只钉声明层（forbid_mock / mock 字样），还钉判定逻辑层的"上限锁 FAIL"
    语义：mock 证据一律锁死为 FAIL，不得计入 PASS。
    """
    for c in criteria["criteria"]:
        assert c["forbid_mock"] is True, c["check_id"]
        assert "mock" in c["evidence_rule"].lower(), c["check_id"]
        assert "上限锁 FAIL" in c["evidence_rule"], (
            f'{c["check_id"]} evidence_rule 缺"上限锁 FAIL"语义'
        )


def test_spec_section_lists_same_check_ids() -> None:
    """人读规格（spec §9.2）与机器定义（criteria.json）的 check_id 集合一致，不漂移。

    限定在 §9.2 文本块内检查：ID 出现在 spec 别处不能算 §9.2 覆盖。
    """
    section = spec_section_92()
    assert "## 9.2" in section or "§9.2" in section or "9.2 W-30" in section, (
        "spec 缺 §9.2 W-30 节"
    )
    for check_id in EXPECTED_CHECK_IDS:
        assert check_id in section, f"spec §9.2 缺 {check_id}"
    for ruling_id in EXPECTED_RULING_IDS:
        assert ruling_id in section, f"spec §9.2 缺口径 {ruling_id}"
