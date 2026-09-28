"""观测端口的测试夹具：记录型 NullTracer（#249 / #250 共用的 seam）。

放共享模块而不是某一个测试文件里：Runtime 侧与 Executor 侧都要用它，且两个
测试文件互相 import 会成环（`test_tracer_port` 已经从 `test_tool_tracing`
借夹具）。
"""

from __future__ import annotations

from typing import Any

from agent_harness.observability.port import NullTracer


class RecordingNullTracer:
    """记录端口调用名，行为委托 NullTracer（证明"谁被调用"，零外部副作用）。

    只记录**方法**调用：`trace_id` / `trace_url` 是数据成员，读取不进记录
    （它们本就不在"被驱动"的断言面内）。
    """

    trace_id: str | None = None
    trace_url: str | None = None

    def __init__(self, calls: list[str] | None = None) -> None:
        self._calls = calls if calls is not None else []
        self._inner = NullTracer()

    @property
    def calls(self) -> list[str]:
        return self._calls

    def __getattr__(self, name: str) -> Any:
        def _record(*args: Any, **kwargs: Any) -> Any:
            self._calls.append(name)
            return getattr(self._inner, name)(*args, **kwargs)

        return _record
