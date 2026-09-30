"""W-31.5（#417）：最近修改文件路径清单的注入/记账/确定性用例。

票面 AC：清单行只含路径字符串 + 固定标题（T6）；注入顺序 = 紧随计划锚块
SystemMessage 之后（PRD §4.6 顺序 2→4）；与摘要第 8 节口径互斥（T7）；
两次 build 前缀逐字节一致（T8）；记账 other 桶 + 六桶求和不变式（T9）；
跨压缩不丢（T5）。

载具语义（票面明示选择）：清单块以计划锚块为载具——计划静默窗内清单与
计划一同缺席（至多 plan_reinject_every_messages 条，缺省 6），压缩后计划
恒在场 ⇒ 清单恒在场；无计划会话不注入（派生本身照常可用）。
"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_harness.context.builder import (
    _MODIFIED_PATHS_BLOCK_HEADING,
    _PLAN_BLOCK_HEADING,
    ContextBuilder,
    _render_modified_paths_block,
)
from agent_harness.context.compactor import _programmatic_summary_sections
from agent_harness.session import (
    COMPACTION_END,
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
)
from agent_harness.session.derive import derive_modified_file_paths
from agent_harness.session.plan import apply_plan_update
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

PLAN_ITEMS = [
    {"id": "1", "content": "写文件", "activeForm": "写文件中",
     "status": "in_progress", "source": "agent"},
]


def _apply_plan(session) -> None:
    outcome = apply_plan_update(session, PLAN_ITEMS)
    assert outcome.ok, outcome.reason


def _write(session, call_id: str, path: str, tool: str = "write") -> None:
    session.append(TOOL_CALL, {
        "tool_call_id": call_id, "tool_name": tool, "args": {"path": path},
    })


def _result(session, call_id: str, path: str) -> None:
    """补 TOOL_RESULT（回显 path）：dangling TOOL_CALL 不投影，§8 抽取走的是
    投影消息里 path 形态的字符串——缺结果事件的写调用对 §8 不可见。"""
    session.append(TOOL_RESULT, {
        "tool_call_id": call_id,
        "content": json.dumps({"ok": True, "path": path}, ensure_ascii=False),
    })


def _model_sections(section5: str) -> str:
    """模型撰写的四节（其余四节由 compactor 程序化生成），第 5 节可注入。"""
    return (
        "## 已完成工作与关键决策\n已完成读取历史记录。\n\n"
        "## 失败方案\n(none)\n\n"
        f"## 当前进行中状态\n{section5}\n\n"
        "## Next Step\n等待当前请求继续。"
    )


def _plan_index(messages) -> int:
    return next(
        i for i, m in enumerate(messages)
        if isinstance(m, SystemMessage) and m.content.startswith(_PLAN_BLOCK_HEADING)
    )


def _paths_block(messages):
    return next(
        (m for m in messages
         if isinstance(m, SystemMessage)
         and m.content.startswith(_MODIFIED_PATHS_BLOCK_HEADING)),
        None,
    )


def test_render_block_is_heading_plus_bare_paths():
    """T6（渲染面）：首行固定标题，其后每行一个裸路径——无 bullet、无计数。"""
    assert _render_modified_paths_block(["a.txt", "b.txt"]) == (
        _MODIFIED_PATHS_BLOCK_HEADING + "\na.txt\nb.txt"
    )


@pytest.mark.asyncio
async def test_block_follows_plan_anchor_in_build(tmp_path):
    """T6（注入面）：清单块 SystemMessage 紧随计划锚块 SystemMessage 之后。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _apply_plan(session)
    _write(session, "w1", "src/a.txt")
    _write(session, "w2", "src/b.txt")
    messages = await ContextBuilder(ScriptedModel([])).build(session)
    plan_idx = _plan_index(messages)
    block = messages[plan_idx + 1]
    assert isinstance(block, SystemMessage)
    assert block.content.startswith(_MODIFIED_PATHS_BLOCK_HEADING)
    assert block.content.splitlines()[1:] == ["src/a.txt", "src/b.txt"]


@pytest.mark.asyncio
async def test_no_plan_no_block_but_derive_still_works(tmp_path):
    """T6（载具语义）：无计划会话不注入清单块；派生本身照常可用。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _write(session, "w1", "a.txt")
    messages = await ContextBuilder(ScriptedModel([])).build(session)
    assert _paths_block(messages) is None
    assert derive_modified_file_paths(session.events) == ["a.txt"]


@pytest.mark.asyncio
async def test_section8_includes_reads_derive_does_not(tmp_path):
    """T7（票面点名）：同一事件流——§8「文件清单」含读文件路径，本清单不含。

    禁止「看起来像就当成同一口径」的机械保障：两函数共享 WRITE_TOOL_NAMES
    一个常量，其余各走各的判据，本用例与 T2 两面各钉一条。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _write(session, "r1", "readme.txt", tool="read_file")
    _result(session, "r1", "readme.txt")
    _write(session, "w1", "b.txt")
    _result(session, "w1", "b.txt")
    sections = _programmatic_summary_sections(session.derive_messages())
    files_section = sections["## 文件清单"]
    assert "readme.txt" in files_section
    assert "b.txt" in files_section
    assert derive_modified_file_paths(session.events) == ["b.txt"]


@pytest.mark.asyncio
async def test_two_builds_byte_identical(tmp_path):
    """T8：同一事件流连续两次 build，消息列表逐字节相等（model_dump_json）。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _apply_plan(session)
    _write(session, "w1", "a.txt")
    builder = ContextBuilder(ScriptedModel([]))
    first = await builder.build(session)
    second = await builder.build(session)
    assert [m.model_dump_json() for m in first] == [m.model_dump_json() for m in second]


@pytest.mark.asyncio
async def test_accounting_other_bucket_and_sum_invariant(tmp_path):
    """T9：清单 token 记独立口并折进 other 桶；六桶求和不变式不破。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始。"})
    _apply_plan(session)
    _write(session, "w1", "a.txt")
    builder = ContextBuilder(ScriptedModel([]))
    await builder.build(session)
    assert builder._last_modified_paths_tokens > 0
    snapshot = builder.usage_snapshot(session)
    assert snapshot["other"] >= builder._last_modified_paths_tokens > 0
    assert snapshot["used_tokens"] == (
        snapshot["messages"] + snapshot["system_prompt"]
        + snapshot["skills"] + snapshot["other"]
    )


@pytest.mark.asyncio
async def test_zero_writes_account_zero_and_no_block(tmp_path):
    """T9（零面）：无写事件 = 记 0、无清单块，求和不变式照常闭合。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "纯读会话。"})
    builder = ContextBuilder(ScriptedModel([]))
    await builder.build(session)
    assert builder._last_modified_paths_tokens == 0
    snapshot = builder.usage_snapshot(session)
    assert snapshot["used_tokens"] == (
        snapshot["messages"] + snapshot["system_prompt"]
        + snapshot["skills"] + snapshot["other"]
    )


@pytest.mark.asyncio
async def test_compaction_does_not_lose_paths(tmp_path):
    """T5：小预算触发一次真压缩——压缩前后派生清单相等（事件流只 shadow
    不删除，纯函数根基），且压缩后 build 的模型可见消息里清单块仍在场
    （计划恒注入载具生效）。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _write(session, "w1", "src/main.py")
    session.append(MODEL_COMPLETED, {"content": "前情一 " * 600})
    session.append(USER_MESSAGE, {"content": "换一部分。"})
    _apply_plan(session)
    session.append(MODEL_COMPLETED, {"content": "前情二 " * 600})
    session.append(USER_MESSAGE, {"content": "current request"})
    before = derive_modified_file_paths(session.events)
    assert before == ["src/main.py"]
    model = ScriptedModel([AIMessage(content=_model_sections("写文件：正在压缩。"))])
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)
    assert any(event.type == COMPACTION_END for event in session.events)
    assert derive_modified_file_paths(session.events) == before
    plan_idx = _plan_index(messages)
    block = messages[plan_idx + 1]
    assert isinstance(block, SystemMessage)
    assert block.content.startswith(_MODIFIED_PATHS_BLOCK_HEADING)
    assert "src/main.py" in block.content


@pytest.mark.asyncio
async def test_summary_is_human_message_after_compaction(tmp_path):
    """守卫锚（#411 改了摘要角色）：压缩摘要 = HumanMessage（name=compaction
    summary）；本清单块仍是 SystemMessage 且在计划锚块之后——两者不同形，
    防止把清单块误当摘要或反之。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    _write(session, "w1", "src/main.py")
    session.append(MODEL_COMPLETED, {"content": "前情一 " * 600})
    session.append(USER_MESSAGE, {"content": "换一部分。"})
    _apply_plan(session)
    session.append(MODEL_COMPLETED, {"content": "前情二 " * 600})
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([AIMessage(content=_model_sections("写文件：正在压缩。"))])
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)
    summary = next(
        m for m in messages
        if isinstance(m, HumanMessage) and m.name == "context_compaction_summary"
    )
    block = _paths_block(messages)
    assert isinstance(summary, HumanMessage)
    assert isinstance(block, SystemMessage)
    assert messages.index(block) < messages.index(summary)
