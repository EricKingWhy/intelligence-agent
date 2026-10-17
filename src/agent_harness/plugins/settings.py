"""#1200：插件管理选择状态的 workspace 级持久化。

定位：这是**运行时选择状态**（禁用哪些 server / 工具 / skill、哪些 server 调用前
需确认），不是配置源。ADR-0010 禁止第二份 YAML 配置源；本文件与
`plugin-installs.json` / `mcp-plugin-installs.json` 同族——由管理 UI 写入、
wiring 读取，二者都不把它当 provider/凭据配置。

形状（票面 §5 / 裁决 C1）：

    {
      "disabled_servers": ["filesystem"],
      "disabled_tools": {"playwright": ["browser_upload"]},
      "require_confirm": ["github"],
      "skill_overrides": {"code-review": {"enabled": false}}
    }

写路径：`registry_lock`（跨进程）内 read-modify-write，tempfile + os.replace
原子落盘（与 `SkillPackageManager._write_manifest` 同一套做法）。
读路径：文件缺失 = 全默认（缺省态不是故障）；文件损坏 = `PluginSettingsError`
响亮失败，由调用方决定降级（wiring 记 warning 用默认；Web 端点如实 500）。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from agent_harness.skills.package_lock import registry_lock

PLUGIN_SETTINGS_FILENAME = "plugin-settings.json"

#: 读入上限（1 MiB）：workspace 是模型可写面，不能让一个被替换成超大的
#: settings 文件在装配期打爆内存（与 SKILL_FILE_MAX_BYTES 同一威胁模型）。
MAX_SETTINGS_BYTES = 1_000_000


class PluginSettingsError(ValueError):
    """settings 文件读不出 / 不合形状 / 写不进去。"""


@dataclass(frozen=True, slots=True)
class PluginSettings:
    """一次读取的不可变快照（wiring 与 Web 端点各读各的，文件即真相）。"""

    disabled_servers: frozenset[str] = frozenset()
    disabled_tools: Mapping[str, frozenset[str]] = field(default_factory=dict)
    require_confirm: frozenset[str] = frozenset()
    skill_overrides: Mapping[str, bool] = field(default_factory=dict)

    def server_enabled(self, name: str) -> bool:
        return name not in self.disabled_servers

    def tool_enabled(self, server: str, tool: str) -> bool:
        return tool not in self.disabled_tools.get(server, frozenset())

    def requires_confirmation(self, server: str) -> bool:
        return server in self.require_confirm

    def skill_enabled(self, name: str) -> bool:
        return self.skill_overrides.get(name, True)

    def skill_disabled_names(self) -> frozenset[str]:
        return frozenset(name for name, enabled in self.skill_overrides.items() if not enabled)

    def to_dict(self) -> dict[str, object]:
        return {
            "disabled_servers": sorted(self.disabled_servers),
            "disabled_tools": {
                server: sorted(tools) for server, tools in sorted(self.disabled_tools.items())
            },
            "require_confirm": sorted(self.require_confirm),
            "skill_overrides": {
                name: {"enabled": enabled}
                for name, enabled in sorted(self.skill_overrides.items())
            },
        }


def settings_path(workspace_dir: str | os.PathLike[str]) -> Path:
    return Path(workspace_dir).expanduser() / PLUGIN_SETTINGS_FILENAME


def _string_set(value: object, field_name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PluginSettingsError(f"{field_name} 必须是字符串数组")
    return frozenset(value)


def _parse(payload: object) -> PluginSettings:
    if not isinstance(payload, dict):
        raise PluginSettingsError("plugin-settings.json 顶层必须是对象")
    raw_tools = payload.get("disabled_tools")
    if raw_tools is None:
        raw_tools = {}
    if not isinstance(raw_tools, dict):
        raise PluginSettingsError("disabled_tools 必须是对象")
    disabled_tools: dict[str, frozenset[str]] = {}
    for server, tools in raw_tools.items():
        if not isinstance(server, str):
            raise PluginSettingsError("disabled_tools 的键必须是 server 名")
        disabled_tools[server] = _string_set(tools, f"disabled_tools.{server}")
    raw_overrides = payload.get("skill_overrides")
    if raw_overrides is None:
        raw_overrides = {}
    if not isinstance(raw_overrides, dict):
        raise PluginSettingsError("skill_overrides 必须是对象")
    skill_overrides: dict[str, bool] = {}
    for name, override in raw_overrides.items():
        if not isinstance(name, str) or not isinstance(override, dict):
            raise PluginSettingsError("skill_overrides 的条目必须是 {<name>: {enabled: bool}}")
        enabled = override.get("enabled")
        if not isinstance(enabled, bool):
            raise PluginSettingsError(f"skill_overrides.{name}.enabled 必须是布尔值")
        skill_overrides[name] = enabled
    return PluginSettings(
        disabled_servers=_string_set(payload.get("disabled_servers"), "disabled_servers"),
        disabled_tools=disabled_tools,
        require_confirm=_string_set(payload.get("require_confirm"), "require_confirm"),
        skill_overrides=skill_overrides,
    )


def load_plugin_settings(workspace_dir: str | os.PathLike[str]) -> PluginSettings:
    """读当前选择状态；文件缺失 = 全默认。损坏/超限显式抛错，不静默吞。"""
    path = settings_path(workspace_dir)
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
    except FileNotFoundError:
        return PluginSettings()
    except OSError as error:
        raise PluginSettingsError(f"无法读取 {path}: {error}") from error
    if len(raw) > MAX_SETTINGS_BYTES:
        raise PluginSettingsError(f"{path} 超过 {MAX_SETTINGS_BYTES} 字节上限")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PluginSettingsError(f"{path} 不是合法 JSON: {error}") from error
    return _parse(payload)


def load_plugin_settings_or_default(
    workspace_dir: str | os.PathLike[str],
) -> tuple[PluginSettings, str | None]:
    """读选择态；坏文件按全默认降级并返回错误文本（调用方决定告警/呈现）。

    三处装配/只读投影（wiring ×2、`GET /api/skills`、`GET /api/mcp/servers`）
    共用这一份降级语义：文件损坏是"选不出"，不是"全禁用"，更不该 500。
    """
    try:
        return load_plugin_settings(workspace_dir), None
    except PluginSettingsError as error:
        return PluginSettings(), str(error)


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def mutate_plugin_settings(
    workspace_dir: str | os.PathLike[str],
    mutate: Callable[[PluginSettings], PluginSettings],
) -> PluginSettings:
    """锁内 read-modify-write；返回落盘后的快照。

    锁与 `plugin-installs.json` / Skill 写路径共用同一把跨进程锁（同一把
    `registry_lock`），不同文件的锁按路径区分，互不干扰。
    """
    path = settings_path(workspace_dir)
    try:
        with registry_lock(path):
            current = load_plugin_settings(workspace_dir)
            updated = mutate(current)
            _write_atomic(path, updated.to_dict())
            return updated
    except OSError as error:
        raise PluginSettingsError(f"无法写入 {path}: {error}") from error


def set_server_enabled(
    workspace_dir: str | os.PathLike[str], name: str, enabled: bool
) -> PluginSettings:
    def _mutate(current: PluginSettings) -> PluginSettings:
        disabled = set(current.disabled_servers)
        disabled.discard(name)
        if not enabled:
            disabled.add(name)
        return replace(current, disabled_servers=frozenset(disabled))

    return mutate_plugin_settings(workspace_dir, _mutate)


def set_tool_enabled(
    workspace_dir: str | os.PathLike[str], server: str, tool: str, enabled: bool
) -> PluginSettings:
    def _mutate(current: PluginSettings) -> PluginSettings:
        disabled = {key: set(value) for key, value in current.disabled_tools.items()}
        bucket = disabled.setdefault(server, set())
        bucket.discard(tool)
        if not enabled:
            bucket.add(tool)
        if not bucket:
            disabled.pop(server, None)
        return replace(
            current,
            disabled_tools={key: frozenset(value) for key, value in disabled.items()},
        )

    return mutate_plugin_settings(workspace_dir, _mutate)


def set_require_confirm(
    workspace_dir: str | os.PathLike[str], name: str, require_confirm: bool
) -> PluginSettings:
    def _mutate(current: PluginSettings) -> PluginSettings:
        names = set(current.require_confirm)
        names.discard(name)
        if require_confirm:
            names.add(name)
        return replace(current, require_confirm=frozenset(names))

    return mutate_plugin_settings(workspace_dir, _mutate)


def set_skill_enabled(
    workspace_dir: str | os.PathLike[str], name: str, enabled: bool
) -> PluginSettings:
    def _mutate(current: PluginSettings) -> PluginSettings:
        overrides = dict(current.skill_overrides)
        if enabled:
            overrides.pop(name, None)
        else:
            overrides[name] = False
        return replace(current, skill_overrides=overrides)

    return mutate_plugin_settings(workspace_dir, _mutate)
