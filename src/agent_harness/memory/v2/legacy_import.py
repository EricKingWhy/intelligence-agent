"""#1494 / ADR-0058 D9：旧 SQLite 五类操作状态 → 文件 owner 的一次性导入。

# 为什么不是"继续读旧表"

ADR-0056 的终态不许留 Memory-SQLite 例外：切换完成后，审批态/job/settings/tombstone
的**运行时**读写必须只剩文件 owner。旧表里的存量仍要有人接走，否则升级一次就丢
待审写入、被拦下的后台写入与未终结的 job——用户看到的是"记忆还在，但审批队列空了"。

# 顺序与可重入

1. **只读**旧表（`memory_v2_settings / _tombstones / _staged / _blocked / _jobs`）；
2. 逐类写成文件格式（已存在的文件不覆盖：崩溃重跑时先写的那份就是证据）；
3. 旧 `completed + committed` job 的 active 记录**按内容补发布**（`legp_` 回执）——
   这些 job 的记录已提交、markdown 可能因上次进程被杀而没落；`add` 同内容天然去重，
   已发布的不会重复追加；
4. 最后写 `memory-state/legacy-import.json`。标记存在 ⇒ 整个导入跳过。

标记最后写、且每步幂等，所以"导入到一半被杀"只会让下次启动把剩下部分补完，不会
把同一条状态导入两次。

# 不删旧表

导入不 `DELETE` 旧行（销毁未授权）。旧表从此不再被运行时读取；是否清理留给部署方。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

from agent_harness.memory.v2._sqlite import connect
from agent_harness.memory.v2.commit import MemoryCommit
from agent_harness.memory.v2.file_state import (
    CorruptStateFile,
    StateFileError,
    safe_id,
    utc_stamp,
    write_json,
)
from agent_harness.memory.v2.jobs import job_id_for
from agent_harness.memory.v2.markdown_store import MarkdownMemoryStore, target_for
from agent_harness.memory.v2.receipts import ReceiptStore
from agent_harness.memory.v2.settings_store import FileMemorySettingsStore
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.tombstones import FileTombstoneStore
from agent_harness.memory.v2.types import MemorySettingsV2
from agent_harness.memory.v2.write_approval import blocked_id_for

logger = logging.getLogger(__name__)

MARKER_SCHEMA = 1

#: blocked 去重时的状态优先级：已处置（promoted/discarded）优先于仍待处置。
_BLOCKED_RANK = {"blocked": 1, "promoted": 2, "discarded": 2}


async def import_legacy_state(
    store: SqliteMemoryV2Store, commit: MemoryCommit,
) -> dict[str, Any]:
    """把旧库里的五类操作状态搬进 `memory-state/`；返回导入读数。

    标记存在时返回 ``{"skipped": True, "reason": "already_imported"}``；旧库不存在时
    返回 ``{"skipped": True, "reason": "no_legacy_database"}``（不写标记：数据目录可能
    在之后的启动里才出现）。
    """
    root = store.state_root
    if root.legacy_marker_path.exists():
        return {"skipped": True, "reason": "already_imported"}
    database = store.database_path
    if not database.exists():
        return {"skipped": True, "reason": "no_legacy_database"}

    counts: dict[str, Any] = {
        "settings": 0, "settings_rows": 0, "settings_conflicts": 0,
        "settings_existing": 0, "settings_unreadable": 0,
        "tombstones": 0, "tombstones_expired": 0,
        "staged": 0, "staged_existing": 0,
        "blocked": 0, "blocked_existing": 0, "jobs": 0, "jobs_existing": 0,
        "republished": 0, "republish_unreadable": 0, "republish_needs_reconcile": 0,
    }
    converged = True
    async with connect(database) as connection:
        converged &= await _import_settings(connection, root, counts)
        await _import_tombstones(connection, root, counts)
        await _import_staged(connection, root, counts)
        await _import_blocked(connection, root, counts)
        await _import_jobs(connection, root, counts)
        converged &= await _republish_completed(connection, root, commit, counts)

    if not converged:
        # D9：有未收敛项（settings 冲突 / 补发布不可读）⇒ 不写完成标记，下次启动重试。
        logger.warning("memory legacy import not converged: %s", counts)
        return {"skipped": False, "converged": False, **counts}
    write_json(root.legacy_marker_path, {
        "schema": MARKER_SCHEMA,
        "source_database": database.name,
        "imported_at": utc_stamp(),
        "counts": counts,
    })
    logger.info("memory legacy state imported: %s", counts)
    return {"skipped": False, "converged": True, **counts}


# ---------------------------------------------------------------------- 五类导入


async def _import_settings(connection, root, counts: dict[str, Any]) -> bool:
    rows = [dict(row) for row in await _fetch(
        connection, "SELECT * FROM memory_v2_settings ORDER BY updated_at DESC")]
    counts["settings_rows"] = len(rows)
    if not rows:
        return True
    store = FileMemorySettingsStore(root)
    if store.path.exists():
        # 已经写过的文件是证据：崩溃重跑不能覆盖用户在窗口内改过的设置。
        # 但"存在"不等于"可读"：坏文件不能计为收敛并写完成标记，否则旧库 settings
        # 永远不会再导入，运行时却一直 fail-closed（review P3-3）。
        try:
            await store.explicit()
        except (CorruptStateFile, StateFileError) as error:
            counts["settings_unreadable"] += 1
            logger.warning("existing memory settings are unreadable: %s", error)
            return False
        counts["settings_existing"] += 1
        return True
    latest = rows[0]
    defaults = MemorySettingsV2()
    keys = ("extraction_enabled", "recall_enabled", "fallback_enabled", "write_approval")

    def _value(row: dict[str, Any], key: str) -> bool:
        # 旧表可能还没有后加的列：缺列/NULL 按既有默认读，不能把「旧库没表达过」
        # 当成「用户关掉了」。
        raw = row.get(key)
        return getattr(defaults, key) if raw is None else bool(raw)

    resolved = {key: _value(latest, key) for key in keys}
    counts["settings_conflicts"] = sum(
        1 for row in rows
        if any(_value(row, key) != resolved[key] for key in keys)
    )
    if counts["settings_conflicts"]:
        # D9：多行且有差异 ⇒ 不导入、不折叠；落一个可见的冲突文件供人工裁决，
        # 并让 marker 保持未完成（下次启动仍能看到）。
        write_json(root.settings_conflict_path, {
            "schema": 1,
            "detected_at": utc_stamp(),
            "keys": list(keys),
            "distinct_values": {
                key: sorted({_value(row, key) for row in rows}) for key in keys
            },
            "resolution": "编辑 memory-state/settings.json 后删除本文件（或旧库）再启动",
        })
        return False
    await store.update(None, **resolved)
    counts["settings"] = 1
    return True


async def _import_tombstones(connection, root, counts: dict[str, Any]) -> None:
    tombstones = FileTombstoneStore(root)
    now_stamp = utc_stamp()
    for raw_row in await _fetch(connection, "SELECT * FROM memory_v2_tombstones"):
        row = dict(raw_row)
        if str(row.get("expires_at") or "") <= now_stamp:
            # 保留期已过的墓碑按既有策略失效；导入它等于把删除抑制凭空续期 30 天。
            counts["tombstones_expired"] += 1
            continue
        path = tombstones.path_for(str(row["memory_id"]))
        if path.exists():
            continue
        write_json(path, {
            "schema": 1,
            "memory_id": row["memory_id"],
            "root_id": row["root_id"],
            "tenant_id": row["tenant_id"],
            "user_id": row["user_id"],
            "scope": row["scope"],
            "project_id": row["project_id"],
            "version": row.get("version"),
            "deleted_at": row["deleted_at"],
            "expires_at": row["expires_at"],
            "deletion_reason": row["deletion_reason"],
            "content_hashes": _json_list(row["content_hashes"]),
            "source_hashes": _json_list(row["source_hashes"]),
        })
        counts["tombstones"] += 1


async def _import_staged(connection, root, counts: dict[str, Any]) -> None:
    for row in await _fetch(connection, "SELECT * FROM memory_v2_staged"):
        path = root.staged_dir / f"{safe_id(str(row['id']), what='staged id')}.json"
        if path.exists():
            counts["staged_existing"] += 1
            continue
        identity = str(row["identity"])
        project_id = _identity_project(identity)
        pins_raw = row["pinned_entry"]
        write_json(path, {
            "schema": 1,
            "id": row["id"],
            "identity": identity,
            "project_id": project_id,
            "payload": _json_obj(row["payload"]),
            "pinned_entry": None if pins_raw is None else _json_list(pins_raw),
            "status": row["status"],
            "claimed_at": row["claimed_at"],
            "created_at": row["created_at"],
        })
        counts["staged"] += 1


async def _import_blocked(connection, root, counts: dict[str, Any]) -> None:
    chosen: dict[str, dict[str, Any]] = {}
    for row in await _fetch(
        connection, "SELECT * FROM memory_v2_blocked ORDER BY created_at, id"
    ):
        identity = str(row["identity"])
        blocked_id = blocked_id_for(identity, str(row["content_hash"]))
        document = {
            "schema": 1,
            "id": blocked_id,
            "legacy_id": row["id"],
            "job_id": row["job_id"],
            "identity": identity,
            "project_id": row["project_id"],
            "origin": row["origin"],
            "reason": row["reason"],
            "payload": _json_obj(row["payload"]),
            "content_hash": row["content_hash"],
            "status": row["status"],
            "disposition_note": row["disposition_note"],
            "created_at": row["created_at"],
            "disposed_at": row["disposed_at"],
        }
        current = chosen.get(blocked_id)
        if current is None or _BLOCKED_RANK.get(str(row["status"]), 0) > _BLOCKED_RANK.get(
            str(current["status"]), 0
        ):
            chosen[blocked_id] = document
    for blocked_id, document in chosen.items():
        path = root.blocked_dir / f"{safe_id(blocked_id, what='blocked id')}.json"
        if path.exists():
            counts["blocked_existing"] += 1
            continue
        write_json(path, document)
        counts["blocked"] += 1


async def _import_jobs(connection, root, counts: dict[str, Any]) -> None:
    for raw_row in await _fetch(connection, "SELECT * FROM memory_v2_jobs"):
        row = dict(raw_row)
        job_id = job_id_for(str(row["idempotency_key"]))
        path = root.jobs_dir / f"{safe_id(job_id, what='job id')}.json"
        if path.exists():
            counts["jobs_existing"] += 1
            continue
        candidates_raw = row.get("protected_fact_extraction_candidates")
        write_json(path, {
            "schema": 1,
            "job_id": job_id,
            "legacy_job_id": row["job_id"],
            "idempotency_key": row["idempotency_key"],
            "tenant_id": row["tenant_id"],
            "user_id": row["user_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "run_id": row.get("run_id"),
            "protected_fact_token_budget": row.get("protected_fact_token_budget"),
            "protected_fact_extraction_state": (
                row.get("protected_fact_extraction_state") or "pending"),
            "protected_fact_extraction_candidates": (
                None if candidates_raw is None else _json_obj_or_list(candidates_raw)),
            "stage": row["stage"],
            "state": _json_obj(row["state"]),
            "outcome": row["outcome"],
            "reason": row["reason"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
        counts["jobs"] += 1


# ---------------------------------------------------------------------- 补发布


async def _republish_completed(
    connection, root, commit: MemoryCommit, counts: dict[str, Any],
) -> bool:
    """旧 `completed + committed` job 的 active 记录按内容补发布。

    只补 **ACTIVE** 记录：用户后来改过/删过的旧版本已经不是当前事实，补发布等于把
    历史版本重新写回权威面。`add` 对同内容条目去重，所以已发布过的不会重复追加。

    D9：记录行缺失（旧库与内容面已经对不上）或发布被拒 ⇒ 该 operation 落
    `NEED_RECONCILE` 回执，并让本次导入保持未收敛——不能写 marker 把问题永久盖住。
    """
    if not isinstance(commit.engine, MarkdownMemoryStore):  # pragma: no cover — 类型防线
        return True
    receipts = ReceiptStore(root)
    converged = True
    seen: set[str] = set()
    for row in await _fetch(
        connection,
        "SELECT job_id, state FROM memory_v2_jobs "
        "WHERE stage='completed' AND outcome='committed'",
    ):
        state = _json_obj(row["state"])
        memory_ids = state.get("memory_ids")
        if not isinstance(memory_ids, list):
            continue
        for memory_id in memory_ids:
            if not isinstance(memory_id, str) or memory_id in seen:
                continue
            seen.add(memory_id)
            record = await _one(
                connection,
                "SELECT kind, tier, content, status FROM memory_v2_records WHERE memory_id=?",
                (memory_id,),
            )
            operation_id = "legp_" + hashlib.sha256(
                f"{row['job_id']}:{memory_id}".encode()).hexdigest()[:32]
            if record is None:
                await _flag_needs_reconcile(
                    receipts, operation_id, f"legacy record {memory_id} is missing",
                    memory_id=memory_id,
                )
                counts["republish_needs_reconcile"] += 1
                converged = False
                continue
            if record["status"] != "active":
                continue
            target = target_for(record["kind"], record["tier"])
            if not commit.engine.target_enabled(target):
                continue
            try:
                response = await commit.add(
                    target, record["content"], operation_id=operation_id)
            except Exception as error:  # noqa: BLE001 — 导入不得因单条发布失败整体失败
                logger.warning(
                    "legacy republish failed (%s): %s", type(error).__name__, memory_id)
                await _flag_needs_reconcile(
                    receipts, operation_id,
                    f"republish failed: {type(error).__name__}", memory_id=memory_id,
                )
                counts["republish_unreadable"] += 1
                converged = False
                continue
            if response.get("success"):
                counts["republished"] += 1
            else:
                await _flag_needs_reconcile(
                    receipts, operation_id, "legacy content rejected by markdown authority",
                    memory_id=memory_id,
                )
                counts["republish_unreadable"] += 1
                converged = False
    return converged


async def _flag_needs_reconcile(
    receipts: ReceiptStore, operation_id: str, reason: str, *, memory_id: str,
) -> None:
    """一次落盘写 NEED_RECONCILE 标记；已有 COMMITTED 不回退。

    与其余回执写路径同一把 per-operation 文件锁：CLI 与 server 同时导入时，不能
    在 `commit.add(legp_...)` 正在发布的中途把同一 id 改成 NEED_RECONCILE。
    回执用空 targets/operations 的合法形状（标记而不是一次内容写意图），
    memory_id 记进 reason，避免写出一个 load 时被判 corrupt 的文件。

    锁与写入都是阻塞 IO，调用方在事件循环上（导入是 async 路径）⇒ 整体丢进
    `to_thread`，跨进程锁竞争时不会卡住启动路径的事件循环（review R11-P4-2）。
    """
    def _write() -> None:
        with receipts.lock(operation_id):
            receipts.flag_needs_reconcile(
                operation_id, origin="legacy_import",
                reason=f"legacy memory {memory_id}: {reason}",
            )

    await asyncio.to_thread(_write)


# ---------------------------------------------------------------------- SQL 辅助


async def _fetch(connection, sql: str) -> list[Any]:
    try:
        cursor = await connection.execute(sql)
    except Exception as error:
        if "no such table" not in str(error).lower():
            raise
        return []
    return list(await cursor.fetchall())


async def _one(connection, sql: str, parameters: tuple[Any, ...]) -> Any | None:
    async with connection.execute(sql, parameters) as cursor:
        return await cursor.fetchone()


def _json_obj(value: Any) -> dict[str, Any]:
    parsed = _json_value(value)
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    parsed = _json_value(value)
    return parsed if isinstance(parsed, list) else []


def _json_obj_or_list(value: Any) -> Any:
    return _json_value(value)


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        logger.warning("legacy state column is not valid JSON; importing as empty")
        return None


def _identity_project(identity: str) -> str | None:
    parsed = _json_obj(identity)
    value = parsed.get("project_id")
    return value if isinstance(value, str) else None


__all__ = ["MARKER_SCHEMA", "import_legacy_state"]
