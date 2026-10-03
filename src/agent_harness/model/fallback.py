"""Model Fallback（ADR-0014 决策 14/15/16）：瞬时故障的两级模型转移。

不变量 #9：Model Fallback 与 Tool Retry 分离——本模块只关心模型调用故障，
绝不触碰工具重试语义（那是 ToolExecutor 的单一责任域）。

分层：
- is_transient_model_error：共享错误分类 helper（决策 15）——瞬时 =
  超时 / 5xx / 429 / 连接失败；认证错 / 参数错等非瞬时换一台模型也没用，
  直接失败不盲切（决策 1）。policy 复用不重写。
- FallbackPolicy：决策 seam（决策 14）。V1 契约只问「这个错误是否值得
  切换」；两级结构（primary → fallback、never 切回、只重试一次）由
  ModelFallbackCoordinator 持有。未来升级全链策略（role/wildcard 多级 +
  cooldown 切回）只换 policy 实现，Agent Loop 零改动。
- ModelFallbackCoordinator：执行调用序列（决策 16「决策在 policy，
  编排在模型层」）。Runtime 拥有 Session，因此切换事实以 FallbackTransition
  返回（drain 语义），由 Runtime 持久化为 model/fallback 事件——白盒透明。

脱敏：FallbackTransition.reason 只带异常类型名，不带异常消息——Provider
回显可能含敏感文本（与 model/failed 事件的脱敏不变量一致）。

`#313`（T5）：本模块**额外**记下每一次**实际**发出去的 Provider 请求
（`ModelRequestAttempt`），供 Runtime 落 `model/request`（计数点定义见 `02 §5.1`）。
放在这一层的原因只有一条：**"这次调用实际发了几次请求"只有编排层知道**——primary
失败后那次 fallback 重试是同一次决策里的第二次请求。与 transitions 同一姿势，本层
只记录事实，落盘与镜像由持有 Session 的 Runtime 负责。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

import httpx
from langchain_core.messages import AnyMessage

from agent_harness.model.accounting import (
    PROVIDER_ROLE_FALLBACK,
    PROVIDER_ROLE_PRIMARY,
    REQUEST_OUTCOME_COMPLETED,
    REQUEST_OUTCOME_FAILED,
)
from agent_harness.model.concurrency import ModelCallGate
from agent_harness.model.stall import ModelStallError, stream_with_stall_guard

# openai SDK 的瞬时错误类名（不硬 import openai：按类名识别，避免版本
# 差异与 LangChain 包装层变化破坏分类）。
_TRANSIENT_ERROR_NAMES = frozenset({
    "APITimeoutError",       # openai 请求超时
    "APIConnectionError",    # openai 连接失败
    "TimeoutError",          # 内建 / asyncio 超时（3.11+ 同一类）
    "ConnectionError",       # 内建 socket 连接失败
})


def _chunk_text_length(chunk: Any) -> int:
    """chunk.content 的字符数（str 直通；非 str 记 0）——attempt 边界标记用。

    不做内容抽取（那是 Runtime 的 `_extract_text` 责任）：这里只要一个"primary
    切换前产出了多少"的**数量**，多算少算都不改正文，只影响边界提示的粒度。
    """
    content = getattr(chunk, "content", "")
    return len(content) if isinstance(content, str) else 0


def _chunk_finish_reason(chunk: Any) -> str | None:
    """chunk 自报的 finish_reason（缺失 / 非 str 记 None，不臆断）。

    与 Runtime 的 `_finish_reason_from_response` 同口径，只是作用在**单个 chunk**
    上：用来判"primary 在切换前是否已经给出收尾信号"（#551 M10-3）。
    """
    meta = getattr(chunk, "response_metadata", None) or {}
    value = meta.get("finish_reason")
    return value if isinstance(value, str) and value else None


def is_transient_model_error(error: BaseException) -> bool:
    """判断模型调用错误是否瞬时（值得切 fallback 重试）。

    覆盖四层异常源：
    - 卡流看门狗：ModelStallError（超时形状，冒烟实测的 10 分钟缓速流）；
    - httpx 传输层：TimeoutException / TransportError（连接被拒、读失败等）；
    - httpx.HTTPStatusError：5xx / 429 瞬时，4xx 认证/参数错非瞬时；
    - openai SDK 风格：带 status_code 属性的按状态码判，APITimeoutError /
      APIConnectionError 按类名判（langchain-openai 的真实异常源）。
    """
    if isinstance(error, ModelStallError):
        return True
    # httpx.HTTPStatusError 与 TransportError 是兄弟分支（同出 HTTPError），
    # 先判状态错再判传输错，语义各自独立。
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return status >= 500 or status == 429
    if isinstance(error, httpx.TransportError):
        # 含 TimeoutException（其子类）与 ConnectError / ReadError 等传输故障
        return True
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        return status_code >= 500 or status_code == 429
    return type(error).__name__ in _TRANSIENT_ERROR_NAMES


@dataclass(frozen=True)
class FallbackTransition:
    """一次成功的模型切换事实（Runtime 持久化为 model/fallback 事件）。

    reason 只带异常类型名不带消息——异常消息可能含 Provider 回显的敏感
    文本，与 model/failed 事件同一脱敏不变量。

    `primary_content_chars` / `primary_finish_reason` 是 **attempt 边界**事实
    （#551 M10-3）：切到 fallback 之前 primary 已产出的文本量与它自报的
    finish_reason。它们只带数量/枚举、不带任何正文，用来让下游区分"fallback
    在续写 primary 的前缀"与"primary 其实已经答完，fallback 只是重答了一遍"
    ——后者是旧 docstring 假设失效的形状，静默拼接会给出重复答案。
    """

    from_model: str
    to_model: str
    reason: str
    primary_content_chars: int = 0
    primary_finish_reason: str | None = None

    def event_data(self) -> dict[str, Any]:
        """model/fallback 事件的共用载荷（成功路径与终态路径同源）。

        边界字段只在**非平凡**时落键（0 / None 省略）：primary 未产出内容时形状
        与旧事件逐字一致，不给既有基线和消费者造无意义的 churn。
        """
        data: dict[str, Any] = {
            "from_model": self.from_model,
            "to_model": self.to_model,
            "reason": self.reason,
        }
        if self.primary_content_chars:
            data["primary_content_chars"] = self.primary_content_chars
        if self.primary_finish_reason:
            data["primary_finish_reason"] = self.primary_finish_reason
        return data


@dataclass(frozen=True)
class ModelRequestAttempt:
    """一次**实际**发出去的 Provider 请求（`#313`：`model_requests` 的一格）。

    三件只有这一层知道的事（其余规则见 `02 §5.1`，本处不复述）：

    - `role`：primary / fallback。closeout 那一次由 Runtime 的收口调用点自己记
      ——那条路径**绕过**本协调器（见 `_closeout_continuation`）。
    - `outcome`：拿到响应（completed）或没拿到（failed：瞬时故障、非瞬时错误、
      取消、断连）。没拿到的**照样算一次请求**：它真的发出去了。
    - usage / cost **不在这里**：那是**响应**的属性，而流式路径的响应由 Runtime
      聚合（本层只见 chunk），落盘见 `agent/runtime.py::_append_model_request`。
    """

    role: str
    outcome: str
    request_id: str | None = None


@runtime_checkable
class FallbackPolicy(Protocol):
    """fallback 决策 seam（ADR-0014 决策 14）。

    V1 只问「这个错误是否值得切换」；两级结构与 never 切回由 coordinator
    持有。未来升级 oh-my-pi 全链（role/wildcard 多级、cooldown 切回）时
    扩展为携带链状态的签名，Agent Loop 与本 Protocol 的消费方不变。
    """

    def should_fallback(self, error: BaseException) -> bool: ...


class TwoLevelFallbackPolicy:
    """默认策略：瞬时故障切换，非瞬时（认证/参数错）直接失败（决策 1/15）。"""

    def should_fallback(self, error: BaseException) -> bool:
        return is_transient_model_error(error)


class ModelFallbackCoordinator:
    """两级模型调用编排：primary 失败 → policy 放行 → 切 fallback 重试一次。

    - 决策在 FallbackPolicy（瞬时性判断）；结构在本类（两级、never 切回、
      只重试一次——fallback 再失败异常上抛，由 Runtime 统一失败兜底）。
    - 每实例绑定一次 run 的调用序列：切到 fallback 后 self.current 永久
      指向 fallback（never 切回，决策 14），后续调用不再碰 primary。
    - 并发安全：Runtime 每个 run 新建一个 coordinator（同 T1 的 guard
      姿势），切换状态不跨 run 共享。
    - transitions 是 drain 语义：Runtime 在每次模型调用完成后取走并持久化
      为 model/fallback 事件（Runtime 拥有 Session，模型层不持有会话）。
    """

    def __init__(
        self,
        *,
        primary: Any,
        fallback: Any | None = None,
        policy: FallbackPolicy | None = None,
        primary_name: str = "primary",
        fallback_name: str = "fallback",
        idle_timeout: float = 0.0,
        total_timeout: float = 0.0,
        gate: ModelCallGate | None = None,
    ) -> None:
        self._policy = policy or TwoLevelFallbackPolicy()
        self._fallback = fallback
        self._primary_name = primary_name
        self._fallback_name = fallback_name
        # 流式守卫（秒，逐项 ≤0 关闭）：idle = N 秒无新 chunk（死连接）；
        # total = 整条流必须 N 秒内完成（慢滴漏，冒烟 10 分钟场景）。
        # 每次流式尝试（含 fallback 重试）都被包住，超时抛 ModelStallError
        # （瞬时）→ fallback 接管。
        self._idle_timeout = idle_timeout
        self._total_timeout = total_timeout
        # 进程级模型并发闸（#89）：共享实例（assembly 创建、parent/child 传递
        # 同一引用）。闸包在 stall 看门狗【外面】——排队等槽位不计入 idle。
        self._gate = gate
        self.current = primary
        self._transitions: list[FallbackTransition] = []
        self._requests: list[ModelRequestAttempt] = []
        #: 同一决策里 primary 与 fallback **都失败**时记下 (primary类型名, fallback
        #: 类型名)；只在那一刻设置，异常上抛后 Run 随即终结，故不会被后续步骤污染。
        self._double_failure: tuple[str, str] | None = None

    def _guarded_stream(self, model: Any, messages: list[AnyMessage]) -> AsyncIterator[Any]:
        """单次流式尝试按需包双守卫；调用方已持有并发槽位。"""
        stream = model.astream(messages)
        if (self._idle_timeout and self._idle_timeout > 0) or (
            self._total_timeout and self._total_timeout > 0
        ):
            stream = stream_with_stall_guard(
                stream, idle_timeout=self._idle_timeout,
                total_timeout=self._total_timeout,
            )
        return stream

    def _slot(self) -> AbstractAsyncContextManager[None]:
        """取一个**新**的并发槽位——每次尝试都必须新取一个。

        `ModelCallGate.slot()` 是 `@asynccontextmanager` 产物，**一次性**：退出时
        contextlib 会 `del self.args`，复用同一个 CM 二次进入抛 `AttributeError`
        （2026-09-11 真实事故：非流式 run 回退重试必崩；回归锁见
        `tests/test_model_fallback.py::TestCoordinatorAinvoke::
        test_ainvoke_fallback_retry_reacquires_concurrency_slot`）。
        """
        return self._gate.slot() if self._gate is not None else nullcontext(None)

    async def ainvoke(
        self, messages: list[AnyMessage], *,
        on_request_started: Callable[[str, str], None] | None = None,
    ) -> Any:
        """非流式调用：primary 瞬时失败 → 切 fallback 重试一次。

        V1 看门狗不覆盖 ainvoke（总时限会误杀合法长推理）——socket 级
        read-timeout 仍是底线，见 model/stall.py 模块 docstring。
        并发闸同样生效（非流式调用占一个槽位，两次尝试各占一次）。

        每次尝试**恰记一格** `ModelRequestAttempt`（`#313`）：成功与失败都记，
        取消 / 断连（`BaseException`，如 `CancelledError`）也记——请求发出去了就
        发生过，只是没拿到响应。记录走 `except BaseException` 而不是
        `except Exception`：后者会让取消/断连这类"请求已发出但未完成"从账上消失。
        """
        role = self._current_role()
        request_id: str | None = None
        try:
            async with self._slot():
                request_id = self._start_request(role, on_request_started)
                result = await self.current.ainvoke(messages)
        except BaseException as error:
            if request_id is None:
                raise
            self._record_request(role, REQUEST_OUTCOME_FAILED, request_id)
            if not isinstance(error, Exception) or not self._try_switch(error):
                raise
            try:
                return await self._ainvoke_once(messages, on_request_started)
            except BaseException as retry_error:
                if isinstance(retry_error, Exception):
                    self._double_failure = (
                        type(error).__name__, type(retry_error).__name__,
                    )
                raise
        self._record_request(role, REQUEST_OUTCOME_COMPLETED, request_id)
        return result

    async def _ainvoke_once(
        self, messages: list[AnyMessage],
        on_request_started: Callable[[str, str], None] | None,
    ) -> Any:
        """切换后的那一次重试（**不再**切换：never 切回、只重试一次）。"""
        role = self._current_role()
        request_id: str | None = None
        try:
            async with self._slot():
                request_id = self._start_request(role, on_request_started)
                result = await self.current.ainvoke(messages)
        except BaseException:
            if request_id is None:
                raise
            self._record_request(role, REQUEST_OUTCOME_FAILED, request_id)
            raise
        self._record_request(role, REQUEST_OUTCOME_COMPLETED, request_id)
        return result

    async def astream(
        self, messages: list[AnyMessage], *,
        on_request_started: Callable[[str, str], None] | None = None,
    ) -> AsyncIterator[Any]:
        """流式调用：流中途瞬时失败（含卡流）→ 切 fallback 继续产出。

        已产出的 chunk 由消费者聚合（前缀 + fallback 续写）——SSE 客户端
        看到的是一段连续流；完整聚合结果由 model/completed 持久化。

        切换事实带上 attempt 边界（#551 M10-3）：primary 切走前产出的字符数与
        它自报的 finish_reason 记进 FallbackTransition，让下游能区分"续写前缀"
        与"primary 已答完、fallback 重答"——不删内容、不去重。

        记账同 `ainvoke`：每次尝试恰一格，且**流被半途关闭**（消费者断连 →
        `GeneratorExit`）也算一次失败的请求——那一格在第一段 `except
        BaseException` 里记，`yield` 型生成器关闭时同样会走到。
        """
        role = self._current_role()
        primary_content_chars = 0
        primary_finish_reason: str | None = None
        request_id: str | None = None
        try:
            async with self._slot():
                request_id = self._start_request(role, on_request_started)
                async for chunk in self._guarded_stream(self.current, messages):
                    primary_content_chars += _chunk_text_length(chunk)
                    finish = _chunk_finish_reason(chunk)
                    if finish is not None:
                        primary_finish_reason = finish
                    yield chunk
        except BaseException as error:
            if request_id is None:
                raise
            self._record_request(role, REQUEST_OUTCOME_FAILED, request_id)
            if not isinstance(error, Exception) or not self._try_switch(
                error,
                primary_content_chars=primary_content_chars,
                primary_finish_reason=primary_finish_reason,
            ):
                raise
            retry_role = self._current_role()
            retry_request_id: str | None = None
            try:
                async with self._slot():
                    retry_request_id = self._start_request(
                        retry_role, on_request_started,
                    )
                    async for chunk in self._guarded_stream(self.current, messages):
                        yield chunk
            except BaseException as retry_error:
                if retry_request_id is None:
                    raise
                self._record_request(
                    retry_role, REQUEST_OUTCOME_FAILED, retry_request_id,
                )
                # 只把"两级都真的失败"记成双挂；消费方断连（GeneratorExit /
                # CancelledError）走同一分支但**不是**模型级双挂。
                if isinstance(retry_error, Exception):
                    self._double_failure = (
                        type(error).__name__, type(retry_error).__name__,
                    )
                raise
            self._record_request(
                retry_role, REQUEST_OUTCOME_COMPLETED, retry_request_id,
            )
            return
        self._record_request(role, REQUEST_OUTCOME_COMPLETED, request_id)

    def drain_transitions(self) -> list[FallbackTransition]:
        """取走自上次 drain 以来的切换事实（Runtime 逐调用持久化用）。"""
        out = self._transitions
        self._transitions = []
        return out

    def drain_requests(self) -> list[ModelRequestAttempt]:
        """取走自上次 drain 以来的**实际请求**记录（Runtime 落 `model/request` 用）。

        drain 语义与 transitions 一致：同一次决策可能留下两格（primary 失败 +
        fallback 成功），Runtime 逐格落盘后据此镜像——被拒绝 / 传输失败的那一格
        也**在**（`02 §5.1`：失败请求占 `model_requests` 一席，但不增 `agent_turns`）。
        """
        out = self._requests
        self._requests = []
        return out

    @property
    def double_failure_errors(self) -> tuple[str, str] | None:
        """同一决策里 primary 与 fallback **都失败**时的 (primary, fallback) 类型名。

        只在那一刻有值；Run 的异常臂据此把两级错误类型名一并落进 `run/failed`
        （#551 M10-8：首因不得只存在于 model/fallback 事件里）。只带类型名、
        不带正文，脱敏边界与 FallbackTransition.reason 相同。
        """
        return self._double_failure

    def _current_role(self) -> str:
        """此刻的调用角色：`self.current` 指向 fallback 就是 fallback，否则 primary。

        按**身份**判（`is`）而不是按序号：`never 切回`由 `_try_switch` 保证，
        所以"当前是谁"就是"这次请求以谁的名义发出"。
        """
        if self._fallback is not None and self.current is self._fallback:
            return PROVIDER_ROLE_FALLBACK
        return PROVIDER_ROLE_PRIMARY

    @staticmethod
    def _start_request(
        role: str, callback: Callable[[str, str], None] | None,
    ) -> str:
        request_id = str(uuid4())
        if callback is not None:
            callback(role, request_id)
        return request_id

    def _record_request(self, role: str, outcome: str, request_id: str) -> None:
        self._requests.append(ModelRequestAttempt(
            role=role, outcome=outcome, request_id=request_id,
        ))

    def _try_switch(
        self, error: BaseException, *,
        primary_content_chars: int = 0, primary_finish_reason: str | None = None,
    ) -> bool:
        """错误值得切且还没切过 → 切换并记录事实；否则 False（上抛原异常）。

        `primary_content_chars` / `primary_finish_reason` 是切走前 primary 已产出的
        量与它自报的 finish_reason（#551 M10-3 的 attempt 边界事实）。
        """
        if self._fallback is None or self.current is self._fallback:
            return False
        if not self._policy.should_fallback(error):
            return False
        self._transitions.append(FallbackTransition(
            from_model=self._primary_name,
            to_model=self._fallback_name,
            reason=type(error).__name__,
            primary_content_chars=primary_content_chars,
            primary_finish_reason=primary_finish_reason,
        ))
        self.current = self._fallback
        return True
