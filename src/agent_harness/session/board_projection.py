"""#1303：看板后端投影（§3.2）。

设计边界（票面「板是投影，不是数据库」）：本模块**不新增任何事实源**。
``BoardProjection`` 是 ``SessionEvent`` 的纯投影；``BoardProjectionStore`` 的内存
dict 与 ``board_state.json`` sidecar **都只是缓存**——删掉内存与 sidecar 后，
``get()`` 从事件流重放必须得到逐字段相同的结果。

先例来源（抄优先，逐项注明）：

* 产品四态 / 跃迁时刻：``session/task.py::derive_task_state`` 是**唯一 owner**，
  本模块只消费，不复制第二份语义。
* 三级读取（内存 → sidecar → 事件重放）：本仓 ``store.py::read_session_summary``
  的「文件戳缓存 + 戳变化回读」纪律（#516）；sidecar 校验用事件文件的
  ``(size, mtime_ns)`` 戳，**不**走 ``_last_seq_on_disk`` 的 ``read_events`` 回退。
  该等价关系成立的前提是**同一时刻只有一个写者**（本仓 ``append_event`` 的跨进程
  文件锁就是这个前提）。旁路写者若插在「``append_event`` 返回」与「取戳」之间，
  戳会覆盖一条本投影没读到的事件；为此每次落盘前还做一次 O(1) 对账
  （``_view_matches_disk``：内存视图的 seq 连续 + 等于盘上最后一行的 seq），
  不成立就丢弃缓存、不写 sidecar，让下一次 ``get()`` 从盘上重放。读路径同理再
  校验 ``projection_seq`` 等于盘上最后一行的 ``seq``（``_disk_tail_seq``，O(1)
  尾部读），戳只能证明文件没变，证明不了 sidecar 写的是最新水位（#1310）。
* 原子写：照搬 ``session/progress.py::_write_meta_atomic`` 的
  ``mkstemp + flush + fsync + os.replace`` 形状（本票唯一指定的抄写来源）。
* best-effort 钩子：照搬 ``session/session.py`` 的 listener 纪律——观察者异常只
  ``logger.exception``，绝不破坏 append 契约。
* ack 比较形状：Orca ``agent-attention-acknowledgement.ts:26-48``（@de1f6e3d）
  按「turn / 状态起始时刻」比较，不按 ``updatedAt``；对应到本仓即
  ``product_state_changed_at``（只在状态跃迁时更新）。

已知上限（ponytail）：每次触发集事件是一次 O(事件数) 重放；触发点只在 run 边界
与用户动作上，单 run 约 4–8 次。升级路径是在 ``derive_task_state`` 上做增量
fold，**不是**另写一份投影语义。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from agent_harness.session.event import (
    RUN_TERMINAL_TYPES,
    TOOL_APPROVAL_REQUESTED,
    SessionEvent,
)
from agent_harness.session.store import JsonlSessionStore

logger = logging.getLogger("agent_harness.session.board_projection")

AttentionKind = Literal["input", "approval"]

#: 尾部对账的块大小与上限：只读最后一行，按 8KB 从文件尾反向收缩；
#: 超过 4MB 仍找不到行首（病态单行）就返回 None —— 本轮不落盘，正确性优先。
_TAIL_CHUNK_BYTES = 8 * 1024
_TAIL_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class BoardProjection:
    """看板一行的完整投影快照（纯派生值；可随时从事件流重建）。"""

    product_state: str
    product_state_changed_at: str | None
    attention_kind: AttentionKind | None
    attention_summary: str | None
    #: 本投影覆盖到的最后一个事件 seq；空会话为 -1。
    projection_seq: int


def _attention(events: list[SessionEvent]) -> tuple[AttentionKind | None, str | None]:
    """注意力候选 A/B 的求值；调用方保证 ``product_state == "executing"``。

    候选 A：最新 run 上未答复的 ``ask_user``（必须带 ``kind`` 过滤——
    ``protected_fact_conflict`` 的载荷没有 ``questions`` 键）。
    候选 B：全会话口径的最后一条未决审批（``approval/requested`` 与
    ``permission/resolved`` 载荷都不带 ``run_id``，无法限定 run）。
    两者都在时取 ``seq`` 更大者。
    """
    # 延迟导入：session 包初始化期间本模块先于 tooling/agent 装配；模块级导入会
    # 触发 `agent_harness.agent.__init__ -> tooling -> session` 的循环。
    from agent_harness.agent.run_budget import latest_run_id
    from agent_harness.session.approval import unresolved_approval_ids
    from agent_harness.session.ask_user_timeout import latest_pending_input_request

    run_id = latest_run_id(events)
    if run_id is None:
        return None, None
    if any(
        event.run_id == run_id and event.type in RUN_TERMINAL_TYPES
        for event in events
    ):
        return None, None

    candidates: list[tuple[int, AttentionKind, str]] = []

    request = latest_pending_input_request(events, kind="ask_user")
    if request is not None:
        questions = request.data.get("questions")
        if isinstance(questions, list) and questions and isinstance(questions[0], dict):
            header = questions[0].get("header")
            if isinstance(header, str):
                candidates.append((request.seq, "input", f"待回答：{header}"))

    unresolved = set(unresolved_approval_ids(events))
    if unresolved:
        last_request: SessionEvent | None = None
        for event in events:
            if (
                event.type == TOOL_APPROVAL_REQUESTED
                and event.data.get("approval_id") in unresolved
            ):
                last_request = event
        if last_request is not None:
            title = last_request.data.get("title")
            if isinstance(title, str):
                candidates.append((last_request.seq, "approval", f"待批准：{title}"))

    if not candidates:
        return None, None
    _, kind, summary = max(candidates, key=lambda item: item[0])
    return kind, summary


def _derive(events: list[SessionEvent]) -> BoardProjection | None:
    """纯派生；派生本身失败时返回 ``None``（由调用方决定是否沿用旧缓存）。

    与 ``derive_board_projection`` 分开是为了让 store 能区分「合法的空投影」与
    「派生失败」——后者绝不能落盘/进缓存，否则一次瞬时异常会把看板钉成空列，
    直到下一个触发事件才自愈（独立审查 P2）。
    """
    materialized = list(events)
    projection_seq = max((event.seq for event in materialized), default=-1)
    try:
        # 延迟导入：避免 session 包初始化期的循环导入（同 `_attention`）。
        from agent_harness.session.task import derive_task_state

        state = derive_task_state(materialized)
    except Exception:
        logger.exception(
            "看板投影派生失败（session=%s）",
            materialized[-1].session_id if materialized else None,
        )
        return None

    attention_kind: AttentionKind | None = None
    attention_summary: str | None = None
    if state.product_state == "executing":
        try:
            attention_kind, attention_summary = _attention(materialized)
        except Exception:
            logger.exception(
                "看板注意力派生失败（session=%s）；attention 置空",
                materialized[-1].session_id if materialized else None,
            )
    return BoardProjection(
        product_state=state.product_state,
        product_state_changed_at=state.product_state_changed_at,
        attention_kind=attention_kind,
        attention_summary=attention_summary,
        projection_seq=projection_seq,
    )


def _empty(events: list[SessionEvent]) -> BoardProjection:
    seq = max((event.seq for event in events), default=-1)
    return BoardProjection(
        product_state="",
        product_state_changed_at=None,
        attention_kind=None,
        attention_summary=None,
        projection_seq=seq,
    )


def derive_board_projection(events: list[SessionEvent]) -> BoardProjection:
    """纯函数：events → BoardProjection（幂等；重放 N 次结果一致）。

    异常隔离：本函数在 ``Session._persist_event`` 的 append 路径上被调用，任何
    派生异常都必须被吞掉并记日志，**不得**冒到 append 契约。失败时返回一份
    保守的空投影（product_state="" / attention=None），宁可列表短暂少一列，
    也不能让写事件失败。``BoardProjectionStore`` 走 ``_derive``，派生失败时
    不落盘/不覆盖缓存，避免把瞬时空值钉住。
    """
    projection = _derive(events)
    return projection if projection is not None else _empty(events)


#: 单例按 store root 分片：不同 root（测试 / 多实例）互不干扰。
_STORES: dict[str, BoardProjectionStore] = {}
_STORES_LOCK = threading.Lock()


def get_board_projection_store(store: JsonlSessionStore) -> BoardProjectionStore:
    """按 ``store._root`` 取进程级单例（``Session.__init__`` 不新增参数）。"""
    key = os.path.abspath(os.fspath(store._root))
    with _STORES_LOCK:
        instance = _STORES.get(key)
        if instance is None:
            instance = BoardProjectionStore(store)
            _STORES[key] = instance
        return instance


class BoardProjectionStore:
    """看板投影的三级缓存：内存 → sidecar → 事件重放。

    不变量（写进 docstring 与测试）：

    1. 内存与 sidecar **都只是缓存**。删掉两者后 ``get()`` 从事件流重放，结果
       逐字段不变；
    2. sidecar 命中**零** ``read_events``：校验只读事件文件 ``(size, mtime_ns)``
       戳（目录元数据）+ 最后一行的 ``seq``（O(1) 尾部读，不重放）；戳只证明
       文件没变，水位还必须是盘上最大 ``seq``（#1310）；
    3. 任何写失败只 ``logger.exception``，绝不影响 ``Session.append`` 契约；
    4. 所有 ``_persist_event``（含触发集外）都推进 sidecar 水位到当前 seq——
       否则 ``USER_MESSAGE`` 等非触发事件会让 sidecar 永远失配、列表路径每次
       回退到全量重放；
    5. 冷缓存（本实例还没算过该会话）一律从事件流重放，**不复用** sidecar 里
       的旧投影：sidecar 可能因写失败/旁路写者落后于事件文件，直接复用会把
       旧状态盖上当前戳（独立审查 P1）。
    """

    def __init__(self, store: JsonlSessionStore) -> None:
        self._store = store
        self._root = store._root
        # session_id -> (projection, events 文件戳)。戳用于识别「另一个写者
        # 追加过」——内存命中也不能越过外部追加。
        self._cache: dict[str, tuple[BoardProjection, tuple[int, int]]] = {}
        self._lock = threading.Lock()

    # ── 公开读取 ────────────────────────────────────────────────────────

    def get(self, session_id: str) -> BoardProjection:
        """读投影：内存 → sidecar → 重放（并回填 sidecar）。"""
        with self._lock:
            stamp = self._events_stamp(session_id)
            cached = self._cache.get(session_id)
            if cached is not None and cached[1] == stamp:
                return cached[0]

            if stamp is not None:
                sidecar = self._read_sidecar(session_id, stamp)
                if sidecar is not None:
                    self._cache[session_id] = (sidecar, stamp)
                    return sidecar

        # 重放不持锁：冷启动的整段重放是 O(事件数)，持锁会串行阻塞 append 钩子
        # （独立审查 P3）。两次并发重放得到同一结果，回填以最后落锁者为准。
        events = self._store.read_events(session_id)
        projection = _derive(events)
        if projection is None:
            # 读路径同样不能留下旧缓存：下一次非触发 append 会用缓存重算并推进
            # 水位，把这次失败时的旧状态钉住（独立审查第三轮 P3）。
            with self._lock:
                self._cache.pop(session_id, None)
            return _empty(events)
        stamp_after = self._events_stamp(session_id)
        # 只有「读事件期间文件没变」才回填缓存；否则下一轮重放，避免把
        # 少一条事件的投影钉在更新的戳上。
        if stamp_after is not None and stamp_after == stamp:
            with self._lock:
                # 落锁前再核一次戳：重放不持锁，期间 append 钩子可能已经写入
                # 更新的一份（独立审查 P3 回归）；此时绝不能用旧投影覆盖它。
                if self._events_stamp(session_id) == stamp_after:
                    self._persist(session_id, projection, stamp_after)
        return projection

    # ── 写入钩子 ────────────────────────────────────────────────────────

    def update_from_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        *,
        recompute: bool,
    ) -> None:
        """``Session._persist_event`` 的 best-effort 钩子。

        ``recompute=True``（触发集）：从事件流重算投影。
        ``recompute=False``（触发集外）：只推进水位，不重算投影；若本实例还没
        算过该会话（冷缓存），退化为一次重放以建立正确的初始投影——绝不复用
        sidecar 的旧值（它可能落后于事件文件，复用等于把旧状态盖上当前戳）。

        任何异常只记日志——本方法是 append 路径上的观察者。
        """
        if not events:
            return
        try:
            with self._lock:
                stamp = self._events_stamp(session_id)
                if stamp is None:
                    return
                current_seq = events[-1].seq
                if recompute:
                    projection = _derive(events)
                else:
                    # 触发集外：本条事件按定义不改变产品态/注意力，直接复用上一份
                    # 投影，只把水位推到当前 seq。这里**不**校验旧戳——戳在 append
                    # 之后必然已变，校验会让每次 USER_MESSAGE 都退化成全量重放。
                    cached = self._cache.get(session_id)
                    projection = (
                        replace(cached[0], projection_seq=current_seq)
                        if cached is not None
                        else _derive(events)
                    )
                if projection is None:
                    # 派生失败：保留旧 sidecar，但丢弃内存缓存，下一次读/写都从
                    # 事件流重放——否则下一条非触发事件会把旧缓存盖上当前水位
                    # （独立审查 P2/P3）。
                    self._cache.pop(session_id, None)
                    return
                # O(1) 对账：本视图的最后一笔必须就是盘上最后一笔，且 seq 连续
                # （连续 ⇒ 本视图没漏掉旁路写者插进来的事件）。不成立就不落盘、
                # 不进缓存，让 get() 从盘上重放（独立审查 P2）。
                if not self._view_matches_disk(session_id, events):
                    self._cache.pop(session_id, None)
                    return
                self._persist(session_id, projection, stamp)
        except Exception:
            logger.exception(
                "看板投影更新失败（session=%s）；append 契约不受影响", session_id,
            )

    # ── sidecar IO ──────────────────────────────────────────────────────

    def _events_stamp(self, session_id: str) -> tuple[int, int] | None:
        """事件文件 ``(size, mtime_ns)``；只做 stat，**绝不** read_events。"""
        try:
            stat = self._store.events_path(session_id).stat()
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime_ns)

    def _view_matches_disk(
        self, session_id: str, events: list[SessionEvent]
    ) -> bool:
        """本视图是否覆盖盘上最后一条事件（O(1)：一次尾部读 + 一次连续性检查）。

        两条证据缺一不可：

        1. ``events[-2].seq + 1 == events[-1].seq``：本视图没有 seq 缺口——旁路
           写者插入的事件会让内存列表出现空档；
        2. 盘上最后一行的 ``seq`` == ``events[-1].seq``：本视图之后没有旁路写入。

        任一不成立说明「内存只是我的视角、盘上还有我没读到的事实」，此时写
        sidecar 就是把旧状态盖上新戳。调用方应丢弃缓存并让 ``get()`` 重放。
        """
        if not events:
            return False
        last = events[-1].seq
        if len(events) >= 2 and events[-2].seq + 1 != last:
            return False
        return self._disk_tail_seq(session_id) == last

    def _disk_tail_seq(self, session_id: str) -> int | None:
        """事件文件最后一行的 seq（只读最后一行；解析不了返回 None）。"""
        line = self._read_last_line(self._store.events_path(session_id))
        if line is None:
            return None
        try:
            raw = json.loads(line)
        except ValueError:
            return None
        seq = raw.get("seq") if isinstance(raw, dict) else None
        return seq if isinstance(seq, int) and not isinstance(seq, bool) else None

    @staticmethod
    def _read_last_line(path: Path) -> bytes | None:
        """从文件尾部反向按块读取最后一条非空行。

        逐块收缩而不是「读固定 64KB 窗口」：单事件大 payload（task_text 上限
        100K 字符，UTF-8 可达数百 KB）不会让最后一行永远解析不到。上限
        ``_TAIL_MAX_BYTES`` 之外的病态行返回 None（调用方只是跳过落盘，正确性
        不受影响）。
        """
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                pos = fh.tell()
                buffer = b""
                while pos > 0:
                    step = min(_TAIL_CHUNK_BYTES, pos)
                    pos -= step
                    fh.seek(pos)
                    buffer = fh.read(step) + buffer
                    stripped = buffer.rstrip(b"\r\n")
                    if b"\n" in stripped:
                        return stripped.rsplit(b"\n", 1)[-1]
                    if len(buffer) > _TAIL_MAX_BYTES:
                        return None
                return buffer.strip(b"\r\n") or None
        except OSError:
            return None

    def _read_sidecar(
        self,
        session_id: str,
        stamp: tuple[int, int] | None,
    ) -> BoardProjection | None:
        """读 sidecar；只有「戳匹配 + 水位是盘上最大 seq + 内容自洽」才算命中。

        戳匹配只证明「写 sidecar 之后事件文件没变」，证明不了写 sidecar 的那一版
        代码算到了最新事件（跨版本残留）或手改过 sidecar（#1310）。因此补两条
        fail-closed 判据，任一不成立即视为未命中（回落重放并自愈）：

        1. ``projection_seq`` 非负且等于事件文件最后一行的 ``seq``
           （``_disk_tail_seq``，O(1) 尾部读，不调用 ``read_events``）；
        2. ``product_state`` 是合法枚举（``""`` 或 ``PRODUCT_STATES``），且
           非 ``executing`` 时 ``attention_*`` 必须为空（attention 只属于执行中）。
        """
        path = self._store.board_state_path(session_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        if (
            stamp is None
            or raw.get("events_size") != stamp[0]
            or raw.get("events_mtime_ns") != stamp[1]
        ):
            return None
        projection_seq = raw.get("projection_seq")
        product_state = raw.get("product_state")
        changed_at = raw.get("product_state_changed_at")
        attention_kind = raw.get("attention_kind")
        attention_summary = raw.get("attention_summary")
        if not isinstance(projection_seq, int) or isinstance(projection_seq, bool):
            return None
        # 延迟导入：同 `_derive`，避免 session 包初始化期的循环导入。
        from agent_harness.session.task import PRODUCT_STATES

        if projection_seq < 0 or projection_seq != self._disk_tail_seq(session_id):
            return None
        if not isinstance(product_state, str) or product_state not in (
            "",
            *PRODUCT_STATES,
        ):
            return None
        if changed_at is not None and not isinstance(changed_at, str):
            return None
        if attention_kind not in (None, "input", "approval"):
            return None
        if attention_summary is not None and not isinstance(attention_summary, str):
            return None
        if product_state != "executing" and (
            attention_kind is not None or attention_summary is not None
        ):
            return None
        return BoardProjection(
            product_state=product_state,
            product_state_changed_at=changed_at,
            attention_kind=attention_kind,
            attention_summary=attention_summary,
            projection_seq=projection_seq,
        )

    def _persist(
        self,
        session_id: str,
        projection: BoardProjection,
        stamp: tuple[int, int],
    ) -> None:
        try:
            self._write_sidecar(session_id, projection, stamp)
        except Exception:
            logger.exception(
                "看板 sidecar 写入失败（session=%s）；内存缓存仍可用", session_id,
            )
        self._cache[session_id] = (projection, stamp)

    def _write_sidecar(
        self,
        session_id: str,
        projection: BoardProjection,
        stamp: tuple[int, int],
    ) -> None:
        """原子写 sidecar（照抄 ``progress.py::_write_meta_atomic`` 的形状）。"""
        path = self._store.board_state_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "product_state": projection.product_state,
            "product_state_changed_at": projection.product_state_changed_at,
            "attention_kind": projection.attention_kind,
            "attention_summary": projection.attention_summary,
            "projection_seq": projection.projection_seq,
            "events_size": stamp[0],
            "events_mtime_ns": stamp[1],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with suppress(OSError):
                os.unlink(tmp_name)
            raise
