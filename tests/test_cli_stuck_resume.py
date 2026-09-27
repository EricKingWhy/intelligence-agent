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

from agent_harness import cli
from agent_harness.agent.budget import BudgetConflict
from agent_harness.agent.run_budget import (
    REASON_STUCK,
    RESUME_BASIS_BUDGET_INCREASE,
    RESUME_BASIS_ENVIRONMENT_CHANGE,
    RESUME_BASIS_POLICY_CHANGE,
    RESUME_BASIS_RELEVANT_STEER,
    STUCK_RESUME_REQUIREMENTS,
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
    TOOL_FAILURE_GUARD,
    USER_MESSAGE,
)
from agent_harness.session.event import STEER_APPLIED
from agent_harness.session.store import JsonlSessionStore
from tests.scripted_model import ScriptedModel


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        model_api_key="sk-test",
        workspace_dir=str(tmp_path / "workspace"),
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
    printed: list[str] = []
    outcome = await resume_command(
        session_id, expected_version=paused.version,
        basis=RESUME_BASIS_RELEVANT_STEER, write=printed.append,
    )

    assert outcome.paused is False
    assert outcome.final_text == "这次换了个做法"
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
