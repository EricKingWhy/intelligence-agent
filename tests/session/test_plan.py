"""进度清单服务端契约（W-26 / #380，PRD §7）：handler 硬校验 + 投影重放 + 统一工具路径。

判据全部来自票面工作指令 1：
- 双 in_progress 拒绝；51 条拒绝；completed 回退拒绝；
- 合法整表接受且可重放重建（幂等）；
- 缺字段/非法枚举拒绝且**不产生事件**（读 store 验证——"拒绝整个更新"必须落成
  "JSONL 里一个字节都没多"，不是"投影上看不见"）。

工具路径按不变量 #7（Tool 只有一条统一执行路径）打在 **ToolExecutor 的真实执行**
上（与 tests/memory/test_forget_tool.py 同款 harness），不测"工具自己说自己合规"。
"""

from __future__ import annotations

import pytest

from agent_harness.session import TASK_PLAN_UPDATED, USER_MESSAGE, Session
from agent_harness.session.context import current_session_var
from agent_harness.session.plan import (
    PLAN_MAX_ITEMS,
    apply_plan_update,
    derive_plan,
)
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session


def _item(
    id_: str, status: str = "pending", content: str | None = None,
    source: str = "agent", active_form: str | None = None,
) -> dict:
    return {
        "id": id_,
        "content": content if content is not None else f"任务 {id_}",
        "activeForm": active_form if active_form is not None else f"执行 {id_}",
        "status": status,
        "source": source,
    }


def _plan_events(session) -> list:
    return [e for e in session.events if e.type == TASK_PLAN_UPDATED]


def _reload(session) -> object:
    return Session.load(JsonlSessionStore(root=session._store._root),
                        session.session_id)


# ---------------------------------------------------------------------------
# handler 硬校验（PRD §7.2 的四条，全部"拒绝整个更新、不产生事件"）
# ---------------------------------------------------------------------------


class TestHandlerHardValidation:
    def test_double_in_progress_rejected_without_event(self, tmp_path):
        """判据 1：同一 payload 中 in_progress 恰为 0 或 1 项——两项即拒绝。"""
        session = make_session(tmp_path)
        result = apply_plan_update(session, [
            _item("a", "in_progress"), _item("b", "in_progress"),
        ])
        assert result.ok is False
        assert "in_progress" in result.reason
        assert result.current == ()
        assert _plan_events(session) == []

    def test_fifty_one_items_rejected_with_merge_hint(self, tmp_path):
        """判据 2：软上限 50——51 条拒绝，错误文案要求合并相邻项。"""
        session = make_session(tmp_path)
        items = [_item(f"t{i:02d}") for i in range(PLAN_MAX_ITEMS + 1)]
        result = apply_plan_update(session, items)
        assert result.ok is False
        assert "合并" in result.reason
        assert _plan_events(session) == []

    def test_fifty_items_accepted(self, tmp_path):
        session = make_session(tmp_path)
        items = [_item(f"t{i:02d}") for i in range(PLAN_MAX_ITEMS)]
        result = apply_plan_update(session, items)
        assert result.ok is True
        assert len(result.applied) == PLAN_MAX_ITEMS

    @pytest.mark.parametrize("regressed", ["pending", "in_progress"])
    def test_completed_cannot_regress(self, tmp_path, regressed):
        """判据 3：completed 不可回退（回退=新增项）——按 id 对齐后降级即拒绝。"""
        session = make_session(tmp_path)
        first = apply_plan_update(session, [_item("a", "completed")])
        assert first.ok is True
        result = apply_plan_update(session, [_item("a", regressed)])
        assert result.ok is False
        assert "completed" in result.reason
        assert len(_plan_events(session)) == 1, "首笔合法更新仍在，拒绝的不落盘"

    def test_pending_cannot_skip_to_completed(self, tmp_path):
        """PRD §7.2 状态机：只允许 pending↔in_progress、in_progress→completed。"""
        session = make_session(tmp_path)
        assert apply_plan_update(session, [_item("a")]).ok is True
        result = apply_plan_update(session, [_item("a", "completed")])
        assert result.ok is False
        assert len(_plan_events(session)) == 1

    def test_full_table_overwrite_rewrites_content_and_status(self, tmp_path):
        """整表覆盖：同 id 项的 content/activeForm 随表更新；状态机只管 status。"""
        session = make_session(tmp_path)
        assert apply_plan_update(session, [_item("a")]).ok is True
        result = apply_plan_update(session, [
            _item("a", "in_progress", content="改写后的任务描述"),
        ])
        assert result.ok is True
        assert result.applied[0].content == "改写后的任务描述"

    def test_new_item_may_be_born_completed(self, tmp_path):
        """合并相邻项（50 上限的解法）= 新 id 项以 completed 出生——不是回退。"""
        session = make_session(tmp_path)
        assert apply_plan_update(session, [
            _item("a", "completed"), _item("b", "completed"),
        ]).ok is True
        result = apply_plan_update(session, [
            _item("ab", "completed", content="合并后的已完成项"),
        ])
        assert result.ok is True

    @pytest.mark.parametrize("field", ["id", "content", "activeForm", "status", "source"])
    def test_missing_field_rejected_without_event(self, tmp_path, field):
        """判据 4a：缺字段拒绝——payload 必须整行合法，不做缺省补全。"""
        session = make_session(tmp_path)
        item = _item("a")
        del item[field]
        result = apply_plan_update(session, [item])
        assert result.ok is False
        assert _plan_events(session) == []

    def test_illegal_enum_rejected_without_event(self, tmp_path):
        """判据 4b：status/source 枚举外取值拒绝。"""
        session = make_session(tmp_path)
        result = apply_plan_update(session, [_item("a", status="doing")])
        assert result.ok is False
        assert _plan_events(session) == []

    def test_duplicate_id_rejected(self, tmp_path):
        """id 在表内唯一（整表覆盖的对齐键不容重复）。"""
        session = make_session(tmp_path)
        result = apply_plan_update(session, [_item("a"), _item("a", "in_progress")])
        assert result.ok is False
        assert _plan_events(session) == []

    def test_rejection_message_carries_full_current_list(self, tmp_path):
        """工作指令 2：错误文案必须带当前完整清单（供弱模型自修复）。"""
        session = make_session(tmp_path)
        assert apply_plan_update(session, [_item("a", "in_progress")]).ok is True
        result = apply_plan_update(session, [
            _item("a", "in_progress"), _item("b", "in_progress"),
        ])
        assert result.ok is False
        assert "a" in result.reason and "任务 a" in result.reason

    def test_empty_table_accepted(self, tmp_path):
        """0 或 1 个 in_progress——0 项全清也是合法状态（任务收尾）。"""
        session = make_session(tmp_path)
        assert apply_plan_update(session, [_item("a", "completed")]).ok is True
        result = apply_plan_update(session, [])
        assert result.ok is True
        assert derive_plan(session.events).items == ()


# ---------------------------------------------------------------------------
# 投影：纯函数、幂等、可重放重建（不变量 #22：事件流是唯一事实）
# ---------------------------------------------------------------------------


class TestProjection:
    def test_empty_events_yield_empty_plan(self):
        assert derive_plan([]).items == ()

    def test_replay_rebuilds_full_table_and_is_idempotent(self, tmp_path):
        """判据 5：合法整表接受且可重放重建；derive 重放 N 次结果一致。"""
        session = make_session(tmp_path)
        assert apply_plan_update(session, [
            _item("a", "in_progress"), _item("b"),
        ]).ok is True
        assert apply_plan_update(session, [
            _item("a", "completed"), _item("b", "in_progress"),
        ]).ok is True

        first = derive_plan(session.events)
        assert [item.status for item in first.items] == ["completed", "in_progress"]
        assert [item.content for item in first.items] == ["任务 a", "任务 b"]
        for _ in range(3):
            assert derive_plan(session.events) == first

    def test_reload_from_store_replays_identically(self, tmp_path):
        """重载（重启恢复链）后投影一致——投影只依赖事件流，不依赖进程内状态。"""
        session = make_session(tmp_path)
        assert apply_plan_update(session, [
            _item("a", "in_progress"), _item("b"),
        ]).ok is True
        snapshot = derive_plan(session.events)

        reloaded = _reload(session)
        assert derive_plan(reloaded.events) == snapshot

    def test_malformed_payload_is_skipped_not_fatal(self, tmp_path):
        """derive 的既有哲学：一行坏数据不 brick 整条恢复链（derive.py 同款降级）。"""
        session = make_session(tmp_path)
        assert apply_plan_update(session, [_item("a", "in_progress")]).ok is True
        session.append(TASK_PLAN_UPDATED, {"items": "garbage"})
        assert derive_plan(session.events).items[0].id == "a"


# ---------------------------------------------------------------------------
# Fork：child 在边界继承那一刻的清单（事件前缀重放，零额外机制）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_inherits_plan_at_boundary(tmp_path):
    from agent_harness.session.fork import fork_session
    from agent_harness.storage.sqlite import SqliteSessionMetaStore

    store = JsonlSessionStore(root=tmp_path)
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    session = make_session(tmp_path)
    assert apply_plan_update(session, [
        _item("a", "in_progress"), _item("b"),
    ]).ok is True
    # fork 锚点必须是用户消息（fork.py 的边界校验）——清单之后补一条。
    session.append(USER_MESSAGE, {"content": "继续"})
    anchor = session.events[-1].seq

    child = await fork_session(
        store, meta, session.session_id, boundary_user_message_seq=anchor,
        child_session_id="child",
    )
    assert derive_plan(child.events).items == derive_plan(session.events).items


# ---------------------------------------------------------------------------
# 工具路径：ToolExecutor 统一路径（不变量 #7），拒绝路径不产生事件
# ---------------------------------------------------------------------------


def _tc(items: list[dict], call_id: str = "c1") -> dict:
    return {"id": call_id, "name": "update_plan", "args": {"items": items}}


def _executor() -> ToolExecutor:
    from agent_harness.tools.update_plan import UpdatePlanTool

    registry = ToolRegistry()
    registry.register(UpdatePlanTool())
    return ToolExecutor(registry)


class TestUpdatePlanToolPath:
    @pytest.mark.asyncio
    async def test_legal_update_appends_event_via_executor(self, tmp_path):
        session = make_session(tmp_path)
        token = current_session_var.set(session)
        try:
            outcome = await _executor().execute(_tc([_item("a", "in_progress")]))
        finally:
            current_session_var.reset(token)
        assert outcome.result.ok is True
        events = _plan_events(session)
        assert len(events) == 1
        assert events[0].data["items"][0]["id"] == "a"

    @pytest.mark.asyncio
    async def test_invalid_update_returns_reason_and_current_list_without_event(
        self, tmp_path,
    ):
        session = make_session(tmp_path)
        assert apply_plan_update(session, [_item("a", "completed")]).ok is True
        token = current_session_var.set(session)
        try:
            outcome = await _executor().execute(_tc([_item("a", "in_progress")]))
        finally:
            current_session_var.reset(token)
        assert outcome.result.ok is False
        assert "completed" in outcome.result.message, "错误原因回给模型"
        assert outcome.result.metadata["current_items"][0]["id"] == "a", \
            "当前完整清单回给模型（弱模型自修复）"
        assert len(_plan_events(session)) == 1, "拒绝路径不产生 plan 事件"

    @pytest.mark.asyncio
    async def test_over_cap_via_tool_carries_merge_hint_and_current_list(
        self, tmp_path,
    ):
        """>50 走 handler 拒绝而非参数校验短路：自修复文案（合并提示 + 当前
        完整清单）必须穿透到模型面（PRD §7.2；pydantic 不设 max_length 的原因）。"""
        session = make_session(tmp_path)
        assert apply_plan_update(session, [_item("old", "in_progress")]).ok is True
        token = current_session_var.set(session)
        try:
            outcome = await _executor().execute(
                _tc([_item(f"n{i:02d}") for i in range(51)]),
            )
        finally:
            current_session_var.reset(token)
        assert outcome.result.ok is False
        assert "合并相邻项" in outcome.result.message
        assert outcome.result.metadata["current_items"][0]["id"] == "old"
        assert len(_plan_events(session)) == 1, "拒绝路径不产生 plan 事件"

    @pytest.mark.asyncio
    async def test_without_run_session_the_tool_fails_without_event(self, tmp_path):
        """current_session_var 缺省（装配回归 / 旁路调用）：如实失败，不假装可用。"""
        session = make_session(tmp_path)
        outcome = await _executor().execute(_tc([_item("a")]))
        assert outcome.result.ok is False
        assert _plan_events(session) == []

    @pytest.mark.asyncio
    async def test_extra_arg_is_rejected_before_execute(self, tmp_path):
        """extra="forbid"（与 memory 工具同款安全属性）：多塞字段进不了 execute。"""
        session = make_session(tmp_path)
        tc = _tc([_item("a")])
        tc["args"]["tenant_id"] = "other"
        token = current_session_var.set(session)
        try:
            outcome = await _executor().execute(tc)
        finally:
            current_session_var.reset(token)
        assert outcome.result.ok is False
        assert _plan_events(session) == []
