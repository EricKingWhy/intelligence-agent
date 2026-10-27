"""ModelProvider：唯一的职责是把 ModelConfig 变成可用的 ChatModel。

不做重试/缓存——这些由 langchain-openai 和底层 openai SDK 负责。**唯一**自建的
HTTP 对象是 #1485 的守卫客户端（`guarded_clients`）：它只是给 SDK 默认 client 挂一个
request hook，超时 / 连接池 / 重定向仍取 SDK 默认值。
"""

import logging
from typing import Any

from langchain_openai import ChatOpenAI

from agent_harness.logging import log_event
from agent_harness.model.config import (
    REASONING_EFFORT_WIRE_VALUES as WIRE_REASONING_EFFORTS,
)
from agent_harness.model.config import (
    ConfigError,
    ModelClientConstructionError,
    ModelConfig,
)
from agent_harness.model.endpoint_guard import guarded_clients

logger = logging.getLogger("agent_harness.model")

class ReasoningChatOpenAI(ChatOpenAI):
    """接出第三方网关思考内容的 ChatOpenAI 子类（D-B① / ADR-0016 §3.4）。

    基类只认 OpenAI 官方字段：DeepSeek/Qwen 系网关在流式 delta 里的
    reasoning_content 会被 _convert_delta_to_message_chunk 静默丢弃
    （基类 docstring 明示「Use a provider-specific subclass」）。本类在
    chunk 转换层把 delta.reasoning_content 抬进 additional_kwargs——
    Runtime 逐 chunk 读取；聚合时 langchain 对同名字符串拼接，聚合消息
    自带完整思考文本但 Runtime 不消费（思考不回灌上下文，D7 隐私边界）。
    """

    def _convert_chunk_to_generation_chunk(
        self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None,
    ):
        reasoning: str | None = None
        if isinstance(chunk, dict):
            choices = chunk.get("choices") or []
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta")
                if isinstance(delta, dict):
                    raw = delta.get("reasoning_content")
                    if isinstance(raw, str) and raw:
                        reasoning = raw
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info,
        )
        if reasoning is not None and generation_chunk is not None:
            message = generation_chunk.message
            if hasattr(message, "additional_kwargs"):
                message.additional_kwargs = {
                    **message.additional_kwargs, "reasoning_content": reasoning,
                }
        return generation_chunk


def _close_quietly(client: Any) -> None:
    """构造失败路径的清理：关闭异常不能盖掉原本要上报的构造错误。"""
    if client is None:
        return
    close = getattr(client, "close", None)
    if close is None:
        return  # 异步 client 只有 aclose()；未发过请求 ⇒ 没有连接要回收
    try:
        close()
    except Exception:  # noqa: BLE001, S110 — 清理失败不改变失败结论
        pass


def create_chat_model(
    config: ModelConfig, *, reasoning_effort: str | None = None,
    request_timeout: float = 300.0,
) -> ReasoningChatOpenAI:
    """根据配置创建 OpenAI 兼容的 ChatModel（DeepSeek/Qwen/OpenAI 通吃）。

    显式声明 request_timeout / max_retries，不吃 SDK 默认（600s × (1+2) 次尝试
    最坏拖 ~30 分钟，且静默重试与 Harness 的 attempt 记账矛盾）：
    - max_retries=0：重试语义由 Harness 单一责任域拥有（不变量 #8/#9），
      与 memory/embeddings.py 同一原则。
    - request_timeout=300：chat 生成 legitimately 比 embedding 慢（长输出可到
      分钟级），300s 覆盖正常长生成、又把挂死调用的最坏代价从 30min 压到 5min。
      #203：连接测试传更短的专用超时（settings.model_test_timeout_seconds，
      默认 15s）——测试不该等 300s。

    The selected product level must be explicitly supported and mapped by this model's
    catalog entry. The validated wire value is injected at construction so ChatOpenAI
    includes it in the request body.
    """
    kwargs: dict[str, Any] = {
        "model": config.model_name,
        "api_key": config.get_secret_value(),
        "base_url": config.base_url,
        "temperature": config.temperature,
        "request_timeout": request_timeout,
        "max_retries": 0,
        # 显式关掉 langchain-openai 的 socket 选项注入：它只在**自建** transport 时
        # 生效，而本函数总是自带 client（下面），那条路径根本不走。留着默认值会让
        # `_warn_if_proxy_env_shadowed` 在设了代理变量的环境里每进程报一次
        # "代理自动探测被禁用"——对本路径是**假警报**（我们的 client 是裸 httpx2，
        # env 代理照常生效）。传 `()` = 显式声明"不要注入"，警告随之消失。
        "http_socket_options": (),
    }
    if reasoning_effort is not None:
        capability = config.validate_reasoning_effort(reasoning_effort)
        wire = capability.wire_mapping.get(reasoning_effort)
        if wire not in WIRE_REASONING_EFFORTS:
            raise ConfigError(
                f"model {config.model_name!r} has an invalid reasoning_effort wire mapping"
            )
        kwargs["reasoning_effort"] = wire
    try:
        # #1485 端点误配置守卫：注入带 request hook 的 httpx2 客户端。httpx2 在**每一跳**
        # 重定向前都调 request hook（`_send_handling_redirects` 循环体首行），所以被拦
        # 目标既收不到请求也收不到 Authorization 头。例外只来自所有者登记的
        # `Settings.model_endpoint_allowlist`（经 ModelConfig 带到这里）。
        # 放在 try 内：构造代理映射时可能抛 `InvalidURL`，那是同族的构造期失败，
        # 必须仍包成 `ModelClientConstructionError`（web 端点据此回 503 而非裸 500）。
        kwargs["http_client"], kwargs["http_async_client"] = guarded_clients(
            base_url=config.base_url,
            allow=config.endpoint_allowlist,
        )
        return ReasoningChatOpenAI(**kwargs)
    except Exception as error:
        # 已经建出来的客户端不归 SDK 管（`ReasoningChatOpenAI` 构造失败时它不会
        # 接手关闭），显式释放连接池，别指望 `__del__`（异步那个在没有运行 loop
        # 时会直接跳过）。
        for client in (kwargs.get("http_client"), kwargs.get("http_async_client")):
            _close_quietly(client)
        # 构造期失败（#517 BUG-05）：代理 URL 解析坏、key 形态非法等。稳定
        # outcome 让 JSONL 能按 outcome 直接捞出这条——与上面
        # reasoning_effort_not_injected 同一排障原则。
        log_event(
            logger, "system_log",
            "模型 client 构造失败",
            level="error",
            exc_info=True,
            component="model_provider",
            outcome="model_client_construction_failed",
            model=config.model_name,
        )
        raise ModelClientConstructionError(
            f"模型 client 构造失败（代理/网络环境或配置）：{error}"
        ) from error
