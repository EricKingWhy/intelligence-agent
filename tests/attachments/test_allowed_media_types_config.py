"""#822 MM-01：`attachment_allowed_media_types` 的配置校验落在**启动期**。

审查 P3-4：此前校验只在 `resolve_image_limits`（请求路径）跑，配错会让**每次上传**
返回 500，而不是启动即失败。现在 `Settings` 的 `field_validator` 在构造期就校验，
与同族 `Field(ge=1)` 同风格；解析规则单点仍是 `attachments.types.parse_allowed_media_types`。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent_harness.attachments import resolve_image_limits
from agent_harness.config import Settings


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, model_api_key="sk-test", **overrides)


def test_default_allowed_media_types_pass_startup_validation() -> None:
    limits = resolve_image_limits(_settings())
    assert limits.media_types == ("image/png", "image/jpeg", "image/webp", "image/gif")


def test_unknown_media_type_fails_at_settings_construction() -> None:
    with pytest.raises(ValidationError):
        _settings(attachment_allowed_media_types="image/png,image/bmp")


def test_empty_media_types_fails_at_settings_construction() -> None:
    with pytest.raises(ValidationError):
        _settings(attachment_allowed_media_types=" , ")


def test_duplicates_are_collapsed_in_order() -> None:
    settings = _settings(attachment_allowed_media_types="image/png, image/png,image/jpeg")
    assert resolve_image_limits(settings).media_types == ("image/png", "image/jpeg")


def test_image_projection_config_defaults() -> None:
    """#823 / MM-02（B2/B4）：投影相关配置键有 DSH 一组默认值。"""
    settings = _settings()
    assert settings.image_detail == "auto"
    assert settings.image_normalize_max_dimension == 2048
    assert settings.image_normalize_max_bytes == 4 * 1024 * 1024


def test_image_projection_config_overrides() -> None:
    settings = _settings(
        image_detail="low",
        image_normalize_max_dimension=1024,
        image_normalize_max_bytes=512 * 1024,
    )
    assert settings.image_detail == "low"
    assert settings.image_normalize_max_dimension == 1024
    assert settings.image_normalize_max_bytes == 512 * 1024


# --- #939 M-27：展示名（`name`）的真实来源 ---


def test_image_reference_carries_the_display_name() -> None:
    """引用形状**能**带展示名；不传即缺省（`name` 是可空字段，不是恒缺省）。"""
    from agent_harness.attachments import image_reference

    without = image_reference(
        "sha256:" + "a" * 64, "image/png", size=10, width=2, height=3
    )
    assert "name" not in without  # `exclude_none=True`：缺省就是字段不出现

    with_name = image_reference(
        "sha256:" + "a" * 64, "image/png", size=10, width=2, height=3,
        name="/tmp/shots/cat.png",
    )
    # 展示名必须**去掉本地路径**（`file_leaf_name` 是唯一消毒点）
    assert with_name["name"] == "cat.png"
