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
from pathlib import Path

from agent_harness.session.event import SessionEvent

logger = logging.getLogger("agent_harness.session.store")


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

    def read_events(self, session_id: str) -> list[SessionEvent]:
        """读取 Session 的全部有效事件，跳过无法解析的损坏行。

        容错范围：JSON 语法损坏（半行）、合法 JSON 但非事件字典（null / [1]）、
        非法 UTF-8 字节、seq 缺失或类型非法——一行坏数据只损失该行，不得 brick
        整个 session 的恢复。
        """
        path = self._events_path(session_id)
        if not path.exists():
            return []

        events: list[SessionEvent] = []
        # errors="replace"：非法 UTF-8 字节替换为 U+FFFD，让坏行走统一的跳过路径
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.warning(
                        "跳过损坏行 %s:%d（半行或写入中断）", path.name, lineno
                    )
                    continue
                # 合法 JSON 但不是事件字典（如 null / [1]）——跳过
                if not isinstance(parsed, dict):
                    logger.warning(
                        "跳过损坏行 %s:%d（合法 JSON 但非事件字典）", path.name, lineno
                    )
                    continue
                # seq 缺失、类型非法（如 "x"）或为负——跳过，避免污染 seq 计数器
                seq = parsed.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                    logger.warning(
                        "跳过损坏行 %s:%d（seq 缺失、类型非法或为负）", path.name, lineno
                    )
                    continue
                try:
                    events.append(SessionEvent.from_dict(parsed))
                except Exception:  # 单行损坏只损失该行（容错兜底）
                    logger.warning(
                        "跳过损坏行 %s:%d（事件字段解析失败）",
                        path.name,
                        lineno,
                        exc_info=True,
                    )
        return events

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
