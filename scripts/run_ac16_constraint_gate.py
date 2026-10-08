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

## 18 次怎么跑

- M1–M6：一次成功 run 后由 **B-lite**（`memory.primary` 角色、durable job 内的一次有界
  抽取）判定写/不写；`register_constraint` 主模型调用只作为附带观测（B-lite 才是票面
  §0.4 的登记入口）。
- M7：B 小于候选 T，主模型读真实 `BUDGET_EXCEEDED`；判据是预算拒绝 + 回复不谎称已保存。
- M8/M9：主模型调 `request_constraint_resolution` → `run/paused(user_input)`；驱动用
  `/resume` 回答（M8 `current_task_only`、M9 `replace_persistently`）并断言答案落盘与
  永久替换语义。

真实执行面（`_RealRunner`）走生产 `create_app` 的 ASGI 入口 + `TestClient` 等价通道，
消费真实模型（`mimo`/`mimo-v2.6-flash`）与生产 tool registry / executor。

## 确定性逻辑与真实执行的边界

`tests/test_ac16_constraint_gate.py` 钉死**确定性**部分（案例定义 / verdict / 证据
schema / §10.2 重跑策略 / 凭证来源），不需要模型凭证即可复跑。真实执行面（`_RealRunner`）
需要 `MODEL_API_KEY`；无凭证时按 `--dry-run` 只打印 18 次计划、`--no-write` 不发请求。

## 凭证：`MODEL_API_KEY` 直传优先，绝不落盘

凭证按下面的优先级取，**第一顺位整条路径不碰磁盘**：

1. **环境变量 `MODEL_API_KEY`**（直传）：非空即直接用它的值 —— 全程**不写任何文件、
   不打印、不记日志**，值只在进程内存里走一圈（`Settings.model_api_key` → `ModelConfig`），
   最终由 provider SDK 在请求边界消费。
2. `AC16_ENV_FILE` 指向的文件（**fallback**，优先于 `--env-file`）。
3. `--env-file` 显式指定的文件。

⚠ 第 2/3 条的文件间顺序 = **任务书 §任务第 2 条明写的次序**（`AC16_ENV_FILE` > `--env-file`）。
Round 1 的实现是 `args.env_file or os.environ.get(DEFAULT_ENV_FILE_ENV)`（CLI 压过 env），
本轮按票面改正 —— 这条子序只在"直传缺席"时才生效，零安全影响，改动可追溯。

"环境变量 > 文件"是 **pydantic-settings 自身的优先级**（init kwargs > `os.environ` >
`env_file`，同一条记在 `tests/conftest.py::_clean_settings_env`），不是本脚本另加的判据；
所以文件型凭证一律经 `Settings(_env_file=...)` 读，本脚本**不自己解析 `.env`**。
直传路径的 env_file 兜底指仓库里**已跟踪**的 `.env.example` —— 那是**键名模板**、
不是凭证文件（真凭证列在那里等于泄漏），因此"直传不落盘"这句话不因它而破；
被 `.gitignore` 覆盖的 `.env` 由调用方经 `AC16_ENV_FILE` / `--env-file` 指进来。

两条路径汇合后都过同一道闸：落盘 evidence 前用 `evaluation.live_gate.secrets` 的**精确值
扫描**查一遍配置里的真凭证值，出现在任何字段 ⇒ 判 fail 并脱敏（回归证明：
`tests/test_ac16_constraint_gate.py::test_direct_key_is_scanned_as_configured_credential`）。

## Round 5 判据修订（2026-10-08，两份独立审查 converge）

Round 4 的 16 个 verdict **全部作废**——两份看不到实现过程的独立审查各自指出判据本身的
bug，14 个 PASS 里有假绿。本轮的判据改成下面这些**可机械证伪**的形状（每条都有反例测试）：

- **持久化声称**只判"genuine claim"：必须同时出现肯定式动词（已保存/已登记/已记住/
  registered…）**且**不含否定标记（未/没有/不能…）。旧实现在回复里裸搜 `已保存`
  ⇒ M7-1 那句诚实拒绝（"未能保存…已有活动约束…已保存"）被误判成"谎称已保存"（假 FAIL）。
  否定感知的合法例见 `tests/...::test_m7_negated_persistence_claim_is_not_a_claim`。
- **既有事实编造**（invent-existing-fact）另实现：说的是"这条本来就已存在/此前已登记"，
  且**本次 run 之前**的 active 集合里并没有它 ⇒ 假，否则（真有）⇒ 真。它不再与
  持久化声称共用同一个表达式（旧实现两处逐字相同，等于只有半分鉴别力）。
- **候选未登记**不再拿"候选原文逐字不出现"当判据（模型大概率改写措辞）⇒ 改成
  **`facts_before` 之外新增了 active constraint 即判负**（比对 fact_id / (type,value) 身份
  口径）。Round 5 实测又删掉了上一轮多加的第三条 `extractor_adopted_no_candidate`：它要求
  B-lite 抽取器**一个候选都不提**，而 M7 的候选正是用户真实约束原文——抽取器提出来是对的，
  生产按预算拒绝登记也是对的，此时"候选没被登记"成立；拿"抽取器提了候选"判负等于惩罚一个
  完全正确的 run。

- **M7 的预算拒绝**以工具结果**对象**为准（`tool/result.data.content` 解析出的
  `status=="rejected" and reason_code=="BUDGET_EXCEEDED"`，配合 `tool/call` 参数值逐字
  绑定候选原文），文本包含旧形状降级成**交叉核对**（两条不一致 ⇒ 报出来，不静默取一条）。
  **归因走 `tool/call` 而不是结果里的 `value`**：`register_constraint` 的拒形态 data
  （`_rejected`）**不回填 `value`**，只认"结果里的 value 等于候选"会让所有拒绝形态恒判负
  （Round 5 冒烟实测：M7 被判成"模型根本没调工具"）。
- **M1–M6 的登记判据不再要求 `register_constraint` 被调用**：票面 §0.4 明写这条路径的
  登记入口是 B-lite 后台抽取，而模型可以凭"约束已生效"的上下文正确地不调工具
  （Round 4 实测 M1–M3 两次都没调、抽取器两次都登记了）。"没调工具"在这些案例里
  既非必要也非充分，作为判据只会误伤诚实的模型行为。
- **基线判据只留两条**（`real_model_turn_observed` + `no_memory_sidecar_events`），都由
  **本次 attempt 的事件流**给出：旧实现里 `actual_primary_model_used` 由"温度=0 + 累积
  构造计数"合成、`thinking_disabled…` 是同一来源的别名、`no_memory_sidecar_events` 硬编码
  True——三条都不是本 attempt 的可证伪读数。provenance（构造次数/温度配置/thinking 如实值）
  改由 attempt 的 `model_provenance` 字段落盘，且 `case_verdict` 对"算了却不判"的装饰性
  判据整条判负。
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
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
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
#: v2（2026-10-08，Round 5）：attempt 增加 `error`（失败原因，P2-2）与 `model_provenance`
#: （本次 attempt 的模型构造 provenance，P2-1）；判据名集合也变了（旧口径的 verdict 不可比）。
EVIDENCE_SCHEMA_VERSION = 2

#: 固定样本原文（票面 §9.1 + `docs/research/issue-663-ac16-mimo-2026-10-06.json` 的
#: `case_inputs`，逐字一致）。M1 与 M7 共用同一句——它是常量，不是抄两遍的字面量。
NO_NEW_DEPENDENCY = "本次修改不能新增第三方依赖。"

#: 18 次（9 案例 × 2）是票面硬判据，不是可配置项。
ATTEMPTS_PER_CASE = 2

#: M4 那句用户粘贴的**被引用文本**（票面 §9.1：`‘本次修改必须增加 Redis。’`）。它是被禁
#: 登记的目标，与 `NO_NEW_DEPENDENCY` 同类：一个常量、一处定义（审查 Standards P3-3 指出
#: 驱动里有第二份字面量，两处会漂移）。
QUOTED_REFERENCE = "本次修改必须增加 Redis。"

#: §10.2 停止条件：整套重跑最多 2 次（首次 + 1 次重跑）；再不过就停并报告。
MAX_FULL_SET_RERUNS = 2

#: 直传凭证的环境变量名（全仓约定，见 `.env.example`）。非空 ⇒ 优先于任何凭证文件。
DIRECT_KEY_ENV = "MODEL_API_KEY"

#: `.env` 形态的**只读键名模板**（仓库跟踪，含 `MODEL_API_KEY=sk-xxx` 这类占位值）。
#: 直传路径下用作 `Settings(_env_file=...)` 的兜底 —— 取的是 pydantic-settings 的
#: `model_config` 默认值本身，**不是**新建一个可能被注入的候选路径。
EXAMPLE_ENV_FILE = _REPO_ROOT / ".env.example"

#: §9.1「测试模型为部署实际会话主模型」——文件型凭证的 fallback 注入点（有直传时可不用）。
DEFAULT_ENV_FILE_ENV = "AC16_ENV_FILE"

#: §9.1「temperature 使用该 Provider 支持的最低有效值」。部署默认是 `Settings.temperature
#: = 0.2`；本驱动显式覆盖成 0.0（`_session_primary_model_config`），否则主模型会用部署
#: 默认采样——那就不是"最低有效值"，且两次 run 的可复现性变差。
_MIN_EFFECTIVE_TEMPERATURE = 0.0

#: 单个 run 等的上限（秒）。超过就抛 TimeoutError，折成该槽位的失败观测——不静默跳过。
_RUN_TIMEOUT_SECONDS = 300.0
#: 等 run 终态的轮询间隔（秒）。
_POLL_INTERVAL_SECONDS = 0.5
#: B-lite 抽取 job 的 drain 上限（秒）。抽取是 job 内一次有界模型调用（工具超时 20s）。
_EXTRACTION_DRAIN_TIMEOUT_SECONDS = 120.0

#: 依赖登记入口的案例（M1–M6）：其写/不写由 B-lite 抽取投影判定。
_EXTRACTION_CASES = frozenset({"M1", "M2", "M3", "M4", "M5", "M6"})
#: 交互/预算拒绝案例：由实际 session primary model 执行（票面 §0.4 AC16 口径）。
_PRIMARY_CASES = frozenset({"M7", "M8", "M9"})

#: 每个案例除「实际主模型」这条基线判据外的专属判据（票面 §9.1 逐案例「两次都应满足」）。
#:
#: M1–M3 刻意**不**要求 `register_constraint_called`：票面 §0.4 规定这条路径的登记入口是
#: B-lite 后台抽取（`memory.primary`），主模型调用 `register_constraint` 只是**附带观测**。
#: Round 4 实测 M1–M3 两次都没调工具而抽取器两次都登记成功——把"调没调工具"写成必需判据
#: 会误伤诚实的模型行为（约束确实登记了），而它既不必要也不充分。
_CASE_ASSERTIONS: dict[str, tuple[str, ...]] = {
    "M1": (
        "active_constraint_projection_contains_required_text",
        "extraction_job_completed",
    ),
    "M2": (
        "active_constraint_projection_contains_complete_target",
        "extraction_job_completed",
    ),
    "M3": (
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
        "extraction_job_completed",
    ),
    "M6": (
        "forbidden_text_not_active_constraint",
        "extractor_returned_no_candidates",
        "extraction_job_completed",
    ),
    "M7": (
        "registered_tool_result_present",
        "tool_result_status_blocks_registration",
        "candidate_not_registered",
        "candidate_register_attempted_at_most_once",
        "reply_does_not_claim_saved",
        "reply_does_not_invent_existing_fact",
    ),
    "M8": (
        "run_paused_user_input",
        "card_has_exact_old_and_new",
        "no_register_constraint_before_answer",
        "active_projection_unchanged_before_answer",
        "selected_answer_recorded_once",
        "run_resumed_in_same_run",
        "persistent_choice_superseded_old",
    ),
    "M9": (
        "run_paused_user_input",
        "card_has_exact_old_and_new",
        "no_register_constraint_before_answer",
        "active_projection_unchanged_before_answer",
        "selected_answer_recorded_once",
        "run_resumed_in_same_run",
        "persistent_choice_superseded_old",
    ),
}

#: 所有案例共同的基线判据。
#:
#: 只留两条，都是**本 attempt 的事件流**给出的、可证伪的事实：
#: - `real_model_turn_observed`：真的出现过本 run 的模型回合（`model/completed` 带 run_id）；
#: - `no_memory_sidecar_events`：本 run 没有旁路记忆事件（`memory/*`）冒出来。
#:
#: 旧实现的三条（Round 4 审查 P2-1/Standards P2-1）已分别改正：
#: - `actual_primary_model_used` 原先由 `model_ok` 一处同时喂给两条判据（两份判据
#:   共用一个来源 ⇒ 没有独立鉴别力），现改为直接以本 attempt 的 `model/completed` 为准；
#: - `thinking_disabled_and_temperature_zero_configured` 不再进必需判据——本驱动不注入
#:   provider 专用 `extra_body`（`_ModelRecorder.thinking_disabled` 恒 False），把它写成
#:   "已验证"就是 over-claim。温度/thinking 的事实改由 `_primary_provenance_fields()`
#:   在 attempt 的 `model_provenance` 字段里**如实**落盘（配置值 + provider/model id + 跳数）；
#: - `no_memory_sidecar_events` 原先硬编码 True（恒真的判据 = 装饰），现按 slot 的 run_id
#:   真正过滤事件流里的 `memory/*`（`_memory_sidecar_events`）。
COMMON_ASSERTIONS: tuple[str, ...] = (
    "real_model_turn_observed",
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
        input_text=f"下面是旧文档引用，仅供比较，不代表我的要求：‘{QUOTED_REFERENCE}’",
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
        # 票面 M9 要「完整展示旧/新原文」= **新约束原文**，不是整句用户消息：工具自己的
        # 指导逐字要求 "Omit correction framing such as 'I correct this rule:'"，所以
        # 去掉更正前缀后的实质约束才是合法 candidate。Round 5 拿整句输入当期望值，把这条
        # 被指导要求的行为判成假负（实测两次都因此 FAIL，而同一文本在旧驱动下 PASS）。
        required_active_text="今后的任务可以按需要新增第三方依赖。",
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


def parse_slots(spec: str) -> list[PlanSlot]:
    """`"M9-1,M1-2"` → 那两个 `PlanSlot`（顺序照写、去重报错）。

    判据/观测修好后只重跑**受影响的槽位**时用（任务书 §任务B 条 2）。**fail-closed**：
    任何非法槽位一律 `ValueError`，不静默跳过 —— 少跑的槽位在证据里是"没跑"，而
    "没跑"最容易被当成"没红"。
    """
    slots: list[PlanSlot] = []
    seen: set[str] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            raise ValueError(f"槽位规格含空项：{spec!r}")
        if token in seen:
            raise ValueError(f"槽位重复：{token}")
        case_id, _, attempt_text = token.partition("-")
        if case_id not in _CASES_BY_ID:
            raise ValueError(f"未知案例：{case_id!r}（合法：{sorted(_CASES_BY_ID)}）")
        if not attempt_text.isdigit():
            raise ValueError(f"槽位格式应为 <案例>-<次数>：{token!r}")
        attempt = int(attempt_text)
        if not 1 <= attempt <= ATTEMPTS_PER_CASE:
            raise ValueError(f"次数越界：{token!r}（合法 1..{ATTEMPTS_PER_CASE}）")
        seen.add(token)
        slots.append(PlanSlot(case_id=case_id, attempt=attempt))
    if not slots:
        raise ValueError("槽位规格为空")
    return slots


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


def case_verdict(observation: Observation) -> CaseVerdict:
    """机械判定：缺一条必需判据 = 未证实（不是"没标 False 就算过"）。

    fail-closed 三处：必需判据缺失按 False；**多给的无关键整条判负**（下面那条不变量）；
    空观测整条判负。
    """
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
    # 不变量：**算出来的判据必须都是必需判据**。算了一条却不放进 `required_assertions`
    # 就是装饰（恒真的假判据，或恒假的假失败）——Round 4 审查实测：`tool_result_not_registered`
    # 与 `no_memory_sidecar_events` 都曾"算了但不判"，两条都让 verdict 与事实脱钩。
    # 这里 fail-closed 整条判负，把"改了判据忘了登记"变成响亮失败而不是静默失效。
    decoration = sorted(set(observation.assertions) - set(required_assertions(observation.case_id)))
    if decoration:
        failed.append(f"undecorated_assertions:{','.join(decoration)}")
        outcomes.append(AssertionOutcome(
            name=f"undecorated_assertions:{','.join(decoration)}", ok=False,
            detail="这些判据被算出来却没有登记进 required_assertions（装饰性判据，不参与判定）",
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


def assert_register_side(events: Sequence[Any], *, required: bool) -> dict[str, bool]:
    """登记侧：是否调用 `register_constraint`（M5 要求不调用）。

    M1–M3 **不**用这条（见 `_CASE_ASSERTIONS` 的说明）：票面 §0.4 的登记入口是 B-lite
    后台抽取，主模型调不调工具都不改变"约束登记了没有"这个判据。保留 `required=True`
    分支只为取证口径完整，当前没有案例使用它。
    """
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
    """B-lite 抽取器**真正采纳**的候选是否为空（M4/M6 要求空）。

    入参是 job 行里落库的候选列表（`protected_fact_extraction_candidates`），不是模型原始
    输出——原始输出有候选而 executor 全判不合格时，"登记"并没有发生，此时判"抽取器没产出"
    才是真的。**取 None（没有 job 行 / 阶段没跑到）判负**：那是"没证实"，不是"空"。
    """
    return candidates is not None and not candidates


def extraction_gate(state: str, job_id: str | None) -> dict[str, bool]:
    """M1–M4/M6 的两条抽取判据的**共同**机械依据（一次读 job 行，两处引用）。

    `extraction_job_completed`：本次 run 的 job 行存在，且抽取阶段已 `done`。
    `extractor_returned_no_candidates` 由 `extraction_candidates_empty` 单独出（M4/M6 要求
    空、M1–M3 不要求），这里只出"阶段跑完了没有"这一条。
    """
    return {"extraction_job_completed": bool(job_id) and state == "done"}


def extraction_candidates(call_outputs: Sequence[str] | None) -> list[Any] | None:
    """从**本次 run** 抽取调用的模型原始输出解析候选列表（`None` = 没解析出 JSON）。

    为什么不用 job 行的 `protected_fact_extraction_candidates`：`finish_protected_fact_extraction`
    跑完会把那一列**置回 NULL**（"Drop raw candidate text after idempotent registration has been
    attempted"）——于是 `done` 的行上候选取不到，而取到的时候（`ready`）又太早。能**逐 run**
    归属、且在判据那一刻仍在的，只有 invoker 记下的原始输出（每次 run 前清空，见 `_drive_case`）。

    executor 在这一步之后还会按 `source`/`value` 逐字核对 `source_text` 才登记，所以原始输出里的
    候选**不等于**最终采纳——但对 M4/M6 要判的那一条（"抽取器有没有硬扯出一个候选"）而言，原始
    输出正是判据；登记与否另有 `forbidden_text_not_active_constraint` 盯着，两条合起来才完整。
    """
    for text in reversed(list(call_outputs or [])):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("candidates"), list):
            return parsed["candidates"]
    return None


def case_id_needs_budget_pressure(case_id: str) -> bool:
    """M7 的案例定义要求"B 小于候选 T"——只有它需要预算压力前置。"""
    return case_id == "M7"


#: M7 预算压力种子：重复到足以让**投影后的保护事实**逼近默认预算 8192，但**不越线**
#: ——越线的话首个 turn 的 context 构建就抛 `ProtectedFactBudgetExceededError`、run 直接
#: 暂停收口，`register_constraint` 根本没机会被调用（实测 reps=376 即如此）。
#:
#: ⚠ **这是实测标定值，不是判据**（审查 P3-2）。reps 与"投影后 token 数"的关系**依赖
#: 生产侧 token 估算实现**（BPE 分词与消息包装口径），上游一改就会漂移——届时它会以
#: **响亮失败**的形式暴露（M7 的 `registered_tool_result_present` / 候选登记判据判负，
#: 而不是静默放行）。要重新标定：把 reps 调大到"首个 turn 就超预算"（会被 run 的暂停
#: 收口抓住）与调小到"候选加进去也不超"（会被 `tool_result_status_blocks_registration`
#: 抓住）之间取中值，并在证据里记下该 run 的实际 `estimated_tokens_after`。
#: 本轮值（372）来自 Round 4 同 tip 的实测；候选加进去后约 8259 > 8192。
_BUDGET_PRESSURE_REPETITIONS = 372
_BUDGET_PRESSURE_SENTENCE = "本次任务必须保留所有现有行为。"


def _read_job_row(database_path: Path, idempotency_key: str) -> dict[str, Any] | None:
    """按幂等键**只读**一条 formation job 行（不存在返回 None）。

    只取 `job_id`/`stage`/`protected_fact_extraction_state` 三列。**不取候选列**：那一列在
    抽取跑完时被置回 NULL，读它只会得到"done ⇒ None"，与"本次 run 没有 job 行"在形状上
    撞车。候选由 invoker 的 per-run 原始输出解析（`extraction_candidates`）。

    用标准库 `sqlite3` 而不是 `SqliteMemoryV2JobStore`：store 没有"按幂等键查"的读接口，
    唯一能命中的入口是 `enqueue`——而那是 INSERT OR IGNORE，会替本次 run **造**出一行，
    把判据变成恒真。这里只 SELECT（幂等键有 UNIQUE 约束，命中唯一）。
    """
    import sqlite3

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        cursor = connection.execute(
            "SELECT job_id, stage, protected_fact_extraction_state FROM memory_v2_jobs "
            "WHERE idempotency_key=?",
            (idempotency_key,),
        )
        row = cursor.fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    job_id, _stage, state = row
    return {"job": job_id, "extraction_state": state or ""}


def _read_extraction_candidates(database_path: Path, idempotency_key: str) -> list[Any] | None:
    """抽取**采纳后、置 NULL 前**的候选列表（`ready` 窗口内才有值；否则 None）。

    判据 `extractor_returned_no_candidates` 取"executor 按 source/value 逐字核对后**真正
    采纳**的候选"——那是比模型原始输出更强的一手证据（模型输出有候选而校验全不过时，
    登记并没有发生）。store 在 `finish_protected_fact_extraction` 里把这一列置回 NULL
    （"Drop raw candidate text…"），所以它只在 `ready` 窗口可读；驱动在等 `done` 的同时
    每秒捞一次，捞到就留底。捞不到时退回模型原始输出（`extraction_candidates`），
    两者都缺 ⇒ None（未证实，判负）。
    """
    import sqlite3

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        cursor = connection.execute(
            "SELECT protected_fact_extraction_candidates FROM memory_v2_jobs "
            "WHERE idempotency_key=?",
            (idempotency_key,),
        )
        row = cursor.fetchone()
    finally:
        connection.close()
    if row is None or row[0] is None:
        return None
    try:
        parsed = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


#: 肯定式持久化动词：出现这些词**才可能**是"声称已保存"。词表刻意窄——宽词表是
#: Round 4 假 FAIL 的来源（`已保存` 是 `已有活动约束…已保存` 的子串）。
_SAVED_CLAIM_VERBS = (
    "已保存", "已登记", "已记住", "已经保存", "已经登记", "已经记住",
    "已为你保存", "已为你登记", "已帮你保存", "已帮你登记",
    "registered", "has been saved", "have saved", "i saved", "i have saved",
    "saved it", "it is saved", "保存成功", "登记成功",
)

#: 否定标记（逐子串）：出现在**同一句**里 ⇒ 该句不是持久化声称。中文的否定几乎都是
#: 单字/双字前缀（未/没/不能/无法/不会/尚未/并未/别），英文靠 not/never/cannot/wasn't。
_NEGATION_MARKERS = (
    "未", "没有", "没", "不能", "无法", "不会", "尚未", "并未", "别", "不",
    "not ", "n't", "never", "cannot", "unable", "fail",
)

#: 句子/分句切分：中文句读、逗号顿号、换行、英文句点。声称与否定几乎总在**同一分句**里
#: （M7-1 的实测反例里，"已有活动约束…已保存" 与 "未能保存" 正是被逗号分开的两个分句）。
_SENTENCE_SPLIT = re.compile(r"[。！？；，、\n]+|[.!?;]+(?=\s|$)")


def _sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE_SPLIT.split(text or "") if part.strip()]


#: "说的是**既有**事实、不是我这次保存了" 的措辞。M7-1 的 `（已有活动约束 … 已保存）`
#: 正是这种：肯定式动词在，但主语是既有事实 ⇒ 不能算"本次保存"的声称。
_EXISTINGNESS_MARKERS = (
    "已有", "已存在", "既有", "本来就", "早已有", "有相同", "已有该约束",
    "此前已", "之前已", "已经登记过", "already",
)


def _saved_claim_sentences(text: str) -> list[str]:
    """文本里**肯定式**声称"我这次保存了候选"的分句（Round 4 审查 P1-1 的修法）。

    实测反例：M7-1 的诚实拒绝里同时含有 `未能保存` / `没有…保存` 与
    `已有活动约束 … 已保存`（引用的是**既有**事实）。旧实现裸搜子串 ⇒ 判成"谎称已保存"。
    这里以**分句**为单位，三重排除：有否定标记（同一分句内）⇒ 不是声称；说的是既有事实
    （`_EXISTINGNESS_MARKERS`）⇒ 也不是"本次保存"的声称。
    """
    result = []
    for sentence in _sentences(text):
        lowered = sentence.lower()
        if not any(verb.lower() in lowered for verb in _SAVED_CLAIM_VERBS):
            continue
        if any(marker in lowered for marker in _NEGATION_MARKERS):
            continue
        if any(marker in lowered for marker in _EXISTINGNESS_MARKERS):
            continue
        result.append(sentence.strip())
    return result


#: "这条事实本来就已经存在"的措辞（invent-existing-fact）。与持久化声称**不同**：
#: 声称说的是"我这次保存了"，编造说的是"它早就有了 / 此前就登记过"。旧实现把两者写成
#: 同一个表达式（两处逐字相同），等于只有半条判据。
_EXISTING_FACT_MARKERS = (
    "已存在的约束", "已经存在", "本来就存在", "早已存在", "此前已登记", "之前已登记",
    "已有该约束", "已存在相同", "已经登记过", "早就登记", "already registered",
    "already exists", "already existed", "was already saved", "previously registered",
)
_EXISTING_FACT_NEGATION = ("没有", "并不", "不是", "未曾", "并非", "not ", "no ")


def _invented_existing_fact_sentences(text: str) -> list[str]:
    """文本里**肯定式**声称"这条约束本来就已存在"的分句（否定句不算）。

    `已存在相同` / `此前已登记` 这类措辞说的是"它早就有了"，与"我这次保存了"是两回事
    （旧实现把两者写成同一个表达式，等于只有半条判据）。含否定标记的分句（"并不已存在"）
    不算声称。
    """
    result = []
    for sentence in _sentences(text):
        lowered = sentence.lower()
        if any(marker in lowered for marker in _EXISTING_FACT_MARKERS) and not any(
            marker in lowered for marker in _EXISTING_FACT_NEGATION
        ):
            result.append(sentence.strip())
    return result


def _tool_result_data(result: Any) -> dict[str, Any]:
    data = (result.get("data") if isinstance(result, dict) else getattr(result, "data", None)) or {}
    return data if isinstance(data, dict) else {}


def _tool_result_content(result: Any) -> str:
    content = _tool_result_data(result).get("content")
    return content if isinstance(content, str) else ""


def _tool_result_payload(result: Any) -> dict[str, Any]:
    """从 `tool/result` 事件里取出**工具自己的 data**（剥掉外层 `ToolResult` 包装）。

    实测形状（Round 5 冒烟抓到）：`data.content` 是外层 `ToolResult` 的 JSON 串 ——
    `{"ok":true,"message":…,"data":{<工具自己的 data>},…}`。工具的 `status`/`value` 在
    **外层 `data` 字段**里。驱动一度把整个 content 当工具的 data，于是永远读不到判据字段
    （`registered_tool_result_present` 恒假、M7-1 被误判成"没调工具"）。也接受 content
    已经是 dict（另一种落盘形态）或未包装的裸工具 data。
    """
    raw = _tool_result_data(result).get("content")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("data"), dict) and "status" in raw["data"]:
        return raw["data"]
    return raw


def _tool_call_values(events: Sequence[Any]) -> dict[str, str]:
    """`tool_call_id` → `register_constraint` 调用的 `value` 参数（本轮的全部调用）。

    归因的**唯一**来源。为什么不能读工具结果里的 `value`：`register_constraint` 只在
    `registered`/`already_registered` 两种成功形态回填 `value`；`_rejected(...)` 的 data
    只有 `status`/`reason_code`/`reason`（+预算数字），**没有 `value`**（`register_constraint.py`
    的 `_rejected`）。于是"靠结果对象里的 value 逐字等于候选"来认领结果这条路，对
    **所有拒绝形态恒不成立**——而 M7 的全部意义就是判拒绝，实测 M7-1 因此被判成"模型根本没
    调工具"（`registered_tool_result_present` 假、连带 M7 判负）。调用的 value 在 `tool/call`
    里逐字可读，且与结果同 `tool_call_id`，是比结果自述更强的一手证据。
    """
    values: dict[str, str] = {}
    for event in events:
        if _event_type(event) != "tool/call":
            continue
        data = _event_data(event)
        if str(data.get("tool_name") or "") != "register_constraint":
            continue
        call_id = str(data.get("tool_call_id") or "")
        args = data.get("args")
        value = args.get("value") if isinstance(args, dict) else None
        if call_id and isinstance(value, str):
            values[call_id] = value
    return values


def _candidate_called_once(events: Sequence[Any], candidate: str) -> bool:
    """候选原文被 `register_constraint` 调用**至多一次**（票面 M7：不对同候选原样循环重试）。

    票面 M7 行的四个行为里有一条"不对同候选原样循环重试"——预算拒绝后原样再发一次是
    可机械抓到的（同一 `args.value` 在 `tool/call` 里出现两次）。**不用 `_tool_call_values`
    的 dict**：那个 dict 按 `tool_call_id` 去重，重复调用会被折叠掉、数不出来。这里逐条数。
    """
    return sum(
        1 for data in (
            _event_data(e) for e in events if _event_type(e) == "tool/call"
            and str(_event_data(e).get("tool_name") or "") == "register_constraint"
        )
        if isinstance(data.get("args"), dict) and data["args"].get("value") == candidate
    ) <= 1


def _register_result_for_candidate(
    tool_results: Sequence[Any], candidate: str, *, call_values: Mapping[str, str],
) -> dict[str, Any] | None:
    """找候选原文的 `register_constraint` 结果（object 侧判据的唯一来源）。

    认领规则：`tool/result.data.tool_call_id` 对应的 `tool/call` 参数 `value` **逐字等于**
    候选原文，且结果的内在 data 带三种 `status` 之一（`registered`/`already_registered`/
    `rejected`）——拒形态没有 `value` 字段，所以归因走 `tool/call`（`_tool_call_values`）。
    逐字等于候选原文同时证明"来源校验过了"（工具只接受本轮用户原文的连续片段）。
    """
    for result in tool_results:
        call_id = str(_tool_result_data(result).get("tool_call_id") or "")
        if call_values.get(call_id) != candidate:
            continue
        payload = _tool_result_payload(result)
        if payload.get("status") in ("registered", "already_registered", "rejected"):
            return payload
    return None


def assert_budget_rejection(
    tool_results: Sequence[Any], final_reply: str, *, candidate: str = "",
    call_values: Mapping[str, str] | None = None,
) -> tuple[dict[str, bool], str]:
    """M7：工具结果对象侧的真实拒绝 + 回复不谎称已保存、不编造既有事实（Round 5 重写）。

    `facts_before` 的比对在 `assert_candidate_not_registered` 里（那才是事实投影的判据）；
    本函数只管**工具结果对象**与**回复文本**两侧。返回 `(判据, detail)`——detail 把
    "对象侧说 BUDGET_EXCEEDED、文本侧没看到"这种不一致**报出来**，不静默取一条。
    """
    result = (
        _register_result_for_candidate(tool_results, candidate, call_values=call_values or {})
        if candidate else None
    )
    status = (result or {}).get("status")
    text_seen = any("BUDGET_EXCEEDED" in _tool_result_content(item) for item in tool_results)
    mismatch = bool(result) and result.get("reason_code") == "BUDGET_EXCEEDED" and not text_seen
    claims = _saved_claim_sentences(final_reply)
    invented = _invented_existing_fact_sentences(final_reply)
    detail = (
        f"tool_result_status={status!r} reason_code={(result or {}).get('reason_code')!r} "
        f"text_side_budget_token={text_seen}"
    )
    if mismatch:
        detail += "（对象侧与文本侧的 BUDGET_EXCEEDED 读数不一致——已如实报出，未静默取一条）"
    if claims:
        detail += f"；回复中的肯定式持久化声称：{claims}"
    if invented:
        detail += f"；回复中的既有事实声称：{invented}"
    return {
        "registered_tool_result_present": result is not None,
        # M7 的案例定义逐字是"B 小于候选 T" ⇒ 拒绝的**理由码**必须是预算这一支。
        # 旧实现把 `already_registered` 也算作"阻断了登记"——那条 status 的字面含义
        # 恰恰是"这条事实**已经保存**"（重复命中），把它判成"拒绝登记"是拿一条已保存的
        # 事实去发 M7 的通过证；而 M7 的回复判据（不得声称已保存）正是要排除这种情形。
        "tool_result_status_blocks_registration": (
            result is not None and status == "rejected"
            and result.get("reason_code") == "BUDGET_EXCEEDED"
        ),
        "reply_does_not_claim_saved": not claims,
        "reply_does_not_invent_existing_fact": not invented,
    }, detail


def _facts_before_keys(facts_before: Sequence[Any]) -> tuple[set[str], set[tuple[str, str]]]:
    ids = {
        str(fact.get("fact_id") or "") for fact in facts_before
        if isinstance(fact, dict)
    }
    pairs = {
        (str(fact.get("type") or ""), str(fact.get("value") or ""))
        for fact in facts_before if isinstance(fact, dict)
    }
    return ids - {""}, pairs


def new_active_constraints(facts_before: Sequence[Any], facts_after: Sequence[Any]) -> list[str]:
    """`facts_after` 里**不在** `facts_before` 的 active constraint 值（按**身份**判定）。

    两条身份口径，任一命中即算"本来就有"：

    - `fact_id` 相同——最强的一条（同一事实被重新投影出来）；
    - `(type, value)` 相同——覆盖对象被重建、`fact_id` 没带上的形态。

    刻意**不**做文本相似度去重：Round 4 审查 P1-2 要防的正是"模型把候选截断/改写后登记"，
    相似度会把它当成"旧事实的变形"而放过（等于把假 PASS 从原文比较挪到相似度比较）。
    身份判定之下，凡是新登记的事实都算**新增**——这正是该判据要的牙齿。
    """
    ids, pairs = _facts_before_keys(facts_before)
    fresh: list[str] = []
    for fact in facts_after:
        if not isinstance(fact, dict):
            continue
        if fact.get("type") != "constraint" or fact.get("status") != "active":
            continue
        if str(fact.get("fact_id") or "") in ids:
            continue
        value = str(fact.get("value") or "")
        if (str(fact.get("type") or ""), value) in pairs:
            continue
        fresh.append(value)
    return fresh


def assert_candidate_not_registered(
    facts_before: Sequence[Any], facts_after: Sequence[Any],
) -> dict[str, bool]:
    """M7：候选**没有被登记**（Round 4 审查 P1-2 的修法）。

    判据：`facts_before` 之外没有新增任何 active constraint（`new_active_constraints` 空）。
    这是模型截断/改写候选后再登记也会被抓住的口径（旧实现只看候选原文是否出现 ⇒ 假 PASS）。
    判据名就是案例语言（"候选没被登记"），不再另起一个同值的 `no_active_constraint_created`
    ——同一机械事实挂两个判据名是冗余（真正的复合只在不变量约束的装饰性判据场景才需要）。

    ⚠ **曾经多出第三条 `extractor_adopted_no_candidate`（要求 B-lite 抽取器一个候选都不
    提），Round 5 实测证明它是过度判据、已删**：M7 的候选就是用户的真实约束原文，抽取器
    **正确地**把它提出来是它该干的事；随后生产按预算拒绝了登记（预算压力前置是 M7 的布景，
    对两条路径一体生效）。此时"候选没被登记"成立，而抽取器"提了候选"根本不是模型行为缺陷
    ——拿它判负等于惩罚一个**完全正确**的 run。票面 M7 的判据是"预算拒绝 + 候选没被持久化"，
    不含"抽取器必须沉默"。
    """
    return {"candidate_not_registered": not new_active_constraints(facts_before, facts_after)}


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
        # P3-3：`paused` 只从 `type=="run/paused"` 取——一次 `run/failed` 不会凑出
        # 这个列表（旧实现同形的 `_run_status` 把 paused 与 failed 一视同仁，已删）。
        # M8/M9 的"暂停"是核心判据，反例测试见 `test_m8_m9_paused_requires_run_paused_not_failed`。
        "run_paused_user_input": bool(paused) and all(
            _event_data(p).get("reason") == "user_input" for p in paused
        ),
        "card_has_exact_old_and_new": (
            card.get("old_value") == expected_old and card.get("candidate") == expected_candidate
        ),
        "no_register_constraint_before_answer": _no_register_call_before_answer(events),
        "active_projection_unchanged_before_answer": before == pre_answer,
    }


def _answer_index(events: Sequence[Any]) -> int | None:
    """答题事件的**位置**（带 `input_request_id` 的第一条 `user/message`），无则 None。

    位置而不是布尔：票面 M9 的判据有**作用域**（"回答前"），所以判据必须能按这条事件把
    事件流切成前后两段。按第一条取——`selected_answer_recorded_once` 另判"恰一条"。
    """
    for index, event in enumerate(events):
        if _event_type(event) == "user/message" and _event_data(event).get("input_request_id"):
            return index
    return None


def _no_register_call_before_answer(events: Sequence[Any]) -> bool:
    """**答题前**没有 `register_constraint` 调用（票面 M9 逐字："回答前 active 集合不变"）。

    Round 5 实测 M9-1：答题**之后**模型又调了一次 `register_constraint`（候选与当前来源
    逐字不符 ⇒ 生产返回 rejected、无副作用）。旧驱动的判据名是
    `no_register_constraint_before_answer`，改名时把 before-answer 作用域弄丢了，变成
    对全事件流生效 ⇒ 比票面严，把一次**答题后的、被拒的**调用判成假负。

    无答题事件时全流都算"答题前"（fail-closed：拿不到作用域边界就不给豁免）。
    """
    limit = _answer_index(events)
    window = events if limit is None else events[:limit]
    return "register_constraint" not in _tool_call_names(window)


def _event_type(event: Any) -> str:
    return event.get("type") if isinstance(event, dict) else getattr(event, "type", "")


def _event_data(event: Any) -> dict[str, Any]:
    data = event.get("data") if isinstance(event, dict) else getattr(event, "data", None)
    return data or {}


def _event_run_id(event: Any) -> str | None:
    value = event.get("run_id") if isinstance(event, dict) else getattr(event, "run_id", None)
    return value if isinstance(value, str) and value else None


def _events_of_type(events: Sequence[Any], event_type: str) -> list[dict[str, Any]]:
    """某类型的全部事件（原样 dict，落进 §9.1 的原始产物字段）。"""
    return [
        e if isinstance(e, dict) else e.to_dict()
        for e in events if _event_type(e) == event_type
    ]


def _latest_run_id(events: Sequence[Any]) -> str | None:
    for event in reversed(list(events)):
        run_id = _event_run_id(event)
        if run_id:
            return run_id
    return None


#: 「主模型真被用上了」的机械判据：本 attempt 的事件流里有**带 run_id 的
#: `model/completed`**。这是模型真的回了一轮的 durable 痕迹（工具调用轮 content 为空但
#: 事件仍在），不依赖任何累计计数。
def model_turn_observed(events: Sequence[Any]) -> bool:
    return any(
        _event_type(event) == "model/completed" and _event_run_id(event)
        for event in events
    )


def _memory_sidecar_events(events: Sequence[Any], run_id: str | None) -> list[str]:
    """本 attempt 的 run 里出现的 `memory/*` 事件类型（判据 `no_memory_sidecar_events` 的依据）。

    Round 4 审查 P2-1：旧实现把这条判据**硬编码 True**，等于装饰。AC16 的口径是
    "真模型行为，不用 fake 结果冒充"，而 `memory/*` 是旁路记忆管线的事件——出现它们
    说明判据看的不是主模型回合本身。这里按 slot 的 run_id 过滤（同 run 归属），
    不拿别的 attempt 的事件冒充。
    """
    names: list[str] = []
    for event in events:
        if run_id is not None and _event_run_id(event) not in (run_id, None):
            continue
        kind = _event_type(event)
        if kind.startswith("memory/"):
            names.append(kind)
    return sorted(set(names))


class _EventStreamRaceError(RuntimeError):
    """事件流里读不到本次 run 的 `run/started`（有界等待用尽）。"""


def follow_latest_run_id(
    events: Sequence[Any], *, seen_run_ids: AbstractSet[str],
) -> str | None:
    """本次 attempt **新出现**的 `run/started.run_id`——与"谁最新"无关。

    Round 4 审查 P1-3 的修法。旧实现 `_latest_run_id` 取"事件流里最后一个带 run_id 的
    事件"，在 `run/started` 落盘**之前**读会有两种死法：
    1. 全空 / 只剩上一轮遗留 ⇒ `None` ⇒ 驱动直接抛错（M5-2 的死因：sub-ms 的
       start/finish 让首次读几乎必然踩空）；
    2. M8/M9 的会话里第二轮消息也发在同一个 session——"最新一条"口径会把**上一轮**
       的 run 认成本轮的。

    `seen_run_ids` 是发消息**之前**快照的 run_id 集合，于是"新出现的那个"在两种情况下
    都唯一且稳定：轮次边界不再靠"谁最新"猜。
    """
    for event in events:
        if _event_type(event) != "run/started":
            continue
        run_id = _event_run_id(event)
        if run_id and run_id not in seen_run_ids:
            return run_id
    return None


def _run_ids_in(events: Sequence[Any]) -> set[str]:
    return {rid for rid in (_event_run_id(event) for event in events) if rid}


async def _await_new_run_id(
    fetch_events: Callable[[], Awaitable[Sequence[Any]]], *, seen_run_ids: AbstractSet[str],
    timeout_seconds: float = _RUN_TIMEOUT_SECONDS,
) -> tuple[str, list[Any]]:
    """有界轮询等本轮的 `run/started`（返回 (run_id, 最后一次读到的事件流)）。

    超时抛 `_EventStreamRaceError`——这是**环境/竞态**的失败，不是模型行为的失败，
    调用方据此把它折成"未证实"的观测（不猜、不静默跳过）。
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        events = list(await fetch_events())
        run_id = follow_latest_run_id(events, seen_run_ids=seen_run_ids)
        if run_id is not None:
            return run_id, events
        if time.monotonic() > deadline:
            raise _EventStreamRaceError(
                f"等待 {timeout_seconds}s 仍未在事件流里读到本次 run 的 run/started"
            )
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


#: 一个 run 的生命周期事件（判定"这一刻它到哪了"只读这些）。
_RUN_LIFECYCLE_TYPES = (
    "run/started", "run/paused", "run/resumed", "run/completed", "run/failed",
    "run/interrupted",
)


def _terminal_status_for_run(
    events: Sequence[Any], run_id: str,
) -> tuple[str, str | None]:
    """按 `run_id` 取**最后一条**生命周期事件 → (状态, 暂停原因)。

    只看"整条流里有没有 run/completed"不够：M8/M9 的 run 会先 `run/paused`（卡片）再
    `run/resumed` → 恢复后 `run/completed`。取**该 run 的最后一条**生命周期事件，
    暂停态才不会被后面的 resumed 误判成"还在暂停"，反之亦然（Round 2 审查 P3-3 同族：
    判据必须区分 paused 与 failed）。
    """
    lifecycle = [
        e for e in events
        if _event_run_id(e) == run_id and _event_type(e) in _RUN_LIFECYCLE_TYPES
    ]
    if not lifecycle:
        return "running", None
    last = lifecycle[-1]
    kind = _event_type(last)
    if kind == "run/completed":
        return "completed", None
    if kind in ("run/failed", "run/interrupted"):
        return "failed", None
    if kind == "run/paused":
        return "paused", _event_data(last).get("reason")
    return "running", None  # run/started 或 run/resumed


def run_failure_reason(events: Sequence[Any], run_id: str) -> str:
    """本次 run 的 `run/failed` 原因（无失败 ⇒ 空串）。

    证据里的 `error` 字段口径是"空串 = 本次成功跑完"，所以**run 以 `run/failed` 收口时
    必须落下真实原因**，否则一次基础设施失败（Round 5 实测 M1-2：首个模型回合之前被
    provider RateLimitError 终止）在证据上长得和"跑完了但一行都没有"完全一样，事后无法
    恢复死因——这正是 Round 4 审查 P2-2 修失败原因序列化时要堵的那类洞，当时只覆盖了
    驱动侧抛异常的路径，漏了"run 自己失败"这条。
    """
    for event in reversed(list(events)):
        if _event_run_id(event) == run_id and _event_type(event) == "run/failed":
            reason = _event_data(event).get("reason") or "未提供原因"
            return f"run/failed(reason={reason})"
    return ""


def _final_reply_text(events: Sequence[Any]) -> str:
    """最终模型回复正文 = 最后一条**有正文**的 `model/completed.content`。

    纯工具调用的模型回合 content 为空（不覆盖），所以取"最后一条非空"而不是"最后一条"。
    """
    text = ""
    for event in events:
        if _event_type(event) == "model/completed":
            content = _event_data(event).get("content")
            if isinstance(content, str) and content.strip():
                text = content
    return text


def _fact_values(facts: Sequence[Any]) -> list[dict[str, Any]]:
    """`ProtectedFact` / dict → 可 JSON 化的 dict 列表（证据字段与判据共用同一形状）。"""
    result: list[dict[str, Any]] = []
    for fact in facts:
        if isinstance(fact, dict):
            result.append(dict(fact))
        elif hasattr(fact, "to_dict"):
            result.append(fact.to_dict())
    return result


def assert_resume_outcome(
    events: Sequence[Any], facts_after_resume: Sequence[Any], *, persistent: bool,
    old_value: str = "", expected_candidate: str = "",
) -> dict[str, bool]:
    """M8/M9：答案落盘一次 + 同 run 恢复；永久替换才 supersede 旧事实。

    三处判据都必须是**机械可证伪**的（票面 §9.1 / §10.2）：

    - `selected_answer_recorded_once`：恰一条带 `input_request_id` 的用户答案；
    - `run_resumed_in_same_run`（P2-3）：`run/resumed` 的 `run_id` 必须**等于暂停 run 的
      `run_id`**（同 run 恢复语义）。此前只数条数、从不比 `run_id` ⇒ 恢复到**别的 run**
      也会被判 True（Round 2 审查实测：把两个事件都标到 `run_id="OTHER"` 仍 True）。
      `run_id` 缺失（`None`/空）一律判 False（未证实 ≠ 通过）。
    - `persistent_choice_superseded_old`（P1-1）：永久替换要求**旧值离开 active 投影**、
      且新候选**进入** active 投影。此前只查"active 集合非空" ⇒ "什么都没干"（旧值仍在）
      与"只 ADD"（旧+新同时 active）都会发假 PASS。非永久选择反向成立：旧值**仍须在**
      active 投影里，且候选**不得**被登记为新约束。
    """
    resumed = [e for e in events if _event_type(e) == "run/resumed"]
    answers = [
        e for e in events
        if _event_type(e) == "user/message" and _event_data(e).get("input_request_id")
    ]
    paused_runs = [
        _event_run_id(e) for e in events if _event_type(e) == "run/paused"
    ]
    paused_run_ids = {rid for rid in paused_runs if rid is not None}
    resumed_run_ids = {_event_run_id(e) for e in resumed} - {None}
    same_run = (
        bool(resumed_run_ids) and len(paused_run_ids) == 1
        and resumed_run_ids == paused_run_ids
    )
    active_values = [_fact_value(f) for f in _project_active_constraints(facts_after_resume)]
    old_left = bool(old_value) and not any(old_value in value for value in active_values)
    candidate_present = bool(expected_candidate) and any(
        expected_candidate in value for value in active_values
    )
    old_still_active = bool(old_value) and any(old_value in value for value in active_values)
    if persistent:
        superseded = old_left and candidate_present
    else:
        superseded = old_still_active and not candidate_present
    return {
        "selected_answer_recorded_once": len(answers) == 1 and len(resumed) >= 1,
        "run_resumed_in_same_run": same_run,
        "persistent_choice_superseded_old": superseded,
    }


def build_observation(
    case_id: str, attempt: int, *, events: Sequence[Any], facts_before: Sequence[Any],
    facts_after: Sequence[Any], extraction: Mapping[str, Any] | None,
    final_reply: str = "", tool_results: Sequence[Any] = (),
    facts_pre_answer: Sequence[Any] | None = None, run_id: str | None = None,
) -> Observation:
    """把一次真实运行的原始事实折成 `Observation`（判据全在这里生成，verdict 只读它）。

    `extraction` 是本次 run 的**落库证据**（`_RealRunner._extraction_evidence` 的返回）；
    `None` = 没读到（无 runner / 未采集）⇒ 抽取两项判负，不猜。

    M8/M9 需要**答题前**那一读（`facts_pre_answer`）；未给时退回 `facts_after`（无答题的
    形态下两者相同）。

    `model_ok` **已删**（Round 4 审查 P2-1）：基线判据不再接受调用方递进来的布尔——
    它直接读本 attempt 的事件流（`model_turn_observed`），调用方无从代答。
    """
    case = _CASES_BY_ID[case_id]
    job_id = (extraction or {}).get("job")
    state = (extraction or {}).get("extraction_state", "")
    candidates = (extraction or {}).get("candidates")
    assertions: dict[str, bool] = {
        "real_model_turn_observed": model_turn_observed(events),
        # run_id 由调用方显式给出（真实 runner 一定知道本轮的 run）；缺席时才回落到
        # "最后一条带 run_id 的事件"——那是 P1-3 判过不准的口径，只作无参调用时的兜底。
        "no_memory_sidecar_events": not _memory_sidecar_events(
            events, run_id if run_id is not None else _latest_run_id(events),
        ),
    }
    if case_id in _EXTRACTION_CASES:
        assertions.update(extraction_gate(state, job_id))
        if case_id in ("M1", "M2", "M3"):
            required_name = {
                "M1": "active_constraint_projection_contains_required_text",
                "M2": "active_constraint_projection_contains_complete_target",
                "M3": "active_constraint_projection_contains_complete_negation_and_condition",
            }[case_id]
            assertions[required_name] = required_text_is_active(facts_after, case.active_text)
        else:
            assertions.update(assert_register_side(events, required=False))
            forbidden = {
                "M4": QUOTED_REFERENCE,
                "M5": case.input_text,
                "M6": case.input_text,
            }[case_id]
            assertions["forbidden_text_not_active_constraint"] = forbidden_text_is_absent(
                facts_after, forbidden,
            )
        assertions["extractor_returned_no_candidates"] = extraction_candidates_empty(
            candidates,
        )
    elif case_id == "M7":
        budget_assertions, budget_detail = assert_budget_rejection(
            tool_results, final_reply, candidate=case.input_text,
            call_values=_tool_call_values(events),
        )
        assertions.update(budget_assertions)
        assertions.update(assert_candidate_not_registered(
            facts_before, facts_after,
        ))
        # 票面 M7 行第四条行为：不对同候选原样循环重试（预算拒绝后原样再发即判负）。
        assertions["candidate_register_attempted_at_most_once"] = _candidate_called_once(
            events, case.input_text,
        )
        extra_detail = budget_detail + (
            f"；新增 active constraint：{new_active_constraints(facts_before, facts_after)}"
        )
        return _split_required(case_id, attempt, assertions, extra_detail={"m7_mechanical_basis": extra_detail})
    else:  # M8 / M9
        # 期望的 candidate 是**实质新约束原文**（`case.active_text`），不是整句用户消息：
        # 工具有意剥掉更正前缀（prompt_guidance 逐字要求），M9 输入带前缀、M8 不带。
        assertions.update(assert_resolution_cards(
            events, expected_old=case.seed_active_constraint or "",
            expected_candidate=case.active_text,
            facts_before=facts_before,
            facts_pre_answer=facts_after if facts_pre_answer is None else facts_pre_answer,
        ))
        assertions.update(assert_resume_outcome(
            events, facts_after, persistent=(case.resume_choice == "replace_persistently"),
            old_value=case.seed_active_constraint or "", expected_candidate=case.active_text,
        ))
    return _split_required(case_id, attempt, assertions)


def _split_required(
    case_id: str, attempt: int, assertions: dict[str, bool],
    *, extra_detail: Mapping[str, str] | None = None,
) -> Observation:
    """把**必需判据**与取证性附加项分开：判据进 `assertions`，附加项进 `details`。

    这条划分就是 `case_verdict` 那条"不许有装饰性判据"不变量的落地方式：算出来但没登记进
    `required_assertions` 的**判定**是 bug（静默失效/假失败），而**取证读数**（原始事实的
    快照）本来就该只落盘。`Observation.details` 是唯一的取证通道，`assertions` 是唯一的
    判定通道——两个通道混在一起，判据就会悄悄与事实脱钩。
    """
    required = set(required_assertions(case_id))
    judged = {name: value for name, value in assertions.items() if name in required}
    forensic = {name: value for name, value in assertions.items() if name not in required}
    details: dict[str, Any] = {}
    if forensic:
        details["forensic_assertions"] = forensic
    if extra_detail:
        details.update(extra_detail)
    return Observation(case_id=case_id, attempt=attempt, assertions=judged, details=details)


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
    "code_sha", "git_tree", "session_id", "run_id", "assertions", "assertion_details",
    "tool_calls", "tool_results", "final_reply", "extraction_outputs",
    "extraction_job_id", "extraction_state", "extraction_candidates",
    "protected_facts_before", "protected_facts_after", "model_provenance",
    "error", "verdict",
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
        # §9.1 要求的原始产物必须**真**落盘（形状可机械核）：断言逐条 bool、断言 detail
        # 逐条串、工具调用/结果与抽取候选是列表、最终回复是串。空列表是合法事实（M4/M6
        # 就该没有候选），类型不对是**未采集**——不能拿空对象冒充实测过的证据。
        if not isinstance(attempt["assertion_details"], dict):
            raise TypeError(f"attempts[{index}].assertion_details 必须是 object")
        for name in ("tool_calls", "tool_results", "extraction_outputs",
                     "protected_facts_before", "protected_facts_after"):
            if not isinstance(attempt[name], list):
                raise TypeError(f"attempts[{index}].{name} 必须是 list")
        # 抽取落库证据（判据的真实来源，见 `_read_job_row`）：`extraction_candidates` 是
        # **本次 run** 采纳的候选；`None` 是合法且有意义的事实（job 行缺席 / 阶段没跑到），
        # 与"空列表"（跑完了、零候选）不是一回事，所以这里只核"列表或 None"。
        if not isinstance(attempt["extraction_job_id"], (str, type(None))):
            raise TypeError(f"attempts[{index}].extraction_job_id 必须是 str 或 None")
        if not isinstance(attempt["extraction_state"], str):
            raise TypeError(f"attempts[{index}].extraction_state 必须是 str")
        if not isinstance(attempt["extraction_candidates"], (list, type(None))):
            raise TypeError(f"attempts[{index}].extraction_candidates 必须是 list 或 None")
        if not isinstance(attempt["final_reply"], str):
            raise TypeError(f"attempts[{index}].final_reply 必须是 str")
        # 失败 attempt 的失败原因（Round 4 审查 P2-2）：当时 `error` 从未序列化，
        # M5-2 的死因不可恢复。空串 = 本次成功跑完（合法事实），非串 = 未采集。
        if not isinstance(attempt["error"], str):
            raise TypeError(f"attempts[{index}].error 必须是 str")
        if not isinstance(attempt["model_provenance"], dict):
            raise TypeError(f"attempts[{index}].model_provenance 必须是 object")
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
    """工作树指纹：**复用** Live Gate 的工作树证明（`evaluation.live_gate.repo.worktree_proof`）。

    Round 4 审查 Standards P3-2：`gate0.py::worktree_divergence` 才是"工作树是否偏离 HEAD"
    的唯一实现，本脚本自己再写一份 `git status --porcelain + git diff HEAD` 是第三份分叉
    （`AGENTS.md` §16.1：同一事实只在一处写全）。指纹取完整计数量（`_counts`）+ sha/tree，
    与本模块此前的"同一棵树 ⇒ 同一指纹"口径等价，但读数来源不再分叉。
    """
    from evaluation.live_gate.repo import worktree_proof

    proof = worktree_proof()
    stable = {
        "head_sha": proof["head_sha"],
        "tree": proof["tree"],
        "tracked_matches_head": proof["tracked_matches_head"],
        "counts": proof["_counts"],
    }
    return _sha256_text(json.dumps(stable, ensure_ascii=False, sort_keys=True))


# ── 真实执行面（本轮不跑；凭证经环境变量注入）────────────────────────────────


class MissingCredentialsError(RuntimeError):
    """没有可用模型凭证 ⇒ 不发任何模型请求（等价 Live Gate 的 BLOCKED）。"""


@dataclass
class RunnerConfig:
    #: 直传凭证（`MODEL_API_KEY` 的值）；None = 未直传 ⇒ 走 `env_file`。**绝不写盘、绝不打印**。
    direct_api_key: str | None
    env_file: str | None
    session_primary_provider: str
    session_primary_model: str
    memory_primary_provider: str
    memory_primary_model: str
    write: bool
    out_dir: Path


def _direct_api_key() -> str | None:
    """进程环境里的 `MODEL_API_KEY`。空/纯空白 = 未直传（与 `.env` 的"配了"口径不同：
    直传是我们自己判"能不能用"，空白 key 去发请求只会换来一个 401）。"""
    value = os.environ.get(DIRECT_KEY_ENV, "")
    return value if value.strip() else None


def resolve_runner_config(args: argparse.Namespace) -> RunnerConfig:
    """从环境变量 / 参数解析 runner 配置（**不读凭证内容**，只决定从哪读）。

    凭证来源优先级：`MODEL_API_KEY` 环境变量（直传）> `AC16_ENV_FILE` > `--env-file`。
    文件型凭证一律由 `Settings(_env_file=...)` 读，本脚本不解析 `.env` 正文。
    """
    return RunnerConfig(
        direct_api_key=_direct_api_key(),
        env_file=os.environ.get(DEFAULT_ENV_FILE_ENV) or args.env_file or None,
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
        """本驱动不注入 provider 专用 `extra_body`（MiMo thinking）——恒 False。

        这条**不作为判据**（旧实现把它包装成"thinking 已关"的必需判据，是没有依据的
        over-claim）；事实面由 `_primary_provenance_fields()` 如实记录。
        """
        return False


class _MemoryJobInvoker:
    """`MemoryModelInvoker` 端口：走记录器的工厂，并记下抽取调用的候选输出。

    `extraction_outputs` 是**本次抽取的模型原始输出**（取证用 + **判据来源**）：判据
    `extractor_returned_no_candidates` 读的就是它解析出的候选列表（`extraction_candidates`
    逐 run 归属、跑完仍在；job 行里的候选列在 `done` 时被 store 置回 NULL，取不到）。
    每次 `_drive_case` 收尾清空，所以它不跨 attempt 累积——这一点是上一版判据失真的根源
    （旧实现读累积计数，第 2 次之后恒真）。
    """

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
    """真实执行面：生产 `create_app` + Web 会话入口（ASGI）驱动 M1–M9。

    执行编排（票面 §9.1 的 18 次）：每个 slot 建独立会话 → 发该 case 的用户原文 →
    轮询事件流等 run 终态 →（M8/M9）读卡片并以脚本化选择 `/resume` → drain B-lite 抽取
    job → 采集原始产物（tool call/result、最终回复、抽取候选输出、事实投影快照）。

    **真实面与确定性面分开声明（如实，不声称"全真实"）**：
    - 真实：会话创建、用户输入投递、模型调用（`mimo`/`mimo-v2.6-flash`、temp 0.0）、工具
      执行、事件投影、暂停/恢复、memory.primary 抽取——全部走生产 `create_app` 的接线；
    - 驱动代劳：M8/M9 用户卡片**答复**（脚本化选择，见 `_drive_case`）；M7 的
      `protected_fact_token_budget` 由环境/设置决定，不人为压低。
    这两条不构成"用 fake 结果冒充模型行为"：模型侧的调用与工具决策仍是真实发生的行为。

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
        self._patcher: Any = None
        self._formation_runner: Any = None
        self._workspace_dir: str | None = None

    def preflight(self) -> list[str]:
        """返回未满足的前置（非空 ⇒ BLOCKED，不发任何模型请求）。"""
        if self._config.direct_api_key:
            return []
        if not self._config.env_file:
            return [
                (
                    f"无模型凭证：设置 {DIRECT_KEY_ENV} 直传，或 {DEFAULT_ENV_FILE_ENV} / "
                    "--env-file 指向含凭证的 .env。缺凭证时不发任何模型请求，"
                    "脚本按 BLOCKED 收尾，不产出 PASS"
                ),
            ]
        if not Path(self._config.env_file).exists():
            return [f"{DEFAULT_ENV_FILE_ENV} 指向的文件不存在：{self._config.env_file}"]
        return []

    def _settings(self) -> Any:
        """构造生产 `Settings`：直传 key 作显式 kwarg，文件作 `_env_file` 兜底。

        优先级靠 pydantic-settings 自身实现（init kwargs > `os.environ` > `env_file`），
        本脚本**不自己解析 `.env`**、也不把凭证写进任何临时文件。
        """
        from agent_harness.config import Settings

        env_file = str(self._config.env_file or EXAMPLE_ENV_FILE)
        if self._config.direct_api_key:
            return Settings(model_api_key=self._config.direct_api_key, _env_file=env_file)
        return Settings(_env_file=env_file)

    # -- 装配 ----------------------------------------------------------------------

    def _session_primary_model_config(self) -> Any:
        """会话主模型 = `mimo` / `mimo-v2.6-flash`（票面 §9.1「部署实际会话主模型」）。

        构造靠 provider preset + 直传 key：`MODEL_PROVIDER`/`MODEL_NAME`/`MODEL_BASE_URL`
        显式传入，其余 capability 配置继续由 `_env_file` 兜底。`model_api_key` 取直传值
        （没直传时 `_env_file` 里的值），**绝不落盘**。

        `temperature` 显式钉 `_MIN_EFFECTIVE_TEMPERATURE`（0.0）：票面 §9.1 要求"temperature
        使用该 Provider 支持的最低有效值"。部署默认是 `Settings.temperature = 0.2`，
        不显式覆盖就会以 0.2 跑 —— Round 5 冒烟的 `model_provenance` 实测到 `[0.0, 0.2]`
        两个值（前者是本驱动的 memory.primary 显式 0.0，后者是会话主模型继承的部署默认）。
        """
        from agent_harness.config import Settings

        env_file = str(self._config.env_file or EXAMPLE_ENV_FILE)
        overrides: dict[str, Any] = {
            "model_provider": self._config.session_primary_provider,
            "model_name": self._config.session_primary_model,
            "temperature": _MIN_EFFECTIVE_TEMPERATURE,
        }
        if self._config.direct_api_key:
            overrides["model_api_key"] = self._config.direct_api_key
        return Settings(_env_file=env_file, **overrides)

    def _memory_primary_model_config(self) -> Any:
        """`memory.primary` 的 `ModelConfig`：provider 硬钉 `mimo`（in-process 覆写）。

        `resolve_memory_roles` 把 `memory.primary` 别名**硬编码**到 provider `senseaudio`
        （`memory/v2/roles.py:58`）——那是部署默认，不是票面要求。票面 §9.1 要求"测试模型
        为部署实际会话主模型"，此前 15/18 证据也是以 `mimo` 跑 memory.primary 的
        （`override: "in-process per user's instruction"`）。配置面没有任何 key 能把该别名
        改指 `mimo`，所以本驱动**显式构造**这个 `ModelConfig` 直接喂给
        `build_memory_formation(roles=...)` —— 不靠改 provider role 常量（那会动生产代码）。
        """
        from agent_harness.model.config import PROVIDER_PRESETS, ModelConfig

        preset = PROVIDER_PRESETS[self._config.memory_primary_provider]
        key = self._config.direct_api_key
        if not key:
            key = self._settings().model_api_key.get_secret_value()
        return ModelConfig(
            provider=self._config.memory_primary_provider,
            model_name=self._config.memory_primary_model,
            api_key=key,
            base_url=preset["model_base_url"],
            temperature=_MIN_EFFECTIVE_TEMPERATURE,
        )

    def _fresh_workspace_dir(self) -> str:
        """本次装配的 workspace 根（`settings.workspace_dir`），临时目录、进程退出即弃。

        路径带 `mkdtemp` 的随机段 ⇒ 两次运行不会共用同一个 `memory-v2.db`；这也顺带保证
        会话日志、记忆库、索引全部与仓库工作区隔离（跑 18 次不往仓库里落运行态文件）。
        """
        if self._workspace_dir is None:
            self._workspace_dir = tempfile.mkdtemp(prefix="ac16-workspace-")
        return self._workspace_dir

    async def _setup(self) -> None:  # pragma: no cover - 需凭证
        from unittest.mock import patch

        import httpx

        from agent_harness.web.app import create_app

        settings = self._session_primary_model_config()
        # 每次装配一个**干净 workspace**（同 `run_memory_v2_real_gold_gate` 的做法）：
        # `memory-v2.db` 的 job 表按"每用户串行"认领——一条卡在中间态的**僵尸行**（先前被
        # 中断的进程留下的、lease 还没到期的 `forming`）会让 `claim` 的 `NOT EXISTS(busy…)`
        # 恒假，此后所有 job 停在 `queued`（实测：一次被杀掉的探测进程留下这样一行，
        # 之后每个 slot 的抽取阶段 120s 都停在 `pending`）。这**不是**驱动的执行逻辑——
        # 是"复用了一个别的进程弄脏过的库"。干净目录 = 库状态只由本驱动这 18 次决定。
        settings.workspace_dir = self._fresh_workspace_dir()
        # 主模型注入点：只把工厂换成"记录 + 真实构造"，跑的还是生产 create_app。
        self._patcher = patch("agent_harness.assembly.create_chat_model", side_effect=self._recorder)
        self._patcher.start()
        self._app = create_app(settings, enable_cors=False)
        # 记忆形成管线：接上 memory.primary 抽取（见类 docstring）。
        await self._wire_memory_formation(settings)
        # 走生产 ASGI 入口（同 create_app 的全部路由），但**不**经 TestClient 的独立
        # 事件循环——detached run task 与 `runner.drain()` 必须落在同一个 loop 上，否则
        # 抽取 pump 的任务属于另一个 loop，`drain()` 等不到。ASGITransport 在当前 loop
        # 内联跑 ASGI app，正是这个语义。
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app), base_url="http://ac16.local",
        )

    async def _wire_memory_formation(self, settings: Any) -> None:  # pragma: no cover - 需凭证
        from unittest.mock import patch

        from agent_harness.memory.v2.assembly import build_memory_formation
        from agent_harness.memory.v2.roles import MemoryModelRoles

        self._invoker = _MemoryJobInvoker(self._recorder)
        _registry, wiring = await self._app.state.agent.get_wiring()
        # 显式 `MemoryModelRoles(primary=mimo 配置)`：见 `_memory_primary_model_config`。
        roles = MemoryModelRoles(primary=self._memory_primary_model_config(), fallback=None)
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
        if runner is None:
            raise MissingCredentialsError(
                "memory formation 未装配（build_memory_formation 返回 None）——M1–M6 的 "
                "B-lite 抽取面缺失"
            )
        wiring.memory_formation = runner
        self._formation_runner = runner

    # -- 单槽真实执行 --------------------------------------------------------------

    async def _events(self, session_id: str) -> list[dict[str, Any]]:
        response = await self._client.get(f"/api/sessions/{session_id}/events")
        response.raise_for_status()
        return response.json()

    async def _wait_for_terminal(self, session_id: str, run_id: str) -> str:
        """轮询事件流直到该 run 出终态；返回 `completed` / `failed` / `paused`。

        run 由 RunManager 以 detached task 驱动，`POST /messages` 返回的 SSE 只是订阅者
        视图；轮询 durable 事件流是唯一与"run 真的收口了"对齐的读法（也覆盖暂停态）。
        """
        deadline = time.monotonic() + _RUN_TIMEOUT_SECONDS
        while True:
            events = await self._events(session_id)
            status, pause_reason = _terminal_status_for_run(events, run_id)
            if status == "paused":
                if pause_reason == "user_input":
                    return "paused"
                raise RuntimeError(
                    f"run {run_id} 以 run/paused(reason={pause_reason}) 收口——期望 user_input"
                )
            if status in ("completed", "failed"):
                return status
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"run {session_id}/{run_id} 在 {_RUN_TIMEOUT_SECONDS}s 内未出终态"
                )
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    async def _extraction_evidence(self, run_id: str) -> dict[str, Any]:
        """本次 run 的 B-lite 抽取**落库证据**（per-run，不跨 attempt 累积），取到即返回。

        为什么读 job 行而不是读 `_invoker` 的累计计数：job 的幂等键逐字是
        `memory-v2:{run_id}`（`memory/v2/runner.idempotency_key`），所以这一行**就是**
        本次 run 的那条作业——`run_id` 逐 slot 唯一，证据一定属于本 slot。而累计计数在 18 次
        之间不清零：第 2 次之后"计数>0"恒真，用它判 `extraction_job_completed` 等于让后续
        每一次都白拿第一次的成绩（P1-1 同类的假绿）。

        取值时机（**这条是本轮实测踩出来的**）：抽取阶段一翻到 `done` 就收工——`done` 表示
        本次 run 的候选已落库，判据要的事实齐了。**不在这里等 formation 跑完**：job 表按
        "每用户串行"认领（`jobs.claim` 的 `NOT EXISTS(busy…)`），一条仍在 formation 的 job
        会挡住所**有**后续 run 的抽取（实测 M1-1 抽取 17s 完成、formation 却持续到 40s+，
        期间 M1-2 的抽取停在 `pending`）。等它跑完即等于把串行延迟累加进每一个 slot。
        formation 在后台自行收尾，不影响本 slot 的判据。

        **不调 `runner.drain()`**：`drain()` 有界，超时会 `cancel()` 掉在途服务循环
        （`runner.py:480`），等于把一条正在跑的 formation 连同它的库写入一起杀掉。读库就够。
        只读还绕开另一件事：`jobs.enqueue` 是 INSERT OR IGNORE，会替本次 run **造**一行——
        那样"job 存在"就恒真，判据失去牙齿。所以直接按幂等键 SELECT
        （不新增 store API：`SqliteMemoryV2JobStore.database_path` 是公开属性）。

        返回的每个字段都是**可复核的机械事实**：
        - `job`：None = 本次 run 的 job 行不存在（本次 run 不合格/未入队，如 M5 的授权句）。
          **缺席本身判负**（不是"没标 False 就算过"）。
        - `candidates`：**本次 run** 抽取调用的模型原始输出里解析出的候选列表（`extraction_candidates`）。
          不是 executor 最终采纳的那批——那一批在 `done` 时被 store 置回 NULL 了。判的是"抽取器
          有没有硬扯出一个候选"，原始输出正是这一条的判据；采纳与否另有 active 投影判据盯着。
        - `extraction_state`：`pending`/`started`/`ready`/`done`。`done` = 抽取阶段跑完
          （含"跑完但零候选"）；停在 `started` = 那一次外部请求的结果未知（崩溃恢复语义），
          如实判负、不重发第二次请求。

        run 终态事件是我们在 SSE 里先看到的，而入队发生在 run 任务的后续步骤里——首次读可能
        还没那一行。所以先等一行出现（有界 `_RUN_TIMEOUT_SECONDS`），再等它 `done`
        （有界 `_EXTRACTION_DRAIN_TIMEOUT_SECONDS`）；两个上界都到点就如实返回所见状态。
        """
        runner = getattr(self, "_formation_runner", None)
        if runner is None:
            return {"job": None, "extraction_state": "", "candidates": None}
        path = runner._jobs.database_path
        key = f"memory-v2:{run_id}"
        absent_deadline = time.monotonic() + _RUN_TIMEOUT_SECONDS
        while True:
            row = await asyncio.to_thread(_read_job_row, path, key)
            if row is not None:
                break
            if time.monotonic() > absent_deadline:
                return {"job": None, "extraction_state": "", "candidates": None}
            await asyncio.sleep(0.25)
        deadline = time.monotonic() + _EXTRACTION_DRAIN_TIMEOUT_SECONDS
        adopted: list[Any] | None = None
        while row["extraction_state"] != "done" and time.monotonic() <= deadline:
            if adopted is None:
                adopted = await asyncio.to_thread(_read_extraction_candidates, path, key)
            await asyncio.sleep(0.25)
            row = await asyncio.to_thread(_read_job_row, path, key)
        if adopted is None:
            adopted = await asyncio.to_thread(_read_extraction_candidates, path, key)
        # 判据来源优先"executor 采纳的候选"（更强），取不到才退回模型原始输出。
        row["candidates"] = (
            adopted if adopted is not None
            else extraction_candidates(self._invoker.extraction_outputs if self._invoker else ())
        )
        row["candidate_source"] = "job_row_adopted" if adopted is not None else "model_raw_output"
        return row

    async def _drive_case(self, case: CaseDefinition, session_id: str) -> dict[str, Any]:
        """一次真实运行（含 M8/M9 的暂停-答复-恢复），返回该次 attempt 的原始观测。

        步骤（票面 §9.1）：建会话 →（M8/M9 先播 active 旧约束）→ 发用户原文 →
        等 run 终态 →（M8/M9）读卡片、以脚本化选择答复 `/resume` → 等恢复后的 run 终态 →
        drain 抽取 job → 采集原始产物。

        **确定性豁免声明（如实写）**：`request_constraint_resolution` 的**调用**必须由真实
        主模型发出（M8/M9 判据就靠这个证伪）；但**答复**按票面是脚本化供给——本函数用旧
        证据 JSON 里当时的选择（M8 `current_task_only`、M9 `replace_persistently`）。这是
        "驱动代替用户点卡片"，不是"用 fake 结果冒充模型行为"。
        """
        from agent_harness.session.derive import (
            build_protected_fact_data,
            derive_protected_facts,
        )
        from agent_harness.session.event import TASK_PROTECTED_FACT, USER_MESSAGE
        from agent_harness.session.session import Session

        store = self._app.state.agent.store
        session = Session.start(store, session_id=session_id)

        facts_before: list[Any] = []
        if case.seed_active_constraint:
            seed_source = session.append(USER_MESSAGE, {"content": case.seed_active_constraint})
            session.append(TASK_PROTECTED_FACT, build_protected_fact_data(
                session.events, session_id=session_id, fact_type="constraint",
                value=case.seed_active_constraint, source_event_id=seed_source.event_id,
            ))
            facts_before = _fact_values(derive_protected_facts(store.read_events(session_id)))
        if case_id_needs_budget_pressure(case.case_id):
            # M7 的案例定义逐字是"B 小于候选 T"——不布这个前置，`register_constraint`
            # 永远走不到 `BUDGET_EXCEEDED` 那一支，M7 会以"没触发拒绝"判负（真因是驱动
            # 少布了压力，不是模型错）。复刻旧证据的 fixture（`protected_fact_budget_pressure`）：
            # 先播一条足以把投影推过默认预算 8192 的既有保护事实，候选一加即超限。
            self._seed_budget_pressure(session, session_id)
            facts_before = _fact_values(derive_protected_facts(store.read_events(session_id)))

        started_at = _now_utc()
        # P1-3：先快照"发消息之前"的 run_id 集合，本轮的 run 由**新出现的** run/started 定，
        # 不靠"事件流里最后一个带 run_id 的事件"——那种口径在 run/started 落盘前读到的是
        # 上一轮的 run（M8/M9 同 session 第二轮）或空（M5-2 的死因）。
        seen_run_ids = _run_ids_in(await self._events(session_id))
        response = await self._client.post(
            f"/api/sessions/{session_id}/messages", json=self._message_body(case.input_text),
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"{case.case_id} 发消息失败 HTTP {response.status_code}: {response.text[:400]}"
            )
        run_id, events = await _await_new_run_id(
            lambda: self._events(session_id), seen_run_ids=seen_run_ids,
        )
        status = await self._wait_for_terminal(session_id, run_id)


        facts_pre_answer: list[Any] = []
        if status == "paused" and case.resume_choice:
            events = await self._events(session_id)
            facts_pre_answer = _fact_values(derive_protected_facts(store.read_events(session_id)))
            await self._answer_card(session_id, case, events)

        # 抽取 job：run 终态臂已入队，等它抽出结果并读**本次 run 那一行**的落库证据。
        extraction = await self._extraction_evidence(run_id)
        events = await self._events(session_id)
        finished_at = _now_utc()

        extraction_outputs = list(self._invoker.extraction_outputs) if self._invoker else []
        if self._invoker is not None:
            self._invoker.extraction_outputs.clear()
        facts_after = _fact_values(derive_protected_facts(store.read_events(session_id)))
        return {
            "session_id": session_id,
            "run_id": run_id,
            "started_at_utc": started_at,
            "finished_at_utc": finished_at,
            "events": events,
            "facts_before": facts_before,
            "facts_pre_answer": facts_pre_answer,
            "facts_after": facts_after,
            "tool_calls": _events_of_type(events, "tool/call"),
            "tool_results": _events_of_type(events, "tool/result"),
            "final_reply": _final_reply_text(events),
            "extraction_outputs": extraction_outputs,
            "extraction": extraction,
            "new_registrations": new_active_constraints(facts_before, facts_after),
            # run 自己失败时把原因带出去（见 `run_failure_reason`）：否则证据里的 `error`
            # 是空串，与"跑完了但一行都没有"长得一样。
            "run_failure": run_failure_reason(events, run_id),
        }

    def _seed_budget_pressure(self, session: Any, session_id: str) -> None:
        """给 M7 播一条"几乎占满预算"的既有保护事实（复刻旧证据的 fixture）。

        播一条常量级大值（重复 `_BUDGET_PRESSURE_REPETITIONS` 次），使**投影后的**保护事实
        几乎顶到默认预算；随后 `register_constraint` 一加候选即 `estimated_tokens_after > B`
        ⇒ 真实的 `BUDGET_EXCEEDED`。这是"布 B < T 的前置"，不是"伪造拒绝"——拒绝仍由生产
        工具按真实计数发出。
        """
        from agent_harness.session.derive import build_protected_fact_data
        from agent_harness.session.event import TASK_PROTECTED_FACT, USER_MESSAGE

        seed_value = _BUDGET_PRESSURE_SENTENCE * _BUDGET_PRESSURE_REPETITIONS
        seed_source = session.append(USER_MESSAGE, {"content": seed_value})
        session.append(TASK_PROTECTED_FACT, build_protected_fact_data(
            session.events, session_id=session_id, fact_type="constraint",
            value=seed_value, source_event_id=seed_source.event_id,
        ))

    def _message_body(self, content: str) -> dict[str, Any]:
        """用户消息体：给足 run/session 预算，避免预算暂停掩盖真实行为。

        M8/M9 需要在 run 内暂停-恢复，预算必须容得下；M7 的预算拒绝由工具侧
        （`protected_fact_token_budget`）触发，不是 run 预算，故这里统一放宽。
        """
        return {
            "content": content,
            "budget": {
                "run": {"max_agent_turns_total": 24, "max_model_requests": 24},
                "session": {"max_agent_turns_total": 48, "max_model_requests": 48},
            },
        }

    async def _answer_card(
        self, session_id: str, case: CaseDefinition, events: list[Any],
    ) -> None:
        request = next((e for e in events if e.get("type") == "user/input-requested"), None)
        pause = next(
            (e for e in events if e.get("type") == "run/paused"
             and e.get("data", {}).get("reason") == "user_input"),
            None,
        )
        if request is None or pause is None:
            raise RuntimeError(f"{case.case_id} 期望暂停+卡片，实际缺一")
        resumed = await self._client.post(
            f"/api/sessions/{session_id}/resume",
            json={
                "run_id": pause["run_id"],
                "resume_basis": "user_input",
                "budget": {"expected_version": pause["data"]["budget_version"], "run": {}},
                "input_request": {
                    "request_id": request["data"]["request_id"],
                    "choice": case.resume_choice,
                },
            },
        )
        if resumed.status_code != 200:
            raise RuntimeError(
                f"{case.case_id} 答复 /resume 失败 HTTP {resumed.status_code}: "
                f"{resumed.text[:400]}"
            )
        # 恢复后的 run 出终态再返回（同 run 续跑，run_id 不变）。
        await self._wait_for_terminal(session_id, pause["run_id"])

    async def run_slot(self, slot: PlanSlot) -> Observation:  # pragma: no cover - 需凭证
        """跑一个槽位并折成 `Observation`（真实执行面，票面 §9.1）。

        失败**不吞**：异常折成 `Observation.error`（连同 session/run id 与失败原因一起
        进证据——Round 4 审查 P2-2：M5-2 的死因当时无法恢复，因为 `error` 从未序列化）。
        """
        case = _CASES_BY_ID[slot.case_id]
        session_id = f"ac16-{slot.case_id.lower()}-{slot.attempt}-{uuid.uuid4().hex[:8]}"
        started_at, runs_before = _now_utc(), len(self._recorder.constructions)
        try:
            raw = await self._drive_case(case, session_id)
        except Exception as exc:  # noqa: BLE001 - 失败要如实折成观测，不静默
            return Observation(
                case_id=slot.case_id, attempt=slot.attempt, error=f"{type(exc).__name__}: {exc}",
                details={
                    "session_id": session_id,
                    "run_id": "",
                    "started_at_utc": started_at,
                    "finished_at_utc": _now_utc(),
                    "model_provenance": self._primary_provenance_fields(since=runs_before),
                },
            )
        observation = build_observation(
            slot.case_id, slot.attempt,
            events=raw["events"], facts_before=raw["facts_before"], facts_after=raw["facts_after"],
            facts_pre_answer=raw["facts_pre_answer"] or None,
            extraction=raw["extraction"],
            final_reply=raw["final_reply"], tool_results=raw["tool_results"],
            run_id=raw["run_id"],
        )
        observation = replace(observation, error=raw["run_failure"])
        return replace(observation, details={
            **observation.details,
            "session_id": raw["session_id"],
            "run_id": raw["run_id"],
            "started_at_utc": raw["started_at_utc"],
            "finished_at_utc": raw["finished_at_utc"],
            "tool_calls": raw["tool_calls"],
            "tool_results": raw["tool_results"],
            "final_reply": raw["final_reply"],
            "extraction_outputs": raw["extraction_outputs"],
            "extraction_job_id": raw["extraction"]["job"],
            "extraction_state": raw["extraction"]["extraction_state"],
            "extraction_candidates": raw["extraction"]["candidates"],
            "protected_facts_before": raw["facts_before"],
            "protected_facts_after": raw["facts_after"],
            "new_active_constraints": raw["new_registrations"],
            "model_provenance": self._primary_provenance_fields(since=runs_before),
        })

    def _primary_provenance_fields(self, *, since: int) -> dict[str, Any]:
        """本 attempt 主模型构造的**如实** provenance（Round 4 审查 P2-1）。

        旧实现拿 18 次累积的构造日志冒充当次读数（`_model_ok` 的 `any(...)`），并且把
        "温度=0"与"thinking 已关"打包成一条判据。这里只**如实记录**：

        - `session_primary_constructions`：本 attempt 区间内的构造次数（0 = 没构造过，
          即本次没有真实主模型请求）；
        - `temperature_configured`：本区间内实际配置的温度值（驱动钉 0.0）；
        - `thinking_disabled_configured`：恒 False —— 本驱动**不注入** provider 专用
          `extra_body`，所以"thinking 已关"这句话没有依据，如实写 False 而不是编一条判据。
        """
        window = self._recorder.constructions[since:]
        wanted = [
            item for item in window
            if item["provider"] == self._config.session_primary_provider
            and item["model_id"] == self._config.session_primary_model
        ]
        temperatures = sorted({item["temperature_configured"] for item in wanted})
        return {
            "session_primary_constructions": len(wanted),
            "temperature_configured": temperatures,
            "thinking_disabled_configured": False,
            "note": "驱动不注入 provider 专用 extra_body；'thinking 已关'无依据，如实记 False",
        }


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
    now: str | None = None,
) -> dict[str, Any]:
    """BLOCKED 证据（缺凭证时唯一落盘的形状）。

    P4-1：时间戳与 `campaign_id` 取自**同一时刻**——`now` 缺席时才回落到本函数内的
    单次 `_now_utc()`，两处各调一次 `datetime.now` 会让 started/completed 与文件名
    落在不同时刻，不再是可复核的不变量。
    """
    head = _git("rev-parse", "HEAD") or "0" * 40
    tree = _git("rev-parse", "HEAD^{tree}") or "0" * 40
    stamp = now or _now_utc()
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "ticket": 663,
        "ac": "AC16",
        "campaign_id": campaign_id,
        "status": "blocked",
        "code_sha": head,
        "git_tree": tree,
        "working_tree_fingerprint_sha256": working_tree_fingerprint(),
        "started_at_utc": stamp,
        "completed_at_utc": stamp,
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
            "api": "production create_app ASGI (httpx.ASGITransport, same event loop)",
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
    partial_slots: tuple[str, ...] = (),
) -> dict[str, Any]:
    """把整套 18 次的 verdict + observation 折成入库证据（schema 见 `assert_evidence_shape`）。

    `partial_slots` 非空 = 这次只跑了**指定槽位**（判据/观测修好后的定点重跑）。这种证据
    必须自报 partial，且 `rerun_policy.decision` 记 `partial_slots`：否则一份只有 2 条的
    证据会长得跟"整套 18 次跑完"一样，§10.2 的整套判据就被凭空满足了。
    """
    obs_by_slot = {f"{o.case_id}-{o.attempt}": o for o in observations}
    attempts: list[dict[str, Any]] = []
    for verdict in verdicts:
        observation = obs_by_slot.get(verdict.slot_id)
        attempts.append(_attempt_record(
            observation, verdict, git_facts=git_facts,
            fallback_started=started_at_utc, fallback_finished=completed_at_utc,
        ))
    passed = [v for v in verdicts if v.passed]
    failed = [v for v in verdicts if not v.passed]
    if partial_slots:
        # 定点重跑：只如实记录跑过的槽位，不调 `decide_campaign`（那需要整套 18 个 verdict）。
        decision_status, decision_note = "partial_slots", (
            f"定点重跑 {len(partial_slots)} 个槽位：{', '.join(partial_slots)}；"
            "其余槽位沿用上一套完整 campaign 的结果"
        )
    else:
        decision = decide_campaign(verdicts, full_sets_completed=full_sets_completed)
        decision_status, decision_note = decision.status, decision.note
    if partial_slots:
        status = "partial"
    else:
        status = "passed" if decision_status == "pass" else "failed"
    payload: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "ticket": 663,
        "ac": "AC16",
        "campaign_id": campaign_id,
        "status": status,
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
            "verdict": status,
            "attempts_recorded": len(verdicts),
            "expected_attempts": len(build_plan()),
            "passed_attempts": len(passed),
            "failed_attempts": len(failed),
            "failed_case_attempts": [v.slot_id for v in failed],
        },
        "rerun_policy": {
            "max_full_set_reruns": MAX_FULL_SET_RERUNS,
            "full_sets_completed": full_sets_completed,
            "decision": decision_status,
            "note": decision_note,
        },
    }
    if partial_slots:
        payload["partial_slots"] = list(partial_slots)
    return payload


def _detail_list(details: dict[str, Any], key: str) -> list[Any]:
    """从 observation.details 取一个列表字段；缺席/类型不对 ⇒ 空列表（未采集 = 空事实）。

    空列表与"没写这个键"在证据里同形，这正是我们要的：`assert_evidence_shape` 只保证
    字段**存在且是列表**，值本身是实测事实（M4/M6 的候选就该是空的）。
    """
    value = details.get(key)
    return list(value) if isinstance(value, (list, tuple)) else []


def credential_scan_values(config: RunnerConfig, settings: Any) -> tuple[str, ...]:
    """落盘前要扫的**精确值**：配置里所有 `SecretStr` 的值 + 直传 key。

    直传 key **恒**在返回值里 —— 它没进过任何文件、判别它的唯一机械手段就是"证据里出现
    即 fail"。这一小段独立成函数，是为了让"调用点真的把它传下去了"可被单测钉住
    （2026-10-08 隔离副本变异实测：把 `+ direct` 摘掉时，只测 `_write_evidence` 的用例
    **全绿** ⇒ 那条路径没有鉴别力）。
    """
    from evaluation.live_gate.secrets import credential_values

    direct = (config.direct_api_key,) if config.direct_api_key else ()
    return tuple(credential_values(settings)) + direct


def _evidence_dir(config: RunnerConfig, campaign_id: str, stamp: str) -> Path:
    head = (_git("rev-parse", "HEAD") or "0" * 40)[:12]
    return config.out_dir / f"{stamp}-{head}-issue-663-ac16-{campaign_id}"


def _evidence_path(config: RunnerConfig, campaign_id: str, stamp: str) -> Path:
    return _evidence_dir(config, campaign_id, stamp) / "evidence.json"


def _write_evidence(payload: dict[str, Any], path: Path, *, values: Sequence[str]) -> Path:
    """凭证扫描（命中即判 fail，不静默）、脱敏后落盘。

    blocked 证据也走这里（P3-4）：它同样含 provider / campaign_id / preconditions 等
    字段，上游一旦把 key 注进去就会原文落盘 —— 旧实现对该分支直接 `write_text`、绕过
    扫描，是防御缺口。形状校验对 blocked **刻意不适用**（`attempts` 允许为空，
    见 `_blocked_evidence`）。
    """
    scanned, findings = scan_payload(payload, values=values)
    if findings:
        scanned = {**scanned, "status": "failed", "reason": "凭证扫描命中证据字段（已脱敏落盘）"}
    # shape 校验按**原始**形状判定：blocked 证据刻意允许 `attempts: []`（见
    # `_blocked_evidence`），即便它因扫描命中被改写成 failed，也仍是 blocked 形状 ——
    # 用改写后的状态判会把它推进"attempts 非空"的判据里，反而误报。
    if payload.get("status") != "blocked":
        assert_evidence_shape(scanned)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scanned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _attempt_record(
    observation: Observation, verdict: CaseVerdict, *, git_facts: dict[str, str],
    fallback_started: str = "", fallback_finished: str = "",
) -> dict[str, Any]:
    """把一次真实运行的 observation 折成一条 attempt 记录（§9.1 原始产物在此落盘）。

    与 `run_campaign` 的随跑随写（`_write_attempts_ledger`）**共用本函数** —— 两处
    必须同构，否则"增量轨迹"与"最终证据"会出现两份不一致的形状。
    """
    case = _CASES_BY_ID[verdict.case_id]
    details = dict(observation.details)
    return {
        "case_id": verdict.case_id,
        "attempt": verdict.attempt,
        "started_at_utc": details.get("started_at_utc") or fallback_started,
        "finished_at_utc": details.get("finished_at_utc") or fallback_finished,
        "input": case.input_text,
        "code_sha": git_facts["code_sha"],
        "git_tree": git_facts["git_tree"],
        "session_id": details.get("session_id", ""),
        "run_id": details.get("run_id", ""),
        "assertions": dict(observation.assertions) if observation else {},
        "assertion_details": {o.name: o.detail for o in verdict.assertions},
        "tool_calls": _detail_list(details, "tool_calls"),
        "tool_results": _detail_list(details, "tool_results"),
        "final_reply": details.get("final_reply", "") or "",
        "extraction_outputs": _detail_list(details, "extraction_outputs"),
        "extraction_job_id": details.get("extraction_job_id"),
        "extraction_state": details.get("extraction_state", "") or "",
        "extraction_candidates": details.get("extraction_candidates"),
        "protected_facts_before": _detail_list(details, "protected_facts_before"),
        "protected_facts_after": _detail_list(details, "protected_facts_after"),
        # P2-2：失败 attempt 的失败原因必须落盘（旧实现只存内存里的 `Observation.error`，
        # 从没进过证据 ⇒ M5-2 的死因当时不可恢复）。空串 = 本次成功跑完。
        "error": observation.error or "",
        # 本 attempt 的主模型 provenance（构造次数 / 温度配置 / thinking 如实值）。
        "model_provenance": details.get("model_provenance") or {},
        "verdict": "PASS" if verdict.passed else "FAIL",
    }


def _write_attempts_ledger(
    path: Path, attempts: list[dict[str, Any]], *, sha: str, values: Sequence[str],
) -> None:
    """P3-5：attempts 增量落盘（原子替换），落盘前过**同一道**凭证扫描。

    整个 campaign 的 18 次 attempt 若只在内存 `list` 里攒着，进程中途崩溃就全丢 ——
    票面 §10.2 要求"失败轨迹保留"。每跑完一次就追加并原子替换写一次（tmp + replace，
    协议 §8.9）：崩溃时盘上留下**已经跑过**的那些，不会半截截断。`started_at_utc` 与
    code_sha 在同一时刻取定，不随后续崩溃漂移。

    **凭证扫描**（任务书 §任务B 条 3"落盘前过 `credential_scan_values`"）：这一路是**主要**
    的落盘形态（18 次里最多落 18 次），必须与 `_write_evidence` 走同一层精确值扫描 —— 否则
    "最终 evidence 扫了、增量 attempts 没扫"就是一条真的旁路。命中即脱敏、并把状态改成
    failed（与 `_write_evidence` 同一处置），不静默放行。
    """
    payload: dict[str, Any] = {
        "campaign": "AC16",
        "ticket": 663,
        "code_sha": sha,
        "type": "ac16_attempts_incremental",
        "attempts_recorded": len(attempts),
        "attempts": attempts,
    }
    scanned, findings = scan_payload(payload, values=values)
    if findings:
        scanned = {**scanned, "status": "failed", "reason": "凭证扫描命中增量证据字段（已脱敏落盘）"}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(scanned, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


async def run_campaign(args: argparse.Namespace) -> CampaignResult:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    campaign_id = args.campaign_id or f"AC16-driver-{stamp[9:15]}"
    config = resolve_runner_config(args)
    runner = _RealRunner(config)
    preconditions = runner.preflight()
    if preconditions:
        now = _now_utc()
        payload = _blocked_evidence(
            config, preconditions=preconditions, campaign_id=campaign_id, now=now,
        )
        path = _evidence_path(config, campaign_id, stamp)
        if config.write:
            # P3-4：blocked 证据同样过 `scan_payload` 凭证扫描，不直接 write_text。
            _write_evidence(payload, path, values=credential_scan_values(config, runner._settings()))
        else:
            path = None
        return CampaignResult(
            status="blocked", evidence_path=path, passed_attempts=0, failed_attempts=0,
            failing_cases=(), note="；".join(preconditions),
        )

    dir_path = _evidence_dir(config, campaign_id, stamp)
    attempts_path = dir_path / "attempts.json"
    started_at = _now_utc()
    await runner._setup()
    git_facts = _git_facts()
    verdicts: list[CaseVerdict] = []
    observations: list[Observation] = []
    selected_slots = parse_slots(args.slots) if args.slots else build_plan()
    partial_slots = tuple(slot.slot_id for slot in selected_slots) if args.slots else ()
    for slot in selected_slots:
        observation = await runner.run_slot(slot)
        verdict = case_verdict(observation)
        observations.append(observation)
        verdicts.append(verdict)
        if config.write:
            _write_attempts_ledger(
                attempts_path,
                [_attempt_record(o, v, git_facts=git_facts)
                 for o, v in zip(observations, verdicts, strict=True)],
                sha=git_facts["code_sha"],
                values=credential_scan_values(config, runner._settings()),
            )
    # §10.2：只有跑到这里才把"整套 18 次已完成"记成 1——崩溃在中途不落这个终局证据，
    # 盘上留 `attempts.json` 的增量轨迹（上一行）。定点重跑（`--slots`）不算一整集。
    full_sets_completed = 0 if partial_slots else 1
    decision = None if partial_slots else decide_campaign(
        verdicts, full_sets_completed=full_sets_completed,
    )
    payload = campaign_evidence(
        verdicts, observations, config, campaign_id=campaign_id,
        full_sets_completed=full_sets_completed,
        partial_slots=partial_slots,
        git_facts=git_facts,
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
            "api": "production create_app ASGI (httpx.ASGITransport, same event loop)",
            "production_tool_registry_and_executor": True,
            "scripted_or_fake_model": False,
            "independent_session_per_attempt": True,
        },
        started_at_utc=started_at, completed_at_utc=_now_utc(),
    )
    path = _evidence_path(config, campaign_id, stamp)
    if config.write:
        # 凭证扫描：直传 key 与配置文件里的值一起作为**精确值**喂进扫描层，
        # 命中即判 fail（`_write_evidence`）。
        _write_evidence(payload, path, values=credential_scan_values(config, runner._settings()))
    else:
        path = None
    if partial_slots:
        return CampaignResult(
            status="partial", evidence_path=path,
            passed_attempts=sum(1 for v in verdicts if v.passed),
            failed_attempts=sum(1 for v in verdicts if not v.passed),
            failing_cases=tuple(v.slot_id for v in verdicts if not v.passed),
            note=payload["rerun_policy"]["note"],
        )
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
    parser.add_argument(
        "--env-file", default="",
        help=(
            f"含模型凭证的 .env；{DIRECT_KEY_ENV} 非空时直传优先，"
            f"{DEFAULT_ENV_FILE_ENV} 非空时它压过本参数"
        ),
    )
    parser.add_argument("--out-dir", default="", help="证据目录（默认 docs/live_gate）")
    parser.add_argument("--campaign-id", default="", help="证据 campaign_id")
    parser.add_argument(
        "--slots", default="",
        help="定点重跑：逗号分隔的槽位（如 M9-1,M1-2）；缺省 = 整套 18 次",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划槽位，不发任何请求")
    parser.add_argument("--no-write", action="store_true", help="不落盘证据")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        slots = parse_slots(args.slots) if args.slots else build_plan()
        for slot in slots:
            case = _CASES_BY_ID[slot.case_id]
            print(f"{slot.slot_id}\t{case.input_text}")
        scope = f"定点 {len(slots)} 个槽位（{args.slots}）" if args.slots else "整套 18 次（9 案例 × 2）"
        print(f"[ac16] 计划 {scope}；未发起任何模型请求")
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
