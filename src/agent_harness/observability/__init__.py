"""Langfuse 旁路观测（ADR-0018）：``Degradation.OPTIONAL_OBSERVABILITY`` 档首个实现。

对外只暴露 :class:`LangfuseSink`——所有方法故障隔离，调用方零感知。
装配入口（runtime / CLI / web）用 ``Settings`` 门控：key 空 = disabled sink。
"""

from __future__ import annotations

from agent_harness.observability.sink import LangfuseSink

__all__ = ["LangfuseSink"]
