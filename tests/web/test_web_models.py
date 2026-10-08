"""T7 web 契约（ADR-0016 §5）：GET /api/models + POST /api/sessions 的 model 参数。"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel

_AGENT_MODELS = (
    '[{"name": "qwen-max", "provider": "senseaudio", "model_name": "qwen3.8-max-0902"},'
    ' {"name": "mimo-flash", "provider": "mimo", "model_name": "mimo-v2.6-flash"}]'
)


@pytest.fixture
def catalog_app(tmp_path):
    # _env_file=None：钉死测试配置，不吃机器 .env（MODEL_NAME/FALLBACK_* 会漂移）；
    # provider_store_path 钉进 tmp_path——自定义 provider 存储默认读 HOME 用户态
    # （~/.agent-harness/model-providers.json），不密封会把宿主真配置泄进 catalog 断言。
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        model_provider="deepseek", model_name="deepseek-chat",
        fallback_model_provider="mimo", fallback_model_name="mimo-v2.6-flash",
        fallback_model_api_key="sk-fallback",
        agent_models=_AGENT_MODELS,
        provider_store_path=str(tmp_path / "model-providers.json"),
    )
    return create_app(settings, enable_cors=False)


@pytest.fixture
def catalog_client(catalog_app):
    return TestClient(catalog_app)


class TestModelsEndpoint:
    def test_lists_default_and_catalog_without_secrets(self, catalog_client):
        resp = catalog_client.get("/api/models")
        assert resp.status_code == 200
        body = resp.json()
        models = body["models"]
        # 旧字段 alias（向后兼容）
        assert [m["name"] for m in models] == ["deepseek-chat", "qwen-max", "mimo-flash"]
        assert models[0]["default"] is True and models[1]["default"] is False
        assert models[1]["model"] == "qwen3.8-max-0902"
        assert models[1]["provider"] == "senseaudio"
        assert models[2]["model"] == "mimo-v2.6-flash"
        assert models[2]["provider"] == "mimo"
        # Phase 2 新字段（SDD 03 §16 ModelOption）
        assert [m["id"] for m in models] == ["deepseek-chat", "qwen-max", "mimo-flash"]
        assert models[0]["is_default"] is True and models[1]["is_default"] is False
        assert all(m["is_available"] is True for m in models)
        assert models[0]["metadata_source"] == "provider_preset"
        assert models[0]["display_name"] == "deepseek-chat"
        serialized = json.dumps(body)
        assert "api_key" not in serialized and "sk-" not in serialized, \
            "模型列表绝不携带密钥字段"

    def test_models_carry_provider_preset_capabilities(self, catalog_client):
        """deepseek preset 已知能力位下发，未知能力位省略（契约：not guessed）。"""
        body = catalog_client.get("/api/models").json()
        deepseek = next(m for m in body["models"] if m["id"] == "deepseek-chat")
        # 已验证的能力位（来自 PROVIDER_PRESETS）
        assert deepseek.get("supports_tools") is True
        assert deepseek.get("context_window") == 64000
        assert deepseek.get("speed_tier") == "fast"
        # #520：官方文档证实的自动前缀缓存（无标记参数）随 preset 下发
        assert deepseek.get("prompt_cache") == "automatic"
        # 未在 preset 声明的能力位必须不出现在响应里
        assert "supports_vision" not in deepseek, \
            "supports_vision 未在 deepseek preset 声明，不该被猜出来"
        assert "supports_reasoning_summary" not in deepseek

        mimo = next(m for m in body["models"] if m["id"] == "mimo-flash")
        assert mimo["provider"] == "mimo"
        assert mimo.get("supports_tools") is True
        # #823 / MM-02（A5）：mimo preset 经 AC11 真机验证过视觉 ⇒ 出厂声明 True。
        assert mimo.get("supports_vision") is True
        assert mimo["metadata_source"] == "provider_preset"
        # mimo 未做缓存机制核实 ⇒ 不声明（不猜测）
        assert "prompt_cache" not in mimo

    def test_models_expose_declared_reasoning_effort_capability(self, tmp_path):
        descriptor = {
            "supported": ["minimal", "deep"],
            "default": "minimal",
            "wire_mapping": {"minimal": "low", "deep": "high"},
        }
        settings = Settings(
            _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
            provider_store_path=str(tmp_path / "model-providers.json"),
            model_provider="deepseek", model_name="deepseek-chat",
            agent_models=json.dumps([{
                "name": "reasoning-model", "provider": "deepseek",
                "model_name": "deepseek-r1", "reasoning_effort": descriptor,
            }]),
        )
        client = TestClient(create_app(settings, enable_cors=False))

        models = client.get("/api/models").json()["models"]
        reasoning_model = next(m for m in models if m["id"] == "reasoning-model")
        default_model = next(m for m in models if m["is_default"])

        assert reasoning_model["reasoning_effort"] == descriptor
        assert "reasoning_effort" not in default_model

    def test_default_model_uses_matching_catalog_effort_declaration(self, tmp_path):
        descriptor = {
            "supported": ["minimal", "standard"],
            "default": "standard",
            "wire_mapping": {"minimal": "none", "standard": "low"},
        }
        settings = Settings(
            _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
            provider_store_path=str(tmp_path / "model-providers.json"),
            model_provider="deepseek", model_name="deepseek-chat",
            agent_models=json.dumps([{
                "name": "default-profile", "provider": "deepseek",
                "model_name": "deepseek-chat", "reasoning_effort": descriptor,
            }]),
        )
        client = TestClient(create_app(settings, enable_cors=False))

        models = client.get("/api/models").json()["models"]
        default_model = next(m for m in models if m["is_default"])

        assert default_model["reasoning_effort"] == descriptor

    def test_models_catalog_entry_without_capabilities_falls_back_to_preset(self, catalog_client):
        """catalog 条目不声明能力位 → 回落 preset，metadata_source=provider_preset。"""
        body = catalog_client.get("/api/models").json()
        # qwen-max 的 provider 是 senseaudio（见 _AGENT_MODELS），preset 无能力位声明
        qwen = next(m for m in body["models"] if m["id"] == "qwen-max")
        assert qwen["metadata_source"] == "provider_preset"
        # senseaudio preset 没有任何能力位 → 全部省略
        assert "supports_tools" not in qwen
        assert "context_window" not in qwen

    def test_models_catalog_entry_overrides_preset(self, tmp_path):
        """catalog 条目显式声明能力位 → 覆盖 preset，metadata_source=agent_models。"""
        from agent_harness.config import Settings
        from agent_harness.web.app import create_app

        agent_models = (
            '[{"name": "deepseek-pro", "provider": "deepseek", '
            '"model_name": "deepseek-chat", '
            '"context_window": 128000, "supports_tools": false}]'
        )
        settings = Settings(
            _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
            provider_store_path=str(tmp_path / "model-providers.json"),
            model_provider="deepseek", model_name="deepseek-chat",
            agent_models=agent_models,
        )
        client = TestClient(create_app(settings, enable_cors=False))
        body = client.get("/api/models").json()
        pro = next(m for m in body["models"] if m["id"] == "deepseek-pro")
        assert pro["metadata_source"] == "agent_models"
        assert pro["context_window"] == 128000, "catalog 覆盖 preset"
        assert pro["supports_tools"] is False, "catalog 覆盖 preset"
        # 未在 catalog 条目声明的能力位（speed_tier）回落 preset
        assert pro.get("speed_tier") == "fast"

    def test_models_no_secret_leak_with_capabilities(self, catalog_client):
        """加能力位后序列化仍无密钥泄露（跨字段序列化）。"""
        body = catalog_client.get("/api/models").json()
        serialized = json.dumps(body)
        assert "sk-test" not in serialized
        assert "api_key" not in serialized


class TestSessionModelParam:
    def test_model_selection_reaches_chat_model(self, catalog_client):
        """model 参数命中 catalog → 装配层拿到该 model_name 的配置。"""
        seen: dict = {}

        def fake_factory(config, *, reasoning_effort=None, **kw):
            # build_runtime 对 primary/fallback 各调一次——收集全量，主链取首个
            seen.setdefault("names", []).append(config.model_name)
            return ScriptedModel(responses=[AIMessage(content="ok")])

        with patch("agent_harness.assembly.create_chat_model", side_effect=fake_factory):
            resp = catalog_client.post(
                "/api/sessions", json={"task": "hi", "model": "qwen-max"})
        assert resp.status_code == 200
        assert seen["names"][0] == "qwen3.8-max-0902"
        assert "mimo-v2.6-flash" in seen["names"], "fallback 链不受 model 选择影响（ADR-0014）"

    def test_no_model_param_keeps_default_chain(self, catalog_client):
        """不传 model = 默认链（现行为不变）。"""
        seen: dict = {}

        def fake_factory(config, *, reasoning_effort=None, **kw):
            seen.setdefault("names", []).append(config.model_name)
            return ScriptedModel(responses=[AIMessage(content="ok")])

        with patch("agent_harness.assembly.create_chat_model", side_effect=fake_factory):
            resp = catalog_client.post("/api/sessions", json={"task": "hi"})
        assert resp.status_code == 200
        assert seen["names"][0] == "deepseek-chat"

    @pytest.mark.parametrize(
        ("fallback_effort", "expected_fallback_effort"),
        [
            (
                {
                    "supported": ["minimal"],
                    "default": "minimal",
                    "wire_mapping": {"minimal": "low"},
                },
                "minimal",
            ),
            (None, None),
        ],
        ids=["declared-default", "provider-default"],
    )
    def test_fallback_uses_its_own_default_or_provider_default(
        self, tmp_path, fallback_effort, expected_fallback_effort,
    ):
        primary_effort = {
            "supported": ["minimal", "deep"],
            "default": "deep",
            "wire_mapping": {"minimal": "low", "deep": "high"},
        }
        fallback_entry = {
            "name": "fallback",
            "provider": "mimo",
            "model_name": "mimo-v2.6-flash",
        }
        if fallback_effort is not None:
            fallback_entry["reasoning_effort"] = fallback_effort
        settings = Settings(
            _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-primary",
            model_provider="deepseek", model_name="deepseek-chat",
            fallback_model_provider="mimo", fallback_model_name="mimo-v2.6-flash",
            fallback_model_api_key="sk-fallback",
            agent_models=json.dumps([
                {"name": "primary", "provider": "deepseek", "model_name": "deepseek-chat",
                 "reasoning_effort": primary_effort},
                fallback_entry,
            ]),
            provider_store_path=str(tmp_path / "model-providers.json"),
        )
        client = TestClient(create_app(settings, enable_cors=False))
        seen: list[tuple[str, str | None]] = []

        def fake_factory(config, *, reasoning_effort=None, **kw):
            seen.append((config.model_name, reasoning_effort))
            return ScriptedModel(responses=[AIMessage(content="ok")])

        with patch("agent_harness.assembly.create_chat_model", side_effect=fake_factory):
            response = client.post(
                "/api/sessions", json={"task": "hi", "reasoning_effort": "deep"},
            )

        assert response.status_code == 200
        assert seen == [
            ("deepseek-chat", "deep"),
            ("mimo-v2.6-flash", expected_fallback_effort),
        ]

    def test_unknown_model_422(self, catalog_client):
        with patch("agent_harness.assembly.create_chat_model",
                   return_value=ScriptedModel(responses=[AIMessage(content="ok")])):
            resp = catalog_client.post(
                "/api/sessions", json={"task": "hi", "model": "no-such"})
        assert resp.status_code == 422
        assert "no-such" in resp.text

    def test_unsupported_reasoning_effort_is_rejected_with_actionable_response(self, tmp_path):
        descriptor = {
            "supported": ["minimal"],
            "default": "minimal",
            "wire_mapping": {"minimal": "low"},
        }
        settings = Settings(
            _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
            provider_store_path=str(tmp_path / "model-providers.json"),
            model_provider="deepseek", model_name="deepseek-chat",
            agent_models=json.dumps([{
                "name": "limited-primary", "provider": "deepseek",
                "model_name": "deepseek-chat", "reasoning_effort": descriptor,
            }]),
        )
        client = TestClient(create_app(settings, enable_cors=False))

        response = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "model": "limited-primary",
                "reasoning_effort": "deep",
            },
        )

        assert response.status_code == 422
        assert "deepseek-chat" in response.json()["detail"]
        assert "deep" in response.json()["detail"]
        workspace_root = tmp_path / "workspaces"
        assert not workspace_root.exists() or not any(workspace_root.iterdir())

    def test_invalid_reasoning_effort_wire_mapping_is_rejected_before_workspace_creation(
        self, tmp_path,
    ):
        settings = Settings(
            _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
            provider_store_path=str(tmp_path / "model-providers.json"),
            model_provider="deepseek", model_name="deepseek-chat",
            agent_models=json.dumps([{
                "name": "invalid-wire-map", "provider": "deepseek",
                "model_name": "deepseek-r1",
                "reasoning_effort": {
                    "supported": ["minimal"],
                    "default": "minimal",
                    "wire_mapping": {"minimal": "deep"},
                },
            }]),
        )
        client = TestClient(create_app(settings, enable_cors=False))

        response = client.post(
            "/api/sessions",
            json={"task": "hi", "model": "invalid-wire-map", "reasoning_effort": "minimal"},
        )

        assert response.status_code == 422
        assert "wire_mapping" in response.json()["detail"]
        workspace_root = tmp_path / "workspaces"
        assert not workspace_root.exists() or not any(workspace_root.iterdir())
