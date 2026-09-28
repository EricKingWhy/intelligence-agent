"""跨测试文件的**工作目录绑定**夹具（#266）。

会话归属有两个事实源：`session/started.cwd`（会话侧）与 WorkspaceRegistry 的映射文件
（注册表侧）。要让两侧漂移，测试就得"把映射改指别处"——这个动作在
`tests/session/` 与 `tests/web/` 里各写了一遍（T01 落地时实测 3 份，批量审查 P3）。
夹具放这里而不是某个 conftest：三个用例分属两个包，共同祖先只有 `tests/` 根
（`tests/scripted_model.py` 同款先例）。
"""

from __future__ import annotations

import json
from pathlib import Path


def rewrite_workspace_mapping(
    workspaces_root: Path, session_id: str, root: str | Path,
) -> None:
    """把 `<workspaces_root>/<session_id>.json` 的 `workspace_root` 改指 `root`。

    模拟映射漂移 / 手工损坏（对账的另一侧事实），只动这一个键，其余字段逐字保留——
    映射文件是注册表的持久事实，测试伪造它时必须与真实写出的形状同构。
    """
    path = workspaces_root / f"{session_id}.json"
    mapping = json.loads(path.read_text(encoding="utf-8"))
    mapping["workspace_root"] = str(root)
    path.write_text(json.dumps(mapping), encoding="utf-8")
