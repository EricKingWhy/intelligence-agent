"""`/journey` 的记忆卡片侧（#1353 M10）。

卡片模型、指纹与词法边算法移植自 NousResearch/hermes-agent
``agent/learning_graph.py``（commit b324b1f7ed52369ab4bd3b1a03378729120b0f01，
MIT，Copyright (c) 2025 Nous Research）。上游从 ``MEMORY.md`` / ``USER.md`` 的
文件 mtime 与解析结果建卡片；本仓的数据源是**版本链**（`list_versions(root_id)`），
markdown 仍是当前态——所以卡片描述的是"这条记忆是怎么变成现在这样的"，而不是
"文件什么时候被碰过"。

# 搬过来的三件东西

- ``memory_fingerprint``：``sha256(entry.strip())[:12]``。卡片由**它说的话**标识，
  不是由它坐的位置标识——版本链在一次编辑里会插入新行，裸下标会把别人的卡片
  指成这一张（上游 #119668 的同一条理由）。
- 卡片模型 ``{source, timestamp, title, body, fingerprint}``：``title`` 取首行
  ``[:80]``、``body`` 取 ``[:1200]``、指纹对**整段**取（不是截断后的 body）。
- ``_tokenize`` 与 ``_memory_skill_edges``：``[^a-z0-9]+`` 切分、最短 3 字符、
  每条记忆取 top-4 技能、名字命中权重 6。

# 本期留空的部分

**skill 边**（issue §1 非目标）：本仓还没有"学习到的技能"这一层，`build_journey`
固定以 ``skills=[]`` 调 `_memory_skill_edges` ⇒ ``edges`` 恒为 ``[]``。算法留在
这里而不是删掉，是因为它是 M10 的一半判据（AC 抽查会对着上游锚点读）；接上技能
图时只需把节点列表传进来，不用重写打分。

# 时间戳的偏差（显式）

上游的 ``timestamp`` 是整数（mtime + 条目序号）；本仓的版本链自带 ISO-8601
``created_at``，直接用它。前端按"同一字段可排序"消费，换单位没有语义损失，但
**不要**把它当 Unix 秒——两者混用会让时间线整体错位。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

from agent_harness.memory.v2.markdown_store import target_for
from agent_harness.memory.v2.types import MemoryRecordV2

#: 卡片正文截断长度（上游 ``chunk[:1200]``）。
BODY_LIMIT = 1200
#: 卡片标题截断长度（上游 ``first[:80]``）。
TITLE_LIMIT = 80
#: 每条记忆最多连几个技能（上游 ``scored[:4]``）。
MAX_SKILL_EDGES = 4
#: 技能名整体命中时的权重（上游 ``6 if name_lower in text``）。
NAME_HIT_WEIGHT = 6


def memory_fingerprint(entry: str) -> str:
    """条目文本的短稳定摘要，带在节点 id 里（上游同名函数逐行）。

    ``strip()`` 是对齐 markdown 存储层的解析结果：同一段文本在写入侧与读取侧
    必须得到同一个指纹，否则卡片永远"过期"。
    """
    return hashlib.sha256(entry.strip().encode("utf-8")).hexdigest()[:12]


def memory_node_id(card: dict[str, Any], index: int) -> str:
    """``memory:<source>:<index>:<fingerprint>``——位置给这一次出现，文本给身份。"""
    return f"memory:{card['source']}:{index}:{card['fingerprint']}"


def _tokenize(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 3}


def _memory_skill_edges(
    memory_cards: Sequence[dict[str, Any]], skills: Sequence[Any],
) -> list[tuple[str, str]]:
    """每条记忆卡片连 top-4 词法重叠的技能（名字整体命中权重 6）。

    ``skills`` 只要求有 ``.name``；本期传空序列 ⇒ 返回空边（见模块 docstring）。
    """
    edges: list[tuple[str, str]] = []
    skill_meta = [(s.name, _tokenize(s.name), s.name.lower()) for s in skills]
    for idx, card in enumerate(memory_cards):
        text = f"{card.get('title', '')}\n{card.get('body', '')}".lower()
        text_tokens = _tokenize(text)
        scored = sorted(
            (
                (score, name)
                for name, tokens, name_lower in skill_meta
                if (score := (NAME_HIT_WEIGHT if name_lower in text else 0)
                    + len(tokens & text_tokens)) > 0
            ),
            key=lambda item: (-item[0], item[1]),
        )
        edges.extend((memory_node_id(card, idx), name) for _, name in scored[:MAX_SKILL_EDGES])
    return edges


def _source(record: MemoryRecordV2) -> str:
    """卡片来源：分拣规则只认 `markdown_store.target_for` 一处。

    卡片词汇是 ``memory`` / ``profile``（前端契约），权威面词汇是 ``memory`` /
    ``user``（文件名）；这里只做词表映射，不做第二次判断——否则加一档时又会漂移。
    """
    return "profile" if target_for(record.kind.value, record.tier.value) == "user" else "memory"


def _title(content: str) -> str:
    lines = content.splitlines()
    first = re.sub(r"^#{1,6}\s+", "", lines[0].strip()).strip() if lines else ""
    return (first[:TITLE_LIMIT] + "…") if len(first) > TITLE_LIMIT else first


def card_for(record: MemoryRecordV2) -> dict[str, Any]:
    """一条版本 → 一张卡片。指纹对**整段正文**取，不是截断后的 body。"""
    return {
        "source": _source(record),
        "timestamp": record.created_at,
        "title": _title(record.content),
        "body": record.content[:BODY_LIMIT],
        "fingerprint": memory_fingerprint(record.content),
        # 版本链独有：没有这两个字段，卡片点开只能看到"某个版本"，说不出是哪一条。
        "memory_id": record.id,
        "version": record.version,
        "state": record.status.value,
    }


def build_journey(histories: Sequence[Sequence[MemoryRecordV2]]) -> dict[str, Any]:
    """把若干条记忆的版本链折成 `learning_graph` 形状的载荷。

    ``histories`` 是 `list_versions(root_id)` 的返回值（新→旧）。每条版本一张卡片，
    卡片顺序 = 传入顺序（调用方按当前记忆的排序传进来，通常是最新活跃的在前）。
    """
    cards: list[dict[str, Any]] = [card_for(record) for history in histories for record in history]
    # 本期 skill 边留空（issue §1 非目标）；算法本身已移植，接上技能图时改这一个实参。
    memory_edges = _memory_skill_edges(cards, ())
    nodes = [
        {
            "id": memory_node_id(card, index),
            "label": card["title"],
            "kind": "memory",
            "memorySource": card["source"],
            "timestamp": card["timestamp"],
            "category": "memory",
            "useCount": 0,
            "state": card["state"],
            "createdBy": "memory",
            "pinned": False,
        }
        for index, card in enumerate(cards)
    ]
    return {
        "nodes": nodes,
        "edges": [{"source": source, "target": target} for source, target in memory_edges],
        "memory": cards,
        "stats": {
            "memory_nodes": len(cards),
            "memory_skill_edges": len(memory_edges),
            "learned_skills": 0,
            "memories": len(histories),
        },
    }


__all__ = [
    "BODY_LIMIT",
    "MAX_SKILL_EDGES",
    "NAME_HIT_WEIGHT",
    "TITLE_LIMIT",
    "build_journey",
    "card_for",
    "memory_fingerprint",
    "memory_node_id",
]
