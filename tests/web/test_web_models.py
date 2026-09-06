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
    '[{"name": "qwen-max", "provider": "senseaudio", "model_name": "qwen3.8-max-0902"}]'
)


@pytest.fixture
def catalog_app(tmp_path):
    # _env_file=None：钉死测试配置，不吃机器 .env（MODEL_NAME/FALLBACK_* 会漂移）
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
        model_provider="deepseek", model_name="deepseek-chat",
        fallback_model_provider="zhipu", fallback_model_name="glm-4.5-air",
        fallback_model_api_key="sk-fallback",
        agent_models=_AGENT_MODELS,
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
        assert [m["name"] for m in models] == ["deepseek-chat", "qwen-max"]
        assert models[0]["default"] is True and models[1]["default"] is False
        assert models[1]["model"] == "qwen3.8-max-0902"
        assert models[1]["provider"] == "senseaudio"
        serialized = json.dumps(body)
        assert "api_key" not in serialized and "sk-" not in serialized, \
            "模型列表绝不携带密钥字段"


class TestSessionModelParam:
    def test_model_selection_reaches_chat_model(self, catalog_client):
        """model 参数命中 catalog → 装配层拿到该 model_name 的配置。"""
        seen: dict = {}

        def fake_factory(config):
            # build_runtime 对 primary/fallback 各调一次——收集全量，主链取首个
            seen.setdefault("names", []).append(config.model_name)
            return ScriptedModel(responses=[AIMessage(content="ok")])

        with patch("agent_harness.assembly.create_chat_model", side_effect=fake_factory):
            resp = catalog_client.post(
                "/api/sessions", json={"task": "hi", "model": "qwen-max"})
        assert resp.status_code == 200
        assert seen["names"][0] == "qwen3.8-max-0902"
        assert "glm-4.5-air" in seen["names"], "fallback 链不受 model 选择影响（ADR-0014）"

    def test_no_model_param_keeps_default_chain(self, catalog_client):
        """不传 model = 默认链（现行为不变）。"""
        seen: dict = {}

        def fake_factory(config):
            seen.setdefault("names", []).append(config.model_name)
            return ScriptedModel(responses=[AIMessage(content="ok")])

        with patch("agent_harness.assembly.create_chat_model", side_effect=fake_factory):
            resp = catalog_client.post("/api/sessions", json={"task": "hi"})
        assert resp.status_code == 200
        assert seen["names"][0] == "deepseek-chat"

    def test_unknown_model_422(self, catalog_client):
        with patch("agent_harness.assembly.create_chat_model",
                   return_value=ScriptedModel(responses=[AIMessage(content="ok")])):
            resp = catalog_client.post(
                "/api/sessions", json={"task": "hi", "model": "no-such"})
        assert resp.status_code == 422
        assert "no-such" in resp.text
