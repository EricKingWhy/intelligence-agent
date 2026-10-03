"""#551 模型流终结语义缺口：红测（M10-2 / M10-3 / M10-8）。

冻结的判据来源：`docs/adr/0014-phase12-web-search-reliability.md` 的 Model Fallback
节（决策 14/15/16/18）、`docs/adr/0016-streaming-ui-detached-run.md` §3.1（`text/delta`
是合帧 durable 事实，15.4 只保证"部分内容以已落盘 delta 保留"）、以及 #551 票面的
issue-audit（不得删除部分内容、不得按字符串去重、合法重复必须保留、缺 [DONE] 的
「有 finish_reason」与「无 finish_reason」两条路径必须分开）。

本文件只钉**语义缺口**，不发明新基座：接缝沿用
`tests/agent/test_model_fallback_runtime.py` 的 `_StalledStreamModel` / `FailOnceModel`
形状与 `tests/scripted_model.py` 的 `ScriptedModel`。

覆盖：
- R1 primary 完整答完（finish_reason=stop）后 stall → fallback 重答：不得静默拼接
  （必须有 attempt 边界标记），且最终消息与 run/completed.final_text 一致。
- R2 干净 EOF、缺 [DONE]、有合法 finish_reason ⇒ 视为正常终结（不标中断）。
- R3 缺 [DONE] 且无 finish_reason ⇒ 与 R2 语义不同：可观测标记 + 保留部分内容。
- R4（comment 1，scope 待裁决）`model/request` 在途可见性——当前 RED，见用例 docstring。
- R5 primary + fallback 双挂 ⇒ run/failed.data 同时保留两级错误类型名。
- 反向锚 G1/G2：合法重复文本原样保留、中断保留部分内容（不得回归）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import BaseModel, Field

from agent_harness.agent.runtime import AgentRuntime
from agent_harness.model.fallback import TwoLevelFallbackPolicy
from agent_harness.session import (
    MODEL_COMPLETED,
    MODEL_FALLBACK,
    MODEL_REQUEST,
    RUN_COMPLETED,
    RUN_FAILED,
    TEXT_DELTA,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


class _EchoArgs(BaseModel):
    text: str = Field(default="x", description="回显文本")


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "原样回显文本的测试工具。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EchoArgs

    async def execute(self, args: _EchoArgs) -> ToolResult:
        return ToolResult.success(message=args.text, data={"text": args.text})


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(EchoTool())
    return reg


def _runtime(
    primary: Any, fallback: Any | None, *,
    idle_timeout: float = 0.0, total_timeout: float = 0.0,
) -> AgentRuntime:
    return AgentRuntime(
        model=primary, registry=_registry(), executor=ToolExecutor(_registry()),
        max_agent_turns=10,
        fallback_model=fallback,
        fallback_policy=TwoLevelFallbackPolicy(),
        primary_model_name="primary-model",
        fallback_model_name="fallback-model",
        stream_idle_timeout=idle_timeout,
        stream_total_timeout=total_timeout,
    )


def _events(session: Any, type_: str) -> list[Any]:
    return [e for e in session._events if e.type == type_]


# ---------------------------------------------------------------------------
# 替身（只注入一种故障，不模拟逻辑）
# ---------------------------------------------------------------------------


class _StalledStreamModel:
    """吐一个 chunk 后长挂（模拟"无 [DONE] 的连接 hang"，触发 idle 看门狗）。"""

    def __init__(self, first_chunk: str, stall_seconds: float) -> None:
        self._first = first_chunk
        self._stall = stall_seconds

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        raise NotImplementedError

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(content=self._first)
        await asyncio.sleep(self._stall)
        yield AIMessageChunk(content=" done")


class _FinishThenStallModel:
    """吐完整答案（末 chunk 带 finish_reason=stop）后长挂。

    这是 inst-d3 的形状：chunks 完整 + finish_reason=stop + 无 [DONE] + 连接 hang，
    于是 idle 看门狗触发 fallback —— 而 primary 其实**已经答完了**（M10-3 根因）。
    """

    def __init__(self, text: str, finish_reason: str = "stop", stall: float = 99.0) -> None:
        self._text = text
        self._finish_reason = finish_reason
        self._stall = stall

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        raise NotImplementedError

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(
            content=self._text,
            response_metadata={"finish_reason": self._finish_reason},
        )
        await asyncio.sleep(self._stall)


class _CleanEofWithFinishReasonModel:
    """吐完整答案（末 chunk 带合法 finish_reason）后**干净 EOF**（无 [DONE]、无异常）。"""

    def __init__(self, text: str, finish_reason: str) -> None:
        self._text = text
        self._finish_reason = finish_reason

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        raise NotImplementedError

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(
            content=self._text,
            response_metadata={"finish_reason": self._finish_reason},
        )


class _AlwaysFailingModel:
    """每次 ainvoke/astream 都抛同一异常（双挂场景的两级替身）。"""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        raise self._error

    async def astream(self, messages, **kwargs):
        raise self._error
        yield AIMessageChunk(content="")  # 声明 async generator（不可达）


class _FakeAPIConnectionError(Exception):
    """类名命中原 openai 瞬时错误表（`fallback.py` 的 `_TRANSIENT_ERROR_NAMES`）的替身。"""


def _http_500() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://primary.invalid/v1/chat")
    response = httpx.Response(500, request=request)
    return httpx.HTTPStatusError("500 Internal Server Error", request=request, response=response)


# ---------------------------------------------------------------------------
# R1（M10-3）：primary 完整答完后 stall → fallback 重答
# ---------------------------------------------------------------------------


class TestFallbackReanswerBoundary:
    @pytest.mark.asyncio
    async def test_fallback_reanswer_is_not_silently_concatenated(self, tmp_path):
        """primary 已产出完整答案（finish_reason=stop）后 stall → fallback 重答。

        硬红线：不得删除部分内容、不得按字符串去重 —— content 必须仍是
        primary + fallback 的**完整**拼接；缺口在于"静默"：必须有可区分的 attempt
        边界标记，且最终消息与 run/completed.final_text 一致。
        """
        primary_text = "primary 已经答完了。"
        fallback_text = "fallback 又答了一遍。"
        primary = _FinishThenStallModel(primary_text)
        fallback = ScriptedModel([AIMessage(content=fallback_text)])
        runtime = _runtime(primary, fallback, idle_timeout=0.2)
        session = make_session(tmp_path)

        async for _ in runtime.run_stream(session, "你好"):
            pass

        completed = _events(session, MODEL_COMPLETED)
        assert len(completed) == 1
        content = completed[0].data["content"]
        # 不删、不去重：两段原文都在（合法重复也保留）
        assert content == primary_text + fallback_text

        fallbacks = _events(session, MODEL_FALLBACK)
        assert len(fallbacks) == 1
        fb_data = fallbacks[0].data
        # attempt 边界标记：primary 在切换前已产出的量 + 它是否已给出 finish_reason
        assert fb_data.get("primary_content_chars") == len(primary_text)
        assert fb_data.get("primary_finish_reason") == "stop"

        # C2（配对）：本步发生过 fallback ⇒ 拼接消息的 finish_reason（此处来自
        # primary）不得落进 model/completed —— 「有 finish_reason」只能代表**无
        # fallback 的干净终结**。primary 的收尾事实已由 model/fallback 携带，
        # 答案层不得把它冒充成"整条拼接流说完了"（M10-3 症状面）。
        assert "finish_reason" not in completed[0].data

        # 最终消息与 run/completed.final_text 一致（fallback 新块与最终消息配对）
        finals = _events(session, RUN_COMPLETED)
        assert len(finals) == 1
        assert finals[0].data["final_text"] == content


# ---------------------------------------------------------------------------
# C2 负例（R1/R2，修后重审）：primary 零产出 ⇒ fallback 的 finish_reason 必须落
# ---------------------------------------------------------------------------


class TestFallbackWithoutPrimaryOutput:
    """原判据「有任一 fallback 就压制 finish_reason」**过度**（R1/R2 修后重审）。

    primary 零产出即失败（流式连一个 chunk 都没吐 / ainvoke 入口从未产生响应）时，
    `model/completed` 的 `ai` 就是 fallback **单独**的响应——它的 `finish_reason`
    是权威事实。压制等于凭空丢掉"这一次模型确实正常说完了"的信号，并把"零贡献"
    与"有贡献"两种形状混为一谈。两个入口各一遍：任一路丢键都判红。
    """

    @pytest.mark.asyncio
    async def test_stream_entry_zero_primary_output_records_fallback_finish_reason(
        self, tmp_path,
    ):
        """流式入口：primary 零 chunk 即失败 ⇒ `ai` 全属 fallback，finish_reason 照落。"""
        primary = _AlwaysFailingModel(_http_500())
        fallback = _CleanEofWithFinishReasonModel("fallback 的回答。", "stop")
        runtime = _runtime(primary, fallback)
        session = make_session(tmp_path)

        async for _ in runtime.run_stream(session, "你好"):
            pass

        completed = _events(session, MODEL_COMPLETED)
        assert len(completed) == 1
        data = completed[0].data
        assert data["content"] == "fallback 的回答。"
        assert data.get("finish_reason") == "stop"
        assert data.get("stream_terminated_without_finish_reason") is not True

        # 切换事实照记，零贡献的边界字段按"非平凡才落"省略（`event_data` 口径）
        fallbacks = _events(session, MODEL_FALLBACK)
        assert len(fallbacks) == 1
        assert "primary_content_chars" not in fallbacks[0].data
        assert "primary_finish_reason" not in fallbacks[0].data

    @pytest.mark.asyncio
    async def test_invoke_entry_zero_primary_output_records_fallback_finish_reason(
        self, tmp_path,
    ):
        """ainvoke 入口（`run`）：primary 抛异常从未产出 ⇒ 照落（原判据丢事实最明显）。"""
        primary = _AlwaysFailingModel(_http_500())
        fallback = ScriptedModel([AIMessage(
            content="fallback 的答案", response_metadata={"finish_reason": "stop"},
        )])
        runtime = _runtime(primary, fallback)
        session = make_session(tmp_path)

        await runtime.run(session, "你好")

        completed = _events(session, MODEL_COMPLETED)
        assert len(completed) == 1
        assert completed[0].data["content"] == "fallback 的答案"
        assert completed[0].data.get("finish_reason") == "stop"


# ---------------------------------------------------------------------------
# R2 / R3（M10-2）：缺 [DONE] 的两条路径必须语义不同
# ---------------------------------------------------------------------------


class TestEofTerminalHonesty:
    @pytest.mark.asyncio
    async def test_clean_eof_with_finish_reason_is_normal(self, tmp_path):
        """R2：干净 EOF、缺 [DONE]、但有合法 finish_reason ⇒ 视为正常终结。"""
        primary = _CleanEofWithFinishReasonModel("完整的回答。", "stop")
        runtime = _runtime(primary, None)
        session = make_session(tmp_path)

        async for _ in runtime.run_stream(session, "你好"):
            pass

        completed = _events(session, MODEL_COMPLETED)
        assert len(completed) == 1
        data = completed[0].data
        assert data["content"] == "完整的回答。"
        # 有 finish_reason ⇒ 正常终结，不得被标成"流断了"
        assert data.get("finish_reason") == "stop"
        assert data.get("stream_terminated_without_finish_reason") is not True
        assert len(_events(session, RUN_COMPLETED)) == 1
        assert not _events(session, RUN_FAILED)

    @pytest.mark.asyncio
    async def test_clean_eof_without_finish_reason_is_honestly_marked(self, tmp_path):
        """R3：缺 [DONE] 且无 finish_reason（中断）⇒ 有可观测标记，且保留部分内容。

        与 R2 语义必须不同：这一支不臆断截断（#506），但必须诚实标注"流在没有
        finish_reason 的情况下结束"；已产出的部分内容原样保留。
        """
        primary = ScriptedModel([AIMessage(content="partial more ")])
        runtime = _runtime(primary, None)
        session = make_session(tmp_path)

        async for _ in runtime.run_stream(session, "你好"):
            pass

        completed = _events(session, MODEL_COMPLETED)
        assert len(completed) == 1
        data = completed[0].data
        assert data["content"] == "partial more "  # 部分内容绝不删除
        assert data.get("stream_terminated_without_finish_reason") is True
        assert "finish_reason" not in data  # 无 finish_reason 就不落这个键
        assert len(_events(session, RUN_COMPLETED)) == 1

    @pytest.mark.asyncio
    async def test_two_eof_shapes_are_distinguishable(self, tmp_path):
        """R2 与 R3 的 durable 事件必须能区分（一个是 finish_reason，一个是中断标记）。"""
        s_finish = make_session(tmp_path / "finish")
        async for _ in _runtime(
            _CleanEofWithFinishReasonModel("ok", "stop"), None,
        ).run_stream(s_finish, "你好"):
            pass
        s_interrupted = make_session(tmp_path / "interrupted")
        async for _ in _runtime(
            ScriptedModel([AIMessage(content="half")]), None,
        ).run_stream(s_interrupted, "你好"):
            pass

        d_finish = _events(s_finish, MODEL_COMPLETED)[0].data
        d_interrupted = _events(s_interrupted, MODEL_COMPLETED)[0].data
        assert d_finish.get("finish_reason") == "stop"
        assert d_interrupted.get("stream_terminated_without_finish_reason") is True
        assert d_finish.get("stream_terminated_without_finish_reason") is not True
        assert "finish_reason" not in d_interrupted


# ---------------------------------------------------------------------------
# R4（comment 1，scope 待裁决）：model/request 在途不可见
# ---------------------------------------------------------------------------


_ACTION_REQUIRED = (
    "#551 comment 1：model/request 在响应完成后才落盘，在途调用不可见。"
    "这是票面 comment 的附带观察，不在 M10-2/3/8 的三条 AC 内；是否并入本票 "
    "需用户裁决。当前 tip 复现为 RED（text/delta 的 seq 早于 model/request）。"
)


class TestInFlightVisibility:
    @pytest.mark.xfail(reason=_ACTION_REQUIRED, strict=False)
    @pytest.mark.asyncio
    async def test_model_request_is_visible_while_in_flight(self, tmp_path):
        """R4：消费到第一个 text/delta（模型仍在途）时，model/request 应已落盘。

        现状（复现 comment 1 / 侦察 CASE C）：`model/request` 在整条流拉完之后才
        记账/落盘 ⇒ 此刻事件流里还没有"这次调用"。标记为 xfail 以记录缺口、
        不污染 GREEN 计数；scope 归属见 `_ACTION_REQUIRED`。
        """
        primary = _StalledStreamModel(first_chunk="部分回答", stall_seconds=0.06)
        session = make_session(tmp_path)
        stream = _runtime(primary, None).run_stream(session, "你好")

        saw_delta = False
        async for frame in stream:
            if frame.type == TEXT_DELTA:
                saw_delta = True
                break
        assert saw_delta, "没走到真·在途那一刻，本用例失去区分力"

        # 此刻模型流仍悬挂（尚未 aclose），检查 durable 流里的调用可见性
        in_flight = _events(session, MODEL_REQUEST)
        delta_seqs = [e.seq for e in _events(session, TEXT_DELTA)]
        await stream.aclose()

        assert in_flight, "在途模型调用必须在 durable 事件流里可见"
        assert in_flight[0].seq < delta_seqs[0], (
            "model/request 应先于首个 text/delta 落盘（当前相反：text/delta 在前）"
        )


# ---------------------------------------------------------------------------
# R5（M10-8）：双挂保留两级错误类型名
# ---------------------------------------------------------------------------


class TestDoubleFailureAttribution:
    @pytest.mark.asyncio
    async def test_double_failure_keeps_both_error_levels(self, tmp_path):
        """primary HTTP 500（瞬时→触发切换）+ fallback 连接拒绝 → run/failed 两级归因。"""
        primary = _AlwaysFailingModel(_http_500())
        fallback = _AlwaysFailingModel(_FakeAPIConnectionError("connection refused"))
        runtime = _runtime(primary, fallback)
        session = make_session(tmp_path)

        async for _ in runtime.run_stream(session, "你好"):
            pass

        failed = _events(session, RUN_FAILED)
        assert len(failed) == 1
        data = failed[0].data
        # 两级错误**类型名**都在（不泄露正文）；首因不得只剩 model/fallback 里
        assert data.get("primary_error") == "HTTPStatusError"
        assert data.get("fallback_error") == "_FakeAPIConnectionError"
        # 既有 reason/message 归因面保持不变（fallback 那次异常仍是终端 reason）
        assert data.get("reason") == "_FakeAPIConnectionError"

    @pytest.mark.asyncio
    async def test_double_failure_keeps_both_error_levels_on_invoke_entry(self, tmp_path):
        """同一条双挂契约在 `run()`（ainvoke 入口）上同样成立——两条入口各一遍。"""
        primary = _AlwaysFailingModel(_http_500())
        fallback = _AlwaysFailingModel(_FakeAPIConnectionError("connection refused"))
        runtime = _runtime(primary, fallback)
        session = make_session(tmp_path)

        result = await runtime.run(session, "你好")

        assert result.status == "failed"
        data = _events(session, RUN_FAILED)[0].data
        assert data.get("primary_error") == "HTTPStatusError"
        assert data.get("fallback_error") == "_FakeAPIConnectionError"


# ---------------------------------------------------------------------------
# 反向锚（不得回归）
# ---------------------------------------------------------------------------


class TestReverseAnchors:
    @pytest.mark.asyncio
    async def test_legitimate_repeated_text_is_preserved(self, tmp_path):
        """G1：单条流内合法重复文本逐字保留——绝不得按字符串去重。"""
        repeated = "这是重复的一句。这是重复的一句。"
        runtime = _runtime(ScriptedModel([AIMessage(content=repeated)]), None)
        session = make_session(tmp_path)

        async for _ in runtime.run_stream(session, "你好"):
            pass

        completed = _events(session, MODEL_COMPLETED)
        assert completed[0].data["content"] == repeated
        joined = "".join(e.data["delta"] for e in _events(session, TEXT_DELTA))
        assert joined == repeated

    @pytest.mark.asyncio
    async def test_interrupted_stream_keeps_partial_content(self, tmp_path):
        """G2：非正常终结的部分内容仍在 durable text/delta（不得删除）。"""
        primary = _StalledStreamModel(first_chunk="半句话", stall_seconds=99.0)
        runtime = _runtime(primary, None, idle_timeout=0.2)
        session = make_session(tmp_path)

        async for _ in runtime.run_stream(session, "你好"):
            pass

        joined = "".join(e.data["delta"] for e in _events(session, TEXT_DELTA))
        assert "半句话" in joined
        assert len(_events(session, RUN_FAILED)) == 1
