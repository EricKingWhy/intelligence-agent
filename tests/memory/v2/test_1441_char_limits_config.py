"""#1441 §1（F-09）：容量预算从部署配置注入 markdown store。

票面判据：`memory_char_limit` / `user_char_limit` 落到记忆配置段（实际载体是
`config.Settings`——`MemorySettingsV2` 是按用户的库内冻结设置，不是部署配置），
缺省 2200 / 1375，唯一注入点是 `assembly.build_memory_v2_service`。

这里钉两件事：
1. 缺省值就是 2200 / 1375（不是复制粘贴的常量巧合，`MarkdownMemoryStore` 的默认值
   与配置默认值必须同值——两处漂移时以配置为准，本用例会红）；
2. 改配置后 store 的上限**随之变化**，且真的参与写入判定（不是只挂在属性上）。
"""

from __future__ import annotations

import pytest

from agent_harness.config import Settings
from agent_harness.memory.v2.assembly import build_memory_v2_service

pytestmark = pytest.mark.asyncio


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(_env_file=None, workspace_dir=str(tmp_path), **overrides)


async def test_defaults_are_2200_and_1375(tmp_path) -> None:
    settings = _settings(tmp_path)
    assert settings.memory_char_limit == 2200
    assert settings.user_char_limit == 1375

    service = await build_memory_v2_service(settings)
    markdown = service.markdown
    assert markdown is not None, "装配层必须注入 markdown 权威面"
    assert markdown.memory_char_limit == 2200
    assert markdown.user_char_limit == 1375


async def test_configured_limits_reach_the_store_and_gate_writes(tmp_path) -> None:
    service = await build_memory_v2_service(
        _settings(tmp_path, memory_char_limit=40, user_char_limit=20),
    )
    markdown = service.markdown
    assert markdown is not None
    assert (markdown.memory_char_limit, markdown.user_char_limit) == (40, 20)

    # 上限不是装饰：40 字符的 MEMORY.md 收得下 39，收不下 41。
    assert (await markdown.add("memory", "m" * 39))["success"] is True
    assert (await markdown.add("memory", "n" * 3))["success"] is False
    assert (await markdown.add("user", "u" * 20))["success"] is True
    assert (await markdown.add("user", "v"))["success"] is False
