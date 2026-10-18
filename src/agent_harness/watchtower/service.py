"""Serve-owned one-shot Watchtower scheduler."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_harness.session.amend import AmendOptions
from agent_harness.session.ask_user_timeout import latest_pending_input_request
from agent_harness.session.event import (
    MODEL_COMPLETED,
    MODEL_REQUEST,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_STARTED,
    USER_INPUT_REQUESTED,
    USER_MESSAGE,
)
from agent_harness.session.service import (
    REASON_USER_INPUT,
    RESUME_BASIS_USER_INPUT,
    ActiveRunConflict,
    BudgetConflict,
    BudgetRejection,
    SessionNotFound,
    latest_paused_run,
)
from agent_harness.tooling.contract import PermissionPolicy
from agent_harness.watchtower.models import WatchtowerExecution, WatchtowerRule
from agent_harness.watchtower.schedule import ScheduledRule, evaluate_schedule
from agent_harness.watchtower.store import SqliteWatchtowerStore

logger = logging.getLogger("agent_harness.watchtower")

CONSENT_VERSION = "watchtower-consent-v1"
GRACE_WINDOW = timedelta(minutes=5)
MONITOR_RECOVERY_POLL_SECONDS = 30.0
WAIT_TIMEOUT_MESSAGE = (
    "约束冲突等待已超时。未选择或授权任何选项；run 已恢复并收到停止受冲突影响工作的指令。"
)
TERMINAL_RUN_EVENTS = {RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED}


def _failed_after_first_request_429(events: list[Any]) -> bool:
    """A failed first provider attempt requires manual retry; a later 429 does not."""
    first_request_seen = False
    first_request_status: int | None = None
    model_requests = 0
    model_completed = False
    for event in events:
        if event.type == MODEL_REQUEST:
            model_requests += 1
            if not first_request_seen:
                first_request_seen = True
                status = event.data.get("http_status")
                first_request_status = status if isinstance(status, int) else None
        elif event.type == MODEL_COMPLETED:
            model_completed = True
        elif event.type == RUN_FAILED:
            return _first_request_429_requires_retry(
                first_request_status=first_request_status,
                model_requests=model_requests,
                model_completed=model_completed,
                failure_status=event.data.get("http_status"),
            )
    return False


def _first_request_429_requires_retry(
    *, first_request_status: int | None, model_requests: int,
    model_completed: bool, failure_status: Any,
) -> bool:
    """Apply AC11, retaining the old single-request event shape for in-flight fires."""
    return not model_completed and (
        first_request_status == 429
        or (
            first_request_status is None
            and model_requests == 1
            and failure_status == 429
        )
    )


def absolute_trigger(local_time: str, timezone: str) -> datetime:
    """Resolve a local wall time once, rejecting DST gaps and choosing the earlier fold."""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("timezone must be a valid IANA time zone") from error
    try:
        local = datetime.fromisoformat(local_time)
    except ValueError as error:
        raise ValueError("local_time must be an ISO local date and time") from error
    if local.tzinfo is not None:
        raise ValueError("local_time must not include a UTC offset")
    candidate = local.replace(tzinfo=zone, fold=0)
    if candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local:
        raise ValueError("local_time falls inside a daylight-saving time gap")
    return candidate.astimezone(UTC)


def scheduled_prompt(rule: WatchtowerRule, fire_at: datetime, now: datetime) -> str:
    planned = fire_at.astimezone(ZoneInfo(rule.timezone)).isoformat()
    actual = now.astimezone(ZoneInfo(rule.timezone)).isoformat()
    title = " ".join(rule.prompt.split())[:80]
    return (
        f"[自动值班触发]\n规则：{title}\n计划触发：{planned}\n实际触发：{actual}\n\n"
        f"{rule.prompt}"
    )


class WatchtowerService:
    """Owns Watchtower rule transitions and the single serve timer."""

    def __init__(self, *, store: SqliteWatchtowerStore, session_service: Any, settings: Any) -> None:
        self.store = store
        self.session_service = session_service
        self.settings = settings
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task | None = None
        self._monitors: set[asyncio.Task] = set()
        self._monitored_fire_ids: set[str] = set()
        self._last_tick_at: datetime | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        await self.store.initialize()
        self._stop.clear()
        await self.tick()
        self._scheduler_task = asyncio.create_task(
            self._run_timer(), name="watchtower-scheduler",
        )

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._scheduler_task is not None:
            await self._scheduler_task
            self._scheduler_task = None
        for task in tuple(self._monitors):
            task.cancel()
        if self._monitors:
            await asyncio.gather(*self._monitors, return_exceptions=True)
        self._monitors.clear()

    @property
    def running(self) -> bool:
        return self._scheduler_task is not None and not self._scheduler_task.done()

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "single_instance": True,
            "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
            "error": self._last_error,
            "consent_version": CONSENT_VERSION,
        }

    async def create_rule(
        self,
        *,
        prompt: str,
        trigger_kind: str,
        timezone: str,
        delay_seconds: int | None,
        local_time: str | None,
        model: str | None,
        sandbox_backend: str | None,
        cwd: str | None,
        budget_turns: int | None,
        budget_tokens: int | None,
        budget_duration_seconds: int | None,
        consent_acknowledged: bool,
        consent_version: str,
    ) -> WatchtowerRule:
        if not consent_acknowledged or consent_version != CONSENT_VERSION:
            raise ValueError("当前值班危险权限知情同意必须由用户明确确认")
        if not prompt.strip():
            raise ValueError("prompt must not be blank")
        now = datetime.now(UTC)
        if trigger_kind == "relative":
            if delay_seconds is None or local_time is not None:
                raise ValueError("relative rules require delay_seconds only")
            trigger_at = now + timedelta(seconds=delay_seconds)
        elif trigger_kind == "absolute":
            if local_time is None or delay_seconds is not None:
                raise ValueError("absolute rules require local_time only")
            trigger_at = absolute_trigger(local_time, timezone)
        else:
            raise ValueError("trigger_kind must be relative or absolute")
        if trigger_at <= now:
            raise ValueError("trigger time must be in the future")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must be a valid IANA time zone") from error
        rule = WatchtowerRule(
            rule_id=str(uuid4()), prompt=prompt.strip(), trigger_kind=trigger_kind,
            trigger_at=trigger_at, timezone=timezone, local_time=local_time,
            delay_seconds=delay_seconds, model=model, sandbox_backend=sandbox_backend,
            cwd=cwd, budget_turns=budget_turns, budget_tokens=budget_tokens,
            budget_duration_seconds=budget_duration_seconds,
            consent_version=CONSENT_VERSION, consented_at=now,
            state="active", created_at=now, updated_at=now,
        )
        created = await self.store.create_rule(rule)
        self.wake()
        return created

    async def pause(self, rule_id: str) -> WatchtowerRule | None:
        rule = await self.store.get_rule(rule_id)
        if rule is None or rule.state not in {"active", "armed"}:
            return None
        updated = await self.store.transition(
            rule_id, expected_version=rule.version, state="paused",
            event_kind="paused", patch={"paused_at": datetime.now(UTC).isoformat()},
        )
        self.wake()
        return updated

    async def resume(self, rule_id: str) -> WatchtowerRule | None:
        rule = await self.store.get_rule(rule_id)
        if rule is None or rule.state != "paused":
            return None
        now = datetime.now(UTC)
        if now >= rule.trigger_at.astimezone(UTC):
            updated = await self.store.transition(
                rule_id, expected_version=rule.version, state="expired",
                event_kind="expired",
            )
            self.wake()
            return updated
        trigger_at = (
            now + timedelta(seconds=rule.delay_seconds)
            if rule.trigger_kind == "relative" and rule.delay_seconds is not None
            else rule.trigger_at
        )
        updated = await self.store.transition(
            rule_id, expected_version=rule.version, state="active",
            event_kind="resumed",
            patch={"trigger_at": trigger_at.isoformat(), "paused_at": None},
        )
        self.wake()
        return updated

    async def archive(self, rule_id: str) -> WatchtowerRule | None:
        rule = await self.store.get_rule(rule_id)
        if rule is None or rule.state == "dispatching":
            return None
        updated = await self.store.transition(
            rule_id, expected_version=rule.version, state="archived",
            event_kind="archived",
        )
        self.wake()
        return updated

    async def reschedule(
        self, rule_id: str, *, local_time: str, timezone: str,
    ) -> WatchtowerRule | None:
        rule = await self.store.get_rule(rule_id)
        if rule is None or rule.state != "expired":
            return None
        trigger_at = absolute_trigger(local_time, timezone)
        if trigger_at <= datetime.now(UTC):
            raise ValueError("new trigger time must be in the future")
        updated = await self.store.transition(
            rule_id, expected_version=rule.version, state="active",
            event_kind="rescheduled",
            patch={
                "trigger_kind": "absolute", "trigger_at": trigger_at.isoformat(),
                "timezone": timezone, "local_time": local_time, "delay_seconds": None,
                "last_error": None, "last_notice": None,
                "last_timed_out_request_id": None,
            },
        )
        self.wake()
        return updated

    async def retry_now(self, rule_id: str) -> WatchtowerRule | None:
        rule = await self.store.get_rule(rule_id)
        if rule is None or rule.state != "launch_failed":
            return None
        now = datetime.now(UTC)
        updated = await self.store.transition(
            rule_id, expected_version=rule.version, state="armed",
            event_kind="retry_requested",
            patch={
                "trigger_at": now.isoformat(), "last_error": None,
                "last_notice": None, "last_timed_out_request_id": None,
            },
        )
        self.wake()
        if updated is not None:
            await self.tick()
            return await self.store.get_rule(rule_id)
        return None

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> None:
        """Serialize timer and explicit wake-up ticks."""
        async with self._tick_lock:
            await self._tick_locked()

    async def _tick_locked(self) -> None:
        """Reconcile durable intents, execute due rules serially, then time out waits."""
        self._last_tick_at = datetime.now(UTC)
        self._last_error = None
        for rule, execution in await self.store.list_dispatching():
            await self._dispatch(rule, execution)

        rules = await self.store.list_rules()
        now = datetime.now(UTC)
        decision = evaluate_schedule(
            [ScheduledRule(rule.rule_id, rule.trigger_at, rule.state)
             for rule in rules if rule.state in {"active", "armed", "paused"}],
            now, grace=GRACE_WINDOW,
        )
        for due_rule in decision.expired:
            rule = await self.store.get_rule(due_rule.rule_id)
            if rule is not None and rule.state in {"active", "armed", "paused"}:
                await self.store.transition(
                    rule.rule_id, expected_version=rule.version, state="expired",
                    event_kind="expired",
                )
        for due_rule in decision.due:
            rule = await self.store.get_rule(due_rule.rule_id)
            if rule is None or rule.state not in {"active", "armed"}:
                continue
            if rule.state == "active":
                rule = await self.store.transition(
                    rule.rule_id, expected_version=rule.version, state="armed",
                    event_kind="armed",
                )
            if rule is None:
                continue
            fire_id, session_id, fire_at = str(uuid4()), str(uuid4()), rule.trigger_at
            execution = await self.store.claim_fire(
                rule.rule_id, expected_version=rule.version,
                fire_id=fire_id, session_id=session_id, fire_at=fire_at,
            )
            if execution is not None:
                await self._dispatch(rule, execution)

        await self._expire_user_waits()
        await self._attach_existing_run_monitors()

    async def _run_timer(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            delay = await self._seconds_until_next_action()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass
            if not self._stop.is_set():
                try:
                    await self.tick()
                except Exception as error:
                    self._last_error = type(error).__name__
                    logger.exception("watchtower scheduler tick failed")

    async def _seconds_until_next_action(self) -> float:
        now = datetime.now(UTC)
        deadlines: list[datetime] = []
        for rule in await self.store.list_rules():
            if rule.state in {"active", "armed", "paused"}:
                deadlines.append(rule.trigger_at)
        for rule, execution in await self.store.list_fired():
            try:
                events = await self.session_service.get_events(execution.session_id)
            except SessionNotFound:
                continue
            request = latest_pending_input_request(events)
            if request is not None and request.data.get("request_id") != rule.last_timed_out_request_id:
                timeout = request.data.get("timeout_seconds")
                if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                    timeout = self.settings.ask_user_question_timeout
                deadlines.append(_event_datetime(request.time) + timedelta(seconds=timeout))
            if execution.fire_id not in self._monitored_fire_ids:
                latest_start = next(
                    (event for event in reversed(events) if event.type == RUN_STARTED),
                    None,
                )
                paused = latest_paused_run(events)
                if (
                    latest_start is not None
                    and (paused is None or paused.reason == REASON_USER_INPUT)
                    and not any(
                        event.run_id == latest_start.run_id
                        and event.type in TERMINAL_RUN_EVENTS
                        for event in events
                    )
                ):
                    # A human can answer a durable wait and close the browser. Poll
                    # before the orphan-reap grace expires so serve can adopt the
                    # resumed run with its own subscriber.
                    deadlines.append(
                        now + timedelta(seconds=MONITOR_RECOVERY_POLL_SECONDS),
                    )
        if not deadlines:
            return 3600.0
        return max(0.05, min(3600.0, (min(deadlines) - now).total_seconds()))

    async def _dispatch(self, rule: WatchtowerRule, execution: WatchtowerExecution) -> None:
        try:
            if (
                rule.consent_version != CONSENT_VERSION
                or rule.consented_at.tzinfo is None
                or not rule.prompt.strip()
            ):
                raise PermissionError("持久化的值班知情同意无效，拒绝启动")
            try:
                events = await self.session_service.get_events(execution.session_id)
            except SessionNotFound:
                # The dispatch intent preallocates the stable Session ID before
                # creation; a missing session is the normal first-attempt state.
                events = []
            if events:
                starts = [event for event in events if event.type == RUN_STARTED]
                if starts:
                    if _failed_after_first_request_429(events):
                        await self.store.finish_fire(
                            execution.fire_id, state="launch_failed",
                            error="首个模型请求返回 HTTP 429；未自动顺延。",
                        )
                    else:
                        await self.store.finish_fire(execution.fire_id, state="fired")
                    return

            # Use the durable dispatch-intent timestamp so a retry after restart
            # emits the identical user message and the partial-write guard can
            # recognize it before attempting to launch the same fire twice.
            prompt = scheduled_prompt(
                rule, execution.fire_at, execution.created_at,
            )
            if not events and rule.budget_duration_seconds is not None:
                execution = await self.store.set_deadline(
                    execution.fire_id,
                    duration_seconds=rule.budget_duration_seconds,
                )
            if not events:
                await self.session_service.create_and_launch(
                    task=rule.prompt, cwd=rule.cwd,
                    permission_mode=PermissionPolicy.DANGER_FULL_ACCESS,
                    permission_mode_explicit=True, auto_approve=True,
                    auto_approve_explicit=True, amend=AmendOptions(model=rule.model),
                    sandbox_backend=rule.sandbox_backend, launch=False,
                    run_deadline_at=execution.deadline_at,
                    session_id=execution.session_id,
                )
                events = await self.session_service.get_events(execution.session_id)
            if (
                any(
                    event.type == USER_MESSAGE and event.data.get("content") == prompt
                    for event in events
                )
                and not any(
                    event.type == USER_MESSAGE
                    and event.data.get("idempotency_key") == execution.fire_id
                    for event in events
                )
            ):
                raise RuntimeError("触发输入已存在但缺少幂等标识，拒绝重复执行。")
            # A prior process may have died after the durable message was
            # appended but before run/started. AgentRuntime uses the fire ID
            # as an idempotency key and will launch that same input once.
            result = await self.session_service.resume_and_launch(
                session_id=execution.session_id,
                task=prompt,
                user_input_metadata={"idempotency_key": execution.fire_id},
                amend=AmendOptions(model=rule.model),
                run_max_agent_turns_total=rule.budget_turns,
                run_max_total_tokens=rule.budget_tokens,
                run_deadline_at=execution.deadline_at,
            )
            await self.store.finish_fire(execution.fire_id, state="fired")
            self._monitor(result, rule.rule_id, execution.fire_id, launch_failure_eligible=True)
        except Exception as error:  # noqa: BLE001 - record one-shot failure; manual retry
            try:
                events = await self.session_service.get_events(execution.session_id)
            except Exception:  # noqa: BLE001
                events = []
            if any(event.type == RUN_STARTED for event in events):
                await self.store.finish_fire(execution.fire_id, state="fired")
                return
            message = _safe_error(error)
            await self.store.finish_fire(
                execution.fire_id, state="launch_failed", error=message,
            )
            logger.warning(
                "watchtower fire failed (rule=%s fire=%s): %s",
                rule.rule_id, execution.fire_id, type(error).__name__,
            )

    def _monitor(
        self, result: Any, rule_id: str, fire_id: str, *, launch_failure_eligible: bool,
    ) -> None:
        if (
            result.run is None or result.subscriber is None
            or fire_id in self._monitored_fire_ids
        ):
            return
        self._monitored_fire_ids.add(fire_id)
        task = asyncio.create_task(
            self._monitor_run(
                result.run, result.subscriber, rule_id, fire_id,
                launch_failure_eligible=launch_failure_eligible,
            ),
            name=f"watchtower-run-{fire_id}",
        )
        self._monitors.add(task)
        task.add_done_callback(self._monitors.discard)

    async def _monitor_run(
        self, run: Any, subscriber: Any, rule_id: str, fire_id: str,
        *, launch_failure_eligible: bool,
    ) -> None:
        model_requests = 0
        first_request_status: int | None = None
        model_completed = False
        try:
            while True:
                event = await subscriber.queue.get()
                if event is run.DONE:
                    return
                if event.type == USER_INPUT_REQUESTED:
                    self.wake()
                elif event.type == MODEL_REQUEST:
                    model_requests += 1
                    if model_requests == 1:
                        status = event.data.get("http_status")
                        first_request_status = status if isinstance(status, int) else None
                elif event.type == MODEL_COMPLETED:
                    model_completed = True
                elif event.type == RUN_FAILED:
                    if launch_failure_eligible and _first_request_429_requires_retry(
                        first_request_status=first_request_status,
                        model_requests=model_requests,
                        model_completed=model_completed,
                        failure_status=event.data.get("http_status"),
                    ):
                        await self.store.finish_fire(
                            fire_id, state="launch_failed",
                            error="首个模型请求返回 HTTP 429；未自动顺延。",
                        )
                    return
                elif event.type in TERMINAL_RUN_EVENTS:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("watchtower run monitor failed (fire=%s)", fire_id)
        finally:
            try:
                run.unsubscribe(subscriber)
            finally:
                self._monitored_fire_ids.discard(fire_id)
                self.wake()


    async def _attach_existing_run_monitors(self) -> None:
        """Adopt live runs resumed by a user after an earlier wait paused them."""
        for rule, execution in await self.store.list_fired():
            if execution.fire_id in self._monitored_fire_ids:
                continue
            try:
                handle = await self.session_service.stream_reconnect(
                    session_id=execution.session_id,
                )
            except SessionNotFound:
                continue
            self._monitor(
                handle, rule.rule_id, execution.fire_id,
                launch_failure_eligible=False,
            )

    async def _expire_user_waits(self) -> None:
        for rule, execution in await self.store.list_fired():
            try:
                events = await self.session_service.get_events(execution.session_id)
            except SessionNotFound:
                continue
            timeout_answers = [
                event.data["input_request_answer"].get("request_id")
                for event in events
                if event.type == USER_MESSAGE
                and isinstance(event.data.get("input_request_answer"), dict)
                and event.data["input_request_answer"].get("timeout") is True
            ]
            for timed_out_id in reversed(timeout_answers):
                timed_out_request = next(
                    (
                        event for event in reversed(events)
                        if event.type == USER_INPUT_REQUESTED
                        and event.data.get("request_id") == timed_out_id
                    ),
                    None,
                )
                if (
                    timed_out_request is not None
                    and timed_out_request.data.get("kind") == "protected_fact_conflict"
                ):
                    if rule.last_timed_out_request_id != timed_out_id:
                        await self.store.set_notice(
                            rule.rule_id, fire_id=execution.fire_id,
                            request_id=timed_out_id, notice=WAIT_TIMEOUT_MESSAGE,
                        )
                    break
            request = latest_pending_input_request(events)
            if request is None:
                continue
            request_id = request.data.get("request_id")
            if not isinstance(request_id, str) or request_id == rule.last_timed_out_request_id:
                continue
            timeout = request.data.get("timeout_seconds")
            if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
                timeout = self.settings.ask_user_question_timeout
            if datetime.now(UTC) < _event_datetime(request.time) + timedelta(seconds=timeout):
                continue
            if request.data.get("kind") == "ask_user":
                paused = latest_paused_run(events)
                if (
                    paused is None or paused.reason != REASON_USER_INPUT
                    or paused.input_request_id != request_id
                ):
                    continue
                try:
                    result = await self.session_service.resume_and_launch(
                        session_id=execution.session_id,
                        resume_run_id=paused.run_id,
                        expected_version=paused.version,
                        resume_basis=RESUME_BASIS_USER_INPUT,
                        input_request={"request_id": request_id, "timeout": True},
                    )
                except (BudgetConflict, BudgetRejection, ActiveRunConflict):
                    continue
                self._monitor(
                    result, rule.rule_id, execution.fire_id,
                    launch_failure_eligible=False,
                )
            elif request.data.get("kind") == "protected_fact_conflict":
                paused = latest_paused_run(events)
                if (
                    paused is None or paused.reason != REASON_USER_INPUT
                    or paused.input_request_id != request_id
                ):
                    continue
                try:
                    result = await self.session_service.resume_and_launch(
                        session_id=execution.session_id,
                        resume_run_id=paused.run_id,
                        expected_version=paused.version,
                        resume_basis=RESUME_BASIS_USER_INPUT,
                        input_request={"request_id": request_id, "timeout": True},
                    )
                except (BudgetConflict, BudgetRejection, ActiveRunConflict):
                    continue
                await self.store.set_notice(
                    rule.rule_id, fire_id=execution.fire_id,
                    request_id=request_id, notice=WAIT_TIMEOUT_MESSAGE,
                )
                self._monitor(
                    result, rule.rule_id, execution.fire_id,
                    launch_failure_eligible=False,
                )
def _event_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_error(error: Exception) -> str:
    if isinstance(error, PermissionError):
        return str(error)
    return f"{type(error).__name__}；未自动顺延，可手动立即重跑。"
