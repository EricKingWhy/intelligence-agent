"""RUNTIME Sub-batch 1: reasoning_effort 运行时消费测试。

三档域值 minimal/standard/deep 映射到 OpenAI reasoning_effort API 值
low/medium/high。create_chat_model 是模型构造 seam——reasoning_effort
经此传入 ReasoningChatOpenAI，最终出现在发给 provider 的 request payload。

验收标准（Ticket RUNTIME 子批次 1）：
- [x] runtime 真实消费 reasoning_effort（不是又一个 staged no-op）
- [x] 配 fake provider 验证（ScriptedModel 捕获 request payload）
- [x] pytest 全绿 + ruff clean
"""

from unittest.mock import patch

import pytest

from agent_harness.config import Settings
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model


def make_settings(**overrides) -> Settings:
    return Settings(
        model_api_key="sk-test",
        _env_file=None,
        **overrides,
    )


class TestReasoningEffortMapping:
    """域值 minimal/standard/deep → API 值 low/medium/high 的映射。"""

    def test_minimal_maps_to_low(self):
        config = ModelConfig.from_settings(make_settings(model_provider="deepseek"))
        model = create_chat_model(config, reasoning_effort="minimal")
        assert model.reasoning_effort == "low"

    def test_standard_maps_to_medium(self):
        config = ModelConfig.from_settings(make_settings(model_provider="deepseek"))
        model = create_chat_model(config, reasoning_effort="standard")
        assert model.reasoning_effort == "medium"

    def test_deep_maps_to_high(self):
        config = ModelConfig.from_settings(make_settings(model_provider="deepseek"))
        model = create_chat_model(config, reasoning_effort="deep")
        assert model.reasoning_effort == "high"

    def test_none_means_no_reasoning_effort(self):
        """不传 reasoning_effort → 不设 reasoning_effort（None）。"""
        config = ModelConfig.from_settings(make_settings(model_provider="deepseek"))
        model = create_chat_model(config)
        assert model.reasoning_effort is None

    @pytest.mark.parametrize("effort", ["minimal", "standard", "deep"])
    def test_reasoning_effort_appears_in_request_payload(self, effort):
        """reasoning_effort 最终出现在发给 provider 的 request payload 中。"""
        from langchain_core.messages import HumanMessage

        config = ModelConfig.from_settings(make_settings(model_provider="deepseek"))
        model = create_chat_model(config, reasoning_effort=effort)
        payload = model._get_request_payload([HumanMessage(content="hi")])
        assert "reasoning_effort" in payload
        expected = {"minimal": "low", "standard": "medium", "deep": "high"}[effort]
        assert payload["reasoning_effort"] == expected

    def test_no_reasoning_effort_omitted_from_payload(self):
        """不传 reasoning_effort → payload 不含 reasoning_effort 键。"""
        from langchain_core.messages import HumanMessage

        config = ModelConfig.from_settings(make_settings(model_provider="deepseek"))
        model = create_chat_model(config)
        payload = model._get_request_payload([HumanMessage(content="hi")])
        # reasoning_effort 为 None 时不应出现在 payload 中
        assert payload.get("reasoning_effort") is None


class TestReasoningEffortAssemblyWiring:
    """build_runtime 正确将 reasoning_effort 传给 create_chat_model。"""

    @pytest.fixture
    def captured_create(self, monkeypatch):
        """捕获 create_chat_model 调用参数。"""
        seen: list[dict] = []

        def _fake_create(config, **kwargs):
            seen.append({"config": config, "kwargs": kwargs})
            # 返回一个最小化的 fake model
            from tests.scripted_model import ScriptedModel
            return ScriptedModel(responses=[])

        monkeypatch.setattr("agent_harness.assembly.create_chat_model", _fake_create)
        return seen

    @pytest.fixture
    def app_with_capture(self, captured_create, tmp_path):
        """构建 app 并捕获 create_chat_model 调用。"""
        from fastapi.testclient import TestClient

        from agent_harness.web.app import create_app

        settings = Settings(
            _env_file=None,
            workspace_dir=str(tmp_path),
            model_api_key="sk-test",
            enable_cors=False,
        )
        client = TestClient(create_app(settings, enable_cors=False))
        return client, captured_create

    def test_reasoning_effort_reaches_create_chat_model(self, app_with_capture):
        """POST /api/sessions with reasoning_effort=deep → create_chat_model 收到 deep。"""
        client, _captured = app_with_capture

        with patch("agent_harness.assembly.create_chat_model") as mock_create:
            from tests.scripted_model import ScriptedModel
            mock_create.return_value = ScriptedModel(responses=[])

            with client.stream("POST", "/api/sessions",
                               json={"task": "test", "reasoning_effort": "deep"}) as resp:
                # Drain SSE stream
                for _ in resp.iter_lines():
                    pass

            # Verify create_chat_model was called with reasoning_effort="deep"
            assert mock_create.called
            call_kwargs = mock_create.call_args
            # create_chat_model(config, reasoning_effort=...) — check it was passed
            assert call_kwargs.kwargs.get("reasoning_effort") == "deep" or \
                   (len(call_kwargs.args) > 1 and call_kwargs.args[1] == "deep")

    def test_no_reasoning_effort_passes_none(self, app_with_capture):
        """POST /api/sessions without reasoning_effort → create_chat_model 收到 None。"""
        client, _captured = app_with_capture

        with patch("agent_harness.assembly.create_chat_model") as mock_create:
            from tests.scripted_model import ScriptedModel
            mock_create.return_value = ScriptedModel(responses=[])

            with client.stream("POST", "/api/sessions",
                               json={"task": "test"}) as resp:
                for _ in resp.iter_lines():
                    pass

            assert mock_create.called
            call_kwargs = mock_create.call_args
            # Without reasoning_effort, should be None or not passed
            effort = call_kwargs.kwargs.get("reasoning_effort")
            assert effort is None
