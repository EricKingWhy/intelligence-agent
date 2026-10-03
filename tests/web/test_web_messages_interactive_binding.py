"""#545（F1-approvals）：/messages 与 /resume 续聊路径的交互式审批回调必须绑定 Session。

现象（票面 R4-高）：``auto_approve=false`` 且未声明 ``permission_mode`` 的会话，
凡经 ``POST /messages`` / ``POST /resume`` 发起（或续跑）的 run，模型一旦发起需
审批的工具调用，run 直接 ``run/failed``——server 日志反复出现
``RuntimeError: interactive callback invoked before Session.start``，
审批卡从未出现：human-in-the-loop 在续聊路径上完全失效。

根因：``SessionService.resume_and_launch`` 的 ``build_resume_runtime`` 里，外层
``interactive`` 只在「声明档位」分支被置 True；#423 分支（未选档位 +
``auto_approve=false``）构建了 ``InteractiveCallbackHolder`` 却漏置该标志
⇒ service 尾部 ``if interactive and isinstance(...): bind_session(session)``
被跳过（审批队列的 run 终结 GC 同样被跳过）⇒ holder 的 ``_session`` 保持 None。

回归链（2026-10-03 audit 增强）：first launch → idle messages → resume →
permission changed → pending 审批禁改档（ADR-0041 D5）→ cancel；真实 callback
决策与权限事件（tool/approval-requested ↔ permission/resolved）按 approval_id
一一配对。

跑法同 ``test_web_phase5_approval.py``：uvicorn 真服务器（不是 TestClient）——
需要在 run 等待审批时并发发 /approve、/permission、/cancel。
"""

from __future__ import annotations

import asyncio
import itertools
import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent_harness.session import JsonlSessionStore

_CALL_SEQ = itertools.count(1)


class _ContentTriggeredImpl:
    """内容触发词驱动的 fake 模型：最后一条消息是本轮新用户消息且含 "bash"
    → 出 bash tool_call（DANGER → 需审批）；最后一条是本轮的 tool result
    → 出纯文本收尾。无跨 run 状态，多 leg 回归链复用同一 monkeypatch。
    """

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        last = messages[-1]
        if getattr(last, "type", None) == "tool":
            yield AIMessageChunk(content="done")
            return
        if "bash" in str(getattr(last, "content", "")):
            yield AIMessage(content="", tool_calls=[{
                "name": "bash", "args": {"command": "echo hi"},
                "id": f"call{next(_CALL_SEQ)}", "type": "tool_call",
            }])
        else:
            yield AIMessageChunk(content="ok")


async def _start_server(tmp_path, monkeypatch):
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: _ContentTriggeredImpl(),
    )
    app = create_app(Settings(
        _env_file=None, workspace_dir=str(tmp_path),
        model_api_key="sk-test", enable_cors=False,
    ))
    uv_config = uvicorn.Config(app, host="127.0.0.1", port=0,
                               log_level="error", lifespan="on")
    server = uvicorn.Server(uv_config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, serve_task, port, app


async def _shutdown(server, serve_task):
    server.should_exit = True
    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass


def _parse(line: str) -> dict:
    return json.loads(line.removeprefix("data:").strip())


async def _launch(client, url: str, payload: dict) -> tuple[str | None, dict | None]:
    """POST 发起 run（launched 分支返回 SSE 流），读到 tool/approval-requested
    或流结束为止。返回 (session_id, approval_frame)。

    detached-run（ADR-0016）：提前断开流不会取消 run——审批等待期断开是常态。
    """
    session_id = None
    approval_frame = None
    async with client.stream("POST", url, json=payload) as response:
        assert response.status_code == 200, f"POST {url} → {response.status_code}"
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            frame = _parse(line)
            if session_id is None and frame.get("session_id"):
                session_id = frame["session_id"]
            if frame.get("type") == "tool/approval-requested":
                approval_frame = frame
                break
    return session_id, approval_frame


async def _approve(client, base: str, session_id: str, approval_id: str) -> dict:
    resp = await client.post(
        f"{base}/api/sessions/{session_id}/approve",
        json={"approval_id": approval_id, "approved": True, "reason": "test"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _wait_terminal(store, session_id: str, deadline_seconds: float = 20.0):
    """轮询事件流直到出现 run 终态（completed / failed），返回最后一条终态事件。"""
    async with asyncio.timeout(deadline_seconds):
        while True:
            events = store.read_events(session_id)
            terminals = [e for e in events if e.type in ("run/completed", "run/failed")]
            if terminals:
                return terminals[-1]
            await asyncio.sleep(0.05)


async def _wait_idle(app, session_id: str, deadline_seconds: float = 10.0):
    """等会话真正空闲（run task 收尾完成）。

    run/completed 事件落盘后 task 还有收尾窗口（finalizer 在途），此刻
    resume/messages 会被 409 拒——这是产品语义（终态后拒绝并发两轮），
    测试必须等 RunManager 视角 idle 再发起下一 leg。"""
    async with asyncio.timeout(deadline_seconds):
        while app.state.agent.run_manager.get_active(session_id) is not None:
            await asyncio.sleep(0.02)


def _assert_approval_pairs(events) -> dict[str, str]:
    """tool/approval-requested ↔ permission/resolved 必须按 approval_id 成对。

    返回 {approval_id: decision}。run 中途取消的等待由 fail-closed expire 结清
    （决策 deny），同样必须留下 resolved——否则完成闸门谓词 2 会把会话锁死。
    """
    requested: set[str] = set()
    resolved: dict[str, str] = {}
    for event in events:
        approval_id = event.data.get("approval_id")
        if not isinstance(approval_id, str):
            continue
        if event.type == "tool/approval-requested":
            assert approval_id not in requested, f"重复 requested：{approval_id}"
            requested.add(approval_id)
        elif event.type == "permission/resolved":
            resolved.setdefault(approval_id, event.data.get("decision", ""))
    assert requested <= set(resolved), (
        f"只 requested 未 resolved：{requested - set(resolved)}"
    )
    return resolved


@pytest.mark.asyncio
async def test_messages_and_resume_paths_bind_interactive_callback(
    tmp_path, monkeypatch,
):
    """核心回归（#545）：auto_approve=false 未选档位的会话，/messages 与 /resume
    发起的 run 必须出现审批卡并可在批准后完成。

    first launch（create+launch）是健康对照：创建路径回调一直正确绑定；
    漏接线只发生在续聊路径（build_resume_runtime）。
    """
    import httpx2

    server, serve_task, port, app = await _start_server(tmp_path, monkeypatch)
    store = JsonlSessionStore(root=tmp_path / "sessions")
    try:
        base = f"http://127.0.0.1:{port}"
        async with httpx2.AsyncClient(timeout=None) as client:
            # leg 1：first launch（健康对照）
            session_id, approval = await _launch(
                client, f"{base}/api/sessions",
                {"task": "跑 bash", "auto_approve": False},
            )
            assert session_id, "必须取得 session_id"
            assert approval, "first launch 必须出现 tool/approval-requested（对照腿）"
            await _approve(client, base, session_id, approval["data"]["approval_id"])
            terminal = await _wait_terminal(store, session_id)
            assert terminal.type == "run/completed", (
                f"对照腿批准后应完成，实际 {terminal.type} reason={terminal.data.get('reason')}"
            )

            await _wait_idle(app, session_id)

            # leg 2：idle messages（缺陷腿：续聊重建的 holder 必须绑定 Session）
            sid2, approval2 = await _launch(
                client, f"{base}/api/sessions/{session_id}/messages",
                {"content": "再跑一次 bash"},
            )
            assert sid2 == session_id
            assert approval2, (
                "idle messages 续跑必须出现 tool/approval-requested——"
                "missing 即 #545：回调未绑 Session，run 以 RuntimeError 失败"
            )
            await _approve(client, base, session_id, approval2["data"]["approval_id"])
            terminal = await _wait_terminal(store, session_id)
            assert terminal.type == "run/completed", (
                f"messages 腿批准后应完成，实际 {terminal.type} reason={terminal.data.get('reason')}"
            )

            await _wait_idle(app, session_id)

            # leg 3：/resume 显式续跑（同一缺陷路径）
            sid3, approval3 = await _launch(
                client, f"{base}/api/sessions/{session_id}/resume",
                {"task": "再跑一次 bash"},
            )
            assert sid3 == session_id
            assert approval3, (
                "/resume 续跑必须出现 tool/approval-requested（同一漏接线）"
            )
            await _approve(client, base, session_id, approval3["data"]["approval_id"])
            terminal = await _wait_terminal(store, session_id)
            assert terminal.type == "run/completed"

        # 真实 callback 决策与权限事件配对：三张审批卡各恰好一条 approve_once
        events = store.read_events(session_id)
        decisions = _assert_approval_pairs(events)
        assert len(decisions) == 3
        assert set(decisions.values()) == {"approve_once"}
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_interactive_lifecycle_permission_change_pending_block_and_cancel(
    tmp_path, monkeypatch,
):
    """入口回归链（audit 增强）：first launch → idle messages → resume →
    permission changed → pending 审批禁改档（ADR-0041 D5 409）→ cancel
    （取消等待 fail-closed 结清，run 终态 reason=cancelled，审批事件成对）。
    """
    import httpx2

    server, serve_task, port, app = await _start_server(tmp_path, monkeypatch)
    store = JsonlSessionStore(root=tmp_path / "sessions")
    try:
        base = f"http://127.0.0.1:{port}"
        async with httpx2.AsyncClient(timeout=None) as client:
            # leg 1：first launch（对照）→ leg 2：idle messages → leg 3：resume
            session_id, approval = await _launch(
                client, f"{base}/api/sessions",
                {"task": "跑 bash", "auto_approve": False},
            )
            assert session_id and approval
            await _approve(client, base, session_id, approval["data"]["approval_id"])
            assert (await _wait_terminal(store, session_id)).type == "run/completed"

            await _wait_idle(app, session_id)
            sid2, approval2 = await _launch(
                client, f"{base}/api/sessions/{session_id}/messages",
                {"content": "再跑一次 bash"},
            )
            assert sid2 == session_id and approval2
            await _approve(client, base, session_id, approval2["data"]["approval_id"])
            assert (await _wait_terminal(store, session_id)).type == "run/completed"

            await _wait_idle(app, session_id)
            sid3, approval3 = await _launch(
                client, f"{base}/api/sessions/{session_id}/resume",
                {"task": "再跑一次 bash"},
            )
            assert sid3 == session_id and approval3
            await _approve(client, base, session_id, approval3["data"]["approval_id"])
            assert (await _wait_terminal(store, session_id)).type == "run/completed"

            # leg 4：permission changed（此刻无 pending → 200，ADR-0041 D2/D4）
            resp = await client.post(
                f"{base}/api/sessions/{session_id}/permission",
                json={"permission_mode": "workspace-write", "auto_approve": False},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["permission_mode"] == "workspace-write"

            # leg 5：pending 审批禁改档（D5：409，不落 permission/changed）
            await _wait_idle(app, session_id)
            _sid5, approval5 = await _launch(
                client, f"{base}/api/sessions/{session_id}/messages",
                {"content": "再跑一次 bash"},
            )
            assert approval5, "改档后续聊仍须弹审批卡"
            pending_id = approval5["data"]["approval_id"]
            resp = await client.post(
                f"{base}/api/sessions/{session_id}/permission",
                json={"permission_mode": "read-only", "auto_approve": False},
            )
            assert resp.status_code == 409, (
                f"有 pending 审批时改档必须 409（D5），实际 {resp.status_code}"
            )

            # leg 6：cancel → run/failed[reason=cancelled] + pending 审批 fail-closed 结清
            resp = await client.post(f"{base}/api/sessions/{session_id}/cancel")
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "cancelling"
            terminal = await _wait_terminal(store, session_id)
            assert terminal.type == "run/failed", (
                f"取消后应 run/failed，实际 {terminal.type}"
            )
            assert terminal.data.get("reason") == "cancelled", (
                f"取消终态 reason 必须=cancelled（取消与失败不混淆，02 §17），"
                f"实际 {terminal.data.get('reason')}"
            )

            events = store.read_events(session_id)
            decisions = _assert_approval_pairs(events)
            assert decisions[pending_id] == "deny", (
                "取消的等待必须留下成对 fail-closed deny 结清（#316 谓词 2 配对纪律）"
            )
    finally:
        await _shutdown(server, serve_task)
