"""ContextBuilder system_prompt 注入（ADR-0020a，agent_profile 运行时消费）。

system_prompt 是 runtime 装配期上下文（agent_profile 决定），不是持久历史
事件（不变量 #5）：不写 JSONL、不进入 derive_messages 投影，只在 build()
返回前 prepend 一条 SystemMessage。token 预算必须把它的成本计入（否则
system_prompt 越长越不会被压缩掉，挤占对话预算）。

这些测试覆盖四条契约：
  G1：注入——build() 返回的首条消息是 SystemMessage(content=system_prompt)；
  G2：默认无——不传 system_prompt 时行为不变（向后兼容）；
  G3：位置——provider 内容插在 system_prompt 之后（既有 _with_providers 约定）；
  G4：预算——system_prompt 的 token 成本计入 token_estimate。
"""

from __future__ import annotations

import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.session import COMPACTION_START, MODEL_COMPLETED, USER_MESSAGE
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

COMPACT_SUMMARY = """\
## 已完成工作与关键决策
已检查早期记录。

## 失败方案
(none)

## 当前进行中状态
早期记录处理完成。

## Next Step
继续当前请求。"""


@pytest.mark.asyncio
async def test_context_builder_injects_system_prompt(tmp_path):
    """G1：传了 system_prompt → build() 首条消息是 SystemMessage。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "你好"})
    builder = ContextBuilder(ScriptedModel([]), system_prompt="你是编码 agent。")

    messages = await builder.build(session)

    assert len(messages) == 4
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == "你是编码 agent。"
    assert isinstance(messages[1], SystemMessage)
    assert "Runtime permission and approval checks are authoritative" in messages[1].content
    assert isinstance(messages[2], HumanMessage)
    assert messages[2].content.startswith("## Protected task facts\n")
    # 最后一条仍是投影出的用户消息；system_prompt 与事实注入都不改写历史。
    assert messages[3].content == "你好"


@pytest.mark.asyncio
async def test_context_builder_no_system_prompt_by_default(tmp_path):
    """G2：不传 system_prompt；仅保留独立的保护事实注入。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "你好"})
    builder = ContextBuilder(ScriptedModel([]))

    messages = await builder.build(session)

    assert isinstance(messages[0], SystemMessage)
    assert isinstance(messages[1], HumanMessage)
    assert messages[1].content.startswith("## Protected task facts\n")
    assert not any(m.content == "你是编码 agent。" for m in messages)
    assert messages[2:] == session.derive_messages()


@pytest.mark.asyncio
async def test_context_builder_system_prompt_before_providers(tmp_path):
    """G3：system_prompt 在最前、provider 内容紧随其后、对话在最后。

    _with_providers 既有约定是把 provider 内容插在开头连续 SystemMessage 之后。
    build() 先跑 _with_providers（此时开头无 SystemMessage，provider 进首位），
    再 prepend system_prompt —— 最终顺序是 system_prompt → provider → 对话。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "你好"})

    class _StaticProvider:
        """测试用 provider：返回一条固定的 SystemMessage 内容（duck-type Protocol）。"""

        async def select(self, session, remaining_tokens):
            return [SystemMessage(content="[provider 注入]")]

    builder = ContextBuilder(
        ScriptedModel([]),
        context_providers=[_StaticProvider()],
        system_prompt="你是 coding agent。",
    )

    messages = await builder.build(session)

    # system_prompt 最前
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == "你是 coding agent。"
    # 静态事实规则与 provider 都留在 system 前缀，事实值保持 user role。
    assert isinstance(messages[1], SystemMessage)
    assert isinstance(messages[2], SystemMessage)
    assert messages[2].content == "[provider 注入]"
    assert isinstance(messages[3], HumanMessage)
    assert messages[3].content.startswith("## Protected task facts\n")
    # 对话在最后
    assert messages[4].content == "你好"


@pytest.mark.asyncio
async def test_context_builder_system_prompt_token_estimated(tmp_path, caplog):
    """G4：system_prompt 的 token 成本计入 token_estimate（不漏算预算）。

    验证路径：通过 debug 日志的 token_estimate 字段比对——有 system_prompt
    时的估算严格大于无 system_prompt 时的估算（差额 = system_prompt 的 token 数）。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "你好"})
    session.append(MODEL_COMPLETED, {"content": "你好！"})

    # 无 system_prompt 的基线估算
    builder_no_prompt = ContextBuilder(ScriptedModel([]))
    with caplog.at_level(logging.DEBUG, logger="agent_harness.context"):
        await builder_no_prompt.build(session)
    baseline = next(
        getattr(r, "token_estimate", 0) for r in caplog.records
        if getattr(r, "token_estimate", None)
    )

    # 有 system_prompt 的估算
    caplog.clear()
    builder_with_prompt = ContextBuilder(
        ScriptedModel([]), system_prompt="你是 coding agent，在共享 workspace 内完成 scoped task。",
    )
    with caplog.at_level(logging.DEBUG, logger="agent_harness.context"):
        await builder_with_prompt.build(session)
    with_prompt = next(
        getattr(r, "token_estimate", 0) for r in caplog.records
        if getattr(r, "token_estimate", None)
    )

    assert with_prompt > baseline, (
        f"system_prompt 必须计入 token 预算：with_prompt={with_prompt} "
        f"应 > baseline={baseline}"
    )


@pytest.mark.asyncio
async def test_context_builder_system_prompt_token_cached_across_builds(tmp_path):
    """system_prompt 的 token 估算只算一次（文本终身不变，缓存复用）。

    两次 build 后内部 _system_prompt_tokens 应已缓存且相同——不依赖 session
    状态，是 builder 实例级常量。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "你好"})
    builder = ContextBuilder(ScriptedModel([]), system_prompt="固定的角色提示。")

    await builder.build(session)
    first = builder._system_prompt_tokens
    assert first is not None and first > 0

    session.append(MODEL_COMPLETED, {"content": "好的。"})
    await builder.build(session)
    second = builder._system_prompt_tokens

    assert first == second, "system_prompt token 估算必须缓存复用（文本不变）"


@pytest.mark.asyncio
async def test_context_builder_compaction_path_reserves_system_prompt_tokens(tmp_path):
    """压缩路径也必须把 system_prompt 的 token 成本计入 provider 预算。

    回归测试：compaction 后 result.token_estimate 只含 messages——
    若不补回 system_prompt 的 token，_with_providers 会把 system_prompt
    占用的预算当作可用空间分配给 provider 内容。
    """
    session = make_session(tmp_path)
    # 填入足够多的事件以触发 compaction（需要至少一个完整 early turn）
    for i in range(5):
        session.append(USER_MESSAGE, {"content": f"处理第 {i} 条记录。"})
        session.append(MODEL_COMPLETED, {"content": f"历史模型输出 {i} " * 100})
    compact_summary = AIMessage(content=COMPACT_SUMMARY)
    builder = ContextBuilder(
        ScriptedModel([compact_summary]),
        max_context_tokens=8000,
        auto_compact_threshold=0.30,
        hard_guard_threshold=0.90,
        system_prompt="你是 coding agent。",
    )

    await builder.build(session)

    assert any(event.type == COMPACTION_START for event in session.events)
    assert builder._system_prompt_tokens is not None
    assert builder._token_estimate_total >= builder._system_prompt_tokens, (
        "压缩路径的 token_estimate 必须包含 system_prompt 的 token 成本"
    )
