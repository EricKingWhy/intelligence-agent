"""#565 收敛 P2 加固：append 前必须中立化「无换行撕裂尾段」。

双轴审查发现（红面）：崩溃留下的无换行尾段被 #565 读侧宽容，但 `append_event`
以 text-mode 直接追加——新记录拼进尾段字节，把「宽容的预期形状」变成「完整
坏行」：事件从读投影消失 + 该会话此后所有恢复入口被永久拒绝。

成熟产品同型语义（§6.1 方案依据，四独立来源收敛）：
- Redis AOF：载入时丢弃最后一个不完整命令并记 `Truncating the AOF at offset N`，
  `aof-load-truncated yes` 默认保可用性；
- etcd WAL：`ReadAll` 写模式 seek 到最后有效记录偏移 `ZeroToEnd`（注释：torn
  write 的记录从未完整落盘，清零安全，目的即防止后续 append 产生 CRC 错）；
- SQLite WAL：恢复停在最后有效校验帧（mxFrame），坏尾被无视，新帧安全覆写；
- LevelDB：损坏→跳块，writer 永远只写完整 record。

钉住的契约：
- append 前尾段**可解析为完整事件**（只缺终止符，如写到 `\\r` 后断电）→ 补
  终止符封印（保留有效记录，Redis/SQLite 同为「截到最后有效记录」而非「截到
  上一个记录」）；
- 尾段**不可解析** → 截断到最后换行边界，脱敏指纹（offset/len/sha256）进诊断
  日志，不含内容；
- 尾段干净（空 / \\n / \\r\\n 结尾）→ 零动作零日志；
- 撕裂字节绝不留在主日志里（恢复的预期形状不能被写入者自己破坏）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_harness.session import (
    SESSION_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    SessionEvent,
)


@pytest.fixture
def store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path)


def _event(
    session_id: str, seq: int, event_type: str, data: dict | None = None
) -> SessionEvent:
    return SessionEvent(
        event_id=f"evt-{seq}",
        seq=seq,
        type=event_type,
        session_id=session_id,
        time=f"t{seq}",
        data=data or {},
    )


def _line_bytes(event: SessionEvent) -> bytes:
    """与 store.append_event 同一序列化（ensure_ascii=False + 紧凑分隔符）。"""
    return json.dumps(
        event.to_dict(), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _seed(store: JsonlSessionStore, session_id: str, n: int) -> Path:
    for seq in range(n):
        event_type = SESSION_STARTED if seq == 0 else USER_MESSAGE
        data = None if seq == 0 else {"content": f"m{seq}"}
        store.append_event(session_id, _event(session_id, seq, event_type, data))
    return store._events_path(session_id)


def _repair_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "撕裂尾段" in r.getMessage()
    ]


def test_append_after_unparseable_torn_tail_truncates_and_appends(
    store: JsonlSessionStore,
) -> None:
    """不可解析尾段：截断到最后换行边界再 append——新行不拼接、前缀字节保真。"""
    session_id = "torn-append-truncate"
    path = _seed(store, session_id, 2)
    on_disk = path.read_bytes()
    torn = _line_bytes(_event(session_id, 2, "tool/result", {"tool_call_id": "c"}))[:30]
    with path.open("ab") as fh:
        fh.write(torn)  # 半行、无换行——崩溃撕裂形状

    store.append_event(session_id, _event(session_id, 2, "tool/result", {"ok": 1}))

    after = path.read_bytes()
    line_ending = b"\r\n" if b"\r\n" in on_disk else b"\n"
    new_line = _line_bytes(_event(session_id, 2, "tool/result", {"ok": 1})) + line_ending
    # 逐字节等价：前缀保真 + 撕裂字节消失 + 新行恰好接在边界后（不拼接不缺字）
    assert after == on_disk + new_line
    events, integrity = store.read_events_report(session_id)
    assert [e.seq for e in events] == [0, 1, 2]
    assert integrity.healthy, f"修复后必须 healthy: {integrity}"


@pytest.mark.parametrize(
    "torn_shape",
    ["writer-crlf", "posix-bare"],
    ids=["写到终止符前一字节断", "写到行尾字节断"],
)
def test_append_after_parseable_torn_tail_seals_it(
    store: JsonlSessionStore, torn_shape: str
) -> None:
    """可解析尾段（完整事件缺终止符）：封印保留而非丢弃——有效记录是数据。"""
    session_id = "torn-append-seal"
    path = _seed(store, session_id, 2)
    on_disk = path.read_bytes()
    line_ending = b"\r\n" if b"\r\n" in on_disk else b"\n"
    sealed = _event(session_id, 2, "tool/result", {"tool_call_id": "c2"})
    torn = _line_bytes(sealed)
    if torn_shape == "writer-crlf":
        torn += line_ending[:-1]  # 真实写者形状：写到 \r 断（差最后一个 \n）

    with path.open("ab") as fh:
        fh.write(torn)

    store.append_event(session_id, _event(session_id, 3, USER_MESSAGE, {"content": "m3"}))

    events, integrity = store.read_events_report(session_id)
    assert [e.seq for e in events] == [0, 1, 2, 3], "封印的事件不得被截掉"
    assert integrity.healthy
    after = path.read_bytes()
    assert after.startswith(on_disk + torn)
    assert after[len(on_disk) + len(torn):].startswith(b"\n"), "封印必须补终止符"


def test_clean_append_has_no_repair_warning(
    store: JsonlSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    """尾段干净（\\n 结尾）→ 零修复动作零修复日志。"""
    session_id = "clean-append"
    _seed(store, session_id, 1)
    with caplog.at_level(logging.DEBUG):
        store.append_event(
            session_id, _event(session_id, 1, USER_MESSAGE, {"content": "m1"})
        )
    assert _repair_warnings(caplog) == []


def test_torn_only_file_repairs_to_boundary_and_appends(
    store: JsonlSessionStore,
) -> None:
    """整文件只有撕裂行（session/started 写一半崩了）：截到空边界后正常 append。"""
    session_id = "torn-only"
    path = store._events_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    torn = _line_bytes(_event(session_id, 0, SESSION_STARTED))[:25]
    with path.open("wb") as fh:
        fh.write(torn)

    store.append_event(session_id, _event(session_id, 0, SESSION_STARTED))

    events, integrity = store.read_events_report(session_id)
    assert [e.seq for e in events] == [0]
    assert integrity.healthy
    after = path.read_bytes()
    assert after == _line_bytes(_event(session_id, 0, SESSION_STARTED)) + b"\r\n" or (
        after == _line_bytes(_event(session_id, 0, SESSION_STARTED)) + b"\n"
    ), f"文件必须恰为新事件一行: {after!r}"


def test_concurrent_appends_with_torn_tail_stay_wellformed(
    store: JsonlSessionStore,
) -> None:
    """并发 append 撞上撕裂尾段：修复只在锁内做一次，全部事件完整可读。"""
    session_id = "torn-concurrent"
    path = _seed(store, session_id, 2)
    torn = b'{"seq": 2, "type": "tool/resul'
    with path.open("ab") as fh:
        fh.write(torn)

    def append(seq: int) -> None:
        store.append_event(
            session_id,
            _event(session_id, seq, "tool/result", {"tool_call_id": f"c{seq}"}),
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(2, 6)))

    events, integrity = store.read_events_report(session_id)
    assert [e.seq for e in events] == [0, 1, 2, 3, 4, 5]
    assert integrity.healthy, f"并发修复后必须 healthy: {integrity}"
    assert torn not in path.read_bytes()


def test_truncate_fingerprint_is_sanitized(
    store: JsonlSessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    """截断指纹只含 offset/len/sha256——撕裂内容本身不进日志。"""
    session_id = "torn-fingerprint"
    path = _seed(store, session_id, 2)
    secret = b'{"secret_payload":"TOPSECRET-VALUE"'
    with path.open("ab") as fh:
        fh.write(secret)

    with caplog.at_level(logging.WARNING):
        store.append_event(session_id, _event(session_id, 2, "tool/result", {"ok": 1}))

    joined = "\n".join(_repair_warnings(caplog))
    assert hashlib.sha256(secret).hexdigest() in joined
    assert "TOPSECRET-VALUE" not in joined
