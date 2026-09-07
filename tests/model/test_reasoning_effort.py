"""create_chat_model 的 reasoning_effort 注入（RUNTIME 子批次）。

reasoning_effort 是会话级思考深度控制（minimal | standard | deep）。
非 None 时经 model_kwargs → extra_body 传给 API；不支持的 Provider
静默忽略（OpenAI SDK 语义）。不在每次 astream/ainvoke 调用时传递——
构造期注入即可。

覆盖四条契约：
  G1：传 reasoning_effort="deep" → model.model_kwargs 含 {"reasoning_effort": "deep"}；
  G2：不传 reasoning_effort → model.model_kwargs 不含 reasoning_effort；
  G3：传 reasoning_effort=None → 同默认（不含 reasoning_effort）；
  G4：传 reasoning_effort="minimal" → model.model_kwargs["reasoning_effort"] == "minimal"。
"""

from __future__ import annotations

from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model


def _config() -> ModelConfig:
    return ModelConfig(
        provider="deepseek",
        model_name="deepseek-chat",
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        temperature=0.2,
    )


def test_create_chat_model_passes_reasoning_effort():
    """G1：传 reasoning_effort="deep" → 模型拿到 reasoning_effort="deep"。"""
    model = create_chat_model(_config(), reasoning_effort="deep")
    assert getattr(model, "reasoning_effort", None) == "deep"


def test_create_chat_model_no_reasoning_effort_by_default():
    """G2：不传 reasoning_effort → 模型的 reasoning_effort 为 None。"""
    model = create_chat_model(_config())
    assert getattr(model, "reasoning_effort", None) is None


def test_create_chat_model_none_reasoning_effort():
    """G3：传 reasoning_effort=None → 同默认。"""
    model = create_chat_model(_config(), reasoning_effort=None)
    assert getattr(model, "reasoning_effort", None) is None


def test_create_chat_model_minimal_reasoning_effort():
    """G4：传 reasoning_effort="minimal" → 模型拿到 reasoning_effort="minimal"。"""
    model = create_chat_model(_config(), reasoning_effort="minimal")
    assert getattr(model, "reasoning_effort", None) == "minimal"
