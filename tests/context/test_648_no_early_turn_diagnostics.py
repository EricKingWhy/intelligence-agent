"""#648 选项 B（用户 2026-10-06 批准）：无可压缩早期轮拒绝的诊断待遇。

照搬 #639 阶段 3a 的模式（`test_639_preflight_diagnostics.py`）：同属"写前硬拒绝"，
#639 的 preflight 拒绝有 `CompactionFailure(attempt=0)` + #348 任务可见通道，
本拒绝此前是裸抛（`failures=[]`，注释刻意"保持为空表"）。本次是用户批准的
刻意反转——**只加诊断**：判定式（`>`）、抛点、异常类型一字不动；切分逻辑与
hard 行为不碰（票面铁律）。

- `error_class="no_compactable_early_turn"` 收进既有有界词表；
- 诊断经抛错路径 `ContextWindowExceededError(failures=...)` 由 builder 既有
  except 落 #348 通道；
- 异常文案英文首句与旧文案逐字一致，追加中文恢复指引（对标 #639 B）。
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.compactor import (
    _SUMMARY_ERROR_CLASSES,
    CompactionFailure,
    ContextCompactor,
    ContextWindowExceededError,
)
from agent_harness.session.event import CONTEXT_COMPACTION_FAILED
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

#: 新增的有界拒绝类——写死以钉住词表稳定（#348/#639 同款纪律）。
NO_EARLY_TURN_ERROR_CLASS = "no_compactable_early_turn"

#: 票面阈值口径：max_context_tokens=100000 × hard_guard_threshold=0.85（默认）。
MAX_CONTEXT_TOKENS = 100_000
HARD_LIMIT = 85_000


class _NeverCalledModel:
    """记录调用次数的桩——预检拒绝路径必须零调用。"""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, request):
        self.calls += 1
        return AIMessage(content="(unused: no-early-turn must not call the model)")


def _resume_projection():
    """issue #648 探针的恢复投影形态：完整 tool 块 + 无 early 可压。"""
    return [
        HumanMessage(name="context_compaction_summary", content="x"),
        AIMessage(content="", tool_calls=[{"id": "a", "name": "read", "args": {}}]),
        ToolMessage(tool_call_id="a", content="r"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("token_estimate", [85_001, 90_000])
async def test_no_early_turn_rejection_carries_bounded_diagnosis(token_estimate):
    """超 hard + 无 early ⇒ 抛错携带一条有界诊断（attempt=0），零行为变更。

    判定式、抛点、异常类型与旧行为一字不动；新增的只是 failures 里的诊断
    与文案追加的中文恢复指引。
    """
    model = _NeverCalledModel()
    messages = _resume_projection()

    with pytest.raises(ContextWindowExceededError) as exc_info:
        await ContextCompactor(
            model, max_context_tokens=MAX_CONTEXT_TOKENS,
        ).compact(messages, token_estimate)

    # 英文首句与旧文案逐字一致（既有调用方按前缀匹配不受影响）。
    assert str(exc_info.value).startswith("No complete early turn can be compacted")
    # 中文恢复指引（对标 #639 B 的三条）。
    assert "恢复指引" in str(exc_info.value)
    # 不是摘要尝试：零模型调用。
    assert model.calls == 0

    # 新增：一条任务可见诊断（此前刻意为空表，本次是用户批准的反转）。
    assert len(exc_info.value.failures) == 1
    failure = exc_info.value.failures[0]
    assert isinstance(failure, CompactionFailure)
    assert failure.attempt == 0  # 预检 0 次尝试，不是 1/2
    assert failure.error_class == NO_EARLY_TURN_ERROR_CLASS
    assert failure.error_class in _SUMMARY_ERROR_CLASSES  # 收进既有有界词表
    assert failure.hard_limit == HARD_LIMIT
    assert failure.token_estimate == token_estimate
    assert failure.request_budget_tokens == HARD_LIMIT
    # 卡住的东西：整个原子块的 token 数（判别式左值语义）。
    assert failure.request_token_estimate > 0


def test_no_early_turn_error_class_is_bounded():
    """有界词表显式收录新拒绝类（#348 纪律：词表逐项对应一条拒绝面）。"""
    assert NO_EARLY_TURN_ERROR_CLASS in _SUMMARY_ERROR_CLASSES


@pytest.mark.asyncio
async def test_no_early_turn_diagnosis_lands_in_task_visible_channel(tmp_path):
    """诊断经 builder 既有 except 落 #348 通道（context/compaction_failed）。"""
    session = make_session(tmp_path)
    failure = CompactionFailure(
        attempt=0,
        error_class=NO_EARLY_TURN_ERROR_CLASS,
        message="No complete early turn can be compacted",
        auto_limit=70_000,
        hard_limit=HARD_LIMIT,
        token_estimate=90_000,
        summary_model_id=None,
        duration_ms=0,
        request_token_estimate=1_000,
        request_budget_tokens=HARD_LIMIT,
    )
    builder = ContextBuilder(ScriptedModel([]), max_context_tokens=MAX_CONTEXT_TOKENS)
    builder._record_compaction_failures(session, [failure])

    failures = [e for e in session.events if e.type == CONTEXT_COMPACTION_FAILED]
    assert len(failures) == 1
    assert failures[0].data["attempt"] == 0
    assert failures[0].data["error_class"] == NO_EARLY_TURN_ERROR_CLASS
