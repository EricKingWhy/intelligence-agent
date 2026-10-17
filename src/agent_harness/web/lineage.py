"""Web lineage API（Phase 14 T7, #113, ADR-0017 决策 6/10）。

只读 lineage 查询面 + （T7 #137 起）fork 创建面。**独立 router 文件**：
本阶段 app.py 正被流式改造（ADR-0016）重刀，注册只经 create_app 里的一行
调用接入——把冲突面压到最小。fork 创建原为 CLI-only（ADR 决策 6），
T7 #137 按 PRD §2.4 重新启用 Web 端点，实现仍复用 `session/fork.py`。

形状（前端消费契约）：
{
  "session_id": ...,
  "ancestors": [{"session_id", "origin", "fork_point_seq"} ...],  # 顶层祖先 → 直接父
  "children":  [{"session_id", "origin", "fork_point_seq", "created_at"} ...],
  "edges":     [{"from", "to", "origin", "fork_point_seq"} ...],  # 收集到的全部边
}
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import Field

from agent_harness.sandbox.capabilities import SandboxUnavailableError
from agent_harness.session.lineage import (
    LineageNode,
    build_lineage_index,
    build_lineage_tree,
)
from agent_harness.session.service import (
    ActiveRunConflict,
    InvalidForkBoundary,
    InvalidSessionId,
    SessionNotFound,
)
from agent_harness.storage.sqlite import StorageBusyError
from agent_harness.web.app import _AmendValueValidators, session_service
from agent_harness.web.domain_errors import (
    http_error,
    sandbox_unavailable_http_error,
    storage_http_error,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


class ForkRequest(_AmendValueValidators):
    """POST /api/sessions/{id}/forks 的请求体（T7 #137，PRD §2.4）。

    from_seq 是父会话中用户消息的 seq（fork 锚点，锚点消息不进 child）。

    sandbox_backend（`#1278`）：None = 继承父会话显式选择（父没选 ⇒ 部署默认）；
    显式值优先。取值校验复用 `_AmendValueValidators._validate_sandbox_backend`
    ——与 create / resume / send 同一份判据，未知后端名在请求层 422。
    """

    from_seq: int = Field(ge=0)
    sandbox_backend: str | None = None


def register_lineage_routes(
    app: FastAPI, *, validate_session_id: Callable[[str], str]
) -> None:
    """把 lineage 路由挂到既有 app（app.py 侧一行调用的接入面）。"""

    @app.get("/api/sessions/{session_id}/lineage")
    async def get_session_lineage(session_id: str) -> dict:
        # #517 BUG-07：/events、/budget 对同一非法 id 都给 422，这里曾漏映射——
        # `InvalidSessionId` 逃逸成 500（同类输入三种结果）。与其余端点对齐。
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        state = app.state.agent
        await state.ensure_stores()
        metas = await build_lineage_index(state.store, state.session_meta_store)
        by_id = {m.session_id: m for m in metas}
        if session_id not in by_id:
            raise HTTPException(
                status_code=404, detail=f"session '{session_id}' not found"
            )
        roots = build_lineage_tree(metas)

        # 定位目标节点：树上找，孤儿/缺口节点也在 roots 里
        target = _find(roots, session_id)
        assert target is not None  # by_id 已含 session_id，树必含节点

        # 祖先链（顶层 → 直接父）+ 后代（直接子件）；edges 覆盖收集到的全部边
        ancestors: list[dict] = []
        node = target
        while node.parent_session_id is not None:
            parent = _find(roots, node.parent_session_id)
            if parent is None:
                break  # orphan：链在此截断（provenance 仍在本节点字段里）
            ancestors.insert(0, {
                "session_id": parent.session_id,
                "origin": node.origin,
                "fork_point_seq": node.fork_point_seq,
            })
            node = parent
        descendants: list[dict] = []
        edges: list[dict] = []

        def _collect(n: LineageNode) -> None:
            for child in n.children:
                descendants.append({
                    "session_id": child.session_id,
                    "origin": child.origin,
                    "fork_point_seq": child.fork_point_seq,
                    "created_at": child.created_at,
                })
                edges.append({
                    "from": n.session_id, "to": child.session_id,
                    "origin": child.origin,
                    "fork_point_seq": child.fork_point_seq,
                })
                _collect(child)

        _collect(target)
        return {
            "session_id": session_id,
            "ancestors": ancestors,
            "children": descendants,
            "edges": edges,
        }

    @app.post("/api/sessions/{session_id}/forks")
    async def fork_session(session_id: str, req: ForkRequest) -> dict:
        """从历史用户消息 seq 派生 child session（T7 #137，PRD §2.4）。

        404 = session 不存在；409 = 在途 run（历史未 settled）或 sandbox 后端不可用；
        422 = from_seq 非法（负值/非锚点）或 sandbox_backend 未知。

        `sandbox_backend`（`#1278`）：缺省 / `null` = 继承父会话的显式选择
        （父没选 ⇒ 部署默认）。本字段没有"强制走部署默认"的表达方式——已知
        表达力边界（父选了 docker 而调用方想要部署默认时无解）。
        """
        service = session_service(app.state.agent)
        try:
            child_id = await service.fork(
                session_id=session_id, from_seq=req.from_seq,
                # `#1278`：None = 继承父的显式选择；显式值优先。
                sandbox_backend=req.sandbox_backend,
            )
        except (
            InvalidSessionId,
            SessionNotFound,
            ActiveRunConflict,
            InvalidForkBoundary,
        ) as e:
            raise http_error(e) from e
        except SandboxUnavailableError as e:
            # `#1278`：显式传入或继承来的 docker 在本机不可用 → 与 create 同一份
            # 结构化 409（child 未创建，客户端可换后端重试）。
            raise sandbox_unavailable_http_error(e) from e
        except StorageBusyError as e:
            # #515：fork 要给 child 补 session_meta 行（共享 harness.db 写），锁竞争
            # 重试耗尽报 503——child 未创建，客户端稍后重试即可。
            raise storage_http_error(e) from e
        return {"session_id": child_id, "from_seq": req.from_seq}


def _find(roots: list[LineageNode], session_id: str) -> LineageNode | None:
    for root in roots:
        found = _find_in(root, session_id)
        if found is not None:
            return found
    return None


def _find_in(node: LineageNode, session_id: str) -> LineageNode | None:
    if node.session_id == session_id:
        return node
    for child in node.children:
        found = _find_in(child, session_id)
        if found is not None:
            return found
    return None
