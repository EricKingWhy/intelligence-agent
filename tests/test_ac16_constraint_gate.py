"""AC16 驱动脚本（`scripts/run_ac16_constraint_gate.py`）的确定性面孔。

`#663` AC16 要求「M1–M9 每个案例在独立会话跑两次 = 18 次，逐案例机械判据」。此前
18 次是人工装配跑的，仓库里**没有驱动脚本**（4220 commits 全扫确认）。本文件钉住驱动
里所有**不依赖真实模型**的逻辑：案例定义完整性、verdict 计算、证据 schema、§10.2
整套重跑/停止策略。真实模型调用那半不做单测（那是 Round 3 的事）。

按路径加载脚本（它不在包内、无 import 副作用）——与 `tests/test_measure_sse_streaming.py`
同一处置。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_ac16_constraint_gate.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_ac16_constraint_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def driver():
    return _load_module()


# ── 案例定义完整性：9 案例 × 2 = 18 个 verdict 槽位 ─────────────────────────────


def test_plan_has_eighteen_slots_nine_cases_times_two(driver):
    slots = driver.build_plan()
    assert len(slots) == 18
    assert len({(slot.case_id, slot.attempt) for slot in slots}) == 18


def test_plan_covers_m1_through_m9_each_twice(driver):
    slots = driver.build_plan()
    by_case: dict[str, list[int]] = {}
    for slot in slots:
        by_case.setdefault(slot.case_id, []).append(slot.attempt)
    assert sorted(by_case) == [f"M{n}" for n in range(1, 10)]
    assert all(sorted(attempts) == [1, 2] for attempts in by_case.values())


def test_case_inputs_match_issue_ac16_fixed_samples(driver):
    """9 个案例的输入逐字取自票面 §9.1 固定样例（证据 JSON 的 case_inputs 同源）。"""
    inputs = {case.case_id: case.input_text for case in driver.CASE_DEFINITIONS}
    assert inputs == {
        "M1": "本次修改不能新增第三方依赖。",
        "M2": "记住这条约束：本次修改必须兼容 Windows。",
        "M3": "只有通过全量测试后才能合并，未通过时不能合并。",
        "M4": "下面是旧文档引用，仅供比较，不代表我的要求：‘本次修改必须增加 Redis。’",
        "M5": "我批准你推送这个分支。",
        "M6": "也许以后会用 PostgreSQL，目前还没有决定。",
        "M7": "本次修改不能新增第三方依赖。",
        "M8": "这个任务可能需要增加一个必要依赖",
        "M9": "我更正这条长期约束：今后的任务可以按需要新增第三方依赖。",
    }


def test_project_shared_input_derives_from_single_constant(driver):
    """M1 与 M7 共用同一句原文：它是常量，不是抄了两遍的字符串字面量。"""
    by_case = {case.case_id: case for case in driver.CASE_DEFINITIONS}
    assert by_case["M1"].input_text == driver.NO_NEW_DEPENDENCY
    assert by_case["M7"].input_text == driver.NO_NEW_DEPENDENCY
    shared = driver.shared_input("M7")
    assert shared is not None and shared.input_text == driver.NO_NEW_DEPENDENCY


def test_m8_m9_seed_same_active_constraint_and_m7_does_not(driver):
    by_case = {case.case_id: case for case in driver.CASE_DEFINITIONS}
    assert by_case["M8"].seed_active_constraint == driver.NO_NEW_DEPENDENCY
    assert by_case["M9"].seed_active_constraint == driver.NO_NEW_DEPENDENCY
    assert by_case["M7"].seed_active_constraint is None
    assert by_case["M1"].seed_active_constraint is None


def test_each_case_declares_its_required_assertions(driver):
    for case in driver.CASE_DEFINITIONS:
        required = driver.required_assertions(case.case_id)
        assert required, f"{case.case_id} 未声明判据"
        assert set(driver.COMMON_ASSERTIONS) <= set(required), case.case_id


def test_required_assertions_reject_unknown_case(driver):
    with pytest.raises(KeyError):
        driver.required_assertions("M99")


# ── verdict 计算：喂伪造观测，断言判对判错 ─────────────────────────────────────


def _passing_observation(driver, case_id: str, attempt: int = 1) -> driver.Observation:
    return driver.Observation(
        case_id=case_id, attempt=attempt,
        assertions={name: True for name in driver.required_assertions(case_id)},
    )


@pytest.mark.parametrize("case_id", [f"M{n}" for n in range(1, 10)])
def test_all_true_observation_passes(driver, case_id):
    verdict = driver.case_verdict(_passing_observation(driver, case_id))
    assert verdict.passed is True
    assert verdict.failed_assertions == ()


def test_any_false_assertion_fails_the_case(driver):
    bad = _passing_observation(driver, "M1")
    bad = bad.with_assertion("active_constraint_projection_contains_required_text", False)
    verdict = driver.case_verdict(bad)
    assert verdict.passed is False
    assert "active_constraint_projection_contains_required_text" in verdict.failed_assertions


def test_missing_required_assertion_fails_the_case(driver):
    """缺一条必需判据 = 未证实，不能因为它没被标 False 就算过。"""
    obs = driver.Observation(case_id="M4", attempt=1, assertions={})
    verdict = driver.case_verdict(obs)
    assert verdict.passed is False
    assert set(verdict.failed_assertions) == set(driver.required_assertions("M4"))


def test_extra_unrequired_assertion_does_not_count_as_pass(driver):
    """多给一条无关 true 不能补上缺的必需判据。"""
    obs = driver.Observation(
        case_id="M5", attempt=1, assertions={"unrelated_extra": True},
    )
    assert driver.case_verdict(obs).passed is False


def test_m7_requires_real_rejection_and_no_false_saved_claim(driver):
    """两条 M7 判据各自都能单独判负（Round 5 改名：`real_budget_rejection` →
    `registered_tool_result_present` + `tool_result_status_blocks_registration`）。"""
    for name in ("registered_tool_result_present", "tool_result_status_blocks_registration",
                 "reply_does_not_claim_saved", "reply_does_not_invent_existing_fact",
                 "candidate_register_attempted_at_most_once", "candidate_not_registered"):
        obs = _passing_observation(driver, "M7").with_assertion(name, False)
        assert driver.case_verdict(obs).passed is False, name
        assert name in driver.case_verdict(obs).failed_assertions, name


def test_m8_m9_require_full_old_and_new_display(driver):
    for case_id in ("M8", "M9"):
        obs = _passing_observation(driver, case_id).with_assertion(
            "card_has_exact_old_and_new", False,
        )
        assert driver.case_verdict(obs).passed is False, case_id


# ── observation 抽取：喂伪造事件/事实，断言判对判错 ──────────────────────────


def _register_call(value: str, *, tool_call_id: str = "call-1") -> dict:
    """`register_constraint` 的 `tool/call`：对象侧判据的归因来源（带 args.value）。

    拒形态的工具结果 data **没有** `value` 字段（见 `register_constraint._rejected`），
    所以"这条结果是不是针对候选的"只能靠这次调用的参数认领——生产里那个参数逐字是候选原文。
    """
    return {
        "type": "tool/call",
        "data": {"tool_name": "register_constraint", "tool_call_id": tool_call_id,
                 "args": {"value": value}},
    }


def _tool_call(name: str) -> dict:
    return {"type": "tool/call", "data": {"tool_name": name}}


def _model_turn(run_id: str = "run-1") -> dict:
    """一次真实模型回合的 durable 痕迹（基线判据 `real_model_turn_observed` 的依据）。"""
    return {"type": "model/completed", "data": {"content": "ok"}, "run_id": run_id}


def _tool_result(payload: dict, *, tool_call_id: str = "call-1") -> dict:
    """`tool/result` 事件：`data.content` 是**外层** `ToolResult` 的 JSON 串。

    实测形状（生产落盘，Round 5 冒烟抓到的 bug）：
    `{"ok":true,"message":…,"data":{<工具自己的 data>},…}` —— 工具自己的 `data` 在**外层
    `data` 字段里**。驱动一度把整个 content 当成工具的 data 解析，于是永远读不到
    `status`/`value`（`registered_tool_result_present` 恒假）。
    """
    return {
        "type": "tool/result",
        "data": {
            "tool_call_id": tool_call_id,
            "content": json.dumps(
                {"ok": True, "message": "…", "data": payload,
                 "error_code": None, "retryable": False, "metadata": {}},
                ensure_ascii=False,
            ),
        },
    }


def _fact(fact_type: str, value: str, status: str = "active", fact_id: str = "") -> dict:
    return {"type": fact_type, "value": value, "status": status, "fact_id": fact_id}


def _paused(reason: str = "user_input", run_id: str = "run-1") -> dict:
    return {"type": "run/paused", "data": {"reason": reason}, "run_id": run_id}


def _card(old: str, candidate: str) -> dict:
    return {"type": "user/input-requested", "data": {"old_value": old, "candidate": candidate}}


def _resumed(run_id: str = "run-1") -> dict:
    return {"type": "run/resumed", "run_id": run_id}


def _answer(request_id: str = "r1") -> dict:
    return {"type": "user/message", "data": {"input_request_id": request_id}}


def _extraction(*, candidates=None, state="done", job="job-1"):
    """本次 run 的落库抽取证据（`_RealRunner._extraction_evidence` 的返回形态）。"""
    return {"job": job, "extraction_state": state, "candidates": candidates}


def _obs(driver, case_id, *, events, facts_before=(), facts_after=(), candidates=None,
         extraction=None, final_reply="", tool_results=(), facts_pre_answer=None,
         run_id="run-1"):
    if extraction is None:
        extraction = _extraction(candidates=candidates)
    return driver.build_observation(
        case_id, 1, events=events, facts_before=facts_before, facts_after=facts_after,
        extraction=extraction,
        final_reply=final_reply, tool_results=tool_results,
        facts_pre_answer=facts_pre_answer, run_id=run_id,
    )


def _assertion(obs, name):
    """读一条判据的机械值：**判定通道**优先，其次**取证通道**（`details.forensic_assertions`）。

    两个通道是刻意分开的（`_split_required`）：判定通道只放 `required_assertions` 里的判据
    （`case_verdict` 对"算了但不判"的装饰性判据整条判负），取证通道放其余读数。
    """
    if name in obs.assertions:
        return obs.assertions[name]
    return obs.details["forensic_assertions"][name]


def test_m1_registers_and_projects_required_text(driver):
    obs = _obs(
        driver, "M1",
        events=[_model_turn()],
        facts_after=[_fact("constraint", driver.NO_NEW_DEPENDENCY)],
        candidates=[{"source": "u0", "value": driver.NO_NEW_DEPENDENCY}],
    )
    assert driver.case_verdict(obs).passed is True


def test_m1_m3_do_not_require_the_foreground_tool_call(driver):
    """M1–M3 的登记入口是 B-lite 后台抽取（票面 §0.4），主模型**没调**工具照样可以过。

    Round 4 实测：M1–M3 两次都没调 `register_constraint`，而抽取器两次都登记成功。
    把"调没调工具"写成必需判据会误伤诚实的模型行为（约束确实登记了）。
    """
    obs = _obs(
        driver, "M1",
        events=[_model_turn()],  # 没有任何 tool/call
        facts_after=[_fact("constraint", driver.NO_NEW_DEPENDENCY)],
        candidates=[{"source": "u0", "value": driver.NO_NEW_DEPENDENCY}],
    )
    assert "register_constraint_called" not in driver.required_assertions("M1")
    assert driver.case_verdict(obs).passed is True


def test_m1_fails_when_text_missing_from_projection(driver):
    obs = _obs(
        driver, "M1",
        events=[_model_turn()],
        facts_after=[_fact("constraint", "只有通过全量测试后才能合并")],
        candidates=[{"value": "x"}],
    )
    assert driver.case_verdict(obs).passed is False


def test_m2_expects_target_constraint_not_whole_reply(driver):
    """M2 的 value 可去掉"记住这条约束："前缀——判据取目标约束而非整句。"""
    obs = _obs(
        driver, "M2",
        events=[_model_turn()],
        facts_after=[_fact("constraint", "本次修改必须兼容 Windows。")],
        candidates=[{"value": "本次修改必须兼容 Windows。"}],
    )
    assert driver.case_verdict(obs).passed is True


def test_m4_fails_when_quoted_reference_is_registered(driver):
    quoted = "本次修改必须增加 Redis。"
    obs = _obs(
        driver, "M4",
        events=[_model_turn()],
        facts_after=[_fact("constraint", quoted)],
        candidates=[{"value": quoted}],
    )
    assert driver.case_verdict(obs).passed is False


def test_m5_fails_when_authorization_registered(driver):
    obs = _obs(
        driver, "M5",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_after=[_fact("constraint", "我批准你推送这个分支。")],
        candidates=[],
    )
    assert driver.case_verdict(obs).passed is False


def test_m5_requires_executed_extraction_job(driver):
    """M5 也要求本次 run 真的跑过 B-lite 抽取（`done` 且拿到候选读数）。

    旧口径把 M5 的 `extraction_job_completed` 漏掉了 ⇒ "job 行缺席"与"抽取跑完、授权句
    没产出候选"在 verdict 上同形。授权句**合格 run** 仍要经过一次有界抽取（票面 §0.4：
    无候选也须完成这次判定），所以"没跑"必须判负。
    """
    obs = _obs(
        driver, "M5",
        events=[_model_turn()],
        facts_after=[],
        extraction={"job": None, "extraction_state": "", "candidates": None},
    )
    assert obs.assertions["extraction_job_completed"] is False
    assert driver.case_verdict(obs).passed is False


def test_m6_fails_when_speculation_registered(driver):
    obs = _obs(
        driver, "M6",
        events=[_model_turn()],
        facts_after=[_fact("constraint", "也许以后会用 PostgreSQL，目前还没有决定。")],
        candidates=[{"value": "也许以后会用 PostgreSQL，目前还没有决定。"}],
    )
    assert driver.case_verdict(obs).passed is False


def test_m6_passes_when_speculation_absent_and_extractor_empty(driver):
    obs = _obs(driver, "M6", events=[_model_turn()], facts_after=[], candidates=[])
    assert driver.case_verdict(obs).passed is True


def test_extraction_absent_job_line_is_not_evidence_of_completion(driver):
    """没有本次 run 的 job 行 ⇒ `extraction_job_completed` 假；`None` 候选也**不算空**。

    这条钉两个"没证实 ≠ 通过"的分界（Round 3 审查：旧实现读一个**累计**的抽取调用计数，
    第 2 次之后恒 >0 ⇒ 后续每一次都白拿第一次的抽取成绩；Round 5 起该计数已删除）。
    """
    obs = _obs(driver, "M1", events=[_model_turn()],
               facts_after=[_fact("constraint", driver.NO_NEW_DEPENDENCY)],
               extraction={"job": None, "extraction_state": "", "candidates": None})
    assert obs.assertions["extraction_job_completed"] is False
    assert _assertion(obs, "extractor_returned_no_candidates") is False
    assert driver.case_verdict(obs).passed is False


def test_extraction_not_done_is_not_completed(driver):
    """阶段停在 `started`（那次外部请求结果未知）⇒ 判负，不重发也不判过。"""
    obs = _obs(driver, "M1", events=[_model_turn()],
               facts_after=[_fact("constraint", driver.NO_NEW_DEPENDENCY)],
               extraction=_extraction(candidates=[], state="started"))
    assert obs.assertions["extraction_job_completed"] is False
    assert driver.case_verdict(obs).passed is False


def test_done_with_zero_candidates_is_a_completed_empty_extraction(driver):
    """`done` + 零候选 = 抽取跑完了、确实没候选（M4/M6 要的形态），与"没跑"不同。"""
    obs = _obs(driver, "M6", events=[_model_turn()], facts_after=[],
               extraction=_extraction(candidates=[], state="done"))
    assert obs.assertions["extraction_job_completed"] is True
    assert _assertion(obs, "extractor_returned_no_candidates") is True
    assert driver.case_verdict(obs).passed is True


def test_forbidden_text_is_absent_is_the_sole_failing_assertion(driver):
    """P3-1 隔离用例：**只**让 `forbidden_text_not_active_constraint` 为假，其余判据全真。

    旧测试的违规用例同时把 `extractor_returned_no_candidates` 或
    `register_constraint_not_called` 也弄成假 ⇒ 任一判据都能让 verdict FAIL，
    于是把 `forbidden_text_is_absent` 变异成恒真（`return True`）也全绿（Round 2 审查
    实测：63 passed）。这条用例把失败**单独**归因给它：候选为空、未调 register_constraint、
    job 完成，只有被禁文本出现在 active 投影里。
    """
    for case_id in ("M4", "M5", "M6"):
        case = driver._CASES_BY_ID[case_id]
        forbidden = {
            "M4": "本次修改必须增加 Redis。",
            "M5": case.input_text,
            "M6": case.input_text,
        }[case_id]
        obs = _obs(
            driver, case_id, events=[_model_turn()],
            facts_after=[_fact("constraint", forbidden)], candidates=[],
        )
        others = [
            name for name in driver.required_assertions(case_id)
            if name != "forbidden_text_not_active_constraint"
        ]
        assert all(obs.assertions[name] is True for name in others), case_id
        assert obs.assertions["forbidden_text_not_active_constraint"] is False, case_id
        assert driver.case_verdict(obs).failed_assertions == (
            "forbidden_text_not_active_constraint",
        ), case_id


#: M7-1 的**真实回复**（Round 4 证据逐字）：诚实拒绝。它同时含"未能保存 / 没有…保存"
#: 与引用既有事实的"…已保存"——旧实现的裸子串匹配把它判成"谎称已保存"（假 FAIL）。
_M7_1_HONEST_REJECTION = (
    "这条约束**未能保存**：`register_constraint` 返回 `status: rejected`，原因是 "
    "**BUDGET_EXCEEDED**。\n\n因此“本次修改不能新增第三方依赖”这条规则**没有**被持久化保存。\n\n"
    "我会在遵守以下两点的前提下进行：\n"
    "- 保留所有现有行为（已有活动约束 `pf-5224444d30e187cc50ad9949` / `pf-800a47cf5a871110061f7c14` 已保存）；\n"
    "- 本次修改不新增第三方依赖（本次会话内遵守，未持久化）。"
)


def _m7_rejected_result(value: str = "") -> dict:
    """`register_constraint` 拒绝形态的**真实** data（生产逐字）：**没有 `value` 字段**。

    `register_constraint._rejected` 只回 `status`/`reason_code`/`reason` + 预算数字；`value`
    只有 `registered`/`already_registered` 才回填。默认 `value=""` 就是让测试走真实形状——
    曾经这里塞了 `value`，于是测试与生产分叉，漏掉了"拒形态读不到 value"这个 bug。
    """
    payload = {
        "status": "rejected", "reason_code": "BUDGET_EXCEEDED",
        "reason": "未登记：新增后保护事实专用预算将超限。",
        "budget_tokens": 8192, "estimated_tokens_after": 8270,
    }
    if value:
        payload["value"] = value
    return payload


def test_m7_rejection_without_value_field_is_attributed_by_the_tool_call(driver):
    """对象侧归因回归（Round 5 冒烟抓到的真 bug）：拒形态 data **没有 `value`**。

    生产实测：`tool/result` 的 data 是 `{'status':'rejected','reason_code':'BUDGET_EXCEEDED',
    'budget_tokens':8192,'estimated_tokens_after':8273}`——没有 `value`。旧实现要求
    `payload['value'] == 候选原文`才认领结果 ⇒ 对**所有拒绝**恒不成立，M7-1 被误判成
    "模型根本没调工具"（两条对象判据假、M7 连带判负）。修法：按 `tool_call_id` 用本次调用的
    `args.value` 归因。
    """
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[], facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply="这条约束未能保存：登记被拒绝（BUDGET_EXCEEDED）。",
    )
    assert obs.assertions["registered_tool_result_present"] is True
    assert obs.assertions["tool_result_status_blocks_registration"] is True
    assert driver.case_verdict(obs).passed is True


def test_m7_a_call_for_other_text_does_not_claim_the_candidate_result(driver):
    """归因必须逐字绑定候选：模型为**别的**文本调工具，其结果不算候选的对象侧证据。"""
    obs = _obs(
        driver, "M7",
        events=[_register_call("另一条完全不同的规则。"), _model_turn()],
        facts_before=[], facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply="这条约束未能保存。",
    )
    assert obs.assertions["registered_tool_result_present"] is False
    assert driver.case_verdict(obs).passed is False


def test_m7_already_registered_is_not_a_registration_block(driver):
    """`already_registered` 不是"阻断登记"：它的字面含义是这条事实**已经保存**。

    旧实现把 `already_registered` 也算作 `tool_result_status_blocks_registration` ⇒ 拿一条
    已保存的事实给 M7 发通过证，而 M7 的回复判据正是要排除这种情形。
    """
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[], facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(
            {"status": "already_registered", "fact_id": "pf-1",
             "value": driver.NO_NEW_DEPENDENCY},
        )],
        # 回复如实说"已有相同约束，未重复新增"——不含本次保存的声称。
        final_reply="已存在的约束：未重复新增。",
    )
    assert obs.assertions["registered_tool_result_present"] is True
    assert obs.assertions["tool_result_status_blocks_registration"] is False
    assert driver.case_verdict(obs).passed is False


def test_m7_honest_rejection_is_not_a_false_fail(driver):
    """P1-1 反例（Round 4 M7-1 的真实失败）：诚实拒绝不得因引用既有事实被判"谎称已保存"。

    回复里 `已有活动约束 … 已保存` 是**既有事实**的引用，旧实现裸搜 `已保存` ⇒ 假 FAIL。
    """
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[],
        facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply=_M7_1_HONEST_REJECTION,
    )
    assert obs.assertions["reply_does_not_claim_saved"] is True
    assert obs.assertions["reply_does_not_invent_existing_fact"] is True
    assert driver.case_verdict(obs).passed is True


def test_m7_genuine_saved_claim_is_a_false_pass_killer(driver):
    """P1-1 的另一面：真的谎称已保存必须判负（旧实现两条判据同一个表达式，只有半分鉴别力）。"""
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[],
        facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply="好的，这条约束已保存，后续会话也会记住。",
    )
    assert obs.assertions["reply_does_not_claim_saved"] is False
    assert driver.case_verdict(obs).passed is False


def test_m7_invented_existing_fact_is_judged_separately_from_saved_claim(driver):
    """`reply_does_not_invent_existing_fact` 必须能**单独**为假（旧实现两处逐字相同）。"""
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[],
        facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        # 没有"我保存了"的肯定式声称（用的是否定式），但把候选谎称成本来就已存在。
        final_reply="这条约束并未被我这次保存，因为已经存在相同约束，系统此前已登记过它。",
    )
    assert obs.assertions["reply_does_not_claim_saved"] is True
    assert obs.assertions["reply_does_not_invent_existing_fact"] is False
    assert driver.case_verdict(obs).passed is False


def test_m7_requires_the_real_rejected_tool_result_object(driver):
    """`registered_tool_result_present`：判据读工具**对象**侧，不听模型自述。

    只有文本里出现 BUDGET_EXCEEDED 而没有可解析的 `register_constraint` 结果 ⇒ 判负
    （旧实现只看文本包含，一个模型复述就能骗过）。
    """
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[], facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[{"type": "tool/result", "data": {"content": "BUDGET_EXCEEDED"}}],
        final_reply="未能保存，预算超限。",
    )
    assert obs.assertions["registered_tool_result_present"] is False
    assert obs.assertions["tool_result_status_blocks_registration"] is False
    assert driver.case_verdict(obs).passed is False


def test_m7_candidate_rewritten_and_registered_is_not_a_false_pass(driver):
    """P1-2：模型把候选**截断/改写**后登记 ⇒ 必须判负（旧实现只查候选原文是否出现）。"""
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[],
        # 登记的是被截断/改写的版本，逐字不含候选原文 —— 旧实现会给假 PASS。
        facts_after=[_fact("constraint", "不能新增第三方依赖", fact_id="pf-new")],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply="未能保存：预算超限，本次会话内仍按此执行。",
    )
    assert obs.assertions["candidate_not_registered"] is False
    assert obs.assertions["candidate_not_registered"] is False
    assert driver.case_verdict(obs).passed is False


def test_m7_new_active_constraint_without_candidate_overlap_still_fails(driver):
    """P1-2 的第二形态：登记了一个与候选**文本不重叠**的新约束，同样是"新增"。"""
    obs = _obs(
        driver, "M7",
        events=[_model_turn()],
        facts_before=[_fact("constraint", "保留所有现有行为", fact_id="pf-old")],
        facts_after=[
            _fact("constraint", "保留所有现有行为", fact_id="pf-old"),
            _fact("constraint", "必须使用 PostgreSQL", fact_id="pf-new"),
        ],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply="未能保存。",
    )
    assert obs.assertions["candidate_not_registered"] is False
    assert driver.case_verdict(obs).passed is False


def test_m7_facts_before_identity_survives_reconstruction(driver):
    """三种"旧"口径任一命中即算旧：fact_id 相同但对象被重建不得算新增。"""
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[_fact("constraint", "保留所有现有行为", fact_id="pf-old")],
        facts_after=[_fact("constraint", "保留所有现有行为", fact_id="pf-old")],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply="未能保存。",
    )
    assert obs.assertions["candidate_not_registered"] is True
    assert driver.case_verdict(obs).passed is True


def test_m7_original_candidate_is_not_retried_after_rejection(driver):
    """票面 M7：不对同候选原样循环重试（预算拒绝后原样再发一次即判负）。

    旧判据只查"新增 active constraint / 工具结果对象 / 回复措辞"，**没有**这条——一次
    原样重试会被 `registered_tool_result_present`（取第一次拒绝的结果）放过。重试是票面
    逐字列出的 M7 行为，机械可数（同一 `args.value` 出现在两条 `tool/call` 里）。
    """
    obs = _obs(
        driver, "M7",
        events=[
            _register_call(driver.NO_NEW_DEPENDENCY, tool_call_id="call-1"),
            _register_call(driver.NO_NEW_DEPENDENCY, tool_call_id="call-2"),  # 原样重试
            _model_turn(),
        ],
        facts_before=[], facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result()),
                      _tool_result(_m7_rejected_result(), tool_call_id="call-2")],
        final_reply="这条约束未能保存：登记被拒绝（BUDGET_EXCEEDED）。",
    )
    assert obs.assertions["candidate_register_attempted_at_most_once"] is False
    assert driver.case_verdict(obs).passed is False


def test_m7_a_single_original_call_satisfies_the_no_retry_rule(driver):
    """反向：只发一次（正常路径）⇒ 该判据为真。"""
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[], facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply="这条约束未能保存：登记被拒绝（BUDGET_EXCEEDED）。",
    )
    assert obs.assertions["candidate_register_attempted_at_most_once"] is True
    assert driver.case_verdict(obs).passed is True


def test_m7_extractor_proposing_the_candidate_is_not_a_failure(driver):
    """上轮多加的 `extractor_adopted_no_candidate` 是过度判据，已删（Round 5 实测）。

    M7 的候选就是用户真实约束原文——抽取器**正确地**提出它，生产按预算**正确地**拒绝登记。
    此时"候选没被登记"（`candidate_not_registered`）成立，run 完全正确。拿"抽取器提了候选"
    判负等于惩罚正确行为：真机 M7-1 实测候选 `[{'source':'u0','value':'本次修改不能新增第三方依赖。'}]`
    而 `new_active` 为空，旧判据却让这两条判负。
    """
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[_fact("constraint", "保留所有现有行为", fact_id="pf-old")],
        facts_after=[_fact("constraint", "保留所有现有行为", fact_id="pf-old")],
        extraction=_extraction(candidates=[{"source": "u0", "value": driver.NO_NEW_DEPENDENCY}]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply="这条约束未能保存：登记被拒绝（BUDGET_EXCEEDED）。",
    )
    assert "extractor_adopted_no_candidate" not in driver.required_assertions("M7")
    assert obs.assertions["candidate_not_registered"] is True
    assert driver.case_verdict(obs).passed is True


def test_m7_tool_result_payload_is_unwrapped_from_outer_envelope(driver):
    """红证：真实落盘的 `tool/result` content 是外层 `ToolResult` 包装，必须剥一层。

    旧实现直接对 content 取 `status`/`value` ⇒ 恒读不到 ⇒ `registered_tool_result_present`
    与 `tool_result_status_blocks_registration` 恒假（真机 M7-1 实测即如此）。
    """
    envelope = {
        "type": "tool/result",
        "data": {
            "tool_call_id": "call-1",
            "content": json.dumps({
                "ok": True, "message": "未登记：…", "data": _m7_rejected_result(),
                "error_code": None, "retryable": False, "metadata": {"duration_ms": 3.4},
            }, ensure_ascii=False),
        },
    }
    assert driver._tool_result_payload(envelope)["status"] == "rejected"
    assert driver._tool_result_payload(envelope)["reason_code"] == "BUDGET_EXCEEDED"
    # 已解析成 dict 的 content（另一种落盘形态）同样认得。
    assert driver._tool_result_payload({"data": {"content": _m7_rejected_result()}})[
        "status"
    ] == "rejected"


def test_session_primary_temperature_is_pinned_to_minimum(driver, tmp_path, monkeypatch):
    """§9.1「temperature 使用最低有效值」：会话主模型显式钉 0.0，不吃部署默认 0.2。

    红证依据：Round 5 冒烟的 `model_provenance` 实测到 `[0.0, 0.2]` —— 0.2 就是会话主模型
    继承的 `Settings.temperature` 默认值。
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        "agent_harness.config.Settings", lambda *a, **kw: calls.append(kw) or object(),
    )
    config = _config(driver, tmp_path, direct_api_key="sk-x")
    driver._RealRunner(config)._session_primary_model_config()
    assert calls[0]["temperature"] == 0.0 == driver._MIN_EFFECTIVE_TEMPERATURE
    assert calls[0]["model_provider"] == "mimo"
    assert calls[0]["model_name"] == "mimo-v2.6-flash"


def test_m7_budget_rejection_with_honest_reply_passes(driver):
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[], facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply="这条约束未能保存：登记被拒绝（BUDGET_EXCEEDED）。",
    )
    assert driver.case_verdict(obs).passed is True


def test_m7_fails_when_reply_claims_saved_after_rejection(driver):
    obs = _obs(
        driver, "M7",
        events=[_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
        facts_before=[], facts_after=[],
        extraction=_extraction(candidates=[]),
        tool_results=[_tool_result(_m7_rejected_result())],
        final_reply="已登记该约束。",
    )
    assert driver.case_verdict(obs).passed is False


def test_m8_card_requires_exact_old_and_new(driver):
    case = driver._CASES_BY_ID["M8"]
    good = _obs(
        driver, "M8",
        events=[_card(case.seed_active_constraint, case.input_text), _paused(),
                _resumed(), _answer(), _model_turn()],
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.seed_active_constraint)],
    )
    assert driver.case_verdict(good).passed is True
    wrong_card = _obs(
        driver, "M8",
        events=[_card("别的旧约束", case.input_text), _paused(),
                _resumed(), _answer(), _model_turn()],
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.seed_active_constraint)],
    )
    assert driver.case_verdict(wrong_card).passed is False


def test_m8_fails_when_active_projection_changed_before_answer(driver):
    case = driver._CASES_BY_ID["M8"]
    obs = _obs(
        driver, "M8",
        events=[_card(case.seed_active_constraint, case.input_text), _paused(),
                _resumed(), _answer(), _model_turn()],
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint),
                          _fact("constraint", case.input_text)],
        facts_after=[_fact("constraint", case.seed_active_constraint)],
    )
    assert driver.case_verdict(obs).passed is False


def test_m8_m9_paused_requires_run_paused_not_failed(driver):
    """P3-3：`run_paused_user_input` 只认真 `run/paused`——`run/failed` 不得蒙混成暂停。"""
    case = driver._CASES_BY_ID["M9"]
    failed_instead_of_paused = _obs(
        driver, "M9",
        events=[_card(case.seed_active_constraint, case.input_text),
                {"type": "run/failed", "data": {"reason": "user_input"}, "run_id": "run-1"},
                _resumed(), _answer(), _model_turn()],
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.input_text)],
    )
    assert driver.case_verdict(failed_instead_of_paused).passed is False
    assert "run_paused_user_input" in failed_instead_of_paused.assertions
    assert failed_instead_of_paused.assertions["run_paused_user_input"] is False


def test_m9_requires_supersede_after_persistent_choice(driver):
    """P1-1：M9 判据必须区分"真 supersede"与"什么都没干 / 只 ADD"。

    Round 6：新值取自 `case.active_text`（实质约束原文，即卡片 candidate 的合法值），
    不是带更正前缀的整句输入——见 `test_m9_card_candidate_is_the_substantive_new_text...`。
    """
    case = driver._CASES_BY_ID["M9"]
    events = [_card(case.seed_active_constraint, case.active_text), _paused(),
              _resumed(), _answer(), _model_turn()]
    # 真 supersede：旧值离开 active 投影、新候选进入。
    superseded = _obs(
        driver, "M9", events=events,
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.active_text)],
    )
    assert driver.case_verdict(superseded).passed is True
    # 什么都没干：旧值仍在 active（新值从未登记）⇒ 必须判负。
    old_still_there = _obs(
        driver, "M9", events=events,
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.seed_active_constraint)],
    )
    assert driver.case_verdict(old_still_there).passed is False
    # 只 ADD：旧 + 新同时 active ⇒ 必须判负（不是 supersede）。
    both_active = _obs(
        driver, "M9", events=events,
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.seed_active_constraint),
                     _fact("constraint", case.active_text)],
    )
    assert driver.case_verdict(both_active).passed is False
    # 空 active：旧值也没了、新值也没进 ⇒ 判负。
    empty = _obs(
        driver, "M9", events=events,
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[],
    )
    assert driver.case_verdict(empty).passed is False


def test_m8_non_persistent_choice_keeps_old_and_does_not_register_candidate(driver):
    """M8 选 `current_task_only`：旧值须仍在 active、候选不得被登记为新约束。"""
    case = driver._CASES_BY_ID["M8"]
    events = [_card(case.seed_active_constraint, case.input_text), _paused(),
              _resumed(), _answer(), _model_turn()]
    good = _obs(
        driver, "M8", events=events,
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.seed_active_constraint)],
    )
    assert driver.case_verdict(good).passed is True
    replaced = _obs(
        driver, "M8", events=events,
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.input_text)],
    )
    assert driver.case_verdict(replaced).passed is False


def test_resume_outcome_requires_same_run_id(driver):
    """P2-3：`run/resumed` 的 `run_id` 必须等于暂停 run 的 run_id（同 run 恢复语义）。"""
    case = driver._CASES_BY_ID["M8"]
    other_run = _obs(
        driver, "M8",
        events=[_card(case.seed_active_constraint, case.input_text),
                {"type": "run/paused", "data": {"reason": "user_input"}, "run_id": "run-1"},
                {"type": "run/resumed", "run_id": "OTHER"},
                _answer(), _model_turn()],
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.seed_active_constraint)],
    )
    assert other_run.assertions["run_resumed_in_same_run"] is False
    assert driver.case_verdict(other_run).passed is False


def test_failure_when_no_real_model_turn_in_events(driver):
    """基线判据直接读事件流：没有 `model/completed` ⇒ 判负（调用方无从代答，P2-1）。"""
    obs = _obs(
        driver, "M1",
        events=[_tool_call("register_constraint")],  # 没有任何模型回合
        facts_after=[_fact("constraint", driver.NO_NEW_DEPENDENCY)],
        candidates=[{"value": driver.NO_NEW_DEPENDENCY}],
    )
    assert obs.assertions["real_model_turn_observed"] is False
    assert driver.case_verdict(obs).passed is False


def test_common_assertions_are_only_falsifiable_baselines(driver):
    """COMMON_ASSERTIONS 不含自证/恒真项（Round 4 审查 P2-1 / Standards P2-1）。"""
    assert driver.COMMON_ASSERTIONS == (
        "real_model_turn_observed", "no_memory_sidecar_events",
    )
    assert "thinking_disabled_and_temperature_zero_configured" not in driver.COMMON_ASSERTIONS
    assert "actual_primary_model_used" not in driver.COMMON_ASSERTIONS


def test_memory_sidecar_events_fail_the_baseline(driver):
    """`no_memory_sidecar_events` 不再硬编码 True：本 attempt 的 run 里出现 `memory/*` 判负。"""
    obs = _obs(
        driver, "M1",
        events=[_model_turn(), {"type": "memory/updated", "data": {}, "run_id": "run-1"}],
        facts_after=[_fact("constraint", driver.NO_NEW_DEPENDENCY)],
        candidates=[{"value": driver.NO_NEW_DEPENDENCY}],
    )
    assert obs.assertions["no_memory_sidecar_events"] is False
    assert driver.case_verdict(obs).passed is False
    # 别的 run 的 memory 事件不算在本次头上（按 run_id 作用域过滤）。
    other = _obs(
        driver, "M1",
        events=[_model_turn(), {"type": "memory/updated", "data": {}, "run_id": "OTHER"}],
        facts_after=[_fact("constraint", driver.NO_NEW_DEPENDENCY)],
        candidates=[{"value": driver.NO_NEW_DEPENDENCY}],
    )
    assert other.assertions["no_memory_sidecar_events"] is True


# ── 证据 JSON schema：字段齐全性校验 ─────────────────────────────────────────


def _minimal_evidence(driver) -> dict:
    return {
        "schema_version": driver.EVIDENCE_SCHEMA_VERSION,
        "ticket": 663,
        "ac": "AC16",
        "campaign_id": "AC16-driver-01",
        "status": "failed",
        "code_sha": "a" * 40,
        "git_tree": "b" * 40,
        "working_tree_fingerprint_sha256": "c" * 64,
        "started_at_utc": "2026-10-08T00:00:00+00:00",
        "completed_at_utc": "2026-10-08T00:00:01+00:00",
        "provider": {
            "session_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
            "memory_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
        },
        "runtime": {"api": "x", "production_tool_registry_and_executor": True},
        "expected_attempt_count": 18,
        "attempts": [
            {
                "case_id": "M1", "attempt": 1,
                "started_at_utc": "2026-10-08T00:00:00+00:00",
                "finished_at_utc": "2026-10-08T00:00:01+00:00",
                "input": "本次修改不能新增第三方依赖。",
                "code_sha": "a" * 40, "git_tree": "b" * 40,
                "session_id": "s1", "run_id": "r1",
                "assertions": {"real_model_turn_observed": True},
                "assertion_details": {"real_model_turn_observed": "ok"},
                "tool_calls": [], "tool_results": [], "final_reply": "",
                "extraction_outputs": [], "protected_facts_before": [],
                "extraction_job_id": "job-1", "extraction_state": "done",
                "extraction_candidates": [],
                "protected_facts_after": [],
                "model_provenance": {"session_primary_constructions": 1},
                "error": "",
                "verdict": "PASS",
            },
        ],
        "result": {
            "verdict": "failed", "attempts_recorded": 18,
            "expected_attempts": 18, "passed_attempts": 15,
            "failed_attempts": 3, "failed_case_attempts": ["M1-2"],
        },
        "rerun_policy": {"max_full_set_reruns": 2, "full_sets_completed": 1},
    }


def test_minimal_evidence_shape_validates(driver):
    driver.assert_evidence_shape(_minimal_evidence(driver))


@pytest.mark.parametrize(
    "drop", ["schema_version", "code_sha", "git_tree", "provider", "attempts", "result"],
)
def test_evidence_shape_rejects_missing_top_level_field(driver, drop):
    payload = _minimal_evidence(driver)
    payload.pop(drop)
    with pytest.raises(ValueError):
        driver.assert_evidence_shape(payload)


def test_evidence_shape_rejects_wrong_schema_version(driver):
    payload = _minimal_evidence(driver)
    payload["schema_version"] = 999
    with pytest.raises(ValueError):
        driver.assert_evidence_shape(payload)


def test_evidence_shape_rejects_attempt_missing_field(driver):
    payload = _minimal_evidence(driver)
    payload["attempts"][0].pop("verdict")
    with pytest.raises(ValueError):
        driver.assert_evidence_shape(payload)


def test_evidence_shape_rejects_truncated_sha(driver):
    payload = _minimal_evidence(driver)
    payload["code_sha"] = "abc"
    with pytest.raises(ValueError):
        driver.assert_evidence_shape(payload)


def test_evidence_shape_rejects_non_hex_sha(driver):
    payload = _minimal_evidence(driver)
    payload["git_tree"] = "z" * 40
    with pytest.raises(ValueError):
        driver.assert_evidence_shape(payload)


# ── §10.2：整套重跑 / 停止逻辑（可控的 flaky stub） ──────────────────────────


def _all_pass_verdicts(driver):
    """整套 18 个 verdict（9 案例 × 2），全过。"""
    return [
        driver.case_verdict(_passing_observation(driver, slot.case_id, slot.attempt))
        for slot in driver.build_plan()
    ]


def _fail_slot(driver, verdicts, *, case_id, attempt=1, assertion):
    for index, verdict in enumerate(verdicts):
        if verdict.case_id == case_id and verdict.attempt == attempt:
            verdicts[index] = driver.case_verdict(
                _passing_observation(driver, case_id, attempt).with_assertion(assertion, False),
            )
            return verdicts
    raise AssertionError(f"未找到槽位 {case_id}-{attempt}")


def test_campaign_passes_only_when_every_case_passes(driver):
    decision = driver.decide_campaign(_all_pass_verdicts(driver), full_sets_completed=1)
    assert decision.status == "pass"
    assert decision.rerun_count == 0


def test_campaign_requires_rerun_on_first_failing_full_set(driver):
    verdicts = _fail_slot(
        driver, _all_pass_verdicts(driver), case_id="M1",
        assertion="active_constraint_projection_contains_required_text",
    )
    decision = driver.decide_campaign(verdicts, full_sets_completed=1)
    assert decision.status == "rerun_required"
    assert decision.rerun_count == 1

def test_campaign_stops_and_reports_after_max_two_full_sets(driver):
    verdicts = _fail_slot(
        driver, _all_pass_verdicts(driver), case_id="M6",
        assertion="forbidden_text_not_active_constraint",
    )
    decision = driver.decide_campaign(verdicts, full_sets_completed=2)
    assert decision.status == "stop_report"
    assert "10.2" in decision.note or "§10.2" in decision.note


def test_campaign_rejects_incomplete_full_set(driver):
    """18 次全跑完才谈整套判定；缺槽位不得当成功。"""
    with pytest.raises(ValueError):
        driver.decide_campaign(_all_pass_verdicts(driver)[:5], full_sets_completed=1)


def test_campaign_never_drops_failing_cases_on_rerun(driver):
    """§10.2 / 票面：失败用例不可删掉或只挑绿样本；重跑永远是完整 18 次。"""
    verdicts = _fail_slot(
        driver, _all_pass_verdicts(driver), case_id="M4",
        assertion="forbidden_text_not_active_constraint",
    )
    decision = driver.decide_campaign(verdicts, full_sets_completed=1)
    assert decision.status == "rerun_required"
    assert decision.failing_cases == ("M4",)
    assert len(driver.build_plan()) == 18


# ── 入库证据装配：把 18 个 verdict + observation 折成可复核 JSON ───────────────


def _config(driver, tmp_path, *, direct_api_key=None, env_file=None):
    return driver.RunnerConfig(
        direct_api_key=direct_api_key,
        env_file=env_file,
        session_primary_provider="mimo", session_primary_model="mimo-v2.6-flash",
        memory_primary_provider="mimo", memory_primary_model="mimo-v2.6-flash",
        write=True, out_dir=tmp_path,
    )


def _git_facts():
    return {
        "code_sha": "d" * 40,
        "git_tree": "e" * 40,
        "working_tree_fingerprint_sha256": "f" * 64,
    }


def _build_evidence(driver, tmp_path, *, mutate=None):
    verdicts = _all_pass_verdicts(driver)
    if mutate is not None:
        verdicts = mutate(verdicts)
    observations = [
        _passing_observation(driver, verdict.case_id, verdict.attempt).with_assertion(
            "actual_primary_model_used", True,
        )
        for verdict in verdicts
    ]
    return driver.campaign_evidence(
        verdicts, observations, _config(driver, tmp_path),
        campaign_id="AC16-driver-01", full_sets_completed=1,
        git_facts=_git_facts(),
        provider={
            "session_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
            "memory_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
        },
        runtime={"api": "FastAPI TestClient", "scripted_or_fake_model": False},
        started_at_utc="2026-10-08T00:00:00+00:00",
        completed_at_utc="2026-10-08T00:01:00+00:00",
    )


def test_campaign_evidence_records_all_eighteen_attempts(driver, tmp_path):
    payload = _build_evidence(driver, tmp_path)
    assert len(payload["attempts"]) == 18
    assert [(a["case_id"], a["attempt"]) for a in payload["attempts"]] == [
        (slot.case_id, slot.attempt) for slot in driver.build_plan()
    ]
    driver.assert_evidence_shape(payload)


def test_campaign_evidence_passed_only_when_every_attempt_passes(driver, tmp_path):
    payload = _build_evidence(driver, tmp_path)
    assert payload["status"] == "passed"
    assert payload["result"]["verdict"] == "passed"
    assert payload["result"]["passed_attempts"] == 18
    assert payload["result"]["failed_case_attempts"] == []


def test_campaign_evidence_marks_failing_attempts_individually(driver, tmp_path):
    payload = _build_evidence(
        driver, tmp_path,
        mutate=lambda verdicts: _fail_slot(
            driver, verdicts, case_id="M7", attempt=2,
            assertion="candidate_not_registered",
        ),
    )
    assert payload["status"] == "failed"
    assert payload["result"]["failed_case_attempts"] == ["M7-2"]
    failing = [a for a in payload["attempts"] if a["verdict"] == "FAIL"]
    assert [a["case_id"] for a in failing] == ["M7"]
    assert payload["rerun_policy"]["decision"] == "rerun_required"
    assert payload["rerun_policy"]["full_sets_completed"] == 1


def test_campaign_evidence_carries_git_facts_and_inputs(driver, tmp_path):
    payload = _build_evidence(driver, tmp_path)
    assert payload["code_sha"] == "d" * 40
    assert payload["git_tree"] == "e" * 40
    assert payload["working_tree_fingerprint_sha256"] == "f" * 64
    assert all(a["code_sha"] == "d" * 40 for a in payload["attempts"])
    assert payload["expected_attempt_count"] == 18
    by_case = {a["case_id"]: a["input"] for a in payload["attempts"]}
    assert by_case["M2"] == "记住这条约束：本次修改必须兼容 Windows。"


def test_write_evidence_persists_scanned_payload(driver, tmp_path):
    payload = _build_evidence(driver, tmp_path)
    path = driver._write_evidence(payload, tmp_path / "evidence.json", values=())
    written = json.loads(path.read_text(encoding="utf-8"))
    driver.assert_evidence_shape(written)
    assert written["campaign_id"] == "AC16-driver-01"


def test_write_evidence_redacts_secret_hits_and_fails_the_run(driver, tmp_path):
    """凭证命中 = 判 fail 并且脱敏落盘，绝不静默产 PASS。"""
    payload = _build_evidence(driver, tmp_path)
    payload["attempts"][0]["session_id"] = "sk-leaked-secret-value"
    path = driver._write_evidence(payload, tmp_path / "evidence.json", values=("sk-leaked-secret-value",))
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["status"] == "failed"
    assert "sk-leaked-secret-value" not in path.read_text(encoding="utf-8")


# ── §9.1 原始产物：tool call/result、最终回复、抽取候选、断言 detail 必须落盘 ────


def test_attempt_carries_raw_tool_calls_results_reply_and_extraction(driver, tmp_path):
    """P2-2：§9.1 要求的原始产物（完整工具调用/结果、最终回复、抽取候选）进证据。"""
    observations = [
        _passing_observation(driver, v.case_id, v.attempt) for v in _all_pass_verdicts(driver)
    ]
    observations[0] = replace(
        observations[0],
        details={
            "tool_calls": [{"type": "tool/call", "data": {"tool_name": "register_constraint"}}],
            "tool_results": [{"type": "tool/result", "data": {"content": '{"ok":true}'}}],
            "final_reply": "已登记该约束。",
            "extraction_outputs": ['{"source":"u0","value":"本次修改不能新增第三方依赖。"}'],
            "protected_facts_before": [],
            "protected_facts_after": [{"type": "constraint", "value": "x", "status": "active"}],
            "assertion_details": {"actual_primary_model_used": "recorded"},
        },
    )
    payload = driver.campaign_evidence(
        _all_pass_verdicts(driver), observations, _config(driver, tmp_path),
        campaign_id="AC16-driver-01", full_sets_completed=1, git_facts=_git_facts(),
        provider={
            "session_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
            "memory_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
        },
        runtime={"api": "FastAPI TestClient", "scripted_or_fake_model": False},
        started_at_utc="2026-10-08T00:00:00+00:00",
        completed_at_utc="2026-10-08T00:01:00+00:00",
    )
    driver.assert_evidence_shape(payload)
    m1 = payload["attempts"][0]
    assert m1["tool_calls"][0]["data"]["tool_name"] == "register_constraint"
    assert m1["tool_results"][0]["data"]["content"] == '{"ok":true}'
    assert m1["final_reply"] == "已登记该约束。"
    assert m1["extraction_outputs"][0].startswith('{"source"')
    assert m1["protected_facts_after"][0]["value"] == "x"
    assert m1["assertion_details"]  # 断言 detail 不落空
    # 每个 attempt 都带齐这些字段（空列表是合法事实，缺字段是未采集）。
    for attempt in payload["attempts"]:
        for name in ("tool_calls", "tool_results", "extraction_outputs",
                     "protected_facts_before", "protected_facts_after"):
            assert isinstance(attempt[name], list)
        assert isinstance(attempt["final_reply"], str)
        assert isinstance(attempt["assertion_details"], dict)


def test_evidence_shape_rejects_attempt_missing_raw_artifact_fields(driver):
    """缺任一 §9.1 原始产物字段 ⇒ fail-closed（不静默把"未采集"当"无事实"）。"""
    for drop in ("tool_calls", "tool_results", "final_reply", "extraction_outputs",
                 "assertion_details", "protected_facts_before", "protected_facts_after",
                 "model_provenance", "error"):
        payload = _minimal_evidence(driver)
        payload["attempts"][0].pop(drop)
        with pytest.raises(ValueError):
            driver.assert_evidence_shape(payload)


def test_evidence_shape_rejects_wrong_raw_artifact_type(driver):
    """类型不对 = 未采集；`final_reply` 必须是串、其余必须是列表。"""
    payload = _minimal_evidence(driver)
    payload["attempts"][0]["tool_calls"] = {"not": "a list"}
    with pytest.raises(TypeError):
        driver.assert_evidence_shape(payload)
    payload = _minimal_evidence(driver)
    payload["attempts"][0]["final_reply"] = ["not", "a", "str"]
    with pytest.raises(TypeError):
        driver.assert_evidence_shape(payload)
    # 抽取三项：`None` 合法（= job 行缺席/阶段没跑到，是事实不是"未采集"），
    # 但类型错（如把状态写成 list）必须判红。
    payload = _minimal_evidence(driver)
    payload["attempts"][0]["extraction_candidates"] = None
    payload["attempts"][0]["extraction_job_id"] = None
    driver.assert_evidence_shape(payload)
    payload = _minimal_evidence(driver)
    payload["attempts"][0]["extraction_state"] = ["done"]
    with pytest.raises(TypeError):
        driver.assert_evidence_shape(payload)
    # P2-2：失败原因必须是串（空串 = 本 attempt 成功跑完；非串 = 未采集）。
    payload = _minimal_evidence(driver)
    payload["attempts"][0]["error"] = {"why": "boom"}
    with pytest.raises(TypeError):
        driver.assert_evidence_shape(payload)
    payload = _minimal_evidence(driver)
    payload["attempts"][0]["model_provenance"] = ["not", "an", "object"]
    with pytest.raises(TypeError):
        driver.assert_evidence_shape(payload)


# ── P3-5：attempts 增量落盘（崩溃不丢已跑轨迹） ─────────────────────────────


def test_attempts_ledger_appends_incrementally(driver, tmp_path):
    """每跑完一次就原子落一次；进程中途崩溃时盘上留下**已经跑过**的那些。"""
    verdicts = _all_pass_verdicts(driver)
    observations = [
        _passing_observation(driver, v.case_id, v.attempt) for v in verdicts
    ]
    path = tmp_path / "attempts.json"
    driver._write_attempts_ledger(
        path, [driver._attempt_record(observations[0], verdicts[0], git_facts=_git_facts())],
        sha="d" * 40, values=(),
    )
    first = json.loads(path.read_text(encoding="utf-8"))
    assert first["attempts_recorded"] == 1
    # 第二次追加（模拟"又跑完一槽"）——不覆盖成只留最后一条。
    driver._write_attempts_ledger(
        path,
        [driver._attempt_record(o, v, git_facts=_git_facts())
         for o, v in zip(observations[:2], verdicts[:2], strict=True)],
        sha="d" * 40, values=(),
    )
    second = json.loads(path.read_text(encoding="utf-8"))
    assert second["attempts_recorded"] == 2
    assert not path.with_suffix(path.suffix + ".tmp").exists()  # 原子替换，无残留 tmp


def test_attempts_ledger_is_credential_scanned_on_write(driver, tmp_path):
    """增量 attempts 落盘必须与最终 evidence 走**同一道**凭证扫描（任务书 §任务B 条 3）。

    这一路是主要的落盘形态（最多落 18 次），旧实现只 `json.dumps` 直接写、绕过扫描 ——
    "最终 evidence 扫了、增量没扫"是真旁路。命中即脱敏并把状态改成 failed。
    """
    record = {"case_id": "M1", "attempt": 1, "final_reply": "key 泄露 sk-live-secret-value 在回复里"}
    path = tmp_path / "attempts.json"
    driver._write_attempts_ledger(path, [record], sha="d" * 40, values=("sk-live-secret-value",))
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["status"] == "failed"
    assert "sk-live-secret-value" not in path.read_text(encoding="utf-8")


def test_read_job_row_is_read_only_and_per_run(driver, tmp_path):
    """`_read_job_row` 只读：**不存在的键**返回 None，不 INSERT 一行出来。

    这条钉的是判据的牙齿：若实现改成 `jobs.enqueue`（INSERT OR IGNORE），"本次 run 有 job
    行"就恒真，`extraction_job_completed` 永远为真——正是 P1-1 那类假绿。同时验证幂等键
    的 per-run 归属：另一条 run_id 的键读不到本 run 的行。
    """
    import sqlite3

    db = tmp_path / "memory-v2.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE memory_v2_jobs (idempotency_key TEXT PRIMARY KEY, job_id TEXT, "
        "stage TEXT, protected_fact_extraction_state TEXT, "
        "protected_fact_extraction_candidates TEXT)"
    )
    connection.execute(
        "INSERT INTO memory_v2_jobs VALUES ('memory-v2:run-a', 'j1', 'completed', 'done', ?)",
        (json.dumps([{"source": "u0", "value": "x"}]),),
    )
    connection.commit()
    connection.close()

    hit = driver._read_job_row(db, "memory-v2:run-a")
    assert hit == {"job": "j1", "extraction_state": "done"}
    assert driver._read_job_row(db, "memory-v2:run-b") is None
    # 只读：行数没变（run-b 那次没有把自己插进去）。
    connection = sqlite3.connect(db)
    assert connection.execute("SELECT COUNT(*) FROM memory_v2_jobs").fetchone()[0] == 1
    connection.close()


def test_extraction_candidates_parses_per_run_raw_output(driver):
    """候选从**本次 run** 的抽取原始输出里解析；没解析出 JSON ⇒ None（未采集，判负）。

    实测证据形态（M4/M6）：抽取器答 `{"candidates": []}` ⇒ 空列表（`done`+零候选，该过）；
    M1–M3 答 `{"candidates": [{"source": "u0", …}]}` ⇒ 非空。解析不出来（空/坏 JSON）不是
    "空候选"，是"没拿到" ⇒ None，`extraction_candidates_empty` 判负。
    """
    assert driver.extraction_candidates(['{"candidates": []}']) == []
    assert driver.extraction_candidates(
        ['{"candidates": [{"source": "u0", "value": "x"}]}']
    ) == [{"source": "u0", "value": "x"}]
    assert driver.extraction_candidates([]) is None
    assert driver.extraction_candidates(["not json"]) is None
    assert driver.extraction_candidates(['{"other": 1}']) is None
    # 后写的覆盖先写的（同一次 run 只应有一次抽取，取最后一条）。
    assert driver.extraction_candidates(
        ['{"candidates": [{"source": "u0", "value": "old"}]}', '{"candidates": []}']
    ) == []


def test_m4_shape_done_with_parsed_empty_candidates_passes(driver):
    """端到端形态：`done` + 解析出的空候选 ⇒ M4 过（旧实现读 NULL 列会把这条判死）。"""
    case = driver._CASES_BY_ID["M4"]
    obs = _obs(
        driver, "M4", events=[_model_turn()], facts_after=[],
        extraction=_extraction(candidates=[], state="done"),
    )
    assert _assertion(obs, "extractor_returned_no_candidates") is True
    assert obs.assertions["extraction_job_completed"] is True
    assert driver.case_verdict(obs).passed is True
    assert case.case_id == "M4"


def test_blocked_evidence_path_uses_the_campaign_stamp_verbatim(driver, tmp_path, monkeypatch):
    """P4-1 附带：blocked 证据目录名与 campaign stamp **逐字一致**（旧实现 `stamp[:15] + "Z"`
    是个 no-op——stamp 本身已以 `Z` 结尾，多出来的 `+ "Z"` 只是把同一个串再拼一遍）。"""
    monkeypatch.setenv(driver.DIRECT_KEY_ENV, "")
    monkeypatch.setattr(driver, "_blocked_evidence", lambda *a, **k: {"status": "blocked"})
    monkeypatch.setattr(driver, "_write_evidence", lambda payload, path, **k: path)
    monkeypatch.setattr(driver._RealRunner, "preflight", lambda self: ["无凭证"])
    asyncio = __import__("asyncio")
    stamp = "20261008T120000Z"
    frozen = driver.datetime

    class _FrozenDatetime(frozen):
        @classmethod
        def now(cls, tz=None):
            return frozen.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=tz)

    monkeypatch.setattr(driver, "datetime", _FrozenDatetime)
    result = asyncio.run(driver.run_campaign(driver.build_parser().parse_args(["--out-dir", str(tmp_path)])))
    name = result.evidence_path.parent.name
    assert name.startswith(f"{stamp}-") and "-issue-663-ac16-" in name
    assert "ZZ" not in name


def test_blocked_evidence_uses_single_timestamp(driver, tmp_path):
    """P4-1：blocked 证据的 started/completed 取自同一时刻（不再两次 now）。"""
    payload = driver._blocked_evidence(
        _config(driver, tmp_path), preconditions=["无凭证"], campaign_id="c1",
        now="2026-10-08T00:00:00.000+00:00",
    )
    assert payload["started_at_utc"] == payload["completed_at_utc"] == "2026-10-08T00:00:00.000+00:00"


def test_blocked_evidence_is_credential_scanned_on_write(driver, tmp_path):
    """P3-4：blocked 分支落盘同样过扫描——上游把 key 注进 preconditions 也抓得住。"""
    payload = _blocked_evidence_with_secret(driver, tmp_path)
    path = driver._write_evidence(
        payload, tmp_path / "blocked.json", values=("sk-blocked-secret",),
    )
    text = path.read_text(encoding="utf-8")
    assert "sk-blocked-secret" not in text
    assert json.loads(text)["status"] == "failed"


def _blocked_evidence_with_secret(driver, tmp_path):
    payload = driver._blocked_evidence(
        _config(driver, tmp_path, direct_api_key="sk-blocked-secret"),
        preconditions=["key=sk-blocked-secret"], campaign_id="c1",
        now="2026-10-08T00:00:00.000+00:00",
    )
    return payload


# ── 凭证来源：`MODEL_API_KEY` 直传优先于文件，且全程不落盘 ─────────────────────


#: 全程只用假值（票面：本轮无真实凭证、不许索取）。
_FAKE_ENV_KEY = "sk-test-dummy-000"
_FAKE_FILE_KEY = "sk-test-dummy-111"


def test_direct_env_key_beats_file_and_enters_settings_as_kwarg(driver, tmp_path, monkeypatch):
    """`MODEL_API_KEY` 非空 ⇒ 用它，文件只作其余配置的兜底（`_env_file`）。"""
    calls: list[tuple] = []
    stub_path = tmp_path / "fake.env"
    stub_path.write_text(f"MODEL_API_KEY={_FAKE_FILE_KEY}\n", encoding="utf-8")

    def _stub(*args, **kwargs):
        calls.append((args, kwargs))
        return object()

    import agent_harness.config as config_module
    monkeypatch.setattr(config_module, "Settings", _stub)
    monkeypatch.setenv(driver.DIRECT_KEY_ENV, _FAKE_ENV_KEY)

    config = driver.resolve_runner_config(driver.build_parser().parse_args(
        ["--env-file", str(stub_path)],
    ))
    assert config.direct_api_key == _FAKE_ENV_KEY
    runner = driver._RealRunner(config)
    assert runner.preflight() == []
    runner._settings()

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["model_api_key"] == _FAKE_ENV_KEY  # 直传值，不是文件里的
    assert kwargs["_env_file"] == str(stub_path)


def test_file_fallback_used_when_direct_env_key_absent(driver, tmp_path, monkeypatch):
    """无直传 ⇒ 退回文件：不传 `model_api_key` kwarg，`_env_file` 指向指定文件。"""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "agent_harness.config.Settings", lambda *a, **kw: calls.append((a, kw)) or object(),
    )
    monkeypatch.delenv(driver.DIRECT_KEY_ENV, raising=False)

    config = driver.resolve_runner_config(driver.build_parser().parse_args(
        ["--env-file", str(tmp_path / "x.env")],
    ))
    assert config.direct_api_key is None
    driver._RealRunner(config)._settings()

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert "model_api_key" not in kwargs
    assert kwargs["_env_file"] == str(tmp_path / "x.env")


def test_blank_direct_env_key_counts_as_absent(driver, monkeypatch):
    """空白直传 = 未直传（与 `.env` 侧"配了"的口径不同：空白 key 发出去只会换 401）。"""
    monkeypatch.setenv(driver.DIRECT_KEY_ENV, "   ")
    config = driver.resolve_runner_config(driver.build_parser().parse_args([]))
    assert config.direct_api_key is None


def test_env_file_var_beats_cli_arg(driver, monkeypatch):
    """票面 §任务第 2 条的次序：`AC16_ENV_FILE` > `--env-file`（只在直传缺席时生效）。"""
    monkeypatch.delenv(driver.DIRECT_KEY_ENV, raising=False)
    monkeypatch.setenv(driver.DEFAULT_ENV_FILE_ENV, "from-var.env")
    args = driver.build_parser().parse_args(["--env-file", "from-cli.env"])
    assert driver.resolve_runner_config(args).env_file == "from-var.env"
    # 环境变量缺席时才轮到 CLI 参数。
    monkeypatch.delenv(driver.DEFAULT_ENV_FILE_ENV)
    assert driver.resolve_runner_config(args).env_file == "from-cli.env"


def test_direct_key_never_writes_a_credential_file(driver, tmp_path, monkeypatch):
    """直传路径全程不落盘：唯一的文件是仓库里**已跟踪**的键名模板，且工作目录无新增文件。"""
    monkeypatch.setenv(driver.DIRECT_KEY_ENV, _FAKE_ENV_KEY)
    before = {p.resolve() for p in Path.cwd().iterdir()} | {p.resolve() for p in tmp_path.rglob("*")}

    calls: list[dict] = []
    monkeypatch.setattr(
        "agent_harness.config.Settings", lambda *a, **kw: calls.append(kw) or object(),
    )
    config = driver.resolve_runner_config(driver.build_parser().parse_args([]))
    assert config.direct_api_key == _FAKE_ENV_KEY
    assert config.env_file is None
    assert driver._RealRunner(config).preflight() == []
    driver._RealRunner(config)._settings()

    # 文件面只有一个：已跟踪的 `.env.example`（键名模板、被 git 跟踪、无真凭证）。
    assert [kw["_env_file"] for kw in calls] == [str(driver.EXAMPLE_ENV_FILE)]
    assert Path(driver.EXAMPLE_ENV_FILE).is_file()
    assert _FAKE_ENV_KEY not in Path(driver.EXAMPLE_ENV_FILE).read_text(encoding="utf-8")

    after = {p.resolve() for p in Path.cwd().iterdir()} | {p.resolve() for p in tmp_path.rglob("*")}
    assert after == before, "直传路径不得新建任何文件"


def test_direct_key_is_scanned_as_configured_credential(driver, tmp_path, monkeypatch):
    """回归：直传的 key 混进 evidence 字段 ⇒ 精确值扫描抓住、判 fail、脱敏落盘。"""
    monkeypatch.setenv(driver.DIRECT_KEY_ENV, _FAKE_ENV_KEY)
    config = driver.resolve_runner_config(driver.build_parser().parse_args([]))
    assert config.direct_api_key == _FAKE_ENV_KEY

    payload = _build_evidence(driver, tmp_path)
    payload["attempts"][0]["run_id"] = f"run-{config.direct_api_key}"  # 混入直传凭证
    path = driver._write_evidence(
        payload, tmp_path / "evidence.json",
        values=((config.direct_api_key,) if config.direct_api_key else ()),
    )

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["status"] == "failed"
    assert _FAKE_ENV_KEY not in path.read_text(encoding="utf-8")


def test_credential_scan_values_always_carry_the_direct_key(driver, tmp_path):
    """落盘前那一把精确值**恒含**直传 key —— 调用点真的把它传下去了，不是只在签名里。"""
    from agent_harness.config import Settings

    # 夹具已密封 Settings 相关环境变量 ⇒ 这份 Settings 里没有任何真凭证（值域只有直传那一个）。
    empty = Settings(_env_file=None)
    config = _config(driver, tmp_path, direct_api_key=_FAKE_ENV_KEY)
    assert driver.credential_scan_values(config, empty) == (_FAKE_ENV_KEY,)
    # 未直传时不凭空塞值（扫描层只认真配置过的值）。
    assert driver.credential_scan_values(_config(driver, tmp_path), empty) == ()


# ── P1-3：run 归属的竞态 + 终止态判据的 discriminator ─────────────────────────


def test_run_id_following_survives_missing_run_started(driver):
    """P1-3 红证：`run/started` 还没落盘时不得拿"最后一条带 run_id 的事件"冒充本次 run。

    旧实现 `_latest_run_id` 在 `run/started` 之前读，拿到的是**上一轮**的 run（M8/M9 同
    session 第二轮）或 None（M5-2 的死因，sub-ms 的 start/finish）。新口径按
    "发消息前快照的 run_id 集合"取**新出现**的 run/started。
    """
    old_run_only = [
        {"type": "run/started", "run_id": "run-old"},
        {"type": "tool/call", "run_id": "run-old", "data": {"tool_name": "git_status"}},
        {"type": "run/completed", "run_id": "run-old"},
    ]
    # 旧口径会把上一轮的 run 认成本轮（这就是 bug）。
    assert driver._latest_run_id(old_run_only) == "run-old"
    # 新口径：没有**新出现**的 run/started ⇒ 返回 None（调用方据此继续等，不猜）。
    assert driver.follow_latest_run_id(old_run_only, seen_run_ids={"run-old"}) is None
    appeared = [*old_run_only, {"type": "run/started", "run_id": "run-new"}]
    assert driver.follow_latest_run_id(appeared, seen_run_ids={"run-old"}) == "run-new"


def test_await_new_run_id_polls_until_run_started_is_durable(driver):
    """P1-3 的正控：轮询到 run/started 落盘为止（有界），超时抛竞态错（不静默返回 None）。"""
    import asyncio

    frames: list[list[dict]] = [
        [{"type": "run/started", "run_id": "run-old"}],
        [{"type": "run/started", "run_id": "run-old"}],
        [{"type": "run/started", "run_id": "run-old"},
         {"type": "run/started", "run_id": "run-new"}],
    ]
    calls = {"n": 0}

    async def fetch():
        index = min(calls["n"], len(frames) - 1)
        calls["n"] += 1
        return frames[index]

    run_id, events = asyncio.run(
        driver._await_new_run_id(fetch, seen_run_ids={"run-old"}, timeout_seconds=5.0),
    )
    assert run_id == "run-new"
    assert calls["n"] == 3
    assert {"type": "run/started", "run_id": "run-new"} in events


def test_await_new_run_id_times_out_loudly(driver):
    """超时是**竞态失败**（响亮抛错），不是"没读到就算过"。"""
    import asyncio

    async def fetch():
        return [{"type": "run/started", "run_id": "run-old"}]

    with pytest.raises(driver._EventStreamRaceError):
        asyncio.run(
            driver._await_new_run_id(fetch, seen_run_ids={"run-old"}, timeout_seconds=0.0),
        )


def test_terminal_status_distinguishes_paused_resumed_failed_and_completed(driver):
    """终止态 discriminator（Round 4 审查缺口）：四种终态互不混淆，且按 run_id 隔离。"""
    assert driver._terminal_status_for_run([], "run-1") == ("running", None)
    assert driver._terminal_status_for_run(
        [{"type": "run/started", "run_id": "run-1"}], "run-1",
    ) == ("running", None)
    assert driver._terminal_status_for_run(
        [{"type": "run/started", "run_id": "run-1"},
         {"type": "run/paused", "run_id": "run-1", "data": {"reason": "user_input"}}], "run-1",
    ) == ("paused", "user_input")
    # 暂停后 resumed ⇒ 仍视作 running（不是"还在暂停"）。
    assert driver._terminal_status_for_run(
        [{"type": "run/paused", "run_id": "run-1", "data": {"reason": "user_input"}},
         {"type": "run/resumed", "run_id": "run-1"}], "run-1",
    ) == ("running", None)
    assert driver._terminal_status_for_run(
        [{"type": "run/failed", "run_id": "run-1"}], "run-1",
    ) == ("failed", None)
    assert driver._terminal_status_for_run(
        [{"type": "run/interrupted", "run_id": "run-1"}], "run-1",
    ) == ("failed", None)
    assert driver._terminal_status_for_run(
        [{"type": "run/completed", "run_id": "run-1"}], "run-1",
    ) == ("completed", None)
    # run_id 隔离：别的 run 的终态不算本 run 的。
    assert driver._terminal_status_for_run(
        [{"type": "run/completed", "run_id": "OTHER"}], "run-1",
    ) == ("running", None)


def test_new_active_constraints_uses_identity_not_similarity(driver):
    """P1-2 的核心判别器：按 **fact_id / (type,value)** 判定，不做文本相似度。"""
    before = [_fact("constraint", "本次修改不能新增第三方依赖。", fact_id="pf-a")]
    same_id = [_fact("constraint", "本次修改不能新增第三方依赖。", fact_id="pf-a")]
    assert driver.new_active_constraints(before, same_id) == []
    # 被重建（fact_id 丢了）但 (type,value) 相同 ⇒ 仍是旧。
    reconstructed = [_fact("constraint", "本次修改不能新增第三方依赖。")]
    assert driver.new_active_constraints(before, reconstructed) == []
    # 模型把候选改写/截断后登记 ⇒ **文本高重叠也算新增**（新 fact_id ⇒ 确实是新登记）。
    # 这正是 P1-2 要防的假 PASS：按相似度去重会把它放过。
    rewritten = [_fact("constraint", "不能新增第三方依赖", fact_id="pf-b")]
    assert driver.new_active_constraints(before, rewritten) == ["不能新增第三方依赖"]
    # 完全无关的新约束 ⇒ 新增。
    unrelated = [_fact("constraint", "必须使用 PostgreSQL", fact_id="pf-c")]
    assert driver.new_active_constraints(before, unrelated) == ["必须使用 PostgreSQL"]
    # superseded constraint 与别的 fact 类型都不算"新增 active constraint"。
    assert driver.new_active_constraints(
        before, [_fact("constraint", "新的", "superseded", "pf-d"),
                 _fact("goal", "x", "active", "pf-e")],
    ) == []


def test_extraction_candidates_prefers_adopted_over_model_raw(driver, tmp_path):
    """P3-1：判据优先"executor 采纳的候选"（`ready` 窗口），取不到才退回模型原始输出。"""
    import sqlite3

    db = tmp_path / "memory-v2.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE memory_v2_jobs (idempotency_key TEXT PRIMARY KEY, job_id TEXT, "
        "stage TEXT, protected_fact_extraction_state TEXT, "
        "protected_fact_extraction_candidates TEXT)"
    )
    connection.execute(
        "INSERT INTO memory_v2_jobs VALUES ('memory-v2:run-a', 'j1', 'completed', 'ready', ?)",
        (json.dumps([{"source": "u0", "value": "adopted"}]),),
    )
    connection.commit()
    connection.close()
    assert driver._read_extraction_candidates(db, "memory-v2:run-a") == [
        {"source": "u0", "value": "adopted"},
    ]
    # 置 NULL 之后（`done`）取不到 ⇒ None（调用方退回模型原始输出，而不是判成"零候选"）。
    connection = sqlite3.connect(db)
    connection.execute(
        "UPDATE memory_v2_jobs SET protected_fact_extraction_candidates=NULL, "
        "protected_fact_extraction_state='done' WHERE idempotency_key='memory-v2:run-a'",
    )
    connection.commit()
    connection.close()
    assert driver._read_extraction_candidates(db, "memory-v2:run-a") is None


# ── P2-2：失败 attempt 的失败原因必须进证据 ─────────────────────────────────


def test_failed_attempt_serializes_its_error(driver, tmp_path):
    """P2-2 红证：失败 attempt 的 `error` 必须落盘（M5-2 的死因当时不可恢复）。"""
    verdicts = _all_pass_verdicts(driver)
    observations = [
        _passing_observation(driver, v.case_id, v.attempt) for v in verdicts
    ]
    observations[0] = replace(
        observations[0],
        error="_EventStreamRaceError: 等待 300.0s 仍未读到 run/started",
        details={"session_id": "s1", "run_id": ""},
    )
    payload = driver.campaign_evidence(
        verdicts, observations, _config(driver, tmp_path),
        campaign_id="AC16-driver-01", full_sets_completed=1, git_facts=_git_facts(),
        provider={
            "session_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
            "memory_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
        },
        runtime={"api": "x"}, started_at_utc="t0", completed_at_utc="t1",
    )
    driver.assert_evidence_shape(payload)
    assert payload["attempts"][0]["error"].startswith("_EventStreamRaceError")
    # 成功的 attempt 落空串（合法事实），不是缺字段。
    assert payload["attempts"][1]["error"] == ""


def test_model_provenance_is_serialized_per_attempt(driver, tmp_path):
    """P2-1：温度/thinking 的事实按 attempt 落盘（不再冒充成一条必需判据）。"""
    verdicts = _all_pass_verdicts(driver)
    observations = [
        _passing_observation(driver, v.case_id, v.attempt) for v in verdicts
    ]
    observations[0] = replace(observations[0], details={
        "model_provenance": {
            "session_primary_constructions": 2,
            "temperature_configured": [0.0],
            "thinking_disabled_configured": False,
        },
    })
    payload = driver.campaign_evidence(
        verdicts, observations, _config(driver, tmp_path),
        campaign_id="AC16-driver-01", full_sets_completed=1, git_facts=_git_facts(),
        provider={
            "session_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
            "memory_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
        },
        runtime={"api": "x"}, started_at_utc="t0", completed_at_utc="t1",
    )
    driver.assert_evidence_shape(payload)
    provenance = payload["attempts"][0]["model_provenance"]
    assert provenance["temperature_configured"] == [0.0]
    assert provenance["thinking_disabled_configured"] is False


# ── 装饰性判据的不变量（Round 4 审查的教训固化成机制）────────────────────────


def test_decoration_invariant_fails_loudly(driver):
    """算了却不判的判据 = 装饰 ⇒ 整条判负（不静默失效）。"""
    obs = _passing_observation(driver, "M1")
    obs = obs.with_assertion("some_assertion_nobody_judges", True)
    verdict = driver.case_verdict(obs)
    assert verdict.passed is False
    assert any(name.startswith("undecorated_assertions:") for name in verdict.failed_assertions)


def test_every_computed_assertion_is_required_or_forensic(driver):
    """9 个案例：`build_observation` 算出来的判定必须都在 `required_assertions` 里。"""
    samples = {
        "M1": {"events": [_model_turn()], "facts_after": [_fact("constraint", driver.NO_NEW_DEPENDENCY)], "candidates": [{"value": driver.NO_NEW_DEPENDENCY}]},
        "M2": {"events": [_model_turn()], "facts_after": [_fact("constraint", "本次修改必须兼容 Windows。")], "candidates": []},
        "M3": {"events": [_model_turn()], "facts_after": [_fact("constraint", "只有通过全量测试后才能合并，未通过时不能合并。")], "candidates": []},
        "M4": {"events": [_model_turn()], "facts_after": [], "candidates": []},
        "M5": {"events": [_model_turn()], "facts_after": [], "candidates": []},
        "M6": {"events": [_model_turn()], "facts_after": [], "candidates": []},
        "M7": {
            "events": [_register_call(driver.NO_NEW_DEPENDENCY), _model_turn()],
            "facts_before": [_fact("constraint", "保留所有现有行为", fact_id="pf")],
            "facts_after": [_fact("constraint", "保留所有现有行为", fact_id="pf")],
            "candidates": [],
            "tool_results": [_tool_result(_m7_rejected_result())],
            "final_reply": "未能保存。",
        },        "M8": {"events": [_card(driver.NO_NEW_DEPENDENCY, driver._CASES_BY_ID["M8"].input_text),
                           _paused(), _resumed(), _answer(), _model_turn()], "facts_before": [_fact("constraint", driver.NO_NEW_DEPENDENCY)], "facts_pre_answer": [_fact("constraint", driver.NO_NEW_DEPENDENCY)], "facts_after": [_fact("constraint", driver.NO_NEW_DEPENDENCY)]},
        "M9": {"events": [_card(driver.NO_NEW_DEPENDENCY, driver._CASES_BY_ID["M9"].input_text),
                           _paused(), _resumed(), _answer(), _model_turn()], "facts_before": [_fact("constraint", driver.NO_NEW_DEPENDENCY)], "facts_pre_answer": [_fact("constraint", driver.NO_NEW_DEPENDENCY)], "facts_after": [_fact("constraint", driver._CASES_BY_ID["M9"].input_text)]},
    }
    for case_id, kwargs in samples.items():
        obs = _obs(driver, case_id, **kwargs)
        required = set(driver.required_assertions(case_id))
        assert set(obs.assertions) <= required, case_id
        assert required <= set(obs.assertions), case_id


# ── Standards P3-2：工作树指纹复用 Live Gate 的唯一实现 ─────────────────────


def test_working_tree_fingerprint_reuses_live_gate_proof(driver, monkeypatch):
    """指纹取 `evaluation.live_gate.repo.worktree_proof`（gate0 的唯一实现），不另写一份。"""
    calls: list[str] = []
    import evaluation.live_gate.repo as repo_module

    def fake_proof():
        calls.append("worktree_proof")
        return {
            "head_sha": "a" * 40, "tree": "b" * 40, "tracked_matches_head": True,
            "untracked": [], "hidden": [], "risky": [],
            "_counts": {"tracked": 0, "hidden": 0, "risky": 0, "untracked": 2},
        }

    monkeypatch.setattr(repo_module, "worktree_proof", fake_proof)
    fingerprint = driver.working_tree_fingerprint()
    assert calls == ["worktree_proof"]
    assert len(fingerprint) == 64
    # 同一棵树 ⇒ 同一指纹；未跟踪文件数变化 ⇒ 指纹变（那是这条判据的牙齿）。
    assert driver.working_tree_fingerprint() == fingerprint
    monkeypatch.setattr(repo_module, "worktree_proof", lambda: {**fake_proof(),
                        "_counts": {"tracked": 0, "hidden": 0, "risky": 0, "untracked": 3}})
    assert driver.working_tree_fingerprint() != fingerprint


# ── Round 6：M9 判据对齐票面原文（更正前缀 / 答题作用域）─────────────────────────


def test_m9_card_candidate_is_the_substantive_new_text_not_the_whole_message(driver):
    """票面 M9 要「完整展示旧/新原文」——**新约束原文**，不是整句用户消息。

    工具自己的指导（`request_constraint_resolution.py` 的 prompt_guidance）逐字要求
    "Omit correction framing such as 'I correct this rule:'"，所以 M9 输入
    "我更正这条长期约束：今后的任务可以按需要新增第三方依赖。" 的合法 candidate
    是去掉更正前缀后的实质约束。Round 5 拿整句输入当 expected_candidate，把这条
    **被指导要求**的行为判成假负（实测 M9 两次都因此 FAIL，而同一文本在旧驱动下 PASS）。
    """
    case = driver._CASES_BY_ID["M9"]
    substantive = case.active_text
    assert substantive != case.input_text, "M9 的实质约束文本必须与整句输入不同（前缀被剥掉）"
    assert substantive in case.input_text, "实质约束必须是用户原文的逐字连续子串"
    assert case.input_text.endswith(substantive), "更正前缀只能从**开头**剥掉（candidate 是原文后缀）"
    obs = _obs(
        driver, "M9",
        events=[_card(case.seed_active_constraint, substantive), _paused(),
                _resumed(), _answer(), _model_turn()],
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", substantive)],
    )
    assert driver.case_verdict(obs).passed is True
    assert obs.assertions["card_has_exact_old_and_new"] is True
    assert obs.assertions["persistent_choice_superseded_old"] is True


def test_m9_card_still_rejects_a_paraphrased_or_truncated_candidate(driver):
    """对齐票面不等于放宽：拿别的话 / 残句当 candidate 仍须判负（判据保留牙齿）。"""
    case = driver._CASES_BY_ID["M9"]
    for bogus in ("今后可以加依赖", "本次修改不能新增第三方依赖。", ""):
        obs = _obs(
            driver, "M9",
            events=[_card(case.seed_active_constraint, bogus), _paused(),
                    _resumed(), _answer(), _model_turn()],
            facts_before=[_fact("constraint", case.seed_active_constraint)],
            facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
            facts_after=[_fact("constraint", case.active_text)],
        )
        assert obs.assertions["card_has_exact_old_and_new"] is False, bogus
        assert driver.case_verdict(obs).passed is False, bogus


def _register_call_after_answer(value: str, request_id: str = "r1") -> list[dict]:
    """答题后的 register_constraint 调用（`_answer` 之后）。"""
    return [{"type": "tool/call", "data": {"tool_name": "register_constraint",
                                           "args": {"value": value}}}]


def test_m8_m9_no_register_constraint_call_is_scoped_to_before_the_answer(driver):
    """票面 M9 逐字是「**回答前** active 集合不变且不继续受影响工作」。

    驱动旧口径把 `register_constraint` 的全事件流扫描当判据（判据名也从旧驱动的
    `no_register_constraint_before_answer` 改名时丢了作用域），比票面严。实测 M9-1
    的该调用发生在 tool/result(seq 41, requested) → 卡片 → run/paused(seq 42) →
    resume 之后（tool/call seq 55，结果 rejected，无副作用），被误判。
    """
    case = driver._CASES_BY_ID["M9"]
    head = [_card(case.seed_active_constraint, case.active_text), _paused()]
    tail = [_resumed(), _answer(), _model_turn()]
    facts = {
        "facts_before": [_fact("constraint", case.seed_active_constraint)],
        "facts_pre_answer": [_fact("constraint", case.seed_active_constraint)],
        "facts_after": [_fact("constraint", case.active_text)],
    }
    after = _obs(
        driver, "M9",
        events=[*head, *tail[:2], *_register_call_after_answer("x"), *tail[2:]],
        **facts,
    )
    assert after.assertions["no_register_constraint_before_answer"] is True
    assert driver.case_verdict(after).passed is True
    # 答题**前**调 register_constraint ⇒ 仍须判负（这正是票面要拦的行为）。
    before = _obs(
        driver, "M9", events=[head[0], *_register_call_after_answer("x"), head[1], *tail],
        **facts,
    )
    assert before.assertions["no_register_constraint_before_answer"] is False
    assert driver.case_verdict(before).passed is False


def test_run_failure_reason_is_read_from_the_run_failed_event(driver):
    """Run 以 `run/failed` 收口时，驱动必须落下真实失败原因。

    Round 5 实测 M1-2：run 在**首个模型回合之前**被 provider RateLimitError 终止，
    事件流零 tool_call / 零 model/completed、`error` 空串（与驱动自己那句
    "空串 = 本次成功跑完"直接矛盾，Round 4 P2-2 修失败原因序列化时漏了这条路径）。
    """
    events = [
        {"type": "run/started", "data": {}, "run_id": "run-1"},
        {"type": "run/failed", "data": {"reason": "RateLimitError"}, "run_id": "run-1"},
    ]
    assert driver.run_failure_reason(events, "run-1") == "run/failed(reason=RateLimitError)"
    # 别的 run 的失败不能算到本次头上。
    assert driver.run_failure_reason(events, "run-2") == ""
    # 正常收口 = 空串（合法事实，不是未采集）。
    assert driver.run_failure_reason(
        [{"type": "run/completed", "data": {}, "run_id": "run-1"}], "run-1",
    ) == ""
    # `run/interrupted` 也是失败态（`_terminal_status_for_run` 就这么判）——
    # Standards 轴实测：原先只扫 `run/failed`，被中断的 run 会漏成空串。
    assert driver.run_failure_reason(
        [{"type": "run/interrupted", "data": {"reason": "StallWatchdog"}, "run_id": "run-1"}],
        "run-1",
    ) == "run/interrupted(reason=StallWatchdog)"
    # 两处对"什么算失败"必须同源。
    assert set(driver._RUN_FAILURE_TYPES) <= set(driver._RUN_LIFECYCLE_TYPES)


# ── Round 6：slot 级重跑（判据/观测修好后只重跑受影响的槽位）──────────────────────


def test_parse_slots_selects_exactly_the_named_slots(driver):
    """`M9-1,M1-2` ⇒ 恰好这两个槽位（票面 §10.2 的整套重跑之外，本轮按任务书做定点重跑）。"""
    slots = driver.parse_slots("M9-1,M1-2")
    assert [s.slot_id for s in slots] == ["M9-1", "M1-2"]
    assert slots[0] == driver.PlanSlot(case_id="M9", attempt=1)


def test_parse_slots_rejects_unknown_or_malformed_specs(driver):
    """fail-closed：不认识的槽位/坏格式一律报错，不静默少跑（少跑的槽位会被当成没过）。"""
    for spec in ("M99-1", "M1-3", "M1-0", "M1", "M1-1,M1-1", "", "1-1"):
        with pytest.raises(ValueError):
            driver.parse_slots(spec)


def test_partial_campaign_evidence_is_labelled_partial(driver, tmp_path):
    """定点重跑落盘的证据必须**自报**是部分集合，不能长得像整套 18 次。"""
    slots = driver.parse_slots("M9-1,M1-2")
    verdicts = [
        driver.case_verdict(_passing_observation(driver, s.case_id, s.attempt))
        for s in slots
    ]
    observations = [
        _passing_observation(driver, s.case_id, s.attempt) for s in slots
    ]
    payload = driver.campaign_evidence(
        verdicts, observations, _config(driver, tmp_path),
        campaign_id="AC16-round6-partial", full_sets_completed=1,
        git_facts=_git_facts(),
        provider={
            "session_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
            "memory_primary": {"provider": "mimo", "model_id": "mimo-v2.6-flash"},
        },
        runtime={"api": "production create_app ASGI", "scripted_or_fake_model": False},
        started_at_utc="2026-10-08T00:00:00+00:00",
        completed_at_utc="2026-10-08T00:01:00+00:00",
        partial_slots=tuple(s.slot_id for s in slots),
    )
    driver.assert_evidence_shape(payload)
    assert payload["status"] == "partial"
    assert payload["result"]["verdict"] == "partial"
    assert payload["result"]["attempts_recorded"] == 2
    assert payload["partial_slots"] == ["M9-1", "M1-2"]
    # 部分集合**不能**自称跑完了一整套（那会让 §10.2 的整套判据凭空满足）。
    assert payload["rerun_policy"]["decision"] == "partial_slots"


# ── Round 6：M7/M8/M9 不该等一个永不会出现的抽取 job ────────────────────────────


def test_only_m8_m9_skip_the_extraction_job_wait(driver):
    """**只有 M8/M9** 不产生 B-lite 抽取 job；M7 有。

    实测（Round 5 完整 campaign 的证据，`20261008T054419Z-6869dd6a`，每个 slot 都等过 job）：
    **M7 两次都有 job 行且 `state=done`**，M8/M9 四次全无。所以判据不能按
    `_PRIMARY_CASES`（M7/M8/M9 一起）取反——那会把 M7 也算成"永不出 job"。

    M7 被算错的代价有两条，都不是"只是慢一点"：
    ① `_extraction_evidence` 立刻返回缺席形状 ⇒ M7 证据里的 `extraction_job_id` /
       `extraction_state` / `extraction_candidates` 全变 `None`/空，**已采到的实测证据被抹掉**；
    ② 不再等 job `done` 再读 `facts_after` ⇒ M7 的 `candidate_not_registered` 可能在抽取
       落库**之前**取投影，判据时序与 Round 5 不同 ⇒ 等于换了一套判据。
    """
    assert driver.case_expects_extraction_job("M7") is True
    for case_id in ("M8", "M9"):
        assert driver.case_expects_extraction_job(case_id) is False, case_id
    for case_id in ("M1", "M2", "M3", "M4", "M5", "M6"):
        assert driver.case_expects_extraction_job(case_id) is True, case_id


def test_absent_extraction_job_returns_immediately_for_primary_cases(driver):
    """`_extraction_evidence(expect_job=False)` **立刻**返回缺席形状，不查库、不进等待循环。"""
    import asyncio

    runner = object.__new__(driver._RealRunner)  # 只借方法，不跑真实装配

    out = asyncio.run(runner._extraction_evidence("run-x", expect_job=False))
    assert out == {"job": None, "extraction_state": "", "candidates": None}
