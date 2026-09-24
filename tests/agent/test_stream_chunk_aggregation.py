"""#281（B8）：流式 chunk 聚合的语义 golden。

票面 #281 的硬要求是「**先**补『多 chunk 合并后正确』的用例（作为 golden），**再**做优化；
优化后必须仍然全绿」。本文件就是那批 golden。

## 断言面为什么分两层（如实记）

- **端到端层**（真跑 `run_stream`，读会话事件）：只能观测聚合结果进事的子集——
  `model/completed` 的 `content` / `tool_calls` / `usage`（聚合后的 AIMessage 对象本身
  不进事件，下一轮 `messages` 是 `context_builder` 从事件 derive 出来的投影）。
- **表达式层**（`_naive_fold` vs `_oneshot_expression`）：把全部字段都比到——
  `type` / `content` / `tool_calls` / `tool_call_chunks` / `additional_kwargs` /
  `response_metadata` / `usage_metadata` / `id` / `chunk_position`。
  这一层钉的是「一次性官方聚合与逐 chunk `+` 等价」这条**语义不变式**，也就是
  优化后 `_drive` 里那条表达式的全部可观测后果。

两层并集 = AC1/AC2 的断言面。优化后的**红证**（见 PR / 台账）靠定向变异：
把聚合顺序反转、丢掉尾 chunk、把 list 形态退回逐项折叠并改坏语义 —— 各自打红对应用例。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage

from agent_harness.agent import AgentRuntime
from agent_harness.session import MODEL_COMPLETED, Session
from agent_harness.tooling import ToolExecutor, ToolRegistry
from tests.conftest import make_session

# --------------------------------------------------------------------------------------
# 模型替身：按剧本吐**原始 chunk 列表**（真实 provider 的分片形态：args 跨 chunk 拼）
# --------------------------------------------------------------------------------------


class _ChunkScriptModel:
    """把每条剧本（一串 AIMessageChunk）原样 astream 出去；记录每次调用的 messages。"""

    def __init__(self, scripts: list[list[AIMessageChunk]]) -> None:
        self._scripts = list(scripts)
        self._cursor = 0
        self.requests: list[list[AnyMessage]] = []

    async def astream(self, messages: list[AnyMessage]) -> AsyncIterator[AIMessageChunk]:
        self.requests.append(list(messages))
        if self._cursor >= len(self._scripts):
            raise RuntimeError("_ChunkScriptModel 剧本耗尽：Runtime 调用次数超出预期")
        script = self._scripts[self._cursor]
        self._cursor += 1
        for chunk in script:
            yield chunk

    async def ainvoke(self, messages: list[AnyMessage]) -> AIMessage:
        raise AssertionError("本文件的用例只走 run_stream（ainvoke 是 run() 的路径）")


def _build_runtime(model: Any) -> AgentRuntime:
    registry = ToolRegistry()
    return AgentRuntime(
        model=model,
        registry=registry,
        executor=ToolExecutor(registry),
        max_agent_turns=5,
    )


async def _drive(model: _ChunkScriptModel, session: Session, text: str = "hi") -> None:
    runtime = _build_runtime(model)
    async for _ in runtime.run_stream(session, text):
        pass


def _model_completed(session: Session) -> dict[str, Any]:
    events = [e for e in session.events if e.type == MODEL_COMPLETED]
    assert len(events) == 1, f"期望恰好一条 model/completed，实际 {len(events)} 条"
    return events[0].data


# --------------------------------------------------------------------------------------
# 改造前形态（golden 参照）：逐 chunk `+`，逐字复刻 `_drive` 里那段聚合
# --------------------------------------------------------------------------------------


def _naive_fold(chunks: list[AIMessageChunk]) -> AIMessage:
    ai: AIMessage = chunks[0]
    for c in chunks[1:]:
        ai = ai + c  # type: ignore[assignment]
    if not isinstance(ai, AIMessage):
        ai = AIMessage(content=ai.content, tool_calls=ai.tool_calls)  # type: ignore[arg-type]
    return ai


def _oneshot_expression(chunks: list[AIMessageChunk]) -> AIMessage:
    """优化后 `_drive` 实际执行的那条表达式（官方一次性聚合）。"""
    ai: AIMessage = chunks[0]
    if chunks[1:]:
        ai = ai + chunks[1:]  # type: ignore[assignment]
    if not isinstance(ai, AIMessage):
        ai = AIMessage(content=ai.content, tool_calls=ai.tool_calls)  # type: ignore[arg-type]
    return ai


def _fingerprint(msg: AIMessage) -> str:
    """全字段指纹（含内部字段），用于表达式层等价断言。"""
    return json.dumps(
        {
            "type": type(msg).__name__,
            "content": msg.content if isinstance(msg.content, str) else repr(msg.content),
            "tool_calls": msg.tool_calls,
            "tool_call_chunks": getattr(msg, "tool_call_chunks", None),
            "additional_kwargs": msg.additional_kwargs,
            "response_metadata": msg.response_metadata,
            "usage_metadata": getattr(msg, "usage_metadata", None),
            "id": msg.id,
            "chunk_position": getattr(msg, "chunk_position", None),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


# --------------------------------------------------------------------------------------
# 语料：真实流式形态 + 易分歧点
# --------------------------------------------------------------------------------------

SPLIT_ARGS_CHUNKS = [
    AIMessageChunk(content="", id="run-provider-1", tool_call_chunks=[
        {"name": "bash", "args": '{"comm', "id": "tc1", "index": 0}]),
    AIMessageChunk(content="", tool_call_chunks=[{"args": 'and": "ec', "index": 0}]),
    AIMessageChunk(content="", tool_call_chunks=[{"args": 'ho hi"}', "index": 0}]),
]

TWO_CALLS_CHUNKS = [
    AIMessageChunk(content="好", id="lc_run-abc", tool_call_chunks=[
        {"name": "bash", "args": '{"command":', "id": "tc1", "index": 0},
        {"name": "read", "args": '{"path":', "id": "tc2", "index": 1}]),
    AIMessageChunk(content="的", tool_call_chunks=[
        {"args": ' "ls"}', "index": 0}, {"args": ' "a.txt"}', "index": 1}]),
    AIMessageChunk(content="。"),
]

CORPORA: list[tuple[str, list[AIMessageChunk]]] = [
    ("纯文本 3 chunk", [AIMessageChunk(content="Hello"), AIMessageChunk(content=", "),
                        AIMessageChunk(content="world")]),
    ("tool args 分片（index 对齐）", SPLIT_ARGS_CHUNKS),
    ("两个 tool_calls 交错 index + 文本", TWO_CALLS_CHUNKS),
    ("重复 index（同槽续写）", [
        AIMessageChunk(content="", tool_call_chunks=[
            {"name": "bash", "args": '{"a"', "id": "t", "index": 0}]),
        AIMessageChunk(content="", tool_call_chunks=[{"args": ': 1', "index": 0}]),
        AIMessageChunk(content="", tool_call_chunks=[{"args": '}', "index": 0}]),
    ]),
    ("多 chunk 带 usage（累加语义）", [
        AIMessageChunk(content="a", usage_metadata={"input_tokens": 11, "output_tokens": 7,
                                                    "total_tokens": 18}),
        AIMessageChunk(content="b", usage_metadata={"input_tokens": 1, "output_tokens": 1,
                                                    "total_tokens": 2}),
        AIMessageChunk(content="c"),
    ]),
    ("additional_kwargs / response_metadata 同键冲突", [
        AIMessageChunk(content="a", additional_kwargs={"x": 1},
                       response_metadata={"finish_reason": "stop"}),
        AIMessageChunk(content="b", additional_kwargs={"x": 2, "y": 3},
                       response_metadata={"id": "r2"}),
        AIMessageChunk(content="c", additional_kwargs={"x": 4},
                       response_metadata={"finish_reason": "length"}),
    ]),
    ("chunk_position=last 在中段 + 混合 id", [
        AIMessageChunk(content="a", id="lc_run-1", chunk_position="last"),
        AIMessageChunk(content="b", id="provider-2"),
        AIMessageChunk(content="c", id="lc_run-3"),
    ]),
    ("provider id 只在末 chunk（rank 语义）", [
        AIMessageChunk(content="a", id="lc_run-1"),
        AIMessageChunk(content="b", id="lc_run-2"),
        AIMessageChunk(content="c", id="provider-9"),
    ]),
    ("单 chunk", [AIMessageChunk(content="only")]),
]

#: 只能在**表达式层**比的语料：args 拼完不是合法 JSON ⇒ LangChain 把它归到
#: `invalid_tool_calls`，端到端会走「空响应」失败臂（没有 model/completed 可读）。
#: 聚合本身的语义仍必须与改造前一致，所以这一条留在表达式层钉住。
CORPORA_EXPRESSION_ONLY: list[tuple[str, list[AIMessageChunk]]] = [
    ("args 拼完非法 JSON（归 invalid_tool_calls）", [
        AIMessageChunk(content="", tool_call_chunks=[
            {"name": "bash", "args": '{"a"', "id": "t", "index": 0}]),
        AIMessageChunk(content="", tool_call_chunks=[{"args": "garbage", "index": 0}]),
    ]),
]


# --------------------------------------------------------------------------------------
# AC1/AC2：端到端（真跑 run_stream）
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_only_chunks_merge_to_concatenated_content(tmp_path):
    """AC1-1：纯文本多 chunk ⇒ content 与逐 chunk 拼接逐字相同。"""
    chunks = [AIMessageChunk(content="Hello"), AIMessageChunk(content=", "),
              AIMessageChunk(content="world")]
    session = make_session(tmp_path)
    await _drive(_ChunkScriptModel([chunks]), session)

    data = _model_completed(session)
    assert data["content"] == "Hello, world"
    assert data["content"] == _naive_fold(chunks).content
    assert "tool_calls" not in data


@pytest.mark.asyncio
async def test_tool_call_args_split_across_chunks_merge_field_by_field(tmp_path):
    """AC1-2 / AC2：args 跨 chunk 分片 ⇒ 合并后 tool_calls 逐字段与改造前相同。

    冻结断言（字面量）与「改造前形态」断言**都**写：前者防 LangChain 语义漂移，
    后者防本票的聚合改动改语义。
    """
    session = make_session(tmp_path)
    await _drive(_ChunkScriptModel([SPLIT_ARGS_CHUNKS]), session)

    data = _model_completed(session)
    assert data["content"] == ""
    # 冻结字面量：分片 args 必须被拼成完整 JSON 再解析
    assert data["tool_calls"] == [
        {"id": "tc1", "name": "bash", "args": {"command": "echo hi"}}
    ]
    # 与改造前形态逐字段相同
    expected = _naive_fold(SPLIT_ARGS_CHUNKS)
    assert data["tool_calls"] == [
        {"id": c["id"], "name": c["name"], "args": c["args"]} for c in expected.tool_calls
    ]


@pytest.mark.asyncio
async def test_two_tool_calls_interleaved_merge_in_index_order(tmp_path):
    """AC1-2：两个 tool_calls 的 args 交错分片 ⇒ 顺序与字段都按 index 对齐。"""
    session = make_session(tmp_path)
    await _drive(_ChunkScriptModel([TWO_CALLS_CHUNKS]), session)

    data = _model_completed(session)
    assert data["content"] == "好的。"
    assert data["tool_calls"] == [
        {"id": "tc1", "name": "bash", "args": {"command": "ls"}},
        {"id": "tc2", "name": "read", "args": {"path": "a.txt"}},
    ]
    expected = _naive_fold(TWO_CALLS_CHUNKS)
    assert data["content"] == expected.content
    assert data["tool_calls"] == [
        {"id": c["id"], "name": c["name"], "args": c["args"]} for c in expected.tool_calls
    ]


@pytest.mark.asyncio
async def test_empty_stream_is_reported_as_empty_response(tmp_path):
    """AC1-3：0 chunk ⇒ 聚合退化成空 content，随后被「空响应不是成功」按既有语义拦下。

    聚合块自己的 `else` 分支只在**下游**可见（R6-2 空响应判定），所以这里断言端到端
    可观测的后果：`model/started → model/failed → run/failed` 且**没有** model/completed
    （失败原因是未分类 RuntimeError，即 R6-2 那条；具体文案不进事件、只进日志）。
    另外用「一个空 content chunk」对照：两者失败形状相同 ⇒ 拦的是空内容，不是"没有 chunk"。
    """
    session = make_session(tmp_path)
    runtime = _build_runtime(_ChunkScriptModel([[]]))
    events = [e async for e in runtime.run_stream(session, "hi")]

    assert [e.type for e in events] == [
        "user/message", "run/started", "model/started", "model/failed", "run/failed"
    ]
    assert all(e.type != MODEL_COMPLETED for e in session.events)

    # 对照臂：一条空 content chunk（有 chunk、无内容）⇒ 同样失败（拦的是空内容）
    session_b = make_session(tmp_path / "b")
    runtime_b = _build_runtime(_ChunkScriptModel([[AIMessageChunk(content="")]]))
    events_b = [e async for e in runtime_b.run_stream(session_b, "hi")]
    assert [e.type for e in events_b] == [
        "user/message", "run/started", "model/started", "model/failed", "run/failed"
    ]


@pytest.mark.asyncio
async def test_single_chunk_with_and_without_tool_calls(tmp_path):
    """AC1-4：单 chunk ⇒ 与改造前相同（含纯 tool_calls 的单 chunk）。"""
    text_only = [AIMessageChunk(content="lone")]
    session_a = make_session(tmp_path / "a")
    await _drive(_ChunkScriptModel([text_only]), session_a)
    assert _model_completed(session_a)["content"] == "lone"

    single_call = [AIMessageChunk(
        content="", tool_calls=[{"id": "t", "name": "bash", "args": {"command": "ls"}}])]
    session_b = make_session(tmp_path / "b")
    await _drive(_ChunkScriptModel([single_call]), session_b)
    data = _model_completed(session_b)
    assert data["content"] == ""
    assert data["tool_calls"] == [{"id": "t", "name": "bash", "args": {"command": "ls"}}]
    assert data["tool_calls"] == [
        {"id": c["id"], "name": c["name"], "args": c["args"]}
        for c in _naive_fold(single_call).tool_calls
    ]


@pytest.mark.asyncio
async def test_usage_metadata_merges_like_before(tmp_path):
    """AC1-5：多 chunk 带 usage ⇒ 合并语义（逐字段累加 / 缺失省略）与改造前相同。"""
    chunks = [
        AIMessageChunk(content="a", usage_metadata={"input_tokens": 11, "output_tokens": 7,
                                                    "total_tokens": 18}),
        AIMessageChunk(content="b", usage_metadata={"input_tokens": 1, "output_tokens": 1,
                                                    "total_tokens": 2}),
        AIMessageChunk(content="c"),
    ]
    session = make_session(tmp_path)
    await _drive(_ChunkScriptModel([chunks]), session)

    data = _model_completed(session)
    expected = _naive_fold(chunks)
    assert data["content"] == "abc"
    assert expected.usage_metadata == {"input_tokens": 12, "output_tokens": 8,
                                       "total_tokens": 20}
    assert data["usage"] == {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}


# --------------------------------------------------------------------------------------
# 表达式层：全字段等价（AC1-5/6 的 metadata 面 + AC2 的「逐字段」口径）
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,chunks", CORPORA + CORPORA_EXPRESSION_ONLY,
    ids=[c[0] for c in CORPORA + CORPORA_EXPRESSION_ONLY],
)
def test_oneshot_aggregation_is_field_identical_to_naive_fold(name, chunks):
    """优化后的那条表达式与逐 chunk `+` 在**全部字段**上逐字段相同。

    这一层能覆盖端到端观测不到的内部字段（id / chunk_position / tool_call_chunks /
    additional_kwargs / response_metadata）。
    """
    assert _fingerprint(_oneshot_expression(chunks)) == _fingerprint(_naive_fold(chunks)), name


@pytest.mark.asyncio
async def test_aggregation_issues_a_single_merge_call_for_the_whole_stream(tmp_path, monkeypatch):
    """#281 的红证：聚合必须把 N-1 个后续 chunk 交给**一次**官方归并调用。

    这是**结构性**判据（不测时间，故不进 flaky 名单）：改造前逐 chunk `+` ⇒
    `add_ai_message_chunks` 被调 N-1 次、每次都复制已累计内容（二次复制）；
    改造后应为 **1 次**。任何人把实现退回逐项折叠，本用例立刻转红。
    """
    import langchain_core.messages.ai as ai_mod

    calls: list[int] = []
    real = ai_mod.add_ai_message_chunks

    def spy(left: Any, *others: Any) -> Any:
        calls.append(len(others))
        return real(left, *others)

    monkeypatch.setattr(ai_mod, "add_ai_message_chunks", spy)

    n = 12
    chunks = [AIMessageChunk(content=f"c{i}") for i in range(n)]
    session = make_session(tmp_path)
    await _drive(_ChunkScriptModel([chunks]), session)

    assert _model_completed(session)["content"] == "".join(f"c{i}" for i in range(n))
    assert calls == [n - 1], (
        f"期望恰好一次归并调用（吃掉其余 {n - 1} 个 chunk），实际 {len(calls)} 次：{calls}"
    )


@pytest.mark.parametrize("name,chunks", CORPORA, ids=[c[0] for c in CORPORA])
@pytest.mark.asyncio
async def test_end_to_end_observable_fields_match_naive_fold(tmp_path, name, chunks):
    """端到端：真跑 run_stream，`model/completed` 的可观测字段与改造前形态一致。"""
    session = make_session(tmp_path)
    await _drive(_ChunkScriptModel([chunks]), session)

    data = _model_completed(session)
    expected = _naive_fold(chunks)
    content = expected.content if isinstance(expected.content, str) else ""
    assert data["content"] == content, name
    if expected.tool_calls:
        assert data["tool_calls"] == [
            {"id": c["id"], "name": c["name"], "args": c["args"]} for c in expected.tool_calls
        ], name
    else:
        assert "tool_calls" not in data, name
    if expected.usage_metadata:
        assert data["usage"] == {
            "prompt_tokens": expected.usage_metadata.get("input_tokens", 0),
            "completion_tokens": expected.usage_metadata.get("output_tokens", 0),
            "total_tokens": expected.usage_metadata.get("total_tokens", 0),
        }, name
