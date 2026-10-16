"""#1197 项 1：per-session 上下文窗口（append-only 事件 + derive）。

票面要求「per-session 独立」；用户裁决 A（2026-10-16 14:06）定路由为
`GET`/`PATCH /api/sessions/{session_id}/context-settings`，并明令「复用仓库统一
per-session 形状：一手先例是 workflow-mode、permission-mode 的路由写法 + append-only
事件 + derive 模式，照着抄，不要发明新约定」。

于是存储形态照抄 `session/workflow.py`：新事件类型 + 唯一写入口 + 倒序 last-wins 派生。
本文件钉住派生/写入两侧的语义，路由层见 `tests/web/test_context_settings_api.py`。
"""

from __future__ import annotations

import pytest

from agent_harness.config import CONTEXT_WINDOW_TIERS
from agent_harness.context.window import (
    DEFAULT_CONTEXT_WINDOW,
    append_context_window_change,
    effective_context_window,
)
from agent_harness.session.event import CONTEXT_WINDOW_CHANGED, SessionEvent


def _event(data: dict, seq: int = 0) -> SessionEvent:
    return SessionEvent(
        session_id="s1", seq=seq, type=CONTEXT_WINDOW_CHANGED, data=data,
        agent_id="default",
    )


def test_default_is_the_settings_default() -> None:
    """无事件时回落 200K——与 `Settings.max_context_tokens` 的默认同值。"""
    from agent_harness.config import Settings

    assert DEFAULT_CONTEXT_WINDOW == Settings(_env_file=None).max_context_tokens == 200_000


def test_no_event_falls_back_to_default() -> None:
    assert effective_context_window([]) == DEFAULT_CONTEXT_WINDOW


def test_fallback_is_the_deployment_default_not_a_hardcoded_200k() -> None:
    """部署级默认（.env 的 MAX_CONTEXT_TOKENS）优先于字面量回落。

    写死 200K 会让 ``MAX_CONTEXT_TOKENS=500000`` 被静默忽略——面板显示 500K、
    实际按 200K 压缩，是「配置不生效」里最难查的一种。
    """
    assert effective_context_window([], default=500_000) == 500_000
    assert effective_context_window(
        [_event({"context_window": "bad"})], default=100_000,
    ) == 100_000


def test_last_write_wins() -> None:
    """倒序扫、最后一次胜——同 `effective_workflow_mode`。"""
    events = [
        _event({"context_window": 100_000}, seq=0),
        _event({"context_window": 500_000}, seq=1),
        _event({"context_window": 300_000}, seq=2),
    ]
    assert effective_context_window(events) == 300_000


def test_unrelated_events_are_skipped() -> None:
    other = SessionEvent(
        session_id="s1", seq=0, type="workflow/mode-changed", data={"mode": "plan"},
        agent_id="default",
    )
    assert effective_context_window([other]) == DEFAULT_CONTEXT_WINDOW


@pytest.mark.parametrize("bad", [None, "200000", 0, -1, 100, 1_000_000, 256_001, True])
def test_bad_value_warns_and_keeps_looking(bad: object) -> None:
    """坏值记 warning 后继续往下找（不猜一个"差不多"的挡位），全坏则回落默认。"""
    events = [_event({"context_window": 300_000}, seq=0), _event({"context_window": bad}, seq=1)]
    assert effective_context_window(events) == 300_000
    assert effective_context_window([_event({"context_window": bad})]) == DEFAULT_CONTEXT_WINDOW


def _session(tmp_path):
    from agent_harness.session.session import Session
    from agent_harness.session.store import JsonlSessionStore

    return Session.start(JsonlSessionStore(root=tmp_path / "sessions"))


@pytest.mark.parametrize("tier", CONTEXT_WINDOW_TIERS)
def test_append_accepts_every_tier(tmp_path, tier: int) -> None:
    session = _session(tmp_path)
    assert append_context_window_change(session, tier) == tier
    appended = [e for e in session.events if e.type == CONTEXT_WINDOW_CHANGED]
    assert [e.data for e in appended] == [{"context_window": tier}]
    assert effective_context_window(session.events) == tier


@pytest.mark.parametrize("bad", [0, -1, 100, 150_000, 1_000_000])
def test_append_rejects_non_tier(tmp_path, bad: int) -> None:
    """写入口把守挡位——挡位外的值不许进事件日志（否则 derive 侧只能靠坏值分支兜）。"""
    session = _session(tmp_path)
    with pytest.raises(ValueError):
        append_context_window_change(session, bad)
    assert [e for e in session.events if e.type == CONTEXT_WINDOW_CHANGED] == []


# ── 接线：会话挡位经 build() / compact_now() 生效 ──────────────────────────


async def _build_with_window(tmp_path, window: int | None, *, payload: int = 20):
    """在会话里跑一次 build，返回 (builder, 投影, 会话)。

    默认小载荷（不触发压缩，只看 builder 派生出的窗口）；大载荷（85_000 词
    ≈ 85001 token）用于让**挡位本身**成为判定差异的成因。
    """
    from langchain_core.messages import AIMessage

    from agent_harness.context.builder import ContextBuilder
    from agent_harness.session.event import USER_MESSAGE
    from tests.scripted_model import ScriptedModel

    session = _session(tmp_path)
    session.append(USER_MESSAGE, {"content": "large " * payload})
    if window is not None:
        append_context_window_change(session, window)
    builder = ContextBuilder(ScriptedModel([AIMessage(content="ok")]))
    messages = await builder.build(session)
    return builder, messages, session


@pytest.mark.asyncio
async def test_build_derives_the_session_window(tmp_path) -> None:
    """会话挡位改到 500K 后，build 按 500K 判定（同一 builder 实例，不重建）。"""
    builder, _messages, _session_ = await _build_with_window(tmp_path, 500_000)
    assert builder.max_context_tokens == 500_000


@pytest.mark.asyncio
async def test_build_falls_back_to_the_constructor_default(tmp_path) -> None:
    """会话没改过挡位 ⇒ 用构造期收到的部署级默认（不是 200K 字面量）。

    构造期传的是 200K，改传 100K 后本用例若仍读 200K 就说明回落写死了。
    """
    builder, _messages, _session_ = await _build_with_window(tmp_path, None)
    assert builder.max_context_tokens == 200_000


@pytest.mark.asyncio
async def test_session_window_changes_the_compaction_decision(tmp_path) -> None:
    """同一份载荷：500K 窗口放行，100K 窗口越硬护栏 —— 挡位真影响判定。

    这是票面「per-session 独立挡位」的可观测落点：窗口不是只改一个显示数字。

    载荷 ≈ 85001 token（``"large " * 85_000``）：500K 的硬护栏 425K 放行；100K 的
    硬护栏 85K 越线 ⇒ 走压缩，而单条用户消息**没有可压缩的早期轮** ⇒ compactor
    按 fail-closed 抛 ``ContextWindowExceededError``（compactor.py:697）。
    用「抛不抛」当观测量而非「压缩成不成功」，是为了不把摘要质量/分块逻辑拖进本用例。
    """
    from agent_harness.context.compactor import ContextWindowExceededError

    _b500, m500, _s500 = await _build_with_window(tmp_path / "a", 500_000, payload=85_000)
    assert m500  # 放行：拿到投影
    with pytest.raises(ContextWindowExceededError):
        await _build_with_window(tmp_path / "b", 100_000, payload=85_000)


@pytest.mark.asyncio
async def test_configured_default_is_not_clobbered_by_a_session_override(tmp_path) -> None:
    """先按会话挡位 build 过一轮后，**回落值**仍是部署级默认。

    若把派生结果写回 ``self._configured_max_context_tokens``（或直接用
    ``self.max_context_tokens`` 当回落），第二轮改回「无事件」时会继承上一轮的
    挡位——同一个 builder 实例跨 build 复用（run 内每步都 build）。
    """
    from langchain_core.messages import AIMessage

    from agent_harness.context.builder import ContextBuilder
    from agent_harness.session.event import USER_MESSAGE
    from tests.scripted_model import ScriptedModel

    session = _session(tmp_path)
    session.append(USER_MESSAGE, {"content": "hi"})
    append_context_window_change(session, 100_000)
    builder = ContextBuilder(ScriptedModel([AIMessage(content="ok")]), max_context_tokens=300_000)

    await builder.build(session)
    assert builder.max_context_tokens == 100_000
    # 事件被清空（等价于"会话没改过挡位"）：必须回落到构造期的 300K。
    session._events = [e for e in session._events if e.type != CONTEXT_WINDOW_CHANGED]
    await builder.build(session)
    assert builder.max_context_tokens == 300_000
