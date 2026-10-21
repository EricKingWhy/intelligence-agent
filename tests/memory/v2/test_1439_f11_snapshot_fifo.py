"""#1439 F-11（甲案）：快照的失效模型是「按 session 冻结 + 128 个 session 的 FIFO 淘汰」。

# 为什么钉这条

`markdown_store` 的注释曾写「会话开始渲染一次，**会话结束才失效**」——但本仓**没有**
任何 session 结束钩子（`grep invalidate_snapshot` / `_snapshot.clear` 零命中），唯一的
回收路径是 `render_snapshot` 里的插入序 FIFO：缓存满 `SNAPSHOT_CACHE_LIMIT`（128）时
丢最早插入的那一个。甲案的处置是把描述改成真实语义，并**把这条行为变成回归网**：

- 129 个 session 之后，最早那个 session 的快照被淘汰；
- 它下一次渲染**重新读盘**，因此看得见会话中途写入的新条目（注入内容可能在会话中途变更）；
- 还在缓存里的 session 仍然逐字节不变（AC8 的冻结语义在容量内成立）。

AC8 既有 5 例（`test_1353_ac8_snapshot.py`）钉的是容量内的冻结，本文件钉容量边界。
"""

from __future__ import annotations

import pytest

from agent_harness.memory.v2.markdown_store import SNAPSHOT_CACHE_LIMIT
from agent_harness.memory.v2.recall import MemoryV2ContextProvider
from tests.conftest import make_session
from tests.memory.v2._markdown_memory_1353 import build_store, call

pytestmark = pytest.mark.asyncio


def _text(messages) -> str:
    return "\n".join(str(getattr(message, "content", message)) for message in messages)


async def test_the_oldest_session_is_evicted_after_the_limit_and_rereads_disk(tmp_path) -> None:
    store = build_store(tmp_path)
    provider = MemoryV2ContextProvider(store)
    sessions = [make_session(tmp_path / "sessions")]

    assert (await call(store.add, "memory", "first fact"))["success"] is True
    first = _text(await provider.select(sessions[0], 100_000))
    assert "first fact" in first
    frozen_bytes = first.encode("utf-8")
    oldest = sessions[0].session_id

    # 再开 `SNAPSHOT_CACHE_LIMIT` 个 session：总数 129 > 上限 ⇒ 最早的被淘汰。
    for _ in range(SNAPSHOT_CACHE_LIMIT):
        session = make_session(tmp_path / "sessions")
        sessions.append(session)
        await provider.select(session, 100_000)
    assert oldest not in store._snapshot, (
        f"{SNAPSHOT_CACHE_LIMIT + 1} 个 session 之后最早的那个必须已被 FIFO 淘汰"
    )
    assert len(store._snapshot) == SNAPSHOT_CACHE_LIMIT

    # 会话中途写入：还在缓存里的 session 看不到（AC8 的冻结语义）。
    assert (await call(store.add, "memory", "second fact"))["success"] is True
    newest = sessions[-1]
    cached = _text(await provider.select(newest, 100_000))
    assert "second fact" not in cached, "未淘汰的 session 必须保持冻结"

    # 被淘汰的 session 重新渲染 = 重新读盘 ⇒ 新条目出现。这条是「注入可能在会话中途
    # 变更」的可执行版本，也是甲案注释改写的事实依据。
    thawed = _text(await provider.select(sessions[0], 100_000))
    assert "first fact" in thawed and "second fact" in thawed
    assert thawed.encode("utf-8") != frozen_bytes
