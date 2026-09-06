"""ModelProvider：唯一的职责是把 ModelConfig 变成可用的 ChatModel。

不写 HTTP Client、不做重试/缓存——这些都由 langchain-openai 和底层 openai SDK 负责。
"""

from langchain_openai import ChatOpenAI

from agent_harness.model.config import ModelConfig


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


def create_chat_model(config: ModelConfig) -> ReasoningChatOpenAI:
    """根据配置创建 OpenAI 兼容的 ChatModel（DeepSeek/Qwen/OpenAI 通吃）。

    显式声明 request_timeout / max_retries，不吃 SDK 默认（600s × (1+2) 次尝试
    最坏拖 ~30 分钟，且静默重试与 Harness 的 attempt 记账矛盾）：
    - max_retries=0：重试语义由 Harness 单一责任域拥有（不变量 #8/#9），
      与 memory/embeddings.py 同一原则。
    - request_timeout=300：chat 生成 legitimately 比 embedding 慢（长输出可到
      分钟级），300s 覆盖正常长生成、又把挂死调用的最坏代价从 30min 压到 5min。
    """
    return ReasoningChatOpenAI(
        model=config.model_name,
        api_key=config.get_secret_value(),
        base_url=config.base_url,
        temperature=config.temperature,
        request_timeout=300,
        max_retries=0,
    )