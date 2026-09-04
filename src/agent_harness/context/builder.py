"""Session 事件投影到 Runtime Context 的单一入口。"""

import logging
from typing import Any

from langchain_core.messages import AnyMessage

from agent_harness.context.compactor import ContextCompactor, ContextWindowExceededError
from agent_harness.context.provider import ContextProvider
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.session import Session
from agent_harness.session.event import CONTEXT_COMPACTED

logger = logging.getLogger("agent_harness.context")

__all__ = ["ContextBuilder", "ContextWindowExceededError"]


class ContextBuilder:
    """按预算压缩投影；不修改历史，Provider 选择留 Phase 6。"""

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

    async def build(self, session: Session) -> list[AnyMessage]:
        """不修改历史；估算包含 tool_calls 等结构字段的投影 token 数。"""
        messages = session.derive_messages()
        token_estimate = estimate_message_tokens(messages)
        logger.debug(
            "Context projection token estimate: %s", token_estimate,
            extra={"session_id": session.session_id, "token_estimate": token_estimate},
        )
        if token_estimate <= self.max_context_tokens * self.auto_compact_threshold:
            return messages
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
        return result.messages
