"""#528（IMP-11）工具曝光级别 / 延迟加载：红→绿测试。

票面验收：
1. 注册 N 个 ``deferred`` 工具时，模型请求定义数不含它们；
2. ``tool_search`` 命中后下一轮请求包含目标工具；
3. 权限剔除的工具不出现在请求定义中（与既有 dropped_tools 边界一致）；
4. 回归：``direct`` 工具全量注入行为不变；工具调用配对/重试语义不变（由既有
   工具绑定与 executor 测试回归覆盖，本文件只钉默认曝光级别的导出等价性）。

机制（PORT DESIGN，见 docs/agents/528-research.md）：曝光级别只控制"模型看得到
什么"（export_model_definitions / bind_tools 的定义集），不控制"能调用什么"——
执行权边界仍是 Registry 成员资格 + Permission/Approval；tool_search 是注册进
Registry 的普通 Tool，经统一 ToolExecutor 执行，无隐藏调用路径。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from agent_harness.tooling.exposure import (
    ToolExposure,
    ToolExposureController,
    ToolSearchTool,
)
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

# ---- 夹具：可指定曝光级别的探针工具 ----


class _ProbeArgs(BaseModel):
    topic: str = ""


class _ProbeTool(Tool):
    """名字/描述/曝光级别可参数化的探针工具。"""

    def __init__(self, *, name: str, description: str, exposure: ToolExposure | None = None):
        self._name = name
        self._description = description
        self._exposure = exposure

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def args_schema(self) -> type[BaseModel]:
        return _ProbeArgs

    @property
    def exposure(self) -> ToolExposure:
        if self._exposure is None:
            return ToolExposure.DIRECT
        return self._exposure

    async def execute(self, args: _ProbeArgs) -> ToolResult:
        return ToolResult.success(message=f"{self._name} ok", data={"topic": args.topic})


def _registry_with_deferred() -> ToolRegistry:
    """1 个 direct + 3 个 deferred（其中 budget_reporter 是 tool_search 的靶子）。"""
    reg = ToolRegistry()
    reg.register(_ProbeTool(name="visible_a", description="direct 工具 A，总是可见"))
    reg.register(_ProbeTool(
        name="weather_lookup", description="deferred weather lookup by city name",
        exposure=ToolExposure.DEFERRED,
    ))
    reg.register(_ProbeTool(
        name="budget_reporter", description="deferred monthly budget report tool",
        exposure=ToolExposure.DEFERRED,
    ))
    reg.register(_ProbeTool(
        name="stock_pricer", description="deferred stock price quote tool",
        exposure=ToolExposure.DEFERRED,
    ))
    return reg


# ---- 1 + 4：导出面过滤与默认行为回归 ----


class TestExportFiltering:
    def test_deferred_and_hidden_not_exported(self):
        """注册 deferred/hidden 工具时，export_model_definitions 不含它们。"""
        reg = _registry_with_deferred()
        reg.register(_ProbeTool(
            name="hidden_x", description="hidden 工具", exposure=ToolExposure.HIDDEN,
        ))
        names = [d["name"] for d in reg.export_model_definitions()]
        assert names == ["visible_a"]

    def test_default_exposure_is_direct_and_export_unchanged(self):
        """默认曝光级别 = direct：既有工具零改动，导出行为与 #528 之前逐字等价。"""
        tool = _ProbeTool(name="plain", description="普通工具")
        assert tool.exposure is ToolExposure.DIRECT
        reg = ToolRegistry()
        reg.register(tool)
        defs = reg.export_model_definitions()
        assert [d["name"] for d in defs] == ["plain"]
        assert set(defs[0]) == {"name", "description", "parameters"}


# ---- 2：controller 搜索 / 激活 / 定义集演化 ----


class TestExposureController:
    def test_search_activates_only_matched_deferred(self):
        """tool_search 命中后目标工具进入定义集，未命中的不进。"""
        reg = _registry_with_deferred()
        controller = ToolExposureController(reg)
        # 初始定义集 = direct only
        assert [d["name"] for d in controller.current_definitions()] == ["visible_a"]

        matches = controller.search("monthly budget report")
        assert [m["name"] for m in matches] == ["budget_reporter"]

        now = [d["name"] for d in controller.current_definitions()]
        assert "budget_reporter" in now
        assert "stock_pricer" not in now
        assert "weather_lookup" not in now

    def test_search_never_returns_direct_or_hidden(self):
        """搜索对象只有 deferred 工具——direct 已在菜单里，hidden 不可达。"""
        reg = _registry_with_deferred()
        reg.register(_ProbeTool(
            name="hidden_y", description="monthly budget hidden",
            exposure=ToolExposure.HIDDEN,
        ))
        controller = ToolExposureController(reg)
        matches = controller.search("monthly budget")
        assert [m["name"] for m in matches] == ["budget_reporter"]

    def test_search_no_match_activates_nothing(self):
        reg = _registry_with_deferred()
        controller = ToolExposureController(reg)
        assert controller.search("totally unrelated quantum flux") == []
        assert controller.activated == frozenset()


# ---- 2：tool_search 经统一 Executor 执行（无隐藏调用路径）----


class TestToolSearchViaExecutor:
    @pytest.mark.asyncio
    async def test_tool_search_runs_through_unified_executor(self):
        reg = _registry_with_deferred()
        controller = ToolExposureController(reg)
        reg.register(ToolSearchTool(controller))
        executor = ToolExecutor(reg)
        execution = await executor.execute(
            {"id": "call_ts_1", "name": "tool_search", "args": {"query": "stock price"}}
        )
        assert execution.result.ok
        assert [t["name"] for t in execution.result.data["tools"]] == ["stock_pricer"]
        assert "stock_pricer" in {d["name"] for d in controller.current_definitions()}

    def test_tool_search_is_direct_exposure(self):
        """tool_search 自己必须 direct：模型看不到搜索工具就没法发现任何 deferred。"""
        reg = ToolRegistry()
        controller = ToolExposureController(reg)
        tool_search = ToolSearchTool(controller)
        reg.register(tool_search)
        assert tool_search.exposure is ToolExposure.DIRECT
        assert [d["name"] for d in reg.export_model_definitions()] == ["tool_search"]


# ---- 票面 AC（runtime 级）：模型请求定义数的演化 ----


class TestRuntimeRequestDefinitions:
    @pytest.mark.asyncio
    async def test_deferred_absent_then_present_after_tool_search(self, tmp_path):
        """AC1+AC2：首轮请求定义不含 deferred；tool_search 命中后下一轮含目标工具。"""
        reg = _registry_with_deferred()
        controller = ToolExposureController(reg)
        reg.register(ToolSearchTool(controller))
        model = ScriptedModel([
            AIMessage(content="", tool_calls=[{
                "name": "tool_search",
                "args": {"query": "monthly budget report"},
                "id": "call_ts_1",
                "type": "tool_call",
            }]),
            AIMessage(content="已找到并使用预算工具。"),
        ])
        runtime = AgentRuntime(
            model=model, registry=reg, executor=ToolExecutor(reg),
            tool_exposure=controller,
        )
        result = await runtime.run(make_session(tmp_path), "帮我看看预算")
        assert result.status == "completed"

        assert len(model.snapshots) == 2
        first = {d["name"] for d in model.snapshots[0].tools or []}
        second = {d["name"] for d in model.snapshots[1].tools or []}
        # AC1：deferred 工具不在首轮请求定义中；tool_search 与 direct 工具在。
        for deferred in ("weather_lookup", "budget_reporter", "stock_pricer"):
            assert deferred not in first
        assert "tool_search" in first and "visible_a" in first
        # AC2：tool_search 命中后，下一轮请求包含目标工具。
        assert "budget_reporter" in second
        assert "stock_pricer" not in second

    @pytest.mark.asyncio
    async def test_default_runtime_binding_unchanged_without_controller(self, tmp_path):
        """回归：默认曝光级别（全 direct）+ 未接控制器时，注入行为与 #528 之前一致。"""
        reg = ToolRegistry()
        reg.register(_ProbeTool(name="plain_a", description="普通工具 A"))
        reg.register(_ProbeTool(name="plain_b", description="普通工具 B"))
        model = ScriptedModel([AIMessage(content="done")])
        runtime = AgentRuntime(
            model=model, registry=reg, executor=ToolExecutor(reg),
        )
        await runtime.run(make_session(tmp_path), "你好")
        assert len(model.snapshots) == 1
        # 全 direct 默认 ⇒ 全量注入（与 #528 之前逐字等价）。
        assert {d["name"] for d in model.snapshots[0].tools or []} == {
            "plain_a", "plain_b",
        }


# ---- AC3：权限剔除的工具不出现在请求定义中 ----


class TestPermissionDroppedTools:
    @pytest.mark.asyncio
    async def test_dropped_tool_never_enters_request_definitions(self, tmp_path):
        """被权限/档位剔除的工具（registry 收窄后缺席）搜不到、也不进请求定义。"""
        full = _registry_with_deferred()
        # 模拟 assembly 的收窄顺序：先 filtered（物理剔除），再注册 tool_search。
        narrowed = full.filtered(frozenset({"visible_a", "budget_reporter"}))
        controller = ToolExposureController(narrowed)
        narrowed.register(ToolSearchTool(controller))
        # narrowed 里 budget_reporter 是 deferred（仍在册、只是不进菜单），
        # weather_lookup / stock_pricer 已被"权限"剔除。
        model = ScriptedModel([
            AIMessage(content="", tool_calls=[{
                "name": "tool_search",
                "args": {"query": "weather lookup by city"},
                "id": "call_ts_2",
                "type": "tool_call",
            }]),
            AIMessage(content="没有找到可用工具。"),
        ])
        runtime = AgentRuntime(
            model=model, registry=narrowed, executor=ToolExecutor(narrowed),
            tool_exposure=controller,
        )
        result = await runtime.run(make_session(tmp_path), "查天气")
        assert result.status == "completed"
        # 被剔除的工具在任何一轮请求定义中都不出现。
        for snapshot in model.snapshots:
            names = {d["name"] for d in snapshot.tools or []}
            assert "weather_lookup" not in names
            assert "stock_pricer" not in names
        # 搜索没命中被剔除的工具：激活集为空，工具也确实不在册（执行面同样拒绝）。
        assert controller.activated == frozenset()
        with pytest.raises(KeyError):
            narrowed.get("weather_lookup")
