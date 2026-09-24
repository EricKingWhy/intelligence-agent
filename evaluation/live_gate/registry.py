"""Live Gate 场景注册表（`#307` Must Do：为后续 T3/T4/T7/T9/T10 预留场景注册入口）。

## 为什么不复用 `ToolRegistry`

`agent_harness.tooling.registry.ToolRegistry` 的抽象是 `Tool`（`name` / `description` /
`parameters` / `run`），场景不是 Tool：让场景实现 `parameters`、`run(tool_call)` 只是为了让
"注册表"能装它，会把两套语义搅在一起（`#288` 那条"为复用而长出的假抽象"是同类账）。
所以这里只有 30 行：`register` / `get` / `list`，**重复拒绝**（照抄 `ToolRegistry.register`
的 fail-closed 形状：注册阶段就抛，不留"后注册的悄悄覆盖前一个"这种静默面）。

## 注册是显式的，不在 import 时发生

`builtin.py::register_builtin_scenarios()` 显式调用；不做 import 副作用 —— 否则
"某个测试 import 了 registry，内置场景就出现在生产列表里"，`--list` 的输出会随 import 图漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from evaluation.live_gate.schema import AssertionResult


@dataclass
class ScenarioContext:
    """交给场景的一次尝试语境：工作区已建好、session 目录已就位，场景只管跑。"""

    settings: Any
    sandbox: Any  # agent_harness.sandbox.base.Sandbox（鸭子类型，测试可传替身）
    session_root: Path
    session_id: str
    attempt_index: int
    #: 验证用的受控失败标记（空 = 无注入）。场景可以选择据此制造失败；**不得**据此产出"通过"。
    injected_failure: str = ""

    @property
    def sandbox_name(self) -> str:
        return type(self.sandbox).__name__


@dataclass
class AttemptOutcome:
    """一次尝试的结果面。runner 只消费这些字段，不关心场景内部怎么组织（FREE）。"""

    ok: bool
    session_id: str = ""
    run_id: str = ""
    run_status: str = ""
    steps: int = 0
    tool_calls: list[str] = field(default_factory=list)
    assertions: list[AssertionResult] = field(default_factory=list)
    error: str = ""
    event_count: int = 0
    output_tail: str = ""


@runtime_checkable
class LiveScenario(Protocol):
    """场景协议。后续票（预算 / 暂停 / 卡死 / deadline / 委派树）按这个面接进来。"""

    id: str
    version: int
    description: str

    async def prepare(self, ctx: ScenarioContext) -> list[str]:
        """在隔离工作区里准备场景（seed 数据 / 自查前置）。

        返回**未满足的前置**清单：非空 ⇒ 本次 Gate 判 `BLOCKED`（不发模型请求），
        不判 `FAIL` —— "环境缺 git"与"实现有 bug"是两件必须分开写清的事。
        """
        ...

    async def run(self, ctx: ScenarioContext) -> AttemptOutcome:
        """跑一次。**任何异常都必须自己转成 `ok=False`**（runner 另有兜底捕获）。"""
        ...


_SCENARIOS: dict[str, LiveScenario] = {}


def register_scenario(scenario: LiveScenario) -> LiveScenario:
    """注册一个场景。id 重复直接抛（拒绝静默覆盖，照 `ToolRegistry.register`）。"""
    if not getattr(scenario, "id", ""):
        raise ValueError("场景必须有非空 id")
    if scenario.id in _SCENARIOS:
        raise ValueError(f"场景 id '{scenario.id}' 已注册，拒绝覆盖")
    _SCENARIOS[scenario.id] = scenario
    return scenario


def get_scenario(scenario_id: str) -> LiveScenario:
    if scenario_id not in _SCENARIOS:
        raise KeyError(f"未知场景 '{scenario_id}'；已注册：{sorted(_SCENARIOS)}")
    return _SCENARIOS[scenario_id]


def list_scenarios() -> list[LiveScenario]:
    return [_SCENARIOS[key] for key in sorted(_SCENARIOS)]


def unregister_scenario(scenario_id: str) -> None:
    """只给测试用（注册表是进程级单例，测试之间要能互不干扰）。"""
    _SCENARIOS.pop(scenario_id, None)
