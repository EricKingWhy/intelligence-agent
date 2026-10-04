"""W-07（#351）：Task / Run / 验证 / 接受四种事实分开的分轴契约。

票面「状态契约」：Task 身份 = Session ID，一 Task 多 Run；三轴事实**分别追加、
分别投影**——定义轴 `task/defined` + `task/acceptance-revised`、验证轴
`verification/updated`、接受轴 `task/accepted` + `task/acceptance-released`。
`run/completed` 只说明 Runtime 收口（#305 完成闸门零写入契约原样），MUST NOT
自动写验证值或接受状态；产品四态（执行中/待验证/可交付/已接受）是纯投影
（`derive_task_state`），刷新/重启从事件流重建同一结果，TUI/Web 不各算一套。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.errors import SeqConflict
from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_STARTED,
    TASK_ACCEPTANCE_REVISED,
    TASK_ACCEPTED,
    TASK_DEFINED,
    USER_MESSAGE,
)
from agent_harness.session.fork import fork_session
from agent_harness.session.task import (
    apply_acceptance,
    apply_acceptance_release,
    apply_acceptance_revision,
    apply_task_definition,
    apply_verification,
    derive_task_state,
)
from agent_harness.storage.sqlite import SqliteSessionMetaStore


def _session(tmp_path) -> Session:
    return Session.start(JsonlSessionStore(root=tmp_path), cwd=str(tmp_path))


def _criteria(state):
    return [c.item_id for c in state.criteria]


# ── 定义轴：task/defined + task/acceptance-revised ──────────────────────────


def test_task_definition_roundtrip(tmp_path) -> None:
    session = _session(tmp_path)
    outcome = apply_task_definition(
        session,
        task_text="把登录页修好",
        read_write_intent="只读 src/auth，可写 tests/",
        criteria=[{"text": "登录成功跳转", "origin": "user"}],
    )
    assert outcome.ok, outcome.reason
    assert session.events[-1].type == TASK_DEFINED
    state = derive_task_state(session.events)
    assert state.defined and state.task_text == "把登录页修好"
    assert state.read_write_intent == "只读 src/auth，可写 tests/"
    # 工作目录从 session/started 的既有 cwd 锚单源读取（AC5），task/defined 不复制第二份
    assert state.cwd == str(tmp_path)
    assert len(state.criteria) == 1
    assert state.criteria[0].text == "登录成功跳转"
    assert state.criteria[0].origin == "user"
    assert state.criteria[0].confirmed is True
    assert state.criteria[0].item_id.startswith("ac-")
    # 无 open run、无验证值、未接受 ⇒ 待验证；版本从 0 起
    assert state.product_state == "pending_verification"
    assert state.version == 0
    assert state.acceptance is None


def test_task_definition_rejects_bad_payload(tmp_path) -> None:
    session = _session(tmp_path)
    before = len(session.events)
    assert not apply_task_definition(session, task_text="   ").ok
    assert not apply_task_definition(
        session, task_text="T", criteria=[{"text": "A", "origin": "model"}]
    ).ok, "origin 枚举外必须拒绝"
    assert not apply_task_definition(
        session, task_text="T", criteria=[{"text": "A", "confirmed": "yes"}]
    ).ok, "confirmed 必须是布尔"
    assert not apply_task_definition(
        session,
        task_text="T",
        criteria=[{"text": "A", "item_id": "ac-x"}, {"text": "B", "item_id": "ac-x"}],
    ).ok, "item_id 重复必须拒绝"
    assert len(session.events) == before, "拒绝 = 零事件（不产生半条定义）"


def test_acceptance_revision_appends_and_keeps_history(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "旧判据"}])
    first_event = session.events[-1]
    outcome = apply_acceptance_revision(
        session,
        criteria=[
            {"text": "新判据一", "origin": "agent"},  # 缺 confirmed：Agent 提出的生来未确认
            {"text": "新判据二"},
        ],
    )
    assert outcome.ok, outcome.reason
    revision = session.events[-1]
    assert revision.type == TASK_ACCEPTANCE_REVISED
    # 变更 AC 只追加事件，且保留旧版来源（source_event_ids 指向上一版定义/修订）
    assert revision.source_event_ids == [first_event.event_id]
    state = derive_task_state(session.events)
    assert [c.text for c in state.criteria] == ["新判据一", "新判据二"]
    assert state.criteria[0].confirmed is False, "agent 提出的清单默认未确认"
    assert state.criteria[1].confirmed is True, "user 清单默认已确认"
    # 旧版事实原样留在事件流里（append-only，不删除不改写）
    assert any(
        e.type == TASK_DEFINED and e.event_id == first_event.event_id
        for e in session.events
    )


# ── 验证轴：verification/updated 逐项 last-wins ─────────────────────────────


def test_verification_last_wins(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}, {"text": "B"}])
    state0 = derive_task_state(session.events)
    item_a, item_b = _criteria(state0)
    assert apply_verification(session, item_a, "in_progress").ok
    assert apply_verification(
        session, item_a, "passed", evidence="pytest -q tests/auth：12 passed"
    ).ok
    assert apply_verification(session, item_b, "failed", evidence="3 failed").ok
    state = derive_task_state(session.events)
    assert state.verification[item_a].value == "passed"
    assert state.verification[item_a].evidence == "pytest -q tests/auth：12 passed"
    assert state.verification[item_b].value == "failed"
    # 观察事实可被后续 run 重估覆盖（回归是真实语义），旧值留痕于更早事件
    assert apply_verification(session, item_a, "failed").ok
    assert derive_task_state(session.events).verification[item_a].value == "failed"


def test_verification_rejects_unknown_item_and_bad_value(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}])
    item_a = _criteria(derive_task_state(session.events))[0]
    before = len(session.events)
    unknown = apply_verification(session, "ac-does-not-exist", "passed")
    assert not unknown.ok and unknown.error_kind == "shape"
    bad_value = apply_verification(session, item_a, "skipped")
    assert not bad_value.ok and bad_value.error_kind == "shape"
    assert len(session.events) == before, "拒绝 = 零事件"


# ── 接受轴：task/accepted + task/acceptance-released，CAS 保护 ───────────────


def test_acceptance_cas_and_no_double_write(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}])
    stale = apply_acceptance(session, "accepted", expected_version=1)
    assert not stale.ok and stale.error_kind == "conflict", "版本过期必须 409 语义"
    assert apply_acceptance(session, "accepted", expected_version=0).ok
    state = derive_task_state(session.events)
    assert state.product_state == "accepted"
    assert state.version == 1
    # 已接受再接受 = 明确冲突（不能双写）：接受事件全流恰一条
    again = apply_acceptance(session, "accepted", expected_version=1)
    assert not again.ok and again.error_kind == "conflict"
    assert len([e for e in session.events if e.type == TASK_ACCEPTED]) == 1
    # 释放：过期版本拒绝；正确版本回未接受，version 再 +1
    assert apply_acceptance_release(session, expected_version=0).error_kind == "conflict"
    assert apply_acceptance_release(session, expected_version=1, reason="还要补测试").ok
    state = derive_task_state(session.events)
    assert state.acceptance is None and state.version == 2
    # 释放后可再接受（带缺项接受也要走 CAS）
    assert apply_acceptance(
        session, "accepted_with_gaps", reason="失败项留到下批", expected_version=2
    ).ok
    state = derive_task_state(session.events)
    assert state.acceptance is not None and state.acceptance.decision == "accepted_with_gaps"
    assert state.product_state == "accepted"


def test_acceptance_shape_rules(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}])
    before = len(session.events)
    gaps_without_reason = apply_acceptance(
        session, "accepted_with_gaps", reason=None, expected_version=0
    )
    assert not gaps_without_reason.ok and gaps_without_reason.error_kind == "shape"
    bad_decision = apply_acceptance(session, "auto_accepted", expected_version=0)
    assert not bad_decision.ok and bad_decision.error_kind == "shape"
    release_when_never_accepted = apply_acceptance_release(
        session, expected_version=0
    )
    assert (
        not release_when_never_accepted.ok
        and release_when_never_accepted.error_kind == "conflict"
    ), "从未接受过就没有可释放的接受事实"
    assert len(session.events) == before, "全部拒绝 = 零事件"


# ── 产品四态投影：执行中/待验证/可交付/已接受 + run/completed 边界 ────────────


def test_product_state_precedence_and_run_completed_boundary(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}, {"text": "B"}])
    item_a, item_b = _criteria(derive_task_state(session.events))
    session.append(RUN_STARTED, {}, run_id="r1")
    assert derive_task_state(session.events).product_state == "executing"
    # run/completed 只说明 Runtime 收口：验证轴 / 接受轴零自动写入
    session.append(RUN_COMPLETED, {"final_text": "做完了"}, run_id="r1")
    state = derive_task_state(session.events)
    assert state.product_state == "pending_verification"
    assert state.acceptance is None
    assert item_a not in state.verification and item_b not in state.verification
    apply_verification(session, item_a, "passed")
    assert derive_task_state(session.events).product_state == "pending_verification"
    apply_verification(session, item_b, "passed")
    assert derive_task_state(session.events).product_state == "deliverable"
    assert apply_acceptance(session, "accepted", expected_version=0).ok
    assert derive_task_state(session.events).product_state == "accepted"


def test_product_state_requires_at_least_one_passed_item(tmp_path) -> None:
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T")  # 缺项：清单为空
    session.append(RUN_COMPLETED, {}, run_id="r1")
    state = derive_task_state(session.events)
    assert state.product_state == "pending_verification", (
        "零验收项时『证据齐全』不成立——run/completed 不能把空清单任务变成可交付"
    )


# ── 持久化：重启后从事件流重建同一投影 ────────────────────────────────────────


def test_restart_rebuilds_same_state(tmp_path) -> None:
    store = JsonlSessionStore(root=tmp_path)
    session = Session.start(store, cwd=str(tmp_path))
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}])
    item_a = _criteria(derive_task_state(session.events))[0]
    apply_verification(session, item_a, "passed", evidence="12 passed")
    apply_acceptance(session, "accepted", expected_version=0)
    before = derive_task_state(session.events).to_payload()
    reloaded = Session.load(store, session.session_id)
    assert derive_task_state(reloaded.events).to_payload() == before


def test_session_without_task_definition_has_no_task_state(tmp_path) -> None:
    session = _session(tmp_path)
    state = derive_task_state(session.events)
    assert not state.defined
    assert state.product_state == ""  # 未定义任务：无交付状态可言（REST 404 语义）

# ── Fork：接受事实随事件前缀继承，fork 后父子独立（票面 AC「Fork 独立接受」）──


@pytest.mark.asyncio
async def test_fork_independent_acceptance(tmp_path) -> None:
    store = JsonlSessionStore(root=tmp_path)
    parent = Session.start(store, session_id="parent", cwd=str(tmp_path))
    apply_task_definition(parent, task_text="T", criteria=[{"text": "A"}])
    item_id = _criteria(derive_task_state(parent.events))[0]
    apply_verification(parent, item_id, "passed")
    # fork 边界必须是用户消息（fork 契约）：验证事实之后用户要求分叉重试
    parent.append(USER_MESSAGE, {"content": "分一条支线重试"})
    anchor = parent.events[-1].seq

    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    child = await fork_session(
        store, meta, "parent", boundary_user_message_seq=anchor,
        child_session_id="child",
    )

    # child 的 seed 里有同一份定义/验证事实（重放重建成立）
    child_state = derive_task_state(child.events)
    assert child_state.defined and child_state.acceptance is None
    assert child_state.verification[item_id].value == "passed"
    # child 里接受：只写 child 的事件流
    assert apply_acceptance(child, "accepted", expected_version=0).ok
    assert derive_task_state(child.events).product_state == "accepted"
    # 父零写入（spec 03 §7：fork 后父子独立）
    parent_state = derive_task_state(store.read_events("parent"))
    assert parent_state.acceptance is None
    assert parent_state.version == 0
    assert parent_state.product_state == "deliverable"

# ── 服务层写入路径（票面 AC「两客户端同时接受」：并发恰一生效 + 重试有界）──────
# 测试移植自 test_model_change.py 的两条先例（frozen 首快照 + asyncio.gather；
# 持续冲突断言尝试次数）——_apply_task_handler 逐字复制了那条重试机制，测试随迁。


def _svc_state(tmp_path):
    """与 test_model_change.py::_state 同构的隔离 AppState 替身（只测写入路径）。"""
    from agent_harness.config import Settings
    from tests.session.ledger_doubles import idle_operation_ledger

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
    )
    state = MagicMock()
    state.settings = settings
    state.store = JsonlSessionStore(root=tmp_path / "sessions")
    state.workspaces_root = tmp_path
    state.workspace_registry = None
    state.workspace_index = None
    state.run_manager = MagicMock()
    state.run_manager.get_active = MagicMock(return_value=None)
    state.get_wiring = AsyncMock(return_value=(MagicMock(), MagicMock()))
    state.operation_ledger = idle_operation_ledger()
    state.ensure_stores = AsyncMock()
    state.stores = MagicMock()
    state.stores.delegation_tree_ledger.get_session_budget = AsyncMock(return_value=None)
    return state


def _service(state):
    from agent_harness.web.app import session_service

    return session_service(state)


def _seed_defined_task(state) -> Session:
    session = Session.start(state.store, session_id="sid")
    assert apply_task_definition(session, task_text="T", criteria=[{"text": "A"}]).ok
    return session


def test_parallel_acceptance_exactly_one_wins(tmp_path, monkeypatch) -> None:
    """两个客户端同时接受（同 CAS 版本）：恰一生效，另一个 409——不能双写。"""
    state = _svc_state(tmp_path)
    _seed_defined_task(state)
    real_read = state.store.read_events
    frozen = real_read("sid")
    reads: list[int] = []

    def read_with_frozen_first_snapshot(session_id: str):
        if len(reads) < 2:  # 两个并发写者各读到一次「接受前」快照
            reads.append(1)
            return list(frozen)
        return real_read(session_id)

    monkeypatch.setattr(state.store, "read_events", read_with_frozen_first_snapshot)

    async def run_both():
        service = _service(state)
        return await asyncio.gather(
            service.task_acceptance(
                session_id="sid", decision="accepted", expected_version=0
            ),
            service.task_acceptance(
                session_id="sid", decision="accepted", expected_version=0
            ),
        )

    outcomes = asyncio.run(run_both())
    events = real_read("sid")
    accepted = [e for e in events if e.type == TASK_ACCEPTED]
    assert len(accepted) == 1, "接受事件全流恰一条（不能双写）"
    assert [o.ok for o in outcomes].count(True) == 1
    rejected = next(o for o in outcomes if not o.ok)
    assert rejected.error_kind == "conflict"
    # seq 严格单调（并发写者不撞号）
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


def test_task_command_retry_is_bounded_and_surfaces_persistent_conflict(
    tmp_path, monkeypatch
) -> None:
    """持续冲突（重试用尽）→ 冒泡 SeqConflict，且重试次数有界。

    断言尝试次数而不是只断言异常类型：否则「零重试」的实现也能通过（假绿）。
    """
    state = _svc_state(tmp_path)
    _seed_defined_task(state)
    attempts: list[int] = []

    def always_conflict(session_id: str, event):
        attempts.append(1)
        raise SeqConflict("注入：持续冲突")

    monkeypatch.setattr(state.store, "append_event", always_conflict)

    with pytest.raises(SeqConflict):
        asyncio.run(
            _service(state).task_acceptance(
                session_id="sid", decision="accepted", expected_version=0
            )
        )
    assert len(attempts) == 3, f"重试次数应为 3（有界），实际 {len(attempts)}"


# ── 审查修复轮补充：修订拒 None、逐字持久化字段封顶、污染日志严格投影 ────────


def test_acceptance_revision_rejects_missing_criteria(tmp_path) -> None:
    """漏发 criteria 字段 ≠ 显式空清单：整表替换语义下静默清空不可逆，按 shape 拒。"""
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}])
    before = len(session.events)
    outcome = apply_acceptance_revision(session, None)
    assert not outcome.ok and outcome.error_kind == "shape"
    assert len(session.events) == before


def test_persistent_text_fields_are_capped(tmp_path) -> None:
    """逐字持久化字段写侧封顶（先例：task 100_000 / reason、evidence、清单 text 2000）；
    超限错误不回显原文。"""
    session = _session(tmp_path)
    outcome = apply_task_definition(session, task_text="长" * 100_001)
    assert not outcome.ok and outcome.error_kind == "shape"
    assert "长" * 50 not in outcome.reason, "超限错误不得回显原文"
    assert apply_task_definition(session, task_text="T", criteria=[{"text": "A"}]).ok
    item_a = _criteria(derive_task_state(session.events))[0]
    before = len(session.events)
    assert not apply_verification(
        session, item_a, "passed", evidence="证" * 2001
    ).ok
    assert not apply_acceptance(
        session, "accepted", reason="理" * 2001, expected_version=0
    ).ok
    assert not apply_acceptance_revision(
        session, [{"text": "判" * 2001}]
    ).ok
    assert len(session.events) == before


def test_derive_skips_revised_event_without_criteria_key(tmp_path) -> None:
    """手写/污染 JSONL 里缺 criteria 键的 revised 事件被投影跳过，不当成空清单。"""
    session = _session(tmp_path)
    apply_task_definition(session, task_text="T", criteria=[{"text": "A"}])
    session.append(TASK_ACCEPTANCE_REVISED, {})
    state = derive_task_state(session.events)
    assert [c.text for c in state.criteria] == ["A"], "缺键事件不应用为空清单"
