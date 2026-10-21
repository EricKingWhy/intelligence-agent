"""#1437：markdown 权威面 → SQLite 派生层的投影通道。

F-01 甲案（用户裁决 2026-10-21）：`memory` 工具的写入也要进派生层，`retrieve_memory`
才搜得到。markdown 仍是权威，SQLite 是派生/历史层——所以这条通道**只读 markdown、
只写 SQLite**，绝不回写文件（`capability.create` 的 `_mirror_markdown` 是反方向，
复用它会重复落盘）。

三个部件对应设计稿 §4–§6：

- `project_entry` / `retract_entry`：单条投影与撤回（add / replace / remove 的传播）；
- `apply_write`：把一次已落盘的 markdown 响应翻译成投影动作；失败进 pending 账本；
- `drain_pending` / `reproject_target`：F-06 重试与 F-06b 全量对账。

对账键是 `entry_digest`（sha256），与墓碑的 `content_hashes`、`store._digest` 同一个
函数——不新造第二套摘要。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from agent_harness.memory.v2.markdown_store import (
    MarkdownMemoryStore,
    entry_digest,
    normalize_entry,
)
from agent_harness.memory.v2.policy import find_secret
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.threat_patterns import first_threat_message
from agent_harness.memory.v2.types import (
    CONTENT_MAX_CHARS,
    EvidenceItem,
    MemoryDraftV2,
    MemoryKind,
    MemoryScope,
    MemoryTier,
    SemanticCategory,
    SemanticPayload,
    SourceType,
    TrustedMemoryIdentity,
)

logger = logging.getLogger(__name__)

#: 确定性跳过原因：不进 pending 表，重试也不会变（设计稿 §3.3）。
SKIP_TOO_LONG = "content_too_long"
SKIP_POLICY = "rejected_by_policy"

#: 与 `_RememberV2Args` 的默认值一致（`tools.py`）；不新增配置项。
_IMPORTANCE = 0.8
_STRENGTH = 0.8

_SUBJECT = {"user": "用户", "memory": "环境与约定"}
_CATEGORY = {"user": SemanticCategory.PROFILE, "memory": SemanticCategory.CONSTRAINT}


def draft_for(
    target: str, content: str, *, session_id: str | None = None, event_id: str | None = None,
) -> MemoryDraftV2:
    """把一条 markdown 条目折成 `MemoryDraftV2`（设计稿 §3.1 逐字段表）。

    `tier` 对两个 target 都是 `collection`：`store.keyword_search` 硬过滤
    `tier='collection'`，`profile` 行对 `retrieve_memory` 永久不可见。来源类型取
    `USER_EDIT`：markdown 是用户自己维护的权威清单，且只有它能合法携带空
    `source_event_ids`（无 session 的直调 / 审批重放场景）。
    """
    # provenance 豁免是**合取**：有 session 就必须指得出事件。拿不到事件时两者一起置空。
    session = session_id if event_id else None
    return MemoryDraftV2(
        kind=MemoryKind.SEMANTIC,
        tier=MemoryTier.COLLECTION,
        scope=MemoryScope.USER_GLOBAL,
        project_id=None,
        content=content,
        payload=SemanticPayload(
            kind="semantic", subject=_SUBJECT[target], fact=content, category=_CATEGORY[target],
        ),
        importance=_IMPORTANCE,
        strength=_STRENGTH,
        source_type=SourceType.USER_EDIT,
        source_session_id=session,
        source_event_ids=[event_id] if event_id else [],
        evidence=[EvidenceItem(
            role="user" if event_id else "memory_tool",
            excerpt=content[:300],
            hash=entry_digest(content),
        )],
    )


def _skip_reason(content: str) -> str | None:
    """确定性拒绝（重试无意义）：超长 / 凭证 / 注入样本。"""
    if len(content) > CONTENT_MAX_CHARS:
        return SKIP_TOO_LONG
    if find_secret(content) is not None:
        return SKIP_POLICY
    if first_threat_message(content, scope="strict") is not None:
        return SKIP_POLICY
    return None


async def project_entry(
    store: SqliteMemoryV2Store, target: str, content: str,
    trusted: TrustedMemoryIdentity, *, session_id: str | None = None,
    event_id: str | None = None,
) -> str:
    """投影一条条目；返回 ``created`` / ``existing`` / ``skipped``。

    ``existing`` 覆盖两种情形：已投影过（幂等），以及同一文本已在**另一个** target 上
    投影过（跨文件去重——两行会让 `retrieve_memory` 返回两句一模一样的话）。
    """
    content = normalize_entry(content)
    if not content or _skip_reason(content) is not None:
        return "skipped"
    if await store.find_active_by_digest(trusted, entry_digest(content)) is not None:
        return "existing"
    await store.create(
        draft_for(target, content, session_id=session_id, event_id=event_id), trusted,
        projection_source="tool", projection_target=target,
    )
    return "created"


async def retract_entry(
    store: SqliteMemoryV2Store, target: str, content: str,
    trusted: TrustedMemoryIdentity, *, reason: str,
) -> bool:
    """撤回一条**工具来源** active 行（落 tombstone）；服务来源行即使内容相同也不动。"""
    content = normalize_entry(content)
    if not content:
        return False
    record = await store.find_tool_projection(trusted, target, entry_digest(content))
    if record is None:
        return False
    try:
        await store.delete(record.id, trusted, reason=reason)
    except KeyError:
        return False
    return True


class _Summary(dict):
    """投影结果汇总；`pending` 只在有失败时置真。"""

    def __init__(self) -> None:
        super().__init__(created=0, deleted=0, skipped=[], pending=False)

    def skipped_add(self, target: str, content: str, reason: str) -> None:
        self["skipped"].append([target, entry_digest(content), reason])


async def _project(
    store: SqliteMemoryV2Store, markdown: MarkdownMemoryStore, target: str, content: str,
    trusted: TrustedMemoryIdentity, *, session_id: str | None, event_id: str | None,
    summary: _Summary,
) -> None:
    """投影一条并处理失败：先写 pending 账本，成功再删（write-ahead，F-06）。

    确定性跳过**不进**账本——账本只表达「可能重试成功」。
    """
    content = normalize_entry(content)
    if not content:
        return
    if (reason := _skip_reason(content)) is not None:
        summary.skipped_add(target, content, reason)
        return
    digest = entry_digest(content)
    await store.enqueue_projection(trusted, target, content, session_id=session_id, event_id=event_id)
    try:
        outcome = await project_entry(
            store, target, content, trusted, session_id=session_id, event_id=event_id)
    except Exception as error:  # noqa: BLE001 — 派生层失败不得回滚权威写入
        summary["pending"] = True
        logger.warning(
            "memory projection pending (%s/%s): %s", target, digest[:12], type(error).__name__)
        await store.record_projection_failure(trusted, target, digest, type(error).__name__)
        return
    await store.clear_projection(trusted, target, digest)
    summary["created"] += outcome == "created"


async def apply_write(
    store: SqliteMemoryV2Store, markdown: MarkdownMemoryStore, target: str,
    response: dict[str, Any], trusted: TrustedMemoryIdentity, *,
    operations: Sequence[dict[str, Any]] | None = None, content: str | None = None,
    session_id: str | None = None, event_id: str | None = None,
) -> dict[str, Any]:
    """把一次已落盘的 markdown 写入传播到派生层（设计稿 §6）。

    `operations` 非空时按原始顺序逐条处理——同一批里「先 remove 再 add」的最终态必须
    与 markdown 一致。`drift_backup` 存在时改为先 drain 再全量对账：漂移意味着文件被
    外部改过，局部传播只覆盖本次写入，覆盖不了外部改动。
    """
    summary = _Summary()
    if not response.get("success"):
        return summary

    if response.get("drift_backup"):
        await drain_pending(store, markdown, trusted)
        full = await reproject_target(store, markdown, trusted, target)
        summary["created"] += full["created"]
        summary["deleted"] += full["deleted"]
        summary["skipped"].extend(full["skipped"])
        summary["pending"] = full["pending"]
        return summary

    if operations is not None:
        replaced = response.get("replaced_entries") or {}
        removed = response.get("removed_entries") or {}
        for index, operation in enumerate(operations, 1):
            action = operation.get("action")
            if index in removed:
                if await retract_entry(store, target, removed[index], trusted,
                                       reason="markdown_removed"):
                    summary["deleted"] += 1
            if index in replaced:
                if await retract_entry(store, target, replaced[index], trusted,
                                       reason="markdown_replaced"):
                    summary["deleted"] += 1
            if action in ("add", "replace"):
                await _project(
                    store, markdown, target, operation.get("content") or "", trusted,
                    session_id=session_id, event_id=event_id, summary=summary)
        return summary

    if removed_entry := response.get("removed_entry"):
        if await retract_entry(store, target, removed_entry, trusted, reason="markdown_removed"):
            summary["deleted"] += 1
        return summary
    if replaced_entry := response.get("replaced_entry"):
        if await retract_entry(store, target, replaced_entry, trusted,
                               reason="markdown_replaced"):
            summary["deleted"] += 1
    if content:
        await _project(
            store, markdown, target, content, trusted,
            session_id=session_id, event_id=event_id, summary=summary)
    return summary


async def drain_pending(
    store: SqliteMemoryV2Store, markdown: MarkdownMemoryStore,
    trusted: TrustedMemoryIdentity,
) -> dict[str, Any]:
    """重试待投影账本；markdown 已没有该条目的行直接丢弃（权威说没有就是没有）。

    收敛判据：`remaining == 0` 且每个 markdown 条目都有一条 active 派生行（或出现在
    `skipped` 里）。行数在快照时就固定，所以循环天然有界。
    """
    drained = 0
    errors: list[list[str]] = []
    for row in await store.pending_projections(trusted):
        target = str(row["target"])
        content = str(row["content"])
        digest = str(row["content_digest"])
        if normalize_entry(content) not in {normalize_entry(e) for e in markdown.entries(target)}:
            await store.clear_projection(trusted, target, digest)
            continue
        try:
            await project_entry(
                store, target, content, trusted,
                session_id=row["session_id"], event_id=row["event_id"],
            )
        except Exception as error:  # noqa: BLE001 — 一条失败不能挡住后面的行
            errors.append([target, digest, type(error).__name__])
            await store.record_projection_failure(trusted, target, digest, type(error).__name__)
            continue
        await store.clear_projection(trusted, target, digest)
        drained += 1
    remaining = len(await store.pending_projections(trusted))
    return {"drained": drained, "remaining": remaining, "errors": errors}


async def reproject_target(
    store: SqliteMemoryV2Store, markdown: MarkdownMemoryStore,
    trusted: TrustedMemoryIdentity, target: str,
) -> dict[str, Any]:
    """F-06b：把派生层的工具来源行与 markdown 的活条目对账。

    幂等：读的是文件**当前**条目与派生层**当前**状态的差集，不依赖顺序/时间戳/水位。
    删除判定看**两个文件**——同一文本还在另一个文件里就不删，避免 ping-pong。
    """
    summary = _Summary()
    entries = {entry_digest(entry): entry for entry in markdown.entries(target)}
    summary["desired"] = len(entries)
    other_target = "user" if target == "memory" else "memory"
    other = {entry_digest(entry) for entry in markdown.entries(other_target)}
    actual = set(await store.tool_projection_digests(trusted, target))

    for digest, entry in entries.items():
        if digest in actual:
            continue
        await _project(
            store, markdown, target, entry, trusted,
            session_id=None, event_id=None, summary=summary)
    for digest in actual - entries.keys():
        if digest in other:
            continue
        record = await store.find_tool_projection(trusted, target, digest)
        if record is None:
            continue
        try:
            await store.delete(record.id, trusted, reason="markdown_removed")
        except KeyError:
            continue
        summary["deleted"] += 1
    return summary


__all__ = [
    "SKIP_POLICY",
    "SKIP_TOO_LONG",
    "apply_write",
    "draft_for",
    "drain_pending",
    "project_entry",
    "reproject_target",
    "retract_entry",
]
