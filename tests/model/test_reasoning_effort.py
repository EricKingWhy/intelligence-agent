"""Model-declared reasoning effort mappings at the ChatOpenAI construction boundary."""

from __future__ import annotations

import pytest
import json

from agent_harness.config import Settings
from agent_harness.model.config import (
    ConfigError,
    ModelConfig,
    ReasoningEffortCapability,
    REASONING_EFFORT_LEVELS,
)
from agent_harness.model.provider import (
    WIRE_REASONING_EFFORTS,
    create_chat_model,
)


def _harness_levels() -> tuple[str, ...]:
    """Web 层 validator 认的档位（请求体取值的单一事实源）。

    函数内 import：model 测试车道不必在收集期拖起整个 FastAPI app。
    """
    from agent_harness.web.app import REASONING_EFFORT_DESCRIPTIONS

    return tuple(REASONING_EFFORT_DESCRIPTIONS)


def _config() -> ModelConfig:
    return ModelConfig(
        provider="deepseek",
        model_name="deepseek-chat",
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        temperature=0.2,
        reasoning_effort=ReasoningEffortCapability(
            supported=("minimal", "standard", "deep"),
            default="standard",
            wire_mapping={
                "minimal": "minimal", "standard": "medium", "deep": "high",
            },
        ),
    )


def _mapped_config() -> ModelConfig:
    settings = Settings(
        _env_file=None, workspace_dir="/tmp/x", model_api_key="sk-test",
        model_provider="deepseek", model_name="deepseek-chat",
        agent_models=json.dumps([{
            "name": "model-with-custom-effort-map",
            "provider": "deepseek",
            "model_name": "deepseek-r1",
            "reasoning_effort": {
                "supported": ["minimal", "standard"],
                "default": "minimal",
                "wire_mapping": {"minimal": "none", "standard": "low"},
            },
        }]),
    )
    return ModelConfig.resolve_selection(settings, "model-with-custom-effort-map")


def test_create_chat_model_uses_selected_model_wire_mapping():
    model = create_chat_model(_mapped_config(), reasoning_effort="standard")
    assert getattr(model, "reasoning_effort", None) == "low"


@pytest.mark.parametrize("effort", ["deep", "unknown"])
def test_create_chat_model_rejects_unsupported_effort(effort):
    with pytest.raises(ConfigError, match="reasoning_effort"):
        create_chat_model(_mapped_config(), reasoning_effort=effort)


def test_create_chat_model_translates_deep_to_wire_enum():
    """G1：语义档位 "deep" → 线格式 "high"（不能原样发 "deep"）。"""
    model = create_chat_model(_config(), reasoning_effort="deep")
    assert getattr(model, "reasoning_effort", None) == "high"


def test_create_chat_model_translates_standard_to_wire_enum():
    """G1b：语义档位 "standard" → 线格式 "medium"（不能原样发 "standard"）。"""
    model = create_chat_model(_config(), reasoning_effort="standard")
    assert getattr(model, "reasoning_effort", None) == "medium"


def test_create_chat_model_no_reasoning_effort_by_default():
    """G2：不传 reasoning_effort → 模型的 reasoning_effort 为 None。"""
    model = create_chat_model(_config())
    assert getattr(model, "reasoning_effort", None) is None


def test_create_chat_model_none_reasoning_effort():
    """G3：传 reasoning_effort=None → 同默认。"""
    model = create_chat_model(_config(), reasoning_effort=None)
    assert getattr(model, "reasoning_effort", None) is None


def test_create_chat_model_minimal_reasoning_effort():
    """G4：语义档位 "minimal" 线格式同名。"""
    model = create_chat_model(_config(), reasoning_effort="minimal")
    assert getattr(model, "reasoning_effort", None) == "minimal"


def test_every_harness_level_maps_into_legal_wire_enum():
    """G4b（invariant）：**任何** harness 档位翻译后都必须落在 provider 合法枚举内。

    `deep` 这档曾经被原样发出去，导致真实会话每一次 run 都 400。这条断言把
    「不允许再出现非法字面量」变成结构性约束——档位集合直接取自 Web 层
    validator 的事实源（不在这里再抄一份名单），新增档位忘了翻译表就红。
    """
    for level in _harness_levels():
        model = create_chat_model(_config(), reasoning_effort=level)
        wire = getattr(model, "reasoning_effort", None)
        assert wire in WIRE_REASONING_EFFORTS, (
            f"harness 档位 {level!r} 翻译成了非法线格式字面量 {wire!r}——"
            f"provider 会返回 400。合法集合：{sorted(WIRE_REASONING_EFFORTS)}"
        )


def test_web_effort_levels_match_model_catalog_vocabulary():
    assert set(_harness_levels()) == set(REASONING_EFFORT_LEVELS)
