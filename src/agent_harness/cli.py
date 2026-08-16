"""最小 Agent CLI：向模型发送一条消息并打印回复。

当前项目还没有 ToolExecutor 和 Checkpoint；日志只记录真实发生的最小链路，
不会为了看起来完整而伪造 tool_operation/checkpoint_saved/retry 事件。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Mapping
from time import perf_counter
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.config import Settings
from agent_harness.logging import (
    LogContext,
    log_context,
    log_event,
    new_span_id,
    setup_logging,
)
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model

logger = logging.getLogger("agent_harness")


async def run(message: str) -> str:
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    context = LogContext.create(service="agent-harness", env="local")

    task_started = perf_counter()
    task_span_id = new_span_id()
    agent_span_id = new_span_id()
    llm_span_id = new_span_id()

    with log_context(context):
        log_event(
            logger,
            "task_start",
            "收到用户任务",
            node_name="cli",
            step=0,
            span_id=task_span_id,
            outcome="started",
        )
        log_event(
            logger,
            "agent_start",
            "Agent 开始处理任务",
            node_name="agent",
            step=1,
            span_id=agent_span_id,
            parent_span_id=task_span_id,
            outcome="started",
        )

        try:
            config = ModelConfig.from_settings(settings)
            model = create_chat_model(config)
            llm_started = perf_counter()
            try:
                result = await model.ainvoke([HumanMessage(content=message)])
            except Exception as error:
                duration_ms = _elapsed_ms(llm_started)
                error_fields = _error_fields(error)
                log_event(
                    logger,
                    "llm_call",
                    "LLM 调用失败",
                    level="error",
                    node_name="model",
                    step=2,
                    span_id=llm_span_id,
                    parent_span_id=agent_span_id,
                    provider=config.provider,
                    model_id=config.model_name,
                    stream=False,
                    llm_input=message,
                    attempt=1,
                    max_attempts=1,
                    duration_ms=duration_ms,
                    outcome="failure",
                    **error_fields,
                )
                raise

            llm_fields = _llm_result_fields(result)
            log_event(
                logger,
                "llm_call",
                "LLM 调用完成",
                node_name="model",
                step=2,
                span_id=llm_span_id,
                parent_span_id=agent_span_id,
                provider=config.provider,
                model_id=config.model_name,
                stream=False,
                llm_input=message,
                llm_output=result.content,
                attempt=1,
                max_attempts=1,
                duration_ms=_elapsed_ms(llm_started),
                outcome="success",
                **llm_fields,
            )
            log_event(
                logger,
                "agent_decision",
                "模型已给出最终回复，Agent 决定结束任务",
                node_name="agent",
                step=3,
                span_id=new_span_id(),
                parent_span_id=agent_span_id,
                decision="finish",
                requested_tools=[],
                execution_mode="serial",
                remaining_steps=0,
                reason="当前最小流程收到最终文本回复，且没有待执行的 Tool Call",
                outcome="success",
            )
        except Exception as error:
            error_fields = _error_fields(error)
            log_event(
                logger,
                "error",
                "Agent 处理任务时发生错误",
                level="error",
                exc_info=True,
                node_name="agent",
                step=3,
                span_id=new_span_id(),
                parent_span_id=agent_span_id,
                retryable=False,
                attempt=1,
                max_attempts=1,
                outcome="failure",
                **error_fields,
            )
            log_event(
                logger,
                "task_failed",
                "任务执行失败",
                level="error",
                node_name="cli",
                step=4,
                span_id=task_span_id,
                duration_ms=_elapsed_ms(task_started),
                outcome="failure",
                **error_fields,
            )
            raise

        log_event(
            logger,
            "task_completed",
            "任务执行完成",
            node_name="cli",
            step=4,
            span_id=task_span_id,
            duration_ms=_elapsed_ms(task_started),
            outcome="success",
        )
        return str(result.content)


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 3)


def _error_fields(error: Exception) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "error_type": type(error).__name__,
        "error_message": str(error),
    }
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        fields["error_code"] = str(status_code)
    return fields


def _llm_result_fields(result: AIMessage) -> dict[str, Any]:
    """只提取 LangChain/Provider 实际返回的元数据，不补造不可得字段。"""

    response_metadata = result.response_metadata or {}
    usage = _token_usage(result.usage_metadata, response_metadata)
    fields: dict[str, Any] = {
        "token_usage": usage or None,
        "finish_reason": response_metadata.get("finish_reason"),
    }

    provider_request_id = response_metadata.get("id") or response_metadata.get(
        "request_id"
    )
    if provider_request_id is not None:
        fields["provider_request_id"] = provider_request_id
    return {key: value for key, value in fields.items() if value is not None}


def _token_usage(
    usage_metadata: Mapping[str, Any] | None,
    response_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    usage_metadata = usage_metadata or {}
    provider_usage = response_metadata.get("token_usage") or {}

    input_details = usage_metadata.get("input_token_details") or {}
    output_details = usage_metadata.get("output_token_details") or {}
    usage = {
        "input": usage_metadata.get("input_tokens", provider_usage.get("prompt_tokens")),
        "output": usage_metadata.get(
            "output_tokens", provider_usage.get("completion_tokens")
        ),
        "total": usage_metadata.get("total_tokens", provider_usage.get("total_tokens")),
        "cached_input": input_details.get(
            "cache_read", provider_usage.get("prompt_cache_hit_tokens")
        ),
        "reasoning": output_details.get(
            "reasoning", provider_usage.get("reasoning_tokens")
        ),
    }
    return {key: value for key, value in usage.items() if value is not None}


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Harness CLI")
    parser.add_argument("message", help="发送给模型的消息")
    args = parser.parse_args()
    print(asyncio.run(run(args.message)))


if __name__ == "__main__":
    main()
