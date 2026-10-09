"""`#317` T9：stuck 暂停在**真链路**上的恢复（CLI → SessionService → runtime）。

判据来源：`02 §5.3`（三类依据，观测不到就 409）、`03 §3.4`（`run/resumed` 的形状与
CAS）、`03 §5`（对账优先于恢复是状态规则）。ADR-0048 D7/D8。

与 `tests/agent/test_stuck_resume.py` 的分工：那边证明**判据**（纯函数与 `validate_resume`），
这里证明**接线**——一次真实跑出来的 stuck 暂停，其载荷里带着暂停那一刻的环境 / 策略快照；
恢复侧用同一份算法现算，才可能得出"变了 / 没变"；被拒的请求**零副作用**（不写事件、
不建目录、不启动 run）。

脚本模型只请求一个**未注册**的工具（`no_such_tool`）且参数逐次相同：这是"确定性失败的
同动作"最小构造——`ToolExecutor` 在准入时给出 `TOOL_NOT_FOUND` 失败结果，既不需要
sandbox，也不碰 Docker。真模型那半在 Live Gate 场景③。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from agent_harness import cli
from agent_harness.agent.budget import BudgetConflict, BudgetRejection
from agent_harness.agent.run_budget import (
    REASON_STUCK,
    RESUME_BASIS_BUDGET_INCREASE,
    RESUME_BASIS_ENVIRONMENT_CHANGE,
    RESUME_BASIS_POLICY_CHANGE,
    RESUME_BASIS_RELEVANT_STEER,
    STUCK_RESUME_REQUIREMENTS,
    SessionLimits,
    latest_paused_run,
)
from agent_harness.cli import resume_command
from agent_harness.config import Settings
from agent_harness.session import (
    GUARD_STUCK,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RESUMED,
    SESSION_RESUMED,
    TOOL_FAILURE_GUARD,
    USER_MESSAGE,
)
from agent_harness.session.event import STEER_APPLIED
from agent_harness.session.store import JsonlSessionStore
from tests.scripted_model import ScriptedModel


def _settings(tmp_path: Path) -> Settings:
    # deepseek-chat 的默认 preset 不声明 reasoning_effort，但 from_settings 会从
    # AGENT_MODELS catalog 补 capability。这里给默认模型声明 deep，使
    # create_and_launch 的 validate_reasoning_effort("deep") 通过（#865）。
    return Settings(
        model_api_key="sk-test",
        workspace_dir=str(tmp_path / "workspace"),
        agent_models=SecretStr(json.dumps([{
            "name": "deepseek-chat",
            "provider": "deepseek",
            "model_name": "deepseek-chat",
            "reasoning_effort": {
                "supported": ["minimal", "standard", "deep"],
                "default": "standard",
                "wire_mapping": {"minimal": "none", "standard": "low", "deep": "high"},
            },
        }])),
        _env_file=None,  # 不吃仓库根 .env（测试必须自足）
    )


def _sessions_root(settings: Settings) -> Path:
    return Path(settings.workspace_dir) / "sessions"


def _only_session_id(settings: Settings) -> str:
    session_ids = [
        path.parent.name for path in _sessions_root(settings).glob("*/events.jsonl")
    ]
    assert len(session_ids) == 1, f"应有且仅有一个会话，实际 {session_ids}"
    return session_ids[0]


def _failing_round(index: int) -> AIMessage:
    """同一个未注册工具、同一份参数：动作指纹恒定 ⇒ ① 连续累积。"""
    return AIMessage(
        content="",
        tool_calls=[{
            "id": f"call_{index:04d}", "name": "no_such_tool", "args": {"command": "ls"},
        }],
    )


def _continuation_json() -> AIMessage:
    return AIMessage(content=json.dumps({
        "completed": ["试过同一个工具"], "remaining": ["还没拿到结果"],
        "blockers": ["同一条工具调用反复失败"], "next_safe_action": "换一条路",
    }, ensure_ascii=False))


async def _run_to_stuck_pause(monkeypatch, tmp_path: Path):
    """真链路跑出一场 stuck 暂停（6 次同错失败）。返回 (settings, session_id, 事件列表)。"""
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(
            responses=[_failing_round(index) for index in range(6)]
            + [_continuation_json()],
        ),
    )
    printed: list[str] = []
    outcome = await cli.run("反复用同一个工具", write=printed.append)
    assert outcome.paused is True, "".join(printed)

    session_id = _only_session_id(settings)
    events = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)
    return settings, session_id, events


def _append_steer(settings: Settings, session_id: str, *, content: str = "换一条路试试") -> None:
    """补一条**指向被暂停那个 run** 的 steer（恢复依据是"事件流里存在它"）。

    `run_id` 必须落在本 run 上（`#317` T9 审查 P2）：运行时的 `_applicable_steers` 只认
    run_id 相同，会话级（`run_id=None`）的会被当陈旧 steer 丢弃——那种 steer 连模型都
    到不了，不能算"新输入"的依据。这里直接落事件而不是走 Web 的队列：判据只看事件流
    （`relevant_steer_seq`），投递路径不属于本用例要证明的东西（投递边界见 ADR-0048
    残余 9/10）。
    """
    from agent_harness.session import Session

    store = JsonlSessionStore(root=_sessions_root(settings))
    paused = latest_paused_run(store.read_events(session_id))
    assert paused is not None and paused.run_id is not None
    session = Session.load(store, session_id)
    event = session.append(
        "steer/requested",
        {"steer_id": "steer-test", "content": content, "created_at": "2026-09-27T00:00:00Z"},
        run_id=paused.run_id,
    )
    assert event.type == "steer/requested"


# ── 暂停侧：载荷里必须留下可比对的快照 ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_real_stuck_pause_carries_the_evidence_snapshot(monkeypatch, tmp_path):
    """`run/paused.stuck` 带着**暂停那一刻**的环境 revision 与策略版本。

    这两格是 D7 的"有据可比"：缺席它们，恢复侧的环境 / 策略两条依据一律 409
    （fail-closed），stuck 暂停就只能靠 steer 解开。
    """
    settings, _session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)

    pause_events = [event for event in events if event.type == RUN_PAUSED]
    assert len(pause_events) == 1
    data = pause_events[0].data
    assert data["reason"] == REASON_STUCK
    assert data["resume_requirements"] == list(STUCK_RESUME_REQUIREMENTS)
    stuck = data["stuck"]
    assert stuck["pattern"] == "stuck.tool_failure_loop"
    assert stuck["threshold"] == 3 and stuck["count"] == 6
    assert stuck["replan_count"] == 1
    assert stuck["environment_revision"] is not None
    assert stuck["environment_revision"].startswith("sha256:")
    assert stuck["policy_version"] is not None and stuck["policy_version"].startswith("sha256:")
    # 走的是既有暂停臂：不是终态，且带一份 continuation
    assert data["continuation"]
    assert [event for event in events if event.type == RUN_FAILED] == []
    # 恰好一条纠正：① 的既有形状（`tool/failure-guard` + `injected_by=tool_failure_guard`）
    guards = [event for event in events if event.type == TOOL_FAILURE_GUARD]
    assert [event.data["level"] for event in guards] == ["soft"]
    correctives = [
        event for event in events if event.type == USER_MESSAGE
        and event.data.get("injected_by") in ("tool_failure_guard", "stuck_guard")
    ]
    assert [event.data["injected_by"] for event in correctives] == ["tool_failure_guard"]
    assert [event for event in events if event.type == GUARD_STUCK
            and event.data["level"] == "replan"] == []
    # 快照不是"随便一个 sha256"：它与会话自己那个工作区目录的现算摘要**逐字相同**
    #（CLI 的创建路径把 `workspaces/<session_id>` 交给装配，证据端口照它算）。
    from agent_harness.agent.resume_evidence import environment_revision

    workspace = Path(settings.workspace_dir) / "workspaces" / _session_id
    assert workspace.is_dir(), "CLI 建的会话工作区必须落在 workspaces/<session_id>"
    assert stuck["environment_revision"] == environment_revision(workspace)


# ── 恢复侧：三类依据各自放行 / 各自 409 ──────────────────────────────────


@pytest.mark.asyncio
async def test_resume_with_a_relevant_steer_is_accepted_and_recorded(monkeypatch, tmp_path):
    """`relevant_steer`：暂停之后落一条 steer ⇒ 恢复放行，并把依据写进 `run/resumed`。"""
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None
    _append_steer(settings, session_id)

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[AIMessage(content="这次换了个做法")]),
    )
    import agent_harness.session.service as service_module

    original_build_runtime = service_module.build_runtime
    tool_names: set[str] = set()

    async def capture_resume_tools(**kwargs):
        runtime = await original_build_runtime(**kwargs)
        tool_names.update(tool.name for tool in runtime.registry.list())
        return runtime

    monkeypatch.setattr(service_module, "build_runtime", capture_resume_tools)
    printed: list[str] = []
    outcome = await resume_command(
        session_id, expected_version=paused.version,
        basis=RESUME_BASIS_RELEVANT_STEER, write=printed.append,
    )

    assert outcome.paused is False
    assert outcome.final_text == "这次换了个做法"
    assert "register_constraint" in tool_names
    assert "request_constraint_resolution" not in tool_names
    after = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)
    resumed = [event for event in after if event.type == RUN_RESUMED]
    assert len(resumed) == 1
    evidence = resumed[0].data["resume_evidence"]
    assert evidence["basis"] == RESUME_BASIS_RELEVANT_STEER
    assert evidence["pause_seq"] == paused.pause_seq
    assert isinstance(evidence["steer_seq"], int)
    # 同一个逻辑 run：只有一个 run/started
    assert len([event for event in after if event.type == "run/started"]) == 1


@pytest.mark.asyncio
async def test_cli_resume_rejects_hidden_tool_limit_before_session_budget_cas(
    monkeypatch, tmp_path,
):
    """CLI pre-CAS validation must use the registry with clarification hidden."""
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None
    _append_steer(settings, session_id)

    service = await cli._cli_session_service(settings)
    await service._ensure_stores()
    ledger = service._stores.delegation_tree_ledger
    before_budget = await ledger.ensure_session_budget(
        session_id,
        root_session_id=session_id,
        limits=SessionLimits(tool_call_limits={"register_constraint": 1}),
    )
    before_events = await service.get_events(session_id)

    with pytest.raises(BudgetRejection, match="request_constraint_resolution"):
        await resume_command(
            session_id,
            expected_version=paused.version,
            basis=RESUME_BASIS_RELEVANT_STEER,
            session_tool_limits={"request_constraint_resolution": 1},
            session_expected_version=before_budget.version,
            write=lambda _text: None,
        )

    after_budget = await ledger.get_session_budget(session_id)
    assert after_budget is not None
    assert after_budget.version == before_budget.version
    assert after_budget.limits == before_budget.limits
    assert await service.get_events(session_id) == before_events


@pytest.mark.asyncio
async def test_a_stuck_resume_that_triggers_recovery_recomputes_the_evidence(
    monkeypatch, tmp_path,
):
    """stuck 暂停的恢复**穿过 Recovery** 时，暂停事实与现场观测一起重算（`#317` × `#342`）。

    这条路径是合并 `origin/main` 时暴露的**未被 git 标记**的语义冲突所在：upstream 新增的
    「Recovery 后重读」块按二元解包读 `_paused_resume_state`，而本票已把它改成三元返回
    （多一格现场观测）⇒ 真跑该组合先是 `ValueError: too many values to unpack`（500）。
    变异实测（本用例承重的那两条）：① 重读处退回二元解包 ⇒ **红**（ValueError）；
    ② 保留三元、但不把重读后的证据交给判据（`resume_evidence` 不传 ⇒ 判据收到 `None`）
    ⇒ **红**（409「没有可用的观测」）。
    ⚠ **如实登记的边界**：把重读后的 `_resume_evidence(...)` 整行删掉、沿用锁前那一份观测，
    本用例**测不出来**（变异实测为绿）——因为 Recovery 只追加 `session/resumed`，不会移动
    steer / 环境 / 策略三格的事实，两次读数的结论相同。要区分「重算 vs 沿用」需要一个
    Recovery 会改动这三格之一的构造，属残余（见 tracker T9 残余项：证据端口取自锁前读）。
    为什么必须专门补：`test_concurrent_resume_with_the_same_version_has_exactly_one_winner`
    的 `[recovery]` 臂跑的是**预算**暂停（`reason != stuck` ⇒ 证据那格恒为 `None`），它进重读块
    却用不到这一格 ⇒ 上述两条变异它都照样全绿（ADR-0048 D7）。
    """
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None
    _append_steer(settings, session_id)

    from agent_harness.session.service import SessionService

    recovery_calls = 0
    original_recover = SessionService.recover

    async def count_recovery(service, sid):
        nonlocal recovery_calls
        recovery_calls += 1
        return await original_recover(service, sid)

    async def force_recovery(service, sid):
        return True

    monkeypatch.setattr(SessionService, "_has_unreconciled_operations", force_recovery)
    monkeypatch.setattr(SessionService, "recover", count_recovery)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[AIMessage(content="这次换了个做法")]),
    )
    printed: list[str] = []
    outcome = await resume_command(
        session_id, expected_version=paused.version,
        basis=RESUME_BASIS_RELEVANT_STEER, write=printed.append,
    )

    assert recovery_calls == 1, "本用例要证明的正是「恢复事件穿过 Recovery」那条路径"
    assert outcome.paused is False
    assert outcome.final_text == "这次换了个做法"
    after = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)
    resumed = [event for event in after if event.type == RUN_RESUMED]
    assert len(resumed) == 1
    evidence = resumed[0].data["resume_evidence"]
    assert evidence["basis"] == RESUME_BASIS_RELEVANT_STEER
    assert evidence["pause_seq"] == paused.pause_seq
    assert isinstance(evidence["steer_seq"], int)
    # Recovery 已写过一条 session/resumed；CAS 胜者不得再写第二条（upstream 的
    # `if not needs_recovery`）——两条恢复事实并存 = 同一逻辑 run 被"恢复两次"。
    assert len([event for event in after if event.type == SESSION_RESUMED]) == 1
    assert len([event for event in after if event.type == "run/started"]) == 1


@pytest.mark.asyncio
async def test_a_session_level_steer_does_not_authorize_a_resume(monkeypatch, tmp_path):
    """`run_id=None` 的"会话级" steer 到不了模型 ⇒ 不算依据（`#317` T9 审查 P2）。

    运行时把它当陈旧 steer 丢弃（只认 run_id 相同），它只在 run 走进**终态**时被当普通
    输入投递；stuck 暂停不是终态 ⇒ 这一次它不会进任何模型上下文。把它当依据等于
    "没有新输入也放行"。两处谓词现在同一个函数（`steer_applies_to_run`）。
    """
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None
    from agent_harness.session import Session

    store = JsonlSessionStore(root=_sessions_root(settings))
    session = Session.load(store, session_id)
    session.append(
        "steer/requested",
        {"steer_id": "steer-session", "content": "换个做法",
         "created_at": "2026-09-27T00:00:00Z"},
    )
    before = store.read_events(session_id)

    with pytest.raises(BudgetConflict, match="没有"):
        await resume_command(
            session_id, expected_version=paused.version,
            basis=RESUME_BASIS_RELEVANT_STEER, write=lambda _text: None,
        )
    after = store.read_events(session_id)
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_a_resume_without_a_policy_declaration_cannot_forge_a_policy_change(
    monkeypatch, tmp_path,
):
    """本请求不声明任何策略面 ⇒ 恢复侧**还原**暂停时那一套再比 ⇒ 409「相同」。

    `#317` T9 审查 P1 的回归：暂停快照记的是"那次执行生效的策略面"，恢复侧过去只拿本次
    请求声明的 amend 重算——两次请求的字段集合不同（CLI 的 resume 一个都不声明），摘要
    必不同，于是 `policy_change` 在"其实什么都没变"的恢复上成立（fail-open：无依据放行）。

    两件事一起断言：
    1. 空 amend + `policy_change` ⇒ 409「相同」，零副作用；
    2. 换一条依据（steer）恢复时，恢复腿**真的**按暂停时那一档跑——第二次 stuck 暂停的
       快照里 profile / effort 仍是暂停侧声明过的值（不是悄悄回落默认）。
    """
    settings = _settings(tmp_path)
    settings.agent_models = json.dumps([{
        "name": "deepseek-chat",
        "provider": "deepseek",
        "model_name": "deepseek-chat",
        "reasoning_effort": {
            "supported": ["deep"],
            "default": "deep",
            "wire_mapping": {"deep": "high"},
        },
    }])
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(
            responses=[_failing_round(index) for index in range(6)]
            + [_continuation_json()],
        ),
    )
    from agent_harness.session.amend import AmendOptions

    service = await cli._cli_session_service(settings)
    created = await service.create_and_launch(
        task="反复用同一个工具",
        amend=AmendOptions(agent_profile="coding", reasoning_effort="deep"),
    )
    await created.run.task
    session_id = created.session.session_id
    store = JsonlSessionStore(root=_sessions_root(settings))
    events = store.read_events(session_id)
    paused = latest_paused_run(events)
    assert paused is not None and paused.reason == REASON_STUCK
    recorded = (paused.stuck or {})["policy_inputs"]
    assert recorded["agent_profile"] == "coding"
    assert recorded["reasoning_effort"] == "deep"

    with pytest.raises(BudgetConflict, match="相同"):
        await service.resume_and_launch(
            session_id=session_id, task=None, resume_run_id=paused.run_id,
            resume_basis=RESUME_BASIS_POLICY_CHANGE, expected_version=paused.version,
        )
    assert [event.type for event in store.read_events(session_id)] == [
        event.type for event in events
    ]

    _append_steer(settings, session_id)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(
            responses=[_failing_round(index) for index in range(6, 8)]
            + [_continuation_json()],
        ),
    )
    resumed = await service.resume_and_launch(
        session_id=session_id, task=None, resume_run_id=paused.run_id,
        resume_basis=RESUME_BASIS_RELEVANT_STEER, expected_version=paused.version,
    )
    await resumed.run.task
    after = store.read_events(session_id)
    pauses = [event for event in after if event.type == RUN_PAUSED]
    assert len(pauses) == 2, "恢复腿应当再次撞上同一个循环（计数跨执行累积）"
    second_inputs = pauses[1].data["stuck"]["policy_inputs"]
    assert second_inputs["agent_profile"] == "coding"
    assert second_inputs["reasoning_effort"] == "deep"


@pytest.mark.asyncio
async def test_the_second_pause_is_the_restore_source_for_the_next_resume(
    monkeypatch, tmp_path,
):
    """同一个 run 暂停两次、两次策略面不同 ⇒ 第二次的基线与还原源是**第二份快照**。

    `#317` T9 二轮审查 P1 的端到端回归：还原源取"第一条带逐维值的暂停"时，两侧比的是两
    份不同的快照（① coding / ② main）——一次**不声明任何策略**的 `policy_change` 恢复因此
    摘要必不同 ⇒ 被放行（fail-open），而且恢复腿会跑回**第一次**暂停的档位（coding）。
    判据侧的基线是 `latest_paused_run`（最新那条），所以还原源也必须是最新那条。
    """
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(
            responses=[_failing_round(index) for index in range(6)]
            + [_continuation_json()],
        ),
    )
    from agent_harness.session.amend import AmendOptions

    service = await cli._cli_session_service(settings)
    created = await service.create_and_launch(
        task="反复用同一个工具", amend=AmendOptions(agent_profile="coding"),
    )
    await created.run.task
    session_id = created.session.session_id
    store = JsonlSessionStore(root=_sessions_root(settings))
    first = latest_paused_run(store.read_events(session_id))
    assert first is not None and first.reason == REASON_STUCK
    assert (first.stuck or {})["policy_inputs"]["agent_profile"] == "coding"

    # 一次**真实**的策略变更：声明 main ⇒ 依据成立、恢复腿跑 main，随后又撞上同一个循环。
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(
            responses=[_failing_round(index) for index in range(6, 8)]
            + [_continuation_json()],
        ),
    )
    changed = await service.resume_and_launch(
        session_id=session_id, task=None, resume_run_id=first.run_id,
        resume_basis=RESUME_BASIS_POLICY_CHANGE, expected_version=first.version,
        amend=AmendOptions(agent_profile="main"),
    )
    await changed.run.task
    after = store.read_events(session_id)
    pauses = [event for event in after if event.type == RUN_PAUSED]
    assert len(pauses) == 2, "恢复腿应当再次撞上同一个循环"
    second_inputs = pauses[1].data["stuck"]["policy_inputs"]
    assert second_inputs["agent_profile"] == "main", "恢复腿必须跑在本次声明的档位上"

    # 现在基线 + 还原源都必须是第二份快照（main）：不声明的 policy_change ⇒ 409「相同」。
    second = latest_paused_run(after)
    assert second is not None and second.version > first.version
    with pytest.raises(BudgetConflict, match="相同"):
        await service.resume_and_launch(
            session_id=session_id, task=None, resume_run_id=first.run_id,
            resume_basis=RESUME_BASIS_POLICY_CHANGE, expected_version=second.version,
        )
    assert [event.type for event in store.read_events(session_id)] == [
        event.type for event in after
    ]


def _rewrite_pause_stuck(
    settings: Settings, session_id: str, mutate,
) -> None:
    """直接重写 JSONL 里那条 `run/paused` 的 `stuck` 载荷（伪造一份老/畸形快照）。"""
    path = _sessions_root(settings) / session_id / "events.jsonl"
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("type") == RUN_PAUSED and isinstance(payload.get("data"), dict):
            stuck = payload["data"].get("stuck")
            if isinstance(stuck, dict):
                mutate(stuck)
        lines.append(json.dumps(payload, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _drop_policy_inputs(stuck: dict) -> None:
    stuck.pop("policy_inputs", None)


def _malform_agent_profile(stuck: dict) -> None:
    """把 profile 写成 list，**并让摘要跟着它走**：这样拒它的只剩形状那一关。"""
    from agent_harness.agent.resume_evidence import digest_policy_inputs

    stuck["policy_inputs"]["agent_profile"] = ["not-a-string"]
    stuck["policy_version"] = digest_policy_inputs(stuck["policy_inputs"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate", [_drop_policy_inputs, _malform_agent_profile],
    ids=["legacy_no_inputs", "malformed_profile"],
)
async def test_a_snapshot_that_cannot_be_restored_cannot_resume_by_policy(
    monkeypatch, tmp_path, mutate,
):
    """还原不回来的快照 ⇒ `policy_change` 一律 409，且**不是** 500。

    两种形状（`#317` T9 二轮审查 P1/P2 的端到端回归）：

    - **存量快照**：本票之前写进 JSONL 的那种，只记摘要、没有逐维值。它还原不回来，
      "现算再比"就是把"本次请求声明了什么"当基线 ⇒ 没变也判成变了（fail-open 在存量
      会话上复活）。
    - **畸形快照**：逐维值形状非法（`agent_profile` 被写成 list）。旧写法把它直接塞进
      amend 再算摘要，恢复路径以 `TypeError: unhashable type: 'list'` 收场（500），
      而不是以冲突收场。

    同一份判据（`recorded_policy_inputs`）管这两件事，所以这里也一次验两条：都是
    `BudgetConflict`「还原不回来」，都零副作用。环境那条依据不受影响。
    """
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None and paused.run_id is not None

    _rewrite_pause_stuck(settings, session_id, mutate)

    with pytest.raises(BudgetConflict, match="还原不回来"):
        await resume_command(
            session_id, expected_version=paused.version,
            basis=RESUME_BASIS_POLICY_CHANGE, write=lambda _text: None,
        )
    after = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)
    assert len(after) == len(events)


@pytest.mark.asyncio
async def test_resume_without_a_steer_is_rejected_without_side_effects(monkeypatch, tmp_path):
    """没有那一条 steer ⇒ 409，且**什么都不写**（判定在任何落盘之前）。"""
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None

    with pytest.raises(BudgetConflict, match="没有"):
        await resume_command(
            session_id, expected_version=paused.version,
            basis=RESUME_BASIS_RELEVANT_STEER, write=lambda _text: None,
        )

    after = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)
    assert [event.type for event in after] == [event.type for event in events]


@pytest.mark.asyncio
async def test_resume_on_an_unchanged_workspace_is_rejected(monkeypatch, tmp_path):
    """环境没变 ⇒ 409（"我改过了"是声明，不是证据）。"""
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None

    with pytest.raises(BudgetConflict, match="相同"):
        await resume_command(
            session_id, expected_version=paused.version,
            basis=RESUME_BASIS_ENVIRONMENT_CHANGE, write=lambda _text: None,
        )
    after = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)
    assert len(after) == len(events)


@pytest.mark.asyncio
async def test_resume_after_a_workspace_change_is_accepted(monkeypatch, tmp_path):
    """工作区真的变了（多了一个文件）⇒ 环境依据成立。"""
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None
    # 改的是**会话自己的工作区**（`workspaces/<session_id>`），不是 `workspace_dir`
    # ——快照照会话工作区算，往别处写文件不构成"环境变了"（这正是本用例要证的边）。
    workspace = Path(settings.workspace_dir) / "workspaces" / session_id
    (workspace / "user-edited.txt").write_text("我改过这里", encoding="utf-8")

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[AIMessage(content="在改过的工作区里继续")]),
    )
    outcome = await resume_command(
        session_id, expected_version=paused.version,
        basis=RESUME_BASIS_ENVIRONMENT_CHANGE, write=lambda _text: None,
    )

    assert outcome.paused is False
    after = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)
    evidence = next(
        event.data["resume_evidence"] for event in after if event.type == RUN_RESUMED
    )
    assert evidence["basis"] == RESUME_BASIS_ENVIRONMENT_CHANGE
    assert evidence["environment_revision"] != evidence["recorded"]


@pytest.mark.asyncio
async def test_resume_on_an_unchanged_policy_is_rejected(monkeypatch, tmp_path):
    """策略没变（模型 / 档位 / profile 都一样）⇒ 409。"""
    _settings_obj, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None

    with pytest.raises(BudgetConflict, match="相同"):
        await resume_command(
            session_id, expected_version=paused.version,
            basis=RESUME_BASIS_POLICY_CHANGE, write=lambda _text: None,
        )


@pytest.mark.asyncio
async def test_resume_after_a_model_change_is_accepted(monkeypatch, tmp_path):
    """换了模型 ⇒ 策略版本确实变了 ⇒ 策略依据成立。

    模型名是策略摘要的一维（`policy_version_of`）：暂停侧从启动请求的 amend 算，
    恢复侧从本次请求的 amend 算——两侧同一份算法，所以"变了"是真的变了。

    catalog 是会话级选模型的**准入门**（未知名字 422/ConfigError）：这里给它一条
    `another-model`，让"换模型"走真实解析路径而不是绕过校验。
    """
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None
    settings.agent_models = json.dumps(
        [{"name": "another-model", "provider": "deepseek"}]
    )

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[AIMessage(content="换了模型继续")]),
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    # 走 Web 同一份领域入口：amend 带一个不同的模型名
    from agent_harness.session.amend import AmendOptions

    service = await cli._cli_session_service(settings)
    result = await service.resume_and_launch(
        session_id=session_id, task=None, resume_run_id=paused.run_id,
        resume_basis=RESUME_BASIS_POLICY_CHANGE, expected_version=paused.version,
        amend=AmendOptions(model="another-model"),
    )
    assert result.run is not None
    await result.run.task

    after = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)
    evidence = next(
        event.data["resume_evidence"] for event in after if event.type == RUN_RESUMED
    )
    assert evidence["basis"] == RESUME_BASIS_POLICY_CHANGE
    assert evidence["policy_version"] != evidence["recorded"]
    assert [event for event in after if event.type == RUN_COMPLETED]


@pytest.mark.asyncio
async def test_budget_increase_is_rejected_for_a_stuck_pause(monkeypatch, tmp_path):
    """抬高 ceiling **不是** stuck 的依据（`02 §5.3`）：409，零副作用。"""
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None

    with pytest.raises(BudgetConflict, match="budget_increase"):
        await resume_command(
            session_id, run_turns_total=99, expected_version=paused.version,
            basis=RESUME_BASIS_BUDGET_INCREASE, write=lambda _text: None,
        )
    after = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)
    assert len(after) == len(events)
    assert not [event for event in after if event.type == STEER_APPLIED]


@pytest.mark.asyncio
async def test_a_stale_version_is_a_conflict_not_a_launch(monkeypatch, tmp_path):
    """CAS 照旧：版本对不上 ⇒ 409，且不写事件（stuck 没有旁路）。"""
    settings, session_id, events = await _run_to_stuck_pause(monkeypatch, tmp_path)
    paused = latest_paused_run(events)
    assert paused is not None
    _append_steer(settings, session_id)
    before = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)

    with pytest.raises(BudgetConflict, match="过期"):
        await resume_command(
            session_id, expected_version=paused.version + 1,
            basis=RESUME_BASIS_RELEVANT_STEER, write=lambda _text: None,
        )
    after = JsonlSessionStore(root=_sessions_root(settings)).read_events(session_id)
    assert len(after) == len(before)
