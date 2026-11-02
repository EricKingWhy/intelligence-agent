"""#1441 §8（F-18b②）：`pending()` 的租约边界——恰好等于 cutoff 的行要回收。

既有两例（`test_1353_ac7_approval.py` 的 stale / live）分别钉在 cutoff 两侧一秒，
把"≤ 还是 <"这个边界留给了实现细节。将来有人把 `claimed_at<=?` 改成 `<`，两例都不会红，
而语义会变成"过期那一刻的行还要再等一个回收周期"。这里用冻结时钟把两边钉死：

- `claimed_at == cutoff`（正好到期）→ **回收**；
- `claimed_at == cutoff + 1s`（租约内）→ **不回收**。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from agent_harness.memory.v2 import write_approval
from agent_harness.memory.v2._sqlite import stamp
from agent_harness.memory.v2.commit import build_markdown_commit
from agent_harness.memory.v2.file_state import write_json
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.memory.v2.write_approval import (
    APPROVING_LEASE,
    AWAITING,
    PENDING,
    StagedWriteStore,
)

LOCAL = TrustedMemoryIdentity(tenant_id="local", user_id="local")
_FROZEN_NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    """把模块里的 `datetime.now()` 钉在 `_FROZEN_NOW`——cutoff 才算得准。"""

    @classmethod
    def now(cls, tz=None):
        return _FROZEN_NOW if tz is None else _FROZEN_NOW.astimezone(tz)


@pytest_asyncio.fixture()
async def approval(tmp_path):
    markdown = MarkdownMemoryStore(tmp_path / "memories")
    commit = build_markdown_commit(markdown, tmp_path)
    assert (await commit.add("memory", "alpha preference"))["success"]
    staged = StagedWriteStore(commit.root, commit)
    await staged.initialize()
    return staged


def _claim(staged: StagedWriteStore, record_id: str, claimed_at: datetime) -> None:
    """模拟被 SIGKILL 的 approve：抢到 `approving` 后把 `claimed_at` 写成指定值。"""
    path = staged.path_for(record_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["claimed_at"] = stamp(claimed_at)
    write_json(path, payload)


async def _staged_awaiting(staged: StagedWriteStore) -> str:
    record = await staged.stage(
        {"target": "memory", "action": "replace",
         "old_text": "alpha preference", "content": "beta preference"},
        ["alpha preference"], LOCAL)
    path = staged.path_for(record.id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = AWAITING
    payload["claimed_at"] = stamp(_FROZEN_NOW)
    write_json(path, payload)
    return record.id


@pytest.mark.asyncio
async def test_row_exactly_at_the_cutoff_is_reclaimed(approval, monkeypatch) -> None:
    monkeypatch.setattr(write_approval, "datetime", _FrozenDatetime)
    record_id = await _staged_awaiting(approval)
    _claim(approval, record_id, _FROZEN_NOW - APPROVING_LEASE)

    assert record_id in {item.id for item in await approval.pending(LOCAL)}
    assert (await approval.get(record_id, LOCAL)).status == PENDING


@pytest.mark.asyncio
async def test_row_one_second_inside_the_lease_is_kept(approval, monkeypatch) -> None:
    monkeypatch.setattr(write_approval, "datetime", _FrozenDatetime)
    record_id = await _staged_awaiting(approval)
    _claim(approval, record_id, _FROZEN_NOW - APPROVING_LEASE + timedelta(seconds=1))

    assert record_id not in {item.id for item in await approval.pending(LOCAL)}
    assert (await approval.get(record_id, LOCAL)).status == AWAITING
