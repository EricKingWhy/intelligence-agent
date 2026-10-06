"""#767 方案 A：对象型字段收到 JSON 字符串时，INVALID_ARGUMENT 的 message 给出可操作指引。

背景：`remember_this` 的对象型字段 `payload` 入站是 JSON 字符串时，Pydantic
判别联合拒绝 → `INVALID_ARGUMENT, retryable=False`，message 是 Pydantic 原生
文案，模型 10 次原样重试（不知道"反转义"这个修复动作）。

方案 A = 不做"宽容重解析"（spec `04_TOOL_RUNTIME.md` §5：
"Executor MUST NOT 偷偷替模型猜测/修复参数"；LangGraph/LangChain/
Anthropic/OpenAI 四家成熟产品无一家这么做），只在 executor 的错误构造处
加通用机制：某条 ValidationError 的 `loc` 指向的字段收到的 `input` 是 str、
且 `json.loads` 能成功解析为 dict 时，在 message 后追加可操作指引；
保持 `retryable=False`（spec §6）。通用——任何工具的对象型字段都受益。
成熟产品背书：LangGraph `ToolInvocationError` 模式（错误回模型自纠）+
自家 Vision:172「Tool 参数错误能回模型自修正」。
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from agent_harness.tooling import (
    ErrorCode,
    Tool,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
)


class _ObjPayload(BaseModel):
    kind: str
    text: str


class _ObjArgs(BaseModel):
    payload: _ObjPayload


class _OtherArgs(BaseModel):
    config: _ObjPayload


class _ObjectFieldTool(Tool):
    """对象型字段工具——模拟 `remember_this` 的 payload 形态（通用机制载体）。"""

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def name(self) -> str:
        return "object_field_tool"

    @property
    def description(self) -> str:
        return "接收一个对象参数 payload。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _ObjArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        self.call_count += 1
        return ToolResult.success("ok")


class _GenericObjectTool(Tool):
    """第二个工具、另一个字段名——证明指引机制是通用的，不只认 memory v2。"""

    @property
    def name(self) -> str:
        return "generic_object_tool"

    @property
    def description(self) -> str:
        return "接收一个对象参数 config。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _OtherArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        return ToolResult.success("ok")


def _make_executor() -> tuple[ToolExecutor, _ObjectFieldTool, _GenericObjectTool]:
    reg = ToolRegistry()
    obj_tool = _ObjectFieldTool()
    generic_tool = _GenericObjectTool()
    reg.register(obj_tool)
    reg.register(generic_tool)
    return ToolExecutor(reg), obj_tool, generic_tool


class TestStringifiedJsonGuidance:
    @pytest.mark.asyncio
    async def test_stringified_json_object_field_gets_guidance(self):
        """对象型字段收到 JSON 字符串 → INVALID_ARGUMENT + message 追加可操作指引。"""
        executor, obj_tool, _ = _make_executor()
        tc = {
            "id": "call-767-a",
            "name": "object_field_tool",
            "args": {"payload": json.dumps({"kind": "semantic", "text": "美式"})},
        }

        execution = await executor.execute(tc)

        assert execution.result.ok is False
        assert execution.result.error_code == ErrorCode.INVALID_ARGUMENT
        assert execution.result.retryable is False  # spec §6：INVALID_ARGUMENT 默认不重试
        # 指引必须点名：哪个字段 + 收到的是 JSON 字符串 + 正确的修复动作
        assert "payload" in execution.result.message
        assert "JSON 字符串" in execution.result.message
        assert "直接传对象" in execution.result.message
        # 校验失败 → 工具根本没执行（Validation-first 不动）
        assert obj_tool.call_count == 0

    @pytest.mark.asyncio
    async def test_non_json_string_gets_no_guidance(self):
        """对象型字段收到普通字符串（非 JSON）→ 原样错误，不追加指引。"""
        executor, _, _ = _make_executor()
        tc = {
            "id": "call-767-b",
            "name": "object_field_tool",
            "args": {"payload": "这不是 JSON，只是一段普通文本"},
        }

        execution = await executor.execute(tc)

        assert execution.result.ok is False
        assert execution.result.error_code == ErrorCode.INVALID_ARGUMENT
        assert "JSON 字符串" not in execution.result.message

    @pytest.mark.asyncio
    async def test_valid_dict_executes_without_error(self):
        """合法 dict 照常通过校验、执行，不受新机制影响。"""
        executor, obj_tool, _ = _make_executor()
        tc = {
            "id": "call-767-c",
            "name": "object_field_tool",
            "args": {"payload": {"kind": "semantic", "text": "美式"}},
        }

        execution = await executor.execute(tc)

        assert execution.result.ok is True
        assert obj_tool.call_count == 1

    @pytest.mark.asyncio
    async def test_guidance_is_generic_across_tools(self):
        """通用机制：非 memory 工具的对象型字段（字段名不同）同样受益。"""
        executor, _, _ = _make_executor()
        tc = {
            "id": "call-767-d",
            "name": "generic_object_tool",
            "args": {"config": json.dumps({"kind": "episodic", "text": "出差"})},
        }

        execution = await executor.execute(tc)

        assert execution.result.ok is False
        assert execution.result.error_code == ErrorCode.INVALID_ARGUMENT
        assert execution.result.retryable is False
        assert "config" in execution.result.message
        assert "JSON 字符串" in execution.result.message
        assert "直接传对象" in execution.result.message
