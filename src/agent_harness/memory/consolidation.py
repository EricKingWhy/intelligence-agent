"""#158 冲突消解的 provider 侧机件：交给 LLM 的既有记忆**有界**，且这次额外开销**可见**。

为什么单独一个模块：这里定义的是"provider 检索回来的记忆以什么形状进 prompt"——既不属于记忆契约
（`capability.py`），也不属于 LangMem 能力的实现细节（`langmem_capability.py`）。收在一处，
"注入 prompt 的东西有上界"这条性质才只有一个定义点。

与 `extractor._clip_events` 同一思路（#158 AC4 点名的先例）：单条上限与那里单事件的 1000 字符同数；
条数上界（`CONSOLIDATION_QUERY_LIMIT`）在本模块真正执行，`query_limit` 只是同时告诉上游"别取太多"。
"""

import copy
from dataclasses import dataclass
from typing import Any

#: 单条注入 prompt 的记忆正文上限（字符）。与 `extractor._clip_events` 的单事件上限同数。
INJECTED_MEMORY_CHAR_LIMIT = 1000

#: provider 检索既有记忆的条数上界（LangMem 的 `query_limit`）。
CONSOLIDATION_QUERY_LIMIT = 5

#: 单次"检索后决策"的默认时间预算（秒）。实测真模型 3–26s（#158 探针 D/E 与真机 gate），
#: 所以沿用旧的 15s 会把相当一部分决策判成超时；超时**不再丢写**——`consolidate` 会降级为
#: 无条件写入并如实报告。
#:
#: 它是**默认值**而非硬上限：writeback 会按"外层预算的剩余时间"给每次调用一个更小的
#: `budget_seconds`（见 `writeback._FALLBACK_RESERVE_SECONDS`）。这样多个候选时，靠后的候选
#: 会**降级成只新增**，而不是被外层取消连写都没写（CancelledError 越过降级边界）。
#: 必须小于 writeback 的运行级预算（`writeback.WRITEBACK_TIMEOUT_SECONDS`），这条顺序由
#: `test_consolidation_budget_nests_inside_writeback` 钉住。
CONSOLIDATION_TIMEOUT_SECONDS = 30.0

#: 截断标记：让模型知道"这条记忆还有后半段"，而不是把截断当成完整内容去 Compare & Update。
TRUNCATION_MARKER = "…[已截断]"

#: 降级原因前缀（`MemoryWriteOutcome.degraded_reason`）。只带阶段 + 异常类型名，不带异常消息。
CONSOLIDATION_DEGRADED_PREFIX = "consolidation_failed"


@dataclass
class ConsolidationStats:
    """一次写入里 provider 侧检索的观测面（#158 AC3）：检索几次、取回几条、截断/丢弃几条。"""

    searches: int = 0
    retrieved_items: int = 0
    truncated_items: int = 0
    #: 超过条数上界被**丢掉**的条数。真机 gate 发现：`query_limit` 只让上游在**事后**裁剪
    #: 结果集，交给 manager 的结果可能更多（实测一次检索回来 6 条、上限 5），所以条数上界
    #: 必须在这里真正执行，否则 AC4 的"条数有界"依赖上游内部实现。
    dropped_items: int = 0


class BoundedSearchStore:
    """包住 provider 用的 BaseStore：**检索结果有界 + 计数**，其余原样透传。

    只覆盖 `asearch`——它是"既有记忆进 prompt"的唯一入口；写/读/删经 `__getattr__` 透传，
    保证"决策结果落 #156 的机制"这条路径不会因为包了一层而变形（`aput`/`adelete` 仍是
    原对象，namespace 授权与归属校验一点没少）。

    两个上界都在这里执行：**条数**（`max_items`）与**每条字符数**（`max_chars`）。

    形状不认识的值（不是 `{"content": {...}}` 这种 MemoryPayload）**原样放过**：不认识不等于
    可以改——猜错的截断会静默改坏 provider 看到的内容。
    """

    def __init__(self, inner: Any, *, max_chars: int = INJECTED_MEMORY_CHAR_LIMIT,
                 max_items: int = CONSOLIDATION_QUERY_LIMIT) -> None:
        self._inner = inner
        self._max_chars = max_chars
        self._max_items = max_items
        self.stats = ConsolidationStats()

    async def asearch(self, namespace_prefix: tuple[str, ...], /, *, query: str | None = None,
                      filter: dict | None = None, limit: int = 10,
                      offset: int = 0, **kwargs: Any) -> list:
        items = await self._inner.asearch(namespace_prefix, query=query, filter=filter,
                                         limit=limit, offset=offset, **kwargs)
        self.stats.searches += 1
        self.stats.retrieved_items += len(items)
        if len(items) > self._max_items:
            self.stats.dropped_items += len(items) - self._max_items
            items = items[:self._max_items]
        return [self._bounded(item) for item in items]

    def _bounded(self, item: Any) -> Any:
        value = getattr(item, "value", None)
        payload = value.get("content") if isinstance(value, dict) else None
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str) or len(content) <= self._max_chars:
            return item
        self.stats.truncated_items += 1
        # 浅拷贝 + 换掉 `value`（新 dict，原对象与其嵌套 dict 都不动）：保留**具体类型**
        # （`SearchItem` 的 `score`、子类等）而不 import langgraph —— 本模块由此不越
        # 不变量 #20 的 seam（`tests/orchestration/test_seam.py` 全 src 树扫描）。
        bounded = copy.copy(item)
        bounded.value = {**value, "content": {**payload, "content": content[:self._max_chars] + TRUNCATION_MARKER}}
        return bounded

    def __getattr__(self, name: str) -> Any:
        # aput / adelete / aget / abatch…：原样透传给真 store。
        return getattr(self._inner, name)
