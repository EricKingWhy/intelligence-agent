"""T7（ADR-0016 §5）：多模型 catalog + 会话级模型选择（C6 / D-B②）。

语义：POST /api/sessions 可选 model 参数（不传 = 默认链，现行为不变）；
fallback 链语义不动（ADR-0014）；GET /api/models 列出可选模型（无密钥字段）。
"""

from __future__ import annotations

import json

import pytest

from agent_harness.config import Settings
from agent_harness.model.config import (
    PROVIDER_PRESETS,
    ConfigError,
    ModelConfig,
    _pick_capabilities,
    find_catalog_entry,
    model_supports_vision,
    parse_model_catalog,
)


def _settings(agent_models: str = "", **kwargs) -> Settings:
    return Settings(
        _env_file=None, workspace_dir="/tmp/x", model_api_key="sk-default",
        model_provider="deepseek", model_name="", agent_models=agent_models, **kwargs,
    )


_VALID = json.dumps([
    {"name": "qwen-max", "provider": "senseaudio", "model_name": "qwen3.8-max-0902"},
    {"name": "mimo-flash", "provider": "mimo", "model_name": "mimo-v2.6-flash",
     "api_key": "sk-mimo", "temperature": 0.5},
])


class TestParseModelCatalog:
    def test_empty_settings_empty_catalog(self):
        assert parse_model_catalog(_settings()) == []

    def test_valid_catalog_parsed(self):
        entries = parse_model_catalog(_settings(_VALID))
        assert [e.name for e in entries] == ["qwen-max", "mimo-flash"]
        assert entries[0].model_name == "qwen3.8-max-0902"
        assert entries[1].provider == "mimo"
        assert entries[1].model_name == "mimo-v2.6-flash"

    def test_invalid_json_loud_error(self):
        with pytest.raises(ConfigError, match="AGENT_MODELS"):
            parse_model_catalog(_settings("{not json"))

    def test_non_list_rejected(self):
        with pytest.raises(ConfigError, match="数组"):
            parse_model_catalog(_settings('{"name": "x"}'))

    def test_unknown_provider_rejected(self):
        with pytest.raises(ConfigError, match="provider"):
            parse_model_catalog(_settings(json.dumps([
                {"name": "x", "provider": "mystery", "model_name": "m"}])))

    @pytest.mark.parametrize("provider", ["tencent", "zhipu"])
    def test_replaced_provider_rejected(self, provider):
        with pytest.raises(ConfigError, match=provider):
            parse_model_catalog(_settings(json.dumps([
                {"name": "x", "provider": provider, "model_name": "m"}])))

    def test_duplicate_name_rejected(self):
        with pytest.raises(ConfigError, match="重名|duplicate|name"):
            parse_model_catalog(_settings(json.dumps([
                {"name": "dup", "provider": "deepseek", "model_name": "a"},
                {"name": "dup", "provider": "deepseek", "model_name": "b"}])))


class TestResolveModelConfig:
    def test_unknown_name_raises(self):
        with pytest.raises(ConfigError, match="未知模型"):
            ModelConfig.from_catalog(_settings(_VALID), "no-such")

    def test_entry_overrides_and_fallbacks(self):
        """entry 字段优先；api_key 缺省回落 MODEL_API_KEY；base_url 缺省回落 preset。"""
        config = ModelConfig.from_catalog(_settings(_VALID), "qwen-max")
        assert config.model_name == "qwen3.8-max-0902"
        assert config.provider == "senseaudio"
        assert config.base_url == "https://api.senseaudio.cn/v1"
        assert config.get_secret_value() == "sk-default"
        assert config.temperature == 0.2  # settings 默认

        config2 = ModelConfig.from_catalog(_settings(_VALID), "mimo-flash")
        assert config2.get_secret_value() == "sk-mimo"
        assert config2.temperature == 0.5
        assert config2.provider == "mimo"
        assert config2.model_name == "mimo-v2.6-flash"
        assert config2.base_url == "https://api.xiaomimimo.com/v1"

    def test_default_chain_unaffected(self):
        """不选模型 = 默认链，行为与旧版逐字段一致。"""
        config = ModelConfig.from_settings(_settings(_VALID))
        assert config.model_name == "deepseek-chat"  # deepseek preset 默认
        assert config.get_secret_value() == "sk-default"


class TestFindCatalogEntry:
    """T7 #137：模型切换按 (provider, model_id) 寻址。"""

    def test_match_by_entry_name(self):
        entry = find_catalog_entry(_settings(_VALID), "senseaudio", "qwen-max")
        assert entry is not None and entry.name == "qwen-max"

    def test_match_by_upstream_model_name(self):
        entry = find_catalog_entry(_settings(_VALID), "mimo", "mimo-v2.6-flash")
        assert entry is not None and entry.name == "mimo-flash"

    def test_provider_mismatch_returns_none(self):
        assert find_catalog_entry(_settings(_VALID), "deepseek", "qwen-max") is None

    def test_unknown_returns_none(self):
        assert find_catalog_entry(_settings(_VALID), "mimo", "nope") is None

    def test_name_match_wins_over_model_name_collision(self):
        """条目 A 的 name 撞上条目 B 的 model_name 时，精确 name 匹配优先。"""
        catalog = json.dumps([
            {"name": "alpha", "provider": "deepseek", "model_name": "beta"},
            {"name": "beta", "provider": "deepseek", "model_name": "alpha"},
        ])
        entry = find_catalog_entry(_settings(catalog), "deepseek", "beta")
        assert entry is not None and entry.name == "beta"


class TestPromptCacheCapabilityDeclaration:
    """#520 一期：`prompt_cache` 能力位（declarative only，无行为分支）。

    只给有官方文档依据的 provider 声明（deepseek = 自动前缀缓存，无标记参数）；
    未声明的键省略——「not guessed」契约。取值词汇：`automatic`（无标记、稳定前缀
    自动命中）| `explicit_breakpoints`（Anthropic 式 cache_control）。
    """

    def test_deepseek_preset_declares_automatic_caching(self):
        assert PROVIDER_PRESETS["deepseek"]["prompt_cache"] == "automatic"

    def test_unverified_providers_do_not_declare(self):
        # 遍历全部 preset 而非硬编码清单（两轴审查 P3）：将来新增的 provider
        # 默认不声明，只有带上官方文档依据才单独加钉——「not guessed」契约。
        for name, preset in PROVIDER_PRESETS.items():
            if name == "deepseek":
                continue
            assert "prompt_cache" not in preset, name

    def test_pick_capabilities_passes_the_field_through(self):
        picked = _pick_capabilities({"prompt_cache": "automatic", "unknown": "x"})
        assert picked == {"prompt_cache": "automatic"}

    def test_catalog_entry_may_declare(self):
        entries = parse_model_catalog(_settings(json.dumps([
            {"name": "claude-x", "provider": "deepseek", "model_name": "m",
             "prompt_cache": "explicit_breakpoints"},
        ])))
        assert entries[0].declared_capabilities()["prompt_cache"] == (
            "explicit_breakpoints"
        )

    def test_catalog_entry_rejects_non_string(self):
        with pytest.raises(ConfigError):
            parse_model_catalog(_settings(json.dumps([
                {"name": "bad", "provider": "deepseek", "model_name": "m",
                 "prompt_cache": 123},
            ])))


class TestModelSupportsVision:
    """#823 / MM-02（A5）：视觉能力的解析口径（catalog 显式 > preset 声明 > False）。"""

    def _config(self, provider: str, model_name: str) -> tuple[Settings, ModelConfig]:
        settings = Settings(
            _env_file=None, workspace_dir="/tmp/x", model_api_key="sk-test",
            model_provider=provider, model_name=model_name,
        )
        return settings, ModelConfig.from_settings(settings)

    def test_mimo_preset_declares_vision(self):
        """mimo preset 经 AC11 真机验证过视觉 ⇒ 显式声明 True（出厂即可用）。"""
        settings, config = self._config("mimo", "mimo-v2.6-flash")
        assert model_supports_vision(settings, config) is True

    def test_deepseek_preset_not_guessed(self):
        """未验证的 preset 不猜——deepseek 省略 ⇒ False。"""
        settings, config = self._config("deepseek", "deepseek-chat")
        assert model_supports_vision(settings, config) is False

    def test_catalog_entry_overrides_preset(self):
        """catalog 显式声明优先于 preset 声明。"""
        settings = Settings(
            _env_file=None, workspace_dir="/tmp/x", model_api_key="sk-test",
            model_provider="deepseek", model_name="deepseek-chat",
            agent_models=json.dumps([
                {"name": "vis", "provider": "deepseek", "model_name": "deepseek-chat",
                 "supports_vision": True},
            ]),
        )
        config = ModelConfig.resolve_selection(settings, "vis")
        assert model_supports_vision(settings, config) is True


class TestDefaultEntryResolution:
    """#1016：``/api/models`` 的默认条目 id（= 默认链模型名）经统一解析点
    ``resolve_selection`` 解析成**默认链**，与切换路径判定一致。

    第一性原理：「默认 / 继承默认链」是取值空间之外的一个状态，不是一个 id。
    ``/api/models`` 把默认链广告成一条 id=默认模型名的条目 ⇒ 同一个字符串在
    切换路径（``resolve_model_target``）与续聊 amend 校验路径（``resolve_selection``）
    必须判定一致，否则用户点那条「默认」后会永久 422。
    """

    @staticmethod
    def _settings(**kwargs) -> Settings:
        return Settings(
            _env_file=None, workspace_dir="/tmp/x", model_api_key="sk-test",
            model_provider="deepseek", model_name="deepseek-chat", **kwargs,
        )

    def test_resolve_selection_accepts_default_entry_as_default_chain(self):
        config = ModelConfig.resolve_selection(self._settings(), "deepseek-chat")
        assert config.provider == "deepseek"
        assert config.model_name == "deepseek-chat"

    def test_unified_paths_agree_on_default_entry(self):
        """切换路径与续聊校验路径（裸 resolve_selection）对默认条目判定一致。"""
        from agent_harness.session.model_switch import (
            default_model_id,
            resolve_model_target,
        )

        settings = self._settings()
        default_id = default_model_id(settings)
        target = resolve_model_target(settings, "deepseek", default_id)
        assert target is not None
        assert target.model_id is None  # 切换路径：接受为「清除会话级覆盖」
        # 续聊 amend 走的是裸 resolve_selection——必须同样接受。
        config = ModelConfig.resolve_selection(settings, default_id)
        assert config.model_name == default_id

    def test_default_entry_shadows_same_named_catalog_entry(self):
        """消融实验 4（AC5）：MODEL_NAME 恰与某 catalog 条目同名时，默认条目名解析成
        默认链（与 resolve_model_target:165-167 的「默认条目优先」一致），不锁死到
        该 catalog 条目的 base_url / temperature。"""
        settings = self._settings(agent_models=json.dumps([
            {"name": "deepseek-chat", "provider": "deepseek",
             "model_name": "deepseek-reasoner"},
        ]))
        config = ModelConfig.resolve_selection(settings, "deepseek-chat")
        assert config.model_name == "deepseek-chat"  # 默认链，而非 deepseek-reasoner

    def test_unknown_model_still_raises(self):
        with pytest.raises(ConfigError):
            ModelConfig.resolve_selection(self._settings(), "definitely-not-a-model")
