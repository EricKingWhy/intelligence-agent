"""WorkspaceRegistry 的测试替身（`#1278` fork 继承 sandbox 后端）。

`Session.start` 把选定的后端交给 `registry.create(session_id, backend=...)`。
用例要区分「继承到了父的显式选择」与「没传、走部署默认」，所以替身**记录**
每次 create 的 backend，并让缺省（None）与 `"local"` 在断言里可区分——Windows
上「父选 local、child 落到部署默认 docker」正是 None 的形状。

`exists()` 恒 False：用例的父会话没有物理 workspace，`_copy_workspace` 早返回，
不进入拷贝分支（本节只测后端继承，不测 workspace 复制）。
"""

from __future__ import annotations


class RecordingRegistry:
    """记录 `Session.start` 传给 `registry.create` 的 backend（父无 workspace）。"""

    def __init__(self) -> None:
        self.created: list[tuple[str, str | None]] = []

    def create(self, session_id: str, backend: str | None = None, **_kwargs):
        self.created.append((session_id, backend))

    def exists(self, session_id: str) -> bool:
        return False
