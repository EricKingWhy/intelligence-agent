"""#1493 记忆转向第 3 步：旧身份/版本 → 当前 Markdown 的**逐项批准**映射。

裁决 3（card-03）A 案：只有逐项映射获批后才并入；冲突/设置冲突/超预算项进冲突清单
等人工裁决，**不自动选赢家、不最后写入胜出**；同文去重必须保留全部来源映射。

# 为什么要清单文件，而不是"再写一个更宽的迁移脚本"

`migration.migrate_active_records` 只导 active 的 `memory_id/kind/tier/content`——
既不读身份也不迁 history/settings/staged/jobs/tombstone，所以它的
「exported + deduplicated == active、skipped=[]」只证明那一个窄导出（C §3.2）。
本模块把范围补到 ADR-0056 §3 点名的八类事实，并把**人工裁决**做成流程的一部分：

1. `plan_identity_mapping` 只读盘点，产出 `memory-migration-mapping.json`：源集合
   = 映射 ∪ 档案 ∪ 显式冲突，每个旧行恰好登记一次，带行哈希/内容哈希/旧 ID/版本/
   provenance；
2. `approve_item` / `resolve_conflict` 记录人的逐项决定（清单是唯一的人工输入面）；
3. `apply_identity_mapping` 先过闸（未批复/未裁决/活 lease/源数据漂移一律零落盘），
   再**只追加**写当前文件。

# 清单里为什么不存正文

正文已经在当前文件里；再抄一份到清单，等于给用户数据多造一个副本，也让"拒绝"变得
可被误读成"已保存"。清单只存哈希与 ID（与墓碑同思路），人要读正文时由 CLI 按 ID
回库现取（`--show`）。副作用是 apply 必须回库取内容——顺带就成了"源数据仍与计划一致"
的 fail-closed 校验（`row_hash` 不符即拒绝，不拿计划后的漂移写文件）。

# 不做什么

- 不删改任何旧库行（销毁未授权，`destruction="not_authorized"`）；
- 不自动批准 pending（`memory_v2_staged` 行只进档案，pending 仍 pending）；
- 不把拒绝/归档算成去重（`skipped` 与 `archive` 分开记账，与 #1441 的报告同形）；
- 不扩预算、不截断、不总结丢信息：放不下就是 `budget:<target>` 冲突等人裁；
- 不给旧三元路由换名成新 owner 列（裁决 7）：旧 tenant/user/project 只作**迁移来源标签**。

真实部署执行前必须先有 #1492 式一致备份（C §3.2 的顺序：源数据全保全 → 旧身份/项目
分组 → 逐条映射与冲突裁决 → 新 owner 验证 → 切换读写）。本模块不检查备份是否存在——
它不碰旧库，只读；备份是**数据操作前置**，由操作者在跑 `--apply` 前自行满足。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from agent_harness.memory.v2._sqlite import connect, stamp
from agent_harness.memory.v2.assembly import (
    MEMORIES_DIRECTORY_NAME,
    MEMORY_V2_DATABASE_NAME,
)
from agent_harness.memory.v2.markdown_store import (
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_USER_CHAR_LIMIT,
    ENTRY_DELIMITER,
    MarkdownMemoryStore,
    entry_digest,
    normalize_entry,
    target_for,
)

# `kind_defaulted` 是 #1441 报告口径的一部分（"因 kind 不合法才落 MEMORY.md"），
# 复用同一个判据而不是在这里抄第二份——两份判据迟早一份说兜底、一份说不是。
from agent_harness.memory.v2.migration import _kind_defaulted as kind_defaulted
from agent_harness.memory.v2.store import MemorySettingsV2
from agent_harness.memory.v2.threat_patterns import scan_for_threats
from agent_harness.memory.v2.write_approval import APPROVING_LEASE

logger = logging.getLogger(__name__)

#: 清单文件名（相对 workspace 根）。与 `memories/`、`memory-v2.db` 并列一等文件。
MANIFEST_NAME = "memory-migration-mapping.json"

MANIFEST_SCHEMA_VERSION = 1

#: 要盘点的五张表：旧身份/版本/审批/jobs/墓碑。FTS 与 outbox 是**派生态**
#: （ADR-0056 §3.8 不拥有任何当前事实），不登记、不迁移。
SOURCE_TABLES = (
    "memory_v2_records",
    "memory_v2_settings",
    "memory_v2_staged",
    "memory_v2_jobs",
    "memory_v2_tombstones",
)

#: 这些 stage 是终态；终态行的 `idempotency_key` 必须留档（删了就重复 formation）。
TERMINAL_STAGES = frozenset({"completed", "degraded"})

_SOURCE_ID_COLUMN = {
    "memory_v2_records": "memory_id",
    "memory_v2_staged": "id",
    "memory_v2_jobs": "job_id",
    "memory_v2_tombstones": "memory_id",
}


class MappingBlocked(Exception):
    """切换被阻断。`reasons` 逐条给出"为什么还不能写"，一条都不吞。"""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("；".join(self.reasons))


def _canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _row_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(row).encode("utf-8")).hexdigest()


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    """旧 tenant/user/project：只作**迁移来源标签**，不是运行路由（ADR-0056 §3.7）。"""
    return {
        "tenant_id": row.get("tenant_id"), "user_id": row.get("user_id"),
        "project_id": row.get("project_id"),
    }


def _settings_key(row: dict[str, Any]) -> str:
    return f"{row.get('tenant_id')}/{row.get('user_id')}"


def _settings_values(settings: MemorySettingsV2) -> dict[str, bool]:
    return {
        "extraction_enabled": settings.extraction_enabled,
        "recall_enabled": settings.recall_enabled,
        "fallback_enabled": settings.fallback_enabled,
        "write_approval": settings.write_approval,
    }


def _parse_moment(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _lease_live(row: dict[str, Any], now: datetime) -> bool:
    """job 的 lease 是否仍在飞：未终结 + 有属主 + 未到期。"""
    if row.get("stage") in TERMINAL_STAGES:
        return False
    expires = _parse_moment(row.get("lease_expires_at"))
    return bool(row.get("lease_owner")) and expires is not None and expires > now


def _approval_live(row: dict[str, Any], now: datetime) -> bool:
    """`approving` 的审批租约是否仍在飞（被 SIGKILL 打断后过期 = 可回收，不是活 writer）。"""
    if row.get("status") != "approving":
        return False
    claimed = _parse_moment(row.get("claimed_at"))
    return claimed is not None and claimed + APPROVING_LEASE > now


# --------------------------------------------------------------------- 清单读写


def load_manifest(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _save_manifest(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """原子落盘，并把分区计数重算成**当前** item 集合的真实投影。

    分区每存一次都重算而不是增量维护：裁决会搬动源行（冲突 → 映射/档案），
    增量维护的计数迟早与实际 item 集合对不上，而那正是"源集合可核"的判据本身。
    """
    counts = {"rows_mapped": 0, "rows_archived": 0, "rows_conflicted": 0}
    for item in manifest["items"]:
        rows = len(item.get("sources") or ())
        if item["disposition"] == "map":
            counts["rows_mapped"] += rows
        elif item["disposition"] == "archive":
            counts["rows_archived"] += rows
        else:
            counts["rows_conflicted"] += rows
    manifest["partition"] = {
        **counts,
        "rows_total": sum(counts.values()),
        "items": len(manifest["items"]),
    }
    manifest["updated_at"] = stamp()
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".map_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return manifest


def _find(manifest: dict[str, Any], item_id: str) -> dict[str, Any]:
    for item in manifest["items"]:
        if item["id"] == item_id:
            return item
    raise MappingBlocked([f"清单里没有 {item_id}"])


# --------------------------------------------------------------------- 盘点


async def _read_rows(connection: aiosqlite.Connection, table: str) -> list[dict[str, Any]]:
    """整表只读。老库可能还没有某张表（升级中间态）：当作空，不猜 schema。"""
    try:
        async with connection.execute(f"SELECT * FROM {table}") as cursor:
            return [dict(row) for row in await cursor.fetchall()]
    except aiosqlite.OperationalError as error:
        logger.warning("盘点跳过缺失的表 %s：%s", table, error)
        return []


def _disk_state(workspace: Path, target: str) -> tuple[int, int]:
    """当前文件已有内容的 `(字符数, 条目数)`——预算必须把这个起点算进去。

    直接读文件而不是造一个 `MarkdownMemoryStore`：store 的构造/加载会 `mkdir`
    权威面目录，盘点阶段连目录都不该建。读不出来按 0 处理：届时 apply 的回读校验
    会发现"少算了已有内容"以外的差异吗？不会——所以这里只在文件确实不存在/为空时
    才返回 0，读失败也返回 0 意味着可能写超预算；`apply` 用 store 真实加载后写，
    store 自己的 `add` 仍会按同一预算拒绝，超预算条目落进 `skipped` 而非静默写入。
    """
    path = workspace / MEMORIES_DIRECTORY_NAME / ("USER.md" if target == "user" else "MEMORY.md")
    if not path.exists():
        return 0, 0
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return 0, 0
    entries = MarkdownMemoryStore._parse_entries(raw)
    return len(ENTRY_DELIMITER.join(entries)), len(entries)


def _source_row(row: dict[str, Any], table: str) -> dict[str, Any]:
    """一个旧行的登记项：哈希 + 旧 ID + 身份 + 版本/来源。正文不进清单。"""
    source: dict[str, Any] = {
        "table": table,
        # settings 的主键是 (tenant_id, user_id)，没有单独的 id 列——旧 key 就是它的 ID。
        "id": (str(row.get(_SOURCE_ID_COLUMN[table]))
               if table in _SOURCE_ID_COLUMN else _settings_key(row)),
        "row_hash": _row_hash(row),
        "identity": _identity(row),
    }
    if row.get("content") is not None:
        source["content_hash"] = entry_digest(normalize_entry(str(row["content"])))
        source["version"] = row.get("version")
        source["provenance"] = {
            "source_type": row.get("source_type"),
            "source_session_id": row.get("source_session_id"),
            "source_event_hashes": [
                entry_digest(str(event)) for event in _json_list(row.get("source_event_ids"))
            ],
        }
    return source


def _json_list(raw: Any) -> list[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return list(value) if isinstance(value, (list, tuple)) else []


async def plan_identity_mapping(
    workspace_dir: Path | str,
    *,
    database_path: Path | str | None = None,
    explicit_conflicts: list[list[str]] | None = None,
    memory_char_limit: int | None = None,
    user_char_limit: int | None = None,
    deployment_memory_char_limit: int | None = None,
    deployment_user_char_limit: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """只读盘点，写清单，返回清单。

    `explicit_conflicts` 是**人工点名**的正文冲突（每项是一组 `memory_id`）：
    「两条不同正文在讲同一件事」无法机械判定，只有人能指出来；没被点名的异文照常
    各自成条（不自动选赢家 = 也不自动判它们冲突）。

    `*_char_limit` 与 `Settings` 同义（#1441 §1 的同源要求）；缺省取 store 构造默认值。
    当前文件的已有内容计入预算——迁移写在它后面，跳过它会写出运行时读不进来的文件。

    `deployment_*_char_limit` 是**部署配置**（`Settings`）的当下取值。请求的上限只能
    低于或等于它：清单预算最终决定 apply 写多少字，若允许抬过部署配置，就会写出运行时
    读不进来的文件（"不扩预算"是票面红线）。所以**在 plan 阶段就拒绝**，两者都记进清单
    供人核对（`deployment_limits` 只是审计读数，`apply` 用的是 `limits`）。
    """
    workspace = Path(workspace_dir)
    database = Path(database_path or workspace / MEMORY_V2_DATABASE_NAME)
    manifest_path = workspace / MANIFEST_NAME
    deployment = {
        "memory": (DEFAULT_MEMORY_CHAR_LIMIT if deployment_memory_char_limit is None
                   else int(deployment_memory_char_limit)),
        "user": (DEFAULT_USER_CHAR_LIMIT if deployment_user_char_limit is None
                 else int(deployment_user_char_limit)),
    }
    requested = {
        "memory": deployment["memory"] if memory_char_limit is None else int(memory_char_limit),
        "user": deployment["user"] if user_char_limit is None else int(user_char_limit),
    }
    inflated = [
        f"--{target}-limit={requested[target]} 高于部署配置 {deployment[target]}："
        f"迁移不得扩大预算（apply 只按清单预算写盘，超过部署上限的文件运行时读不进来）"
        for target in ("memory", "user")
        if requested[target] > deployment[target]
    ]
    if inflated:
        raise MappingBlocked(inflated)
    if manifest_path.exists():
        raise MappingBlocked([
            f"{MANIFEST_NAME} 已存在：先用 CLI 审完/作废它，再重新盘点（覆盖会丢已批的裁决）",
        ])
    moment = now if now is not None else datetime.now().astimezone()

    rows: dict[str, list[dict[str, Any]]] = {table: [] for table in SOURCE_TABLES}
    if database.exists():
        async with connect(database) as connection:
            for table in SOURCE_TABLES:
                rows[table] = await _read_rows(connection, table)

    items: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts_of: dict[str, list[str]] = {}
    active_ids = {str(row["memory_id"]) for row in rows["memory_v2_records"]
                  if row.get("status") == "active"}
    for group in explicit_conflicts or ():
        ids = [str(source) for source in group if str(source).strip()]
        if len(ids) < 2:
            raise MappingBlocked([f"--conflict 至少给两条 memory_id（收到 {ids}）"])
        # 点名了不存在/非 active 的行 ⇒ 人写错了 ID。静默忽略会让"我明明裁过它"变成假话。
        unknown = [memory_id for memory_id in ids if memory_id not in active_ids]
        if unknown:
            raise MappingBlocked([
                f"--conflict 里的 {unknown} 不是当前 active 行（不存在或已 superseded/invalidated）",
            ])
        for memory_id in ids:
            conflicts_of[memory_id] = ids

    # ① 记录行：active 进映射（按 目标+正文 归并），其余进档案；扫描/分隔符拒绝进冲突。
    for row in rows["memory_v2_records"]:
        memory_id = str(row["memory_id"])
        if row.get("status") != "active":
            items.append({
                "id": f"archive:memory_v2_records:{memory_id}",
                "kind": "record_archive",
                "disposition": "archive",
                "reason": f"status={row.get('status')}",
                "sources": [_source_row(row, "memory_v2_records")],
            })
            continue
        content = normalize_entry(str(row.get("content") or ""))
        rejected = _rejection(content)
        if rejected is not None:
            items.append({
                "id": f"{rejected}:memory_v2_records:{memory_id}",
                "kind": rejected,
                "disposition": "conflict",
                "reason": rejected,
                "sources": [_source_row(row, "memory_v2_records")],
            })
            continue
        target = target_for(row.get("kind"), row.get("tier"))
        digest = entry_digest(content)
        if memory_id in conflicts_of:
            # 显式冲突：整组人一起登记，正文只在库里，清单只留哈希。
            key = ("entry_conflict", conflicts_of[memory_id][0])
            if key in groups:
                continue
            members = [item for item in rows["memory_v2_records"]
                       if str(item["memory_id"]) in conflicts_of[memory_id]]
            items.append({
                "id": f"conflict:entry:{conflicts_of[memory_id][0]}",
                "kind": "entry_conflict",
                "disposition": "conflict",
                "decision": None,
                "variants": [
                    {
                        "key": str(member["memory_id"]),
                        "target": target_for(member.get("kind"), member.get("tier")),
                        "content_hash": entry_digest(normalize_entry(str(member["content"]))),
                        "content_length": len(normalize_entry(str(member["content"]))),
                        "sources": [_source_row(member, "memory_v2_records")],
                    }
                    for member in members
                ],
                "sources": [_source_row(member, "memory_v2_records") for member in members],
            })
            groups[key] = {"id": items[-1]["id"]}
            continue
        key = (target, digest)
        if key not in groups:
            item = {
                "id": f"entry:{target}:{digest[:16]}",
                "kind": "entry",
                "disposition": "map",
                "target": target,
                "content_hash": digest,
                # 条目长度的**唯一**来源：清单里存长度而不是正文（正文只在库里 / 当前文件里），
                # 预算重算就变成纯计算，不必为裁决再回库同步读一遍。
                "content_length": len(content),
                "kind_defaulted": kind_defaulted(row.get("kind"), row.get("tier")),
                "sources": [],
            }
            items.append(item)
            groups[key] = item
        groups[key]["sources"].append(_source_row(row, "memory_v2_records"))

    # ② 设置：四开关按旧 key 逐条登记；缺行用默认 ≠ 显式设默认。
    items.extend(_settings_item(rows["memory_v2_settings"], rows))

    # ③ 审批态：只进档案。pending 仍 pending；租约未过期的 approving 是活 writer。
    for row in rows["memory_v2_staged"]:
        staged_id = str(row["id"])
        items.append({
            "id": f"archive:memory_v2_staged:{staged_id}",
            "kind": "staged_archive",
            "disposition": "archive",
            "status": row.get("status"),
            "identity": _identity(_identity_of_staged(row)),
            "payload_hash": entry_digest(str(row.get("payload") or "")),
            "claimed_at": row.get("claimed_at"),
            "lease_live": _approval_live(row, moment),
            "sources": [_source_row(row, "memory_v2_staged")],
        })

    # ④ formation job：终态幂等键必须留档；活 lease 阻断切换。
    for row in rows["memory_v2_jobs"]:
        job_id = str(row["job_id"])
        items.append({
            "id": f"archive:memory_v2_jobs:{job_id}",
            "kind": "job_archive",
            "disposition": "archive",
            "idempotency_key": row.get("idempotency_key"),
            "status": row.get("stage"),
            "outcome": row.get("outcome"),
            "identity": _identity(row),
            "lease_live": _lease_live(row, moment),
            "sources": [_source_row(row, "memory_v2_jobs")],
        })

    # ⑤ 墓碑：删除抑制事实（含内容/来源 hash 与过期时刻）——只登记，不清理。
    for row in rows["memory_v2_tombstones"]:
        tombstone_id = str(row["memory_id"])
        items.append({
            "id": f"archive:memory_v2_tombstones:{tombstone_id}",
            "kind": "tombstone_archive",
            "disposition": "archive",
            "identity": _identity(row),
            "version": row.get("version"),
            "deleted_at": row.get("deleted_at"),
            "expires_at": row.get("expires_at"),
            "deletion_reason": row.get("deletion_reason"),
            "content_hashes": _json_list(row.get("content_hashes")),
            "source_hashes": _json_list(row.get("source_hashes")),
            "sources": [_source_row(row, "memory_v2_tombstones")],
        })

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "workspace": str(workspace),
        "database": str(database),
        "created_at": stamp(moment),
        "updated_at": stamp(moment),
        "limits": requested,
        # 部署配置的当下取值：只作审计读数供人核对（`apply` 用的是 `limits`）。
        # 抬预算是**盘点时**就被拒掉的，不在 apply 复算——否则清单里的人手改动会让
        # 两条拒绝通道并存，而测试证明后者不可达。
        "deployment_limits": deployment,
        "applied_at": None,
        "items": items,
        "source_scan": {
            table: len(rows[table]) for table in SOURCE_TABLES
        },
        "partition": {},
    }
    items.extend(_budget_items(manifest, workspace))
    return _save_manifest(manifest_path, manifest)


def _identity_of_staged(row: dict[str, Any]) -> dict[str, Any]:
    """staged 的身份在 `identity` 列的规范化 JSON 里，不在独立列上。"""
    try:
        value = json.loads(row.get("identity") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _rejection(content: str) -> str | None:
    """落盘前的三道硬拒绝：空正文、保留分隔符、threat 扫描。返回 `reason`（台账口径）。

    三者分开命名：报告要能分清"这条压根没正文"与"这条正文会分裂成多条"与"命中注入模式"，
    混成一个 `rejected` 会让人不知道该修什么。
    """
    if not content:
        return "empty_rejected"
    folded = content.replace("\r\n", "\n").replace("\r", "\n")
    if ENTRY_DELIMITER in folded or folded.endswith(ENTRY_DELIMITER[:-1]):
        return "delimiter_rejected"
    if scan_for_threats(content, scope="strict"):
        return "scan_rejected"
    return None


def _settings_item(
    settings_rows: list[dict[str, Any]], rows: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """四开关的逐 key 登记；值全一致才映射，否则冲突（不按更新时间自动胜出）。"""
    by_key: dict[str, dict[str, Any]] = {}
    for row in settings_rows:
        by_key[_settings_key(row)] = row
    # 缺行默认要按**出现过的旧身份**来表达，否则"没有设置行的部署"无法与"显式默认"区分。
    candidates = set(by_key)
    for table in SOURCE_TABLES:
        if table == "memory_v2_settings":
            continue
        for row in rows[table]:
            if row.get("tenant_id") is None:
                continue
            source = _identity_of_staged(row) if table == "memory_v2_staged" else row
            candidates.add(f"{source.get('tenant_id')}/{source.get('user_id')}")

    variants = []
    for key in sorted(candidates):
        row = by_key.get(key)
        if row is None:
            values = _settings_values(MemorySettingsV2())
            variants.append({
                "key": key, "values": values, "values_source": "missing_row_default",
                "sources": [],
            })
            continue
        variants.append({
            "key": key,
            "values": _settings_values(MemorySettingsV2(
                bool(row["extraction_enabled"]), bool(row["recall_enabled"]),
                bool(row["fallback_enabled"]), bool(row["write_approval"]))),
            "values_source": "explicit",
            "sources": [_source_row(row, "memory_v2_settings")],
        })
    sources = [source for variant in variants for source in variant["sources"]]
    conflicting = len({json.dumps(variant["values"], sort_keys=True) for variant in variants}) > 1
    item = {
        "id": "conflict:settings" if conflicting else "settings",
        "kind": "settings_conflict" if conflicting else "settings",
        "disposition": "conflict" if conflicting else "map",
        # 设置的新持久格式归 #1494；本票只登记"哪一个旧 key 的哪组值"的归宿，
        # 所以它没有 target 文件、apply 不写盘。
        "applies_to_file": False,
        "variants": variants,
        "origin": (
            "conflict" if conflicting
            else ("explicit" if any(v["values_source"] == "explicit" for v in variants)
                  else "missing_row_default")
        ),
        "decision": None,
        "sources": sources,
    }
    return [item]


def _budget_items(manifest: dict[str, Any], workspace: Path) -> list[dict[str, Any]]:
    """把放不进预算的映射条目列成 `budget:<target>` 冲突（不扩预算、不截断）。

    预算口径与运行时同源：`len(ENTRY_DELIMITER.join(entries))`（`MarkdownMemoryStore._char_count`）。
    当前文件的已有内容计入起点——迁移写在它后面，忽略它会写出运行时读不进来的文件。
    """
    conflicts: list[dict[str, Any]] = []
    for target, limit in manifest["limits"].items():
        baseline, existing = _disk_state(workspace, target)
        used = baseline
        count = existing
        overflow: list[str] = []
        excess = 0
        for item in manifest["items"]:
            # 只看**还要写进当前文件**的条目：已被裁决移进档案的条目仍然 kind=entry，
            # 若把它们算进预算，冲突永远不会收敛（裁一次、算一次、再冒同一条）。
            if (item["kind"] != "entry" or item["disposition"] != "map"
                    or item.get("target") != target):
                continue
            # 与 `ENTRY_DELIMITER.join(entries)` 同源：第 N+1 条要多一个分隔符。
            cost = item["content_length"] + (len(ENTRY_DELIMITER) if count else 0)
            if used + cost > limit:
                overflow.append(item["id"])
                # 超限项本身也要付分隔符——报告里的 excess 是"还差多少才放得下"。
                excess += item["content_length"] + len(ENTRY_DELIMITER)
            else:
                used += cost
                count += 1
        if overflow:
            conflicts.append({
                "id": f"budget:{target}",
                "kind": "budget_conflict",
                "disposition": "conflict",
                "target": target,
                "limit": limit,
                "baseline": baseline,
                "excess": excess,
                "entries": overflow,
                "sources": [],
                "decision": None,
            })
    return conflicts


def _refresh_budget(manifest: dict[str, Any], workspace: Path) -> None:
    """按当前条目集合重算预算冲突（裁决会搬动条目，旧结论必然过期）。

    先删全部 `budget:*` 再重算：增量维护会在同一目标上留下两条互相矛盾的冲突。
    """
    manifest["items"] = [item for item in manifest["items"]
                         if item["kind"] != "budget_conflict"]
    manifest["items"].extend(_budget_items(manifest, workspace))


# --------------------------------------------------------------------- 人工裁决


async def approve_item(
    manifest_path: Path | str, item_id: str, *, now: datetime | None = None,
) -> dict[str, Any]:
    """逐项批准一条**映射**。归档项不需要批准（它本来就不写当前文件），冲突项要先裁决。"""
    path = Path(manifest_path)
    manifest = load_manifest(path)
    item = _find(manifest, item_id)
    if item["disposition"] != "map":
        raise MappingBlocked([
            f"{item_id} 是 {item['disposition']} 项：映射才需要批准，冲突请先 resolve",
        ])
    item["approved_at"] = stamp(now)
    return _save_manifest(path, manifest)


async def resolve_conflict(
    manifest_path: Path | str, item_id: str, *,
    merge_source: str | None = None,
    archive_items: list[str] | None = None,
    archive_all: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """记录人的裁决。三种形状，都**不**自动选赢家——赢家由人说。

    - `merge_source`：正文冲突取哪一条的正文 / 设置冲突取哪个旧 key 的四开关；
      落选的不删，进档案（`archive`）与冲突裁决记录一起留档。
    - `archive_items`：预算冲突里哪几条不进当前文件（进档案）。
    - `archive_all`：扫描/分隔符拒绝的项整组进档案（明确决定"不导入"）。
    """
    path = Path(manifest_path)
    manifest = load_manifest(path)
    item = _find(manifest, item_id)
    if item["disposition"] != "conflict":
        raise MappingBlocked([f"{item_id} 不是未决冲突（{item['disposition']}），无需裁决"])

    kind = item["kind"]
    if kind == "budget_conflict":
        chosen = [str(value) for value in archive_items or ()]
        # 作用域 = **这个**预算项列出的条目。按全清单 id 校验会让一个语义无关的 flag
        # 把异目标文件的条目、甚至未裁决的正文冲突改成 archive，绕过"未裁决不得切换"。
        scope = list(item["entries"])
        unknown = [value for value in chosen if value not in scope]
        if unknown:
            raise MappingBlocked([
                f"这些条目不属于 {item['id']} 的预算超限集：{unknown}（可移出的只有 {scope}）",
            ])
        for other in manifest["items"]:
            if other["id"] in chosen:
                other["disposition"] = "archive"
                other["reason"] = f"budget:{item['target']} 人工裁决移出当前文件"
                # 归档也是人的决定：`decision=None` 的"已归档"无法与人没碰过的项区分。
                other["decision"] = {
                    "archived_by": item["id"],
                    "archived_at": stamp(now),
                }
        item["entries"] = [value for value in item["entries"] if value not in chosen]
        item["decision"] = {"archived": chosen, "decided_at": stamp(now)}
        if not item["entries"]:
            manifest["items"].remove(item)
        _refresh_budget(manifest, Path(manifest["workspace"]))
        return _save_manifest(path, manifest)

    if kind in {"scan_rejected", "delimiter_rejected"}:
        if not archive_all:
            raise MappingBlocked([
                f"{item_id} 只能整组进档案（--archive-all）：{kind} 的正文不得进当前文件",
            ])
        _to_archive(item, manifest, reason=f"{kind} 人工裁决不导入", now=now)
        manifest["items"].remove(item)
        _refresh_budget(manifest, Path(manifest["workspace"]))
        return _save_manifest(path, manifest)

    if kind == "settings_conflict":
        if merge_source is None:
            raise MappingBlocked([f"{item_id} 需要 --merge <旧 key>：取哪个旧 key 的四开关"])
        chosen = [variant for variant in item["variants"] if variant["key"] == merge_source]
        if not chosen:
            raise MappingBlocked([f"{item_id} 没有 {merge_source} 这个旧 key"])
        item["disposition"] = "map"
        item["kind"] = "settings"
        item["id"] = "settings"
        item["decision"] = {"merge_source": merge_source, "decided_at": stamp(now)}
        _approved_by_decision(item, now)
        for variant in item["variants"]:
            for source in variant["sources"]:
                source["used"] = variant["key"] == merge_source
        item["origin"] = chosen[0]["values_source"]
        return _save_manifest(path, manifest)

    if kind == "entry_conflict":
        if merge_source is None:
            raise MappingBlocked([f"{item_id} 需要 --merge <旧 memory_id>：取哪一条的正文"])
        winner = [variant for variant in item["variants"] if variant["key"] == merge_source]
        if not winner:
            raise MappingBlocked([f"{item_id} 没有 {merge_source} 这个变体"])
        chosen = winner[0]
        losers = [variant for variant in item["variants"] if variant["key"] != merge_source]
        new_id = f"entry:{chosen['target']}:{chosen['content_hash'][:16]}"
        decision = {
            "merge_source": merge_source,
            "archived_sources": [source["id"] for variant in losers for source in variant["sources"]],
            "decided_at": stamp(now),
        }
        # 裁决出的正文可能与某条既有映射逐字相同（同一事实在两个身份下各写一遍，
        # 其中一份又被点名成冲突——完全正常）。此时只能**并入**既有项：重复 id 会让
        # `_find` 永远命中第一条，未批的那条再也批不上，apply 永久 BLOCKED 而 re-plan
        # 又被"清单已存在"拒绝 ⇒ 清单死锁。
        existing = [other for other in manifest["items"]
                    if other["id"] == new_id and other is not item]
        if existing:
            target_item = existing[0]
            _merge_conflict_into_mapping(target_item, item, decision, now)
            _archive_losers(manifest, item, losers, reason_id=target_item["id"])
            manifest["items"].remove(item)
            _refresh_budget(manifest, Path(manifest["workspace"]))
            return _save_manifest(path, manifest)
        item["disposition"] = "map"
        item["kind"] = "entry"
        item["id"] = new_id
        item["target"] = chosen["target"]
        item["content_hash"] = chosen["content_hash"]
        item["content_length"] = chosen.get("content_length", 0)
        item["sources"] = list(chosen["sources"])
        item["decision"] = decision
        _approved_by_decision(item, now)
        _archive_losers(manifest, item, losers, reason_id=item["id"])
        _refresh_budget(manifest, Path(manifest["workspace"]))
        return _save_manifest(path, manifest)

    raise MappingBlocked([f"{item_id} 的 kind={kind} 不支持裁决"])


def _archive_losers(
    manifest: dict[str, Any], conflict_item: dict[str, Any],
    losers: list[dict[str, Any]], *, reason_id: str,
) -> None:
    """落选变体进只读档案（不删、不再进当前文件），并标出它们属于哪条冲突裁决。"""
    known = {other["id"] for other in manifest["items"]
             if other["kind"] == "record_archive"}
    for variant in losers:
        archive_id = f"archive:entry_conflict:{variant['key']}"
        if archive_id in known:
            continue
        manifest["items"].append({
            "id": archive_id,
            "kind": "record_archive",
            "disposition": "archive",
            "reason": f"{reason_id} 裁决落选（保留为只读档案）",
            "sources": list(variant["sources"]),
        })


def _merge_conflict_into_mapping(
    target_item: dict[str, Any], conflict_item: dict[str, Any],
    decision: dict[str, Any], now: datetime | None,
) -> None:
    """把裁决结果并入**既有**映射项：来源并集、裁决记录留档、批准沿用原状态。

    既有项若已获批，说明"这条正文要进当前文件"这件事人已经点过头；裁决只补充
    "另外那条旧行也属于它"。既有项若未获批，批准状态不因裁决而升级——裁决的对象
    是冲突项，不是这条映射。`decisions` 是列表：同一条正文可能被裁决合并多次。
    """
    known = {source["id"] for source in target_item["sources"]}
    target_item["sources"].extend(
        source for source in conflict_item["sources"] if source["id"] not in known)
    target_item.setdefault("decisions", []).append(decision)
    target_item.setdefault("merged_conflicts", []).append(conflict_item["id"])


def _approved_by_decision(item: dict[str, Any], now: datetime | None) -> None:
    """裁决本身就是对这一项的人工决定，顺带记批准。

    为什么不再要一次 `approve`：`--merge` 是逐项指名"用哪一条正文/哪一组开关"，
    比空批准更强；再点一次只是让人多按一次回车，不会多一分证据。裁决内容
    （`decision.merge_source` / `archived_sources`）与批准时间一起留在清单里，
    apply 的"未获批"闸照常按 `approved_at` 判定。
    """
    item["approved_at"] = stamp(now)


def _to_archive(item: dict[str, Any], manifest: dict[str, Any], *, reason: str,
                now: datetime | None) -> None:
    manifest["items"].append({
        "id": f"archive:conflict:{item['id']}",
        "kind": item["kind"] + "_archive",
        "disposition": "archive",
        "reason": reason,
        "decision": {"decided_at": stamp(now)},
        "sources": list(item["sources"]),
    })


# --------------------------------------------------------------------- 切换


async def apply_identity_mapping(
    manifest_path: Path | str, *, now: datetime | None = None,
) -> dict[str, Any]:
    """过了闸才写当前文件：**只追加**，不覆盖、不删除、不碰旧库。

    闸（任一不满足即 `MappingBlocked`，且**零落盘**——连 `memories/` 目录都不建）：

    1. 所有映射项已逐项获批；
    2. 没有未裁决的冲突（内容/设置/预算/扫描/分隔符）；
    3. 没有活 lease（在飞 job 或未过期的 `approving`）——lease 丢失才可恢复；
    4. 每个旧源行仍与清单里的 `row_hash` 一致（盘点后的漂移不拿旧计划去写新数据）。
    """
    path = Path(manifest_path)
    manifest = load_manifest(path)
    workspace = Path(manifest["workspace"])
    moment = now if now is not None else datetime.now().astimezone()

    reasons: list[str] = []
    for item in manifest["items"]:
        if item["disposition"] == "conflict":
            reasons.append(f"{item['id']} 未裁决（{item['kind']}）")
        elif item["disposition"] == "map" and not item.get("approved_at"):
            reasons.append(f"{item['id']} 未获批")
        elif item.get("lease_live"):
            reasons.append(f"{item['id']} 仍有活 lease：先等它结终态或过期再切")
    if reasons:
        raise MappingBlocked(reasons)

    contents = await _verified_contents(manifest)

    store = MarkdownMemoryStore(
        workspace / MEMORIES_DIRECTORY_NAME,
        memory_char_limit=manifest["limits"]["memory"],
        user_char_limit=manifest["limits"]["user"],
    )
    store.load_from_disk()

    counts: dict[str, Any] = {
        "active": 0, "exported": 0, "deduplicated": 0,
        "memory": 0, "user": 0, "kind_defaulted": 0, "skipped": [],
    }
    settings_mapped = 0
    for item in manifest["items"]:
        if item["disposition"] != "map":
            continue
        if item["kind"] == "settings":
            settings_mapped += 1
            continue
        content = contents[item["id"]]
        counts["active"] += 1
        response = await store.add(item["target"], content)
        if response.get("success"):
            counts["deduplicated" if response.get("deduplicated") else "exported"] += 1
            counts[item["target"]] += 1
            if item.get("kind_defaulted"):
                counts["kind_defaulted"] += 1
        else:
            error = str(response.get("error") or "unknown error")
            # 预算在盘点期已拦过；这里还能红说明文件在盘点后被改大 —— 如实记账，不静默吞。
            counts["skipped"].append((item["id"], error))
            logger.warning("映射写入跳过（%s）：%s", item["target"], error)

    receipt = {
        "counts": counts,
        "settings": settings_mapped,
        "archive": {
            "records": sum(1 for item in manifest["items"]
                           if item["disposition"] == "archive"
                           and item["kind"] == "record_archive"),
            "staged": sum(1 for item in manifest["items"]
                          if item["kind"] == "staged_archive"),
            "jobs": sum(1 for item in manifest["items"] if item["kind"] == "job_archive"),
            "tombstones": sum(1 for item in manifest["items"]
                              if item["kind"] == "tombstone_archive"),
        },
        # 批复 ≠ 批准记忆写入 ≠ 批准销毁旧数据（裁决 6 只批加密备份至保留期）。
        "destruction": "not_authorized",
        "applied_at": stamp(moment),
    }
    manifest["applied_at"] = receipt["applied_at"]
    manifest["last_receipt"] = receipt
    _save_manifest(path, manifest)
    return receipt


async def _verified_contents(manifest: dict[str, Any]) -> dict[str, str]:
    """回库取每条映射的正文，同时 fail-closed 校验源数据没漂移。"""
    database = Path(manifest["database"])
    contents: dict[str, str] = {}
    async with connect(database) as connection:
        for item in manifest["items"]:
            if item["disposition"] != "map" or item["kind"] != "entry":
                continue
            content: str | None = None
            for source in item["sources"]:
                row = await _source_in(connection, source)
                if row is None or _row_hash(row) != source["row_hash"]:
                    raise MappingBlocked([(
                        f"{item['id']} 的源行 {source['table']}/{source['id']} 在盘点后变化了；"
                        "重新盘点（源数据必须冻结）"
                    )])
                if row.get("content") is not None and content is None:
                    content = normalize_entry(str(row["content"]))
            if content is None:
                raise MappingBlocked([f"{item['id']} 找不到可写正文"])
            if entry_digest(content) != item["content_hash"]:
                raise MappingBlocked([f"{item['id']} 的正文与清单哈希不一致；重新盘点"])
            contents[item["id"]] = content
    return contents


async def _source_in(
    connection: aiosqlite.Connection, source: dict[str, Any],
) -> dict[str, Any] | None:
    table = source["table"]
    if table == "memory_v2_settings":
        tenant, _, user = str(source["id"]).partition("/")
        async with connection.execute(
            "SELECT * FROM memory_v2_settings WHERE tenant_id=? AND user_id=?",
            (tenant, user),
        ) as cursor:
            row = await cursor.fetchone()
    else:
        async with connection.execute(
            f"SELECT * FROM {table} WHERE {_SOURCE_ID_COLUMN[table]}=?", (source["id"],),
        ) as cursor:
            row = await cursor.fetchone()
    return None if row is None else dict(row)


__all__ = [
    "MANIFEST_NAME",
    "MappingBlocked",
    "apply_identity_mapping",
    "approve_item",
    "load_manifest",
    "plan_identity_mapping",
    "resolve_conflict",
]
