"""Builtin Capability factories（ADR-0010 Q5）：装配期 provider 白名单。

#1353 §5：Memory 的 V1 provider 组合（`MemoryComponents`：sqlite + 向量库 +
outbox + langmem）已整体删除——记忆权威面换成 markdown 文件 + SQLite FTS5，
不再有"每个 provider 自带一套向量存储"的边界（ADR-0024 在记忆域退役）。
现在 memory 只剩**一个实现**，`provider` 配置值因此退化为名字：

- 保留本模块是为了让 `_known_providers("memory")` 仍有一个事实源，装配期配置写错
  照旧响亮失败（不静默接受未知名字）；
- `langmem` 是历史别名，仍被接受（既有 `.env` 里写着它），语义与 `builtin` 相同。
"""

from __future__ import annotations

#: memory 的 provider 白名单：名字 → 同一个（唯一的）markdown 记忆实现。
_MEMORY_PROVIDER_NAMES: frozenset[str] = frozenset({"builtin", "langmem"})


def memory_provider_names() -> set[str]:
    """本模块真正能构造的 memory provider 名（装配期白名单的唯一事实源）。"""
    return set(_MEMORY_PROVIDER_NAMES)
