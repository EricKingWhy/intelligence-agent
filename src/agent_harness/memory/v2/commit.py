"""#1494 / ADR-0058 D2+D3：`MemoryCommit` —— 当前内容的**唯一**可变事实入口。

# 为什么是一个深模块

ADR-0056 §3.1 把「当前内容与 revision 的唯一可变事实入口」判给了 MemoryCommit。落地后：

- **写语义在 here**：add / replace / remove / batch / remove_by_digest 的匹配、来源扫描
  （delimiter / secret / threat）、容量上限、pin 与 revision 校验、批量原子性；
- **写机制在 `markdown_store`**：锁、重读、漂移备份、单文件原子替换；
- **可对账性在 `receipts`**：意图与每目标 before/after digest 先于文件替换落盘，
  重启/CLI 按 digest 判定「未发布 / 已发布 / 冲突」。

三者合起来满足 #1494 AC1/AC3/AC4/AC5：所有入口同一条路径、四个 kill 窗口可对账、
同 operation id 只发布一次、拒绝时零落盘、失败响应带 operation id。

# operation id 与幂等

`operation_id` 是**逻辑更新**的幂等键（不是每次函数调用的键）：

- tool / service 每次逻辑写入生成一个 `mcf_<uuid>`；同一次逻辑写入被重试（审批重放、
  formation 恢复、CLI 补发布）必须复用同一个 id，从而「同 key 只一次逻辑更新」；
- 固定形：`jobp_<job>`（formation apply）、`stgp_<staged>`（待审批准）、`blkp_<blocked>`
  （被拦下写入的 promote）、`del_<memory>`（治理删除）、`legp_<job>_<memory>`（旧数据补发布）、
  `mig_<memory>`（旧记录一次性迁移）。

同一 id 已有 `COMMITTED` 回执 ⇒ 直接回放记录的结果，不再碰磁盘（不重复追加、不重复删除）。

# 顺序（ADR-0058 D8）

**校验 → 回执 → 发布** 三步都在**同一个目标文件锁**内完成（`markdown_store.mutate_now`
的临界区）：校验失败连回执都不写（零落盘）；回执写不进去就不发布；发布后把回执置终态。
跨文件操作（按 digest 清理两个文件、系统镜像一次发布多条记录）逐目标执行；
`apply_batch_many` 先为全部目标写一份 PREPARED 回执，再逐目标替换，部分完成时
回执记录已发布/未发布目标，由重试或对账补齐——失败目标不会「没有回执可查」。

# 不冒称

单文件 `os.replace` 对并发读者原子；**跨文件不原子、断电不保证整体一致**。恢复靠回执对账，
不靠文件系统事务（ADR-0058 D1 末 / AC5）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_harness.memory.v2 import origin as origin_module
from agent_harness.memory.v2.file_state import (
    MEMORY_STATE_DIRECTORY_NAME,
    MemoryStateRoot,
)
from agent_harness.memory.v2.markdown_store import (
    ENTRY_DELIMITER,
    MarkdownMemoryStore,
    entries_digest,
    entry_digest,
    error_response,
    find_unique_match,
    normalize_entry,
    pinned_index,
    stale_entry_message,
)
from agent_harness.memory.v2.receipts import (
    OP_PREFIX_TOOL,
    CommitReceipt,
    ReceiptState,
    ReceiptStore,
)
from agent_harness.memory.v2.threat_patterns import first_threat_message

logger = logging.getLogger(__name__)

#: 动作集合（回执里可重放的最小意图）。
_ACTIONS = frozenset({"add", "replace", "remove", "batch", "remove_by_digest"})


class MemoryCommitError(RuntimeError):
    """发布阶段失败（文件拒写/IO 异常）。**不**等于 committed：内容未必落盘。

    携带 operation id：调用方（工具面、formation、CLI）把它交给用户/日志，
    这样「文件拒写」与「结果不确定」都能被追踪到同一条回执。
    """

    def __init__(self, operation_id: str, target: str, reason: object) -> None:
        self.operation_id = operation_id
        self.target = target
        self.reason = reason
        super().__init__(
            f"memory commit failed (operation {operation_id}, target {target}): {reason}"
        )


class MemoryBatchRejected(ValueError):
    """多目标批量在**写任何文件之前**被拒（校验/预算/pin）。

    与 `MemoryCommitError` 的区别是恢复语义：这里磁盘完全没动，records 事务回滚
    后重试只会得到同一个拒绝；生产方据此把它归成永久失败（formation DEGRADED），
    而不是让 job 无限重试。真正的发布失败（IO / 并发漂移）才用 `MemoryCommitError`。
    """

    def __init__(self, operation_id: str, target: str, reason: object) -> None:
        self.operation_id = operation_id
        self.target = target
        self.reason = reason
        super().__init__(
            f"memory batch rejected before publish (operation {operation_id}, "
            f"target {target}): {reason}"
        )


@dataclass
class ReconcileReport:
    """`reconcile()` 的读数（CLI 与启动日志共用）。"""

    committed: list[str] = field(default_factory=list)
    republished: list[str] = field(default_factory=list)
    needs_reconcile: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    examined: int = 0

    @property
    def ok(self) -> bool:
        return not (self.needs_reconcile or self.unreadable)

    def as_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "committed": list(self.committed),
            "republished": list(self.republished),
            "needs_reconcile": [list(item) for item in self.needs_reconcile],
            "unreadable": [list(item) for item in self.unreadable],
            "ok": self.ok,
        }


def new_operation_id(prefix: str = OP_PREFIX_TOOL) -> str:
    """新逻辑写入的 operation id（文件名安全；前缀即职责标记）。"""
    return f"{prefix}{uuid.uuid4().hex}"


def build_markdown_commit(
    engine: MarkdownMemoryStore, workspace_dir: Path | str,
) -> MemoryCommit:
    """按部署布局构造 commit（`<workspace>/memory-state/receipts/`）。

    装配层与两个一次性迁移入口共用这一个构造函数：状态根的位置只有一处定义，
    否则"迁移把回执写到了另一个目录"会让对账永远看不到那批发布。
    """
    root = MemoryStateRoot(Path(workspace_dir) / MEMORY_STATE_DIRECTORY_NAME)
    return MemoryCommit(engine, ReceiptStore(root), state_root=root)


class MemoryCommit:
    """当前内容提交 seam。一个进程一个实例，与 `MarkdownMemoryStore` 同生共死。"""

    def __init__(
        self, engine: MarkdownMemoryStore, receipts: ReceiptStore, *,
        state_root: MemoryStateRoot | None = None,
    ) -> None:
        self._engine = engine
        self._receipts = receipts
        self.root = state_root

    # ------------------------------------------------------------------ 只读口

    @property
    def engine(self) -> MarkdownMemoryStore:
        return self._engine

    def reset_consolidation_failures(self) -> None:
        self._engine.reset_consolidation_failures()

    async def resolve_entry(self, target: str, old_text: str, verb: str) -> dict[str, Any]:
        """``{"success": True, "matched_entry": <完整条目>}``，在锁内读当前态（不写盘）。"""
        def _resolve(entries: list[str], _limit: int) -> dict[str, Any]:
            located = self._locate(entries, normalize_entry(old_text), verb)
            if isinstance(located, dict):
                return located
            return {"success": True, "matched_entry": entries[located]}

        outcome = await asyncio.to_thread(
            self._engine.mutate_now, target, _resolve, skip_drift=True)
        return dict(outcome.response or {"success": True})

    async def resolve_batch_entries(
        self, target: str, operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """批量 dry-run：只回答「old_text 命中哪条」。模型给的 matched_entry 一律不认。"""
        spec = {**self._batch_spec(target, operations, trusted_pins=False), "dry_run": True}
        outcome = await asyncio.to_thread(
            self._engine.mutate_now, target, self._mutator_for(spec), skip_drift=True)
        return dict(outcome.response or {"success": True})

    # ------------------------------------------------------------------ 写入口

    async def add(
        self, target: str, content: str, *, operation_id: str | None = None,
        origin: str | None = None, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._perform,
            {"target": target, "action": "add", "content": content},
            operation_id=operation_id or new_operation_id(),
            origin=origin or origin_module.current_origin(),
            payload=payload,
        )

    async def replace(
        self, target: str, old_text: str, new_content: str, matched_entry: str | None = None,
        *, fallback_add: bool = False, operation_id: str | None = None,
        origin: str | None = None, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._perform,
            {
                "target": target, "action": "replace", "old_text": old_text,
                "content": new_content, "matched_entry": matched_entry,
                "fallback_add": bool(fallback_add),
            },
            operation_id=operation_id or new_operation_id(),
            origin=origin or origin_module.current_origin(),
            payload=payload,
        )

    async def remove(
        self, target: str, old_text: str, matched_entry: str | None = None, *,
        missing_ok: bool = False, operation_id: str | None = None,
        origin: str | None = None, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """删除一条；``missing_ok`` 只给系统镜像重放用（目标已不在 = 已经删过）。"""
        return await asyncio.to_thread(
            self._perform,
            {
                "target": target, "action": "remove", "old_text": old_text,
                "matched_entry": matched_entry, "missing_ok": bool(missing_ok),
            },
            operation_id=operation_id or new_operation_id(),
            origin=origin or origin_module.current_origin(),
            payload=payload,
        )

    def content_error(self, content: str) -> str | None:
        """内容校验（delimiter / threat）。系统镜像在碰盘前用它做一次全量预检。"""
        return self._write_guard(normalize_entry(content))

    async def apply_batch(
        self, target: str, operations: list[dict[str, Any]], *, trusted_pins: bool = False,
        allow_empty: bool = False, operation_id: str | None = None,
        origin: str | None = None, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``trusted_pins=True`` 只给系统侧调用：审批重放携带 stage 时钉住的条目全文，
        系统镜像（formation / blocked promote）携带自己刚从内容面读到的 match。

        其余调用方（含模型工具面）的 ``matched_entry`` 一律剥掉，按 ``old_text`` 重新定位。
        ``allow_empty=True`` 只给系统镜像用：它可能有意清空一个文件（例如全部撤回），
        模型工具面的 batch 仍拒绝「一次删空」。
        """
        spec = self._batch_spec(
            target, operations, trusted_pins=trusted_pins, allow_empty=allow_empty)
        return await asyncio.to_thread(
            self._perform, spec,
            operation_id=operation_id or new_operation_id(),
            origin=origin or origin_module.current_origin(),
            payload=payload,
        )

    def _preview_batch(
        self, spec: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """在**不碰盘**的前提下跑一遍 batch 判定。

        返回 ``(错误响应, before_digest, after_digest)``：成功时错误为 ``None``，
        两个 digest 供多目标回执在发布前记录计划。预检复用与 `apply_batch` 相同的
        mutator；consolidation 计数按原值还原——预检不是一次模型尝试，不能消耗
        自纠额度。整个快照/还原在引擎锁内完成，避免与并发写入的计数互相覆盖。
        """
        target = str(spec["target"])
        entries = self._engine.disk_entries(target)
        if entries is None:
            return (
                error_response(
                    f"{self._engine.path_for(target).name} is unreadable; refusing to write."),
                None, None)
        with self._engine.preserve_consolidation_failures():
            result = self._mutator_for(spec)(list(entries), self._engine.char_limit(target))
        if isinstance(result, dict):
            return result, None, None
        return None, entries_digest(entries), entries_digest(list(result[0]))

    async def apply_batch_many(
        self, plans: Sequence[tuple[str, list[dict[str, Any]]]], *,
        trusted_pins: bool = False, allow_empty: bool = False,
        operation_id: str, origin: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """多目标系统镜像：**一份回执覆盖全部目标**，预检失败零落盘。

        与逐个 `apply_batch` 的区别是回执先于第一次写入、且覆盖所有目标。跨文件
        替换本身不可能原子（ADR-0058 D1）；后一个目标失败时，已发布目标的事实
        留在同一份 PREPARED 回执里，job / blocked 的重试与启动对账都能按 before/after
        继续，不会出现「内容已进 Markdown、却没有任何回执可查」的孤儿（review P1-1/P1-2）。

        预检失败抛 `MemoryBatchRejected`（磁盘未动）；已经开始发布后的失败抛
        `MemoryCommitError`（回执已落盘、可对账）。
        """
        if not plans:
            raise ValueError("plans list is empty")
        return await asyncio.to_thread(
            self._apply_batch_many_sync,
            [(target, list(operations)) for target, operations in plans],
            trusted_pins=trusted_pins, allow_empty=allow_empty,
            operation_id=operation_id,
            origin=origin or origin_module.current_origin(),
            payload=payload,
        )

    def _apply_batch_many_sync(
        self, plans: list[tuple[str, list[dict[str, Any]]]], *, trusted_pins: bool,
        allow_empty: bool, operation_id: str, origin: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        targets = [target for target, _ in plans]
        if len(set(targets)) != len(targets):
            raise ValueError(f"duplicate target in batch: {targets!r}")
        label = ",".join(targets)
        with self._receipts.lock(operation_id):
            replay = self._replay_if_committed(operation_id)
            if replay is not None:
                return replay
            existing = self._receipts.load(operation_id)
            if existing is not None and existing.state == ReceiptState.NEED_RECONCILE:
                raise MemoryCommitError(
                    operation_id, label, f"needs reconcile: {existing.reason}")

            prepared: list[tuple[dict[str, Any], str, str]] = []
            for target, operations in plans:
                spec = self._batch_spec(
                    target, operations, trusted_pins=trusted_pins,
                    allow_empty=allow_empty)
                error, before, after = self._preview_batch(spec)
                if error is not None:
                    # 还没有碰任何文件/回执：整批拒绝，records 事务可以安全回滚。
                    raise MemoryBatchRejected(
                        operation_id, target, error.get("error") or error)
                assert before is not None and after is not None
                prepared.append((spec, before, after))

            if existing is None:
                try:
                    self._receipts.prepare(
                        operation_id, origin=origin, targets=targets,
                        operations=[spec for spec, _, _ in prepared],
                        payload=payload,
                    )
                except (OSError, RuntimeError) as error:
                    raise MemoryCommitError(
                        operation_id, label, f"receipt prepare failed: {error}") from error

            for spec, before, after in prepared:
                try:
                    self._receipts.record_target_plan(
                        operation_id, str(spec["target"]),
                        before_digest=before, after_digest=after,
                    )
                except (OSError, RuntimeError) as error:
                    raise MemoryCommitError(
                        operation_id, str(spec["target"]),
                        f"receipt plan failed: {error}") from error

            published: list[str] = []
            for spec, _before, after in prepared:
                target = str(spec["target"])
                mutator = self._mutator_for(spec)

                def _guarded(
                    entries: list[str], limit: int, *, _mutator: Callable = mutator,
                    _after: str = after, _target: str = target,
                ) -> Any:
                    result = _mutator(entries, limit)
                    if isinstance(result, dict):
                        return result
                    if entries_digest(list(result[0])) != _after:
                        # 预检之后文件被外部改过：这一目标写下去就会和回执计划不符，
                        # 更会在其它目标已发布时放大半批窗口。停在这里，让对账判定。
                        return error_response(
                            f"{self._engine.path_for(_target).name} changed after preflight; "
                            f"refusing to publish a partial batch. Run `memory reconcile` "
                            f"for operation {operation_id}.",
                            needs_reconcile=True, operation_id=operation_id,
                        )
                    return result

                try:
                    outcome = self._engine.mutate_now(target, _guarded)
                except (OSError, RuntimeError) as error:
                    raise MemoryCommitError(operation_id, target, error) from error
                if outcome.response is not None:
                    raise MemoryCommitError(
                        operation_id, target,
                        outcome.response.get("error") or "markdown batch apply failed")
                try:
                    self._receipts.mark_target_published(operation_id, target)
                except (OSError, RuntimeError) as error:
                    raise MemoryCommitError(
                        operation_id, target,
                        f"content published but receipt update failed: {error}",
                    ) from error
                published.append(self._engine.path_for(target).name)

            response = {
                "success": True, "operation_id": operation_id,
                "published": list(published),
                "targets": {target: True for target in targets},
            }
            try:
                self._receipts.finish(operation_id, result=response)
            except (OSError, RuntimeError) as error:
                raise MemoryCommitError(
                    operation_id, label,
                    f"content published but receipt update failed: {error}",
                ) from error
            return response


    async def remove_by_digest(
        self, digests: Sequence[str] = (), *,
        digests_by_target: Mapping[str, Sequence[str]] | None = None,
        operation_id: str | None = None,
        origin: str | None = None, payload: dict[str, Any] | None = None,
    ) -> list[str]:
        """按正文哈希清理文件；返回被清理的文件名（不返回内容）。

        墓碑是 content-free 的，除哈希外没有分拣线索，所以不能替调用方猜 USER/MEMORY。
        默认清理两个目标；`digests_by_target` 给需要按文件区分共享正文的调用方
        （删除记录已不在时，共享判定只能按目标各自做，见 `_mirror_deleted`）。
        **幂等**：同一 operation id 的重复调用回放第一次的结果，不会再删任何东西。
        """
        if digests_by_target is None:
            wanted = {"memory": list(digests), "user": list(digests)}
        else:
            wanted = {
                str(target): [str(item) for item in values]
                for target, values in digests_by_target.items()
            }
        if not any(wanted.values()):
            return []
        return await asyncio.to_thread(
            self._remove_by_digest_sync, wanted,
            operation_id or new_operation_id(),
            origin or origin_module.current_origin(),
            payload,
        )

    # ------------------------------------------------------------------ 单目标提交

    def _perform(
        self, spec: dict[str, Any], *, operation_id: str, origin: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        target = str(spec["target"])
        with self._receipts.lock(operation_id):
            replay = self._replay_if_committed(operation_id)
            if replay is not None:
                return replay
            conflict = self._conflict_if_uncertain(operation_id, target)
            if conflict is not None:
                return conflict
            return self._publish_one(
                spec, operation_id=operation_id, origin=origin, payload=payload)

    def _publish_one(
        self, spec: dict[str, Any], *, operation_id: str, origin: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        target = str(spec["target"])
        mutator = self._mutator_for(spec)
        prepared = {"done": False}

        def _wrapped(entries: list[str], limit: int) -> Any:
            result = mutator(entries, limit)
            if not isinstance(result, dict):
                new_entries = list(result[0])
                self._receipts.prepare(
                    operation_id, origin=origin, targets=[target],
                    operations=[spec], payload=payload,
                )
                self._receipts.record_target_plan(
                    operation_id, target,
                    before_digest=entries_digest(entries),
                    after_digest=entries_digest(new_entries),
                )
                prepared["done"] = True
            return result

        try:
            outcome = self._engine.mutate_now(target, _wrapped)
        except (OSError, RuntimeError) as error:
            raise MemoryCommitError(operation_id, target, error) from error

        if outcome.response is not None:
            response = dict(outcome.response)
            response.setdefault("operation_id", operation_id)
            return response

        response = self._engine.success_response(
            target, outcome.entries_after or [], outcome.message, **outcome.extra)
        response["operation_id"] = operation_id
        if outcome.drift_backup:
            response["drift_backup"] = outcome.drift_backup
        if prepared["done"]:
            try:
                self._receipts.mark_target_published(operation_id, target)
                self._receipts.finish(operation_id, result=_result_for_replay(response))
            except (OSError, RuntimeError) as error:
                # 文件已经替换成功，但终态回执没写下去：内容已发布、回执停在 PREPARED。
                # 必须当成「已发布、待对账」上报，不能返回成功，也不能让裸 OSError
                # 丢掉 operation id（启动 reconcile 会按 digest 收尾）。
                raise MemoryCommitError(
                    operation_id, target,
                    f"content published but receipt update failed: {error}",
                ) from error
        return response

    def _replay_if_committed(self, operation_id: str) -> dict[str, Any] | None:
        receipt = self._receipts.load(operation_id)
        if receipt is None or receipt.state != ReceiptState.COMMITTED:
            return None
        recorded = dict(receipt.result or {})
        recorded.setdefault("success", True)
        recorded["operation_id"] = operation_id
        recorded["replayed"] = True
        if receipt.result is None:
            # 终态是事实，但旧回执/对账收尾没有留下可回放的结果字段。不要用
            # success=True 冒充「拿到了完整原始响应」——调用方需要知道哪些读数缺失。
            recorded["result_unknown"] = True
        return recorded

    def _conflict_if_uncertain(self, operation_id: str, target: str) -> dict[str, Any] | None:
        """已有未终态回执：先按 digest 判一次，再决定「续跑/重放/冲突」。

        返回 None ⇒ 磁盘仍是 before（或回执没有该目标的计划），可继续按本次调用完成。
        """
        receipt = self._receipts.load(operation_id)
        if receipt is None:
            return None
        if receipt.state == ReceiptState.NEED_RECONCILE:
            return self._needs_reconcile_response(receipt)
        plan = receipt.targets.get(target)
        if plan is None or not plan.planned:
            return None
        disk = self._engine.disk_entries(target)
        if disk is None:
            return self._needs_reconcile_response(receipt, "target file is unreadable")
        digest = entries_digest(disk)
        if digest == plan.after_digest:
            try:
                self._receipts.mark_target_published(operation_id, target)
                self._receipts.finish(operation_id, result=receipt.result)
            except (OSError, RuntimeError) as error:
                # 内容已经是目标态，只是回执没写完：按「已发布、待对账」上报。
                raise MemoryCommitError(
                    operation_id, target,
                    f"content already published but receipt update failed: {error}",
                ) from error
            return self._replay_if_committed(operation_id)
        if digest == plan.before_digest:
            return None
        try:
            self._receipts.mark_needs_reconcile(
                operation_id, f"{target}: disk matches neither before nor after")
        except (OSError, RuntimeError) as error:
            raise MemoryCommitError(
                operation_id, target, f"receipt update failed: {error}") from error
        return self._needs_reconcile_response(
            self._receipts.load(operation_id) or receipt)

    def _needs_reconcile_response(
        self, receipt: CommitReceipt, reason: str | None = None,
    ) -> dict[str, Any]:
        detail = reason or receipt.reason or "outcome is uncertain"
        return error_response(
            f"Memory write outcome is uncertain ({detail}); nothing was retried. "
            f"Run `memory reconcile` to resolve operation {receipt.operation_id}.",
            needs_reconcile=True,
            operation_id=receipt.operation_id,
        )

    # ------------------------------------------------------------------ 多目标：按 digest 清理

    def _remove_by_digest_sync(
        self, digests_by_target: Mapping[str, list[str]], operation_id: str, origin: str,
        payload: dict[str, Any] | None,
    ) -> list[str]:
        wanted = {
            str(target): set(values)
            for target, values in digests_by_target.items() if values
        }
        if not wanted:
            return []
        with self._receipts.lock(operation_id):
            replay = self._replay_if_committed(operation_id)
            if replay is not None:
                return [str(item) for item in replay.get("cleaned") or []]
            receipt = self._receipts.load(operation_id)
            if receipt is not None and receipt.state == ReceiptState.NEED_RECONCILE:
                raise MemoryCommitError(
                    operation_id, "memory+user", f"needs reconcile: {receipt.reason}")

            targets = tuple(wanted)
            specs = [
                {"target": target, "action": "remove_by_digest", "digests": sorted(wanted[target])}
                for target in targets
            ]
            try:
                self._receipts.prepare(
                    operation_id, origin=origin, targets=list(targets),
                    operations=specs, payload=payload,
                )
            except (OSError, RuntimeError) as error:
                # 还没碰文件：回执写不进去就不发布（零落盘）。
                raise MemoryCommitError(
                    operation_id, "memory+user", f"receipt prepare failed: {error}") from error
            cleaned: list[str] = []
            for index, spec in enumerate(specs):
                cleaned.extend(
                    self._publish_digest_target(
                        spec, operation_id, remaining=index < len(specs) - 1))
            try:
                self._receipts.finish(
                    operation_id, result={"cleaned": list(cleaned), "success": True})
            except (OSError, RuntimeError) as error:
                # 内容已经清理，终态回执没写下去：不能报 committed（AC4）。
                raise MemoryCommitError(
                    operation_id, "memory+user",
                    f"content removed but receipt update failed: {error}",
                ) from error
            return cleaned

    def _publish_digest_target(
        self, spec: dict[str, Any], operation_id: str, *, remaining: bool,
    ) -> list[str]:
        target = str(spec["target"])
        cleaned: list[str] = []
        mutator = self._mutator_for(spec)
        prepared = {"removed": 0}

        def _wrapped(entries: list[str], limit: int) -> Any:
            result = mutator(entries, limit)
            if not isinstance(result, dict):
                new_entries = list(result[0])
                self._receipts.record_target_plan(
                    operation_id, target,
                    before_digest=entries_digest(entries),
                    after_digest=entries_digest(new_entries),
                )
                prepared["removed"] = int(result[2].get("removed_by_digest") or 0)
            else:
                # 零命中：目标状态未变，记一条 planned 计划（reconcile 判为已完成）。
                self._receipts.record_target_plan(
                    operation_id, target,
                    before_digest=entries_digest(entries),
                    after_digest=entries_digest(entries),
                )
            return result

        try:
            outcome = self._engine.mutate_now(target, _wrapped)
        except (OSError, RuntimeError) as error:
            raise MemoryCommitError(operation_id, target, error) from error
        if outcome.response is not None and not outcome.response.get("success"):
            raise MemoryCommitError(operation_id, target, outcome.response.get("error"))
        if prepared["removed"]:
            try:
                self._receipts.mark_target_published(operation_id, target)
                cleaned.append(self._engine.path_for(target).name)
                if remaining:
                    self._receipts.mark_partial(operation_id)
            except (OSError, RuntimeError) as error:
                raise MemoryCommitError(
                    operation_id, target,
                    f"content removed but receipt update failed: {error}",
                ) from error
        return cleaned

    # ------------------------------------------------------------------ 对账

    async def reconcile(self) -> ReconcileReport:
        """对账全部未终态回执（幂等、可重入）；启动与 `memory reconcile` 共用。

        ADR-0058 D10：没有 handler 注册表——回执只描述内容发布是否落地，后置处理
        由各入口自己按 operation id 回放，不在对账里按前缀猜测职责。
        """
        await self._receipts.initialize()
        report = ReconcileReport()
        for receipt in self._receipts.list_pending():
            report.examined += 1
            try:
                finished, republished = await asyncio.to_thread(
                    self._reconcile_one, receipt.operation_id)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                # 单条回执写不进去或意图不可重放（磁盘满/只读/未知 action）不能阻断整轮
                # 对账，也不能丢掉归因：operation id 进 unreadable，CLI 与启动日志都能
                # 定位到同一份文件。
                report.unreadable.append(
                    (receipt.operation_id,
                     f"reconcile failed: {type(error).__name__}: {error}"))
                continue
            if finished is None:
                if republished:
                    report.republished.append(receipt.operation_id)
                report.unreadable.append(
                    (receipt.operation_id, "target file unreadable; will retry"))
                continue
            if republished:
                report.republished.append(finished.operation_id)
            if finished.state == ReceiptState.NEED_RECONCILE:
                report.needs_reconcile.append(
                    (finished.operation_id, finished.reason or "needs reconcile"))
                continue
            report.committed.append(finished.operation_id)
        return report

    def _reconcile_one(self, operation_id: str) -> tuple[CommitReceipt | None, bool]:
        with self._receipts.lock(operation_id):
            receipt = self._receipts.load(operation_id)
            if receipt is None or receipt.state in (
                ReceiptState.COMMITTED, ReceiptState.NEED_RECONCILE,
            ):
                return receipt, False
            verdict, republished = self._resolve_receipt(receipt)
            return (
                None if verdict == "unreadable" else self._receipts.load(operation_id),
                republished,
            )

    def _resolve_receipt(self, receipt: CommitReceipt) -> tuple[str, bool]:
        """返回 ``(committed|needs_reconcile|unreadable, 本轮是否补发布过)``。"""
        if not receipt.targets:
            self._receipts.finish(receipt.operation_id)
            return "committed", False
        republished = False
        for target, plan in receipt.targets.items():
            disk = self._engine.disk_entries(target)
            if disk is None:
                logger.warning("reconcile skipped (%s): %s is unreadable",
                               receipt.operation_id, target)
                return "unreadable", republished
            digest = entries_digest(disk)
            if plan.planned and digest == plan.after_digest:
                continue
            if plan.planned and digest != plan.before_digest:
                self._receipts.mark_needs_reconcile(
                    receipt.operation_id,
                    f"{target}: disk matches neither before nor after",
                )
                return "needs_reconcile", republished
            verdict = self._republish_target(receipt, target, digest)
            if verdict == "failed":
                return "needs_reconcile", republished
            republished = republished or verdict == "published"
        self._receipts.finish(receipt.operation_id, result=receipt.result)
        return "committed", republished

    def _republish_target(
        self, receipt: CommitReceipt, target: str, before_digest: str,
    ) -> str:
        """按回执里记录的意图补发布一个目标（确定性：磁盘仍等于 before）。

        返回 ``published`` / ``noop`` / ``failed``；``failed`` ⇒ 意图不再可重放
        （mutator 现在返回失败），置 NEED_RECONCILE。
        """
        specs = [op for op in receipt.operations if str(op.get("target")) == target]
        if not specs:
            # 该目标本来无事可做（多目标操作里未计划的那一侧）。
            self._receipts.record_target_plan(
                receipt.operation_id, target, before_digest=before_digest,
                after_digest=before_digest,
            )
            return "noop"
        if len(specs) != 1:
            self._receipts.mark_needs_reconcile(
                receipt.operation_id, f"{target}: receipt has {len(specs)} specs")
            return "failed"
        spec = specs[0]
        mutator = self._mutator_for(spec)
        published = {"done": False}

        def _wrapped(entries: list[str], limit: int) -> Any:
            result = mutator(entries, limit)
            if isinstance(result, dict):
                return result
            self._receipts.record_target_plan(
                receipt.operation_id, target,
                before_digest=entries_digest(entries),
                after_digest=entries_digest(list(result[0])),
            )
            published["done"] = True
            return result

        try:
            outcome = self._engine.mutate_now(target, _wrapped)
        except (OSError, RuntimeError) as error:
            logger.warning(
                "reconcile republish failed (%s/%s): %s",
                receipt.operation_id, target, type(error).__name__,
            )
            return "failed"
        if outcome.response is not None:
            if outcome.response.get("success"):
                # 幂等空操作（例如重复 add）：按 after 计划收尾。
                self._receipts.record_target_plan(
                    receipt.operation_id, target,
                    before_digest=before_digest, after_digest=before_digest,
                )
                return "noop"
            self._receipts.mark_needs_reconcile(
                receipt.operation_id,
                f"{target}: intent no longer applies ({outcome.response.get('error')})",
            )
            return "failed"
        if published["done"]:
            self._receipts.mark_target_published(receipt.operation_id, target)
            return "published"
        return "noop"

    # ------------------------------------------------------------------ 意图 → mutator

    def _batch_spec(
        self, target: str, operations: list[dict[str, Any]], *, trusted_pins: bool,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        if not operations:
            raise ValueError("operations list is empty")
        ops = [
            {
                "action": (operation or {}).get("action"),
                "content": normalize_entry(
                    (operation or {}).get("content") or (operation or {}).get("new_text") or ""),
                "old_text": normalize_entry((operation or {}).get("old_text") or ""),
                # 系统侧 pin：审批重放与系统镜像能携带（模型直填等于绕过 old_text
                # 改任意条目）。
                "matched_entry": (
                    (operation or {}).get("matched_entry") if trusted_pins else None),
                # 系统镜像重放要幂等：条目已经不在了 = 这次撤回已经完成，不是失败。
                # 只给带系统 pin 的内部调用；模型直填 missing_ok 等于把「未命中」变成成功。
                "missing_ok": bool(trusted_pins and (operation or {}).get("missing_ok")),
            }
            for operation in operations
        ]
        return {
            "target": target, "action": "batch", "operations": ops,
            "allow_empty": allow_empty,
        }

    def _mutator_for(self, spec: dict[str, Any]) -> Callable[[list[str], int], Any]:
        action = str(spec.get("action"))
        if action not in _ACTIONS:
            raise ValueError(f"unknown memory commit action: {action!r}")
        target = str(spec["target"])
        if action == "add":
            return lambda entries, limit: self._op_add(target, entries, limit, spec)
        if action == "replace":
            return lambda entries, limit: self._op_replace(target, entries, limit, spec)
        if action == "remove":
            return lambda entries, limit: self._op_remove(target, entries, limit, spec)
        if action == "batch":
            return lambda entries, limit: self._op_batch(target, entries, limit, spec)
        return lambda entries, limit: self._op_drop_digests(target, entries, spec)

    # ------------------------------------------------------------------ 校验

    def _locate(
        self, entries: list[str], old_text: str, verb: str, matched_entry: str | None = None,
    ) -> int | dict[str, Any]:
        """``old_text`` 选中的条目下标，或编辑应返回的错误 dict。

        staged 写入携带审阅时的**完整条目**（``matched_entry``）：只有那一条合格，
        重放不会命中一条「仍包含 old_text 的更新条目」。
        """
        if matched_entry is not None:
            idx = pinned_index(entries, matched_entry)
            return idx if idx is not None else error_response(stale_entry_message(matched_entry))
        idx, ambiguous = find_unique_match(entries, old_text)
        if ambiguous:
            return error_response(
                f"Multiple entries matched '{old_text}'. Be more specific.",
                matches=[e[:80] + ("..." if len(e) > 80 else "") for e in entries if old_text in e],
            )
        if idx is None:
            return self._engine.consolidation_failure(
                error_response(
                    f"No entry matched '{old_text}'. Check current_entries below and retry with the "
                    f"exact text of the entry you want to {verb}.",
                    current_entries=entries,
                )
            )
        return idx

    # ------------------------------------------------------------------ 操作实现

    def _op_add(
        self, target: str, entries: list[str], limit: int, spec: dict[str, Any],
    ) -> Any:
        content = normalize_entry(str(spec.get("content") or ""))
        if not content:
            return self._engine.consolidation_failure(
                error_response("Content cannot be empty."))
        rejection = self._write_guard(content)
        if rejection is not None:
            return self._engine.consolidation_failure(rejection)
        if content in entries:
            return self._engine.success_response(
                target, entries, "Entry already exists (no duplicate added).", deduplicated=True)
        if len(ENTRY_DELIMITER.join(entries + [content])) > limit:
            return self._engine.failure_with_entries(
                target, entries,
                f"Memory at {len(ENTRY_DELIMITER.join(entries)):,}/{limit:,} chars. Adding this entry "
                f"({len(content)} chars) would exceed the limit. Consolidate now: use 'replace' to "
                f"merge overlapping entries into shorter ones or 'remove' stale or less important "
                f"entries (see current_entries below), then retry this add — all in this turn.",
            )
        return entries + [content], "Entry added.", {"deduplicated": False}

    def _op_replace(
        self, target: str, entries: list[str], limit: int, spec: dict[str, Any],
    ) -> Any:
        old_text = normalize_entry(str(spec.get("old_text") or ""))
        content = normalize_entry(str(spec.get("content") or ""))
        rejection: dict[str, Any] | None = None
        if not old_text:
            rejection = error_response("old_text cannot be empty.")
        elif not content:
            rejection = error_response(
                "new_content cannot be empty. Use 'remove' to delete entries.")
        else:
            rejection = self._write_guard(content)
        if rejection is not None:
            if spec.get("fallback_add"):
                # 上游语义（capability 的 markdown 镜像）：replace 失败时退回 add，
                # 让新内容仍被记住；add 也失败则把 add 的失败原样交给调用方。
                return self._op_add(target, entries, limit, {"content": content})
            return rejection
        located = self._locate(entries, old_text, "replace", spec.get("matched_entry"))
        if isinstance(located, dict):
            if spec.get("fallback_add"):
                return self._op_add(target, entries, limit, {"content": content})
            return located
        replaced = entries[:located] + [content] + entries[located + 1:]
        total = len(ENTRY_DELIMITER.join(replaced))
        if total > limit:
            return self._engine.failure_with_entries(
                target, entries,
                f"Replacement would put memory at {total:,}/{limit:,} chars. Shorten the new "
                f"content, or 'remove' other stale or less important entries to make room (see "
                f"current_entries below), then retry — all in this turn.",
            )
        return replaced, "Entry replaced.", {"replaced_entry": entries[located]}

    def _op_remove(
        self, target: str, entries: list[str], limit: int, spec: dict[str, Any],
    ) -> Any:
        old_text = normalize_entry(str(spec.get("old_text") or ""))
        if not old_text:
            return error_response("old_text cannot be empty.")
        matched_entry = spec.get("matched_entry")
        missing_ok = bool(spec.get("missing_ok"))
        if missing_ok:
            if matched_entry is not None:
                if pinned_index(entries, matched_entry) is None:
                    return self._engine.success_response(
                        target, entries, "Entry already absent (no-op).", already_absent=True)
            else:
                idx, ambiguous = find_unique_match(entries, old_text)
                if ambiguous:
                    return error_response(
                        f"Multiple entries matched '{old_text}'. Be more specific.",
                        matches=[
                            e[:80] + ("..." if len(e) > 80 else "")
                            for e in entries if old_text in e
                        ],
                    )
                if idx is None:
                    return self._engine.success_response(
                        target, entries, "Entry already absent (no-op).", already_absent=True)
        located = self._locate(entries, old_text, "remove", matched_entry)
        if isinstance(located, dict):
            return located
        return entries[:located] + entries[located + 1:], "Entry removed.", {
            "removed_entry": entries[located],
        }

    def _op_batch(
        self, target: str, entries: list[str], limit: int, spec: dict[str, Any],
    ) -> Any:
        ops = list(spec.get("operations") or [])
        if not ops:
            return error_response("operations list is empty.")
        # 先扫所有 add/replace 内容，再碰磁盘——一个投毒 op 拒绝整批。
        # 只查会落盘的 op：remove 的 content 不参与写入，拿它拒绝一次合法删除是误伤。
        for index, operation in enumerate(ops, 1):
            if operation.get("action") not in {"add", "replace"} or not operation["content"]:
                continue
            rejection = self._write_guard(
                str(operation["content"]), prefix=f"Operation {index}: ")
            if rejection is not None:
                return rejection
        working = list(entries)  # 整批校验通过才提交
        matched: list[str | None] = []
        for index, operation in enumerate(ops, 1):
            action = operation.get("action")
            message, previous = self._apply_batch_op(
                working, str(action or ""), str(operation.get("content") or ""),
                str(operation.get("old_text") or ""),
                f"Operation {index} ({action or 'unknown'})",
                operation.get("matched_entry"),
                missing_ok=bool(operation.get("missing_ok")),
            )
            if message:
                return self._engine.batch_failure(target, entries, message)
            matched.append(previous)
        if spec.get("dry_run"):
            # 只读解析（审批 stage 用）：只回答「old_text 命中哪条」，不判预算、不落盘。
            return {"success": True, "matched_entries": list(matched)}
        if entries and not working and not spec.get("allow_empty"):
            # #103419：把最后一条删掉的 consolidation batch 会提交一个空文件当成功。
            # 拒绝；单条 remove() 才是有意清空的路径。
            label = self._engine.path_for(target).name
            return self._engine.batch_failure(
                target, entries,
                f"Refusing to empty {label}: this batch would remove every entry from a "
                f"previously non-empty store. Keep at least one entry — merge overlapping "
                f"entries into a shorter one instead of removing the last one. To delete the "
                f"final entry deliberately, use single remove() calls.",
            )
        total = len(ENTRY_DELIMITER.join(working))  # 只在最终态校验预算
        if total > limit:
            return self._engine.batch_failure(
                target, entries,
                f"After applying all {len(ops)} operations, memory would be at "
                f"{total:,}/{limit:,} chars -- over the limit. Remove or shorten more "
                f"entries in the same batch, then retry.",
            )
        replaced: dict[int, str] = {}
        removed: dict[int, str] = {}
        for index, (operation, previous) in enumerate(zip(ops, matched), 1):
            if previous is None:
                continue
            if operation.get("action") == "replace":
                replaced[index] = previous
            else:
                removed[index] = previous
        extra: dict[str, Any] = {}
        if replaced:
            extra["replaced_entries"] = replaced
        if removed:
            extra["removed_entries"] = removed
        return working, f"Applied {len(ops)} operation(s).", extra

    def _op_drop_digests(
        self, target: str, entries: list[str], spec: dict[str, Any],
    ) -> Any:
        wanted = {str(item) for item in (spec.get("digests") or [])}
        kept = [entry for entry in entries if entry_digest(entry) not in wanted]
        removed = len(entries) - len(kept)
        if not removed:
            return {"success": True, "removed_by_digest": 0}
        return kept, f"Removed {removed} entry(ies) matched by digest.", {
            "removed_by_digest": removed,
        }

    @staticmethod
    def _apply_batch_op(
        working: list[str], action: str, content: str, old_text: str, position: str,
        matched_entry: str | None = None, *, missing_ok: bool = False,
    ) -> tuple[str | None, str | None]:
        """把一个 batch op 应用到 *working*；返回 ``(错误消息, 被替换/删除的原条目)``。"""
        if action == "add":
            if not content:
                return f"{position}: content is required.", None
            if content not in working:  # 幂等：重复条目跳过，不让整批失败
                working.append(content)
            return None, None
        if action not in ("replace", "remove"):
            return f"{position}: unknown action. Use add, replace, or remove.", None
        if not old_text:
            return f"{position}: old_text is required.", None
        if action == "replace" and not content:
            return f"{position}: content is required (use action='remove' to delete).", None
        if matched_entry is not None:
            index = pinned_index(working, matched_entry)
            if index is None:
                if missing_ok and action == "remove":
                    return None, None
                return f"{position}: {stale_entry_message(matched_entry)}", None
        else:
            index, ambiguous = find_unique_match(working, old_text)
            if ambiguous:
                return f"{position}: '{old_text}' matched multiple distinct entries -- be more specific.", None
            if index is None:
                if missing_ok and action == "remove":
                    return None, None
                return f"{position}: no entry matched '{old_text}'.", None
        previous_content = working[index]
        working[index:index + 1] = [content] if action == "replace" else []
        return None, previous_content

    @staticmethod
    def _write_guard(content: str, *, prefix: str = "") -> dict[str, Any] | None:
        """来源/扫描/形状校验：拒绝时**零落盘**（回执也不写）。

        空内容由各入口按自己的措辞先拒；这里只做 delimiter 与 threat 扫描。
        delimiter 拒绝是硬约束：含保留分隔符的正文一旦落盘就会分裂成多条，而 pin /
        digest / 去重全按正文逐字相等判定，转义会让磁盘文本 ≠ 记忆文本。
        """
        delimiter_error = MemoryCommit._delimiter_error(content)
        if delimiter_error is not None:
            return error_response(prefix + delimiter_error)
        scan_error = first_threat_message(content, scope="strict")
        if scan_error is not None:
            return error_response(prefix + scan_error)
        return None

    @staticmethod
    def _delimiter_error(content: str) -> str | None:
        """含保留分隔符的 content 一旦落盘就会分裂成多条——**拒绝，不转义**。

        判定按**读侧口径**、且覆盖两个同形变体：

        - 写侧 `newline=""` 原样保留 `\\r`，读侧 `read_text` 会把 CRLF/CR 折成 LF ⇒
          `\\r\\n§\\r\\n` 落盘后在重载时就是分隔符，必须先折行再判；
        - 结尾的半个分隔符 `\\n§`（`normalize_entry` 已 strip，故不会带尾随空白）
          与下一条 `join` 时凑出 `\\n§\\n`，把**下一条**并进本条——同样要拒。
        """
        folded = content.replace("\r\n", "\n").replace("\r", "\n")
        if ENTRY_DELIMITER in folded or folded.endswith(ENTRY_DELIMITER[:-1]):
            return (
                f"Blocked: content contains the reserved entry delimiter {ENTRY_DELIMITER!r}; "
                "split it into multiple add operations."
            )
        return None


def _result_for_replay(response: dict[str, Any]) -> dict[str, Any]:
    """回执里记录的可回放结果：成功响应的全部字段（去掉易变的 operation_id/replayed）。"""
    return {
        key: value for key, value in response.items()
        if key not in {"operation_id", "replayed"}
    }


__all__ = [
    "MemoryCommit",
    "MemoryCommitError",
    "ReconcileReport",
    "new_operation_id",
]
