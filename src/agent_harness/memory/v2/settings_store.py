"""#1494 / ADR-0058 D6：settings 的文件 owner（`memory-state/settings.json`）。

# 为什么是单文件、且没有身份路由

ADR-0056 §3.4 把四个开关（extraction / recall / fallback / write_approval）判给「本机设置
职责」。旧载体是 `memory_v2_settings` 的 `PRIMARY KEY (tenant_id, user_id)`——一张**按身份
分片**的表；ADR-0056 裁决 7 同时禁止「改名 `owner_id` 复制三元路由」。所以新格式只存
**显式设置过的键**，读取时不看身份：单机部署里"这台机器的记忆开关"就是全部语义。

# 「缺行用默认」与「显式设默认」必须可区分

旧表的 `INSERT ... ON CONFLICT` 一写就把四个键全部物化，于是「用户从没碰过」和「用户
显式关掉了 extraction」读到同一个 `True`。文件里**只写显式键**：缺键 = 取默认，键在 =
用户的选择。`explicit_keys()` 是这条判据的对外读数（AC2 的"原值可核"）。

# 写序

读 → 合并 → `file_lock` 内重读 → 合并 → 原子替换。全程阻塞 IO，调用方经
``asyncio.to_thread`` 进入；JSON 解析失败**不**当默认值（那会把损坏静默成"用户关了所有
开关"），而是抛 `CorruptStateFile`。
"""

from __future__ import annotations

import asyncio
import json

from agent_harness.memory.v2.file_state import (
    CorruptStateFile,
    MemoryStateRoot,
    file_lock,
    read_json,
    utc_stamp,
    write_json,
)
from agent_harness.memory.v2.types import MemorySettingsV2, TrustedMemoryIdentity

#: 四个显式键；顺序即文件里的稳定输出顺序。
SETTING_KEYS: tuple[str, ...] = (
    "extraction_enabled", "recall_enabled", "fallback_enabled", "write_approval",
)

SCHEMA_VERSION = 1


class FileMemorySettingsStore:
    """`memory-state/settings.json` 的读写。身份参数只为保持调用面一致，**不参与路由**。"""

    def __init__(self, root: MemoryStateRoot) -> None:
        self.root = root

    async def initialize(self) -> None:
        await asyncio.to_thread(self.root.ensure)

    # ------------------------------------------------------------------ 读

    def _read_sync(self) -> dict[str, bool]:
        payload = read_json(self.root.settings_path)
        if payload is None:
            return {}
        settings = payload.get("settings")
        if not isinstance(settings, dict):
            raise CorruptStateFile("settings.json has no object under 'settings'")
        return {
            key: bool(settings[key]) for key in SETTING_KEYS if key in settings
        }

    async def explicit(self) -> dict[str, bool]:
        """用户**显式设置过**的键（缺键 = 从未设置）。"""
        return await asyncio.to_thread(self._read_sync)

    async def get(self, trusted: TrustedMemoryIdentity | None = None) -> MemorySettingsV2:
        explicit = await asyncio.to_thread(self._read_sync)
        defaults = MemorySettingsV2()
        return MemorySettingsV2(
            extraction_enabled=explicit.get("extraction_enabled", defaults.extraction_enabled),
            recall_enabled=explicit.get("recall_enabled", defaults.recall_enabled),
            fallback_enabled=explicit.get("fallback_enabled", defaults.fallback_enabled),
            write_approval=explicit.get("write_approval", defaults.write_approval),
        )

    # ------------------------------------------------------------------ 写

    def _update_sync(self, changes: dict[str, bool]) -> dict[str, bool]:
        with file_lock(self.root.settings_path):
            current = self._read_sync()
            current.update(changes)
            write_json(self.root.settings_path, {
                "schema": SCHEMA_VERSION,
                "settings": current,
                "updated_at": utc_stamp(),
            })
            return current

    async def update(
        self, trusted: TrustedMemoryIdentity | None = None, **changes: bool | None,
    ) -> MemorySettingsV2:
        """只写非 None 的键；``False`` / ``True`` 都是显式取值。"""
        explicit = {
            key: bool(value) for key, value in changes.items()
            if key in SETTING_KEYS and value is not None
        }
        unknown = set(changes) - set(SETTING_KEYS)
        if unknown:
            raise TypeError(f"unknown memory setting(s): {', '.join(sorted(unknown))}")
        if explicit:
            await asyncio.to_thread(self._update_sync, explicit)
        return await self.get(trusted)

    # ------------------------------------------------------------------ 诊断

    def raw(self) -> dict[str, object] | None:
        """原样读数（`memory settings` 诊断用；不含身份）。"""
        payload = read_json(self.root.settings_path)
        if payload is None:
            return None
        return json.loads(json.dumps(payload))


__all__ = ["SCHEMA_VERSION", "SETTING_KEYS", "FileMemorySettingsStore"]
