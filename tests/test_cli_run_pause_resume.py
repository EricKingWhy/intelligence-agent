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
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    TOOL_FAILURE_GUARD,
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


# ── `#313` T5：四维的显示面与开关 ─────────────────────────────────────────


def test_pause_block_renders_the_extra_dimensions_only_when_they_carry_facts():
    """有新维度事实 ⇒ 逐维一行；老暂停事件（只有 turns 一维）⇒ 输出逐字不变。

    老事件多打三行 `unavailable / unlimited` 是噪声：那份投影里根本没有那三维的
    事实，渲染出来的"unavailable"会被读成"这次暂停与它们有关"。
    """
    legacy = render_pause_block(_PAUSE_DATA)
    assert "total_tokens" not in legacy and "cost_usd" not in legacy

    four_dim = render_pause_block({
        **_PAUSE_DATA,
        "trigger_dimension": "run.max_total_tokens",
        "consumed": {"agent_turns": 3, "model_requests": 7, "total_tokens": 900,
                     "cost_usd": "0.0125"},
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": 8, "max_model_requests": 9,
                    "max_total_tokens": 1000, "max_cost_usd": "0.02"},
        },
    })
    assert "model_requests: consumed 7 / limit 9 (remaining 2)" in four_dim
    assert "total_tokens: consumed 900 / limit 1000 (remaining 100)" in four_dim
    # cost 在 wire 上是十进制字符串 ⇒ 差值在 Decimal 里算，不引入浮点近似
    assert "cost_usd: consumed 0.0125 / limit 0.02 (remaining 0.0075)" in four_dim
    assert "turns: consumed 3 / limit 8 (remaining 5)" in four_dim


def test_pause_block_says_unavailable_instead_of_zero_for_unknown_dimensions():
    """账目未知（没报 usage）⇒ 那一维显示 unavailable，不显示 0（`11 §6.1`）。"""
    text = render_pause_block({
        **_PAUSE_DATA,
        "consumed": {"agent_turns": 1, "model_requests": 2,
                     "total_tokens": None, "cost_usd": None},
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": 8, "max_total_tokens": 1000},
        },
    })
    assert "total_tokens: consumed unavailable / limit 1000 (remaining unavailable)" in text
    assert "total_tokens: consumed 0" not in text
    assert "cost_usd: consumed unavailable / limit unlimited (remaining unavailable)" in text


def test_resume_hint_names_the_flag_of_the_tripped_dimension():
    """恢复指令必须给一个**抬得动**这次暂停的开关（按 trigger_dimension 取）。

    写死 `--run-turns-total` 在 token 维度暂停时是一句假指令：照抄执行会撞 409
    （turn 维没配过，等于"没真提高"）——而 CLI 显示的应当是 durable 事实本身。
    """
    cases = {
        "run.max_agent_turns_total": "--run-turns-total",
        "run.max_model_requests": "--run-model-requests",
        "run.max_total_tokens": "--run-total-tokens",
        "run.max_cost_usd": "--run-cost-usd",
        # 本票之外的暂停原因（local fuse / 未来维度）回落到任何 run 都读得懂的 turns 开关
        "local.max_agent_turns": "--run-turns-total",
    }
    for dimension, flag in cases.items():
        text = resume_hint("sess-42", data={**_PAUSE_DATA, "trigger_dimension": dimension})
        assert f"{flag} N" in text, f"{dimension} 应给 {flag}：{text}"
        assert "--expected-version 1" in text


def test_resume_hint_gives_the_per_tool_flag_in_its_real_argv_shape():
    """`#314`：per-tool 维度的提示必须是 `--run-tool-limit <name>=N`。

    这个开关的名字与值在**同一个 argv** 里（`NAME=N`），与四维的 `--flag N` 不同。
    只给开关名再拼一个 ` N` 会得到 `--run-tool-limit glob N`——argparse 会把它读成
    "缺 NAME=N 形式"，照抄就报错：提示的全部价值在"照抄能跑"。
    """
    text = resume_hint(
        "sess-42",
        data={**_PAUSE_DATA, "trigger_dimension": "run.tool_call_limits.glob"},
    )

    assert "--run-tool-limit glob=N" in text
    assert "--expected-version 1" in text


def test_resume_hint_states_the_ceiling_rule_of_the_tripped_dimension():
    """尾句的"N 该比什么大"必须与**该维**的实现一致（两轴审查 Standards 面发现）。

    turns / requests 的 ceiling 判定含一次 closeout 预留（`02 §5.2`：保证暂停时还收得了
    口），per-tool 维**不**含——closeout 是一次模型请求、不产生任何工具调用，给它留一格会
    让"配额 = 3"实际允许 4 次调用（`run_budget._dimension_reached` 第三类临界点 / ADR-0045
    D4）。一条四维共用的尾句会在工具维上写出一句与实现相反的话（前端同级提示给的是
    "至少 calls+1"，两处必须说同一件事）。
    """
    tool = resume_hint(
        "sess-42", data={**_PAUSE_DATA, "trigger_dimension": "run.tool_call_limits.glob"},
    )
    assert "严格大于已接纳的调用数" in tool
    assert "必须高于 consumed + 预留 closeout 轮" not in tool

    turns = resume_hint(
        "sess-42", data={**_PAUSE_DATA, "trigger_dimension": "run.max_agent_turns_total"},
    )
    assert "高于 consumed + 预留 closeout 轮" in turns


def test_parse_run_tool_limits_rejects_ambiguous_argv():
    """`NAME=N` 的坏形状在**命令行层**就拒绝（形状规则本体仍在领域层）。"""
    assert cli.parse_run_tool_limits(None) == {}
    assert cli.parse_run_tool_limits(["glob=2", "bash=3"]) == {"glob": 2, "bash": 3}
    for bad in (["glob"], ["glob=2", "glob=3"], ["glob=many"]):
        with pytest.raises(ValueError):
            cli.parse_run_tool_limits(bad)


def test_pause_block_renders_per_tool_quota_lines():
    """`#314`：摘要里 per-tool 的 calls / attempts / limit 各自成事实（不混同）。

    `tool_calls`（逻辑调用）与 `tool_attempts`（真实尝试）是两个 counter——挤成
    一格会让人以为它们是同一个数（`02 §5.1`）。老事件（两个键都没有）⇒ 零行。
    """
    text = render_pause_block({
        **_PAUSE_DATA,
        "trigger_dimension": "run.tool_call_limits.glob",
        "consumed": {
            "agent_turns": 2, "tool_calls": 3, "tool_attempts": 5,
            "tool_calls_by_tool": {"glob": 2, "bash": 1},
            "tool_attempts_by_tool": {"glob": 4, "bash": 1},
        },
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": None, "tool_call_limits": {"glob": 2}},
        },
    })

    assert "tool glob: consumed 2 calls / 4 attempts / limit 2 (remaining 0)" in text
    # bash 只有账（没配 ceiling）⇒ 仍要显示：它是"上限不限"的事实，不是没有事实
    assert "tool bash: consumed 1 calls / 1 attempts / limit unlimited" in text
    # 老事件（没有 per-tool 两个键）⇒ 一行都不打
    assert "tool " not in render_pause_block(_PAUSE_DATA)


def test_pause_block_reads_a_missing_name_in_a_known_table_as_zero():
    """`#314`：表**已知**而某工具名不在表里 ⇒ 0，不是 unavailable（两轴审查共同发现）。

    反例：配了 ceiling 却从未调用过的工具曾被渲染成 `unavailable calls`，而**同一份**
    durable 事件的服务端投影给的是 `remaining = 2`（`BudgetConsumed.calls_for` 的口径：
    表存在 ⇒ 缺名就是 0）——同一事实两个互相矛盾的读数，正是 `11 §6.1` 要消灭的不一致。
    判据因此是"**表**在不在"（键存在），不是"**名字**在不在"。
    """
    known = render_pause_block({
        **_PAUSE_DATA,
        "trigger_dimension": "run.tool_call_limits.glob",
        "consumed": {
            "agent_turns": 2, "tool_calls": 0, "tool_attempts": 0,
            "tool_calls_by_tool": {}, "tool_attempts_by_tool": {},
        },
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": None, "tool_call_limits": {"glob": 2}},
        },
    })
    assert "tool glob: consumed 0 calls / 0 attempts / limit 2 (remaining 2)" in known
    # 逐条钉：per-tool 行不得出现 unavailable（`turns` 行出现它是对的——那维没配 ceiling）
    assert "tool glob: consumed unavailable" not in known

    # 表**缺席**（`#314` 之前的老事件）⇒ 仍必须说 unavailable：不可得 ≠ 0，这一半不能修丢
    absent = render_pause_block({
        **_PAUSE_DATA,
        "trigger_dimension": "run.tool_call_limits.glob",
        "consumed": {"agent_turns": 2},
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": None, "tool_call_limits": {"glob": 2}},
        },
    })
    assert "tool glob: consumed unavailable calls / unavailable attempts" in absent
    assert "remaining unavailable" in absent


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


def test_main_resume_requires_at_least_one_absolute_ceiling(monkeypatch, capsys):
    """一个 ceiling 都不给 ⇒ 用法错误（exit 2），不当成"恢复成功"。

    只给 session_id 的调用在语义上是"去掉全部 run ceiling"（普通续聊就能做），不是
    "抬高后继续"。CLI 层拒绝比放过去更诚实：放过去会返回一个看不出差别的成功。
    """
    monkeypatch.setattr(cli, "Settings", lambda: Settings(_env_file=None, model_api_key="sk-test"))

    with pytest.raises(SystemExit) as excinfo:
        _main_resume(["sess-1", "--expected-version", "1"])

    assert excinfo.value.code == 2, "argparse 的用法错误码"
    assert "至少给一个绝对 ceiling" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_cli_stops_and_resumes_on_a_non_turn_dimension(monkeypatch, tmp_path):
    """真链路：请求维 ceiling 停下 run → 抬高它 → 同一 run 完成（turn 维没参与）。

    这条证明 CLI 的四个开关**真的接到了账本上**（不是只被 argparse 收下）：
    `--run-model-requests 1` 当场暂停并如实报出命中维度与那一维的读数；恢复那一次把
    requests 抬到位（上限从 1 → 3），`--run-total-tokens 2000` 则把**未到线**的 token
    ceiling 一起抬高；两段摘要里的 token 行都来自 durable 快照（closeout 自报的 usage）。
    """
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    def _usage_answer(content: str, total_tokens: int) -> AIMessage:
        return AIMessage(
            content=content,
            usage_metadata={"input_tokens": total_tokens - 3, "output_tokens": 3,
                            "total_tokens": total_tokens},
        )

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[
            _usage_answer(_continuation_json().content, 20),
        ]),
    )
    printed: list[str] = []
    outcome = await cli.run(
        "把 A 改成 B", run_model_requests=1, run_total_tokens=500, write=printed.append,
    )

    assert outcome.paused is True, "撞到请求维 ceiling ⇒ 暂停（不是失败，也不是空回答）"
    text = "".join(printed)
    assert "dimension=run.max_model_requests" in text
    # 请求维含 closeout 预留 ⇒ ceiling=1 连一次普通请求都放行不了：turns 计数是 0，
    # 而 closeout 那一次请求与它自报的 20 token 都在账上。
    assert "model_requests: consumed 1 / limit 1 (remaining 0)" in text
    assert "total_tokens: consumed 20 / limit 500 (remaining 480)" in text
    assert "--run-model-requests N" in text, "恢复指令给的是被命中那一维的开关"

    session_id = _only_session_id(settings)
    store = JsonlSessionStore(root=_sessions_root(settings))
    paused_run_id = next(
        e.run_id for e in store.read_events(session_id) if e.type == RUN_PAUSED
    )

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[AIMessage(content="A 已改完")]),
    )
    printed_again: list[str] = []
    resumed = await resume_command(
        session_id, run_model_requests=3, run_total_tokens=2000,
        expected_version=1, write=printed_again.append,
    )

    assert resumed.paused is False and resumed.final_text == "A 已改完"
    text_again = "".join(printed_again)
    assert "[run resumed]" in text_again
    assert "model_requests: carried consumed 1 / limit 3 (remaining 2)" in text_again
    assert "total_tokens: carried consumed 20 / limit 2000 (remaining 1980)" in text_again

    events = store.read_events(session_id)
    types = [e.type for e in events]
    assert types.count(RUN_STARTED) == 1 and types.count(RUN_RESUMED) == 1
    assert types.count(RUN_COMPLETED) == 1
    assert {e.run_id for e in events if e.run_id} == {paused_run_id}


# ── `#314` 真链路：per-tool 配额用尽 → 暂停 → 抬高 → 同一个 run 完成 ─────────


def _two_glob_calls() -> AIMessage:
    """一轮里对同一个**真实**工具发两条调用（一次多调用批次的最小形状）。"""
    return AIMessage(
        content="",
        tool_calls=[
            {"id": "call_glob_a", "name": "glob", "args": {"pattern": "**/*.py"}},
            {"id": "call_glob_b", "name": "glob", "args": {"pattern": "**/*.md"}},
        ],
    )


@pytest.mark.asyncio
async def test_cli_tool_quota_blocks_extra_call_and_resume_raises_it(monkeypatch, tmp_path):
    """`run --run-tool-limit glob=1` → 同批第二条被拒 → 暂停 → `resume` 抬高后完成。

    这条用例走的是**真工具链**（`glob` 是装配层注册的真工具，不是替身）：配额在
    接纳点计数（同一批的第一条执行了、第二条根本没执行），用尽后复用暂停生命周期，
    恢复时给一个**更高**的绝对配额（不是增量）就接着跑完——与四维维度同一条链。
    """
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(
            responses=[_two_glob_calls(), _continuation_json()],
        ),
    )

    printed: list[str] = []
    outcome = await cli.run(
        "先看看仓库里有哪些文件", run_tool_limits={"glob": 1}, write=printed.append,
    )

    assert outcome.paused is True
    text = "".join(printed)
    assert "dimension=run.tool_call_limits.glob" in text
    # 一条被接纳（执行了）+ 一条被拒（没执行）：两个 counter 与 limit 各自成行
    assert "tool glob: consumed 1 calls / 1 attempts / limit 1 (remaining 0)" in text
    assert "--run-tool-limit glob=N" in text, "恢复指令给的是被命中那一维的开关"

    session_id = _only_session_id(settings)
    store = JsonlSessionStore(root=_sessions_root(settings))
    events = store.read_events(session_id)
    results = [e for e in events if e.type == "tool/result"]
    assert len(results) == 2, "两条 tool/call 各有结果（保序配对）"
    payloads = [json.loads(e.data["content"]) for e in results]
    assert [p["ok"] for p in payloads] == [True, False]
    assert payloads[1]["error_code"] == "BUDGET_EXHAUSTED"
    assert [e.data["budget_delta"]["tool_calls"] for e in results] == [1, 0]
    paused_run_id = next(e.run_id for e in events if e.type == RUN_PAUSED)

    # 抬高**绝对**配额（1 → 3）⇒ 同一工具再次可用，同一个 run 跑完
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(
            responses=[
                AIMessage(content="", tool_calls=[
                    {"id": "call_glob_c", "name": "glob", "args": {"pattern": "**/*.txt"}},
                ]),
                AIMessage(content="看完了"),
            ],
        ),
    )
    printed_again: list[str] = []
    resumed = await resume_command(
        session_id, run_tool_limits={"glob": 3}, expected_version=1,
        write=printed_again.append,
    )

    assert resumed.paused is False, "".join(printed_again)
    assert resumed.final_text == "看完了"
    assert "tool glob: carried consumed 1 calls / 1 attempts / limit 3 (remaining 2)" in (
        "".join(printed_again)
    )

    final_events = store.read_events(session_id)
    types = [e.type for e in final_events]
    assert types.count(RUN_PAUSED) == 1, "抬高之后不再暂停（计数是累计的：1 + 1 < 3）"
    assert types.count(RUN_RESUMED) == 1 and types.count(RUN_COMPLETED) == 1
    assert {e.run_id for e in final_events if e.run_id} == {paused_run_id}


@pytest.mark.asyncio
async def test_quota_rejections_do_not_trip_the_repeated_failure_guard(monkeypatch, tmp_path):
    """`#314`：配额拒绝不喂同错熔断护栏，run 仍以**可恢复**的 `run/paused` 收口。

    反例（两轴审查 Correctness 面发现）：`BUDGET_EXHAUSTED` 是 `ok=False`，与真实工具失败
    同一条路 ⇒ 单条 assistant 消息里 ≥7 条**同参数**调用（第 1 条被接纳，其余 6+ 条在准入
    前被拒）会在护栏那里攒到 SOFT（注入一条声称"连续失败 3 次"的纠正消息）再到 HARD
    （`end_run(failed)`）。而护栏在工具批次末尾、暂停判定在循环顶 ⇒ **终态**
    `run/failed` 抢先，`04 §9.1` 的"耗尽 ⇒ 暂停 / 可恢复"当场落空
    （`run/failed` 之后 `validate_resume` / `latest_paused_run` 都不认它）。

    配额拒绝既不是工具失败（那条调用根本没执行），也不是模型在死循环——预算是照着设计
    到顶的，所以它不喂护栏；**其余**准入前拒绝（参数非法 / 未注册工具）仍照喂，
    ADR-0014 决策 2-6 的 #69 语义不变（见 `tests/agent/test_repeated_tool_failure*.py`）。
    """
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    # 8 条**同参数**调用 = 同一指纹（护栏按指纹计数）；配额 1 ⇒ 第 1 条被接纳、7 条被拒
    same_args = {"pattern": "**/*.txt"}
    batched = AIMessage(content="", tool_calls=[
        {"id": f"call_glob_{index}", "name": "glob", "args": same_args}
        for index in range(8)
    ])
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[batched, _continuation_json()]),
    )

    printed: list[str] = []
    outcome = await cli.run(
        "把文件列一遍", run_tool_limits={"glob": 1}, write=printed.append,
    )

    assert outcome.paused is True, "配额耗尽必须停在可恢复的暂停上，不是终态失败"
    text = "".join(printed)
    assert "dimension=run.tool_call_limits.glob" in text
    assert "tool glob: consumed 1 calls / 1 attempts / limit 1 (remaining 0)" in text

    events = JsonlSessionStore(root=_sessions_root(settings)).read_events(
        _only_session_id(settings)
    )
    types = [e.type for e in events]
    assert types.count(TOOL_FAILURE_GUARD) == 0, "拒绝不是失败：护栏一次都不该触发"
    assert types.count(RUN_PAUSED) == 1 and RUN_FAILED not in types
    # 纠正消息是护栏 SOFT 的产物（`injected_by=tool_failure_guard`）⇒ 一条都不该有
    assert [e.data.get("injected_by") for e in events if e.type == USER_MESSAGE] == [None]

    results = [e for e in events if e.type == "tool/result"]
    payloads = [json.loads(e.data["content"]) for e in results]
    assert [p["ok"] for p in payloads] == [True] + [False] * 7
    assert {p.get("error_code") for p in payloads[1:]} == {"BUDGET_EXHAUSTED"}
    # 账仍然只加 1：被拒的 7 条各带显式 0（`04 §9.1` 的可审计零）
    assert [e.data["budget_delta"]["tool_calls"] for e in results] == [1] + [0] * 7


# ── `#315` T7：deadline 维的 CLI 面 ─────────────────────────────────────


def test_resume_hint_gives_a_real_instant_for_the_deadline_dimension():
    """deadline 的提示必须是一条**能照抄**的命令：值给时刻，判据给"未来"。

    为什么 deadline 要单开一条分支：通用拼装会给出
    `--run-deadline N（N 是**绝对** ceiling，必须高于 consumed + 预留 closeout 轮）`
    ——值不是数字（是 RFC 3339 时刻），判据也不是"比消耗大"（是"必须是一个未来时刻"）。
    per-tool 维当初就是因为同类问题（`--run-tool-limit bash N` 跑不起来）单开一条分支。
    """
    hint = resume_hint("sess-42", data={
        **_PAUSE_DATA,
        "reason": "deadline",
        "trigger_dimension": "run.deadline_at",
    })

    assert "--run-deadline 2026-09-26T04:30:00Z" in hint, "值的位置给的是时刻，不是 N"
    assert "新的未来时刻" in hint
    assert "必须高于 consumed" not in hint
    assert "--expected-version 1" in hint, "恢复请求仍要带版本（CAS 的比较对象）"


def test_pause_block_renders_the_deadline_line():
    """暂停摘要里 deadline 那一行的读数来自 durable 快照（不读进程内状态）。"""
    text = render_pause_block({
        **_PAUSE_DATA,
        "reason": "deadline",
        "trigger_dimension": "run.deadline_at",
        "limits": {
            "local": {"max_agent_turns": 500, "source": "deployment"},
            "run": {"max_agent_turns_total": None, "deadline_at": "2026-09-26T04:10:00Z"},
        },
    })

    assert "reason=deadline" in text
    assert "dimension=run.deadline_at" in text


def test_main_run_accepts_a_past_deadline_as_a_ceiling(monkeypatch, tmp_path):
    """`--run-deadline` 也算"给了 ceiling"：不给它则连"至少一个"的闸门都过不了。

    闸门本身（`--run-turns-total` 那种）由 `test_main_resume_requires_at_least_one_absolute_ceiling`
    钉住；这里证明 deadline 被算进那一族——否则"只给 deadline 的恢复"会被当作
    "去掉全部 ceiling"，而那正是 deadline 暂停唯一需要的依据。
    """
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    seen: dict = {}

    async def _resume(session_id, **kwargs):
        seen.update(kwargs)
        from agent_harness.cli import RunOutcome

        return RunOutcome(final_text="ok")

    monkeypatch.setattr(cli, "resume_command", _resume)
    _main_resume([
        "sess-1", "--run-deadline", "2026-09-26T04:30:00Z", "--expected-version", "1",
    ])

    assert seen["run_deadline_at"] == "2026-09-26T04:30:00Z", "开关值原样透传到领域层"


@pytest.mark.asyncio
async def test_cli_immediate_deadline_pauses_and_resume_uses_a_new_instant(
    monkeypatch, tmp_path,
):
    """真链路：`run --run-deadline <已过去>` 当场暂停 → 换一个未来时刻恢复。

    两段账都从磁盘事件读回（`run/paused` 的 `limits.run.deadline_at` 是快照事实），
    且第一次执行**一次 Provider 请求都没发**（到点后连 closeout 都不发）。
    """
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    models: list[ScriptedModel] = []

    def _factory(config, **kw):
        model = ScriptedModel(responses=[AIMessage(content="做完了")])
        models.append(model)
        return model

    monkeypatch.setattr("agent_harness.assembly.create_chat_model", _factory)

    printed: list[str] = []
    outcome = await cli.run(
        "做完这件事", run_deadline_at="2026-01-01T00:00:00Z", write=printed.append,
    )

    assert outcome.paused is True
    text = "".join(printed)
    assert "reason=deadline" in text
    assert "dimension=run.deadline_at" in text
    assert "--run-deadline 2026-09-26T04:30:00Z" in text, "下一步给的是新时刻的示例"
    assert all(model.snapshots == [] for model in models), "到点后一次请求都没发"

    session_id = _only_session_id(settings)
    store = JsonlSessionStore(root=_sessions_root(settings))
    paused = next(e for e in store.read_events(session_id) if e.type == RUN_PAUSED)
    assert paused.data["limits"]["run"]["deadline_at"] == "2026-01-01T00:00:00Z"
    paused_run_id = paused.run_id

    printed_again: list[str] = []
    resumed = await resume_command(
        session_id, run_deadline_at="2999-01-01T00:00:00Z", expected_version=1,
        write=printed_again.append,
    )

    assert resumed.paused is False and resumed.final_text == "做完了"
    text_again = "".join(printed_again)
    assert "[run resumed]" in text_again
    events = store.read_events(session_id)
    assert [e.type for e in events].count(RUN_STARTED) == 1, "同 run 续跑不新建 run"
    assert {e.run_id for e in events if e.run_id} == {paused_run_id}
    resumed_event = next(e for e in events if e.type == RUN_RESUMED)
    assert resumed_event.data["limits"]["run"]["deadline_at"] == "2999-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_cli_resume_with_the_same_spent_deadline_is_rejected(monkeypatch, tmp_path):
    """沿用那个**已到点**的时刻 ⇒ 领域层拒绝（409 面），事件流一字不改。

    `03 §3.4` 的恢复规则是"给出**绝对** ceiling，且不得把 ceiling 降到已消耗之下"；
    deadline 的对应条款是"必须是一个未来时刻"（`resume_headroom_ok`）。沿用旧时刻
    会让恢复后立刻再次暂停——那不是恢复，是一次立刻重新到点。
    """
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: ScriptedModel(responses=[AIMessage(content="做完了")]),
    )
    await cli.run("做完这件事", run_deadline_at="2026-01-01T00:00:00Z", write=lambda _t: None)

    session_id = _only_session_id(settings)
    store = JsonlSessionStore(root=_sessions_root(settings))
    before = [e.to_dict() for e in store.read_events(session_id)]

    with pytest.raises(BudgetConflict, match="严格在未来"):
        await resume_command(
            session_id, run_deadline_at="2026-01-01T00:00:00Z", expected_version=1,
            write=lambda _t: None,
        )

    assert [e.to_dict() for e in store.read_events(session_id)] == before
