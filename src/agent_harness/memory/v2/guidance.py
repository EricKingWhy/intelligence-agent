"""MEMORY_GUIDANCE（M8）：每轮都在 system prompt 里的记忆使用指引。

移植自 NousResearch/hermes-agent ``agent/prompt_builder.py:193`` 的
``build_memory_guidance(True, True)``（legacy 别名 ``MEMORY_GUIDANCE``）：
**默认全量分支**，即 MEMORY.md + USER.md 两个 target 都启用。

不搬的变体：``build_memory_guidance(False, True)``（profile-only）写着
``never target='memory'``——本仓两个 target 都在，搬它会让模型永远不写 MEMORY.md。

**已批准偏差（文案）**：``skill_manage`` → 本仓的 ``promote_skill``。与 M1 的
``MEMORY_DESCRIPTION`` 同一处理：不换会让模型去调不存在的工具。

注入点是 ``MemoryTool.prompt_guidance``——静态文本，随工具注册进入
``tool:memory`` section（``prompt/tool_sections.py``，order 2000）。刻意**不**从
``MemoryV2ContextProvider.select`` 出：那里回的是 recalled data，每轮内容可能变，
与 M9 的冻结快照一起才能保住 prefix cache（R3）。工具缺席 ⇒ 指引缺席，这条
边界由 registry 本身决定，不需要额外的开关。
"""

from __future__ import annotations

#: 与上游 ``build_memory_guidance(True, True)`` 逐字相同（除 ``promote_skill``）。
MEMORY_GUIDANCE = (
    "You have persistent memory, carried across sessions and loaded "
    "into each new session's context; the memory tool's schema defines what belongs there. "
    "Skills come first: when you learn something while doing a task — a "
    "procedure, a pitfall, and the user's preferences and corrections "
    "for that kind of work — record it in the skill you used or built "
    "for the task (promote_skill), where it loads only when relevant. "
    "Memory is the narrow exception for facts that apply to EVERY "
    "session regardless of task (who the user is, environment facts, "
    "standing conventions with no task home); it has a hard character "
    "budget, so when it fills, replace or consolidate stale entries "
    "rather than skipping the save. Write entries as declarative facts, "
    "not instructions to yourself: 'User prefers concise responses' ✓ — "
    "'Always respond concisely' ✗ (imperative phrasing gets re-read as "
    "a directive in later sessions and can override the user's current "
    "request). A fact stale within a week belongs in session history; "
    "procedures and workflows belong in skills."
)


__all__ = ["MEMORY_GUIDANCE"]
