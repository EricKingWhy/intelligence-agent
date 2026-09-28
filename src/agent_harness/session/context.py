"""Session 层的请求上下文 ContextVar（跨层传 run 归因，不改变 Provider 协议）。

为什么放 session 层：memory / context provider 比 agent 层低，不能反向 import
agent；它们只需要"当前 run 是谁"。runtime 在 begin_run 之后设置，可选消费方
（如 MEMORY_DEGRADED 的 run_id 归因）读取；不读则零影响。
"""

from __future__ import annotations

from contextvars import ContextVar

#: 当前 run 的 id（begin_run 之后有效）；None = 不在 run 上下文中。
run_context_var: ContextVar[str | None] = ContextVar("run_context", default=None)

#: 本 run 已作为自动注入进模型上下文的记忆 entry id（#202 / ADR-0031 D4）。
#: provider 在 `select()` 把实际被拼接进 SystemMessage 的 id 写入（frozenset
#: 写时替换，不共享可变集合）；runtime 在 run 开始设空集合、收尾 reset。
#: `retrieve_memory` 读它打 `injected` 标；无 run 上下文时默认空集合（全 false，
#: 不抛异常）。刻意不落 SessionEvent（不变量 #4：事件是运行事实非诊断明细）。
memory_injected_ids_var: ContextVar[frozenset[str]] = ContextVar(
    "memory_injected_ids", default=frozenset(),
)
