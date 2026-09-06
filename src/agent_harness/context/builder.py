"""Session 事件投影到 Runtime Context 的单一入口。"""

import logging
from typing import Any

from langchain_core.messages import AnyMessage, SystemMessage

from agent_harness.context.compactor import ContextCompactor, ContextWindowExceededError
from agent_harness.context.provider import ContextProvider
from agent_harness.context.tokens import estimate_message_tokens, estimate_tokens
from agent_harness.session import Session
from agent_harness.session.event import (
    CONTEXT_COMPACTED,
    MODEL_COMPLETED,
    TOOL_RESULT,
    USER_MESSAGE,
)

logger = logging.getLogger("agent_harness.context")

__all__ = ["ContextBuilder", "ContextWindowExceededError"]

#: 会投影成消息的事件类型——与 derive_messages 的投影集合一一对应
#: （每个此类事件恰好产出一条消息，顺序一致；dangling 合成注入是唯一例外，
#: 由 _estimate_tokens_cached 的计数守卫回退处理）。
_PROJECTING_EVENT_TYPES = frozenset({USER_MESSAGE, MODEL_COMPLETED, TOOL_RESULT})


class ContextBuilder:
    """按预算压缩投影与选择 Provider 内容，不修改历史。"""

    def __init__(
        self,
        model_provider: Any,
        *,
        max_context_tokens: int = 200_000,
        auto_compact_threshold: float = 0.70,
        hard_guard_threshold: float = 0.85,
        context_providers: list[ContextProvider] | None = None,
    ) -> None:
        if max_context_tokens <= 0 or not 0 < auto_compact_threshold <= hard_guard_threshold <= 1:
            raise ValueError("require positive budget and 0 < auto <= hard <= 1")
        self.model_provider = model_provider
        self.max_context_tokens = max_context_tokens
        self.auto_compact_threshold = auto_compact_threshold
        self.hard_guard_threshold = hard_guard_threshold
        self.context_providers = list(context_providers or [])
        # (session_id, seq) → 该事件投影消息的 token 成本。事件落盘后其投影
        # 消息内容终身不变，成本是常量——此前每步对全部历史重新 model_dump_json
        # + BPE 编码，剖析实证占循环开销 88%（O(N²)：40 步 run 纯开销 2.2s）。
        # memo 终身 = builder 终身 = runtime 终身 = 单会话，无需淘汰。
        self._token_memo: dict[tuple[str, int], int] = {}
        # 最近一次 build 的估算总量——测试观察口（生产路径走参数传递）。
        self._token_estimate_total: int = 0

    async def build(self, session: Session) -> list[AnyMessage]:
        """不修改历史；估算包含 tool_calls 等结构字段的投影 token 数。"""
        messages = session.derive_messages()
        token_estimate = self._estimate_tokens_cached(session, messages)
        logger.debug(
            "Context projection token estimate: %s", token_estimate,
            extra={"session_id": session.session_id, "token_estimate": token_estimate},
        )
        if token_estimate <= self.max_context_tokens * self.auto_compact_threshold:
            return await self._with_providers(session, messages, token_estimate)
        result = await ContextCompactor(
            self.model_provider, max_context_tokens=self.max_context_tokens,
            auto_compact_threshold=self.auto_compact_threshold,
            hard_guard_threshold=self.hard_guard_threshold,
        ).compact(messages, token_estimate)
        if result.compacted_turn_count:
            session.append(CONTEXT_COMPACTED, {
                "compacted_turn_count": result.compacted_turn_count,
                "summary_message_count": 1,
                "token_estimate": result.token_estimate,
                "fallback_used": result.fallback_used,
            })
        return await self._with_providers(session, result.messages, result.token_estimate)

    def _estimate_tokens_cached(
        self, session: Session, messages: list[AnyMessage],
    ) -> int:
        """增量 token 估算：每条投影消息终身只编码一次。

        derive_messages 对投影事件是一一映射（按序各产出一条消息）——
        唯一例外是 dangling 合成 ToolMessage 的块尾注入（事件数 ≠ 消息数），
        此时放弃增量假设整体重估（正确性优先；resume 已修复 dangling，
        运行内该路径罕见）。
        """
        projecting = [e for e in session.events
                      if e.type in _PROJECTING_EVENT_TYPES]
        if len(projecting) != len(messages):
            # 计数失配：合成注入等非常规形态。清掉本会话的 memo 整体重估
            #（下一轮恢复一一对应后重新增量起步）。
            sid = session.session_id
            self._token_memo = {
                key: cost for key, cost in self._token_memo.items() if key[0] != sid
            }
            self._token_estimate_total = estimate_message_tokens(messages)
            return self._token_estimate_total
        total = 0
        for event, message in zip(projecting, messages):
            key = (session.session_id, event.seq)
            cost = self._token_memo.get(key)
            if cost is None:
                cost = estimate_tokens(message.model_dump_json())
                self._token_memo[key] = cost
            total += cost
        self._token_estimate_total = total
        return total

    async def _with_providers(
        self, session: Session, messages: list[AnyMessage], token_estimate: int,
    ) -> list[AnyMessage]:
        remaining = int(self.max_context_tokens * self.hard_guard_threshold) - token_estimate
        selected: list[AnyMessage] = []
        for provider in self.context_providers:
            if remaining <= 0:
                break
            try:
                additions = await provider.select(session, remaining)
            except Exception:  # noqa: BLE001 — optional Provider failure cannot stop the loop.
                logger.warning("Context provider unavailable; continuing without its contribution")
                continue
            for message in additions:
                cost = estimate_message_tokens([message])
                if cost <= remaining:
                    selected.append(message)
                    remaining -= cost
        insertion = 0
        while insertion < len(messages) and isinstance(messages[insertion], SystemMessage):
            insertion += 1
        return messages[:insertion] + selected + messages[insertion:]
