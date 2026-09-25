"""T4（`#312`）CLI：暂停摘要 / 恢复指令 / `resume` 接上同一个逻辑 run。

车票 Must Do 里属于 CLI 的那两条：

- 显示 paused 状态与 consumed / limits / version / continuation，且**与 completed /
  failed / interrupted / NEED_RECONCILE 可区分**（显示面读的是 `run/paused` 的 durable
  data，不是进程内记忆——重启/replay 后同一事件给同一段文本）；
- 恢复接受**新的绝对 ceiling** 与 expected version，并接上同一个 run（CLI 与 Web 走
  同一份 CAS 实现：`agent_harness.cli._cli_session_service` 复用组合根）。

最后一组用例是**真链路**：`cli.run(run_turns_total=1)` 跑出一个真暂停 → 从磁盘读回
session_id → `resume_command(run_turns_total=4)` 让同一个 run 跑完。全程不 mock 事件流。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_harness import cli
from agent_harness.agent.budget import BudgetConflict
from agent_harness.cli import (
    _main_resume,
    render_pause_block,
    render_resume_block,
    resume_command,
    resume_hint,
)
from agent_harness.config import Settings
from agent_harness.session import (
    RUN_COMPLETED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    USER_MESSAGE,
)
from agent_harness.session.store import JsonlSessionStore
from tests.scripted_model import ScriptedModel

#: 一条完整的暂停事实（字段与 `build_pause_data` 同形；测试不重造片段）。
_PAUSE_DATA: dict = {
    "reason": "budget_exhausted",
    "trigger_dimension": "run.max_agent_turns_total",
    "budget_version": 1,
    "consumed": {"agent_turns": 3},
    "limits": {
        "local": {"max_agent_turns": 500, "source": "deployment"},
        "run": {"max_agent_turns_total": 8},
    },
    "continuation": {
        "completed": ["已读完配置", "已改完 A"],
        "remaining": ["还要跑测试"],
        "blockers": ["缺一个测试 fixture"],
        "next_safe_action": "先跑 pytest",
    },
    "closeout_source": "model",
    "resume_requirements": [],
}


def _continuation_json() -> AIMessage:
    return AIMessage(content=json.dumps({
        "completed": ["已读完配置"],
        "remaining": ["还要跑测试"],
        "blockers": [],
        "next_safe_action": "先跑 pytest",
    }, ensure_ascii=False))


# ── 渲染：暂停 / 恢复与终态可区分 ──────────────────────────────────────


def test_pause_block_shows_reason_counters_and_continuation():
    """reason / trigger dimension / version / consumed / limits / continuation 全在。"""
    text = render_pause_block(_PAUSE_DATA)
    assert "[run paused]" in text
    assert "reason=budget_exhausted" in text
    assert "dimension=run.max_agent_turns_total" in text
    assert "version=1" in text
    assert "consumed 3 / limit 8 (remaining 5)" in text
    assert "local fuse 500" in text
    assert "closeout=model" in text
    assert "completed: 已读完配置" in text and "已改完 A" in text
    assert "remaining: 还要跑测试" in text
    assert "blockers: 缺一个测试 fixture" in text
    assert "next: 先跑 pytest" in text
    # 与终态**可区分**：暂停块不带 failed / completed 字样（票面 Must Do）
    assert "[run failed]" not in text
    assert "[run completed]" not in text
    assert "resume:" not in text, "恢复指令由 resume_hint 给（要 session_id），不在这里"


def test_pause_block_never_substitutes_zero_for_missing_numbers():
    """没有 ceiling / 没有消耗快照 ⇒ 显示 unavailable / unlimited，不用 0 顶替（`11 §6.1`）。"""
    text = render_pause_block({
        "reason": "budget_exhausted",
        "trigger_dimension": "local.max_agent_turns",
        "budget_version": 2,
        "limits": {"local": {"max_agent_turns": 3}, "run": {"max_agent_turns_total": None}},
        "closeout_source": "deterministic",
        "resume_requirements": ["需要人工裁决"],
    })
    assert "consumed unavailable / limit unlimited (remaining unavailable)" in text
    assert "resume requirements: 需要人工裁决" in text
    assert "continuation" not in text, "空 continuation 不渲染空壳段落"

    # 有消耗、没有 ceiling（local fuse 撞线）：remaining 仍然 unavailable——
    # 它无从计算（不是 0），也不能拿消耗本身顶替（那是"剩余"的反面）。
    local_only = render_pause_block({
        "reason": "budget_exhausted",
        "trigger_dimension": "local.max_agent_turns",
        "budget_version": 1,
        "consumed": {"agent_turns": 3},
        "limits": {"local": {"max_agent_turns": 500}, "run": {"max_agent_turns_total": None}},
        "closeout_source": "model",
        "resume_requirements": [],
    })
    assert "consumed 3 / limit unlimited (remaining unavailable)" in local_only
    assert "remaining 0" not in local_only


def test_resume_hint_gives_absolute_ceiling_placeholder():
    """恢复指令用占位符给**绝对** ceiling：CLI 不替用户猜一个数字（猜出来会被当策略）。"""
    text = resume_hint("sess-42", data=_PAUSE_DATA)
    assert "agent-harness resume sess-42" in text
    assert "--run-turns-total N" in text
    assert "--expected-version 1" in text
    assert "绝对" in text and "不是增量" in text


def test_resume_block_reports_version_step_and_carried_consumed():
    text = render_resume_block({
        "resume_basis": "budget_increase",
        "previous_budget_version": 1,
        "budget_version": 2,
        "from_pause_seq": 7,
        "consumed": {"agent_turns": 3},
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": 8},
        },
    })
    assert "[run resumed]" in text
    assert "basis=budget_increase" in text
    assert "version=1→2" in text
    assert "from_pause_seq=7" in text
    assert "carried consumed 3 / limit 8 (remaining 5)" in text


# ── 真链路：run 暂停 → resume 接上同一个 run ──────────────────────────────


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        model_api_key="sk-test",
        workspace_dir=str(tmp_path / "workspace"),
        _env_file=None,  # 不吃仓库根 .env（测试必须自足）
    )


def _sessions_root(settings: Settings) -> Path:
    return Path(settings.workspace_dir) / "sessions"


def _only_session_id(settings: Settings) -> str:
    """磁盘上唯一的会话 id（布局 `<root>/<session_id>/events.jsonl`）。"""
    session_ids = [
        path.parent.name for path in _sessions_root(settings).glob("*/events.jsonl")
    ]
    assert len(session_ids) == 1, f"应有且仅有一个会话，实际 {session_ids}"
    return session_ids[0]


@pytest.mark.asyncio
async def test_cli_pause_then_resume_completes_the_same_run(monkeypatch, tmp_path):
    """`run --run-turns-total 1` 暂停 → `resume --run-turns-total 4` 同一个 run 完成。

    `run` 是"创建 + 第一条消息"入口，所以这里默认 local fuse = 500（Deployment 默认），
    撞顶的只能是 run 档 ceiling——两条链路的账本快照因此分开可辨。
    """
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[_continuation_json()]),
    )

    printed: list[str] = []
    outcome = await cli.run("把 A 改成 B", run_turns_total=1, write=printed.append)

    assert outcome.paused is True, "撞到 run ceiling ⇒ 暂停（不是失败，也不是空回答）"
    assert outcome.final_text == ""
    text = "".join(printed)
    assert "[run paused]" in text
    assert "reason=budget_exhausted" in text
    assert "dimension=run.max_agent_turns_total" in text
    # ceiling=1 连一个产出轮都放行不了（判定含预留）⇒ 计数是 0；closeout 那一次是
    # model_requests，不进 agent_turns（`02 §5.1` 的七个 counter 互不混同）。
    assert "consumed 0 / limit 1" in text

    # session_id 从磁盘读回（CLI 的一次性命令打印它，正是为了让用户能恢复）
    session_id = _only_session_id(settings)
    assert f"agent-harness resume {session_id}" in text, "恢复指令必须指名本会话"

    store = JsonlSessionStore(root=_sessions_root(settings))
    paused_events = store.read_events(session_id)
    types = [e.type for e in paused_events]
    assert types.count(RUN_PAUSED) == 1
    assert RUN_COMPLETED not in types, "暂停不是终态"
    paused_run_id = next(e.run_id for e in paused_events if e.type == RUN_PAUSED)

    # 恢复：换剧本（第二次构造模型 = 第二次执行），接上同一个 run
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[AIMessage(content="A 已改完")]),
    )
    printed_again: list[str] = []
    resumed = await resume_command(
        session_id, run_turns_total=4, expected_version=1, write=printed_again.append,
    )

    assert resumed.paused is False
    assert resumed.final_text == "A 已改完"
    text_again = "".join(printed_again)
    assert "[run paused]" in text_again, "先打印暂停摘要（读的是事件，不是进程内状态）"
    assert "[run resumed]" in text_again
    assert "version=1→2" in text_again
    assert "A 已改完" in text_again
    assert "resume:" not in text_again, "跑完了就不该再给恢复指令"

    events = store.read_events(session_id)
    types = [e.type for e in events]
    assert types.count(RUN_STARTED) == 1, "同 run 续跑不新建 run"
    assert types.count(USER_MESSAGE) == 1, "普通预算恢复不需要新任务文本"
    assert types.count(RUN_RESUMED) == 1
    assert types.count(RUN_COMPLETED) == 1
    assert {e.run_id for e in events if e.run_id} == {paused_run_id}


@pytest.mark.asyncio
async def test_cli_resume_rejects_a_stale_version_without_side_effects(monkeypatch, tmp_path):
    """版本过期 ⇒ 领域异常向上抛（`_main_resume` 翻成 exit 1），事件流一字不改。"""
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[_continuation_json()]),
    )
    await cli.run("先撞一下 ceiling", run_turns_total=1, write=lambda _t: None)

    session_id = _only_session_id(settings)
    store = JsonlSessionStore(root=_sessions_root(settings))
    before = [e.to_dict() for e in store.read_events(session_id)]

    with pytest.raises(BudgetConflict):
        await resume_command(
            session_id, run_turns_total=4, expected_version=99, write=lambda _t: None,
        )
    after = [e.to_dict() for e in store.read_events(session_id)]
    assert after == before, "被拒请求不得落任何事件（含 run/resumed）"


@pytest.mark.asyncio
async def test_cli_resume_without_a_paused_run_is_a_clear_error(monkeypatch, tmp_path):
    """没有暂停事实 ⇒ 明确报错，不静默新建一个 run 假装恢复。"""
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[AIMessage(content="ok")]),
    )
    # 一个**已正常完成**的会话：没有暂停可恢复
    await cli.run("跑完就结束", write=lambda _t: None)
    session_id = _only_session_id(settings)
    store = JsonlSessionStore(root=_sessions_root(settings))
    before = [e.to_dict() for e in store.read_events(session_id)]

    with pytest.raises(BudgetConflict, match="不在暂停态"):
        await resume_command(
            session_id, run_turns_total=4, expected_version=1, write=lambda _t: None,
        )

    assert [e.to_dict() for e in store.read_events(session_id)] == before


def test_main_resume_maps_domain_rejection_to_exit_code(monkeypatch, capsys):
    """`_main_resume` 把领域拒绝翻成 exit 1 + stderr 一句话（不重试、不猜）。"""
    async def _reject(*args, **kwargs):
        raise BudgetConflict("budget version 过期：expected_version=99，当前 version=1")

    monkeypatch.setattr(cli, "resume_command", _reject)
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None, model_api_key="sk-test"))

    with pytest.raises(SystemExit) as excinfo:
        _main_resume(["sess-1", "--run-turns-total", "4", "--expected-version", "99"])

    assert excinfo.value.code == 1
    assert "resume 被拒绝" in capsys.readouterr().err
