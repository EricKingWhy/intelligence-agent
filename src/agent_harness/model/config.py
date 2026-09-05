"""ModelConfig：把 Settings 解析成创建 ChatModel 所需的完整配置。

Provider 预设表是唯一允许出现厂商细节的地方。
未知 provider 在这里就抛错，而不是等到网络请求失败才发现。
"""

from pydantic import SecretStr

from agent_harness.config import Settings


class ConfigError(Exception):
    """配置错误（未知 provider、缺少必填项等）。"""


# 各厂商 OpenAI 兼容端点与默认模型。
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "model_base_url": "https://api.deepseek.com",
        "model_name": "deepseek-chat",
    },
    "qwen": {
        "model_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model_name": "qwen-plus",
    },
    # 腾讯 Coding Plan（OpenAI 兼容）。base_url 不含 /chat/completions，
    # SDK 会自动拼接；无默认模型，MODEL_NAME 必填。
    "tencent": {
        "model_base_url": "https://chatapi.weixin.qq.com/openai/v1",
        "model_name": "",
    },
    # SenseAudio（OpenAI 兼容）。无默认模型，MODEL_NAME 必填。
    "senseaudio": {
        "model_base_url": "https://api.senseaudio.cn/v1",
        "model_name": "",
    },
}


class ModelConfig:
    def __init__(
        self,
        *,
        provider: str,
        model_name: str,
        api_key: str,
        base_url: str,
        temperature: float,
        fallback: "ModelConfig | None" = None,
    ):
        # 运行期 SDK 要明文，但持有形态是 SecretStr：repr(config) / vars(config)
        # / pytest 失败局部变量等调试路径脱敏为 **********（与 Settings 层一致）。
        # get_secret_value() 是取明文的唯一出口。
        if not api_key.strip():
            raise ConfigError("ModelConfig 缺少 API key：空白 key 只会把失败推到首次 ainvoke")
        self.provider = provider
        self.model_name = model_name
        self.api_key = SecretStr(api_key)
        self.base_url = base_url
        self.temperature = temperature
        # Model Fallback 两级链（ADR-0014 决策 14）：瞬时故障切 fallback、
        # never 切回。V1 只消费第一级（Runtime 的 coordinator 持有两级结构）。
        self.fallback = fallback

    def get_secret_value(self) -> str:
        """取明文 key（仅 SDK 请求边界使用）。"""
        return self.api_key.get_secret_value()

    @classmethod
    def from_settings(cls, settings: Settings) -> "ModelConfig":
        config = cls._single_from(
            provider=settings.model_provider,
            model_name=settings.model_name,
            api_key=settings.model_api_key.get_secret_value(),
            base_url=settings.model_base_url,
            temperature=settings.temperature,
            key_env="MODEL_API_KEY",
        )
        # FALLBACK_MODEL_PROVIDER 为空 = 单级（无 fallback），静默缺省。
        if settings.fallback_model_provider:
            config.fallback = cls._single_from(
                provider=settings.fallback_model_provider,
                model_name=settings.fallback_model_name,
                api_key=settings.fallback_model_api_key.get_secret_value(),
                base_url=settings.fallback_model_base_url,
                temperature=settings.temperature,
                key_env="FALLBACK_MODEL_API_KEY",
            )
        return config

    @classmethod
    def _single_from(
        cls, *, provider: str, model_name: str, api_key: str,
        base_url: str, temperature: float, key_env: str,
    ) -> "ModelConfig":
        """解析单个（primary 或 fallback）模型配置；未知 provider 在配置期就抛错。"""
        if provider not in PROVIDER_PRESETS:
            raise ConfigError(
                f"未知 provider: {provider!r}，可选: {sorted(PROVIDER_PRESETS)}"
            )

        preset = PROVIDER_PRESETS[provider]
        name = model_name or preset["model_name"]
        if not name:
            raise ConfigError(
                f"provider {provider!r} 无默认模型，必须在 .env 中配置 MODEL_NAME"
            )
        # 空 key 快速失败：已知 provider + 空 key 是确定性配置错误——不应静默
        # 构造后把失败推到首次 ainvoke 时由 SDK 抛模糊的 AuthenticationError；
        # 与未知 provider / 缺 model_name 一致在此快速失败（构造器还有一道
        # 通用校验兜底，这里给出的是更可操作的 .env 指引）。
        if not api_key:
            raise ConfigError(
                f"provider {provider!r} 缺少 API key，必须在 .env 中配置 {key_env}"
            )
        return cls(
            provider=provider,
            # 显式配置优先，否则用厂商预设
            model_name=name,
            base_url=base_url or preset["model_base_url"],
            api_key=api_key,
            temperature=temperature,
        )