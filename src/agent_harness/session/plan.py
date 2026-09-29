"""进度清单（Plan List）服务端契约（W-26 / #380，PRD §7）。

三件东西住在这里，一个字都不散出去：

1. **handler 硬校验**（`apply_plan_update`）：PRD §7.2 的四条——单 in_progress、
   软上限 50、状态机（completed 不可回退 / 禁止 pending→completed 跳态）、
   枚举与 id 唯一。任一违反 → **拒绝整个更新、不产生事件**，错误文案带违反规则
   与当前完整清单（供弱模型自修复；PRD §7.2：整表覆盖范式下天然自修复）。
2. **投影**（`derive_plan`）：纯函数 events → PlanState，幂等、可重放重建
   （不变量 #22：事件流是唯一事实，不维护第二套真相）。Fork 按 #346 同边界继承
   = 事件前缀重放的推论，零额外机制（fork.py 的 seed 逐字复制）。
3. **payload 契约**（`PlanItem`）：`task/plan_updated.data.items` 的行形状，
   PRD §7.1 逐字（id / content / activeForm / status / source）。

写侧只有 `apply_plan_update` 一个入口（工具 `update_plan` 委托它；未来的
initializer / web API 路径也必须走它——校验长在 handler 上，不在工具上）。
derive 侧的坏数据哲学与 `derive.py` 同源：一行坏数据只损失该行，不 brick
整条恢复链（handler 校验过的日志本不该有坏行，防的是手写/污染的 JSONL）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from agent_harness.session.event import TASK_PLAN_UPDATED, SessionEvent

logger = logging.getLogger("agent_harness.session.plan")

#: PRD §7.1：三态状态机 + 来源枚举（`activeForm` 沿用 Claude Code TaskCreate 同款字段名）。
PLAN_STATUSES = ("pending", "in_progress", "completed")
PLAN_SOURCES = ("initializer", "user", "agent")

#: 软上限 50（PRD §7.2：无产品先例，工程判断；W-30 真实模型 Gate 后按实测调整）。
#: 超限的错误文案要求合并相邻项——新 id 项以 completed 出生是合法的合并手段。
PLAN_MAX_ITEMS = 50

#: PRD §7.2 状态机：只允许 pending↔in_progress、in_progress→completed。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"pending", "in_progress"}),
    "in_progress": frozenset({"in_progress", "completed"}),
    # completed 是吸收态：回退一律拒绝（"回退=新增项"，PRD §7.2 原文）。
    "completed": frozenset({"completed"}),
}


@dataclass(frozen=True)
class PlanItem:
    """清单行（PRD §7.1 逐字五字段）。"""

    id: str
    content: str
    active_form: str
    status: str
    source: str

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "activeForm": self.active_form,
            "status": self.status,
            "source": self.source,
        }


@dataclass(frozen=True)
class PlanState:
    """投影出的当前清单（不可变；last-wins 全表覆盖语义下的快照）。"""

    items: tuple[PlanItem, ...]

    def to_payload(self) -> list[dict]:
        return [item.to_payload() for item in self.items]


@dataclass(frozen=True)
class PlanUpdateOutcome:
    """`apply_plan_update` 的结果。

    `ok=False` 时 `reason` 是"违反规则 + 当前完整清单"的自修复文案（工具层原样
    翻译进 ToolResult.message），`current` 是被拒更新发生时的清单快照；`applied`
    只在成功时有值（= 落盘后的新表）。
    """

    ok: bool
    reason: str | None
    current: tuple[PlanItem, ...]
    applied: tuple[PlanItem, ...] | None = None


def _parse_items(raw_items: object) -> tuple[tuple[PlanItem, ...], str | None]:
    """payload 行级校验：五字段齐、枚举合法、id 唯一、非空串。

    返回 (解析成功的行, None) 或 (空表, 错误原因)。刻意**不做缺省补全**——
    缺字段就是非法 payload（判据 4a：拒绝整个更新，不猜模型想写什么）。
    """
    if not isinstance(raw_items, list):
        return (), "items 必须是数组（PRD §7.1 整表覆盖 payload）"
    seen: set[str] = set()
    items: list[PlanItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            return (), f"清单项必须是对象，得到 {type(raw).__name__}"
        missing = [key for key in ("id", "content", "activeForm", "status", "source")
                   if key not in raw]
        if missing:
            return (), f"清单项缺字段 {missing}（PRD §7.1 五字段缺一不可）"
        status, source = raw["status"], raw["source"]
        if status not in PLAN_STATUSES:
            return (), f"status 非法：{status!r}（合法值 {list(PLAN_STATUSES)}）"
        if source not in PLAN_SOURCES:
            return (), f"source 非法：{source!r}（合法值 {list(PLAN_SOURCES)}）"
        item_id, content, active_form = raw["id"], raw["content"], raw["activeForm"]
        for name, value in (("id", item_id), ("content", content),
                            ("activeForm", active_form)):
            if not isinstance(value, str) or not value.strip():
                return (), f"{name} 必须是非空字符串，得到 {value!r}"
        if item_id in seen:
            return (), f"id 重复：{item_id!r}（整表覆盖的对齐键在表内必须唯一）"
        seen.add(item_id)
        items.append(PlanItem(item_id, content, active_form, status, source))
    return tuple(items), None


def _format_current(items: tuple[PlanItem, ...]) -> str:
    """当前清单的自修复文案（PRD §7.2：错误返回完整当前清单）。"""
    if not items:
        return "（当前清单为空）"
    return json.dumps([item.to_payload() for item in items], ensure_ascii=False)


def validate_plan_update(
    current: tuple[PlanItem, ...], incoming: tuple[PlanItem, ...],
) -> str | None:
    """PRD §7.2 状态机与不变量校验。合法返回 None；非法返回错误原因。

    校验按 id 对齐新旧两表：新 id 项"出生"不受状态机约束（合并相邻项 =
    新 completed 项出生，是软上限的合法解法）；既有 id 项的 status 迁移必须
    走 `_ALLOWED_TRANSITIONS`。
    """
    if len(incoming) > PLAN_MAX_ITEMS:
        return (
            f"清单 {len(incoming)} 条超过软上限 {PLAN_MAX_ITEMS}"
            f"（PRD §7.2）：要求合并相邻项后重新提交整表。\n当前清单："
            f"{_format_current(current)}"
        )
    in_progress = [item.id for item in incoming if item.status == "in_progress"]
    if len(in_progress) > 1:
        return (
            f"单 in_progress 违反（PRD §7.2）：in_progress 恰为 0 或 1 项，"
            f"得到 {len(in_progress)} 项（{in_progress}）。\n当前清单："
            f"{_format_current(current)}"
        )
    previous = {item.id: item for item in current}
    for item in incoming:
        old = previous.get(item.id)
        if old is None:
            continue
        if item.status not in _ALLOWED_TRANSITIONS[old.status]:
            return (
                f"状态机违反（PRD §7.2）：项 {item.id!r} 不能从 "
                f"{old.status!r} 迁移到 {item.status!r}（只允许 "
                f"pending↔in_progress、in_progress→completed；completed 不可回退，"
                f"回退=新增项）。\n当前清单：{_format_current(current)}"
            )
    return None


def apply_plan_update(
    session, items: object, *, run_id: str | None = None,
) -> PlanUpdateOutcome:
    """handler：校验 → 落一个 `task/plan_updated`（append-only，整表覆盖）。

    任一校验违反 → **不 append**（判据："读 store 验证不产生事件"），返回失败
    outcome。事件 payload 就是入参的合法化形状——投影（`derive_plan`）从它
    原样重建，不含运行时补的字段。
    """
    incoming, error = _parse_items(items)
    current = derive_plan(session.events).items
    if error is None:
        error = validate_plan_update(current, incoming)
    if error is not None:
        return PlanUpdateOutcome(ok=False, reason=error, current=current)
    session.append(
        TASK_PLAN_UPDATED,
        {"items": [item.to_payload() for item in incoming]},
        run_id=run_id,
    )
    return PlanUpdateOutcome(ok=True, reason=None, current=current, applied=incoming)


def derive_plan(events: list[SessionEvent]) -> PlanState:
    """纯函数投影：events → 当前清单（幂等；重放 N 次结果一致）。

    last-wins：同一张表被整表覆盖时取最新事件。payload 非法的事件跳过并
    告警（handler 挡在写侧，这里防的是手写/污染的 JSONL——与
    `derive.py` 的"一行坏数据只损失该行"同一哲学）。
    """
    state: tuple[PlanItem, ...] = ()
    for event in events:
        if event.type != TASK_PLAN_UPDATED:
            continue
        raw = event.data.get("items") if isinstance(event.data, dict) else None
        parsed, error = _parse_items(raw)
        if error is not None:
            logger.warning(
                "task/plan_updated (seq=%s) payload 非法（%s），投影跳过该事件",
                getattr(event, "seq", None), error,
            )
            continue
        state = parsed
    return PlanState(items=state)
