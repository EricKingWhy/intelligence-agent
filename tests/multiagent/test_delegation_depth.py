"""动态 SubAgent 的 grantable / depth 边界（#286，ADR-0015 决策 11 + spec 10 §5/§12）。

票面冻结语义（逐条对应本文件用例）：

1. 根 depth=0；`max_depth=1` 允许 root → child；`max_depth=2` 允许 root → child → grandchild；
2. runtime 拥有的 remaining depth 不能被 child 的 `AgentSpec` 抬高；
3. 有效子配额 = `min(parent_remaining - 1, child_spec.max_depth)`；
4. remaining=0 时 child registry 不能含 `delegate`；
5. 每个 child 的 tool_scope ⊆ parent grantable scope，**永不**从全量 registry 重建。

测试接缝（都在公开边界上，不碰内部实现）：
- `InProcessSubagentProvider.activate()/run()`：spawn 的成败与产物；
- `DelegateTool.execute()`：模型侧的委派入口（失败经 ToolResult 显式回填）；
- 端到端 `AgentRuntime.run()` 树：真实 executor + 真实 session 落盘。

## 红证分工：本文件只管**行为面**，工厂契约面在 `tests/agent/`

`tests/agent/test_profiles_factory.py::test_create_rejects_missing_grantable` 承担工厂
契约面（`src/agent_harness/agent/` 的 focused 面就是 `tests/agent/`，`grantable` 必填
这件事的证据该落在那边）。本文件负责**行为红**：票面
「Red→green reproduction for `max_depth=1` plus `tool_scope={"delegate"}`」在未修复
代码上必须失败，且失败理由必须是**行为**（depth 用尽仍拿到了 delegate），不能是
签名错。所以 `_env(max_depth=None)` 表示**刻意不传** `max_depth`（走 provider 的出厂
默认 = V1 语义），depth=1 的三条用例因此在未修复代码上是「不该成功却成功了」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent.factory import AgentFactory
from agent_harness.agent.profiles import BUILTIN_PROFILES, AgentSpec
from agent_harness.agent.runtime import AgentRuntime
from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

_BASE_TOOLS = ("read", "delegate")


class _EchoArgs(BaseModel):
    text: str = Field(default="x", description="回显")


class _ToolStub(Tool):
    """最小工具替身——真实 ``Tool`` 子类，所以 executor 读它的
    side_effect/permission 也拿得到契约默认值（不是 duck-typing 的裸对象）。"""

    def __init__(self, name: str = "read") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} 替身"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _EchoArgs

    async def execute(self, args: _EchoArgs) -> ToolResult:
        return ToolResult.success(message=args.text)


def _delegate_args(target: str, task: str) -> object:
    return type("_Args", (), {"target": target, "task": task, "constraints": []})()


def _spec(name: str, tools: set[str], max_depth: int = 1) -> AgentSpec:
    return AgentSpec(
        name=name, description=f"{name} 测试档位", system_prompt=f"你是 {name}。",
        tool_scope=frozenset(tools), max_depth=max_depth,
    )


def _tool_call(call_id: str, target: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "id": call_id, "name": "delegate", "args": {"target": target, "task": "测试任务"},
    }])


class _Env:
    """已激活的 provider + delegate 工具 + 子 registry 捕获列表。

    ``max_depth=None`` = **不传**该参数 → 走 provider 出厂默认（V1 语义 depth=1）。
    这是刻意的：depth=1 那几条用例必须在**旧签名**上也跑得起来，红证才是行为红。
    """

    def __init__(self, tmp_path: Path, *, profiles: dict[str, AgentSpec] | None,
                 child_model: ScriptedModel, max_depth: int | None = None) -> None:
        self.store = JsonlSessionStore(tmp_path / "sessions")
        self.workspaces = WorkspaceRegistry(root=tmp_path / "workspaces")
        self.parent_session_id = "parent-0001"
        self.workspaces.create(self.parent_session_id, workspace_root=tmp_path / "ws")
        self.captured: list[ToolRegistry] = []
        self.provider = InProcessSubagentProvider(profiles=profiles)
        self.delegate = DelegateTool(self.provider, max_delegations=99)

        def executor_factory(registry: ToolRegistry) -> ToolExecutor:
            self.captured.append(registry)
            return ToolExecutor(registry)

        self.factory = AgentFactory(
            model=child_model, primary_model_name="main-model",
            executor_factory=executor_factory,
        )
        self.registry = ToolRegistry()
        for name in _BASE_TOOLS:
            self.registry.register(_ToolStub(name))
        if max_depth is None:
            self.provider.activate(
                factory=self.factory,
                source_registry=self.registry,
                session_store=self.store,
                workspace_registry=self.workspaces,
                parent_session_id=self.parent_session_id,
            )
        else:
            self.provider.activate(
                factory=self.factory,
                source_registry=self.registry,
                session_store=self.store,
                workspace_registry=self.workspaces,
                parent_session_id=self.parent_session_id,
                max_depth=max_depth,
            )

    def child_registries(self) -> list[set[str]]:
        return [{tool.name for tool in reg.list()} for reg in self.captured]


def _env(tmp_path: Path, *, profiles: dict[str, AgentSpec] | None = None,
         max_depth: int | None = None,
         child_model: ScriptedModel | None = None) -> _Env:
    return _Env(
        tmp_path, profiles=profiles, max_depth=max_depth,
        child_model=child_model or ScriptedModel([AIMessage(content="child 完成")]),
    )


class TestDepthBoundary:
    """冻结语义 1/2/3 + AC「Depth is consumed at every spawn and cannot be reset」."""

    @pytest.mark.asyncio
    async def test_spawn_rejects_delegate_when_no_depth_remains(self, tmp_path):
        """红证（票面 repro）：根 max_depth=1 ⇒ child remaining=0 ⇒ 申请 delegate 显式拒绝。

        未修复代码上本用例**失败**——不是签名错，是行为错：`grantable` 省略 ⇒
        默认可授予全集 ⇒ 子 registry 真的拿到了 `delegate`（递归委派逃逸通道），
        于是 `result.ok` 为真、`assert not result.ok` 挂掉。
        """
        env = _env(tmp_path, profiles={"greedy": _spec("greedy", {"read", "delegate"})})

        result = await env.delegate.execute(_delegate_args("greedy", "试图递归委派"))

        assert not result.ok, "depth 用尽仍创建了带 delegate 的 child"
        assert "delegate" in result.message, f"拒绝信息必须点名工具：{result.message}"
        assert "不可授予" in result.message, "越权必须是显式拒绝，不是静默剔除"

    @pytest.mark.asyncio
    async def test_child_cannot_raise_depth_via_own_max_depth(self, tmp_path):
        """冻结语义 2：child spec 自己写 max_depth=99 也抬不动 runtime 的配额。

        未修复代码上同样是**行为红**：child 申请成功（`result.ok` 为真）。
        """
        env = _env(tmp_path, profiles={
            "greedy": _spec("greedy", {"read", "delegate"}, max_depth=99),
        })

        result = await env.delegate.execute(_delegate_args("greedy", "自称可以更深"))

        assert not result.ok, "child spec 的 max_depth 不得抬升 effective allowance"
        assert "delegate" in result.message

    @pytest.mark.asyncio
    async def test_depth_one_child_registry_has_no_delegate(self, tmp_path):
        """V1 语义的**回归护栏**（不是红证）：depth=1 下普通 child 只拿到它申请的工具。

        父 registry 里有 `delegate`，child 没申请 ⇒ child registry 里就没有。
        这条在未修复代码上**也是绿的**（省略 grantable = 全集，child 没申请仍拿不到），
        它守的是「加了 depth 机制别把 V1 的收窄弄坏」。
        """
        env = _env(tmp_path, profiles={"coding_like": _spec("coding_like", {"read"})})

        result = await env.delegate.execute(_delegate_args("coding_like", "普通任务"))

        assert result.ok
        assert env.child_registries() == [{"read"}], (
            f"child registry 只应有它申请且可授予的工具：{env.child_registries()}"
        )

    @pytest.mark.asyncio
    async def test_depth_two_tree_allows_grandchild_but_stops_there(self, tmp_path):
        """冻结语义 1/3：max_depth=2 ⇒ root → child → grandchild，grandchild 无 delegate。

        端到端真实链路（真 executor / 真 session 落盘）：根委派 sup；sup 再委派 leaf。
        sup 的 remaining=1 ⇒ 它的 registry **含** delegate；leaf 的 remaining=0 ⇒
        leaf 的申请被显式拒绝。
        """
        child_model = ScriptedModel([
            _tool_call("c1", "leaf"),
            AIMessage(content="sup 收尾"),
        ])
        env = _env(
            tmp_path, max_depth=2, child_model=child_model,
            profiles={
                "sup": _spec("sup", {"read", "delegate"}, max_depth=2),
                "leaf": _spec("leaf", {"read", "delegate"}, max_depth=99),
            },
        )
        root_model = ScriptedModel([_tool_call("d1", "sup"), AIMessage(content="根收尾")])
        root = AgentRuntime(model=root_model, registry=env.registry,
                            executor=ToolExecutor(env.registry), max_steps=5)

        run = await root.run(make_session(tmp_path), "跑一棵两层树")

        assert run.status == "completed"
        assert env.child_registries() == [{"read", "delegate"}], (
            f"depth=2 时 child 应持有 delegate：{env.child_registries()}"
        )
        sup_session = env.provider.last_child_sessions[0]
        results = [json.loads(e.data["content"]) for e in sup_session.events
                   if e.type == "tool/result"]
        assert results and results[-1]["ok"] is False, "第二层委派必须被拒"
        assert "delegate" in results[-1]["message"] and "不可授予" in results[-1]["message"]

    @pytest.mark.asyncio
    async def test_nested_spawn_uses_child_registry_not_root_registry(self, tmp_path):
        """冻结语义 5：子代理的 scope 不得从**根** registry 重建。

        根持有 `bash`，sup 的 scope 没有。sup 让 leaf 申请 `bash`：leaf 的
        source = **sup 的** registry ⇒ bash 是「该层缺席」⇒ 降级警告，leaf 建成
        `{read}`；若实现错把根的 registry 当 source，bash 就变成「存在但不可授予」
        ⇒ 显式拒绝、leaf 根本建不出来。两种结局 leaf 都拿不到 bash，但**口径**
        可区分（本用例钉住正确的那个）。
        """
        child_model = ScriptedModel([
            _tool_call("c1", "leaf"),
            AIMessage(content="sup 收尾"),
        ])
        env = _env(
            tmp_path, max_depth=2, child_model=child_model,
            profiles={
                "sup": _spec("sup", {"read", "delegate"}, max_depth=2),
                "leaf": _spec("leaf", {"read", "bash"}, max_depth=1),
            },
        )
        env.registry.register(_ToolStub("bash"))  # 根持有 bash；sup 的 scope 没有它

        root_model = ScriptedModel([_tool_call("d1", "sup"), AIMessage(content="根收尾")])
        root = AgentRuntime(model=root_model, registry=env.registry,
                            executor=ToolExecutor(env.registry), max_steps=5)

        await root.run(make_session(tmp_path), "跨层要工具")

        assert env.child_registries() == [{"read", "delegate"}, {"read"}], (
            f"leaf 必须按「该层缺席」降级为 read，而不是从根 registry 捞 bash："
            f"{env.child_registries()}"
        )


class TestBuiltinProfilesUnaffected:
    """AC4：内置档位声明面零变化（边界是运行期收窄，不是改档位）。"""

    def test_builtin_profiles_keep_declared_scopes(self):
        assert "delegate" in BUILTIN_PROFILES["main"].tool_scope
        assert "delegate" not in BUILTIN_PROFILES["coding"].tool_scope
        assert "delegate" not in BUILTIN_PROFILES["research_review"].tool_scope
        assert BUILTIN_PROFILES["main"].max_depth == 1, (
            "V1 出厂深度仍是 1；打开深度是改配置不是改架构（ADR-0015 决策 7）"
        )
