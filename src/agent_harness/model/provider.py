"""ModelProvider：唯一的职责是把 ModelConfig 变成可用的 ChatModel。

不写 HTTP Client、不做重试/缓存——这些都由 langchain-openai 和底层 openai SDK 负责。
"""

import logging
from typing import Any

from langchain_openai import ChatOpenAI

from agent_harness.logging import log_event
from agent_harness.model.config import ConfigError, ModelConfig

logger = logging.getLogger("agent_harness.model")

#: provider 线格式的合法 reasoning_effort 枚举（OpenAI 兼容推理接口字段定义）。
#: 只认这几个字面量——其他值一律 400 invalid_parameter_error（实测）。
WIRE_REASONING_EFFORTS: frozenset[str] = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)

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


class ModelClientConstructionError(RuntimeError):
    """模型 client 构造失败（#517 BUG-05）。

    `create_chat_model` 的构造是**纯本地操作**（字段校验 + SDK client 初始化），
    此处的失败来自配置/环境而非网络调用——典型如 corporate/k8s 代理变量
    （`NO_PROXY` 里的 `[::1]`）让 httpx 的代理映射解析在构造期抛 `InvalidURL`。
    显式类型让 web 层按类型映射成结构化 503（环境/配置暂时不可用，可修好重试），
    而不是把它当服务端 bug 打成裸 500。`__cause__` 保留原始异常。
    """


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
        return ReasoningChatOpenAI(**kwargs)
    except Exception as error:
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
