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


def test_m7_requires_budget_rejection_and_no_false_saved_claim(driver):
    obs = _passing_observation(driver, "M7").with_assertion("real_budget_rejection", False)
    assert driver.case_verdict(obs).passed is False
    obs2 = _passing_observation(driver, "M7").with_assertion("reply_does_not_claim_saved", False)
    assert driver.case_verdict(obs2).passed is False


def test_m8_m9_require_full_old_and_new_display(driver):
    for case_id in ("M8", "M9"):
        obs = _passing_observation(driver, case_id).with_assertion(
            "card_has_exact_old_and_new", False,
        )
        assert driver.case_verdict(obs).passed is False, case_id


# ── observation 抽取：喂伪造事件/事实，断言判对判错 ──────────────────────────


def _tool_call(name: str) -> dict:
    return {"type": "tool/call", "data": {"tool_name": name}}


def _fact(fact_type: str, value: str, status: str = "active") -> dict:
    return {"type": fact_type, "value": value, "status": status}


def _paused(reason: str = "user_input") -> dict:
    return {"type": "run/paused", "data": {"reason": reason}}


def _card(old: str, candidate: str) -> dict:
    return {"type": "user/input-requested", "data": {"old_value": old, "candidate": candidate}}


def _obs(driver, case_id, *, events, facts_before=(), facts_after=(), candidates=None,
         job_completed=True, final_reply="", tool_results=(), model_ok=True,
         facts_pre_answer=None):
    return driver.build_observation(
        case_id, 1, events=events, facts_before=facts_before, facts_after=facts_after,
        extraction_candidates=candidates, job_completed=job_completed,
        final_reply=final_reply, tool_results=tool_results, model_ok=model_ok,
        facts_pre_answer=facts_pre_answer,
    )


def test_m1_registers_and_projects_required_text(driver):
    obs = _obs(
        driver, "M1",
        events=[_tool_call("register_constraint")],
        facts_after=[_fact("constraint", driver.NO_NEW_DEPENDENCY)],
        candidates=[{"source": "u0", "value": driver.NO_NEW_DEPENDENCY}],
    )
    assert driver.case_verdict(obs).passed is True


def test_m1_fails_when_text_missing_from_projection(driver):
    obs = _obs(
        driver, "M1",
        events=[_tool_call("register_constraint")],
        facts_after=[_fact("constraint", "只有通过全量测试后才能合并")],
        candidates=[{"value": "x"}],
    )
    assert driver.case_verdict(obs).passed is False


def test_m2_expects_target_constraint_not_whole_reply(driver):
    """M2 的 value 可去掉"记住这条约束："前缀——判据取目标约束而非整句。"""
    obs = _obs(
        driver, "M2",
        events=[_tool_call("register_constraint")],
        facts_after=[_fact("constraint", "本次修改必须兼容 Windows。")],
        candidates=[{"value": "本次修改必须兼容 Windows。"}],
    )
    assert driver.case_verdict(obs).passed is True


def test_m4_fails_when_quoted_reference_is_registered(driver):
    quoted = "本次修改必须增加 Redis。"
    obs = _obs(
        driver, "M4",
        events=[],
        facts_after=[_fact("constraint", quoted)],
        candidates=[{"value": quoted}],
    )
    assert driver.case_verdict(obs).passed is False


def test_m5_fails_when_authorization_registered(driver):
    obs = _obs(
        driver, "M5",
        events=[_tool_call("register_constraint")],
        facts_after=[_fact("constraint", "我批准你推送这个分支。")],
        candidates=[],
    )
    assert driver.case_verdict(obs).passed is False


def test_m6_fails_when_speculation_registered(driver):
    obs = _obs(
        driver, "M6",
        events=[],
        facts_after=[_fact("constraint", "也许以后会用 PostgreSQL，目前还没有决定。")],
        candidates=[{"value": "也许以后会用 PostgreSQL，目前还没有决定。"}],
    )
    assert driver.case_verdict(obs).passed is False


def test_m6_passes_when_speculation_absent_and_extractor_empty(driver):
    obs = _obs(driver, "M6", events=[], facts_after=[], candidates=[])
    assert driver.case_verdict(obs).passed is True


def test_m7_budget_rejection_with_honest_reply_passes(driver):
    obs = _obs(
        driver, "M7",
        events=[_tool_call("register_constraint")],
        facts_after=[],
        candidates=None,
        tool_results=[{"data": {"content": '{"status":"rejected","reason_code":"BUDGET_EXCEEDED"}'}}],
        final_reply="这条约束未能保存：登记被拒绝（BUDGET_EXCEEDED）。",
    )
    assert driver.case_verdict(obs).passed is True


def test_m7_fails_when_reply_claims_saved_after_rejection(driver):
    obs = _obs(
        driver, "M7",
        events=[_tool_call("register_constraint")],
        facts_after=[],
        tool_results=[{"data": {"content": "BUDGET_EXCEEDED"}}],
        final_reply="已登记该约束。",
    )
    assert driver.case_verdict(obs).passed is False


def test_m8_card_requires_exact_old_and_new(driver):
    case = driver._CASES_BY_ID["M8"]
    good = _obs(
        driver, "M8",
        events=[_card(case.seed_active_constraint, case.input_text), _paused(),
                {"type": "run/resumed"},
                {"type": "user/message", "data": {"input_request_id": "r1"}}],
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.seed_active_constraint)],
    )
    assert driver.case_verdict(good).passed is True
    wrong_card = _obs(
        driver, "M8",
        events=[_card("别的旧约束", case.input_text), _paused(),
                {"type": "run/resumed"},
                {"type": "user/message", "data": {"input_request_id": "r1"}}],
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
                {"type": "run/resumed"},
                {"type": "user/message", "data": {"input_request_id": "r1"}}],
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint),
                          _fact("constraint", case.input_text)],
        facts_after=[_fact("constraint", case.seed_active_constraint)],
    )
    assert driver.case_verdict(obs).passed is False


def test_m9_requires_supersede_after_persistent_choice(driver):
    case = driver._CASES_BY_ID["M9"]
    obs = _obs(
        driver, "M9",
        events=[_card(case.seed_active_constraint, case.input_text), _paused(),
                {"type": "run/resumed"},
                {"type": "user/message", "data": {"input_request_id": "r1"}}],
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[_fact("constraint", case.input_text)],
    )
    assert driver.case_verdict(obs).passed is True
    no_supersede = _obs(
        driver, "M9",
        events=[_card(case.seed_active_constraint, case.input_text), _paused(),
                {"type": "run/resumed"},
                {"type": "user/message", "data": {"input_request_id": "r1"}}],
        facts_before=[_fact("constraint", case.seed_active_constraint)],
        facts_pre_answer=[_fact("constraint", case.seed_active_constraint)],
        facts_after=[],
    )
    assert driver.case_verdict(no_supersede).passed is False


def test_failure_when_wrong_model_used(driver):
    obs = _obs(
        driver, "M1",
        events=[_tool_call("register_constraint")],
        facts_after=[_fact("constraint", driver.NO_NEW_DEPENDENCY)],
        candidates=[{"value": driver.NO_NEW_DEPENDENCY}],
        model_ok=False,
    )
    assert driver.case_verdict(obs).passed is False


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
                "assertions": {"actual_primary_model_used": True},
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
            assertion="real_budget_rejection",
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
