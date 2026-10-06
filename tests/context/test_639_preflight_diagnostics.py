"""#639 阶段3a：preflight 拒绝的可观测性（有界诊断 + attempt=0）。

preflight 拒绝 = `ContextCompactor.compact` 在**任何摘要尝试之前**发现摘要请求
本身超 hard（`compactor.py` 的 `request_token_estimate > self._hard_limit` 分支）。
此前该分支静默：既零模型调用、也零任务可见状态（`failures` 为空，见 #647 的
`test_preflight_rejection_keeps_zero_attempt_failures_empty`）。本票按 #647 已确立
的**有界失败通道纪律**补一条 `CompactionFailure(attempt=0, error_class=
"preflight_request_exceeds_hard_limit")`：

- 走 #348 既有通道（返回路径经 `result.failures`、抛错路径经
  `ContextWindowExceededError(failures=...)`），调用方（builder）落成任务可见状态；
- `attempt=0` 明示**不是摘要尝试**（尝试是 1/2），不为预检伪造第 3 次尝试；
- 判定式 `>`、两档阈值、if/else 分支结构与两条出口语义**一字不动**。

落点理由：与既有 compactor 特征测试同目录（`tests/context/`）。文件名以票号前缀，
便于与 #647 的 `test_compactor_source_rejection.py` 对照阅读；沿用仓库既有
`ScriptedModel` / `make_session` 夹具，不新造基础设施。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import (
    _SUMMARY_ERROR_CLASSES,
    CompactionPostWriteError,
    ContextCompactor,
    ContextWindowExceededError,
)
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import MODEL_COMPLETED, TOOL_RESULT, USER_MESSAGE
from agent_harness.session.event import (
    COMPACTION_END,
    COMPACTION_START,
    CONTEXT_COMPACTED,
    CONTEXT_COMPACTION_FAILED,
    SessionEvent,
)
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

#: 新增的有界拒绝类——写死以钉住词表稳定（#348/#647 同款纪律）。
PREFLIGHT_ERROR_CLASS = "preflight_request_exceeds_hard_limit"

#: max_context_tokens=8000 × 默认阈值 0.70 / 0.85。
AUTO_LIMIT = 5600
HARD_LIMIT = 6800

#: 模型撰写的四节（与 tests/context 其余用例同一合法摘要剧本）。
MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


class _NeverCalledModel:
    """记录调用次数的桩——preflight 拒绝路径必须零调用。"""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, request):
        self.calls += 1
        return AIMessage(content="(unused: preflight must not call the model)")


def _preflight_messages() -> list:
    """early 段的 JSON 转录超 hard，但调用方有效用量在 hard 以内。

    fixture 取**不可缩小**形态（巨型内容在首条消息）：16% 尾部预算只在包含首条
    消息时才达到 ⇒ 无更小的前缀段可缩（`narrow_early_window` 返回 None），
    阶段 A 的缩小重试被跳过，两条出口仍按 3a 语义生效。
    （文本必须不可 BPE 高度合并：连串重复字符会被合并、转录计不出超额——沿用
    #647 用例的观察。）
    """
    return [
        HumanMessage(content="历史分析 " * 2000),
        AIMessage(content="ok"),
        HumanMessage(content="current"),
    ]


def _events() -> list[SessionEvent]:
    return [
        SessionEvent(seq=1, type=USER_MESSAGE, session_id="s",
                     data={"content": "goal"}),
    ]


@pytest.mark.asyncio
async def test_preflight_rejection_is_observable_on_keep_projection_exit():
    """出口①（有效用量 ≤ hard，保留旧投影）：携带一条有界诊断（attempt=0）。"""
    model = _NeverCalledModel()
    messages = _preflight_messages()
    result = await ContextCompactor(model, max_context_tokens=8000).compact(
        messages, 5000, events=_events(), source_ranges=[(1, 1)],
    )

    # 既有出口语义逐字不变：旧投影保留、零压缩、零 bracket。
    assert result.compacted_turn_count == 0
    assert result.summary is None
    assert result.bracket_id is None
    assert result.messages == messages
    # 不是摘要尝试：零模型调用。
    assert model.calls == 0

    # 新增：一条任务可见诊断。
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.attempt == 0  # 预检 0 次尝试，不是 1/2
    assert failure.error_class == PREFLIGHT_ERROR_CLASS
    assert failure.error_class in _SUMMARY_ERROR_CLASSES  # 收进既有有界词表
    assert failure.hard_limit == HARD_LIMIT
    assert failure.auto_limit == AUTO_LIMIT
    assert failure.token_estimate == 5000  # 调用方有效用量原样
    # 判别式数字：请求令牌数 > hard，delta = request - hard > 0。
    assert failure.request_token_estimate > failure.hard_limit
    assert failure.request_token_estimate - failure.hard_limit > 0
    # request_token_estimate 就是判别式里那个值（同一变量）。
    assert failure.request_token_estimate > HARD_LIMIT
    # 无模型调用 ⇒ 无模型 id / 零耗时。
    assert failure.summary_model_id is None
    assert failure.duration_ms == 0


@pytest.mark.asyncio
async def test_preflight_rejection_is_observable_on_hard_guard_exit():
    """出口②（有效用量 > hard，抛错）：受控异常携带同一条有界诊断。"""
    model = _NeverCalledModel()
    messages = _preflight_messages()
    with pytest.raises(ContextWindowExceededError) as error:
        await ContextCompactor(model, max_context_tokens=8000).compact(
            messages, HARD_LIMIT + 1, events=_events(), source_ranges=[(1, 1)],
        )

    # 抛错行为不变：写前失败（非 post-write）、类型与逐字文案不变。
    assert not isinstance(error.value, CompactionPostWriteError)
    assert str(error.value) == (
        "Summary validation failed and original context exceeds hard guard"
    )
    assert [f.error_class for f in error.value.failures] == [PREFLIGHT_ERROR_CLASS]
    failure = error.value.failures[0]
    assert failure.attempt == 0
    assert failure.error_class in _SUMMARY_ERROR_CLASSES
    assert failure.request_token_estimate - failure.hard_limit > 0
    assert failure.token_estimate == HARD_LIMIT + 1
    assert model.calls == 0


@pytest.mark.asyncio
async def test_preflight_pass_is_unaffected_and_still_attempts_summary():
    """放行的仍放行：请求未超 hard ⇒ 正常进入摘要尝试（模型恰被调用一次）。"""
    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    messages = [
        HumanMessage(content="读取 old.txt 后继续。"),
        AIMessage(content="old analysis " * 600),
        HumanMessage(content="current"),
    ]
    result = await ContextCompactor(model, max_context_tokens=8000).compact(
        messages, estimate_message_tokens(messages),
        events=_events(), source_ranges=[(1, 1), (2, 2), (3, 3)],
    )

    assert len(model.snapshots) == 1  # 该放行的放行了
    assert result.compacted_turn_count == 1
    assert result.failures == []  # 放行/成功路径不携带预检诊断


@pytest.mark.asyncio
async def test_diagnostic_does_not_change_builder_branch_or_messages(tmp_path):
    """完整 `builder.build`：失败诊断不改变分支走向、不改变返回的 messages。

    对照构建用子类把 `_record_compaction_failures` 覆写成空操作（= 不落诊断事件）。
    对照**先**在未落诊断的同一份会话上跑（会话保持 pristine），随后正常构建在同一份
    pristine 会话上跑——两次输入事件完全相同，唯一差异是"诊断是否落事件"。两次返回
    的 messages 必须逐字节一致。这直接证明失败诊断条目不改变 `compact_now` 之后的
    任一 builder 分支（keep-old 出口返回 None，尾部装配由 `result is None` 决定，
    与失败记录内容无关）。

    fixture 为可缩小的 tool 小结果形态（阶段 A 会缩小 early 段重试），空剧本下缩小段
    放行后两次摘要尝试各失败一次 ⇒ 正常构建落三条非投影诊断（预检 attempt=0 在首）。
    """
    class _NoRecordBuilder(ContextBuilder):
        def _record_compaction_failures(self, session, failures):
            return None

    def _session(root):
        session = make_session(root)
        session.append(USER_MESSAGE, {"content": "goal"})
        for index in range(40):
            session.append(MODEL_COMPLETED, {
                "content": "",
                "tool_calls": [{"id": f"c{index}", "name": "read", "args": {"path": "x"}}],
            })
            session.append(TOOL_RESULT, {"tool_call_id": f"c{index}", "content": "ok"})
        session.append(USER_MESSAGE, {"content": "current"})
        return session

    session = _session(tmp_path)
    original_projection = session.derive_messages()
    before = list(session.events)

    # 对照（不落诊断）：同一 pristine 会话，诊断记录不落事件。
    control_messages = await _NoRecordBuilder(
        ScriptedModel([]), max_context_tokens=6000,
    ).build(session)
    assert session.events == before  # pristine：对照不留下任何事件

    # 正常构建：同一 pristine 会话，诊断记录启用 ⇒ 预检 attempt=0 + 两次尝试失败。
    messages = await ContextBuilder(
        ScriptedModel([]), max_context_tokens=6000,
    ).build(session)

    failures = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert [e.data["attempt"] for e in failures] == [0, 1, 2]
    assert failures[0].data["error_class"] == PREFLIGHT_ERROR_CLASS
    assert failures[0].data["narrowed"] is False
    assert failures[0].data["request_token_estimate"] > failures[0].data["hard_limit"]
    # 零 bracket：诊断不是压缩；既有事件前缀逐字节不动。
    assert not any(e.type in {COMPACTION_START, CONTEXT_COMPACTED, COMPACTION_END}
                   for e in session.events)
    assert session.events[:len(before)] == before
    # 非投影事件：derive 投影不变（不 shadow、不新增消息）。
    assert session.derive_messages() == original_projection

    # 逐字节一致：诊断条目不改变 builder 的返回 messages。
    assert [m.model_dump(mode="json") for m in messages] == [
        m.model_dump(mode="json") for m in control_messages
    ]
