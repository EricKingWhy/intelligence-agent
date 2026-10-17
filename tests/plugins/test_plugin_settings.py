"""#1200：plugin-settings.json 持久化原语。

钉死三件事：
1. 文件缺失 = 全默认（缺省态不是故障）；损坏/超限/坏形状响亮失败。
2. 每个 setter 只动自己那一格，round-trip 后其余字段不变。
3. 原子写不留临时文件；重复写幂等。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.plugins.settings import (
    MAX_SETTINGS_BYTES,
    PluginSettings,
    PluginSettingsError,
    load_plugin_settings,
    mutate_plugin_settings,
    set_require_confirm,
    set_server_enabled,
    set_skill_enabled,
    set_tool_enabled,
    settings_path,
)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    return tmp_path / "ws"


def test_missing_file_is_all_defaults(ws: Path):
    settings = load_plugin_settings(ws)
    assert settings == PluginSettings()
    assert settings.server_enabled("playwright") is True
    assert settings.tool_enabled("playwright", "browser_upload") is True
    assert settings.requires_confirmation("github") is False
    assert settings.skill_enabled("code-review") is True
    assert settings.skill_disabled_names() == frozenset()


def test_corrupt_json_is_loud(ws: Path):
    path = settings_path(ws)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PluginSettingsError):
        load_plugin_settings(ws)


def test_oversize_is_loud(ws: Path):
    path = settings_path(ws)
    path.parent.mkdir(parents=True)
    path.write_text(" " * (MAX_SETTINGS_BYTES + 1), encoding="utf-8")
    with pytest.raises(PluginSettingsError):
        load_plugin_settings(ws)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"disabled_servers": "filesystem"},
        {"disabled_tools": []},
        {"disabled_tools": {"playwright": "browser_upload"}},
        {"skill_overrides": []},
        {"skill_overrides": {"code-review": {"enabled": "no"}}},
        {"require_confirm": [1]},
    ],
)
def test_bad_shape_is_loud(ws: Path, payload):
    path = settings_path(ws)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PluginSettingsError):
        load_plugin_settings(ws)


def test_setters_round_trip_and_are_independent(ws: Path):
    set_server_enabled(ws, "filesystem", False)
    set_tool_enabled(ws, "playwright", "browser_upload", False)
    set_require_confirm(ws, "github", True)
    set_skill_enabled(ws, "code-review", False)

    settings = load_plugin_settings(ws)
    assert settings.disabled_servers == frozenset({"filesystem"})
    assert settings.disabled_tools == {"playwright": frozenset({"browser_upload"})}
    assert settings.require_confirm == frozenset({"github"})
    assert settings.skill_overrides == {"code-review": False}
    assert settings.skill_disabled_names() == frozenset({"code-review"})

    on_disk = json.loads(settings_path(ws).read_text(encoding="utf-8"))
    assert on_disk == {
        "disabled_servers": ["filesystem"],
        "disabled_tools": {"playwright": ["browser_upload"]},
        "require_confirm": ["github"],
        "skill_overrides": {"code-review": {"enabled": False}},
    }


def test_toggles_are_reversible(ws: Path):
    set_server_enabled(ws, "filesystem", False)
    set_server_enabled(ws, "filesystem", True)
    set_tool_enabled(ws, "playwright", "browser_upload", False)
    set_tool_enabled(ws, "playwright", "browser_upload", True)
    set_require_confirm(ws, "github", True)
    set_require_confirm(ws, "github", False)
    set_skill_enabled(ws, "code-review", False)
    set_skill_enabled(ws, "code-review", True)

    settings = load_plugin_settings(ws)
    assert settings == PluginSettings()
    # 空桶不留残渣
    assert json.loads(settings_path(ws).read_text(encoding="utf-8")) == {
        "disabled_servers": [],
        "disabled_tools": {},
        "require_confirm": [],
        "skill_overrides": {},
    }


def test_atomic_write_leaves_no_temp_files(ws: Path):
    set_server_enabled(ws, "filesystem", False)
    leftovers = [p.name for p in ws.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    assert settings_path(ws).exists()


def test_mutate_returns_persisted_snapshot(ws: Path):
    result = mutate_plugin_settings(ws, lambda current: PluginSettings(
        disabled_servers=current.disabled_servers | {"github"},
        disabled_tools=current.disabled_tools,
        require_confirm=current.require_confirm,
        skill_overrides=current.skill_overrides,
    ))
    assert result.disabled_servers == frozenset({"github"})
    assert load_plugin_settings(ws) == result


def test_mutate_surfaces_io_error(ws: Path, monkeypatch):
    def boom(_current: PluginSettings) -> PluginSettings:
        raise PluginSettingsError("nope")

    with pytest.raises(PluginSettingsError):
        mutate_plugin_settings(ws, boom)
    assert not settings_path(ws).exists()
