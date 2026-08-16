"""ModelProvider：唯一的职责是把 ModelConfig 变成可用的 ChatModel。

不写 HTTP Client、不做重试/缓存——这些都由 langchain-openai 和底层 openai SDK 负责。
"""

from langchain_openai import ChatOpenAI

from agent_harness.model.config import ModelConfig


def create_chat_model(config: ModelConfig) -> ChatOpenAI:
    """根据配置创建 OpenAI 兼容的 ChatModel（DeepSeek/Qwen/OpenAI 通吃）。"""
    return ChatOpenAI(
        model=config.model_name,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
    )