"""JsonlSessionStore：SessionEvent 的薄 IO 层（JSONL append-only）。

只负责两件事：
    1. read_events(session_id) — 读取一个 Session 的全部有效事件
    2. append_event(session_id, event) — 向一个 Session 追加一条事件

不持有业务状态、不做 seq 分配（那是 Session 聚合根的职责）。
"""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_harness.session.event import USER_MESSAGE, SessionEvent

logger = logging.getLogger("agent_harness.session.store")

#: read_session_summary 头部扫描的解析上限：first_user_message 几乎总在
#: 会话最初几条事件里；超过此数仍未找到则放弃（返回 None，前端有
#: events 扫描降级路径）。只约束"解析几条"，不约束行计数（O(1)/行）。
_SUMMARY_HEAD_PARSE_LIMIT = 200


@dataclass(frozen=True)
class SessionSummaryStats:
    """read_session_summary 的产出：列表页所需的最小字段集。"""

    event_count: int
    first_event_time: str | None
    last_event_time: str | None
    first_user_message: str | None


class JsonlSessionStore:
    """JSONL append-only 事件存储。

    文件布局：``<root>/<session_id>/events.jsonl``
    每行一条 JSON 事件，整行写入后立即 flush（崩溃安全：半行 = 没发生）。
    """

    def __init__(self, root: str | Path = ".agent/sessions") -> None:
        self._root = Path(root)

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _events_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "events.jsonl"

    def append_event(self, session_id: str, event: SessionEvent) -> None:
        """向 Session 的 JSONL 追加一条事件（整行 + flush + fsync）。

        fsync 是断电不丢的底线（用户拍板的耐久性决策）：flush 只把进程缓冲
        推到 OS page cache，断电即失；fsync 才真正落盘。代价是每次 append
        一次磁盘同步——事件流是恢复的唯一真相源，宁慢不丢。
        """
        path = self._events_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @staticmethod
    def _parse_event_line(raw_line: str, path_name: str, lineno: int) -> SessionEvent | None:
        """单行解析（容错语义的单一 owner，read_events / summary 共用）。

        容错范围：JSON 语法损坏（半行）、合法 JSON 但非事件字典、非法 UTF-8、
        seq 缺失或类型非法——一行坏数据只损失该行，不得 brick 恢复。
        """
        stripped = raw_line.strip()
        if not stripped:
            return None
        try:
            parsed: Any = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning("跳过损坏行 %s:%d（半行或写入中断）", path_name, lineno)
            return None
        if not isinstance(parsed, dict):
            logger.warning("跳过损坏行 %s:%d（合法 JSON 但非事件字典）", path_name, lineno)
            return None
        seq = parsed.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            logger.warning("跳过损坏行 %s:%d（seq 缺失、类型非法或为负）", path_name, lineno)
            return None
        try:
            return SessionEvent.from_dict(parsed)
        except Exception:  # 单行损坏只损失该行（容错兜底）
            logger.warning(
                "跳过损坏行 %s:%d（事件字段解析失败）", path_name, lineno, exc_info=True,
            )
            return None

    def read_events(self, session_id: str) -> list[SessionEvent]:
        """读取 Session 的全部有效事件，跳过无法解析的损坏行。

        容错范围见 _parse_event_line——一行坏数据只损失该行，不得 brick
        整个 session 的恢复。
        """
        path = self._events_path(session_id)
        if not path.exists():
            return []

        events: list[SessionEvent] = []
        # errors="replace"：非法 UTF-8 字节替换为 U+FFFD，让坏行走统一的跳过路径
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                event = self._parse_event_line(raw_line, path.name, lineno)
                if event is not None:
                    events.append(event)
        return events

    def read_session_summary(self, session_id: str) -> SessionSummaryStats | None:
        """列表页快路径：单趟流式扫描，只解析头部 + 末行。

        GET /api/sessions 曾对每个会话做全量 JSON 解析（30 会话 × 2000 事件
        ≈ 秒级串行阻塞），而列表页只需要：首条 user 消息（头部早退）、首末
        事件时间（首行 + 末行）、事件数（行计数）。本方法把解析量从 O(全部
        事件) 压到 O(头部上限 + 1)。

        精确性契约：扫描路径上发现任何损坏行 → 整体回退 read_events 全量
        解析（列表语义与全量严格一致，只是慢）。未扫描到的中段损坏行会让
        event_count 偏大——该情形只可能来自手工编辑/磁盘异常（正常崩溃损坏
        集中在末行，已覆盖），属显示级字段的已知取舍；resume 恢复仍走
        read_events 全量容错，不受影响。
        """
        path = self._events_path(session_id)
        if not path.exists():
            return None

        event_count = 0
        first_time: str | None = None
        first_user_message: str | None = None
        head_done = False
        head_parsed = 0
        # 末两行 (lineno, stripped)：末行损坏时回退取前一行；lineno 留给
        # 损坏告警——探针日志必须可定位，不用哨兵值伪造位置。
        tail: deque[tuple[int, str]] = deque(maxlen=2)

        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                event_count += 1
                tail.append((lineno, stripped))

                if not head_done:
                    event = self._parse_event_line(raw_line, path.name, lineno)
                    if event is None:
                        # 头部存在损坏行：中段完整性不可信 → 全量回退
                        return self._summary_fallback(session_id)
                    if first_time is None:
                        first_time = event.time
                    if (event.type == USER_MESSAGE
                            and isinstance(event.data.get("content"), str)
                            and event.data["content"].strip()):
                        first_user_message = event.data["content"].strip()[:128]
                        head_done = True
                    else:
                        head_parsed += 1
                        if head_parsed >= _SUMMARY_HEAD_PARSE_LIMIT:
                            head_done = True

        # 末行损坏（崩溃半写的常态位置）→ 全量回退，保证精确
        if tail and self._parse_event_line(tail[-1][1], path.name, tail[-1][0]) is None:
            return self._summary_fallback(session_id)
        last_time = self._last_event_time_from_tail(tail)
        if tail and last_time is None:
            return self._summary_fallback(session_id)

        return SessionSummaryStats(
            event_count=event_count,
            first_event_time=first_time,
            last_event_time=last_time,
            first_user_message=first_user_message,
        )

    def _last_event_time_from_tail(
        self, tail: deque[tuple[int, str]],
    ) -> str | None:
        """从末两行取最后一个可解析事件的时间（末行损坏时用前一行）。"""
        for lineno, stripped in reversed(tail):
            event = self._parse_event_line(stripped, "events.jsonl", lineno)
            if event is not None:
                return event.time
        return None

    def _summary_fallback(self, session_id: str) -> SessionSummaryStats | None:
        """扫描路径发现损坏 → 全量解析，产出与旧实现严格一致的摘要。"""
        events = self.read_events(session_id)
        if not events:
            return SessionSummaryStats(
                event_count=0, first_event_time=None,
                last_event_time=None, first_user_message=None,
            )
        first_user_message = next(
            (e.data.get("content") for e in events
             if e.type == USER_MESSAGE and isinstance(e.data.get("content"), str)
             and e.data["content"].strip()),
            None,
        )
        if first_user_message is not None:
            first_user_message = first_user_message.strip()[:128]
        return SessionSummaryStats(
            event_count=len(events),
            first_event_time=events[0].time,
            last_event_time=events[-1].time,
            first_user_message=first_user_message,
        )

    def list_session_ids(self) -> list[str]:
        """列出 root 下所有有 events.jsonl 的 session_id，按最近修改倒序。

        Phase 9 / Web UI 用：GET /sessions 的基础。空 root 返回空列表。
        """
        if not self._root.exists():
            return []
        ids: list[tuple[str, float]] = []
        for entry in self._root.iterdir():
            if not entry.is_dir():
                continue
            events_path = entry / "events.jsonl"
            if not events_path.exists():
                continue
            try:
                mtime = events_path.stat().st_mtime
            except OSError:
                continue
            ids.append((entry.name, mtime))
        # 按修改时间倒序（最近在前）
        ids.sort(key=lambda x: x[1], reverse=True)
        return [sid for sid, _ in ids]
