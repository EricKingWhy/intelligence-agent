"""#1438 F-13a：审批面的端到端验收（web `/api/memory-pending*`）。

判据逐条对应票面 §5：列出形状、批准真落盘、拒绝语义、pin 失效 → 409 且**保留**记录、
不存在的 id → 404、没有 pin 的 legacy 记录拒绝重放。

复用 `tests/web/test_memory_api.py` 的真 app 装配（真 JWT、真 wiring、真 markdown 权威面）：
审批面要验收的正是"HTTP → `StagedWriteStore` → markdown"这条完整路径，单独搭一套只会
验到测试自己的装配。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.memory.v2._sqlite import connect, stamp
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.memory.v2.write_approval import PENDING, _identity
from tests.web import test_memory_api as _shared
from tests.web.test_memory_api import _ALICE, _auth

# pytest 按**模块属性名**注册 fixture：赋值一次即把 `test_memory_api.memory_app`
# 以原名注册进本模块，复用它那套真 app 装配。
memory_app = _shared.memory_app

_TRUSTED = TrustedMemoryIdentity(_ALICE.tenant_id, _ALICE.user_id)


async def _staged_store(client):
    _registry, wiring = await client.app.state.agent.get_wiring()
    assert wiring.memory_v2 is not None and wiring.memory_v2.staged is not None
    return wiring.memory_v2.staged


async def _stage(client, payload: dict):
    """按真实写入路径 stage：先钉 pin，再入待审区。"""
    staged = await _staged_store(client)
    pins, error = await staged.resolve_pins(payload)
    assert error is None, error
    return await staged.stage(payload, pins, _TRUSTED)


def _memory_file(client) -> Path:
    return Path(client.app.state.agent.settings.workspace_dir) / "memories" / "MEMORY.md"


@pytest.mark.asyncio
async def test_pending_lists_the_staged_write_shape(memory_app):
    client, _ = memory_app
    staged_write = await _stage(client, {
        "target": "memory", "action": "add", "content": "用户喜欢冰美式", "old_text": None})

    response = client.get("/api/memory-pending", headers=_auth(_ALICE))

    assert response.status_code == 200, response.text
    assert response.json() == [{
        "id": staged_write.id,
        "payload": {
            "target": "memory", "action": "add",
            "content": "用户喜欢冰美式", "old_text": None},
        "created_at": staged_write.created_at,
    }]


@pytest.mark.asyncio
async def test_approve_writes_through_to_markdown(memory_app):
    client, _ = memory_app
    staged_write = await _stage(client, {
        "target": "memory", "action": "add", "content": "用户喜欢冰美式", "old_text": None})

    response = client.post(
        f"/api/memory-pending/{staged_write.id}/approve", headers=_auth(_ALICE))

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert "用户喜欢冰美式" in _memory_file(client).read_text(encoding="utf-8")
    assert client.get("/api/memory-pending", headers=_auth(_ALICE)).json() == []


@pytest.mark.asyncio
async def test_reject_discards_without_writing(memory_app):
    client, _ = memory_app
    staged_write = await _stage(client, {
        "target": "memory", "action": "add", "content": "用户喜欢冰美式", "old_text": None})

    response = client.post(
        f"/api/memory-pending/{staged_write.id}/reject", headers=_auth(_ALICE))

    assert response.status_code == 200, response.text
    assert response.json() == {"id": staged_write.id, "status": "rejected"}
    memory_file = _memory_file(client)
    # 拒绝不落盘：文件可能压根没被创建（从未写入过），也可能存在但不含这条。
    assert "用户喜欢冰美式" not in (
        memory_file.read_text(encoding="utf-8") if memory_file.exists() else "")
    assert client.get("/api/memory-pending", headers=_auth(_ALICE)).json() == []


@pytest.mark.asyncio
async def test_approve_after_external_edit_is_409_and_keeps_the_record(memory_app):
    """AC7 的核心在 HTTP 面上：pin 失效 → 409，记录**还在**，文件不被覆盖。"""
    client, _ = memory_app
    memory_file = _memory_file(client)
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("用户喜欢手冲咖啡", encoding="utf-8")
    staged_write = await _stage(client, {
        "target": "memory", "action": "replace",
        "content": "用户喜欢冰美式", "old_text": "手冲咖啡"})

    memory_file.write_text("用户喜欢冷萃咖啡", encoding="utf-8")
    response = client.post(
        f"/api/memory-pending/{staged_write.id}/approve", headers=_auth(_ALICE))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "memory_write_not_applied"
    assert memory_file.read_text(encoding="utf-8") == "用户喜欢冷萃咖啡"
    assert [item["id"] for item in client.get(
        "/api/memory-pending", headers=_auth(_ALICE)).json()] == [staged_write.id]


@pytest.mark.asyncio
async def test_reject_unknown_id_is_404(memory_app):
    client, _ = memory_app
    response = client.post(
        "/api/memory-pending/stg_never_existed/reject", headers=_auth(_ALICE))

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "memory_write_not_pending"


@pytest.mark.asyncio
async def test_legacy_record_without_pin_is_refused_not_reresolved(memory_app):
    """没有 pin 的旧记录不按 old_text 重搜——那正是 TOCTOU 本体。"""
    client, _ = memory_app
    memory_file = _memory_file(client)
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("用户喜欢手冲咖啡", encoding="utf-8")
    payload = {"target": "memory", "action": "replace",
               "content": "用户喜欢冰美式", "old_text": "手冲咖啡"}
    staged = await _staged_store(client)
    async with connect(staged.database_path) as connection:
        await connection.execute(
            "INSERT INTO memory_v2_staged "
            "(id, identity, payload, pinned_entry, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("stg_legacy", _identity(_TRUSTED), json.dumps(payload, ensure_ascii=False),
             None, PENDING, stamp()),
        )
        await connection.commit()

    response = client.post("/api/memory-pending/stg_legacy/approve", headers=_auth(_ALICE))

    assert response.status_code == 409, response.text
    assert "no pinned entry" in response.json()["detail"]["message"]
    assert memory_file.read_text(encoding="utf-8") == "用户喜欢手冲咖啡"
