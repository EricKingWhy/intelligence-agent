"""#639 阶段 B：thrashing guard——连续 N 次预检拒绝后显式失败（TDD 回归）。

## 设计（先写设计，再写红测）

**计数器落点**：`ContextBuilder` 实例属性 `_preflight_rejection_streaks`（按
`session.session_id` 分会话记）。一个 builder 实例跨 build 轮次存活、可能服务多个
会话（`runtime.py:1309` / `service.py:3019` / `assembly.py:750` 构造），跨会话串计数
是错的。成功压缩后该 key `pop`（等价 0，避免无界增长）。

**什么算"一轮预检拒绝"**：某次 `compact_now` 的 `compact()` **以 preflight 拒绝
收尾**——该轮 `failures` **全部**是 3a 诊断（`attempt=0` +
`error_class="preflight_request_exceeds_hard_limit"`）。判据取"全部"而非"含有"：
A 的缩小放行后走两次摘要尝试失败（双失败安全继续）的轮里，也会**含有**一条全段
预检诊断（`attempt=0`），但该轮 `failures` 另含 `attempt>=1` 的摘要失败条目，说明
本轮并非停在预检——按设计（`REPORT §B`：双失败安全继续不增不减）不计。一轮只计
一次：A 的"全段拒绝 + 缩小也拒绝"轮有两条预检诊断，仍只 +1。

**阈值**：`streak >= 3` 触发（第 3 次连续拒绝轮即触发）。

**触发后**：经 #348 既有通道抛 `ContextWindowExceededError(failures=本轮诊断)`，
由 `compact_now` 既有记录（`_record_compaction_failures`）落盘后抛出；run 走非终态
暂停（`runtime._terminal_context_exceeded`）。message 附中文恢复指引三条。触发后
计数器**不清零**：恢复后若下一轮仍拒绝，继续显式失败。

**N 未到之前**：与现状逐字节一致（计数器只增内部状态，不产生事件/不改返回）。

## 落点理由

与既有 compactor / builder 特征测试同目录（`tests/context/`），文件名以票号前缀，
与 `test_639_preflight_diagnostics.py`（3a）、`test_639_narrowed_retry.py`（A）并列；
沿用仓库既有 `ScriptedModel` / `make_session` 夹具，不新造基础设施。
"""

import pytest
from langchain_core.messages import AIMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import ContextWindowExceededError
from agent_harness.session import (
    MODEL_COMPLETED,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.session.event import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    CONTEXT_COMPACTION_FAILED,
)
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

#: 新增的有界拒绝类——与 compactor 词表同名词条（写死以钉住稳定性）。
PREFLIGHT_ERROR_CLASS = "preflight_request_exceeds_hard_limit"
#: 阈值（与实现常量对账，写死以钉住稳定性）。
THRASHING_THRESHOLD = 3
#: 使 both-reject fixture 走出口①（全段与缩小段请求都 > hard，有效用量 ≤ hard）。
REJECT_WINDOW = 5600
#: 该窗口下可成功压缩的 fixture 窗口（巨型单条早期消息，请求不超 hard）。
SUCCESS_WINDOW = 5600
#: 双失败安全继续的窗口（全段预检拒绝→缩小放行→两次摘要尝试失败）。
DOUBLE_FAILURE_WINDOW = 6000

#: 模型撰写的四节（与 tests/context 其余用例同一合法摘要剧本）。
MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


def _rounds(count: int, result: str = "ok") -> list:
    """count 轮 tool 小结果（每轮 MODEL_COMPLETED(tool_calls) + TOOL_RESULT）。"""
    events = []
    for index in range(count):
        events.append((MODEL_COMPLETED, {
            "content": "",
            "tool_calls": [{"id": f"c{index}", "name": "read", "args": {"path": "x"}}],
        }))
        events.append((TOOL_RESULT, {"tool_call_id": f"c{index}", "content": result}))
    return events


def _both_reject_session(root, *, k: int = 40, big_words: int = 100) -> Session:
    """一轮内"全段预检拒绝 + 缩小段也拒绝"（出口①，2 条诊断/轮）。

    构造：goal + 40 轮 tool 小结果（高 JSON 膨胀）+ 尾部单条巨型 AI（16% 保留尾段）
    + current。缩小段 = 前面 40 轮小结果，其摘要请求仍超 hard；保留尾段逐字留在
    投影 ⇒ `compact()` 以 preflight 拒绝收尾、零摘要调用。
    """
    session = make_session(root)
    session.append(USER_MESSAGE, {"content": "goal"})
    for event_type, data in _rounds(k):
        session.append(event_type, data)
    session.append(MODEL_COMPLETED, {
        "content": " ".join(f"w{i}" for i in range(big_words)),
    })
    session.append(USER_MESSAGE, {"content": "current"})
    return session


def _success_session(root, *, filler_words: int = 1600) -> Session:
    """同窗口下可成功压缩的会话：goal + 巨型 AI + current（请求不超 hard）。"""
    session = make_session(root)
    session.append(USER_MESSAGE, {"content": "goal"})
    session.append(MODEL_COMPLETED, {
        "content": " ".join(f"w{i}" for i in range(filler_words)),
    })
    session.append(USER_MESSAGE, {"content": "current"})
    return session


def _double_failure_session(root, *, k: int = 40) -> Session:
    """双失败安全继续：全段预检拒绝→缩小放行→两次摘要尝试都失败（出口①）。"""
    session = make_session(root)
    session.append(USER_MESSAGE, {"content": "goal"})
    for event_type, data in _rounds(k):
        session.append(event_type, data)
    session.append(USER_MESSAGE, {"content": "current"})
    return session


class _PreBBuilder(ContextBuilder):
    """把 thrashing guard 关掉（登记/阈值永不触发）＝加 B 之前的行为。"""

    def _note_preflight_rejection(self, session, failures):
        return False


def _message_dump(messages: list) -> list:
    return [message.model_dump(mode="json") for message in messages]


def _event_dump(events: list) -> list:
    # 只比对 (type, data)：两个独立会话的 session_id/事件 id 不同，非被测口径。
    return [(event.type, event.data) for event in events]


def _brackets(session: Session) -> list:
    return [event.type for event in session.events
            if event.type in {COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END}]


def _failures(session: Session) -> list:
    return [event for event in session.events
            if event.type == CONTEXT_COMPACTION_FAILED]


@pytest.mark.asyncio
async def test_three_consecutive_preflight_rejections_raise_with_recovery_guidance(tmp_path):
    """连续 3 次预检拒绝轮：前 2 次保留旧投影，第 3 次显式失败 + 中文恢复指引。"""
    session = _both_reject_session(tmp_path)
    builder = ContextBuilder(ScriptedModel([]), max_context_tokens=REJECT_WINDOW)

    first = await builder.build(session)
    assert _brackets(session) == []  # N 未到：零 bracket
    second = await builder.build(session)
    assert first == second  # 逐字节一致（keep-projection）
    assert _brackets(session) == []
    # 每轮都是"全段拒绝 + 缩小也拒绝"（2 条 attempt=0 诊断）——钉住 fixture 形状。
    assert [(e.data["attempt"], e.data["error_class"], e.data["narrowed"])
            for e in _failures(session)] == [
        (0, PREFLIGHT_ERROR_CLASS, False), (0, PREFLIGHT_ERROR_CLASS, True),
    ] * 2

    with pytest.raises(ContextWindowExceededError) as error:
        await builder.build(session)

    failures = error.value.failures
    assert [f.error_class for f in failures] == [
        PREFLIGHT_ERROR_CLASS, PREFLIGHT_ERROR_CLASS,
    ]
    assert [f.attempt for f in failures] == [0, 0]
    assert [f.narrowed for f in failures] == [False, True]
    # 3a 诊断字段：request_tokens / hard_limit / delta。
    assert failures[0].request_token_estimate > failures[0].hard_limit
    assert failures[1].request_token_estimate > failures[1].hard_limit
    # 中文恢复指引三条齐全。
    message = str(error.value)
    assert "恢复指引" in message
    assert "/compact" in message
    assert "max_context_tokens" in message and "hard_guard_threshold" in message
    assert "subagent" in message
    # 触发后计数器不清零（streak 保持 ≥ 阈值）。
    assert builder._preflight_rejection_streaks[session.session_id] >= THRASHING_THRESHOLD


@pytest.mark.asyncio
async def test_successful_compaction_resets_streak(tmp_path):
    """成功压缩清零：拒绝、拒绝、成功压缩、拒绝 → 不抛（计数器已清零）。"""
    (tmp_path / "reject").mkdir()
    (tmp_path / "success").mkdir()
    reject = _both_reject_session(tmp_path / "reject")
    # 同一 session_id 的成功会话：验证成功压缩清的是该会话的连续计数。
    success = Session(
        reject.session_id, JsonlSessionStore(root=tmp_path / "success"),
    )
    success.append(USER_MESSAGE, {"content": "goal"})
    success.append(MODEL_COMPLETED, {
        "content": " ".join(f"w{i}" for i in range(1600)),
    })
    success.append(USER_MESSAGE, {"content": "current"})

    builder = ContextBuilder(
        ScriptedModel([AIMessage(content=MODEL_SECTIONS)]),
        max_context_tokens=SUCCESS_WINDOW,
    )
    sid = reject.session_id
    await builder.build(reject)
    await builder.build(reject)
    assert builder._preflight_rejection_streaks[sid] == 2

    await builder.build(success)  # 成功压缩 → 清零
    assert sid not in builder._preflight_rejection_streaks

    # 再来一次拒绝：清零后只到 1，不抛。
    await builder.build(reject)
    assert builder._preflight_rejection_streaks[sid] == 1


@pytest.mark.asyncio
async def test_one_round_counts_once_not_per_diagnostic(tmp_path):
    """一轮只计一次：每轮 2 条诊断 ("全段拒绝 + 缩小也拒绝") 只 +1。"""
    session = _both_reject_session(tmp_path)
    builder = ContextBuilder(ScriptedModel([]), max_context_tokens=REJECT_WINDOW)

    await builder.build(session)
    assert builder._preflight_rejection_streaks[session.session_id] == 1
    # 若按诊断条目数计（2/轮），第 2 轮就会误触发；实际不抛。
    await builder.build(session)
    assert builder._preflight_rejection_streaks[session.session_id] == 2
    # 第 3 轮才触发。
    with pytest.raises(ContextWindowExceededError):
        await builder.build(session)


@pytest.mark.asyncio
async def test_below_threshold_matches_pre_b_builder(tmp_path):
    """N 未到（streak=1、2）：返回 messages 与事件序列与加 B 前逐字节一致。"""
    (tmp_path / "control").mkdir()
    real_session = _both_reject_session(tmp_path)
    # 复制同一份事件 + 同一 session_id 的对照会话：消息里的保护事实带 session_id，
    # 换 id 会引入与被测无关的差异；两 builder 分属不同实例 ⇒ 计数互不干扰。
    control_session = Session(
        real_session.session_id,
        JsonlSessionStore(root=tmp_path / "control"),
        list(real_session.events),
    )
    real = ContextBuilder(ScriptedModel([]), max_context_tokens=REJECT_WINDOW)
    control = _PreBBuilder(ScriptedModel([]), max_context_tokens=REJECT_WINDOW)

    for _ in range(2):
        real_messages = await real.build(real_session)
        control_messages = await control.build(control_session)
        assert _message_dump(real_messages) == _message_dump(control_messages)

    assert _event_dump(real_session.events) == _event_dump(control_session.events)
    # 真实 builder 已计数到 2；守卫关闭的对照从不计数。
    assert real._preflight_rejection_streaks[real_session.session_id] == 2
    assert control._preflight_rejection_streaks == {}


@pytest.mark.asyncio
async def test_streaks_are_isolated_per_session(tmp_path):
    """分会话隔离：会话 A 拒绝 2 次、B 拒绝 1 次 → 都不抛（不串计数）。"""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    session_a = _both_reject_session(tmp_path / "a")
    session_b = _both_reject_session(tmp_path / "b")
    builder = ContextBuilder(ScriptedModel([]), max_context_tokens=REJECT_WINDOW)

    await builder.build(session_a)
    await builder.build(session_b)
    await builder.build(session_a)
    assert builder._preflight_rejection_streaks[session_a.session_id] == 2
    assert builder._preflight_rejection_streaks[session_b.session_id] == 1
    # 再推 B 一次到 2，仍不抛（若串计数则会到 4）。
    await builder.build(session_b)
    assert builder._preflight_rejection_streaks[session_b.session_id] == 2


@pytest.mark.asyncio
async def test_double_failure_does_not_increase_streak(tmp_path):
    """非预检拒绝不计：双失败安全继续（摘要尝试失败）不增加 streak。"""
    session = _double_failure_session(tmp_path)
    builder = ContextBuilder(ScriptedModel([]), max_context_tokens=DOUBLE_FAILURE_WINDOW)

    for _ in range(3):
        result = await builder.build(session)
        assert result is not None  # keep-projection（未压缩）

    # 每轮：全段预检(attempt=0) + 两次摘要尝试失败(attempt=1/2)——含预检条目但非"停在预检"。
    attempts = [e.data["attempt"] for e in _failures(session)]
    assert attempts[:3] == [0, 1, 2]
    assert builder._preflight_rejection_streaks.get(session.session_id, 0) == 0
    assert _brackets(session) == []
