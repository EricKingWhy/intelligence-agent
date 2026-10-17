"""#1303 §3.1 + §3.2：看板后端投影地基（`product_state_changed_at` + BoardProjection）。

票面口径：
- `product_state_changed_at` 是产品四态的**跃迁时刻**（ack 比较键）——只在
  `product_state` 值变化时更新，取该事件的 `event.time`；
- `BoardProjectionStore` 的内存与 sidecar **都只是缓存**，删掉后 `get()` 逐字段不变；
  sidecar 命中时不得 `read_events`（AC-12 的机械形状）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session import board_projection as bp
from agent_harness.session.board_projection import (
    BoardProjection,
    BoardProjectionStore,
    derive_board_projection,
)
from agent_harness.session.event import (
    MODEL_COMPLETED,
    PERMISSION_RESOLVED,
    RUN_COMPLETED,
    RUN_STARTED,
    SESSION_STARTED,
    TASK_ACCEPTANCE_RELEASED,
    TASK_ACCEPTED,
    TASK_DEFINED,
    TOOL_APPROVAL_REQUESTED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
    VERIFICATION_UPDATED,
    SessionEvent,
)
from agent_harness.session.task import apply_task_definition, derive_task_state


def _ev(
    seq: int,
    etype: str,
    *,
    time: str | None = None,
    data: dict | None = None,
    run_id: str | None = None,
    session_id: str = "s1",
) -> SessionEvent:
    return SessionEvent(
        seq=seq,
        type=etype,
        session_id=session_id,
        run_id=run_id,
        time=time or f"2026-10-17T00:00:{seq:02d}.000+00:00",
        data=data or {},
    )


def _defined(seq: int, time: str) -> SessionEvent:
    return _ev(
        seq,
        TASK_DEFINED,
        time=time,
        data={
            "task_text": "看板地基",
            "criteria": [
                {"item_id": "ac-1", "text": "能投影", "origin": "user", "confirmed": True}
            ],
        },
    )


# ── §3.1 product_state_changed_at：五种跃迁 ────────────────────────────────


def test_changed_at_is_none_when_task_never_defined() -> None:
    state = derive_task_state([_ev(0, SESSION_STARTED)])
    assert state.product_state == ""
    assert state.product_state_changed_at is None


def test_changed_at_records_definition_transition_and_ignores_model_completion() -> None:
    defined = _defined(1, "2026-10-17T01:00:00.100+00:00")
    state = derive_task_state([_ev(0, SESSION_STARTED), defined])
    assert state.product_state == "pending_verification"
    assert state.product_state_changed_at == defined.time

    after_model = derive_task_state(
        [
            _ev(0, SESSION_STARTED),
            defined,
            _ev(2, MODEL_COMPLETED, time="2026-10-17T01:00:01.200+00:00"),
        ]
    )
    assert after_model.product_state == "pending_verification"
    assert after_model.product_state_changed_at == defined.time, "同态事件不得改 ack 键"


def test_changed_at_tracks_each_product_state_transition() -> None:
    started = _ev(0, SESSION_STARTED)
    defined = _defined(1, "2026-10-17T01:00:00.100+00:00")
    run_started = _ev(
        2, RUN_STARTED, time="2026-10-17T01:00:02.300+00:00", run_id="r1"
    )
    run_done = _ev(
        3, RUN_COMPLETED, time="2026-10-17T01:00:03.400+00:00", run_id="r1"
    )
    verified = _ev(
        4,
        VERIFICATION_UPDATED,
        time="2026-10-17T01:00:04.500+00:00",
        data={"item_id": "ac-1", "value": "passed"},
    )
    accepted = _ev(
        5,
        TASK_ACCEPTED,
        time="2026-10-17T01:00:05.600+00:00",
        data={"decision": "accepted"},
    )
    released = _ev(
        6,
        TASK_ACCEPTANCE_RELEASED,
        time="2026-10-17T01:00:06.700+00:00",
    )

    assert derive_task_state([started, defined]).product_state_changed_at == defined.time
    assert (
        derive_task_state([started, defined, run_started]).product_state_changed_at
        == run_started.time
    )
    assert (
        derive_task_state([started, defined, run_started, run_done]).product_state_changed_at
        == run_done.time
    )
    assert (
        derive_task_state(
            [started, defined, run_started, run_done, verified]
        ).product_state_changed_at
        == verified.time
    )
    assert (
        derive_task_state(
            [started, defined, run_started, run_done, verified, accepted]
        ).product_state_changed_at
        == accepted.time
    )
    assert (
        derive_task_state(
            [started, defined, run_started, run_done, verified, accepted, released]
        ).product_state_changed_at
        == released.time
    )


def test_task_state_payload_exposes_changed_at_right_after_product_state() -> None:
    state = derive_task_state(
        [_ev(0, SESSION_STARTED), _defined(1, "2026-10-17T01:00:00.100+00:00")]
    )
    payload = state.to_payload()
    keys = list(payload)
    assert keys.index("product_state_changed_at") == keys.index("product_state") + 1
    assert payload["product_state_changed_at"] == "2026-10-17T01:00:00.100+00:00"


# ── §3.2 注意力：优先级 / 终态清空 / kind 过滤 ─────────────────────────────


def _executing_events() -> list[SessionEvent]:
    return [
        _ev(0, SESSION_STARTED),
        _defined(1, "2026-10-17T01:00:00.100+00:00"),
        _ev(2, RUN_STARTED, run_id="r1", time="2026-10-17T01:00:02.300+00:00"),
    ]


def _ask_user(seq: int, request_id: str, header: str, *, kind: str = "ask_user") -> SessionEvent:
    return _ev(
        seq,
        USER_INPUT_REQUESTED,
        run_id="r1",
        data={
            "request_id": request_id,
            "kind": kind,
            "run_id": "r1",
            "questions": [{"header": header, "question": "q?"}],
        },
    )


def _approval(seq: int, approval_id: str, title: str) -> SessionEvent:
    return _ev(
        seq,
        TOOL_APPROVAL_REQUESTED,
        data={"approval_id": approval_id, "title": title},
    )


def test_attention_is_none_unless_executing() -> None:
    projection = derive_board_projection([_ev(0, SESSION_STARTED)])
    assert projection.product_state == ""
    assert projection.attention_kind is None
    assert projection.attention_summary is None

    defined_only = derive_board_projection(
        [_ev(0, SESSION_STARTED), _defined(1, "2026-10-17T01:00:00.100+00:00")]
    )
    assert defined_only.product_state == "pending_verification"
    assert defined_only.attention_kind is None


def test_attention_input_candidate_wins_by_seq_and_uses_header() -> None:
    events = [*_executing_events(), _ask_user(3, "q1", "选 A 还是 B")]
    projection = derive_board_projection(events)
    assert projection.attention_kind == "input"
    assert projection.attention_summary == "待回答：选 A 还是 B"


def test_attention_approval_wins_when_its_seq_is_later_and_resolution_clears_it() -> None:
    events = [
        *_executing_events(),
        _ask_user(3, "q1", "选 A 还是 B"),
        _approval(4, "a1", "Bash (workspace-write)"),
    ]
    projection = derive_board_projection(events)
    assert projection.attention_kind == "approval"
    assert projection.attention_summary == "待批准：Bash (workspace-write)"

    resolved = [
        *events,
        _ev(5, PERMISSION_RESOLVED, data={"approval_id": "a1", "decision": "approve_once"}),
    ]
    projection = derive_board_projection(resolved)
    assert projection.attention_kind == "input", "审批结清后回落到更早的待回答"
    assert projection.attention_summary == "待回答：选 A 还是 B"


def test_attention_takes_last_unresolved_approval_title() -> None:
    events = [
        *_executing_events(),
        _approval(3, "a1", "Read (read-only)"),
        _approval(4, "a2", "Write (workspace-write)"),
    ]
    projection = derive_board_projection(events)
    assert projection.attention_kind == "approval"
    assert projection.attention_summary == "待批准：Write (workspace-write)"


def test_attention_input_wins_when_its_seq_is_later() -> None:
    events = [
        *_executing_events(),
        _approval(3, "a1", "Bash (workspace-write)"),
        _ask_user(4, "q1", "选 A 还是 B"),
    ]
    projection = derive_board_projection(events)
    assert projection.attention_kind == "input"
    assert projection.attention_summary == "待回答：选 A 还是 B"


def test_attention_approval_is_session_wide_across_runs() -> None:
    events = [
        _ev(0, SESSION_STARTED),
        _defined(1, "2026-10-17T01:00:00.100+00:00"),
        _ev(2, RUN_STARTED, run_id="r1", time="2026-10-17T01:00:02.300+00:00"),
        _approval(3, "a1", "Write (workspace-write)"),
        _ev(4, RUN_COMPLETED, run_id="r1", time="2026-10-17T01:00:04.500+00:00"),
        _ev(5, RUN_STARTED, run_id="r2", time="2026-10-17T01:00:05.600+00:00"),
    ]
    projection = derive_board_projection(events)
    assert projection.product_state == "executing"
    assert projection.attention_kind == "approval"
    assert projection.attention_summary == "待批准：Write (workspace-write)"


def test_attention_malformed_ask_user_does_not_raise() -> None:
    malformed = _ev(
        3,
        USER_INPUT_REQUESTED,
        run_id="r1",
        data={"request_id": "q1", "kind": "ask_user", "run_id": "r1", "questions": []},
    )
    projection = derive_board_projection([*_executing_events(), malformed])
    assert projection.attention_kind is None
    assert projection.attention_summary is None


def test_attention_is_cleared_when_latest_run_is_terminal() -> None:
    events = [
        *_executing_events(),
        _approval(3, "a1", "Write (workspace-write)"),
        _ev(4, RUN_COMPLETED, run_id="r1", time="2026-10-17T01:00:04.500+00:00"),
    ]
    projection = derive_board_projection(events)
    assert projection.product_state == "pending_verification"
    assert projection.attention_kind is None
    assert projection.attention_summary is None


def test_attention_ignores_protected_fact_conflict_without_questions_key() -> None:
    conflict = _ev(
        3,
        USER_INPUT_REQUESTED,
        run_id="r1",
        data={
            "request_id": "q1",
            "kind": "protected_fact_conflict",
            "run_id": "r1",
            "question": "冲突",
            "choices": [],
        },
    )
    projection = derive_board_projection([*_executing_events(), conflict])
    assert projection.attention_kind is None, "kind 过滤必须挡住没有 questions 的写入者"


def test_attention_derivation_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_harness.session import task as task_module

    def boom(_events):
        raise RuntimeError("projection bug")

    monkeypatch.setattr(task_module, "derive_task_state", boom)
    projection = derive_board_projection([_ev(0, SESSION_STARTED)])
    assert projection.product_state == ""
    assert projection.attention_kind is None


# ── §3.2 Store：三级读取 / 水位 / 自愈 ─────────────────────────────────────


def _session_with_task(tmp_path: Path) -> tuple[JsonlSessionStore, Session]:
    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(store, cwd=str(tmp_path))
    outcome = apply_task_definition(
        session,
        task_text="看板地基",
        criteria=[{"text": "能投影", "origin": "user"}],
    )
    assert outcome.ok, outcome.reason
    return store, session


def test_answering_ask_user_clears_attention_in_sidecar(tmp_path: Path) -> None:
    """票面写「USER_MESSAGE 不改注意力」——带 input_request_id 的答复是例外。

    `pending_input_request` 的「未答复」谓词正是排除已答复的 request_id；若这条
    USER_MESSAGE 只推进水位不重算，sidecar 会把「待回答」钉到下一个触发事件。
    """
    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(store)
    apply_task_definition(session, task_text="看板地基", criteria=[{"text": "A"}])
    run_id, _ = session.begin_run()
    session.append(
        USER_INPUT_REQUESTED,
        {
            "request_id": "q1",
            "kind": "ask_user",
            "run_id": run_id,
            "questions": [{"header": "选择", "question": "哪个？"}],
        },
        run_id=run_id,
    )
    projection_store = BoardProjectionStore(store)
    assert projection_store.get(session.session_id).attention_kind == "input"

    session.append(
        USER_MESSAGE,
        {"content": "A", "input_request_id": "q1"},
        run_id=run_id,
    )
    assert projection_store.get(session.session_id).attention_kind is None
    assert projection_store.get(session.session_id).attention_summary is None


def test_store_persists_sidecar_and_returns_stable_projection(tmp_path: Path) -> None:
    store, session = _session_with_task(tmp_path)
    projection = BoardProjectionStore(store).get(session.session_id)
    assert projection.product_state == "pending_verification"
    assert projection.product_state_changed_at == session.events[-1].time
    assert projection.projection_seq == session.events[-1].seq

    sidecar = store.board_state_path(session.session_id)
    assert sidecar.exists()
    assert sidecar.name == "board_state.json"


def test_sidecar_is_only_a_cache_and_self_heals_after_deletion(tmp_path: Path) -> None:
    store, session = _session_with_task(tmp_path)
    first = BoardProjectionStore(store).get(session.session_id)
    sidecar = store.board_state_path(session.session_id)
    assert sidecar.exists()

    sidecar.unlink()
    healed = BoardProjectionStore(store).get(session.session_id)
    assert healed == first, "删掉 sidecar 后逐字段不变"
    assert sidecar.exists(), "get() 重放后必须回填 sidecar"


def test_deleted_sidecar_replays_once_then_hits_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _session_with_task(tmp_path)
    first = BoardProjectionStore(store).get(session.session_id)
    store.board_state_path(session.session_id).unlink()

    calls = 0
    original = store.read_events

    def counting(session_id: str):
        nonlocal calls
        calls += 1
        return original(session_id)

    monkeypatch.setattr(store, "read_events", counting)
    fresh = BoardProjectionStore(store)
    assert fresh.get(session.session_id) == first
    assert fresh.get(session.session_id) == first
    assert calls == 1, "删 sidecar 后首次重放一次，第二次必须命中缓存"


def test_sidecar_hit_does_not_read_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _session_with_task(tmp_path)
    first = BoardProjectionStore(store).get(session.session_id)

    def forbidden(_session_id: str):
        raise AssertionError("sidecar 命中时不得 read_events")

    monkeypatch.setattr(store, "read_events", forbidden)
    fresh = BoardProjectionStore(store)
    assert fresh.get(session.session_id) == first


def test_every_persisted_event_advances_the_sidecar_watermark(tmp_path: Path) -> None:
    store, session = _session_with_task(tmp_path)
    store_projection = BoardProjectionStore(store)
    before = store_projection.get(session.session_id)

    session.append(USER_MESSAGE, {"content": "不改产品态"})
    after_event = session.events[-1]
    payload = json.loads(store.board_state_path(session.session_id).read_text("utf-8"))
    assert payload["projection_seq"] == after_event.seq
    assert payload["product_state"] == before.product_state

    after = BoardProjectionStore(store).get(session.session_id)
    assert after.projection_seq == after_event.seq
    assert after.product_state == before.product_state
    assert after.product_state_changed_at == before.product_state_changed_at


def test_non_trigger_event_advances_watermark_without_recomputing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _session_with_task(tmp_path)
    BoardProjectionStore(store).get(session.session_id)

    def forbidden(_events):
        raise AssertionError("触发集外事件不得重算投影")

    monkeypatch.setattr(bp, "_derive", forbidden)
    session.append(USER_MESSAGE, {"content": "只推进水位"})
    payload = json.loads(store.board_state_path(session.session_id).read_text("utf-8"))
    assert payload["projection_seq"] == session.events[-1].seq


def test_stale_sidecar_falls_back_to_replay(tmp_path: Path) -> None:
    store, session = _session_with_task(tmp_path)
    BoardProjectionStore(store).get(session.session_id)

    # 模拟另一个进程越过 Session 钩子直接追加：文件戳变化 ⇒ sidecar 不再是有效缓存。
    event = SessionEvent(
        seq=session.next_seq,
        type=RUN_STARTED,
        session_id=session.session_id,
        run_id="r1",
        data={},
    )
    store.append_event(session.session_id, event)

    projection = BoardProjectionStore(store).get(session.session_id)
    assert projection.product_state == "executing"
    assert projection.projection_seq == event.seq


def test_corrupt_sidecar_falls_back_to_replay(tmp_path: Path) -> None:
    store, session = _session_with_task(tmp_path)
    first = BoardProjectionStore(store).get(session.session_id)
    store.board_state_path(session.session_id).write_text("{not json", encoding="utf-8")

    projection = BoardProjectionStore(store).get(session.session_id)
    assert projection == first


# ── #1310：sidecar 命中必须校验水位与内容一致性 ───────────────────────────


def _mutate_sidecar(store: JsonlSessionStore, sid: str, **changes) -> dict:
    """外部改写 sidecar（不动事件文件）；返回写回的完整 payload。"""
    path = store.board_state_path(sid)
    payload = json.loads(path.read_text("utf-8"))
    payload.update(changes)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _restamp_sidecar(store: JsonlSessionStore, sid: str) -> dict:
    """把 sidecar 的事件戳改成当前戳，模拟「戳匹配、投影陈旧」的坏缓存。"""
    stat = store.events_path(sid).stat()
    return _mutate_sidecar(
        store, sid, events_size=stat.st_size, events_mtime_ns=stat.st_mtime_ns
    )


def test_sidecar_with_matching_stamp_but_stale_watermark_falls_back_to_replay(
    tmp_path: Path,
) -> None:
    """#1310 复现 A/B：戳匹配但 projection_seq 落后于盘上尾 seq，不得被信任。"""
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    stale = BoardProjectionStore(store).get(sid)
    assert stale.product_state == "pending_verification"

    # 旁路写者越过 Session 钩子追加事实：盘上已 accepted，sidecar 仍停在旧水位。
    accepted = SessionEvent(
        seq=session.next_seq,
        type=TASK_ACCEPTED,
        session_id=sid,
        data={"decision": "accepted"},
    )
    store.append_event(sid, accepted)
    _restamp_sidecar(store, sid)

    projection = BoardProjectionStore(store).get(sid)
    assert projection.product_state == "accepted"
    assert projection.projection_seq == accepted.seq
    healed = json.loads(store.board_state_path(sid).read_text("utf-8"))
    assert healed["projection_seq"] == accepted.seq, "重放后必须自愈回填 sidecar"


def test_sidecar_with_illegal_product_state_falls_back_to_replay(
    tmp_path: Path,
) -> None:
    """#1310 复现 B：非法枚举原样透出 —— 读路径必须拒绝并重放。"""
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    first = BoardProjectionStore(store).get(sid)
    _mutate_sidecar(store, sid, product_state="banana")

    projection = BoardProjectionStore(store).get(sid)
    assert projection == first, "非法枚举必须视为损坏缓存"
    healed = json.loads(store.board_state_path(sid).read_text("utf-8"))
    assert healed["product_state"] == first.product_state


def test_sidecar_with_attention_outside_executing_falls_back_to_replay(
    tmp_path: Path,
) -> None:
    """#1310 复现 B：非 executing 带 attention 违反「attention 仅 executing」不变量。"""
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    first = BoardProjectionStore(store).get(sid)
    assert first.product_state != "executing"

    _mutate_sidecar(
        store, sid, attention_kind="input", attention_summary="待回答：伪造"
    )

    projection = BoardProjectionStore(store).get(sid)
    assert projection == first
    assert projection.attention_kind is None
    assert projection.attention_summary is None


def test_sidecar_with_negative_projection_seq_falls_back_to_replay(
    tmp_path: Path,
) -> None:
    """#1310：水位不得为负。

    注意 ``< 0`` 本身是冗余防御：盘上尾 seq 不是 -1，``== 盘上尾`` 已能拒绝，
    本用例证明的是「负水位不被信任」，不是单独钉 ``< 0`` 那半个子句（B-09）。
    """
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    first = BoardProjectionStore(store).get(sid)
    _mutate_sidecar(store, sid, projection_seq=-1)

    assert BoardProjectionStore(store).get(sid) == first


def test_sidecar_with_backlog_timestamp_falls_back_to_replay(tmp_path: Path) -> None:
    """B-02：backlog（从未 defined）不可能有跃迁时刻。"""
    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(store)
    sid = session.session_id
    first = BoardProjectionStore(store).get(sid)
    assert first.product_state == ""
    assert first.product_state_changed_at is None

    _mutate_sidecar(store, sid, product_state_changed_at="2026-10-17T00:00:00.000+00:00")

    projection = BoardProjectionStore(store).get(sid)
    assert projection == first, "backlog 带时间戳是派生不可能产出的组合"
    assert projection.product_state_changed_at is None


def test_sidecar_with_half_attention_pair_falls_back_to_replay(tmp_path: Path) -> None:
    """B-02：``attention_kind`` 与 ``attention_summary`` 恒成对。"""
    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(store)
    apply_task_definition(session, task_text="看板地基", criteria=[{"text": "A"}])
    run_id, _ = session.begin_run()
    session.append(
        USER_INPUT_REQUESTED,
        {
            "request_id": "q1",
            "kind": "ask_user",
            "run_id": run_id,
            "questions": [{"header": "选择", "question": "哪个？"}],
        },
        run_id=run_id,
    )
    sid = session.session_id
    first = BoardProjectionStore(store).get(sid)
    assert first.attention_kind == "input"

    _mutate_sidecar(store, sid, attention_summary=None)

    projection = BoardProjectionStore(store).get(sid)
    assert projection == first, "单边 attention 必须被视为坏 sidecar"
    assert projection.attention_summary == first.attention_summary

    _mutate_sidecar(store, sid, attention_kind=None, attention_summary="伪造")
    assert BoardProjectionStore(store).get(sid) == first


def test_attention_inner_failure_keeps_product_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-05：注意力求值异常只清 attention，不得影响产品列。"""
    from agent_harness.session import ask_user_timeout

    def boom(*_args, **_kwargs):
        raise RuntimeError("attention bug")

    monkeypatch.setattr(ask_user_timeout, "latest_pending_input_request", boom)
    projection = derive_board_projection([*_executing_events(), _ask_user(3, "q1", "选择")])
    assert projection.product_state == "executing"
    assert projection.attention_kind is None
    assert projection.attention_summary is None


def test_attention_failure_is_not_persisted_to_sidecar(tmp_path: Path) -> None:
    """A-2：注意力派生失败不得把「无注意力」写进 sidecar。

    污染行 ``data="oops"`` 会让 ``pending_input_request`` 的 ``.get`` 抛
    ``AttributeError``（``SessionEvent.from_dict`` 不校验 ``data``）；此时投影
    照常返回，但缓存与 sidecar 都不能被覆盖。
    """
    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(store)
    apply_task_definition(session, task_text="看板地基", criteria=[{"text": "A"}])
    run_id, _ = session.begin_run()
    session.append(
        USER_INPUT_REQUESTED,
        {
            "request_id": "q1",
            "kind": "ask_user",
            "run_id": run_id,
            "questions": [{"header": "选择", "question": "哪个？"}],
        },
        run_id=run_id,
    )
    sid = session.session_id
    first = BoardProjectionStore(store).get(sid)
    assert first.attention_kind == "input"
    sidecar = store.board_state_path(sid)
    before = json.loads(sidecar.read_text("utf-8"))

    # 旁路写者追加一条 data 不是 dict 的 USER_MESSAGE（越过 Session 钩子）。
    corrupted = SessionEvent(
        seq=session.next_seq,
        type=USER_MESSAGE,
        session_id=sid,
        data="oops",
    )
    store.append_event(sid, corrupted)

    projection = BoardProjectionStore(store).get(sid)
    assert projection.product_state == "executing", "注意力失败不得影响产品列"
    assert projection.attention_kind is None, "失败时对调用方降级为无注意力"
    assert json.loads(sidecar.read_text("utf-8")) == before, (
        "瞬时注意力失败不得被钉成『无注意力』"
    )


def test_concurrent_cold_gets_replay_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A-3：同一会话的并发冷启动只允许一次 ``read_events`` + 派生。"""
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    BoardProjectionStore(store).get(sid)
    store.board_state_path(sid).unlink()

    projection_store = BoardProjectionStore(store)
    calls = 0
    original = store.read_events

    def slow_read_events(session_id: str):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return original(session_id)

    monkeypatch.setattr(store, "read_events", slow_read_events)

    barrier = threading.Barrier(8)
    results: list[BoardProjection] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            results.append(projection_store.get(sid))
        except BaseException as exc:  # noqa: BLE001 - 线程内异常要带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert calls == 1, "并发突发只放行 leader 重放一次"
    assert len(results) == 8
    assert all(result == results[0] for result in results)


def test_get_does_not_persist_when_tail_is_unparseable(tmp_path: Path) -> None:
    """A-4/B-12：盘上尾不可解析时读侧不得写一份自己永远拒绝的 sidecar。"""
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    BoardProjectionStore(store).get(sid)
    sidecar = store.board_state_path(sid)
    before = json.loads(sidecar.read_text("utf-8"))

    # 模拟崩溃留下的半行：read_events 跳过它，_disk_tail_seq 解析不到。
    with store.events_path(sid).open("ab") as fh:
        fh.write(b'{"seq": 99, "type": "user/mess')

    projection = BoardProjectionStore(store).get(sid)
    assert projection.projection_seq < 99
    assert json.loads(sidecar.read_text("utf-8")) == before, (
        "尾部不可解析时落盘会被读侧永久拒绝，必须跳过"
    )


def test_read_last_line_handles_events_larger_than_the_chunk(tmp_path: Path) -> None:
    """B-06 ①：64KB+ 的单行必须能读到最后一行。"""
    payload = b'{"seq": 7, "data": "' + b"x" * (64 * 1024) + b'"}'
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"seq": 6}\n' + payload + b"\n")
    assert BoardProjectionStore._read_last_line(path) == payload


def test_read_last_line_at_exact_chunk_boundary(tmp_path: Path) -> None:
    """B-06 ②：末行（含换行）恰好 8KB，行首与块边界重合。"""
    prefix = b'{"seq": 8, "pad": "'
    suffix = b'"}'
    last = prefix + b"z" * (8191 - len(prefix) - len(suffix)) + suffix
    assert len(last) + 1 == bp._TAIL_CHUNK_BYTES
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"seq": 7}\n' + last + b"\n")
    assert BoardProjectionStore._read_last_line(path) == last


def test_sidecar_hits_when_last_event_exceeds_the_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-06 ①（集成）：大末行不影响 sidecar 命中（AC-12 仍是零 read_events）。"""
    store, session = _session_with_task(tmp_path)
    session.append(USER_MESSAGE, {"content": "x" * 70_000})
    first = BoardProjectionStore(store).get(session.session_id)

    def forbidden(_session_id: str):
        raise AssertionError("sidecar 命中时不得 read_events")

    monkeypatch.setattr(store, "read_events", forbidden)
    assert BoardProjectionStore(store).get(session.session_id) == first


def test_append_after_bypass_event_never_stamps_a_gapped_view(tmp_path: Path) -> None:
    """B-01：中段缺口 + 再 append 两条，不得把缺事件的旧视图盖上新水位。

    旧判据只查「最后两笔连续」，第 2 条 append 后 ``events[-2]+1 == events[-1]``
    成立，会把一份缺旁路事件的投影写成当前水位并被读侧永久信任。
    """
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    projection_store = BoardProjectionStore(store)
    assert projection_store.get(sid).product_state == "pending_verification"

    # 旁路写者追加触发事件 task/accepted：Session 的内存视图看不到它。
    foreign = SessionEvent(
        seq=session.next_seq,
        type=TASK_ACCEPTED,
        session_id=sid,
        data={"decision": "accepted"},
    )
    store.append_event(sid, foreign)

    # 场景前提：本聚合已交付在途 runtime ⇒ 撞号会在 store 锁内改号（#1062）。
    session.deliver_to_runtime()
    # 同一 Session 再 append 两条非触发事件：视图 [0,1,3,4]，中段缺 2。
    session.append(USER_MESSAGE, {"content": "一"})
    session.append(USER_MESSAGE, {"content": "二"})
    assert [event.seq for event in session.events] == [0, 1, 3, 4]

    stale = json.loads(store.board_state_path(sid).read_text("utf-8"))
    assert stale["projection_seq"] != session.events[-1].seq, (
        "视图有中段缺口时不得推进水位"
    )

    # 下一次读取从盘上重放，拿到旁路事件造成的真实状态。
    healed = projection_store.get(sid)
    assert healed.product_state == "accepted"
    assert healed.projection_seq == store.read_events(sid)[-1].seq


def test_executing_sidecar_with_attention_still_hits_without_read_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合法的 executing + attention sidecar 仍命中（AC-12：零 read_events）。"""
    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session.start(store)
    apply_task_definition(session, task_text="看板地基", criteria=[{"text": "A"}])
    run_id, _ = session.begin_run()
    session.append(
        USER_INPUT_REQUESTED,
        {
            "request_id": "q1",
            "kind": "ask_user",
            "run_id": run_id,
            "questions": [{"header": "选择", "question": "哪个？"}],
        },
        run_id=run_id,
    )
    first = BoardProjectionStore(store).get(session.session_id)
    assert first.product_state == "executing"
    assert first.attention_kind == "input"

    def forbidden(_session_id: str):
        raise AssertionError("sidecar 命中时不得 read_events")

    monkeypatch.setattr(store, "read_events", forbidden)
    assert BoardProjectionStore(store).get(session.session_id) == first


def test_sidecar_write_failure_does_not_break_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, session = _session_with_task(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(BoardProjectionStore, "_write_sidecar", boom)
    session.append(USER_MESSAGE, {"content": "写 sidecar 失败也必须落盘"})
    assert session.events[-1].type == USER_MESSAGE


def test_adopt_history_replays_once_and_fills_sidecar(tmp_path: Path) -> None:
    parent_store = JsonlSessionStore(root=tmp_path / "parent")
    parent = Session.start(parent_store, cwd=str(tmp_path))
    apply_task_definition(parent, task_text="父任务", criteria=[{"text": "A"}])

    child_store = JsonlSessionStore(root=tmp_path / "child")
    child = Session("child-1", child_store)
    child.adopt_history(parent.events)

    projection = BoardProjectionStore(child_store).get(child.session_id)
    assert projection.product_state == derive_task_state(child.events).product_state
    assert projection.projection_seq == child.events[-1].seq
    assert child_store.board_state_path(child.session_id).exists()


def test_store_keyed_by_root_shares_one_instance(tmp_path: Path) -> None:
    store, _session = _session_with_task(tmp_path)
    first = bp.get_board_projection_store(store)
    second = bp.get_board_projection_store(JsonlSessionStore(root=store._root))
    assert first is second

    other = bp.get_board_projection_store(JsonlSessionStore(root=tmp_path / "other"))
    assert other is not first


def test_projection_seq_empty_session_is_minus_one() -> None:
    projection = derive_board_projection([])
    assert projection == BoardProjection(
        product_state="",
        product_state_changed_at=None,
        attention_kind=None,
        attention_summary=None,
        projection_seq=-1,
    )


def test_restart_returns_identical_five_fields(tmp_path: Path) -> None:
    """AC-13 ⑤：新建 store 实例（进程重启）后五个字段逐字段相等。"""
    store, session = _session_with_task(tmp_path)
    first = BoardProjectionStore(store).get(session.session_id)

    # 删掉 sidecar：重启后的读数必须完全来自事件流重放，而不是缓存。
    store.board_state_path(session.session_id).unlink()
    restarted = BoardProjectionStore(JsonlSessionStore(root=store._root))
    assert restarted.get(session.session_id) == first
    assert store.board_state_path(session.session_id).exists()


def test_cold_cache_non_trigger_update_never_trusts_stale_sidecar(
    tmp_path: Path,
) -> None:
    """独立审查 P1：sidecar 落后时，冷缓存的非触发更新不得把它盖上当前戳。"""
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    assert BoardProjectionStore(store).get(sid).product_state == "pending_verification"

    # 触发事件的 sidecar 写失败：盘上已 executing，sidecar 仍是 pending_verification。
    with pytest.MonkeyPatch.context() as mp:

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        mp.setattr(BoardProjectionStore, "_write_sidecar", boom)
        session.begin_run()

    # 冷缓存 + 触发集外事件：必须重放，不能复用旧 sidecar。
    fresh = BoardProjectionStore(store)
    fresh.update_from_events(sid, session.events, recompute=False)
    assert fresh.get(sid).product_state == "executing"


def test_derivation_failure_does_not_poison_memory_or_sidecar(
    tmp_path: Path,
) -> None:
    """独立审查 P2：派生瞬时失败不得把空投影写进缓存/sidecar。"""
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    # 用钩子同一个单例，才能断言失败后的缓存状态。
    projection_store = bp.get_board_projection_store(store)
    before = projection_store.get(sid)
    sidecar_before = json.loads(store.board_state_path(sid).read_text("utf-8"))

    from agent_harness.session import task as task_module

    def boom(_events):
        raise RuntimeError("projection bug")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(task_module, "derive_task_state", boom)
        session.begin_run()

    sidecar_after = json.loads(store.board_state_path(sid).read_text("utf-8"))
    assert sidecar_after["product_state"] == sidecar_before["product_state"]
    assert sidecar_after["projection_seq"] == sidecar_before["projection_seq"]
    # 失败后的下一条**非触发**事件不得把旧缓存盖上当前水位（缓存弹出的判据）；
    # 下一次读取必须从事件流重放，拿到真实的新状态。
    session.append(USER_MESSAGE, {"content": "投影失败后的普通消息"})
    assert projection_store.get(sid).product_state == "executing"
    assert before.product_state == "pending_verification"


def test_read_path_derivation_failure_evicts_the_cache(tmp_path: Path) -> None:
    """独立审查第三轮 P3：读路径派生失败也必须丢掉旧缓存。

    否则下一次非触发更新会拿旧缓存重算并推进水位，把失败时点的旧状态钉住。
    """
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    projection_store = bp.get_board_projection_store(store)
    assert projection_store.get(sid).product_state == "pending_verification"

    # 旁路写者把盘上状态推到 executing，但不经过 Session 钩子（缓存仍是旧值）。
    foreign = SessionEvent(
        seq=session.next_seq,
        type=RUN_STARTED,
        session_id=sid,
        run_id="r1",
        data={},
    )
    store.append_event(sid, foreign)

    from agent_harness.session import task as task_module

    def boom(_events):
        raise RuntimeError("projection bug")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(task_module, "derive_task_state", boom)
        assert projection_store.get(sid).product_state == "", "失败时返回保守空投影"

    # 失败后的非触发更新：事件视图与盘上一致，唯一区别是缓存是否已清掉。
    projection_store.update_from_events(sid, store.read_events(sid), recompute=False)
    assert projection_store.get(sid).product_state == "executing"


def test_update_never_stamps_a_view_that_lags_the_disk(tmp_path: Path) -> None:
    """独立审查 P2：内存视图看不到旁路写入时，不得把旧投影盖上新戳。"""
    store, session = _session_with_task(tmp_path)
    sid = session.session_id
    projection_store = BoardProjectionStore(store)
    assert projection_store.get(sid).product_state == "pending_verification"

    # 旁路写者越过 Session 钩子追加一条触发事件：session.events 里看不到它。
    foreign = SessionEvent(
        seq=session.next_seq,
        type=RUN_STARTED,
        session_id=sid,
        run_id="r1",
        data={},
    )
    store.append_event(sid, foreign)

    # 钩子仍按旧视图推进水位（等价于 append 返回与取戳之间的窗口）。
    projection_store.update_from_events(sid, list(session.events), recompute=False)

    stale = json.loads(store.board_state_path(sid).read_text("utf-8"))
    assert stale["projection_seq"] != foreign.seq, "不得把没投影过的 seq 写成已覆盖"
    assert stale["product_state"] == "pending_verification"

    # 下一次读取必须从盘上重放，拿到旁路事件造成的真实状态。
    assert projection_store.get(sid).product_state == "executing"
