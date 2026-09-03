"""JsonlSessionStore：SessionEvent 的薄 IO 层（JSONL append-only）。

只负责两件事：
    1. read_events(session_id) — 读取一个 Session 的全部有效事件
    2. append_event(session_id, event) — 向一个 Session 追加一条事件

不持有业务状态、不做 seq 分配（那是 Session 聚合根的职责）。
"""

from __future__ import annotations

import json
import logging
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
        """向 Session 的 JSONL 追加一条事件（整行 + flush）。"""
        path = self._events_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def read_events(self, session_id: str) -> list[SessionEvent]:
        """读取 Session 的全部有效事件，跳过无法解析的损坏行。"""
        path = self._events_path(session_id)
        if not path.exists():
            return []

        events: list[SessionEvent] = []
        with path.open("r", encoding="utf-8") as fh:
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
                events.append(SessionEvent.from_dict(parsed))
        return events
