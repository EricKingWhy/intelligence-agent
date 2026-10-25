"""#1441 §3（F-13b）：M10 记忆时间线——版本链折叠、指纹、节点 id、截断、`edges=[]`。

数据源是**版本链**（`list_versions`），不是文件 mtime：markdown 是当前态，时间线要回答
"这条记忆是怎么变成现在这样的"。这里用真 SQLite（`SqliteMemoryV2Store` 的 create/update/
invalidate），不手搓 record——版本号、root_id、状态迁移都由存储层给。
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio

from agent_harness.memory.v2.journey import (
    BODY_LIMIT,
    TITLE_LIMIT,
    build_journey,
    card_for,
    memory_fingerprint,
    memory_node_id,
)
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.types import (
    MemoryRecordV2,
    MemoryStatus,
    MemoryTier,
    TrustedMemoryIdentity,
)
from tests.memory.v2._records import make_draft, make_record

_TRUSTED = TrustedMemoryIdentity("local", "local")


@pytest_asyncio.fixture
async def store(tmp_path) -> SqliteMemoryV2Store:
    instance = SqliteMemoryV2Store(tmp_path / "memory-v2.db")
    await instance.initialize()
    return instance


# ---------------------------------------------------------------- 指纹


def test_fingerprint_is_stable_and_ignores_surrounding_whitespace() -> None:
    assert memory_fingerprint("用户偏好简洁") == memory_fingerprint("  用户偏好简洁\n")
    assert memory_fingerprint("用户偏好简洁") == hashlib.sha256(
        "用户偏好简洁".encode()).hexdigest()[:12]


def test_fingerprint_changes_when_the_words_change() -> None:
    """指纹由**它说的话**标识，不是位置：改一个字就是另一张卡片。"""
    assert memory_fingerprint("用户偏好简洁") != memory_fingerprint("用户偏好冗长")


# ---------------------------------------------------------------- 节点 id


def test_node_id_shape_is_source_index_fingerprint() -> None:
    card = {"source": "profile", "fingerprint": "abcdef123456"}
    assert memory_node_id(card, 3) == "memory:profile:3:abcdef123456"


# ---------------------------------------------------------------- 卡片截断


def test_body_truncates_but_fingerprint_covers_the_whole_text() -> None:
    # `CONTENT_MAX_CHARS=500 < BODY_LIMIT=1200`，合法记录到不了 1200——截断是上游
    # 形状的对齐防线，不是可达路径。`model_copy` 不重跑校验，正好用来钉这条防线。
    content = "标题\n" + "正文" * 1000
    card = card_for(make_record().model_copy(update={"content": content}))

    assert len(card["body"]) == BODY_LIMIT
    # 指纹对**整段**取，不是截断后的 body——否则两段不同记忆会撞成同一个指纹。
    assert card["fingerprint"] == memory_fingerprint(content)
    assert card["fingerprint"] != memory_fingerprint(card["body"])


def test_title_strips_markdown_heading_and_marks_truncation() -> None:
    short = card_for(make_record(content="## 简短标题\n正文"))["title"]
    assert short == "简短标题"

    long = card_for(make_record(content="## " + "长" * (TITLE_LIMIT + 10)))["title"]
    assert long == "长" * TITLE_LIMIT + "…"


def test_card_keeps_the_version_pointers() -> None:
    record = make_record(memory_id="mem-9", version=3, status=MemoryStatus.SUPERSEDED)
    card = card_for(record)

    assert (card["memory_id"], card["version"], card["state"]) == ("mem-9", 3, "superseded")
    assert card["timestamp"] == record.created_at


# ---------------------------------------------------------------- 版本链折叠


@pytest.mark.asyncio
async def test_version_chain_folds_into_one_card_per_version(store) -> None:
    first = await store.create(make_draft(content="初版：偏好 pnpm"), _TRUSTED)
    second = await store.update(first.id, make_draft(content="改版：偏好 npm"), _TRUSTED)

    payload = build_journey([await store.list_versions(second.root_id, _TRUSTED)])

    assert [card["version"] for card in payload["memory"]] == [2, 1], "新→旧"
    assert [card["state"] for card in payload["memory"]] == ["active", "superseded"]
    assert [card["memory_id"] for card in payload["memory"]] == [second.id, first.id]
    assert payload["memory"][0]["source"] == "memory"
    assert payload["stats"] == {
        "memory_nodes": 2, "memory_skill_edges": 0, "learned_skills": 0, "memories": 1,
    }
    assert [node["id"] for node in payload["nodes"]] == [
        memory_node_id(card, index) for index, card in enumerate(payload["memory"])
    ]


@pytest.mark.asyncio
async def test_profile_tier_records_are_profile_cards(store) -> None:
    record = await store.create(
        make_draft(content="用户叫林舟", tier=MemoryTier.PROFILE), _TRUSTED)
    payload = build_journey([await store.list_versions(record.root_id, _TRUSTED)])

    assert payload["memory"][0]["source"] == "profile"
    assert payload["nodes"][0]["memorySource"] == "profile"


@pytest.mark.asyncio
async def test_edges_are_empty_until_skill_graph_is_wired(store) -> None:
    record = await store.create(make_draft(content="偏好 pnpm"), _TRUSTED)
    payload = build_journey([await store.list_versions(record.root_id, _TRUSTED)])

    assert payload["edges"] == []
    assert payload["stats"]["memory_skill_edges"] == 0


@pytest.mark.asyncio
async def test_invalidated_version_stays_in_the_timeline_with_its_state(store) -> None:
    record = await store.create(make_draft(content="会被撤回的偏好"), _TRUSTED)
    await store.invalidate(record.id, _TRUSTED)

    payload = build_journey([await store.list_versions(record.root_id, _TRUSTED)])

    assert [card["state"] for card in payload["memory"]] == ["invalidated"]
    assert payload["stats"]["memories"] == 1


def test_node_shape_carries_the_card_identity() -> None:
    payload = build_journey([[make_record(content="偏好 pnpm")]])
    node = payload["nodes"][0]

    assert node["kind"] == "memory"
    assert node["category"] == "memory"
    assert node["label"] == payload["memory"][0]["title"]
    assert node["state"] == payload["memory"][0]["state"]
    assert node["id"] == memory_node_id(payload["memory"][0], 0)


def test_empty_input_yields_an_empty_graph() -> None:
    assert build_journey([]) == {
        "nodes": [], "edges": [], "memory": [],
        "stats": {"memory_nodes": 0, "memory_skill_edges": 0,
                  "learned_skills": 0, "memories": 0},
    }


def test_card_shape_matches_the_web_response_model() -> None:
    """`MemoryJourneyCard` 只接受这 8 个键；多一个少一个都是契约破裂。"""
    from agent_harness.web.memory import MemoryJourneyCard

    card = card_for(make_record(content="偏好 pnpm"))
    assert set(card) == {
        "source", "timestamp", "title", "body", "fingerprint",
        "memory_id", "version", "state",
    }
    assert MemoryJourneyCard(**card).model_dump() == card


def test_record_type_is_not_mutated_by_card_building() -> None:
    record: MemoryRecordV2 = make_record(content="偏好 pnpm")
    card_for(record)
    assert record.content == "偏好 pnpm"
