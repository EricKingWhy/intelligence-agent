"""ModelProvider 配置选择逻辑的单元测试（不发起真实请求）。"""

import pytest

from agent_harness.config import Settings
from agent_harness.model.config import ConfigError, ModelConfig
from agent_harness.model.provider import create_chat_model


def make_settings(**overrides) -> Settings:
    return Settings(
        model_api_key="sk-test",
        _env_file=None,  # 测试不受本地 .env 影响
        **overrides,
    )


class TestModelConfigFromSettings:
    def test_deepseek_uses_preset(self):
        config = ModelConfig.from_settings(make_settings(model_provider="deepseek"))
        assert config.provider == "deepseek"
        assert config.model_name == "deepseek-chat"
        assert config.base_url == "https://api.deepseek.com"

    def test_qwen_uses_preset(self):
        config = ModelConfig.from_settings(make_settings(model_provider="qwen"))
        assert config.provider == "qwen"
        assert config.model_name == "qwen-plus"
        assert (
            config.base_url
            == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )

    def test_explicit_config_overrides_preset(self):
        config = ModelConfig.from_settings(
            make_settings(
                model_provider="deepseek",
                model_name="deepseek-reasoner",
            )
        )
        assert config.model_name == "deepseek-reasoner"
        assert config.base_url == "https://api.deepseek.com"

    def test_tencent_preset_requires_model_name(self):
        config = ModelConfig.from_settings(
            make_settings(
                model_provider="tencent",
                model_name="claude-sonnet-4-5",
            )
        )
        assert config.model_name == "claude-sonnet-4-5"
        assert config.base_url == "https://chatapi.weixin.qq.com/openai/v1"

        with pytest.raises(ConfigError, match="MODEL_NAME"):
            ModelConfig.from_settings(make_settings(model_provider="tencent"))

    def test_unknown_provider_raises_config_error(self):
        with pytest.raises(ConfigError, match="unknown-provider"):
            ModelConfig.from_settings(make_settings(model_provider="unknown-provider"))


class TestCreateChatModel:
    def test_creates_chat_model_with_config(self):
        config = ModelConfig.from_settings(make_settings(model_provider="qwen"))
        model = create_chat_model(config)
        assert model.model_name == "qwen-plus"
        assert str(model.openai_api_base) == "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def test_temperature_is_applied(self):
        config = ModelConfig.from_settings(
            make_settings(temperature=0.7)
        )
        model = create_chat_model(config)
        assert model.temperature == 0.7