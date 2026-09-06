"""Langfuse 旁路观测（ADR-0018）：``Degradation.OPTIONAL_OBSERVABILITY`` 档首个实现。

对外只暴露 :class:`LangfuseSink`——所有方法故障隔离，调用方零感知。
装配入口（runtime / CLI / web）用 ``Settings`` 门控：key 空 = disabled sink。
"""

from __future__ import annotations

from typing import Any

from agent_harness.observability.sink import LangfuseSink
from agent_harness.observability.tracer import RunTracer

__all__ = ["LangfuseSink", "RunTracer", "get_observability_sink"]

_process_sink: LangfuseSink | None = None


def get_observability_sink(settings: Any) -> LangfuseSink:
    """进程级 sink 单例（ADR-0018 D2 装配）：key 空 = disabled sink（零开销缺席）。

    Disabled sink 的全部方法仍是安全 no-op，调用方无需判空。
    """
    global _process_sink
    if _process_sink is None:
        _process_sink = LangfuseSink(
            public_key=settings.langfuse_public_key.get_secret_value(),
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url,
            trace_content=settings.langfuse_trace_content,
        )
    return _process_sink
