"""ModelProvider：唯一的职责是把 ModelConfig 变成可用的 ChatModel。

不写 HTTP Client、不做重试/缓存——这些都由 langchain-openai 和底层 openai SDK 负责。
"""

from langchain_openai import ChatOpenAI

from agent_harness.model.config import ModelConfig


def create_chat_model(config: ModelConfig) -> ChatOpenAI:
    """根据配置创建 OpenAI 兼容的 ChatModel（DeepSeek/Qwen/OpenAI 通吃）。

    显式声明 request_timeout / max_retries，不吃 SDK 默认（600s × (1+2) 次尝试
    最坏拖 ~30 分钟，且静默重试与 Harness 的 attempt 记账矛盾）：
    - max_retries=0：重试语义由 Harness 单一责任域拥有（不变量 #8/#9），
      与 memory/embeddings.py 同一原则。
    - request_timeout=300：chat 生成 legitimately 比 embedding 慢（长输出可到
      分钟级），300s 覆盖正常长生成、又把挂死调用的最坏代价从 30min 压到 5min。
    """
    return ChatOpenAI(
        model=config.model_name,
        api_key=config.get_secret_value(),
        base_url=config.base_url,
        temperature=config.temperature,
        request_timeout=300,
        max_retries=0,
    )