"""Phase 13 真实 Gate（T12, #93, ADR-0015）。

八条 Gate（roadmap Phase 13 + spec §13 验收 + ticket #93 场景）：
1. research 任务路由到 research_review（真实模型决定委派）
2. coding 任务路由到 coding + 共享 workspace 真实写文件
3. 动态第四个 AgentSpec 经 AgentFactory 创建并真实跑通
4. child 不倾倒完整历史（父流事件数抽查 vs child JSONL）
5. mixed 任务：research 结论 → coding 落盘（两个 child 协作）
6. ScriptedModel 驱动同指纹失败 delegate，验证 RepeatedToolFailureGuard（软熔断）
7. delegation 预算（max_delegations）真实耗尽回填
8. CAPABILITIES 未配 multiagent → 单代理零感知回归（无 delegate）

Gate 1–5、7–8 使用 .env 主模型；Gate 6 使用不联网的确定性 ScriptedModel。
凭证零泄漏。手动跑：
uv run pytest tests/integration/test_phase13_gate.py -m integration -v
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent.factory import AgentFactory
from agent_harness.config import Settings
from agent_harness.session import Session
from agent_harness.session.event import SessionEvent
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import ToolRegistry
from tests.scripted_model import ScriptedModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _assert_child_internal_payloads_are_not_copied(
    child_events: list[SessionEvent], parent_events: list[SessionEvent], *,
    child_summary: str | None = None,
) -> None:
    # Exempt only the final child model event that supplied the delegated summary.
    # Other child model events and all tool payloads remain protected.
    last_model_event = next(
        (event for event in reversed(child_events) if event.type == "model/completed"),
        None,
    )
    summary_event_id = (
        last_model_event.event_id
        if child_summary is not None
        and last_model_event is not None
        and last_model_event.data.get("content") == child_summary
        else None
    )
    child_internal_payloads = {
        (event.type, json.dumps(event.data, sort_keys=True, ensure_ascii=False))
        for event in child_events
        if event.type in {"model/completed", "tool/call", "tool/result"}
        and event.event_id != summary_event_id
    }
    parent_payloads = {
        (event.type, json.dumps(event.data, sort_keys=True, ensure_ascii=False))
        for event in parent_events
    }
    assert child_internal_payloads.isdisjoint(parent_payloads), (
        "父 Session 不得复制 child 的内部模型/工具事件内容"
    )


def _assert_children_internal_payloads_are_not_copied(
    children: list[tuple[list[SessionEvent], str | None]],
    parent_events: list[SessionEvent],
) -> None:
    for child_events, child_summary in children:
        _assert_child_internal_payloads_are_not_copied(
            child_events, parent_events, child_summary=child_summary,
        )


def _finished_delegation_for_child(
    finished_events: list[SessionEvent], child_session_id: str,
) -> SessionEvent:
    matches = [
        event for event in finished_events
        if event.data.get("child_session_id") == child_session_id
    ]
    assert len(matches) == 1, (
        f"expected one delegation finish for {child_session_id}, got {len(matches)}"
    )
    return matches[0]


async def test_gate4_allows_summary_reuse_but_rejects_copied_internal_payloads():
    child_summary = SessionEvent(
        type="model/completed", session_id="child", data={"content": "task summary"},
    )
    parent_summary = SessionEvent(
        type="model/completed", session_id="parent", data={"content": "task summary"},
    )

    _assert_child_internal_payloads_are_not_copied(
        [child_summary], [parent_summary], child_summary="task summary",
    )

    child_intermediate_model = SessionEvent(
        type="model/completed", session_id="child", data={"content": "internal turn"},
    )
    parent_intermediate_model = SessionEvent(
        type="model/completed", session_id="parent", data={"content": "internal turn"},
    )
    with pytest.raises(AssertionError):
        _assert_child_internal_payloads_are_not_copied(
            [child_intermediate_model], [parent_intermediate_model],
        )

    for event_type in ("tool/call", "tool/result"):
        child_tool_event = SessionEvent(
            type=event_type, session_id="child", data={"payload": "private tool data"},
        )
        parent_tool_event = SessionEvent(
            type=event_type, session_id="parent", data={"payload": "private tool data"},
        )
        with pytest.raises(AssertionError):
            _assert_child_internal_payloads_are_not_copied(
                [child_tool_event], [parent_tool_event], child_summary="task summary",
            )

    earlier_summary_text = SessionEvent(
        type="model/completed", session_id="child", data={"content": "task summary"},
    )
    different_final_answer = SessionEvent(
        type="model/completed", session_id="child", data={"content": "actual final answer"},
    )
    with pytest.raises(AssertionError):
        _assert_child_internal_payloads_are_not_copied(
            [earlier_summary_text, different_final_answer],
            [parent_summary],
            child_summary="task summary",
        )


async def test_gate4_checks_internal_payloads_for_every_child():
    first_child_summary = SessionEvent(
        type="model/completed", session_id="child-1", data={"content": "summary one"},
    )
    second_child_internal = SessionEvent(
        type="tool/result", session_id="child-2", data={"payload": "private child two"},
    )
    parent_summary = SessionEvent(
        type="model/completed", session_id="parent", data={"content": "summary one"},
    )
    copied_second_child_payload = SessionEvent(
        type="tool/result", session_id="parent", data={"payload": "private child two"},
    )

    with pytest.raises(AssertionError):
        _assert_children_internal_payloads_are_not_copied(
            [
                ([first_child_summary], "summary one"),
                ([second_child_internal], "summary two"),
            ],
            [parent_summary, copied_second_child_payload],
        )


async def test_gate4_uses_the_finished_event_for_the_inspected_child():
    first_child_finish = SessionEvent(
        type="agent/delegation-finished",
        session_id="parent",
        data={"child_session_id": "child-1", "status": "completed", "summary": "one"},
    )
    last_child_finish = SessionEvent(
        type="agent/delegation-finished",
        session_id="parent",
        data={"child_session_id": "child-2", "status": "completed", "summary": "two"},
    )

    assert _finished_delegation_for_child(
        [first_child_finish, last_child_finish], "child-1",
    ) is first_child_finish
    with pytest.raises(AssertionError, match="expected one delegation finish"):
        _finished_delegation_for_child([last_child_finish], "child-1")


def _gate_settings(tmp_path, *, live_model: bool = True) -> Settings:
    if live_model:
        # 真实 .env（repo 根）提供 MODEL_*/FALLBACK_*；workspace 指向 tmp 隔离。
        settings = Settings()
        if not settings.model_api_key.get_secret_value():
            pytest.skip("Real primary model (MODEL_*) is not configured")
    else:
        # Gate 6 在 Runtime 构造时需要一个合法但不会发送的模型配置；run 前
        # ScriptedModel 会替换它。明确禁用 .env，避免依赖或触碰任何真实凭证。
        from pydantic import SecretStr

        settings = Settings(
            _env_file=None,
            model_provider="deepseek",
            model_name="deepseek-chat",
            model_api_key=SecretStr("test-only-no-network"),
            model_base_url="http://127.0.0.1:9/v1",
            fallback_model_provider="",
            fallback_model_name="",
            fallback_model_api_key=SecretStr(""),
            fallback_model_base_url="",
        )
    settings.workspace_dir = str(tmp_path)
    caps = {"multiagent": {"provider": "builtin", "enabled": True, "options": {}}}
    settings.capabilities = json.dumps(caps)
    return settings


def _build_gate_env(tmp_path, *, live_model: bool):
    """构造隔离的 multiagent Runtime，真实/确定性模型由调用方选择。"""
    import asyncio

    settings = _gate_settings(tmp_path, live_model=live_model)
    from agent_harness.assembly import (
        build_runtime,
        initialize_stores,
        recovery_stores,
    )
    from agent_harness.capability.base import CapabilityRegistry
    from agent_harness.capability.config import parse_capabilities_config
    from agent_harness.capability.wiring import wire_capabilities
    from agent_harness.sandbox import WorkspaceRegistry

    async def _build():
        stores = recovery_stores(tmp_path / "harness.db")
        await initialize_stores(stores)
        registry = CapabilityRegistry()
        wiring = await wire_capabilities(
            registry, parse_capabilities_config(settings.capabilities),
            settings=settings,
        )
        workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
        store = JsonlSessionStore(tmp_path / "sessions")
        session_id = f"gate-{tmp_path.name[:8]}"
        workspace = tmp_path / "workspaces" / session_id
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=workspace_registry,
            session_id=session_id, workspace=workspace,
            max_agent_turns=15, auto_approve=True, session_store=store,
        )
        return runtime, store, workspace_registry, workspace, session_id

    return asyncio.new_event_loop().run_until_complete(_build()) + (tmp_path,)


@pytest.fixture
def gate_env(tmp_path):
    """真实模型 + multiagent capability 的完整运行环境。"""
    return _build_gate_env(tmp_path, live_model=True)


@pytest.fixture
def scripted_gate_env(tmp_path):
    """无凭证、无网络依赖的确定性 multiagent Runtime。"""
    return _build_gate_env(tmp_path, live_model=False)


async def _run_and_collect(runtime, session, task: str):
    events = []
    async for event in runtime.run_stream(session, task):
        events.append(event)
    return events


@pytest.mark.usefixtures("requires_live_model")
class TestGate1ResearchRouting:
    @pytest.mark.asyncio
    async def test_research_task_delegates_to_research_review(self, gate_env):
        """真实模型把调研任务委派给 research_review → delegation 事件 + 结果。

        上游网关瞬时故障（429/5xx）环境噪声大——3 次独立尝试协议（每次全新
        session），任一次路由成功即过（与 Phase 12 breaker gate 同款）。"""
        runtime, store, _workspace_registry, _workspace, _session_id, _tmp_path = gate_env
        last_events: list = []
        for attempt in range(3):
            session = Session.start(store, session_id=f"gate-research-{attempt}")
            await _run_and_collect(
                runtime, session,
                "你必须使用 delegate 工具把下面的任务委派给 research_review 子代理"
                "执行，你自己不要执行任务、不要亲自搜索。委派后把子代理的结果转述"
                "给我。任务：用 web_search 搜索 Tavily 是什么公司的产品，并一句话"
                "总结。",
            )
            delegation_finished = [e for e in session._events
                                   if e.type == "agent/delegation-finished"]
            if delegation_finished and delegation_finished[-1].data["status"] == "completed":
                assert delegation_finished[-1].data["target"] == "research_review"
                assert delegation_finished[-1].data["child_session_id"]
                return
            last_events = session._events
        print("DEBUG-EVENTS:", [(e.type, str(e.data)[:70]) for e in last_events])
        pytest.fail("3 次尝试内 research 路由都未成功（上游故障或模型未委派）")


@pytest.mark.usefixtures("requires_live_model")
class TestGate2CodingRouting:
    @pytest.mark.asyncio
    async def test_coding_task_writes_shared_workspace(self, gate_env):
        """coding 子代理在共享 workspace 真实写文件（spec §9 同一 sandbox）。"""
        runtime, store, _workspace_registry, workspace, _session_id, _tmp_path = gate_env
        for attempt in range(3):
            session = Session.start(store, session_id=f"gate-coding-{attempt}")
            await _run_and_collect(
                runtime, session,
                "你必须使用 delegate 工具把任务委派给 coding 子代理执行，你自己"
                "不要写文件。委派后验证并向我确认。任务：在当前目录创建文件 "
                "hello.txt，内容为一行文本 phase13-ok。",
            )
            delegation_finished = [e for e in session._events
                                   if e.type == "agent/delegation-finished"]
            if (delegation_finished
                    and delegation_finished[-1].data["status"] == "completed"
                    and (workspace / "hello.txt").exists()):
                assert delegation_finished[-1].data["target"] == "coding"
                assert "phase13-ok" in (workspace / "hello.txt").read_text(encoding="utf-8")
                return
        pytest.fail("3 次尝试内 coding 路由+写文件都未成功")


@pytest.mark.usefixtures("requires_live_model")
class TestGate3DynamicFourthAgent:
    @pytest.mark.asyncio
    async def test_fourth_agentspec_runs_real_model(self, gate_env):
        """动态第四个 AgentSpec 经 AgentFactory 创建并真实跑通（Gate 验收）。"""
        runtime, _store, _workspace_registry, _workspace, _session_id, tmp_path = gate_env
        # 复用 runtime 的模型链构造第 4 个 agent
        from agent_harness.agent.profiles import AgentSpec

        spec = AgentSpec(
            name="analyst", description="Gate 专用临时分析角色",
            system_prompt="你是数据分析 agent：把收到的数字列表求和，一句话回答总和。",
            tool_scope=frozenset(),  # 纯推理，无工具
            max_agent_turns=3,
        )
        factory = AgentFactory(
            model=runtime.model, primary_model_name="gate-analyst",
            stream_idle_timeout=60.0, stream_total_timeout=600.0,
        )
        analyst_runtime = factory.create(
            spec, source_registry=ToolRegistry(), grantable=frozenset(),
        )
        analyst_session = Session.start(
            JsonlSessionStore(tmp_path / "sessions" / "analyst"),
        )

        result = await analyst_runtime.run(
            analyst_session, "数字列表：21、21、105。总和是多少？"
        )

        assert result.status == "completed"
        assert "147" in result.final_text


@pytest.mark.usefixtures("requires_live_model")
class TestGate4NoHistoryDump:
    @pytest.mark.asyncio
    async def test_parent_stream_stays_clean_after_delegation(self, gate_env):
        """child 不倾倒完整历史：父 session 不含 child 的 run/模型内部事件。"""
        runtime, store, _workspace_registry, _workspace, _session_id, tmp_path = gate_env
        # Live 路由决策可能单次未发起委派；最多重试 3 个独立 Session。
        # 一旦 child 已启动就不重跑：其失败必须让 Gate 失败，避免掩盖结果或
        # 重复真实搜索副作用。
        session = None
        for attempt in range(3):
            session = Session.start(store, session_id=f"gate-nodump-{attempt}")
            await _run_and_collect(
                runtime, session,
                "你必须使用 delegate 工具把任务派给 research_review 子代理执行，"
                "不要亲自搜索。任务：搜索 Python 官网网址，一句话回答。",
            )
            delegation_started = [
                event for event in session.events
                if event.type == "agent/delegation-started"
            ]
            if not delegation_started:
                tool_calls = [
                    event for event in session.events
                    if event.type == "tool/call"
                ]
                assert not tool_calls, (
                    "attempt made a tool call without a persisted delegation start; "
                    "the tool may already have caused a side effect, so retry is unsafe"
                )
                continue
            delegation_finished = [
                event for event in session.events
                if event.type == "agent/delegation-finished"
            ]
            assert delegation_finished, "child 已启动但没有持久化委派终态"
            child_session_ids = [
                event.data["child_session_id"] for event in delegation_started
            ]
            assert len(child_session_ids) == len(set(child_session_ids)), (
                "每个 child delegation start 必须使用唯一 child_session_id"
            )
            finished_child_ids = [
                event.data.get("child_session_id") for event in delegation_finished
            ]
            assert len(finished_child_ids) == len(set(finished_child_ids)), (
                "每个 child_session_id 只能有一个 delegation finish"
            )
            assert set(finished_child_ids) == set(child_session_ids), (
                "每个已启动 child 都必须且只能有一个匹配的委派终态"
            )
            child_finishes = {
                child_id: _finished_delegation_for_child(
                    delegation_finished, child_id,
                )
                for child_id in child_session_ids
            }
            assert all(
                finish.data["status"] == "completed"
                for finish in child_finishes.values()
            ), "child 已启动但失败；不得以新 Session 重试来掩盖失败"
            break
        else:
            pytest.fail("3 次独立尝试内未观察到 research_review 委派启动")

        parent_types = [e.type for e in session.events]
        from collections import Counter

        counts = Counter(parent_types)
        # 父流只有 supervisor 自己的紧凑事件（不含 child 的多轮内幕）
        assert counts.get("run/completed", 0) <= 1, "父 session 只有自己的 run"
        children = []
        for child_id in child_session_ids:
            child_events = Session.resume(
                JsonlSessionStore(tmp_path / "sessions"), child_id,
            )
            child_types = [event.type for event in child_events.events]
            assert child_types.count("model/completed") >= 1, (
                f"child {child_id} 自己的历史完整留存"
            )
            child_agent_ids = {
                event.agent_id for event in child_events.events if event.agent_id
            }
            assert child_agent_ids == {"research_review"}, (
                f"child {child_id} 事件保留自身 agent provenance"
            )
            children.append((
                child_events.events,
                child_finishes[child_id].data.get("summary"),
            ))
        # 保留父流自身调用上限，并用独立 agent_id / source_event_ids 断言
        # child 内部事件没有进入父 Session。
        parent_model_calls = counts.get("model/completed", 0)
        assert parent_model_calls <= 3, (
            f"父流 model/completed={parent_model_calls}——child 历史疑似倾倒"
        )
        parent_event_ids = {event.event_id for event in session.events}
        child_event_ids = {
            event.event_id
            for child_events, _summary in children
            for event in child_events
        }
        assert parent_event_ids.isdisjoint(child_event_ids), (
            "父 Session 不得复用 child 的持久化事件 ID"
        )
        child_agent_ids = {
            event.agent_id
            for child_events, _summary in children
            for event in child_events
            if event.agent_id
        }
        assert all(event.agent_id not in child_agent_ids for event in session.events), (
            "父 Session 不得包含 child agent 的内部事件"
        )
        assert all(
            not (set(event.source_event_ids or ()) & child_event_ids)
            for event in session.events
        ), "父 Session 不得以 provenance 引用 child 内部事件"
        # Parent may reuse child result summary; full tool payload copies remain forbidden.
        _assert_children_internal_payloads_are_not_copied(children, session.events)


@pytest.mark.usefixtures("requires_live_model")
class TestGate5MixedCoordination:
    @pytest.mark.asyncio
    async def test_mixed_task_two_children_cooperate(self, gate_env):
        """mixed 任务：research 结论经 supervisor 交接给 coding 落盘（两 child 协作）。"""
        runtime, store, _workspace_registry, workspace, _session_id, _tmp_path = gate_env
        for attempt in range(3):
            session = Session.start(store, session_id=f"gate-mixed-{attempt}")
            await _run_and_collect(
                runtime, session,
                "你必须分两步使用 delegate 工具完成下面的任务，不要亲自搜索或写"
                "文件。第一步：委派给 research_review，任务为『用 web_search 搜索"
                "Tavily 是什么公司的产品，一句话总结』。等第一步结果返回后，第二"
                "步：把第一步的总结原文交给 coding 子代理，让它把总结写入当前目"
                "录文件 mixed.txt（一行文本）。最后向我确认两步都完成。",
            )
            finished = [e for e in session._events
                        if e.type == "agent/delegation-finished"]
            if (len(finished) >= 2
                    and all(e.data["status"] == "completed" for e in finished)
                    and {e.data["target"] for e in finished}
                    >= {"research_review", "coding"}
                    and (workspace / "mixed.txt").exists()):
                content = (workspace / "mixed.txt").read_text(encoding="utf-8")
                assert len(content.strip()) >= 5, "coding 应写入 research 的结论"
                return
        pytest.fail("3 次尝试内 mixed 两 child 协作未成功")


class TestGate6BreakerDeterministic:
    @pytest.mark.asyncio
    async def test_repeated_failing_delegations_trip_guard(self, scripted_gate_env):
        """确定性决策驱动真实 Runtime 的 delegate 同错熔断路径。

        同一 run 内三次完全相同的未知 target 在 child spawn 前失败；第 3 次应
        触发 soft guard。真实模型是否重复调用由其它 live gates 覆盖；这里固定决策，
        避免把跨独立 Session 的失败错误地累计成一个 guard 序列。"""
        runtime, store, _workspace_registry, _workspace, _session_id, _tmp_path = (
            scripted_gate_env
        )
        args = {
            "target": "nonexistent_role",
            "task": "熔断验收",
            "constraints": [],
        }
        calls = [
            AIMessage(
                content="",
                tool_calls=[{
                    "id": f"call_gate_breaker_{index}",
                    "name": "delegate",
                    "args": args.copy(),
                }],
            )
            for index in range(3)
        ]
        runtime.model = ScriptedModel(
            [*calls, AIMessage(content="失败委派路径已验证。")]
        )

        session = Session.start(store, session_id="gate-breaker-deterministic")
        await _run_and_collect(runtime, session, "验证重复失败委派熔断")

        delegate_calls = [
            event for event in session.events
            if event.type == "tool/call" and event.data.get("tool_name") == "delegate"
        ]
        assert len(delegate_calls) == 3
        assert all(event.data["args"] == args for event in delegate_calls)

        guard_events = [
            event for event in session.events if event.type == "tool/failure-guard"
        ]
        assert len(guard_events) == 1
        assert guard_events[0].data["tool_name"] == "delegate"
        assert guard_events[0].data["level"] == "soft"
        assert guard_events[0].data["consecutive_failures"] == 3
        assert any(event.type == "run/completed" for event in session.events)


@pytest.mark.usefixtures("requires_live_model")
class TestGate7BudgetReal:
    @pytest.mark.asyncio
    async def test_delegation_budget_exhaustion_real(self, gate_env):
        """预算真实触发：max_delegations=2，第 3 次委派收到明确的预算耗尽回填。

        换 max_delegations=2（同一已激活 delegate 实例，白盒改构造参数——预算是
        DelegateTool 配置而非环境语义，不改生产代码）。三次调用 task 互不相同
        （不同指纹，避免熔断器干扰预算观察）；预算检查在 provider.run 之前，
        未知 target 的前两次失败同样消耗预算。"""
        runtime, store, _workspace_registry, _workspace, _session_id, _tmp_path = gate_env
        active = runtime.registry.get("delegate")
        assert isinstance(active._max_delegations, int)
        active._max_delegations = 2
        for attempt in range(3):
            session = Session.start(store, session_id=f"gate-budget-{attempt}")
            await _run_and_collect(
                runtime, session,
                "这是预算机制验收测试，你必须严格照做：依次发起 3 次 delegate "
                "工具调用（一次一个，等上一个返回再发下一个），target 都填 "
                "nonexistent_role，task 分别填『任务一』『任务二』『任务三』（"
                "必须三个都发，即使前面失败也不要停止）。不要尝试其他工具。",
            )
            delegate_calls = [e for e in session._events if e.type == "tool/call"
                              and e.data.get("tool_name") == "delegate"]
            budget_exhausted = any(
                e.type == "tool/result"
                and "预算耗尽" in e.data.get("content", "")
                for e in session._events
            )
            if len(delegate_calls) >= 3 and budget_exhausted:
                return
        pytest.fail("3 次尝试内预算耗尽未真实回填（模型未完成 3 次委派或预算未触发）")


@pytest.mark.usefixtures("requires_live_model")
class TestGate8CapabilityOff:
    @pytest.mark.asyncio
    async def test_single_agent_regression_without_multiagent(self, gate_env):
        """CAPABILITIES 未配 multiagent → 单代理零感知（无 delegate，纯回归）。

        spec Phase 13 Gate『Single Agent 不依赖 LangGraph』的结构证据：multiagent
        是 opt-in capability，未配置 = 工具缺席 + Agent Loop 零改动。"""
        _runtime, store, workspace_registry, _workspace, _session_id, tmp_path = gate_env
        settings = _gate_settings(tmp_path, live_model=True)
        settings.capabilities = json.dumps({})  # 空 capability 表
        from agent_harness.assembly import (
            assemble_wiring,
            build_runtime,
            initialize_stores,
            recovery_stores,
        )

        async def _build():
            stores = recovery_stores(tmp_path / "single.db")
            await initialize_stores(stores)
            _registry, wiring = await assemble_wiring(settings)
            return await build_runtime(
                settings=settings, wiring=wiring, stores=stores,
                workspace_registry=workspace_registry,
                session_id="gate-single", workspace=tmp_path / "workspaces" / "gate-single",
                max_agent_turns=5, auto_approve=True, session_store=store,
            )

        single_runtime = await _build()
        with pytest.raises(KeyError):
            single_runtime.registry.get("delegate")

        # 上游瞬时 500 也算环境噪声：3 次独立尝试（每次全新 session）。
        last_result = None
        for attempt in range(3):
            session = Session.start(store, session_id=f"gate-single-{attempt}")
            last_result = await single_runtime.run(
                session, "1+1等于几？直接回答，不要用工具"
            )
            if last_result.status == "completed":
                assert "2" in last_result.final_text
                return
        pytest.fail(f"单代理 3 次尝试未完成（上游故障？）：{last_result.status}")
