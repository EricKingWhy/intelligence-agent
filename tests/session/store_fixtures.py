"""Session store 故障注入夹具（#251）：写盘失败的两个形状。

放共享模块而不是各测试文件里各写一份：`test_write_behavior_golden` 与
`test_fork` 都要用"写盘中途失败"这一形状，两份实现会漂移（例：`fail_from`
的计数语义改了一处忘了另一处）。
"""

from __future__ import annotations

from agent_harness.session import JsonlSessionStore, SessionEvent
from agent_harness.session.store import SeqConflict


class RejectingStore(JsonlSessionStore):
    """`append_event` 一律拒写：模拟 seq 冲突 / 存储故障这一类失败。"""

    def __init__(self, root, error_type: type[Exception] = SeqConflict) -> None:
        super().__init__(root=root)
        self._error_type = error_type

    def append_event(self, session_id: str, event: SessionEvent) -> None:
        raise self._error_type("拒写（注入）")


class FailingFromStore(JsonlSessionStore):
    """第 `fail_from` 次 `append_event` 起拒写：注入"写盘中途失败"。

    计数是**本实例自己的**调用次数（从 1 起算），不是全局序号。
    """

    def __init__(self, root, *, fail_from: int) -> None:
        super().__init__(root=root)
        self._calls = 0
        self._fail_from = fail_from

    def append_event(self, session_id: str, event: SessionEvent) -> None:
        self._calls += 1
        if self._calls >= self._fail_from:
            raise RuntimeError(f"磁盘故障（注入，第 {self._calls} 次写）")
        super().append_event(session_id, event)
