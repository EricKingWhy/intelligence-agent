"""#1494 / ADR-0058 D3：提交回执（receipt）——内容 commit 的 outbox 行与对账依据。

# 形状

一个 operation 一个文件（`memory-state/receipts/<operation_id>.json`），四种状态
（ADR-0056 §3.6 的命名待定项在此定稿）：

| 状态 | 含义 |
|---|---|
| ``PREPARED`` | 意图 + 每目标 before/after digest 已落盘；该目标文件尚未替换 |
| ``PUBLISHED`` | 多目标操作中 ≥1 个目标已替换、尚有目标未落地 |
| ``COMMITTED`` | 全部目标已落地且操作的后置条件已满足 |
| ``NEED_RECONCILE`` | 某目标磁盘内容既非 before 也非 after：不盲重跑、不覆盖新编辑 |

**写序**：回执先于文件替换落盘（`prepare` 由 commit 在目标文件的临界区里、写文件之前调用），
所以「有替换一定有回执」；反向的窗口（有回执、文件还没换）由对账按 digest 判定后补发布。

**摘要口径**：对**解析后的条目序列**取 sha256（`ENTRY_DELIMITER.join(entries)`），不是原始字节——
行尾归一化、BOM、条目间空行都不影响判定，判的始终是「内容是不是这一份」。

# 与 AWS transactional outbox 的关系（ADR-0058 §3）

借的是判据：意图与主写入的「发布」之间必须有可扫描的持久记录、消费方按去重键幂等、
只处理已提交的行。这里没有消息队列、没有投递语义，扫描器就是 `MemoryCommit.reconcile`。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_harness.memory.v2.file_state import (
    CorruptStateFile,
    MemoryStateRoot,
    StateFileError,
    file_lock,
    read_json,
    safe_id,
    utc_stamp,
    write_json,
)

logger = logging.getLogger(__name__)

#: 回执文件名前缀 → 提交者职责。前缀只用于观测/审计与测试配对；
#: ADR-0058 D10 明确没有 handler 注册表，对账不按前缀派发。
OP_PREFIX_TOOL = "mcf_"          # memory 工具 / capability service 的单目标写入
OP_PREFIX_JOB = "jobp_"          # formation apply
OP_PREFIX_STAGED = "stgp_"       # 待审写入批准后的重放
OP_PREFIX_BLOCKED = "blkp_"      # 被拦下写入的 promote
OP_PREFIX_DELETE = "del_"        # 治理删除的内容侧
OP_PREFIX_LEGACY = "legp_"       # 旧 completed-but-unpublished 补发布
OP_PREFIX_MIGRATION = "mig_"     # 旧记录一次性迁移（memory migration / identity mapping）


class ReceiptState:
    """四态字符串常量（不用 Enum：JSON 里就是这四个字面量，读写不做过映射）。"""

    PREPARED = "PREPARED"
    PUBLISHED = "PUBLISHED"
    COMMITTED = "COMMITTED"
    NEED_RECONCILE = "NEED_RECONCILE"

    TERMINAL = frozenset({COMMITTED})


#: 回执里可重放的最小意图集合。与 `commit._ACTIONS` 同源；分开定义是为了避免
#: receipts → commit 的循环导入。
_OPERATION_ACTIONS = frozenset({"add", "replace", "remove", "batch", "remove_by_digest"})
_RECEIPT_TARGETS = frozenset({"memory", "user"})


@dataclass
class ReceiptTarget:
    """一个目标文件的计划与进度。``planned=False`` = 尚未计划（多目标操作的后续目标）。"""

    before_digest: str | None = None
    after_digest: str | None = None
    planned: bool = False
    published: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "planned": self.planned,
            "published": self.published,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReceiptTarget:
        return cls(
            before_digest=payload.get("before_digest"),
            after_digest=payload.get("after_digest"),
            planned=bool(payload.get("planned")),
            published=bool(payload.get("published")),
        )


@dataclass
class CommitReceipt:
    operation_id: str
    state: str
    origin: str
    created_at: str
    updated_at: str
    targets: dict[str, ReceiptTarget] = field(default_factory=dict)
    operations: list[dict[str, Any]] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "operation_id": self.operation_id,
            "state": self.state,
            "origin": self.origin,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "targets": {name: target.as_dict() for name, target in sorted(self.targets.items())},
            "operations": self.operations,
            "payload": self.payload,
            "result": self.result,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CommitReceipt:
        operation_id = str(payload.get("operation_id") or "")
        state = str(payload.get("state") or "")
        if not operation_id or state not in {
            ReceiptState.PREPARED, ReceiptState.PUBLISHED,
            ReceiptState.COMMITTED, ReceiptState.NEED_RECONCILE,
        }:
            raise CorruptStateFile(f"receipt {operation_id or '?'} has an unknown state {state!r}")
        raw_targets = payload.get("targets") or {}
        if not isinstance(raw_targets, dict):
            raise CorruptStateFile(f"receipt {operation_id} has non-object targets")
        targets: dict[str, ReceiptTarget] = {}
        for name, value in raw_targets.items():
            if str(name) not in _RECEIPT_TARGETS:
                raise CorruptStateFile(f"receipt {operation_id} has unknown target {name!r}")
            if value is not None and not isinstance(value, dict):
                raise CorruptStateFile(f"receipt {operation_id} target {name!r} is not an object")
            targets[str(name)] = ReceiptTarget.from_dict(value or {})
        raw_operations = payload.get("operations") or []
        if not isinstance(raw_operations, list):
            raise CorruptStateFile(f"receipt {operation_id} has non-list operations")
        operations: list[dict[str, Any]] = []
        for item in raw_operations:
            if not isinstance(item, dict):
                raise CorruptStateFile(f"receipt {operation_id} has a non-object operation")
            if str(item.get("target")) not in _RECEIPT_TARGETS:
                raise CorruptStateFile(
                    f"receipt {operation_id} operation has unknown target "
                    f"{item.get('target')!r}")
            if str(item.get("action")) not in _OPERATION_ACTIONS:
                raise CorruptStateFile(
                    f"receipt {operation_id} operation has unknown action "
                    f"{item.get('action')!r}")
            if str(item.get("target")) not in targets:
                # 目标集与意图必须自洽：operations 里有、targets 里没有的意图
                # 会让缩表守卫看不到它，finish 后把从未发布的目标伪装成成功。
                raise CorruptStateFile(
                    f"receipt {operation_id} operation target "
                    f"{item.get('target')!r} is missing from targets")
            operations.append(dict(item))
        raw_payload = payload.get("payload") or {}
        if not isinstance(raw_payload, dict):
            raise CorruptStateFile(f"receipt {operation_id} has non-object payload")
        return cls(
            operation_id=operation_id,
            state=state,
            origin=str(payload.get("origin") or ""),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            targets=targets,
            operations=operations,
            payload=dict(raw_payload),
            result=(dict(payload["result"]) if isinstance(payload.get("result"), dict) else None),
            reason=payload.get("reason"),
        )


class ReceiptStore:
    """回执的读写与状态迁移。**不加自己的锁**：同一 op 的并发由 op 锁串行化（见 `commit`）。"""

    def __init__(self, root: MemoryStateRoot) -> None:
        self.root = root

    async def initialize(self) -> None:
        await asyncio.to_thread(self.root.ensure)

    # ---------------------------------------------------------------- 路径

    def path_for(self, operation_id: str) -> Path:
        return self.root.receipts_dir / f"{safe_id(operation_id, what='operation id')}.json"

    def lock_path(self, operation_id: str) -> Path:
        return self.root.receipt_lock_path(operation_id)

    def lock(self, operation_id: str) -> Iterator[None]:
        """同 op 的串行化（阻塞 IO，调用方负责 ``to_thread``）。"""
        return file_lock(self.lock_path(operation_id))

    # ---------------------------------------------------------------- 读

    def load(self, operation_id: str) -> CommitReceipt | None:
        try:
            payload = read_json(self.path_for(operation_id))
        except (CorruptStateFile, StateFileError) as error:
            # 坏回执不能当「没有回执」：那会允许一次重复发布。按 NEED_RECONCILE 处理。
            logger.warning("receipt unreadable (%s): %s", operation_id, error)
            return CommitReceipt(
                operation_id=operation_id, state=ReceiptState.NEED_RECONCILE,
                origin="", created_at="", updated_at="", reason=f"corrupt: {error}",
            )
        if payload is None:
            return None
        try:
            receipt = CommitReceipt.from_dict(payload)
            if receipt.operation_id != operation_id:
                # 文件名是调用方的幂等键；与 payload 不一致时按哪个都会重复发布或
                # 覆盖另一条操作，只能当损坏回执交给人工对账（review P4-3）。
                raise CorruptStateFile(
                    f"receipt filename {operation_id!r} does not match payload "
                    f"operation_id {receipt.operation_id!r}")
            return receipt
        except (CorruptStateFile, KeyError, TypeError, ValueError, AttributeError) as error:
            # 合法 JSON 但结构坏（state/action/targets 非法、条目缺字段）同样是
            # 「有回执但不认识」：既不能当不存在（会重复发布），也不能让一条坏文件
            # 把整个 reconcile / 启动装配打崩。语义与坏 JSON 一致：NEED_RECONCILE。
            logger.warning("receipt unreadable (%s): %s", operation_id, error)
            return CommitReceipt(
                operation_id=operation_id, state=ReceiptState.NEED_RECONCILE,
                origin="", created_at="", updated_at="", reason=f"corrupt: {error}",
            )

    def list_pending(self) -> list[CommitReceipt]:
        """非终态回执（含 NEED_RECONCILE）。扫描即读盘，坏文件计入并按待对账返回。"""
        pending: list[CommitReceipt] = []
        directory = self.root.receipts_dir
        if not directory.exists():
            return pending
        for path in sorted(directory.glob("*.json")):
            receipt = self.load(path.stem)
            if receipt is not None and receipt.state not in ReceiptState.TERMINAL:
                pending.append(receipt)
        return pending

    # ---------------------------------------------------------------- 写

    def _write(self, receipt: CommitReceipt) -> None:
        receipt.updated_at = utc_stamp()
        write_json(self.path_for(receipt.operation_id), receipt.as_dict())

    def prepare(
        self, operation_id: str, *, origin: str, targets: list[str],
        operations: list[dict[str, Any]], payload: dict[str, Any] | None = None,
    ) -> CommitReceipt:
        """首次落盘意图。已存在 ⇒ 原样返回（幂等，不覆盖既有进度）。"""
        existing = self.load(operation_id)
        if existing is not None:
            return existing
        # 落盘前先拒绝 targets/operations 不自洽的意图：否则 load 会把它判 corrupt，
        # 而调用方以为已经拿到一份可推进的 PREPARED 回执。
        planned_targets = {str(name) for name in targets}
        unknown = planned_targets - _RECEIPT_TARGETS
        if unknown:
            raise ValueError(f"unknown receipt target(s): {sorted(unknown)!r}")
        for item in operations:
            if str(item.get("target")) not in planned_targets:
                raise ValueError(
                    f"operation target {item.get('target')!r} is missing from targets")
            if str(item.get("action")) not in _OPERATION_ACTIONS:
                raise ValueError(f"unknown operation action: {item.get('action')!r}")
        stamp = utc_stamp()
        receipt = CommitReceipt(
            operation_id=operation_id, state=ReceiptState.PREPARED, origin=origin,
            created_at=stamp, updated_at=stamp,
            targets={name: ReceiptTarget() for name in targets},
            operations=operations, payload=dict(payload or {}),
        )
        self._write(receipt)
        return receipt

    def record_target_plan(
        self, operation_id: str, target: str, *, before_digest: str, after_digest: str,
        spec: dict[str, Any] | None = None,
    ) -> CommitReceipt:
        receipt = self._require(operation_id)
        entry = receipt.targets.setdefault(target, ReceiptTarget())
        entry.before_digest = before_digest
        entry.after_digest = after_digest
        entry.planned = True
        if spec is not None:
            # 重试时扩了 target：只写 plan 不写意图，崩在发布前会让 reconcile
            # 看到“有计划、无 spec”，按 noop 收尾成 false COMMITTED。
            receipt.operations = [
                item for item in receipt.operations
                if str(item.get("target")) != target
            ]
            receipt.operations.append(dict(spec))
        self._write(receipt)
        return receipt

    def mark_target_published(self, operation_id: str, target: str) -> CommitReceipt:
        receipt = self._require(operation_id)
        receipt.targets.setdefault(target, ReceiptTarget()).published = True
        self._write(receipt)
        return receipt

    def mark_partial(self, operation_id: str) -> CommitReceipt:
        """至少一个目标已发布、尚有未发布：置 ``PUBLISHED``（幂等，COMMITTED 不回退）。

        最后一个目标发布后不再置 PUBLISHED——此时只差终态写，调用方紧接着 ``finish``；
        中途崩溃时回执仍可对账（全部 after ⇒ 收尾 COMMITTED）。
        """
        receipt = self._require(operation_id)
        if receipt.state != ReceiptState.PREPARED:
            return receipt
        if all(plan.published for plan in receipt.targets.values()):
            return receipt
        receipt.state = ReceiptState.PUBLISHED
        self._write(receipt)
        return receipt

    def finish(
        self, operation_id: str, result: dict[str, Any] | None = None,
    ) -> CommitReceipt:
        """全部目标落地：置 ``COMMITTED``（幂等；NEED_RECONCILE 不回退）。

        ``result`` 是成功响应的可回放快照：同 operation id 的重试直接回放它，
        不再碰文件、也不再跑一遍 mutator（AC4「同 key 只一次逻辑更新」）。

        终态后正文不再有重放价值：``operations``/``payload``/``result`` 里的正文换成
        sha256 + 长度（保留审计与「是不是同一份意图」的可核对性），避免每条回执长期
        复制一份记忆正文——删除回执尤其不能把已删正文留在 receipts/ 里。
        未终态回执不动——对账补发布还要用它。
        """
        receipt = self._require(operation_id)
        if receipt.state != ReceiptState.NEED_RECONCILE:
            receipt.state = ReceiptState.COMMITTED
            receipt.reason = None
            if result is not None:
                receipt.result = dict(result)
            receipt.operations = _scrub_bodies(receipt.operations)
            receipt.payload = _scrub_bodies(receipt.payload)
            receipt.result = _scrub_bodies(receipt.result)
            self._write(receipt)
        return receipt

    def mark_needs_reconcile(self, operation_id: str, reason: str) -> CommitReceipt:
        receipt = self._require(operation_id)
        if receipt.state != ReceiptState.COMMITTED:
            receipt.state = ReceiptState.NEED_RECONCILE
            receipt.reason = reason
            self._write(receipt)
        return receipt

    def _require(self, operation_id: str) -> CommitReceipt:
        receipt = self.load(operation_id)
        if receipt is None:
            raise FileNotFoundError(f"no receipt {operation_id!r} to update")
        return receipt


_BODY_KEYS = frozenset({
    "content", "old_text", "matched_entry", "current_entries",
    "replaced_entry", "removed_entry", "replaced_entries", "removed_entries",
})


def _scrub_bodies(value: Any) -> Any:
    """递归把正文类字段替换成摘要 + 长度（只用于终态回执）。

    覆盖三种形状：单条正文（str）、条目列表（`current_entries`）、以及
    `replaced_entries` / `removed_entries` 这种 ``{序号: 正文}`` 映射。摘要保持
    「是不是同一份意图」可核对，正文不再长期留在回执里。
    """
    if isinstance(value, dict):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            if key in _BODY_KEYS:
                scrubbed.update(_scrub_body_field(key, item))
            else:
                scrubbed[key] = _scrub_bodies(item)
        return scrubbed
    if isinstance(value, list):
        return [_scrub_bodies(item) for item in value]
    return value


def scrub_bodies(value: Any) -> Any:
    """终态回执的正文脱敏器；调用方拿它把“本次意图”与已 scrub 的回执对齐比较。"""
    return _scrub_bodies(value)


def _scrub_body_field(key: str, item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        if not item:
            return {key: item}
        return {
            f"{key}_digest": hashlib.sha256(item.encode("utf-8")).hexdigest(),
            f"{key}_chars": len(item),
        }
    if isinstance(item, (list, tuple)) and all(isinstance(entry, str) for entry in item):
        return {
            f"{key}_digests": [
                hashlib.sha256(entry.encode("utf-8")).hexdigest() for entry in item
            ],
            f"{key}_chars": sum(len(entry) for entry in item),
        }
    if isinstance(item, dict):
        texts = {str(name): str(entry) for name, entry in item.items()}
        return {
            f"{key}_digests": {
                name: hashlib.sha256(entry.encode("utf-8")).hexdigest()
                for name, entry in texts.items()
            },
            f"{key}_chars": sum(len(entry) for entry in texts.values()),
        }
    return {key: _scrub_bodies(item)}


__all__ = [
    "OP_PREFIX_BLOCKED",
    "OP_PREFIX_DELETE",
    "OP_PREFIX_JOB",
    "OP_PREFIX_LEGACY",
    "OP_PREFIX_MIGRATION",
    "OP_PREFIX_STAGED",
    "OP_PREFIX_TOOL",
    "CommitReceipt",
    "ReceiptState",
    "ReceiptStore",
    "ReceiptTarget",
    "scrub_bodies",
]
