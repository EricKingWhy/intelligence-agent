"""Task 交付状态三轴（W-07 / #351，票面「状态契约」）。

Task 身份 = Session ID（一 Task 多 Run）。三条事实轴**分别追加、分别投影**：

1. **handler 硬校验**（``apply_task_definition`` / ``apply_acceptance_revision``
   / ``apply_verification`` / ``apply_acceptance`` / ``apply_acceptance_release``）：
   任一形状或 CAS 违反 → **拒绝、零事件**（与 ``plan.py`` 同判据：不产生半条
   定义）。接受/释放必须带 ``expected_version``：版本过期或重复接受 → 明确
   ``conflict``（票面：「重复请求幂等或明确 409，不能双写」），形状错误 →
   ``shape``。
2. **投影**（``derive_task_state``）：纯函数 events → TaskState，幂等、可重放
   重建（不变量 #22）。产品四态（executing / pending_verification / deliverable
   / accepted）是三轴的**纯投影**，TUI/Web 不各算一套；``run/completed`` 只说明
   Runtime 收口（#305 完成闸门零写入契约原样），MUST NOT 自动写验证值或接受
   状态。cwd 轴从 ``session/started`` 的既有单源锚读取（``session_cwd``），
   定义事件不复制第二份。
3. **payload 契约**：定义/修订载荷里的验收清单行（item_id / text / origin /
   confirmed）。item_id 由**写侧**补生成（``ac-`` 前缀）；投影侧刻意不做缺省
   补全——缺 id 的行就是非法 payload（与 ``plan.py`` 同哲学：缺省补全会破坏
   重放确定性，两次 derive 会得到不同 id）。

验证值是逐项 last-wins 的**观察事实**（回归是真实语义，可被后续 run 重估覆盖，
旧值留痕于更早事件）；接受是**裁决事实**，用户可带原因接受缺证据或失败结果，
但相应验证值保持原样。坏数据哲学与 ``derive.py`` 同源：一条坏数据只损失该事件，
不 brick 整条恢复链。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import uuid4

from agent_harness.session.approval import declared_permission_mode
from agent_harness.session.cwd import session_cwd
from agent_harness.session.event import (
    RUN_STARTED,
    RUN_TERMINAL_TYPES,
    TASK_ACCEPTANCE_RELEASED,
    TASK_ACCEPTANCE_REVISED,
    TASK_ACCEPTED,
    TASK_DEFINED,
    VERIFICATION_UPDATED,
    SessionEvent,
)

logger = logging.getLogger("agent_harness.session.task")

#: 验收项来源枚举（票面：用户验收项 / Agent 提出的清单；agent 项默认未确认）。
CRITERIA_ORIGINS = ("user", "agent")

#: 逐项验证值枚举（票面六态的服务端 token；中文展示名由客户端映射）。
VERIFICATION_VALUES = (
    "not_started",
    "in_progress",
    "passed",
    "failed",
    "blocked",
    "incomplete",
)

#: 用户裁决枚举（票面：未接受/已接受/带缺项接受——「未接受」是缺省态，不是事件）。
ACCEPTANCE_DECISIONS = ("accepted", "accepted_with_gaps")

#: 产品四态（票面：执行中/待验证/可交付/已接受）。未定义任务无交付状态（""）。
PRODUCT_STATES = ("executing", "pending_verification", "deliverable", "accepted")

#: 逐字持久化字段的长度封顶（先例：创建路径 task 100_000、permission reason
#: 2000——自由文本无上限 = 每次调用可重复放大 append-only JSONL，实测 50KB
#: 原样入库的先例见 web/app.py 的 reason 注释）。这里与 pydantic ``max_length``
#: 同口径：计 Python ``str`` 码点数，不是 UTF-8 字节。超限错误**不回显原文**
#: （错误 detail 会进 HTTP 响应，回显即放大）。
TASK_TEXT_MAX_LENGTH = 100_000
TASK_FIELD_MAX_LENGTH = 2000


@dataclass(frozen=True)
class TaskCriterion:
    """验收清单行。``item_id`` 是验证轴与接受轴引用的对齐键。"""

    item_id: str
    text: str
    origin: str
    confirmed: bool

    def to_payload(self) -> dict:
        return {
            "item_id": self.item_id,
            "text": self.text,
            "origin": self.origin,
            "confirmed": self.confirmed,
        }


@dataclass(frozen=True)
class VerificationEntry:
    """一项验收的当前观察事实（last-wins 快照；历史值留痕于更早事件）。"""

    value: str
    evidence: str | None


@dataclass(frozen=True)
class Acceptance:
    """用户裁决事实。``accepted_with_gaps`` 必带 reason（写侧硬校验）。"""

    decision: str
    reason: str | None


@dataclass(frozen=True)
class TaskState:
    """三轴投影快照（不可变；``product_state`` 是派生值不是存储值）。"""

    defined: bool = False
    task_text: str | None = None
    read_write_intent: str | None = None
    cwd: str | None = None
    #: 会话创建时**显式声明**的权限档（#353 P2-2）。来源 = ``session/started`` 的
    #: ``permission_mode``（``declared_permission_mode``），与 ``cwd`` 同级——只认
    #: 创建期声明、不随会话内改档变化，task/defined 不复制第二份。``None`` = 未声明
    #: （历史会话 / 用户没选），不替用户猜一个更严或更松的档。
    authorization: str | None = None
    criteria: tuple[TaskCriterion, ...] = ()
    verification: dict[str, VerificationEntry] = field(default_factory=dict)
    acceptance: Acceptance | None = None
    #: 接受轴版本号 = task/accepted + task/acceptance-released 事件计数（CAS 对齐键）。
    version: int = 0
    open_run_ids: tuple[str, ...] = ()

    @property
    def product_state(self) -> str:
        """产品四态投影（优先级 accepted > executing > deliverable > 待验证）。"""
        if not self.defined:
            return ""
        if self.acceptance is not None:
            return "accepted"
        if self.open_run_ids:
            return "executing"
        if self.criteria and all(
            self.verification.get(item.item_id) is not None
            and self.verification[item.item_id].value == "passed"
            for item in self.criteria
        ):
            return "deliverable"
        return "pending_verification"

    def to_payload(self) -> dict:
        return {
            "defined": self.defined,
            "task_text": self.task_text,
            "read_write_intent": self.read_write_intent,
            "cwd": self.cwd,
            "authorization": self.authorization,
            "criteria": [item.to_payload() for item in self.criteria],
            "verification": {
                item_id: {"value": entry.value, "evidence": entry.evidence}
                for item_id, entry in self.verification.items()
            },
            "acceptance": (
                None
                if self.acceptance is None
                else {"decision": self.acceptance.decision, "reason": self.acceptance.reason}
            ),
            "version": self.version,
            "open_run_ids": list(self.open_run_ids),
            "product_state": self.product_state,
        }


@dataclass(frozen=True)
class TaskOutcome:
    """五个 handler 的统一结果。``error_kind``：``shape`` = 载荷非法；
    ``conflict`` = 状态/CAS 冲突（REST 层映射 409，形状映射 422——spec 11 §6.1）。
    ``state`` 是本次调用时点的三轴投影快照（成功 = 落盘后，失败 = 被拒时点）。
    """

    ok: bool
    reason: str | None
    error_kind: str | None
    state: TaskState


def _failure(state: TaskState, error_kind: str, reason: str) -> TaskOutcome:
    return TaskOutcome(ok=False, reason=reason, error_kind=error_kind, state=state)


def _parse_criteria(
    raw_items: object, *, write_side: bool
) -> tuple[tuple[TaskCriterion, ...], str | None]:
    """验收清单行级校验。``write_side=False``（投影侧）拒绝缺 id / 缺 confirmed 的行。

    刻意**不做缺省补全**（plan.py 同判据）：投影侧给缺 id 行现场生成 id 会让
    两次 derive 得到不同结果，重放确定性就没了。``criteria=None``（定义时缺项）
    是合法的空清单——票面允许 Agent 稍后提出清单。

    写侧缺省（只发生在写侧，落盘前即具体化）：item_id 自动补 ``ac-`` 前缀；
    confirmed 默认 = (origin == "user")——票面「普通低风险可推进并标记清单未
    确认」，Agent 提出的清单生来未确认，用户的生来已确认。
    """
    if raw_items is None:
        return (), None
    if not isinstance(raw_items, list):
        return (), f"criteria 必须是数组，得到 {type(raw_items).__name__}"
    seen: set[str] = set()
    items: list[TaskCriterion] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return (), f"验收项必须是对象，得到 {type(raw).__name__}"
        text = raw.get("text")
        if not isinstance(text, str) or not text.strip():
            return (), f"text 必须是非空字符串（可执行判据），得到 {text!r}"
        if len(text) > TASK_FIELD_MAX_LENGTH:
            return (), (
                f"text 超过长度上限 {TASK_FIELD_MAX_LENGTH} 字符"
                "（逐字持久化字段，原文不回显）"
            )
        origin = raw.get("origin", "user")
        if origin not in CRITERIA_ORIGINS:
            return (), f"origin 非法：{origin!r}（合法值 {list(CRITERIA_ORIGINS)}）"
        confirmed = raw.get("confirmed", origin == "user" if write_side else None)
        if not isinstance(confirmed, bool):
            if write_side:
                return (), f"confirmed 必须是布尔值，得到 {confirmed!r}"
            return (), f"验收项缺 confirmed（投影侧不做缺省补全）：{text!r}"
        item_id = raw.get("item_id")
        if item_id is None:
            if not write_side:
                return (), f"验收项缺 item_id（投影侧不做缺省补全）：{text!r}"
            item_id = f"ac-{uuid4().hex[:12]}"
        if not isinstance(item_id, str) or not item_id.strip():
            return (), f"item_id 必须是非空字符串，得到 {item_id!r}"
        if item_id in seen:
            return (), f"item_id 重复：{item_id!r}（验证轴与接受轴按它对齐）"
        seen.add(item_id)
        items.append(TaskCriterion(item_id, text, origin, confirmed))
    return tuple(items), None


def _definition_payload(
    event: SessionEvent,
) -> tuple[str, str | None, tuple[TaskCriterion, ...]] | None:
    """task/defined 载荷解析：非法 → None（投影跳过该事件）。"""
    data = event.data if isinstance(event.data, dict) else None
    if not data:
        return None
    task_text = data.get("task_text")
    if not isinstance(task_text, str) or not task_text.strip():
        return None
    read_write_intent = data.get("read_write_intent")
    if read_write_intent is not None and not isinstance(read_write_intent, str):
        return None
    criteria, error = _parse_criteria(data.get("criteria"), write_side=False)
    if error is not None:
        return None
    return task_text, read_write_intent, criteria


def derive_task_state(events: list[SessionEvent]) -> TaskState:
    """纯函数投影：events → TaskState（幂等；重放 N 次结果一致）。

    三轴各取各的事件，互不干扰：定义轴 last-wins（修订整表覆盖清单）、验证轴
    逐项 last-wins、接受轴 last-wins + version 计数。open_run_ids 由
    run/started 与 run 终态事件配对得出（run/completed 在这里**只**参与
    product_state 的 executing 判定，不写验证轴/接受轴——#305 零写入契约）。
    """
    defined = False
    task_text: str | None = None
    read_write_intent: str | None = None
    criteria: tuple[TaskCriterion, ...] = ()
    verification: dict[str, VerificationEntry] = {}
    acceptance: Acceptance | None = None
    version = 0
    open_runs: set[str] = set()
    for event in events:
        etype = event.type
        if etype == TASK_DEFINED:
            parsed = _definition_payload(event)
            if parsed is None:
                logger.warning(
                    "task/defined (seq=%s) payload 非法，投影跳过该事件",
                    getattr(event, "seq", None),
                )
                continue
            task_text, read_write_intent, criteria = parsed
            defined = True
        elif etype == TASK_ACCEPTANCE_REVISED:
            data = event.data if isinstance(event.data, dict) else None
            if not isinstance(data, dict) or "criteria" not in data:
                # 缺 criteria 键 ≠ 空清单：整表替换语义下把"没给"当成 [] 会静默
                # 清空验收项（写侧 handler 已拒 None，这里防的是手写/污染 JSONL）。
                logger.warning(
                    "task/acceptance-revised (seq=%s) 缺 criteria 键，投影跳过该事件",
                    getattr(event, "seq", None),
                )
                continue
            parsed, error = _parse_criteria(data.get("criteria"), write_side=False)
            if error is not None:
                logger.warning(
                    "task/acceptance-revised (seq=%s) payload 非法（%s），投影跳过该事件",
                    getattr(event, "seq", None), error,
                )
                continue
            criteria = parsed
        elif etype == VERIFICATION_UPDATED:
            data = event.data if isinstance(event.data, dict) else {}
            item_id, value = data.get("item_id"), data.get("value")
            evidence = data.get("evidence")
            if (
                not isinstance(item_id, str) or not item_id
                or value not in VERIFICATION_VALUES
                or (evidence is not None and not isinstance(evidence, str))
            ):
                logger.warning(
                    "verification/updated (seq=%s) payload 非法，投影跳过该事件",
                    getattr(event, "seq", None),
                )
                continue
            verification[item_id] = VerificationEntry(value, evidence)
        elif etype == TASK_ACCEPTED:
            data = event.data if isinstance(event.data, dict) else {}
            decision, reason = data.get("decision"), data.get("reason")
            if decision not in ACCEPTANCE_DECISIONS or (
                reason is not None and not isinstance(reason, str)
            ):
                logger.warning(
                    "task/accepted (seq=%s) payload 非法，投影跳过该事件（version 计数照加）",
                    getattr(event, "seq", None),
                )
            else:
                acceptance = Acceptance(decision, reason)
            version += 1
        elif etype == TASK_ACCEPTANCE_RELEASED:
            acceptance = None
            version += 1
        elif etype == RUN_STARTED:
            if event.run_id is not None:
                open_runs.add(event.run_id)
        elif etype in RUN_TERMINAL_TYPES and event.run_id is not None:
            open_runs.discard(event.run_id)
    mode = declared_permission_mode(events)
    return TaskState(
        defined=defined,
        task_text=task_text,
        read_write_intent=read_write_intent,
        cwd=session_cwd(events),
        authorization=mode.value if mode else None,
        criteria=criteria,
        verification=verification,
        acceptance=acceptance,
        version=version,
        open_run_ids=tuple(sorted(open_runs)),
    )


def apply_task_definition(
    session,
    task_text: object,
    read_write_intent: object = None,
    criteria: object = None,
    *,
    run_id: str | None = None,
) -> TaskOutcome:
    """handler：校验 → 落一条 ``task/defined``（append-only，全流至多一条）。

    cwd 不进本事件——工作目录从 ``session/started`` 的单源锚读取，这里复制
    第二份就制造了两个真相。验收项可缺省（票面：缺项时 Agent 可提出清单，
    走 ``apply_acceptance_revision``）。
    """
    current = derive_task_state(session.events)
    if current.defined:
        return _failure(
            current, "conflict",
            "任务已定义（task/defined 全流至多一条）；变更验收清单走"
            " apply_acceptance_revision，不重复定义",
        )
    if not isinstance(task_text, str) or not task_text.strip():
        return _failure(
            current, "shape", f"task_text 必须是非空字符串（原始目标），得到 {task_text!r}"
        )
    if len(task_text) > TASK_TEXT_MAX_LENGTH:
        return _failure(
            current, "shape",
            f"task_text 超过长度上限 {TASK_TEXT_MAX_LENGTH} 字符"
            "（逐字持久化字段，先例 = 创建路径 task 同款封顶；原文不回显）",
        )
    if read_write_intent is not None and (
        not isinstance(read_write_intent, str) or not read_write_intent.strip()
    ):
        return _failure(
            current, "shape",
            f"read_write_intent 必须是非空字符串或 None，得到 {read_write_intent!r}",
        )
    if isinstance(read_write_intent, str) and len(read_write_intent) > TASK_TEXT_MAX_LENGTH:
        return _failure(
            current, "shape",
            f"read_write_intent 超过长度上限 {TASK_TEXT_MAX_LENGTH} 字符（原文不回显）",
        )
    parsed, error = _parse_criteria(criteria, write_side=True)
    if error is not None:
        return _failure(current, "shape", error)
    data: dict = {"task_text": task_text, "criteria": [item.to_payload() for item in parsed]}
    if read_write_intent is not None:
        data["read_write_intent"] = read_write_intent
    session.append(TASK_DEFINED, data, run_id=run_id)
    return TaskOutcome(ok=True, reason=None, error_kind=None, state=derive_task_state(session.events))


def apply_acceptance_revision(
    session, criteria: object, *, run_id: str | None = None
) -> TaskOutcome:
    """handler：验收清单整表替换 → ``task/acceptance-revised``。

    变更 AC 只追加事件；``source_event_ids`` 指向上一版定义/修订（票面：
    「保留旧版来源」——旧版事实原样留在事件流里，append-only 不删除不改写）。
    ``criteria=None``（REST 漏发字段）被拒为 shape：整表替换语义下"没给"静默
    落成空清单等于不可逆清空验收项；要清空必须显式传 ``[]``。
    """
    current = derive_task_state(session.events)
    if not current.defined:
        return _failure(current, "conflict", "尚无任务定义，无可修订的验收清单")
    if criteria is None:
        return _failure(
            current, "shape",
            "criteria 必须显式给出（整表替换）：漏发字段按 422 拒绝，"
            "不静默清空验收项；要清空传 []",
        )
    parsed, error = _parse_criteria(criteria, write_side=True)
    if error is not None:
        return _failure(current, "shape", error)
    previous_id = next(
        (
            event.event_id
            for event in reversed(session.events)
            if event.type in (TASK_DEFINED, TASK_ACCEPTANCE_REVISED)
        ),
        None,
    )
    session.append(
        TASK_ACCEPTANCE_REVISED,
        {"criteria": [item.to_payload() for item in parsed]},
        run_id=run_id,
        source_event_ids=[previous_id] if previous_id else None,
    )
    return TaskOutcome(ok=True, reason=None, error_kind=None, state=derive_task_state(session.events))


def apply_verification(
    session,
    item_id: object,
    value: object,
    evidence: object = None,
    *,
    run_id: str | None = None,
) -> TaskOutcome:
    """handler：单验收项观察事实 → ``verification/updated``（逐项 last-wins）。

    只接受当前清单里存在的 item_id（对齐键必须可解析）；value 是六态枚举。
    重估覆盖是合法语义（回归），不做状态机限制——限制它等于谎报回归。
    """
    current = derive_task_state(session.events)
    if not isinstance(item_id, str) or not item_id.strip():
        return _failure(current, "shape", f"item_id 必须是非空字符串，得到 {item_id!r}")
    if value not in VERIFICATION_VALUES:
        return _failure(
            current, "shape", f"value 非法：{value!r}（合法值 {list(VERIFICATION_VALUES)}）"
        )
    if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
        return _failure(
            current, "shape", f"evidence 必须是非空字符串或 None，得到 {evidence!r}"
        )
    if isinstance(evidence, str) and len(evidence) > TASK_FIELD_MAX_LENGTH:
        return _failure(
            current, "shape",
            f"evidence 超过长度上限 {TASK_FIELD_MAX_LENGTH} 字符"
            "（逐字持久化字段；大原文走 Artifact ref，原文不回显）",
        )
    if all(item.item_id != item_id for item in current.criteria):
        return _failure(current, "shape", f"验收项 {item_id!r} 不在当前清单里")
    data: dict = {"item_id": item_id, "value": value}
    if evidence is not None:
        data["evidence"] = evidence
    session.append(VERIFICATION_UPDATED, data, run_id=run_id)
    return TaskOutcome(ok=True, reason=None, error_kind=None, state=derive_task_state(session.events))


def apply_acceptance(
    session,
    decision: object,
    reason: object = None,
    *,
    expected_version: int,
    run_id: str | None = None,
) -> TaskOutcome:
    """handler：用户裁决 → ``task/accepted``（CAS：``expected_version`` 必填）。

    版本过期 / 重复接受 → ``conflict``（票面：「不能双写」）；带缺项接受必须
    给 reason（票面：「用户可带原因接受缺证据或失败结果」——reason 是裁决的
    可解释性要求，验证值保持原样）。
    """
    current = derive_task_state(session.events)
    if not current.defined:
        return _failure(current, "conflict", "尚无任务定义，无可接受的交付")
    if decision not in ACCEPTANCE_DECISIONS:
        return _failure(
            current, "shape", f"decision 非法：{decision!r}（合法值 {list(ACCEPTANCE_DECISIONS)}）"
        )
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        return _failure(current, "shape", f"reason 必须是非空字符串或 None，得到 {reason!r}")
    if isinstance(reason, str) and len(reason) > TASK_FIELD_MAX_LENGTH:
        return _failure(
            current, "shape",
            f"reason 超过长度上限 {TASK_FIELD_MAX_LENGTH} 字符"
            "（逐字持久化字段，先例 = permission/resolved reason 同款封顶；原文不回显）",
        )
    if decision == "accepted_with_gaps" and (not isinstance(reason, str) or not reason.strip()):
        return _failure(current, "shape", "带缺项接受（accepted_with_gaps）必须给出 reason")
    if current.version != expected_version:
        return _failure(
            current, "conflict",
            f"版本冲突：expected_version={expected_version}，当前 version="
            f"{current.version}（重新读取任务状态后重试）",
        )
    if current.acceptance is not None:
        return _failure(
            current, "conflict",
            "已存在接受事实（不能双写）；要撤销走 apply_acceptance_release",
        )
    data: dict = {"decision": decision}
    if reason is not None:
        data["reason"] = reason
    session.append(TASK_ACCEPTED, data, run_id=run_id)
    return TaskOutcome(ok=True, reason=None, error_kind=None, state=derive_task_state(session.events))


def apply_acceptance_release(
    session,
    reason: object = None,
    *,
    expected_version: int,
    run_id: str | None = None,
) -> TaskOutcome:
    """handler：撤销裁决 → ``task/acceptance-released``（同样 CAS + 零双写）。

    ``source_event_ids`` 指向被释放的接受事件；释放后 version 再 +1，验收项
    验证值保持原样（释放的是裁决，不是观察事实）。
    """
    current = derive_task_state(session.events)
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        return _failure(current, "shape", f"reason 必须是非空字符串或 None，得到 {reason!r}")
    if isinstance(reason, str) and len(reason) > TASK_FIELD_MAX_LENGTH:
        return _failure(
            current, "shape",
            f"reason 超过长度上限 {TASK_FIELD_MAX_LENGTH} 字符"
            "（逐字持久化字段，先例 = permission/resolved reason 同款封顶；原文不回显）",
        )
    if current.acceptance is None:
        return _failure(current, "conflict", "从未接受过，没有可释放的接受事实")
    if current.version != expected_version:
        return _failure(
            current, "conflict",
            f"版本冲突：expected_version={expected_version}，当前 version="
            f"{current.version}（重新读取任务状态后重试）",
        )
    accepted_id = next(
        (event.event_id for event in reversed(session.events) if event.type == TASK_ACCEPTED),
        None,
    )
    data: dict = {}
    if reason is not None:
        data["reason"] = reason
    session.append(
        TASK_ACCEPTANCE_RELEASED,
        data,
        run_id=run_id,
        source_event_ids=[accepted_id] if accepted_id else None,
    )
    return TaskOutcome(ok=True, reason=None, error_kind=None, state=derive_task_state(session.events))
