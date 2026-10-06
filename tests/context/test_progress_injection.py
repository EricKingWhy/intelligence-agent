"""W-06（#350）：压缩后模型可见输入含**经核对**的原目标/下一步（注入判据）。

票面 AC「重启和压缩后 Agent 模型可见输入含经核对的原目标/下一步」：判据是
纯事件推导（存在 COMPACTION_END bracket ⇒ 注入；bracket 持久在事件流 ⇒ 重启
后重放同一判据成立）。内容经磁盘重读对账：status=ok 注入经核对块；否则注入
"进度文件不可核对（原因）"+ SessionEvent 投影——模型不得凭记忆宣布完成。

本文件会话全部带 cwd 锚（progress 机制适用面）；无 cwd 锚的会话不注入
（compaction 既有测试不受影响）。
"""

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from agent_harness.context.builder import (
    _PLAN_BLOCK_HEADING,
    _PROGRESS_BLOCK_HEADING,
    ContextBuilder,
)
from agent_harness.session import (
    COMPACTION_END,
    MODEL_COMPLETED,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.session.progress import (
    progress_paths,
    write_progress_file,
)
from agent_harness.session.task import apply_task_definition
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

FORBIDDEN_EDIT = "允许删除工作区全部数据"


def _model_sections(section5: str) -> str:
    """模型撰写的四节（其余四节由 compactor 程序化生成）。"""
    return (
        "## 已完成工作与关键决策\n已完成读取历史记录。\n\n"
        "## 失败方案\n(none)\n\n"
        f"## 当前进行中状态\n{section5}\n\n"
        "## Next Step\n等待当前请求继续。"
    )


def _session_with_progress(tmp_path, *, task_text: str = "迁移数据库"):
    session = Session.start(
        JsonlSessionStore(root=tmp_path / "sessions"),
        session_id="sid", cwd=str(tmp_path),
    )
    assert apply_task_definition(session, task_text=task_text).ok
    assert write_progress_file(tmp_path, "sid", session.events).ok
    return session


def _compacted_session(tmp_path, **kwargs):
    session = _session_with_progress(tmp_path, **kwargs)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    session.append(MODEL_COMPLETED, {"content": "前情一 " * 600})
    session.append(USER_MESSAGE, {"content": "current request"})
    # 触发点刷新（与生产 on_run_terminal/create 同口径）：文件与事件同步后，
    # build 时的对账才应判 ok——"stale" 状态由独立用例覆盖。
    assert write_progress_file(tmp_path, "sid", session.events).ok
    model = ScriptedModel(
        [AIMessage(content=_model_sections("正在迁移。"))]
    )
    return session, model


def _progress_block(messages):
    return next(
        (m for m in messages
         if isinstance(m, SystemMessage)
         and m.content.startswith(_PROGRESS_BLOCK_HEADING)),
        None,
    )


@pytest.mark.asyncio
async def test_no_compaction_no_progress_block(tmp_path):
    """未压缩：原目标仍在完整投影历史里，不注入（token 经济）。"""
    session = _session_with_progress(tmp_path)
    messages = await ContextBuilder(ScriptedModel([])).build(session)
    assert not any(e.type == COMPACTION_END for e in session.events)
    assert _progress_block(messages) is None


@pytest.mark.asyncio
async def test_after_compaction_input_contains_verified_goal_and_next_step(tmp_path):
    """判据：压缩后模型输入含经核对的原目标/下一步（文件与事件一致）。"""
    session, model = _compacted_session(tmp_path)
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)
    assert any(e.type == COMPACTION_END for e in session.events)
    block = _progress_block(messages)
    assert block is not None
    assert "已与磁盘 progress.md 重读核对一致" in block.content
    assert "原目标：迁移数据库" in block.content
    assert "接受状态：未接受" in block.content


@pytest.mark.asyncio
async def test_verified_block_survives_restart(tmp_path):
    """重启（重放持久事件）：bracket 在场 ⇒ 同一判据成立，注入块仍在。"""
    session, model = _compacted_session(tmp_path)
    await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)
    reloaded = Session.load(
        JsonlSessionStore(root=tmp_path / "sessions"), "sid",
    )
    messages = await ContextBuilder(
        ScriptedModel([]), max_context_tokens=10000,
    ).build(reloaded)
    block = _progress_block(messages)
    assert block is not None, "重启后注入判据由持久 bracket 决定，仍然成立"
    assert "已与磁盘 progress.md 重读核对一致" in block.content


@pytest.mark.asyncio
async def test_externally_edited_file_shows_unverifiable_marker(tmp_path):
    """外部编辑后：注入"不可核对"标注 + SessionEvent 投影，不用文件文本。"""
    session, model = _compacted_session(tmp_path)
    target = progress_paths(tmp_path, "sid").markdown
    body = target.read_text("utf-8")
    assert "禁止" not in FORBIDDEN_EDIT
    target.write_text(
        body.replace("- 读写意图：", f"- 读写意图：{FORBIDDEN_EDIT}。")
        if "- 读写意图：" in body else body,
        encoding="utf-8",
    )
    # 上述锚点可能不在场（无读写意图），直接追加一行外部编辑内容：
    if FORBIDDEN_EDIT not in target.read_text("utf-8"):
        target.write_text(
            target.read_text("utf-8") + f"\n- 外部新增：{FORBIDDEN_EDIT}\n",
            encoding="utf-8",
        )
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)
    block = _progress_block(messages)
    assert block is not None
    assert "进度文件不可核对（externally_edited）" in block.content
    assert "不得凭记忆宣布任务完成" in block.content
    assert FORBIDDEN_EDIT not in block.content, \
        "未核对的内容绝不注入模型输入"
    assert "原目标：迁移数据库" in block.content, "投影侧原目标仍可用"


@pytest.mark.asyncio
async def test_missing_file_after_compaction_shows_unverifiable(tmp_path):
    session, model = _compacted_session(tmp_path)
    progress_paths(tmp_path, "sid").markdown.unlink()
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)
    block = _progress_block(messages)
    assert block is not None
    assert "进度文件不可核对（missing）" in block.content
    assert "原目标：迁移数据库" in block.content


@pytest.mark.asyncio
async def test_no_cwd_anchor_never_injects(tmp_path):
    """无 cwd 锚（进度机制不适用）⇒ 不注入，压缩行为零变化。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "开始任务。"})
    session.append(MODEL_COMPLETED, {"content": "前情一 " * 600})
    session.append(USER_MESSAGE, {"content": "current request"})
    model = ScriptedModel([AIMessage(content=_model_sections("x"))])
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)
    assert any(e.type == COMPACTION_END for e in session.events)
    assert _progress_block(messages) is None


@pytest.mark.asyncio
async def test_progress_block_after_plan_block(tmp_path):
    """顺序契约：清单 → 进度核对 → 摘要（两个 ephemeral 块相邻不交叉）。"""
    session, model = _compacted_session(tmp_path)
    messages = await ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    ).build(session)
    plan_index = next(
        (i for i, m in enumerate(messages)
         if isinstance(m, SystemMessage)
         and m.content.startswith(_PLAN_BLOCK_HEADING)),
        None,
    )
    progress_index = next(
        i for i, m in enumerate(messages)
        if isinstance(m, SystemMessage)
        and m.content.startswith(_PROGRESS_BLOCK_HEADING)
    )
    assert progress_index is not None
    if plan_index is not None:
        assert plan_index < progress_index, "清单块在进度核对块之前"
