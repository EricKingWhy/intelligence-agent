#!/usr/bin/env python
"""AC16 真模型 Gate 驱动（`#663`）。

## 它回答的问题

票面 AC16 / §9.1 要求：M1–M9 九个固定案例，每个在**独立会话**跑两次 = 18 次，逐案例
用**机械判据**判定，失败不覆盖、不挑绿样本。此前 18 次是人工装配跑的，仓库里没有可复跑
的驱动（4220 commits 全扫确认，只剩证据产物）。本脚本把那次运行**反推**成一个可重复的
驱动：案例定义、执行编排、判定、证据格式、§10.2 重跑策略。

## 选型：为什么不复用 `run_memory_v2_real_gold_gate.py`

`run_memory_v2_real_gold_gate.py` 跑的是**合成 Memory V2 gold 集**（`GoldCase` 语料 +
Milvus/embedding 预检 + SQLite 每例一库），判据全是 Memory V2 的写入/召回；它不经
`create_app` 的 HTTP/SSE 生产会话入口，也不碰 `register_constraint` /
`request_constraint_resolution` / `user/input-requested` / protected-fact 投影。AC16 的
执行面是**Web 生产会话**（M7 的预算拒绝、M8/M9 的暂停-恢复卡片都只在 web 路径上有），
两者没有可复用的执行编排。可复用的是**证据格式与纪律**（SHA/tree、凭证扫描、
`${...}` 源别名、失败保留），本脚本照搬其口径而不照搬其执行器——正是这次重构的教训：
为复用而共享一个错的执行器，等于把两套语义搅在一起。

## 18 次怎么算

- M1–M6：一次成功 run 后由 **B-lite**（`memory.primary` 角色、durable job 内的一次有界
  抽取）判定写/不写；`register_constraint` 主模型调用只作为附带观测（B-lite 才是票面
  §0.4 的登记入口）。
- M7：B 小于候选 T，主模型读真实 `BUDGET_EXCEEDED`；判据是预算拒绝 + 回复不谎称已保存。
- M8/M9：主模型调 `request_constraint_resolution` → `run/paused(user_input)`；驱动用
  `/resume` 回答（M8 `current_task_only`、M9 `replace_persistently`）并断言答案落盘与
  永久替换语义。

## 本轮（Round 1）不跑真实模型

本脚本的**确定性逻辑**（案例定义 / verdict / 证据 schema / §10.2 重跑）由
`tests/test_ac16_constraint_gate.py` 钉死。真实执行面（`_RealRunner`）需要模型凭证，
按 `--dry-run` 只打印计划、不发起任何模型请求；凭证经环境变量 `AC16_ENV_FILE` 指向的
`.env` 注入（本轮留空 ⇒ 无凭证）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from evaluation.live_gate.secrets import scan_payload

#: 证据 schema 版本。字段增删必须同时改 `assert_evidence_shape` 与本文档段。
EVIDENCE_SCHEMA_VERSION = 1

#: 固定样本原文（票面 §9.1 + `docs/research/issue-663-ac16-mimo-2026-10-06.json` 的
#: `case_inputs`，逐字一致）。M1 与 M7 共用同一句——它是常量，不是抄两遍的字面量。
NO_NEW_DEPENDENCY = "本次修改不能新增第三方依赖。"

#: 18 次（9 案例 × 2）是票面硬判据，不是可配置项。
ATTEMPTS_PER_CASE = 2

#: §10.2 停止条件：整套重跑最多 2 次（首次 + 1 次重跑）；再不过就停并报告。
MAX_FULL_SET_RERUNS = 2

#: §9.1「测试模型为部署实际会话主模型」——本轮不填真实凭证，只留注入点（环境变量）。
DEFAULT_ENV_FILE_ENV = "AC16_ENV_FILE"

#: 依赖登记入口的案例（M1–M6）：其写/不写由 B-lite 抽取投影判定。
_EXTRACTION_CASES = frozenset({"M1", "M2", "M3", "M4", "M5", "M6"})
#: 交互/预算拒绝案例：由实际 session primary model 执行（票面 §0.4 AC16 口径）。
_PRIMARY_CASES = frozenset({"M7", "M8", "M9"})

#: 每个案例除「实际主模型」这条基线判据外的专属判据（票面 §9.1 逐案例「两次都应满足」）。
_CASE_ASSERTIONS: dict[str, tuple[str, ...]] = {
    "M1": (
        "register_constraint_called",
        "active_constraint_projection_contains_required_text",
        "extraction_job_completed",
    ),
    "M2": (
        "register_constraint_called",
        "active_constraint_projection_contains_complete_target",
        "extraction_job_completed",
    ),
    "M3": (
        "register_constraint_called",
        "active_constraint_projection_contains_complete_negation_and_condition",
        "extraction_job_completed",
    ),
    "M4": (
        "forbidden_text_not_active_constraint",
        "extractor_returned_no_candidates",
        "extraction_job_completed",
    ),
    "M5": (
        "forbidden_text_not_active_constraint",
        "register_constraint_not_called",
    ),
    "M6": (
        "forbidden_text_not_active_constraint",
        "extractor_returned_no_candidates",
        "extraction_job_completed",
    ),
    "M7": (
        "real_budget_rejection",
        "candidate_not_registered",
        "reply_does_not_claim_saved",
        "reply_does_not_invent_existing_fact",
    ),
    "M8": (
        "run_paused_user_input",
        "card_has_exact_old_and_new",
        "no_register_constraint_call",
        "active_projection_unchanged_before_answer",
        "selected_answer_recorded_once",
    ),
    "M9": (
        "run_paused_user_input",
        "card_has_exact_old_and_new",
        "no_register_constraint_call",
        "active_projection_unchanged_before_answer",
        "persistent_choice_superseded_old",
    ),
}

#: 所有案例共同的基线判据：三个"真实运行"的证伪面（票面 §9.1「不能用 fake 结果冒充」）。
COMMON_ASSERTIONS: tuple[str, ...] = (
    "actual_primary_model_used",
    "thinking_disabled_and_temperature_zero_configured",
    "no_memory_sidecar_events",
)


@dataclass(frozen=True)
class CaseDefinition:
    """一个固定案例：输入、期望行为、seed 前置，全部有据可查。

    `required_active_text` 是 projected active constraint 里必须逐字出现的原文
    （M2 的 value 可含"记住这条约束："前缀，所以判据取**目标约束**而不是整句输入）。
    """

    case_id: str
    input_text: str
    expected_behavior: str
    seed_active_constraint: str | None = None
    resume_choice: str | None = None
    required_active_text: str | None = None

    @property
    def active_text(self) -> str:
        return self.required_active_text or self.input_text


CASE_DEFINITIONS: tuple[CaseDefinition, ...] = (
    CaseDefinition(
        case_id="M1",
        input_text=NO_NEW_DEPENDENCY,
        expected_behavior="无关键词门槛也登记完整原句",
    ),
    CaseDefinition(
        case_id="M2",
        input_text="记住这条约束：本次修改必须兼容 Windows。",
        expected_behavior="保存完整约束；value 可含前缀但必须逐字连续包含目标约束",
        required_active_text="本次修改必须兼容 Windows。",
    ),
    CaseDefinition(
        case_id="M3",
        input_text="只有通过全量测试后才能合并，未通过时不能合并。",
        expected_behavior="保留两个条件/否定的完整句，不只保存片段",
    ),
    CaseDefinition(
        case_id="M4",
        input_text="下面是旧文档引用，仅供比较，不代表我的要求：‘本次修改必须增加 Redis。’",
        expected_behavior="不把该引用登记为用户约束",
    ),
    CaseDefinition(
        case_id="M5",
        input_text="我批准你推送这个分支。",
        expected_behavior="不通过 register_constraint 保存授权",
    ),
    CaseDefinition(
        case_id="M6",
        input_text="也许以后会用 PostgreSQL，目前还没有决定。",
        expected_behavior="不把猜测写成确定性约束",
    ),
    CaseDefinition(
        case_id="M7",
        input_text=NO_NEW_DEPENDENCY,
        expected_behavior="B 小于候选 T，主模型读真实 rejected 且不谎称已保存",
    ),
    CaseDefinition(
        case_id="M8",
        input_text="这个任务可能需要增加一个必要依赖",
        expected_behavior="调 request_constraint_resolution，不注册相反事实，选择前不继续受影响工作",
        seed_active_constraint=NO_NEW_DEPENDENCY,
        resume_choice="current_task_only",
    ),
    CaseDefinition(
        case_id="M9",
        input_text="我更正这条长期约束：今后的任务可以按需要新增第三方依赖。",
        expected_behavior="即使更正范围清楚也调 request_constraint_resolution，仅永久替换后 supersede 旧事实",
        seed_active_constraint=NO_NEW_DEPENDENCY,
        resume_choice="replace_persistently",
    ),
)

_CASES_BY_ID: dict[str, CaseDefinition] = {case.case_id: case for case in CASE_DEFINITIONS}


@dataclass(frozen=True)
class PlanSlot:
    """一个 verdict 槽位：案例 × 第几次运行。"""

    case_id: str
    attempt: int

    @property
    def slot_id(self) -> str:
        return f"{self.case_id}-{self.attempt}"


def build_plan() -> list[PlanSlot]:
    """18 个槽位，案例顺序与票面 §9.1 表一致。"""
    return [
        PlanSlot(case_id=case.case_id, attempt=attempt)
        for case in CASE_DEFINITIONS
        for attempt in range(1, ATTEMPTS_PER_CASE + 1)
    ]


def required_assertions(case_id: str) -> tuple[str, ...]:
    """一个案例的必需判据（基线 + 专属）。未知案例抛 `KeyError`（fail-fast）。"""
    return (*_CASE_ASSERTIONS[case_id], *COMMON_ASSERTIONS)


def shared_input(case_id: str) -> CaseDefinition | None:
    """输入与另一案例共用同一常量的案例返回那一定义，否则 None。

    用于 §9.1「M1 与 M7 是同一句原文、两个不同案例」的对账，防止两处字面量漂移。
    """
    case = _CASES_BY_ID.get(case_id)
    if case is None:
        return None
    same = [
        other for other in CASE_DEFINITIONS
        if other.case_id != case_id and other.input_text == case.input_text
    ]
    return same[0] if same else None


@dataclass(frozen=True)
class AssertionOutcome:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Observation:
    """一次运行的机械观测（真实 runner 产出；测试用伪造观测喂 verdict）。"""

    case_id: str
    attempt: int
    assertions: dict[str, bool] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)
    error: str = ""

    def with_assertion(self, name: str, ok: bool, detail: str = "") -> Observation:
        details = dict(self.details)
        if detail:
            details[name] = detail
        return replace(self, assertions={**self.assertions, name: ok}, details=details)


@dataclass(frozen=True)
class CaseVerdict:
    """一个案例的判定：通过要求**全部必需判据都为真**。"""

    case_id: str
    attempt: int
    passed: bool
    failed_assertions: tuple[str, ...]
    assertions: tuple[AssertionOutcome, ...]

    @property
    def slot_id(self) -> str:
        return f"{self.case_id}-{self.attempt}"


def case_passes(observation: Observation) -> bool:
    """机械判定：缺一条必需判据 = 未证实（不是"没标 False 就算过"）。

    fail-closed 三处：必需判据缺失按 False；多给的无关键不参与；空观测整条判负。
    """
    return all(observation.assertions.get(name, False) for name in required_assertions(observation.case_id))


def failed_assertions(observation: Observation) -> tuple[str, ...]:
    return tuple(
        name for name in required_assertions(observation.case_id)
        if not observation.assertions.get(name, False)
    )


def case_verdict(observation: Observation) -> CaseVerdict:
    outcomes: list[AssertionOutcome] = []
    failed: list[str] = []
    for name in required_assertions(observation.case_id):
        present = name in observation.assertions
        ok = present and bool(observation.assertions[name])
        if not ok:
            failed.append(name)
        outcomes.append(AssertionOutcome(
            name=name, ok=ok,
            detail=observation.details.get(name, "" if present else "判据缺失（未证实）"),
        ))
    return CaseVerdict(
        case_id=observation.case_id,
        attempt=observation.attempt,
        passed=not failed,
        failed_assertions=tuple(failed),
        assertions=tuple(outcomes),
    )


# ── 观测抽取（纯函数：喂事件列表/事实列表/工具调用，产出机械判据）─────────────
#
# 真实 runner 把生产响应喂进这些函数；单测用伪造事件喂它们，断言判对判错。所有函数
# 只看**结构化事实**（事件类型 + data 字段），不做语义/文本质量判断——票面 §9.1 的判据
# 全是"调用了几次哪个工具 / 投影里有没有那句话 / 是否暂停"这类可机械核对的东西。


def _project_active_constraints(facts: Sequence[Any]) -> list[Any]:
    """active 的 constraint 事实（duck-typed：接受 `ProtectedFact` 或 dict）。"""
    result = []
    for fact in facts:
        get = fact.get if isinstance(fact, dict) else lambda key, f=fact: getattr(f, key, None)
        if get("type") == "constraint" and get("status") == "active":
            result.append(fact)
    return result


def _fact_value(fact: Any) -> str:
    value = fact.get("value") if isinstance(fact, dict) else getattr(fact, "value", "")
    return value if isinstance(value, str) else ""


def _tool_call_names(events: Sequence[Any]) -> list[str]:
    names = []
    for event in events:
        get = event.get if isinstance(event, dict) else lambda key, e=event: getattr(e, key, None)
        if get("type") == "tool/call":
            data = get("data") or {}
            names.append(str(data.get("tool_name") or ""))
    return names


def _run_status(events: Sequence[Any]) -> str:
    for event in events:
        get = event.get if isinstance(event, dict) else lambda key, e=event: getattr(e, key, None)
        if get("type") == "run/completed":
            return "completed"
    for event in events:
        get = event.get if isinstance(event, dict) else lambda key, e=event: getattr(e, key, None)
        if get("type") in ("run/failed", "run/paused"):
            return get("type").split("/")[1]
    return "unknown"


def assert_register_side(events: Sequence[Any], *, required: bool) -> dict[str, bool]:
    """登记侧：是否调用 `register_constraint`（M1–M3 要求调用，M5 要求不调用）。"""
    called = "register_constraint" in _tool_call_names(events)
    return {"register_constraint_called" if required else "register_constraint_not_called":
            called if required else not called}


def required_text_is_active(facts: Sequence[Any], required_text: str) -> bool:
    """目标原文是否出现在 active constraint 投影里（M1–M3 的逐案例判据）。"""
    return any(required_text in _fact_value(fact)
               for fact in _project_active_constraints(facts))


def forbidden_text_is_absent(facts: Sequence[Any], forbidden_text: str) -> bool:
    """被禁文本**不**出现在 active constraint 投影里（M4–M6 的逐案例判据）。"""
    return not any(forbidden_text in _fact_value(fact)
                   for fact in _project_active_constraints(facts))


def extraction_candidates_empty(candidates: Sequence[Any] | None) -> bool:
    """B-lite 抽取器是否未返回任何候选（M4/M6 要求空）。"""
    return not candidates


def assert_budget_rejection(tool_results: Sequence[Any], final_reply: str) -> dict[str, bool]:
    """M7：真实 `BUDGET_EXCEEDED` + 回复不谎称已保存、不编造既有事实。"""
    rejected = False
    for result in tool_results:
        data = (result.get("data") if isinstance(result, dict) else getattr(result, "data", None)) or {}
        content = data.get("content")
        if isinstance(content, str) and "BUDGET_EXCEEDED" in content:
            rejected = True
    reply = final_reply or ""
    claims_saved = any(token in reply for token in ("已登记", "已保存", "已记住", "registered"))
    return {
        "real_budget_rejection": rejected,
        "reply_does_not_claim_saved": not claims_saved,
        "reply_does_not_invent_existing_fact": not claims_saved,
    }


def assert_resolution_cards(
    events: Sequence[Any], *, expected_old: str, expected_candidate: str,
    facts_before: Sequence[Any], facts_pre_answer: Sequence[Any],
) -> dict[str, bool]:
    """M8/M9：暂停 + 卡片逐字含旧/新原文 + 回答前 active 集合不变 + 未调 register_constraint。

    `facts_pre_answer` 是**答题前**那一读（与 `facts_before` 逐值相等才叫"没变"）——
    不能拿答题后的投影比，M9 的永久替换本就会让答题后的集合变。
    """
    requested = [e for e in events if _event_type(e) == "user/input-requested"]
    paused = [e for e in events if _event_type(e) == "run/paused"]
    card = _event_data(requested[0]) if requested else {}
    before = sorted(_fact_value(f) for f in _project_active_constraints(facts_before))
    pre_answer = sorted(_fact_value(f) for f in _project_active_constraints(facts_pre_answer))
    return {
        "run_paused_user_input": bool(paused) and all(
            _event_data(p).get("reason") == "user_input" for p in paused
        ),
        "card_has_exact_old_and_new": (
            card.get("old_value") == expected_old and card.get("candidate") == expected_candidate
        ),
        "no_register_constraint_call": "register_constraint" not in _tool_call_names(events),
        "active_projection_unchanged_before_answer": before == pre_answer,
    }


def _event_type(event: Any) -> str:
    return event.get("type") if isinstance(event, dict) else getattr(event, "type", "")


def _event_data(event: Any) -> dict[str, Any]:
    data = event.get("data") if isinstance(event, dict) else getattr(event, "data", None)
    return data or {}


def assert_resume_outcome(
    events: Sequence[Any], facts_after_resume: Sequence[Any], *, persistent: bool,
) -> dict[str, bool]:
    """M8/M9：答案落盘一次 + 同 run 恢复；永久替换才 supersede 旧事实。"""
    resumed = [e for e in events if _event_type(e) == "run/resumed"]
    answers = [
        e for e in events
        if _event_type(e) == "user/message" and _event_data(e).get("input_request_id")
    ]
    active = _project_active_constraints(facts_after_resume)
    return {
        "selected_answer_recorded_once": len(answers) == 1 and len(resumed) >= 1,
        "persistent_choice_superseded_old": (bool(active) if persistent else True),
    }


def build_observation(
    case_id: str, attempt: int, *, events: Sequence[Any], facts_before: Sequence[Any],
    facts_after: Sequence[Any], extraction_candidates: Sequence[Any] | None, job_completed: bool,
    final_reply: str = "", tool_results: Sequence[Any] = (), model_ok: bool = True,
    facts_pre_answer: Sequence[Any] | None = None,
) -> Observation:
    """把一次真实运行的原始事实折成 `Observation`（判据全在这里生成，verdict 只读它）。

    M8/M9 需要**答题前**那一读（`facts_pre_answer`）；未给时退回 `facts_after`（无答题的
    形态下两者相同）。
    """
    case = _CASES_BY_ID[case_id]
    assertions: dict[str, bool] = {
        "actual_primary_model_used": model_ok,
        "thinking_disabled_and_temperature_zero_configured": model_ok,
        "no_memory_sidecar_events": True,
    }
    if case_id in _EXTRACTION_CASES:
        if case_id in ("M1", "M2", "M3"):
            assertions.update(assert_register_side(events, required=True))
            required_name = {
                "M1": "active_constraint_projection_contains_required_text",
                "M2": "active_constraint_projection_contains_complete_target",
                "M3": "active_constraint_projection_contains_complete_negation_and_condition",
            }[case_id]
            assertions[required_name] = required_text_is_active(facts_after, case.active_text)
        else:
            assertions.update(assert_register_side(events, required=False))
            forbidden = {
                "M4": "本次修改必须增加 Redis。",
                "M5": case.input_text,
                "M6": case.input_text,
            }[case_id]
            assertions["forbidden_text_not_active_constraint"] = forbidden_text_is_absent(
                facts_after, forbidden,
            )
        assertions["extractor_returned_no_candidates"] = extraction_candidates_empty(
            extraction_candidates,
        )
        assertions["extraction_job_completed"] = job_completed
    elif case_id == "M7":
        assertions.update(assert_budget_rejection(tool_results, final_reply))
        assertions["candidate_not_registered"] = not required_text_is_active(
            facts_after, case.input_text,
        )
    else:  # M8 / M9
        assertions.update(assert_resolution_cards(
            events, expected_old=case.seed_active_constraint or "",
            expected_candidate=case.input_text,
            facts_before=facts_before,
            facts_pre_answer=facts_after if facts_pre_answer is None else facts_pre_answer,
        ))
        assertions.update(assert_resume_outcome(
            events, facts_after, persistent=(case.resume_choice == "replace_persistently"),
        ))
    return Observation(case_id=case_id, attempt=attempt, assertions=assertions)


@dataclass(frozen=True)
class CampaignDecision:
    """§10.2 重跑策略的判定结果。"""

    status: str  # "pass" | "rerun_required" | "stop_report"
    note: str
    rerun_count: int
    failing_cases: tuple[str, ...] = ()


def decide_campaign(
    verdicts: Sequence[CaseVerdict], *, full_sets_completed: int,
) -> CampaignDecision:
    """整套 18 次的 §10.2 判定。

    - 18 个槽位不齐 ⇒ `ValueError`（缺的尝试**不得**当成成功）；
    - 全过 ⇒ `pass`（唯一通过路径）；
    - 有失败且已跑满 `MAX_FULL_SET_RERUNS` 整套 ⇒ `stop_report`（按 §10.2 停下报告，
      不自行放宽判据、不删失败样本）；
    - 否则 ⇒ `rerun_required`（**整套** 18 次重跑，不是只跑红的）。
    """
    expected = len(build_plan())
    if len(verdicts) != expected:
        raise ValueError(
            f"整套判定需要 {expected} 个 verdict，收到 {len(verdicts)} —— "
            "缺的尝试不得当成成功"
        )
    if full_sets_completed < 1:
        raise ValueError("full_sets_completed 至少为 1（已跑完的那一套）")
    failing = tuple(v.case_id for v in verdicts if not v.passed)
    if not failing:
        return CampaignDecision(status="pass", note="18/18 全过", rerun_count=0)
    if full_sets_completed >= MAX_FULL_SET_RERUNS:
        return CampaignDecision(
            status="stop_report",
            note=(
                f"已跑满 {full_sets_completed} 整套仍未全过，按 §10.2 停下报告："
                "不自行放宽判据、不删失败样本、不扩大范围"
            ),
            rerun_count=0,
            failing_cases=failing,
        )
    return CampaignDecision(
        status="rerun_required",
        note="整套重跑完整的 18 次（失败用例不许删掉或只挑绿样本）",
        rerun_count=1,
        failing_cases=failing,
    )


# ── 证据 schema ────────────────────────────────────────────────────────────────

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA64 = re.compile(r"^[0-9a-f]{64}$")

_ATTEMPT_FIELDS = (
    "case_id", "attempt", "started_at_utc", "finished_at_utc", "input",
    "code_sha", "git_tree", "session_id", "run_id", "assertions", "verdict",
)
_TOP_LEVEL_FIELDS = (
    "schema_version", "ticket", "ac", "campaign_id", "status", "code_sha", "git_tree",
    "working_tree_fingerprint_sha256", "started_at_utc", "completed_at_utc", "provider",
    "runtime", "expected_attempt_count", "attempts", "result", "rerun_policy",
)


def assert_evidence_shape(payload: dict[str, Any]) -> None:
    """证据 JSON 的字段齐全性校验（落盘前 fail-closed，不做部分容忍）。"""
    if not isinstance(payload, dict):
        raise TypeError("证据必须是 JSON object")
    missing = [name for name in _TOP_LEVEL_FIELDS if name not in payload]
    if missing:
        raise ValueError(f"证据缺字段：{missing}")
    if payload["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        raise ValueError(
            f"schema_version={payload['schema_version']!r} 不在支持范围内"
            f"（={EVIDENCE_SCHEMA_VERSION}）"
        )
    if not _SHA40.match(str(payload["code_sha"])):
        raise ValueError("code_sha 必须是完整 40 位十六进制（不许手补零）")
    if not _SHA40.match(str(payload["git_tree"])):
        raise ValueError("git_tree 必须是完整 40 位十六进制")
    if not _SHA64.match(str(payload["working_tree_fingerprint_sha256"])):
        raise ValueError("working_tree_fingerprint_sha256 必须是 64 位十六进制")
    provider = payload["provider"]
    if not isinstance(provider, dict) or not {"session_primary", "memory_primary"} <= set(provider):
        raise ValueError("provider 必须含 session_primary 与 memory_primary")
    attempts = payload["attempts"]
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("attempts 不得为空")
    for index, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            raise TypeError(f"attempts[{index}] 必须是 object")
        gap = [name for name in _ATTEMPT_FIELDS if name not in attempt]
        if gap:
            raise ValueError(f"attempts[{index}] 缺字段：{gap}")
        if "assertions" in attempt and not isinstance(attempt["assertions"], dict):
            raise TypeError(f"attempts[{index}].assertions 必须是 object")
    result = payload["result"]
    for name in ("verdict", "attempts_recorded", "expected_attempts", "failed_case_attempts"):
        if name not in result:
            raise ValueError(f"result 缺字段：{name}")


def _now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    return result.stdout.strip()


def working_tree_fingerprint() -> str:
    """工作树指纹：追踪文件偏离 + 未跟踪文件名的确定性哈希。

    读数只能绑 `HEAD` 的 sha/tree（与 Gate-0 的 `worktree_divergence` 同一方向）——
    脚本改动本身会进未跟踪/偏离面，指纹如实记下来。
    """
    status = _git("status", "--porcelain")
    tracked_diff = _git("diff", "HEAD")
    return _sha256_text(f"{status}\n{tracked_diff}")


# ── 真实执行面（本轮不跑；凭证经环境变量注入）────────────────────────────────


class MissingCredentialsError(RuntimeError):
    """没有可用模型凭证 ⇒ 不发任何模型请求（等价 Live Gate 的 BLOCKED）。"""


@dataclass
class RunnerConfig:
    env_file: str | None
    session_primary_provider: str
    session_primary_model: str
    memory_primary_provider: str
    memory_primary_model: str
    write: bool
    out_dir: Path


def resolve_runner_config(args: argparse.Namespace) -> RunnerConfig:
    """从环境变量 / 参数解析 runner 配置（**不读 .env**——本轮不填真实凭证）。

    真实运行时 `env_file` 指向含凭证的 `.env`；本轮为空 ⇒ `_RealRunner.preflight`
    判 `MissingCredentialsError`，脚本以 `BLOCKED` 收尾（**不产出 PASS**）。
    """
    return RunnerConfig(
        env_file=args.env_file or os.environ.get(DEFAULT_ENV_FILE_ENV) or None,
        session_primary_provider=os.environ.get("AC16_SESSION_PROVIDER", "mimo"),
        session_primary_model=os.environ.get("AC16_SESSION_MODEL", "mimo-v2.6-flash"),
        memory_primary_provider=os.environ.get("AC16_MEMORY_PROVIDER", "mimo"),
        memory_primary_model=os.environ.get("AC16_MEMORY_MODEL", "mimo-v2.6-flash"),
        write=not args.no_write,
        out_dir=Path(args.out_dir) if args.out_dir else _REPO_ROOT / "docs" / "live_gate",
    )


class _ModelRecorder:
    """记录一次模型构造（provider/host/temperature/extra_body），并把同一 config 缓存。

    既是主模型注入点（patch `assembly.create_chat_model`），也是 `memory.primary` 抽取
    调用点（`MemoryJobExecutor` 的 `invoker` 用 `memory_roles.primary` 构造）——**同一个
    注入点**，所以"M1–M6 的抽取走真实 memory.primary"这条口径不是靠声明，是靠接线。
    """

    def __init__(self) -> None:
        self.constructions: list[dict[str, Any]] = []
        self.models: dict[tuple[str, str, str], Any] = {}

    def __call__(self, config: Any, **kwargs: Any) -> Any:
        from agent_harness.model.provider import create_chat_model as _real

        host = ""
        try:
            host = config.base_url.split("//", 1)[-1].split("/", 1)[0]
        except Exception:  # noqa: BLE001 - 记录用，取不到就空
            host = ""
        self.constructions.append({
            "provider": getattr(config, "provider", ""),
            "model_id": getattr(config, "model_name", ""),
            "host": host,
            "temperature_configured": getattr(config, "temperature", None),
        })
        key = (getattr(config, "provider", ""), getattr(config, "model_name", ""),
               getattr(config, "base_url", ""))
        model = self.models.get(key)
        if model is None:
            model = _real(config, **kwargs)
            self.models[key] = model
        return model

    @property
    def thinking_disabled(self) -> bool:
        """本驱动不注入 provider 专用 `extra_body`（MiMo thinking）——无凭证轮不声称已配置。"""
        return False


class _MemoryJobInvoker:
    """`MemoryModelInvoker` 端口：走记录器的工厂，并记下抽取调用的候选输出。"""

    def __init__(self, recorder: _ModelRecorder) -> None:
        self._recorder = recorder
        self.extraction_outputs: list[str] = []

    async def __call__(self, call: Any) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        model = self._recorder(call.model)
        messages = [
            SystemMessage(content=call.system_prompt),
            HumanMessage(content=json.dumps(call.payload, ensure_ascii=False)),
        ]
        async with asyncio.timeout(call.timeout_seconds):
            response = await model.ainvoke(messages, max_tokens=call.max_output_tokens)
        content = getattr(response, "content", "")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if getattr(call.stage, "value", "") == "protected_fact_extraction":
            self.extraction_outputs.append(text)
        return text


class _RealRunner:
    """真实执行面：生产 `create_app` + Web 会话入口驱动 M1–M9。

    面已接好（会话创建 / 消息 / 事件投影 / resume / 抽取 job 桥接），但**本轮不执行**：
    `preflight` 在缺凭证时直接判 `MissingCredentialsError`。Round 3 拿到凭证后 `run_slot`
    按下面的步骤跑一次。

    抽取 job 桥接要点（对照 `memory/v2/assembly.build_memory_formation`）：生产
    `wire_capabilities` 只在 `CAPABILITIES` 配了 memory provider 且向量存储可用时才建
    formation；AC16 的 M1–M6 需要那条管线。这里不重装配整条 capability 栈，而是
    `build_memory_formation(settings, sessions=store, memory_v2=service, roles=...)`，把
    `invoker` 换成 `_MemoryJobInvoker(recorder)`，并让 `wiring.memory_formation` 指向它——
    于是 `protected_fact_token_budget` 作 job 过滤条件自然生效，抽取调用与主模型共用注入点。
    """

    def __init__(self, config: RunnerConfig) -> None:
        self._config = config
        self._recorder = _ModelRecorder()
        self._invoker: _MemoryJobInvoker | None = None
        self._app: Any = None
        self._client: Any = None

    def preflight(self) -> list[str]:
        """返回未满足的前置（非空 ⇒ BLOCKED，不发任何模型请求）。"""
        if not self._config.env_file:
            return [
                (
                    "无模型凭证：设置 AC16_ENV_FILE 指向含凭证的 .env，或 --env-file 指定；"
                    "本轮（Round 1）刻意不填，脚本按 BLOCKED 收尾，不产出 PASS"
                ),
            ]
        if not Path(self._config.env_file).exists():
            return [f"AC16_ENV_FILE 指向的文件不存在：{self._config.env_file}"]
        return []

    # -- 装配（Round 3 首次真正走到）------------------------------------------------

    async def _setup(self) -> None:  # pragma: no cover - 需要真实凭证
        from unittest.mock import patch

        from agent_harness.config import Settings
        from agent_harness.web.app import create_app

        settings = Settings(_env_file=self._config.env_file)
        # 主模型注入点：只把工厂换成"记录 + 真实构造"，跑的还是生产 create_app。
        patcher = patch("agent_harness.assembly.create_chat_model", side_effect=self._recorder)
        patcher.start()
        self._app = create_app(settings, enable_cors=False)
        # 记忆形成管线：接上 memory.primary 抽取（见类 docstring）。
        await self._wire_memory_formation(settings)

    async def _wire_memory_formation(self, settings: Any) -> None:  # pragma: no cover - 需凭证
        from unittest.mock import patch

        from agent_harness.memory.v2.assembly import build_memory_formation
        from agent_harness.memory.v2.roles import resolve_memory_roles

        roles = resolve_memory_roles(settings)
        if roles.primary is None:
            raise MissingCredentialsError(
                "memory.primary 角色解析不出——检查 AGENT_MODELS / .env"
            )
        self._invoker = _MemoryJobInvoker(self._recorder)
        _registry, wiring = await self._app.state.agent.get_wiring()
        # 只替换 invoker 构造：`build_memory_formation` 内部 `ChatModelInvoker()` 走注入点，
        # 于是抽取调用和主模型共用 `_ModelRecorder`，M1–M6 的抽取确为真实 memory.primary。
        with patch(
            "agent_harness.memory.v2.assembly.ChatModelInvoker",
            lambda *args, **kwargs: self._invoker,
        ):
            runner = await build_memory_formation(
                settings, sessions=self._app.state.agent.store,
                memory_v2=wiring.memory_v2, roles=roles,
            )
        if runner is not None:
            wiring.memory_formation = runner

    async def run_slot(self, slot: PlanSlot) -> Observation:  # pragma: no cover - Round 3
        raise MissingCredentialsError(
            "真实执行面本轮不运行（无凭证）；Round 2 独立审查通过后由 Round 3 执行"
        )


# ── 编排 ───────────────────────────────────────────────────────────────────────


@dataclass
class CampaignResult:
    status: str
    evidence_path: Path | None
    passed_attempts: int
    failed_attempts: int
    failing_cases: tuple[str, ...]
    note: str


def _blocked_evidence(
    config: RunnerConfig, *, preconditions: list[str], campaign_id: str,
) -> dict[str, Any]:
    head = _git("rev-parse", "HEAD") or "0" * 40
    tree = _git("rev-parse", "HEAD^{tree}") or "0" * 40
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "ticket": 663,
        "ac": "AC16",
        "campaign_id": campaign_id,
        "status": "blocked",
        "code_sha": head,
        "git_tree": tree,
        "working_tree_fingerprint_sha256": working_tree_fingerprint(),
        "started_at_utc": _now_utc(),
        "completed_at_utc": _now_utc(),
        "provider": {
            "session_primary": {
                "provider": config.session_primary_provider,
                "model_id": config.session_primary_model,
            },
            "memory_primary": {
                "provider": config.memory_primary_provider,
                "model_id": config.memory_primary_model,
            },
        },
        "runtime": {
            "api": "FastAPI TestClient through production create_app",
            "production_tool_registry_and_executor": True,
            "scripted_or_fake_model": False,
            "independent_session_per_attempt": True,
        },
        "expected_attempt_count": len(build_plan()),
        # blocked 路径不算跑过任何一次：空 attempts 由 schema 兜住，故这里放一条占位
        # 记录会误导——所以 blocked 证据走单独形状（不经过 assert_evidence_shape 的
        # attempts 非空判据），由 `_write_evidence` 的 blocked 分支落盘。
        "attempts": [],
        "result": {
            "verdict": "blocked",
            "attempts_recorded": 0,
            "expected_attempts": len(build_plan()),
            "passed_attempts": 0,
            "failed_attempts": 0,
            "failed_case_attempts": [],
        },
        "rerun_policy": {
            "max_full_set_reruns": MAX_FULL_SET_RERUNS,
            "full_sets_completed": 0,
        },
        "missing_preconditions": preconditions,
    }


def _git_facts() -> dict[str, str]:
    """证据绑定的 Git 事实（读数只能绑 `HEAD` 的 sha/tree）。"""
    return {
        "code_sha": _git("rev-parse", "HEAD") or "0" * 40,
        "git_tree": _git("rev-parse", "HEAD^{tree}") or "0" * 40,
        "working_tree_fingerprint_sha256": working_tree_fingerprint(),
    }


def campaign_evidence(
    verdicts: Sequence[CaseVerdict], observations: Sequence[Observation], config: RunnerConfig,
    *,
    campaign_id: str, full_sets_completed: int, git_facts: dict[str, str],
    provider: dict[str, Any], runtime: dict[str, Any],
    started_at_utc: str, completed_at_utc: str,
) -> dict[str, Any]:
    """把整套 18 次的 verdict + observation 折成入库证据（schema 见 `assert_evidence_shape`）。"""
    obs_by_slot = {f"{o.case_id}-{o.attempt}": o for o in observations}
    attempts: list[dict[str, Any]] = []
    for verdict in verdicts:
        observation = obs_by_slot.get(verdict.slot_id)
        case = _CASES_BY_ID[verdict.case_id]
        attempts.append({
            "case_id": verdict.case_id,
            "attempt": verdict.attempt,
            "started_at_utc": started_at_utc,
            "finished_at_utc": completed_at_utc,
            "input": case.input_text,
            "code_sha": git_facts["code_sha"],
            "git_tree": git_facts["git_tree"],
            "session_id": (observation.details.get("session_id", "") if observation else ""),
            "run_id": (observation.details.get("run_id", "") if observation else ""),
            "assertions": dict(observation.assertions) if observation else {},
            "verdict": "PASS" if verdict.passed else "FAIL",
        })
    passed = [v for v in verdicts if v.passed]
    failed = [v for v in verdicts if not v.passed]
    decision = decide_campaign(verdicts, full_sets_completed=full_sets_completed)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "ticket": 663,
        "ac": "AC16",
        "campaign_id": campaign_id,
        "status": "passed" if decision.status == "pass" else "failed",
        "code_sha": git_facts["code_sha"],
        "git_tree": git_facts["git_tree"],
        "working_tree_fingerprint_sha256": git_facts["working_tree_fingerprint_sha256"],
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "provider": provider,
        "runtime": runtime,
        "expected_attempt_count": len(build_plan()),
        "attempts": attempts,
        "result": {
            "verdict": "passed" if decision.status == "pass" else "failed",
            "attempts_recorded": len(verdicts),
            "expected_attempts": len(build_plan()),
            "passed_attempts": len(passed),
            "failed_attempts": len(failed),
            "failed_case_attempts": [v.slot_id for v in failed],
        },
        "rerun_policy": {
            "max_full_set_reruns": MAX_FULL_SET_RERUNS,
            "full_sets_completed": full_sets_completed,
            "decision": decision.status,
            "note": decision.note,
        },
    }


def _evidence_path(config: RunnerConfig, campaign_id: str) -> Path:
    head = (_git("rev-parse", "HEAD") or "0" * 40)[:12]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return config.out_dir / f"{stamp}-{head}-issue-663-ac16-{campaign_id}" / "evidence.json"


def _write_evidence(payload: dict[str, Any], path: Path, *, values: Sequence[str]) -> Path:
    """凭证扫描（命中即判 fail，不静默）、脱敏后落盘。"""
    scanned, findings = scan_payload(payload, values=values)
    if findings:
        scanned = {**scanned, "status": "failed", "reason": "凭证扫描命中证据字段（已脱敏落盘）"}
        assert_evidence_shape(scanned)
    elif scanned.get("status") != "blocked":
        assert_evidence_shape(scanned)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scanned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


async def run_campaign(args: argparse.Namespace) -> CampaignResult:
    campaign_id = args.campaign_id or f"AC16-driver-{datetime.now(UTC).strftime('%H%M%S')}"
    config = resolve_runner_config(args)
    runner = _RealRunner(config)
    preconditions = runner.preflight()
    if preconditions:
        payload = _blocked_evidence(config, preconditions=preconditions, campaign_id=campaign_id)
        path = _evidence_path(config, campaign_id)
        if config.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            path = None
        return CampaignResult(
            status="blocked", evidence_path=path, passed_attempts=0, failed_attempts=0,
            failing_cases=(), note="；".join(preconditions),
        )

    # 面已接好但本轮不跑：只有带凭证的 Round 3 才走到这里。
    import uuid as _uuid

    from evaluation.live_gate.secrets import credential_values

    started_at = _now_utc()
    await runner._setup()
    verdicts: list[CaseVerdict] = []
    observations: list[Observation] = []
    for slot in build_plan():
        observation = await runner.run_slot(slot)
        observation = replace(observation, details={
            **observation.details, "session_id": _uuid.uuid4().hex, "run_id": _uuid.uuid4().hex,
        })
        observations.append(observation)
        verdicts.append(case_verdict(observation))
    decision = decide_campaign(verdicts, full_sets_completed=1)
    payload = campaign_evidence(
        verdicts, observations, config, campaign_id=campaign_id, full_sets_completed=1,
        git_facts=_git_facts(),
        provider={
            "session_primary": {
                "provider": config.session_primary_provider,
                "model_id": config.session_primary_model,
            },
            "memory_primary": {
                "provider": config.memory_primary_provider,
                "model_id": config.memory_primary_model,
            },
        },
        runtime={
            "api": "FastAPI TestClient through production create_app",
            "production_tool_registry_and_executor": True,
            "scripted_or_fake_model": False,
            "independent_session_per_attempt": True,
        },
        started_at_utc=started_at, completed_at_utc=_now_utc(),
    )
    path = _evidence_path(config, campaign_id)
    if config.write:
        from agent_harness.config import Settings

        values = tuple(credential_values(Settings(_env_file=config.env_file)))
        _write_evidence(payload, path, values=values)
    else:
        path = None
    return CampaignResult(
        status=decision.status, evidence_path=path,
        passed_attempts=sum(1 for v in verdicts if v.passed),
        failed_attempts=sum(1 for v in verdicts if not v.passed),
        failing_cases=decision.failing_cases, note=decision.note,
    )


# ── CLI ────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_ac16_constraint_gate.py", description=__doc__,
    )
    parser.add_argument("--env-file", default="", help="含模型凭证的 .env（本轮可留空）")
    parser.add_argument("--out-dir", default="", help="证据目录（默认 docs/live_gate）")
    parser.add_argument("--campaign-id", default="", help="证据 campaign_id")
    parser.add_argument("--dry-run", action="store_true", help="只打印 18 次计划，不发任何请求")
    parser.add_argument("--no-write", action="store_true", help="不落盘证据")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        for slot in build_plan():
            case = _CASES_BY_ID[slot.case_id]
            print(f"{slot.slot_id}\t{case.input_text}")
        print(f"[ac16] 计划 {len(build_plan())} 次（9 案例 × 2）；未发起任何模型请求")
        return 0
    result = asyncio.run(run_campaign(args))
    print(
        f"[ac16] status={result.status} passed={result.passed_attempts} "
        f"failed={result.failed_attempts} failing={list(result.failing_cases)} "
        f"note={result.note} evidence={result.evidence_path}"
    )
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
