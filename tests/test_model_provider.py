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

    def test_client_timeout_and_retries_are_explicit(self):
        """chat 模型客户端必须显式声明 timeout 与 max_retries。

        缺省时 openai SDK 默认 request_timeout=600s、max_retries=2：
        一次挂死的 provider 调用最坏拖 ~30 分钟（600s × 3 次尝试），SSE 客户端
        只能干等；且 SDK 静默重试与 cli.py 记录的 attempt=1/max_attempts=1
        相互矛盾——重试语义必须由 Harness 单一责任域拥有（不变量 #8/#9），
        与 memory/embeddings.py 的 request_timeout=15, max_retries=0 同一原则。
        """
        config = ModelConfig.from_settings(make_settings(model_provider="deepseek"))
        model = create_chat_model(config)
        assert model.max_retries == 0
        assert model.request_timeout is not None and model.request_timeout > 0

# ── model_api_key 脱敏（Round 4 安全加固）──


class TestApiKeyHandling:
    def test_settings_model_api_key_is_secretstr(self):
        """model_api_key 必须用 SecretStr——和 milvus_token/embedding_api_key 一致。

        str 字段会被 model_dump() / repr() 原样回显；任何把 Settings 转储进日志、
        异常页、调试器的路径都会泄漏 live key。SecretStr 在这些路径脱敏为 **********。
        """
        from agent_harness.config import Settings

        settings = Settings(model_api_key="sk-live-key", _env_file=None)
        dumped = settings.model_dump(mode="json")
        # 已脱敏：不等于明文，与 milvus_token 行为一致。
        assert dumped["model_api_key"] != "sk-live-key"
        assert dumped["milvus_token"] != "real"  # 同类字段参照
        # 但 SecretStr 仍可拿到明文（运行期正常使用）。
        assert settings.model_api_key.get_secret_value() == "sk-live-key"

    def test_settings_remaining_secrets_are_secretstr(self):
        """jwt_secret 与 artifact_store 密钥同样脱敏（Round 4 加固补全）。

        这三个字段是活密钥：jwt_secret 泄漏等于伪造任意身份，S3 密钥泄漏等于
        丢失整个 artifact bucket 的写权限。与 model_api_key 同一待遇。
        """
        settings = Settings(
            jwt_secret="jwt-live-secret",
            artifact_store_access_key="ak-live",
            artifact_store_secret_key="sk-live",
            _env_file=None,
        )
        dumped = settings.model_dump(mode="json")
        assert dumped["jwt_secret"] != "jwt-live-secret"
        assert dumped["artifact_store_access_key"] != "ak-live"
        assert dumped["artifact_store_secret_key"] != "sk-live"
        # 运行期仍可取明文。
        assert settings.jwt_secret.get_secret_value() == "jwt-live-secret"

    def test_settings_ignores_foreign_env_keys(self):
        """.env 里出现非本应用配置的键不应让启动直接崩溃。

        pydantic-settings 默认 extra='forbid'：部署机上 .env 常混有编辑器 /
        部署工具 / 其它应用的变量，启动时抛 "Extra inputs are not permitted"
        属于把无关环境污染当成致命错误。应忽略而不是拒绝。
        """
        settings = Settings(some_unrelated_deploy_var="x", _env_file=None)
        assert settings.model_provider == "deepseek"  # 正常字段不受影响

    def test_model_config_repr_redacts_api_key(self):
        """ModelConfig 持有明文 key，repr / vars 必须脱敏——否则任何调试/日志路径
        （pytest 失败局部变量、repr(config)、vars(config)）都会回显 live key。
        """
        config = ModelConfig(
            provider="deepseek", model_name="deepseek-chat",
            api_key="sk-live-key", base_url="https://api.deepseek.com",
            temperature=0.2,
        )
        assert "sk-live-key" not in repr(config)
        assert "sk-live-key" not in str(vars(config))

    def test_model_config_rejects_blank_api_key(self):
        """直接构造 ModelConfig 时空白 key 快速失败，不推迟到首次 ainvoke。"""
        with pytest.raises(ConfigError, match="API key"):
            ModelConfig(
                provider="deepseek", model_name="deepseek-chat",
                api_key="  ", base_url="https://api.deepseek.com",
                temperature=0.2,
            )


# ── 空 api_key 快速失败（Round 4 健壮性）──


class TestEmptyApiKeyFastFail:
    def test_known_provider_with_empty_api_key_raises(self):
        """已知 provider 但 api_key 空 -> from_settings 显式 ConfigError。

        不应静默构造一个 api_key="" 的 ModelConfig 把错误推到首次 ainvoke 时
        才由 SDK 抛 AuthenticationError（消息来自远端、语义模糊）。未知 provider
        与缺 model_name 都做了快速失败，api_key 应保持一致。
        """
        with pytest.raises(ConfigError, match="API key|MODEL_API_KEY"):
            ModelConfig.from_settings(
                Settings(model_provider="deepseek", model_api_key="", _env_file=None)
            )
