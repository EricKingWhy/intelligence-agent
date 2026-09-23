"""runtime 拥有的委派配额：一次 spawn 之后「谁还能再委派、还能再往下几层」。

#286：`AgentSpec.max_depth` 此前只在 `__post_init__` 里校验，**没有任何运行期
消费者**——所以「child 重新拿到 `delegate`」的逃逸通道一直敞着：
`AgentFactory.create` 省略 `grantable` 时默认「可授予 = source registry 全量」，
而 `InProcessSubagentProvider` 正好省略它。本模块把「剩余深度」变成**运行期
拥有**的事实，于是可授予集合有了确定性来源：

- 根配额来自**装配点**（`InProcessSubagentProvider.activate(max_depth=…)`），
  不是 child spec 的自述；
- 每 spawn 一次减 1，并取 `min(parent_remaining - 1, child_spec.max_depth)`
  ——child 的自述只能收窄，不能抬高；
- 剩余 0 时 `delegate` 从可授予集合里摘掉 ⇒ child 申请它 = 工厂显式拒绝。

## 为什么是 ContextVar，而不是把深度绑在工具实例上

child registry 是 `ToolRegistry.filtered()` 派生的**新实例**，但里面的工具对象
与父**同一身份**（`delegate` 尤其如此）。把「当前深度」挂在工具实例属性上，会让
同一个 `DelegateTool` 在多棵并行子树上互相踩。ContextVar 的作用域天然是「当前
任务及其派生任务」，与「一条 spawn 链」同形——asyncio 的 `Task` / `gather` 都会
`copy_context()`，所以兄弟子树互不可见，而子代理自己的 `run()` 看见的是它自己的
配额。

生命周期不变量：`bind_scope` 的 set/reset 在同一上下文内成对出现（`try/finally`），
绑定的区间严格包住那一次 `run()`。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_harness.agent.profiles import AgentSpec
    from agent_harness.tooling import ToolRegistry

#: 编排类（dispatch）工具名：这些工具**存在**本身就是「还能再委派」这项能力，
#: 所以剩余深度为 0 时必须从可授予集合里摘掉。将来出现第二个编排工具，加进这里。
DISPATCH_TOOL_NAMES = frozenset({"delegate"})


@dataclass(frozen=True)
class SpawnScope:
    """当前层的配额快照：这一层手里有什么工具 + 还能再往下走几层。"""

    registry: ToolRegistry
    remaining: int


_scope: ContextVar[SpawnScope | None] = ContextVar(
    "agent_harness_multiagent_spawn_scope", default=None,
)


def current_scope() -> SpawnScope | None:
    """当前 spawn 作用域的配额；`None` = 根（还没有人往下走过）。"""
    return _scope.get()


@contextmanager
def bind_scope(scope: SpawnScope) -> Iterator[None]:
    """把 `scope` 绑成「当前任务及其派生任务」的配额作用域，退出时还原。"""
    token: Token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def child_allowance(parent: SpawnScope, spec: AgentSpec) -> int:
    """有效子配额 = `min(parent.remaining - 1, spec.max_depth)`。

    `spec.max_depth` 只能**收窄**，不能抬高 runtime 的剩余额度（#286 冻结语义 2）
    ——所以是 `min` 而不是覆盖。取 0 表示「这个 child 自己不能再委派」。
    """
    return min(parent.remaining - 1, spec.max_depth)


def grantable_names(scope: SpawnScope, allowance: int) -> frozenset[str]:
    """本层**可授予**的工具名：手里的都能下放，除深度用尽时的编排工具。

    全集刻意取「本层 registry 的实有工具」，而不是根 registry：child 的
    `tool_scope` 因此永远 ⊆ 父 grantable scope，**永不**从全量 registry 重建
    （#286 冻结语义 5）。深度用尽时摘掉 `DISPATCH_TOOL_NAMES`，于是越权申请
    会走 `AgentFactory` 的显式拒绝路径，而不是被静默剔除。
    """
    names = {tool.name for tool in scope.registry.list()}
    if allowance <= 0:
        names -= DISPATCH_TOOL_NAMES
    return frozenset(names)
