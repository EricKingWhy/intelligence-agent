"""Web lineage / replay API（Phase 14, #113/#1322, ADR-0017 决策 4/6/10）。

只读 lineage 查询面 + （T7 #137 起）fork 创建面 + （#1322）逻辑回放面。
**独立 router 文件**：本阶段 app.py 正被流式改造（ADR-0016）重刀，注册只经
create_app 里的一行调用接入——把冲突面压到最小。fork 创建原为 CLI-only
（ADR 决策 6），T7 #137 按 PRD §2.4 重新启用 Web 端点，实现仍复用
`session/fork.py`。replay 同理（ADR 决策 4 的「逻辑回放、零副作用契约」）：
Web 端点只做只读投影，渲染逻辑复用 CLI 的 `render_replay_event`，不复制一份。

形状（前端消费契约）：
{
  "session_id": ...,
  "ancestors": [{"session_id", "origin", "fork_point_seq"} ...],  # 顶层祖先 → 直接父
  "children":  [{"session_id", "origin", "fork_point_seq", "created_at"} ...],
  "edges":     [{"from", "to", "origin", "fork_point_seq"} ...],  # 收集到的全部边
}

GET /api/sessions/{id}/replay →
{
  "session_id": ...,
  "count": <可渲染事件数>,
  "steps": [{"seq", "type", "line"} ...],  # 逐事件重建行（生命周期噪音不进 steps）
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
    EventLogCorruptError,
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

    @app.get("/api/sessions/{session_id}/replay")
    async def replay_session(session_id: str) -> dict:
        """逻辑回放（`#1322`；spec 03 §6 / ADR-0017 决策 4）：只读重建会话历史。

        **零副作用契约**（与 CLI `replay_command` 同一份语义，本端点不复制回放
        逻辑）：只经 `SessionService.get_events` 只读加载——不走 `Session.resume`
        （那会追加 `session/resumed`）、不构造 runtime（无模型访问）、不触碰
        workspace、不写任何事件；tool result 一律渲染**冻结终态**。

        渲染复用 CLI 的 `render_replay_event`（纯函数）：回放逻辑只有那一处，
        Web 只把它包成 `{seq, type, line}` 步骤给前端做逐事件推进。生命周期噪音
        （`session/started` 等）由它返回 `None` 过滤掉，不进 `steps`。

        语义：200 → `{session_id, count, steps}`（**幂等**，重复调用结果逐字相同）；
        404 → 没有这个会话；409 → 事件日志损坏（`EventLogCorruptError`，需走恢复
        入口，与 `/events` 同一口径）；422 → id 形态非法（先于 404，路径穿越防线）。

        动词/传输选择：本端点**只读**，故用 GET（与 `/events`、`/budget`、`/lineage`
        同一族），不套来源闸（`require_trusted_origin` 是宿主侧**写**操作的闸门）；
        「二次确认」是前端入口层的表达（把零副作用语义讲给用户），不是服务端契约。
        """
        service = session_service(app.state.agent)
        try:
            events = await service.get_events(session_id)
        except (InvalidSessionId, SessionNotFound, EventLogCorruptError) as e:
            raise http_error(e) from e
        # 复用 CLI 回放投影（`render_replay_event`）：不重写第二份回放逻辑。
        # 延迟导入：web 层不把 cli 拉进模块加载期（cli 侧对 web 亦为延迟导入）。
        from agent_harness.cli import render_replay_event

        steps: list[dict] = []
        for event in events:
            line = render_replay_event(event)
            if line:
                steps.append({"seq": event.seq, "type": event.type, "line": line})
        return {"session_id": session_id, "count": len(steps), "steps": steps}


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
