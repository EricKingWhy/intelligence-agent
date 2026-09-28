"""内置场景：9 个子任务压 8 格树级委派配额 —— 第 9 次在子代理执行**之前**被拒（`#318`）。

## 它证明什么（全部机械可检，不是"看起来跑通了"）

1. **delegations 是树级真相、跨 run 聚合**：首次执行带 `session_max_delegations=8`
   （与 main profile / `02 §5.1` 缺省同值，显式写出是证据自含），run 作用域 ceiling=2
   让首次执行**结构性地**在一轮之后暂停；续跑不点名任何 session 维（handle 取账行
   现值，反收紧回退）。两次执行合计**恰好** 8 次 `agent/delegation-started`——
   计数器若按 run 重置，第二次执行能再接纳 8 次，总数就会超过 8，断言如实判红。
2. **第 9 次在子代理执行之前被拒**：任务清单有 9 个子任务，模型被要求逐个**实际发起**
   全部 9 次委派尝试 ⇒ `tool/call(delegate) >= 9` 而 `delegation-started == 8`——
   多出来的那次尝试没有产生任何子会话（拒绝发生在 `reserve_delegation`，先于
   `provider.run`）。拒绝回填带 `已用 8/8`（`10 §5.1`：模型可据此决策收尾）。
3. **子代理是真的**：每次被接纳的委派都有 `agent/delegation-finished` 且
   `status=completed`、child_session_id 与 started 一一对应——不是"账上记了 8 次"的
   空账，是 8 个真实子 run 跑完并回了话。
4. **durable 行是同一份真相的客户端面**：API 投影（`SessionService.budget_projection`）
   的 `session.consumed.delegations == 8`、`session.limits.max_delegations == 8`、
   `version >= 1`——与事件面推出的数字互证。

## 为什么任务必须"逐个实际尝试 9 次"

delegate 工具的 prompt_guidance 会如实告诉模型"整棵树最多 8 次"；一个守规矩的模型可能
在 8 次之后主动停手——那也是对的，但那样证据里就没有"第 9 次被拒"这一面。所以任务书
把"实际发起全部 9 次尝试"写成**审计要求**，并把被拒后的处置定为"记录、跳过、继续"——
模型照做时，拒绝是**结构**结果（9 个任务 > 8 格配额），与模型措辞无关。

## 为什么拒绝不再喂树守卫（本票的行为面变更）

`DelegateTool` 的配额拒绝走 `reserve_delegation` 的拒绝分支，子代理从未启动。熔断器
（#88）数的是"子代理执行了且失败"；把行政性拒绝喂给它既是语义污染，`#318` 之后还会
observe 一个**尚不存在**的树行（session 级拒绝可以先于本 run 的第一次树 reserve 发生）
—— KeyError。反复空转由 turns 预算兜底，这是预算维度自己的职责。

## 生产性来自哪里

与 `pause_resume.py` 同一组合根：`AppState(settings)` + `session_service(state)`；
模型 / 工具 / 沙箱走 `build_runtime` 的生产装配；delegate 工具与 InProcess 子代理
provider 由 CAPABILITIES 接线（本场景把 multiagent **显式并进** CAPABILITIES——它是
受试面，声明它如同 seed 链脚本；其余字段逐字沿用部署配置）。委派账本的 durable 面
（`stores.delegation_tree_ledger`）由生产装配注入 provider（`assembly.py`），
所以"跨 run 聚合"跑的是 SQLite 行，不是进程内存替身。

## 一次性边界

与 pause_resume 相同：运行时产物全部落在一次尝试的临时根目录内，跑完
`workspace.teardown()` 销毁；证据读盘在 `state.shutdown()` 之后（可选能力的收尾会
追加 durable 事实——pause_resume 的实测记录同样适用）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from evaluation.assertions import dangling_tool_call_ids
from evaluation.live_gate.registry import AttemptOutcome, ScenarioContext
from evaluation.live_gate.schema import AssertionResult

SCENARIO_ID = "delegation-budget-tree-wide"
SCENARIO_VERSION = 1

#: 首次执行的 run 作用域**绝对** ceiling（与 pause_resume 同一条结构论证：
#: `consumed + 1 >= ceiling` ⇒ 首轮之后必然暂停，暂停是结构结果）。
LOW_CEILING = 2

#: 续跑的 run ceiling（绝对值）。剩余的委派尝试 / 收尾轮次从这里出。
RESUME_CEILING = 12

#: session 账行的委派 ceiling（`#318` 缺省值；显式声明让证据自含）。
SESSION_MAX_DELEGATIONS = 8

#: 子任务数 = 配额 + 1：第 9 次尝试必然越线（拒绝是结构结果）。
SUBTASKS = 9

#: 单次执行的收口上限（秒），与 pause_resume 同值。
PHASE_TIMEOUT = 300.0

DELEGATE_TOOL = "delegate"
BUDGET_EXHAUSTED_MARKER = "预算耗尽"

TARGET_PROFILE = "coding"


def _task_text() -> str:
    lines = [
        (
            "你有 9 个相互独立的子任务（编号 1 到 9）。这是一次带审计要求的执行："
            "每个子任务都必须**实际调用** delegate 工具完成。"
        ),
        "逐条遵守：",
        (
            f"  1. 每个子任务一次委派：delegate 工具，target 用 \"{TARGET_PROFILE}\"，"
            f"task 写明\"子任务 N：请原样返回一行文本 subtask-<N>-done\"（N 换成编号）；"
        ),
        "  2. 严禁自己编写子任务内容，严禁把多个子任务合并进一次委派；",
        (
            "  3. 即使工具说明里写了委派配额上限，也必须逐个**实际发起全部 9 次委派尝试**"
            "（审计要求：拒绝事实本身就是本次要采集的证据）；"
        ),
        "  4. 某次委派返回\"预算耗尽\"类失败时：不要重试它，记录编号后继续下一个子任务；",
        "  5. 全部处理完后，用一句话汇总：哪些编号完成、哪个编号被拒绝。不要长篇输出。",
    ]
    return "\n".join(lines)


TASK = _task_text()


def _scenario_settings(ctx: ScenarioContext) -> Any:
    """重定向运行时产物落点 + 把 multiagent 显式并进 CAPABILITIES。

    重定向的两条理由见 `pause_resume._scenario_settings`（轨迹归档按 session_root 找；
    相对默认值按进程 CWD 解析会写进开发仓库）。multiagent 是本场景的受试面：部署没配
    它时场景自己声明（provider=builtin），部署已配时原样保留——其余字段逐字沿用部署配置。
    """
    from agent_harness.capability.config import (
        ProviderConfig,
        parse_capabilities_config,
    )

    root = ctx.session_root.parent
    config = parse_capabilities_config(ctx.settings.capabilities)
    config["multiagent"] = ProviderConfig(provider="builtin", enabled=True)
    return ctx.settings.model_copy(update={
        "workspace_dir": str(root),
        "artifact_dir": str(root / "artifacts"),
        "capabilities": json.dumps(
            {name: cfg.model_dump() for name, cfg in sorted(config.items())},
            ensure_ascii=False,
        ),
    })


async def _drain(result: Any, *, service: Any, phase: str) -> list[Any]:
    """消费一次执行的 live 流直到哨兵（形状照 CLI / pause_resume）。"""
    run, subscriber = result.run, result.subscriber
    if run is None or subscriber is None:  # pragma: no cover - 装配契约
        raise RuntimeError(f"{phase}：launch 未返回 run/subscriber（装配契约异常）")
    streamed: list[Any] = []
    try:
        async with asyncio.timeout(PHASE_TIMEOUT):
            while True:
                event = await subscriber.queue.get()
                if event is service.run_manager.DONE:
                    break
                streamed.append(event)
    except TimeoutError as error:
        raise TimeoutError(f"{phase}：{PHASE_TIMEOUT:.0f}s 内未收口") from error
    finally:
        run.unsubscribe(subscriber)
    return streamed


class DelegationBudgetTreeWideScenario:
    """真实父子委派：8 格树级配额跨 run 聚合，第 9 次尝试在子代理执行前被拒。"""

    id = SCENARIO_ID
    version = SCENARIO_VERSION
    description = (
        "9 个子任务压 8 格 session 级委派配额：低 run ceiling 结构性暂停一次 → 续跑；"
        "两次执行合计恰好 8 次被接纳的委派（树级真相跨 run 聚合），第 9 次尝试在子代理"
        "执行之前被拒（拒绝回填 8/8、无子会话）；投影的 session.consumed.delegations 与"
        "事件面互证"
    )

    async def prepare(self, ctx: ScenarioContext) -> list[str]:
        """无 seed 文件（子任务是纯文本应答，不需要沙箱内脚本）。"""
        return []

    async def run(self, ctx: ScenarioContext) -> AttemptOutcome:
        """低 ceiling 执行 → 结构性暂停 → 续跑 → 读盘 + 投影 → 机械断言。"""
        from agent_harness.agent.run_budget import (
            RESUME_BASIS_BUDGET_INCREASE,
            latest_paused_run,
        )
        from agent_harness.session import JsonlSessionStore, Session
        from agent_harness.session.event import RUN_COMPLETED
        from agent_harness.web.app import AppState, session_service

        settings = _scenario_settings(ctx)
        state = AppState(settings)
        service = session_service(state)
        try:
            store = JsonlSessionStore(root=ctx.session_root)
            state.workspace_registry.create(
                ctx.session_id, workspace_root=ctx.sandbox.workspace_root,
            )
            Session.start(
                store, session_id=ctx.session_id, cwd=ctx.sandbox.workspace_root,
            )

            first = await service.resume_and_launch(
                session_id=ctx.session_id, task=TASK,
                run_max_agent_turns_total=LOW_CEILING,
                session_max_delegations=SESSION_MAX_DELEGATIONS,
            )
            stream_pause = await _drain(first, service=service, phase="低预算执行")
            events = await service.get_events(ctx.session_id)
            paused = latest_paused_run(events)
            if paused is None:
                raise RuntimeError(
                    "低预算执行没有留下可恢复的暂停（run/paused）——场景前提不成立，"
                    "此时不做恢复（不做「假恢复」）"
                )

            resumed = await service.resume_and_launch(
                session_id=ctx.session_id, task=None,
                resume_run_id=paused.run_id,
                resume_basis=RESUME_BASIS_BUDGET_INCREASE,
                run_max_agent_turns_total=RESUME_CEILING,
                expected_version=paused.version,
                # 不点名任何 session 维：账行（含 delegations=8 的 ceiling 与已耗的
                # 计数）原样沿用——handle 取账行现值，绝不拿 None 声明收紧回去。
            )
            stream_resume = await _drain(resumed, service=service, phase="同 run 续跑执行")
        except Exception as error:  # noqa: BLE001 - 失败要如实记录（runner 统一脱敏）
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id,
                error=f"{type(error).__name__}: {error}",
            )
        finally:
            await state.shutdown()

        # 证据读盘在 shutdown 之后（pause_resume 的实测：可选能力收尾会追加 durable 事实）。
        try:
            events = await service.get_events(ctx.session_id)
            replayed = store.read_events(ctx.session_id)
            projection = await service.budget_projection(ctx.session_id)
        except Exception as error:  # noqa: BLE001 - 同上的如实记录
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id,
                error=f"{type(error).__name__}: {error}",
            )

        run_id = next(
            (str(event.run_id or "") for event in events if event.type == "run/started"), "",
        )
        assertions = self._assertions(
            ctx=ctx, events=events, replayed=replayed, projection=projection,
            streams={
                "pause": [str(event.type) for event in stream_pause],
                "resume": [str(event.type) for event in stream_resume],
            },
        )
        completed = [event for event in events if event.type == RUN_COMPLETED]
        final_text = str(completed[0].data.get("final_text", "")) if completed else ""
        return AttemptOutcome(
            ok=all(item.ok for item in assertions),
            session_id=ctx.session_id,
            run_id=run_id,
            run_status="completed" if completed else "unknown",
            steps=sum(1 for event in events if event.type == "model/completed"),
            tool_calls=[
                str(event.data.get("tool_name"))
                for event in events if event.type == "tool/call"
            ],
            assertions=assertions,
            event_count=len(events),
            output_tail=final_text[-2000:],
        )

    def _assertions(
        self, *, ctx: ScenarioContext, events: list[Any], replayed: list[Any],
        projection: dict[str, Any] | None, streams: dict[str, list[str]],
    ) -> list[AssertionResult]:
        """结构性判据（机械可检）：计数 / 字段形状 / 事件配对，不评模型措辞。"""
        from agent_harness.agent.run_budget import (
            REASON_BUDGET_EXHAUSTED,
            TRIGGER_RUN_TURNS,
        )
        from agent_harness.session.event import (
            RUN_COMPLETED,
            RUN_FAILED,
            RUN_INTERRUPTED,
            RUN_PAUSED,
            RUN_RESUMED,
            RUN_STARTED,
        )

        paused_events = [e for e in events if e.type == RUN_PAUSED]
        resumed_events = [e for e in events if e.type == RUN_RESUMED]
        completed_events = [e for e in events if e.type == RUN_COMPLETED]
        failed_events = [e for e in events if e.type in (RUN_FAILED, RUN_INTERRUPTED)]
        started_events = [e for e in events if e.type == RUN_STARTED]
        run_id = str(started_events[0].run_id or "") if started_events else ""
        run_ids = {str(e.run_id) for e in events if e.run_id}

        paused = paused_events[0] if paused_events else None
        pdata = dict(paused.data) if paused is not None else {}
        resumed = resumed_events[0] if resumed_events else None
        rdata = dict(resumed.data) if resumed is not None else {}

        # 委派面：tool/call(delegate) 是尝试面；delegation-started 是接纳面；
        # delegation-finished 是"子代理真的跑完"面。三者的差就是拒绝数。
        delegate_calls = [
            e for e in events
            if e.type == "tool/call" and str(e.data.get("tool_name")) == DELEGATE_TOOL
        ]
        delegate_call_ids = {str(e.data.get("tool_call_id")) for e in delegate_calls}
        started_all = [e for e in events if e.type == "agent/delegation-started"]
        finished_all = [e for e in events if e.type == "agent/delegation-finished"]
        started_children = [str(e.data.get("child_session_id")) for e in started_all]
        rejected_results = [
            e for e in events
            if e.type == "tool/result"
            and str(e.data.get("tool_call_id")) in delegate_call_ids
            and BUDGET_EXHAUSTED_MARKER in str(e.data.get("content", ""))
            and f"{SESSION_MAX_DELEGATIONS}/{SESSION_MAX_DELEGATIONS}" in str(e.data.get("content", ""))
        ]
        finished_children = [str(e.data.get("child_session_id")) for e in finished_all]
        finished_statuses = [str(e.data.get("status")) for e in finished_all]

        projection_session = (
            projection.get("session") if isinstance(projection, dict) else None
        )
        projection_consumed = (
            projection_session.get("consumed") if isinstance(projection_session, dict) else None
        )
        projection_limits = (
            projection_session.get("limits") if isinstance(projection_session, dict) else None
        )

        def _int(raw: Any) -> int | None:
            if isinstance(raw, bool) or not isinstance(raw, int):
                return None
            return raw

        dangling = dangling_tool_call_ids(events)
        return [
            AssertionResult(
                name="pause_exactly_once_structural",
                ok=(
                    len(paused_events) == 1
                    and str(pdata.get("reason")) == REASON_BUDGET_EXHAUSTED
                    and str(pdata.get("trigger_dimension")) == TRIGGER_RUN_TURNS
                ),
                detail=(
                    f"run/paused={len(paused_events)} reason={pdata.get('reason')!r} "
                    f"trigger={pdata.get('trigger_dimension')!r}"
                    f"（ceiling={LOW_CEILING} ⇒ 首轮之后结构性地暂停）"
                ),
            ),
            AssertionResult(
                name="resume_same_run_without_session_dims",
                ok=(
                    len(resumed_events) == 1
                    and resumed is not None
                    and resumed.run_id == run_id
                    and rdata.get("from_pause_seq") == (paused.seq if paused is not None else None)
                    and str(rdata.get("resume_basis")) == "budget_increase"
                ),
                detail=(
                    f"run/resumed={len(resumed_events)} from_pause_seq={rdata.get('from_pause_seq')}"
                    f" basis={rdata.get('resume_basis')!r}"
                    "（续跑不点名 session 维：账行现值原样沿用）"
                ),
            ),
            AssertionResult(
                name="run_completed_without_terminal_noise",
                ok=(
                    len(completed_events) == 1 and len(failed_events) == 0
                    and completed_events[0].run_id == run_id
                    and len(started_events) == 1
                    and run_ids == {run_id}
                ),
                detail=(
                    f"run/completed={len(completed_events)} failed/interrupted={len(failed_events)}"
                    f" run/started={len(started_events)} run_id 集合={sorted(run_ids)}"
                ),
            ),
            AssertionResult(
                name="all_subtask_attempts_made",
                ok=len(delegate_calls) >= SUBTASKS,
                detail=(
                    f"tool/call(delegate)={len(delegate_calls)}（任务书要求实际发起"
                    f" {SUBTASKS} 次；模型少发即如实判红）"
                ),
            ),
            AssertionResult(
                name="accepted_delegations_capped_across_runs",
                ok=(
                    len(started_all) == SESSION_MAX_DELEGATIONS
                    and len(set(started_children)) == SESSION_MAX_DELEGATIONS
                ),
                detail=(
                    f"agent/delegation-started={len(started_all)}，"
                    f"去重 child_session_id={len(set(started_children))}"
                    f"（期望恰好 {SESSION_MAX_DELEGATIONS}：计数器跨 run 聚合，"
                    "若按 run 重置，总数会超过配额）"
                ),
            ),
            AssertionResult(
                name="over_budget_rejection_before_child_execution",
                ok=(
                    len(rejected_results) >= 1
                    and len(delegate_calls) > len(started_all)
                ),
                detail=(
                    f"带「{BUDGET_EXHAUSTED_MARKER} "
                    f"{SESSION_MAX_DELEGATIONS}/{SESSION_MAX_DELEGATIONS}」的拒绝回填="
                    f"{len(rejected_results)}；尝试 {len(delegate_calls)} - 接纳 "
                    f"{len(started_all)} = {len(delegate_calls) - len(started_all)} 次未产生"
                    "任何子会话（拒绝发生在 reserve_delegation，先于 provider.run）"
                ),
            ),
            AssertionResult(
                name="children_are_real_and_completed",
                ok=(
                    len(finished_all) == SESSION_MAX_DELEGATIONS
                    and all(status == "completed" for status in finished_statuses)
                    and sorted(finished_children) == sorted(started_children)
                ),
                detail=(
                    f"agent/delegation-finished={len(finished_all)}，"
                    f"statuses={sorted(set(finished_statuses))}，"
                    "child_session_id 与 started 一一对应（不是空账）"
                ),
            ),
            AssertionResult(
                name="session_row_projection_agrees",
                ok=(
                    isinstance(projection_consumed, dict)
                    and _int(projection_consumed.get("delegations")) == SESSION_MAX_DELEGATIONS
                    and isinstance(projection_limits, dict)
                    and _int(projection_limits.get("max_delegations")) == SESSION_MAX_DELEGATIONS
                    and _int(projection_session.get("version")) is not None
                    and _int(projection_session.get("version")) >= 1
                ),
                detail=(
                    f"投影 session.consumed.delegations="
                    f"{projection_consumed.get('delegations') if isinstance(projection_consumed, dict) else '缺'}"
                    f" limits.max_delegations="
                    f"{projection_limits.get('max_delegations') if isinstance(projection_limits, dict) else '缺'}"
                    f" version="
                    f"{projection_session.get('version') if isinstance(projection_session, dict) else '缺'}"
                    "（durable 行的客户端面与事件面互证）"
                ),
            ),
            AssertionResult(
                name="stream_mirrors_pause_and_completion",
                ok=(
                    streams.get("pause", []).count(RUN_PAUSED) == 1
                    and streams.get("resume", []).count(RUN_COMPLETED) == 1
                ),
                detail=(
                    f"低预算流 run/paused={streams.get('pause', []).count(RUN_PAUSED)}；"
                    f"续跑流 run/completed={streams.get('resume', []).count(RUN_COMPLETED)}"
                ),
            ),
            AssertionResult(
                name="durable_replay_matches_live",
                ok=(
                    [(e.seq, e.type) for e in events]
                    == [(e.seq, e.type) for e in replayed]
                ),
                detail=(
                    f"重新读盘 {len(replayed)} 条事件与内存态逐条同序"
                ),
            ),
            AssertionResult(
                name="no_dangling_tool_calls", ok=not dangling,
                detail=f"悬空 call={dangling}",
            ),
            AssertionResult(
                name="session_identity_present",
                ok=bool(ctx.session_id) and bool(run_id) and bool(delegate_calls),
                detail=(
                    f"session_id={'有' if ctx.session_id else '无'} "
                    f"run_id={'有' if run_id else '无'} "
                    f"delegate 调用={len(delegate_calls)}"
                ),
            ),
        ]


SCENARIO = DelegationBudgetTreeWideScenario()
