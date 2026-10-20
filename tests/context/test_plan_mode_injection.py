"""#1356 方案 1：PLAN 档生效时注入 plan-mode 指令块。

判据全部**纯事件推导**（`effective_workflow_mode`）⇒ 可重放、无 wall-clock、
无实例状态。落点与清单块/进度块同形：插在第一条压缩摘要之前，否则插在开头
连续 SystemMessage 之后。标题必须避开 `_is_compaction_summary` 的识别前缀。
"""

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from agent_harness.context.builder import (
    _PLAN_MODE_BLOCK_HEADING,
    ContextBuilder,
)
from agent_harness.context.compactor import _is_compaction_summary
from agent_harness.session import USER_MESSAGE, JsonlSessionStore, Session
from agent_harness.session.workflow import WorkflowMode, append_workflow_mode_change
from tests.scripted_model import ScriptedModel


def _session(tmp_path, *, mode: WorkflowMode | None = None) -> Session:
    session = Session.start(
        JsonlSessionStore(root=tmp_path / "sessions"), session_id="sid",
    )
    if mode is not None:
        append_workflow_mode_change(session, mode)
    session.append(USER_MESSAGE, {"content": "帮我把 A 改成 B"})
    return session


def _block(messages):
    return next(
        (m for m in messages
         if isinstance(m, SystemMessage)
         and m.content.startswith(_PLAN_MODE_BLOCK_HEADING)),
        None,
    )


def test_heading_does_not_look_like_a_compaction_summary():
    """标题不得命中 `_is_compaction_summary`（否则被摘要识别逻辑误判）。"""
    assert not _is_compaction_summary(SystemMessage(content=_PLAN_MODE_BLOCK_HEADING))
    assert not _is_compaction_summary(
        SystemMessage(content=_PLAN_MODE_BLOCK_HEADING + "\n随便一行正文")
    )


@pytest.mark.asyncio
async def test_plan_mode_injects_the_block(tmp_path):
    """AC1：PLAN 档 ⇒ 模型输入含指令块，四要点齐全。"""
    session = _session(tmp_path, mode=WorkflowMode.PLAN)
    messages = await ContextBuilder(ScriptedModel([])).build(session)

    block = _block(messages)
    assert block is not None, "PLAN 档必须注入指令块"
    text = block.content
    # ① 只能调研、不能改文件
    assert "只读" in text or "不能修改" in text or "不得修改" in text
    # ② 结构化 markdown 要素
    for section in ("目标", "分步改动", "触碰文件", "风险"):
        assert section in text
    # ③ 唯一出口是 submit_plan（不许用散文收口）
    assert "submit_plan" in text
    # ④ 批准后才拿到执行权；要求修改时修订后再交
    assert "批准" in text


@pytest.mark.asyncio
async def test_normal_mode_does_not_inject(tmp_path):
    """AC2：NORMAL 档（含从未切档）⇒ 不注入，零 token 成本。"""
    for mode in (None, WorkflowMode.NORMAL):
        session = _session(tmp_path / (mode.value if mode else "never"), mode=mode)
        messages = await ContextBuilder(ScriptedModel([])).build(session)
        assert _block(messages) is None


@pytest.mark.asyncio
async def test_normal_mode_projection_is_byte_identical_to_the_baseline(tmp_path):
    """AC2：NORMAL 档**其余部分逐字节一致**——新注入器没有挪动任何既有消息。

    只断言"块缺席"钉不住这一面：`_inject_plan_mode_block` 是下标敏感操作
    （`[*messages[:i], SystemMessage(...), *messages[i:]]`），而压缩摘要相关的
    既有契约正是按下标断言的。这里同事件流双跑，PLAN 产物**删掉那条块**后
    必须与 NORMAL 产物逐条逐字节相等。
    """
    # 同一会话双跑（先 NORMAL，再追加切档事件后重跑）：保护事实块里的
    # source_event_id 由事件流派生，两个不同会话会带不同 UUID，比不了字节。
    session = _session(tmp_path)
    normal = await ContextBuilder(ScriptedModel([])).build(session)
    append_workflow_mode_change(session, WorkflowMode.PLAN)
    plan = await ContextBuilder(ScriptedModel([])).build(session)

    stripped = [m for m in plan if _block([m]) is None]
    assert len(stripped) == len(plan) - 1, "PLAN 产物必须恰好多出一条指令块"
    assert [m.model_dump_json() for m in stripped] == [
        m.model_dump_json() for m in normal
    ]


@pytest.mark.asyncio
async def test_block_disappears_after_switching_back(tmp_path):
    """切回 NORMAL ⇒ 块消失（判据是现派生档位，不是历史痕迹）。"""
    session = _session(tmp_path, mode=WorkflowMode.PLAN)
    builder = ContextBuilder(ScriptedModel([]))
    assert _block(await builder.build(session)) is not None

    append_workflow_mode_change(session, WorkflowMode.NORMAL)
    assert _block(await builder.build(session)) is None


@pytest.mark.asyncio
async def test_block_is_replayable_across_restart(tmp_path):
    """可重放：重启后同一份持久事件流得到逐字节相同的块。"""
    session = _session(tmp_path, mode=WorkflowMode.PLAN)
    first = _block(await ContextBuilder(ScriptedModel([])).build(session))
    assert first is not None

    reloaded = Session.load(JsonlSessionStore(root=tmp_path / "sessions"), "sid")
    second = _block(await ContextBuilder(ScriptedModel([])).build(reloaded))
    assert second is not None
    assert second.content == first.content


@pytest.mark.asyncio
async def test_block_lands_before_the_user_message(tmp_path):
    """落点在系统块区域（第一条 HumanMessage 之前），不插进对话中间。"""
    session = _session(tmp_path, mode=WorkflowMode.PLAN)
    messages = await ContextBuilder(ScriptedModel([])).build(session)

    block_index = next(
        i for i, m in enumerate(messages)
        if isinstance(m, SystemMessage) and m.content.startswith(_PLAN_MODE_BLOCK_HEADING)
    )
    first_human = next(
        i for i, m in enumerate(messages) if isinstance(m, HumanMessage)
    )
    assert block_index < first_human


@pytest.mark.asyncio
async def test_block_tokens_are_counted_before_the_threshold(tmp_path):
    """token 纪律「先计入再注入」：块成本进 other 桶并闭合进 used_tokens。

    口径照 `test_modified_paths_injection.py::test_accounting_other_bucket_and_sum_invariant`
    ——只断言 `_last_*_tokens > 0` 是"字段被写了"，不断言"字段进了账"；#1356
    审查 P1-2 的真 bug 恰好落在这条分界线上。
    """
    session = _session(tmp_path, mode=WorkflowMode.PLAN)
    builder = ContextBuilder(ScriptedModel([]))
    await builder.build(session)
    assert builder._last_plan_mode_tokens > 0
    snapshot = builder.usage_snapshot(session)
    assert snapshot["other"] >= builder._last_plan_mode_tokens > 0
    assert snapshot["other_breakdown"]["plan_mode"] == builder._last_plan_mode_tokens
    assert snapshot["used_tokens"] == (
        snapshot["messages"] + snapshot["system_prompt"]
        + snapshot["skills"] + snapshot["other"]
    )
