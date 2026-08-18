"""Day4 Task2：ToolExecutor 单次执行链的行为测试。

Validation-first 是本 Task 最锋利的一刀：参数非法 → tool.execute 根本不被调用
（execute 次数 = 0）。测试用【调用计数器】死死盯住这条边界。

零网络、零真实 API：复用 test_registry.py 的 AddTool 思路，
另造 CountingTool（计数器）和 FlakyTool（脚本化异常）证明执行域语义。

Hands-on 落点（见下方 TODO 注释）：
1. 先补 executor.py 里的 TODO ①（ValidationError → INVALID_ARGUMENT 映射）
2. 跑 test_invalid_argument_* 看 RED→GREEN
3. 填本文件 TODO ② 的断言：证明【参数错时 execute 次数 = 0】（核心 fail-fast 证据）
4. 临时把 AddArgs 的 first_number 类型改成 int，喂个字符串参数，确认仍走 INVALID_ARGUMENT
"""

from __future__ import annotations

from typing import Annotated

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.tooling import (
    ErrorCode,
    Tool,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
    ToolSideEffect,
)
from tests.scripted_model import ScriptedModel

# ============================================================================
# 工具：CountingTool —— 用计数器盯住"execute 到底被调了几次"
# ============================================================================


class _CountArgs(BaseModel):
    value: Annotated[int, Field(..., description="要计入的值")]


class CountingTool(Tool):
    """带调用计数器的工具。

    测试 Validation-first 的关键：参数非法时 execute 次数必须为 0。
    没有计数器就没法证明"工具根本没跑"。
    """

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def name(self) -> str:
        return "count"

    @property
    def description(self) -> str:
        return "计入一个整数值。用于演示执行计数。参数：value 为整数。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _CountArgs

    async def execute(self, args: _CountArgs) -> ToolResult:
        self.call_count += 1
        return ToolResult.success(message=f"计入 {args.value}", data={"value": args.value})


# ============================================================================
# 工具：ExplodingTool —— 工具内部抛异常，证明 TOOL_EXECUTION_ERROR 不冒泡
# ============================================================================


class _BoomArgs(BaseModel):
    pass


class ExplodingTool(Tool):
    """execute 内部总是抛 RuntimeError，用来测执行异常的兜底映射。"""

    @property
    def name(self) -> str:
        return "boom"

    @property
    def description(self) -> str:
        return "总是抛异常的测试工具。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _BoomArgs

    async def execute(self, args: BaseModel) -> ToolResult:
        raise RuntimeError("故意爆炸")


# ============================================================================
# 夹具
# ============================================================================


@pytest.fixture
def registry_with_count() -> tuple[ToolRegistry, CountingTool]:
    """注册了 CountingTool 的 Registry + 工具实例（测试要读 call_count）。"""
    reg = ToolRegistry()
    counting = CountingTool()
    reg.register(counting)
    return reg, counting


@pytest.fixture
def executor_with_count(
    registry_with_count: tuple[ToolRegistry, CountingTool]
) -> tuple[ToolExecutor, CountingTool]:
    reg, counting = registry_with_count
    return ToolExecutor(reg), counting


# ============================================================================
# 测试：阶段 3 —— 合法 tool_call 成功透传
# ============================================================================


class TestExecuteSuccess:
    @pytest.mark.asyncio
    async def test_valid_call_returns_success(
        self, executor_with_count: tuple[ToolExecutor, CountingTool]
    ):
        """合法参数 → success，data 正确，execute 被调 1 次。"""
        executor, counting = executor_with_count
        tc = {"id": "call-1", "name": "count", "args": {"value": 42}}

        print("\n========== [阶段3 成功] 合法 tool_call ==========")
        print(f"输入 tool_call = {tc}")
        print("流程: Registry.get('count') ✓ → model_validate({value:42}) ✓ → execute() ✓")

        execution = await executor.execute(tc)

        print("\n--- 产出 ToolExecution ---")
        print(f"tool_call_id = {execution.tool_call_id}")
        print(f"result.ok    = {execution.result.ok}")
        print(f"result.data  = {execution.result.data}")
        print(f"result.model_dump_json() = {execution.result.model_dump_json()}")
        print(f"CountingTool.call_count  = {counting.call_count}  ← 工具被执行了 1 次")
        print("=" * 52)

        assert execution.tool_call_id == "call-1"
        assert execution.result.ok is True
        assert execution.result.data == {"value": 42}
        assert counting.call_count == 1

    @pytest.mark.asyncio
    async def test_tool_call_id_preserved(
        self, executor_with_count: tuple[ToolExecutor, CountingTool]
    ):
        """tool_call_id 原样透传——ToolMessage 配对协议的硬约束（Task 4/5 要用）。"""
        executor, _ = executor_with_count
        tc = {"id": "abc-123-xyz", "name": "count", "args": {"value": 1}}

        execution = await executor.execute(tc)

        assert execution.tool_call_id == "abc-123-xyz"


# ============================================================================
# 测试：阶段 1 —— 未知工具名 → TOOL_NOT_FOUND（execute 次数 0）
# ============================================================================


class TestToolNotFound:
    @pytest.mark.asyncio
    async def test_unknown_name_returns_tool_not_found(
        self, executor_with_count: tuple[ToolExecutor, CountingTool]
    ):
        """未知工具名 → TOOL_NOT_FOUND，retryable=False。"""
        executor, counting = executor_with_count
        tc = {"id": "call-x", "name": "does_not_exist", "args": {}}

        print("\n========== [阶段1 失败] 未知工具名 ==========")
        print(f"输入 tool_call = {tc}")
        print("流程: Registry.get('does_not_exist') ✗ 抛 KeyError → 映射 TOOL_NOT_FOUND")
        print("       （还没走到 validation，更没走到 execute）")

        execution = await executor.execute(tc)

        print("\n--- 产出 ToolExecution（失败结果，不是异常）---")
        print(f"tool_call_id            = {execution.tool_call_id}")
        print(f"result.ok               = {execution.result.ok}")
        print(f"result.error_code       = {execution.result.error_code}")
        print(f"result.retryable        = {execution.result.retryable}")
        print(f"result.message          = {execution.result.message}")
        print(f"result.model_dump_json()")
        print(f"  = {execution.result.model_dump_json()}")
        print(f"CountingTool.call_count = {counting.call_count}  ← 工具根本没被找到，自然没执行")
        print("=" * 52)

        assert execution.result.ok is False
        assert execution.result.error_code == ErrorCode.TOOL_NOT_FOUND
        assert execution.result.retryable is False
        assert counting.call_count == 0  # 没找到工具，execute 不可能被调

    @pytest.mark.asyncio
    async def test_no_exception_bubbles_up(
        self, executor_with_count: tuple[ToolExecutor, CountingTool]
    ):
        """Executor 对外永远返回结果，绝不抛异常（哪怕工具不存在）。"""
        executor, _ = executor_with_count
        tc = {"id": "call-x", "name": "ghost", "args": {}}

        # 不用 pytest.raises 包：如果抛了，测试直接红——正是我们想要的断言。
        execution = await executor.execute(tc)
        assert execution is not None


# ============================================================================
# 测试：阶段 2 —— 参数非法 → INVALID_ARGUMENT（execute 次数 0）
# 这一组是 Validation-first 的核心证据，也是 Hands-on 的落点。
# ============================================================================


class TestInvalidArgument:
    @pytest.mark.asyncio
    async def test_missing_required_field(
        self, executor_with_count: tuple[ToolExecutor, CountingTool]
    ):
        """缺必填字段 value → INVALID_ARGUMENT，retryable=False。"""
        executor, counting = executor_with_count
        tc = {"id": "call-v", "name": "count", "args": {}}  # 缺 value

        print("\n========== [阶段2 失败] 缺必填字段 ← Validation-first 核心 ==========")
        print(f"输入 tool_call = {tc}")
        print("流程: Registry.get('count') ✓ → model_validate({}) ✗ 抛 ValidationError")
        print("       → 映射 INVALID_ARGUMENT")
        print("       ←★ execute() 根本没被调用 ★")

        execution = await executor.execute(tc)

        print("\n--- 产出 ToolExecution（失败结果，模型可据此自纠错）---")
        print(f"tool_call_id            = {execution.tool_call_id}")
        print(f"result.ok               = {execution.result.ok}")
        print(f"result.error_code       = {execution.result.error_code}")
        print(f"result.retryable        = {execution.result.retryable}")
        print(f"result.message          = {execution.result.message}")
        print(f"result.model_dump_json()")
        print(f"  = {execution.result.model_dump_json()}")
        print(f"CountingTool.call_count = {counting.call_count}  ← 必须是 0！参数错→没执行")
        print("=" * 52)

        assert execution.result.ok is False
        assert execution.result.error_code == ErrorCode.INVALID_ARGUMENT
        assert execution.result.retryable is False
        # message 要能让模型自纠错：至少提到工具名或字段名
        assert "count" in execution.result.message or "value" in execution.result.message

        # Validation-first 最硬的证据：校验失败 → execute 次数必须为 0，工具根本没跑。
        assert counting.call_count == 0

    @pytest.mark.asyncio
    async def test_wrong_type_argument(
        self, executor_with_count: tuple[ToolExecutor, CountingTool]
    ):
        """类型错（value 传字符串给 int 字段）→ INVALID_ARGUMENT，execute 次数 0。"""
        executor, counting = executor_with_count
        tc = {"id": "call-t", "name": "count", "args": {"value": "不是整数"}}

        print("\n========== [阶段2 失败] 类型错 ← 同样走 INVALID_ARGUMENT ==========")
        print(f"输入 tool_call = {tc}")
        print("字段 value 期望 int，实际传了 str → Pydantic 类型校验失败")

        execution = await executor.execute(tc)

        print("\n--- 产出 ---")
        print(f"result.error_code       = {execution.result.error_code}")
        print(f"result.message          = {execution.result.message}")
        print(f"CountingTool.call_count = {counting.call_count}  ← 类型错也是 0")
        print("=" * 52)

        assert execution.result.ok is False
        assert execution.result.error_code == ErrorCode.INVALID_ARGUMENT
        assert counting.call_count == 0


# ============================================================================
# 测试：阶段 3 —— 工具内部抛异常 → TOOL_EXECUTION_ERROR（不冒泡）
# ============================================================================


class TestToolExecutionError:
    @pytest.mark.asyncio
    async def test_tool_raises_returns_execution_error(self):
        """execute 内部抛 RuntimeError → 映射 TOOL_EXECUTION_ERROR，不冒泡给上层。"""
        reg = ToolRegistry()
        reg.register(ExplodingTool())
        executor = ToolExecutor(reg)
        tc = {"id": "call-b", "name": "boom", "args": {}}

        print("\n========== [阶段3 失败] 工具内部抛异常 ==========")
        print(f"输入 tool_call = {tc}")
        print("流程: Registry.get('boom') ✓ → model_validate ✓ → execute() 抛 RuntimeError")
        print("       → Executor 宽捕获 → 映射 TOOL_EXECUTION_ERROR（不冒泡给上层）")

        execution = await executor.execute(tc)

        print("\n--- 产出（异常被吞并固化成 ErrorCode）---")
        print(f"result.ok               = {execution.result.ok}")
        print(f"result.error_code       = {execution.result.error_code}")
        print(f"result.retryable        = {execution.result.retryable}")
        print(f"result.message          = {execution.result.message}")
        print(f"result.model_dump_json()")
        print(f"  = {execution.result.model_dump_json()}")
        print("=" * 52)

        assert execution.result.ok is False
        assert execution.result.error_code == ErrorCode.TOOL_EXECUTION_ERROR
        assert execution.result.retryable is False
        # message 要暴露异常类型和内容，方便模型/人排查
        assert "RuntimeError" in execution.result.message or "故意爆炸" in execution.result.message


# ============================================================================
# 测试：ToolExecution 能序列化成 ToolMessage 的 content（Task 5 接入前的形状证据）
# ============================================================================


class TestToolExecutionSerialization:
    @pytest.mark.asyncio
    async def test_result_json_can_be_toolmessage_content(
        self, executor_with_count: tuple[ToolExecutor, CountingTool]
    ):
        """ToolResult.model_dump_json() 能直接当 ToolMessage content 回填给模型。"""
        executor, _ = executor_with_count
        tc = {"id": "call-1", "name": "count", "args": {"value": 7}}
        execution = await executor.execute(tc)

        # 这是 Task 5 AgentRuntime 会做的事：拿 result 的 JSON 当 ToolMessage content
        content = execution.result.model_dump_json()

        print("\n========== [序列化] ToolResult → ToolMessage content ==========")
        print(f"ToolExecution.result.model_dump_json():")
        print(f"  {content}")
        print("↑ 这串 JSON 就是将来回填给模型的 ToolMessage content（Task 5 接线点）")
        print("=" * 52)

        # model_dump_json() 输出紧凑 JSON（冒号后无空格）
        assert '"ok":true' in content
        assert '"value":7' in content


# ============================================================================
# 测试：Executor 不依赖真实模型 —— 用 ScriptedModel 证明执行链纯本地
# ============================================================================


class TestExecutorIsLocal:
    def test_registry_export_can_feed_scripted_model(
        self, registry_with_count: tuple[ToolRegistry, CountingTool]
    ):
        """Registry 导出的菜单能喂给 ScriptedModel——证明执行链与模型解耦。"""
        reg, _ = registry_with_count
        defs = reg.export_model_definitions()
        model = ScriptedModel([AIMessage(content="ok")])
        bound = model.bind_tools(defs, strict=True)

        print("\n========== [解耦证据] Registry 导出的模型菜单 ==========")
        print("这菜单就是将来 bind_tools 喂给模型的（同一个 args_schema 单一事实源）:")
        print(f"{defs}")
        print("=" * 52)

        assert bound is model
        assert model.bound_tools == defs
