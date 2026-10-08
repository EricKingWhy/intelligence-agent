"""#823 / MM-02（A2）：投影视觉判定跟随**当前请求模型**（PRD D6 fallback 降级）。"""

from __future__ import annotations

import io

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from PIL import Image
from pydantic import BaseModel, Field

from agent_harness.agent import AgentRuntime
from agent_harness.attachments.projection import IMAGE_OMITTED_PLACEHOLDER
from agent_harness.context.builder import ContextBuilder
from agent_harness.model.fallback import (
    ModelFallbackCoordinator,
    TwoLevelFallbackPolicy,
)
from agent_harness.session import USER_MESSAGE
from agent_harness.storage.artifact import FakeArtifactStore, compute_byte_artifact_id
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


def _runtime(builder: ContextBuilder, **kwargs) -> AgentRuntime:
    registry = ToolRegistry()
    return AgentRuntime(
        ScriptedModel([]), registry, ToolExecutor(registry),
        context_builder=builder, **kwargs,
    )


def test_builder_set_supports_vision_toggles_flag() -> None:
    builder = ContextBuilder(ScriptedModel([]))
    assert builder._supports_vision is False
    builder.set_supports_vision(True)
    assert builder._supports_vision is True


def test_runtime_syncs_builder_vision_by_role() -> None:
    builder = ContextBuilder(ScriptedModel([]))
    runtime = _runtime(
        builder, vision_by_model_role={"primary": True, "fallback": False},
    )
    runtime._sync_context_vision("primary")
    assert builder._supports_vision is True
    runtime._sync_context_vision("fallback")
    assert builder._supports_vision is False


def test_runtime_without_mapping_does_not_touch_builder() -> None:
    """未注入映射（既有调用方/测试）⇒ 不介入，builder 视觉位保持构造期值。"""
    builder = ContextBuilder(ScriptedModel([]), model_supports_vision=True)
    runtime = _runtime(builder)
    runtime._sync_context_vision("fallback")
    assert builder._supports_vision is True


def test_sync_is_noop_for_builder_without_seam() -> None:
    """非 ContextBuilder 的测试替身没有该接缝 ⇒ 静默跳过，不中断 run。"""

    class _StubBuilder:
        pass

    runtime = _runtime(_StubBuilder())  # type: ignore[arg-type]
    runtime._vision_by_model_role = {"primary": True, "fallback": False}
    runtime._sync_context_vision("fallback")  # 不应抛错


def test_coordinator_current_role_reflects_switch() -> None:
    primary = ScriptedModel([])
    fallback = ScriptedModel([])
    coord = ModelFallbackCoordinator(primary=primary, fallback=fallback)
    assert coord.current_role == "primary"
    # 模拟 `_try_switch` 的效果（never 切回）：current 指向 fallback。
    coord.current = fallback
    assert coord.current_role == "fallback"


def test_reproject_after_switch_untouched_when_target_supports_vision() -> None:
    """切到**支持视觉**的角色（映射为 True）⇒ 原样返回，绝不降级掉图片。"""
    builder = ContextBuilder(ScriptedModel([]))
    runtime = _runtime(
        builder, vision_by_model_role={"primary": True, "fallback": True},
    )
    messages = [HumanMessage(content=[
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
    ])]
    assert runtime._reproject_messages_after_switch("fallback", messages) is messages


def test_coordinator_without_reproject_hook_returns_messages_unchanged() -> None:
    """未注入 `reproject_on_switch`（既有调用方）⇒ 原样返回，行为逐字不变。"""
    coord = ModelFallbackCoordinator(
        primary=ScriptedModel([]), fallback=ScriptedModel([]),
    )
    messages = [HumanMessage(content="x")]
    assert coord._reproject_after_switch("fallback", messages) is messages


# ── A2 残口：切换当步重试 + 后续每步都按 fallback 口径（_drive 级集成） ────────────


class _EchoArgs(BaseModel):
    text: str = Field(default="x")


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回显文本的测试工具。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EchoArgs

    async def execute(self, args: _EchoArgs) -> ToolResult:
        return ToolResult.success(message=args.text, data={"text": args.text})


class _FailOnceModel:
    """第一次 ainvoke/astream 抛错（触发切换），之后委托 inner。"""

    def __init__(self, inner: ScriptedModel, error: Exception) -> None:
        self._inner = inner
        self._error = error
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        self._inner.bind_tools(tools, **kwargs)
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise self._error
        return await self._inner.ainvoke(messages, **kwargs)

    async def astream(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise self._error
        async for chunk in self._inner.astream(messages, **kwargs):
            yield chunk


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _session_with_image(tmp_path):
    session = make_session(tmp_path)
    data = _png()
    attachment_id = compute_byte_artifact_id(data)
    session.append(USER_MESSAGE, {
        "content": "看图",
        "attachments": [{
            "kind": "image", "attachment_id": attachment_id, "media_type": "image/png",
            "bytes": len(data), "width": 8, "height": 6,
        }],
    })
    store = FakeArtifactStore()
    await store.save_bytes(session.session_id, data, mime_type="image/png")
    return session, store


def _user_message(messages):
    """挑出承载原任务文本的那条 user 消息（注入的 protected-facts 也是 HumanMessage）。"""
    for message in messages:
        if not isinstance(message, HumanMessage):
            continue
        content = message.content
        if isinstance(content, str):
            if content in ("看图", f"看图\n{IMAGE_OMITTED_PLACEHOLDER}"):
                return message
        elif content and content[0] == {"type": "text", "text": "看图"}:
            return message
    raise AssertionError("请求里找不到原 user 消息")


def _user_text(message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    # 视觉投影：首文本块（块列表形态）。
    return content[0]["text"]


@pytest.mark.asyncio
async def test_drive_syncs_and_reprojects_vision_after_fallback(tmp_path):
    """A2 全路径：_drive 换到非视觉 fallback 后，
    ① 切换**当步**的重试（`reproject_on_switch`）与
    ② 其**后每一步**（`runtime.py` 每步 `_sync_context_vision`）
    都不再把 `image_url` 块发给 fallback。

    变异验证：
      * 删掉 `runtime.py` 的 `self._sync_context_vision(...)` 那一行 ⇒ 第 2 步快照
        退回视觉口径（图片块列表），② 的断言变红；
      * 去掉 `_new_coordinator` 的 `reproject_on_switch=` 接线 ⇒ 第 1 步（切换当步
        的重试）退回视觉口径，① 的断言变红。
    """
    session, store = await _session_with_image(tmp_path)
    builder = ContextBuilder(
        ScriptedModel([]),
        artifact_store=store,
        artifact_read_tool_name="read_artifact",
        # 装配期按**主模型**（视觉）定死——切换后必须靠同步/重投影改口径。
        model_supports_vision=True,
    )
    registry = ToolRegistry()
    registry.register(_EchoTool())
    primary = _FailOnceModel(
        ScriptedModel([AIMessage(content="unused")]), TimeoutError("primary down"),
    )
    fallback = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"id": "call_0000", "name": "echo", "args": {"text": "hi"}},
        ]),
        AIMessage(content="工具之后的最终回答"),
    ])
    runtime = AgentRuntime(
        model=primary, registry=registry, executor=ToolExecutor(registry),
        max_agent_turns=10,
        context_builder=builder,
        vision_by_model_role={"primary": True, "fallback": False},
        fallback_model=fallback,
        fallback_policy=TwoLevelFallbackPolicy(),
        primary_model_name="primary-model",
        fallback_model_name="fallback-model",
    )

    result = await runtime.run(session, "看图")

    assert result.status == "completed"
    assert len(fallback.snapshots) == 2, (
        "fallback 应被调用两次：切换当步的重试 + 其后的一步"
    )
    # ① 切换当步的 fallback 重试：已重投影为非视觉（D6 残口闭合）。
    step1_user = _user_message(fallback.snapshots[0].messages)
    assert isinstance(step1_user.content, str)
    assert _user_text(step1_user) == f"看图\n{IMAGE_OMITTED_PLACEHOLDER}"
    # ② 其后每一步：runtime.py:1836 每步同步 ⇒ step 2 也是非视觉。
    step2_user = _user_message(fallback.snapshots[1].messages)
    assert isinstance(step2_user.content, str)
    assert _user_text(step2_user) == f"看图\n{IMAGE_OMITTED_PLACEHOLDER}"
