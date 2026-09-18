"""Tracer 端口（#249）：Core 与观测实现之间的最小契约。

为什么有它：观测是旁路，但"观测是否启用"曾以 ``tracer is None`` 的形状渗进
Runtime 控制流——每加一条运行路径都要记得判空与对称收尾，替换非 Langfuse
实现也只能伪装成 LangfuseSink。端口把"可选"收进实现选择：调用方永远拿到一个
对象（未配置观测时是 :class:`NullTracer`），调用点不判空。

边界（ADR-0018 D3 / 不变量 #21 不变）：
- 本模块只依赖标准库——不 import Langfuse，Core 侧只认协议；
- ``RunTracer`` 是 Langfuse adapter，结构上满足 ``Tracer``（不做继承耦合）；
- 句柄可能是 ``None``（adapter 在根观测缺席/降级时如实返回，不伪造）：两个实现
  都对 ``None`` 句柄安全 no-op，调用方把它原样传回即可，不必为句柄分叉；
- 实现**必须不抛**：观测故障绝不传染主流程。该保证由 Core 单点强制
  （``agent.runtime._GuardedTracer`` 包住选定实现），实现违约也不会改写 run 语义。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Span(Protocol):
    """观测句柄：由 tracer 产出、由调用方原样透传回 tracer。"""

    def update(self, **kwargs: Any) -> Any: ...

    def end(self, **kwargs: Any) -> Any: ...


@runtime_checkable
class Tracer(Protocol):
    """一次 run 的观测端口；方法集 = Runtime 与 ToolExecutor 当前调用的全部方法。

    实现必须不抛（见模块 docstring）。
    """

    #: 真实 trace 标识；观测缺席/降级时如实 None（绝不伪造）。
    trace_id: str | None
    #: 与 trace_id 并列的可点击 URL（同一降级模式）。
    trace_url: str | None

    def run_started(self) -> None: ...

    def model_call_started(
        self, *, step: int, messages: Any, model: str | None = None,
    ) -> Span | None: ...

    def model_call_completed(
        self, generation: Span | None, *, output_text: str,
        usage: dict[str, int] | None = None,
        duration_ms: int | None = None,
        finish_reason: str | None = None,
        provider_request_id: str | None = None,
        response_model: str | None = None,
        fallback_transitions: list[Any] | None = None,
        tool_call_names: list[str] | None = None,
    ) -> None: ...

    def model_call_failed(self, generation: Span | None, *, error_type: str) -> None: ...

    def run_completed(
        self, final_text: str, usage_total: dict[str, int] | None = None,
    ) -> None: ...

    def run_failed(self, reason: str) -> None: ...

    def tool_span_started(
        self, *, tool_name: str, tool_call_id: str, args: Any, is_delegate: bool,
    ) -> Span | None: ...

    def tool_span_completed(
        self, span: Span | None, *, outcome: str, message: str | None = None,
        attempts: list[dict[str, Any]] | None = None,
        session_id: str | None = None, extra: dict[str, Any] | None = None,
    ) -> None: ...

    def context_build_started(self, *, step: int) -> Span | None: ...

    def context_build_completed(
        self, span: Span | None, *, compacted_turn_count: int | None = None,
    ) -> None: ...


class NullSpan:
    """空句柄：任何 update/end 都是 no-op（观测缺席时由 NullTracer 产出）。"""

    def update(self, **kwargs: Any) -> None:
        return None

    def end(self, **kwargs: Any) -> None:
        return None


class NullTracer:
    """观测缺席时的端口实现：零外部副作用（无 sink、无网络、无磁盘），全部 no-op。

    调用方无需判空——它与 ``RunTracer`` 的差异只在"有没有东西被记录"。
    """

    trace_id: str | None = None
    trace_url: str | None = None

    def run_started(self) -> None:
        return None

    def model_call_started(
        self, *, step: int, messages: Any, model: str | None = None,
    ) -> Span:
        return NullSpan()

    def model_call_completed(
        self, generation: Span | None, *, output_text: str,
        usage: dict[str, int] | None = None,
        duration_ms: int | None = None,
        finish_reason: str | None = None,
        provider_request_id: str | None = None,
        response_model: str | None = None,
        fallback_transitions: list[Any] | None = None,
        tool_call_names: list[str] | None = None,
    ) -> None:
        return None

    def model_call_failed(self, generation: Span | None, *, error_type: str) -> None:
        return None

    def run_completed(
        self, final_text: str, usage_total: dict[str, int] | None = None,
    ) -> None:
        return None

    def run_failed(self, reason: str) -> None:
        return None

    def tool_span_started(
        self, *, tool_name: str, tool_call_id: str, args: Any, is_delegate: bool,
    ) -> Span:
        return NullSpan()

    def tool_span_completed(
        self, span: Span | None, *, outcome: str, message: str | None = None,
        attempts: list[dict[str, Any]] | None = None,
        session_id: str | None = None, extra: dict[str, Any] | None = None,
    ) -> None:
        return None

    def context_build_started(self, *, step: int) -> Span:
        return NullSpan()

    def context_build_completed(
        self, span: Span | None, *, compacted_turn_count: int | None = None,
    ) -> None:
        return None
