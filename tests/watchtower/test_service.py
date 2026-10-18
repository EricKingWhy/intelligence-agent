from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_harness.session.event import (
    MODEL_COMPLETED,
    MODEL_REQUEST,
    RUN_FAILED,
    RUN_STARTED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
)
from agent_harness.tooling.contract import PermissionPolicy
from agent_harness.watchtower.service import (
    CONSENT_VERSION,
    WAIT_TIMEOUT_MESSAGE,
    WatchtowerService,
    absolute_trigger,
)
from agent_harness.watchtower.store import SqliteWatchtowerStore


class _Run:
    DONE = object()

    def __init__(self) -> None:
        self.unsubscribed = False

    def unsubscribe(self, subscriber) -> None:
        self.unsubscribed = True


class _FakeSessionService:
    def __init__(self) -> None:
        self.events = {}
        self.created = []
        self.resumed = []
        self.run = _Run()
        self.subscriber = SimpleNamespace(queue=asyncio.Queue())
        self.crash_after_user_message = False
        self.active = False
        self.create_started_at = None
        self.resume_started_at = None
        self.create_delay_seconds = 0

    async def get_events(self, session_id: str):
        return self.events.get(session_id, [])

    async def create_and_launch(self, **kwargs):
        self.create_started_at = datetime.now(UTC)
        self.created.append(kwargs)
        self.events[kwargs["session_id"]] = [
            SimpleNamespace(type="session/started", data={}, run_id=None),
        ]
        if self.create_delay_seconds:
            await asyncio.sleep(self.create_delay_seconds)

    async def resume_and_launch(self, **kwargs):
        self.resume_started_at = datetime.now(UTC)
        self.resumed.append(kwargs)
        if kwargs.get("task") is not None:
            metadata = kwargs.get("user_input_metadata") or {}
            idempotency_key = metadata.get("idempotency_key")
            events = self.events.setdefault(kwargs["session_id"], [])
            existing = [
                event for event in events
                if event.type == USER_MESSAGE
                and event.data.get("idempotency_key") == idempotency_key
            ] if idempotency_key else []
            if existing:
                assert len(existing) == 1
                assert existing[0].data["content"] == kwargs["task"]
            else:
                data = {"content": kwargs["task"]}
                if idempotency_key:
                    data["idempotency_key"] = idempotency_key
                events.append(SimpleNamespace(type=USER_MESSAGE, data=data, run_id=None))
            if self.crash_after_user_message:
                self.crash_after_user_message = False
                raise asyncio.CancelledError
        self.events.setdefault(kwargs["session_id"], []).append(
            SimpleNamespace(type=RUN_STARTED, data={}, run_id="run-1"),
        )
        self.active = True
        return SimpleNamespace(run=self.run, subscriber=self.subscriber)

    async def stream_reconnect(self, *, session_id: str):
        if self.active:
            return SimpleNamespace(run=self.run, subscriber=self.subscriber)
        return SimpleNamespace(run=None, subscriber=None)


def _settings():
    return SimpleNamespace(ask_user_question_timeout=300)


async def _new_service(tmp_path):
    store = SqliteWatchtowerStore(tmp_path / "harness.db")
    await store.initialize()
    session_service = _FakeSessionService()
    service = WatchtowerService(
        store=store, session_service=session_service, settings=_settings(),
    )
    return store, session_service, service


async def _rule(service: WatchtowerService, **overrides):
    values = {
        "prompt": "检查构建失败并修复",
        "trigger_kind": "relative",
        "timezone": "Asia/Shanghai",
        "delay_seconds": 60,
        "local_time": None,
        "model": None,
        "sandbox_backend": "docker",
        "cwd": None,
        "budget_turns": 4,
        "budget_tokens": 32_000,
        "budget_duration_seconds": 1800,
        "consent_acknowledged": True,
        "consent_version": CONSENT_VERSION,
    }
    values.update(overrides)
    return await service.create_rule(**values)


@pytest.mark.asyncio
async def test_create_requires_current_persisted_consent(tmp_path):
    _, _, service = await _new_service(tmp_path)

    with pytest.raises(ValueError, match="知情同意"):
        await _rule(service, consent_acknowledged=False)
    with pytest.raises(ValueError, match="知情同意"):
        await _rule(service, consent_version="old-version")


def test_absolute_trigger_rejects_dst_gap_and_resolves_ambiguous_time():
    with pytest.raises(ValueError, match="daylight-saving"):
        absolute_trigger("2026-03-08T02:30", "America/New_York")

    earlier = absolute_trigger("2026-11-01T01:30", "America/New_York")
    assert earlier == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fire_uses_existing_session_path_and_preserves_sandbox_and_consent(tmp_path):
    store, session_service, service = await _new_service(tmp_path)
    session_service.create_delay_seconds = 0.15
    rule = await _rule(service)
    rule = await store.transition(
        rule.rule_id, expected_version=rule.version, state="active",
        event_kind="test_due", patch={"trigger_at": datetime.now(UTC).isoformat()},
    )

    await service.tick()
    updated = await store.get_rule(rule.rule_id)
    executions = await store.list_executions(rule.rule_id)

    assert updated.state == "fired"
    assert updated.fired_session_id == executions[0].session_id
    assert len(session_service.created) == 1
    assert session_service.created[0]["launch"] is False
    assert session_service.created[0]["session_id"] == executions[0].session_id
    assert session_service.created[0]["permission_mode"] is PermissionPolicy.DANGER_FULL_ACCESS
    assert session_service.created[0]["auto_approve"] is True
    assert session_service.created[0]["sandbox_backend"] == "docker"
    assert len(session_service.resumed) == 1
    assert session_service.resumed[0]["user_input_metadata"]["idempotency_key"] == executions[0].fire_id
    assert session_service.resumed[0]["run_max_agent_turns_total"] == 4
    assert session_service.resumed[0]["run_max_total_tokens"] == 32_000
    assert session_service.resumed[0]["run_deadline_at"] is not None
    deadline = session_service.resumed[0]["run_deadline_at"]
    assert session_service.created[0]["run_deadline_at"] == deadline
    assert session_service.create_started_at <= deadline
    assert deadline <= session_service.create_started_at + timedelta(seconds=1800)
    assert session_service.resume_started_at <= deadline
    assert "计划触发：" in session_service.resumed[0]["task"]
    assert "检查构建失败并修复" in session_service.resumed[0]["task"]

    await service.stop()


@pytest.mark.asyncio
async def test_overlapping_ticks_dispatch_a_fire_once(tmp_path):
    store, session_service, service = await _new_service(tmp_path)
    session_service.create_delay_seconds = 0.15
    rule = await _rule(service)
    await store.transition(
        rule.rule_id, expected_version=rule.version, state="active",
        event_kind="test_due", patch={"trigger_at": datetime.now(UTC).isoformat()},
    )

    first_tick = asyncio.create_task(service.tick())
    for _ in range(500):
        if session_service.create_started_at is not None:
            break
        await asyncio.sleep(0.001)
    assert session_service.create_started_at is not None

    await service.tick()
    await first_tick

    executions = await store.list_executions(rule.rule_id)
    assert len(executions) == 1
    assert len(session_service.created) == 1
    assert len(session_service.resumed) == 1
    assert (await store.get_execution(executions[0].fire_id)).state == "fired"
    await service.stop()


@pytest.mark.asyncio
async def test_restart_recovers_same_dispatch_session_without_creating_another(tmp_path):
    store, session_service, _ = await _new_service(tmp_path)
    rule = await _rule(
        WatchtowerService(store=store, session_service=session_service, settings=_settings()),
    )
    now = datetime.now(UTC)
    armed = await store.transition(
        rule.rule_id, expected_version=rule.version, state="armed", event_kind="armed",
    )
    execution = await store.claim_fire(
        rule.rule_id, expected_version=armed.version,
        fire_id="stable-fire", session_id="stable-session", fire_at=now,
    )
    session_service.events[execution.session_id] = [
        SimpleNamespace(type="session/started", data={}, run_id=None),
    ]
    restarted = WatchtowerService(
        store=store, session_service=session_service, settings=_settings(),
    )

    await restarted.tick()

    assert session_service.created == []
    assert session_service.resumed[0]["session_id"] == "stable-session"
    assert (await store.get_execution("stable-fire")).state == "fired"
    assert (await store.get_rule(rule.rule_id)).fired_session_id == "stable-session"
    await restarted.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_events", "expected_state"),
    [
        ([MODEL_REQUEST, RUN_FAILED], "launch_failed"),
        ([MODEL_REQUEST, MODEL_COMPLETED, MODEL_REQUEST, RUN_FAILED], "fired"),
    ],
)
async def test_restart_classifies_429_by_first_model_request(
    tmp_path, model_events, expected_state,
):
    store, session_service, service = await _new_service(tmp_path)
    rule = await _rule(service)
    armed = await store.transition(
        rule.rule_id, expected_version=rule.version, state="armed", event_kind="armed",
    )
    execution = await store.claim_fire(
        rule.rule_id, expected_version=armed.version,
        fire_id="later-429", session_id="session-later-429", fire_at=datetime.now(UTC),
    )
    session_service.events[execution.session_id] = [
        SimpleNamespace(type=RUN_STARTED, data={}, run_id="run-later-429"),
        *(
            SimpleNamespace(
                type=event_type,
                data={"http_status": 429} if event_type == RUN_FAILED else {},
                run_id="run-later-429",
            )
            for event_type in model_events
        ),
    ]
    restarted = WatchtowerService(
        store=store, session_service=session_service, settings=_settings(),
    )

    await restarted.tick()

    updated = await store.get_rule(rule.rule_id)
    recovered = await store.get_execution(execution.fire_id)
    assert recovered.state == expected_state
    assert updated.state == expected_state
    assert updated.fired_session_id == (
        execution.session_id if expected_state == "fired" else None
    )
    await service.stop()
    await restarted.stop()


@pytest.mark.asyncio
async def test_restart_recovers_a_scheduled_input_already_written(tmp_path):
    store, session_service, service = await _new_service(tmp_path)
    rule = await _rule(service)
    rule = await store.transition(
        rule.rule_id, expected_version=rule.version, state="active",
        event_kind="test_due", patch={"trigger_at": datetime.now(UTC).isoformat()},
    )
    session_service.crash_after_user_message = True

    with pytest.raises(asyncio.CancelledError):
        await service.tick()
    execution = (await store.list_executions(rule.rule_id))[0]

    recovered = WatchtowerService(
        store=SqliteWatchtowerStore(tmp_path / "harness.db"),
        session_service=session_service,
        settings=_settings(),
    )
    await recovered.store.initialize()
    await recovered.tick()

    user_messages = [
        event for event in session_service.events[execution.session_id]
        if event.type == USER_MESSAGE
    ]
    updated = await recovered.store.get_rule(rule.rule_id)
    assert len(user_messages) == 1
    assert len(session_service.resumed) == 2
    assert all(
        call["user_input_metadata"]["idempotency_key"] == execution.fire_id
        for call in session_service.resumed
    )
    assert user_messages[0].data["idempotency_key"] == execution.fire_id
    assert updated.state == "fired"
    await service.stop()
    await recovered.stop()


@pytest.mark.asyncio
async def test_resuming_after_schedule_time_marks_rule_expired(tmp_path):
    store, _, service = await _new_service(tmp_path)
    rule = await _rule(service)
    planned_at = datetime.now(UTC) - timedelta(seconds=1)
    paused = await store.transition(
        rule.rule_id, expected_version=rule.version, state="paused",
        event_kind="paused", patch={"trigger_at": planned_at.isoformat()},
    )
    assert paused.state == "paused"

    updated = await service.resume(rule.rule_id)

    assert updated.state == "expired"
    assert updated.trigger_at == planned_at
    assert (await store.get_rule(rule.rule_id)).state == "expired"
    await service.stop()


@pytest.mark.asyncio
async def test_first_model_429_moves_execution_to_manual_retry(tmp_path):
    store, session_service, service = await _new_service(tmp_path)
    rule = await _rule(service)
    armed = await store.transition(
        rule.rule_id, expected_version=rule.version, state="armed", event_kind="armed",
    )
    execution = await store.claim_fire(
        rule.rule_id, expected_version=armed.version,
        fire_id="fire-429", session_id="session-429", fire_at=datetime.now(UTC),
    )
    await store.finish_fire(execution.fire_id, state="fired")
    session_service.subscriber.queue.put_nowait(
        SimpleNamespace(type="model/request", data={}, seq=1),
    )
    session_service.subscriber.queue.put_nowait(
        SimpleNamespace(type=RUN_FAILED, data={"http_status": 429}, seq=2),
    )
    session_service.subscriber.queue.put_nowait(session_service.run.DONE)

    await service._monitor_run(
        session_service.run, session_service.subscriber,
        rule.rule_id, execution.fire_id, launch_failure_eligible=True,
    )

    updated = await store.get_rule(rule.rule_id)
    failed = await store.get_execution(execution.fire_id)
    assert failed.state == "launch_failed"
    assert updated.state == "launch_failed"
    assert updated.fired_session_id is None
    assert "未自动顺延" in updated.last_error
    assert session_service.run.unsubscribed


@pytest.mark.asyncio
async def test_constraint_wait_timeout_resumes_with_stop_instruction(tmp_path, monkeypatch):
    store, session_service, service = await _new_service(tmp_path)
    rule = await _rule(service)
    armed = await store.transition(
        rule.rule_id, expected_version=rule.version, state="armed", event_kind="armed",
    )
    execution = await store.claim_fire(
        rule.rule_id, expected_version=armed.version,
        fire_id="fire-timeout", session_id="session-timeout", fire_at=datetime.now(UTC),
    )
    await store.finish_fire(execution.fire_id, state="fired")
    request_id = "request-1"
    session_service.events[execution.session_id] = [
        SimpleNamespace(type=RUN_STARTED, data={}, run_id="run-constraint-timeout"),
        SimpleNamespace(
            type=USER_INPUT_REQUESTED,
            data={"request_id": request_id, "kind": "protected_fact_conflict"},
            time=(datetime.now(UTC) - timedelta(seconds=301)).isoformat(),
            run_id="run-constraint-timeout",
        ),
    ]
    monkeypatch.setattr(
        "agent_harness.watchtower.service.latest_paused_run",
        lambda _events: SimpleNamespace(
            reason="user_input", input_request_id=request_id,
            run_id="run-constraint-timeout", version=3,
        ),
    )

    await service._expire_user_waits()

    updated = await store.get_rule(rule.rule_id)

    assert updated.last_timed_out_request_id == "request-1"
    assert updated.last_notice == WAIT_TIMEOUT_MESSAGE
    assert updated.state == "fired"
    assert session_service.resumed[-1] == {
        "session_id": "session-timeout",
        "resume_run_id": "run-constraint-timeout",
        "expected_version": 3,
        "resume_basis": "user_input",
        "input_request": {"request_id": request_id, "timeout": True},
    }
    assert "choice" not in session_service.resumed[-1]["input_request"]


@pytest.mark.asyncio
async def test_constraint_timeout_notice_is_reconciled_after_restart(tmp_path):
    store, session_service, service = await _new_service(tmp_path)
    rule = await _rule(service)
    armed = await store.transition(
        rule.rule_id, expected_version=rule.version, state="armed", event_kind="armed",
    )
    execution = await store.claim_fire(
        rule.rule_id, expected_version=armed.version,
        fire_id="fire-timeout-reconcile", session_id="session-timeout-reconcile",
        fire_at=datetime.now(UTC),
    )
    await store.finish_fire(execution.fire_id, state="fired")
    request_id = "request-timeout-reconcile"
    session_service.events[execution.session_id] = [
        SimpleNamespace(
            type=USER_INPUT_REQUESTED,
            data={"request_id": request_id, "kind": "protected_fact_conflict"},
            run_id="run-timeout-reconcile",
        ),
        SimpleNamespace(
            type=USER_MESSAGE,
            data={
                "content": "约束冲突仍未解决",
                "input_request_id": request_id,
                "input_request_answer": {"request_id": request_id, "timeout": True},
            },
            run_id="run-timeout-reconcile",
        ),
    ]

    await service._expire_user_waits()

    updated = await store.get_rule(rule.rule_id)
    assert updated.last_timed_out_request_id == request_id
    assert updated.last_notice == WAIT_TIMEOUT_MESSAGE
    assert session_service.resumed == []


@pytest.mark.asyncio
async def test_ask_user_timeout_resumes_the_same_run_without_an_answer(tmp_path, monkeypatch):
    store, session_service, service = await _new_service(tmp_path)
    rule = await _rule(service)
    armed = await store.transition(
        rule.rule_id, expected_version=rule.version, state="armed", event_kind="armed",
    )
    execution = await store.claim_fire(
        rule.rule_id, expected_version=armed.version,
        fire_id="fire-ask-timeout", session_id="session-ask-timeout",
        fire_at=datetime.now(UTC),
    )
    await store.finish_fire(execution.fire_id, state="fired")
    request_id = "request-ask-timeout"
    session_service.events[execution.session_id] = [
        SimpleNamespace(type=RUN_STARTED, data={}, run_id="run-ask-timeout"),
        SimpleNamespace(
            type=USER_INPUT_REQUESTED,
            data={"request_id": request_id, "kind": "ask_user", "timeout_seconds": 1},
            time=(datetime.now(UTC) - timedelta(seconds=2)).isoformat(),
            run_id="run-ask-timeout",
        ),
    ]
    monkeypatch.setattr(
        "agent_harness.watchtower.service.latest_paused_run",
        lambda _events: SimpleNamespace(
            reason="user_input", input_request_id=request_id,
            run_id="run-ask-timeout", version=7,
        ),
    )

    await service._expire_user_waits()

    assert session_service.resumed[-1] == {
        "session_id": "session-ask-timeout",
        "resume_run_id": "run-ask-timeout",
        "expected_version": 7,
        "resume_basis": "user_input",
        "input_request": {"request_id": request_id, "timeout": True},
    }
    await service.stop()


@pytest.mark.asyncio
async def test_serve_adopts_a_run_resumed_after_user_wait_without_browser(tmp_path, monkeypatch):
    store, session_service, service = await _new_service(tmp_path)
    rule = await _rule(service)
    armed = await store.transition(
        rule.rule_id, expected_version=rule.version, state="armed", event_kind="armed",
    )
    execution = await store.claim_fire(
        rule.rule_id, expected_version=armed.version,
        fire_id="fire-resumed-run", session_id="session-resumed-run",
        fire_at=datetime.now(UTC),
    )
    await store.finish_fire(execution.fire_id, state="fired")
    session_service.events[execution.session_id] = [
        SimpleNamespace(type=RUN_STARTED, data={}, run_id="run-after-wait"),
    ]
    monkeypatch.setattr(
        "agent_harness.watchtower.service.latest_paused_run", lambda _events: None,
    )

    delay = await service._seconds_until_next_action()
    assert 0 < delay <= 30

    session_service.active = True
    await service.tick()
    assert execution.fire_id in service._monitored_fire_ids

    session_service.subscriber.queue.put_nowait(session_service.run.DONE)
    await asyncio.gather(*tuple(service._monitors))
    assert execution.fire_id not in service._monitored_fire_ids
    assert session_service.run.unsubscribed
    await service.stop()
