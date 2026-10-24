"""#1547 AC2（HTTP 边界）：完成顺序颠倒的并发写不得让 SSE 丢帧/乱序。

真实链路端到端：`follow=1` 连接 → run 的 session listener →
`RunManager.enqueue_session_event`（seq 幂等门，#1547 的丢帧点）→ 会话级扇出。
在 run 进行中从**旁路**并发 `append_async`（生产形状：记忆回写 / 服务层旁路写者），
fsync 完成顺序按入口序**倒置**（同 `tests/session/test_ordered_delivery.py` 的
夹具形状）。修复前：通知乱序 → 幂等门把迟到帧当重复**永久丢弃**（live 丢帧）；
修复后：全部帧按 seq 序到达、无重复。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Self

import pytest
from langchain_core.messages import AIMessage

#: 一次 fsync 前的睡眠步长（入口序倒置夹具）：写者按入口序 0..N-1 前进，
#: 首个进入者睡最久 ⇒ 完成顺序与取号顺序恰好相反。
STAGGER_SECONDS = 0.03
BYPASS_WRITERS = 6


class SlowModel:
    """一轮即收尾，但先睡 0.6s——给旁路并发写留出与 run listener 共存的窗口。"""

    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        await asyncio.sleep(0.6)
        yield AIMessage(content="done")


def _install_inverted_commit(monkeypatch: pytest.MonkeyPatch, writers: int) -> None:
    """把 `_GroupFlush.commit` 换成「按入场序反比延迟」的替身（#1547 复现夹具）。

    与 `tests/session/test_ordered_delivery.py` 的夹具同形：入场序 = 批内 seq 序
    （写阶段被会话写锁串行化），首号睡最久 ⇒ 完成顺序严格倒挂于 seq 顺序。
    """
    import threading
    import time

    from agent_harness.session.store import _GroupFlush

    real_commit = _GroupFlush.commit
    lock = threading.Lock()
    entries: list[int] = []

    def staggered_commit(self: _GroupFlush, path) -> None:
        with lock:
            entries.append(len(entries))
            index = len(entries) - 1
        delay = max(0, writers - 1 - index) * STAGGER_SECONDS
        if delay:
            time.sleep(delay)
        return real_commit(self, path)

    monkeypatch.setattr(_GroupFlush, "commit", staggered_commit)


async def _start_server(tmp_path, monkeypatch):
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model", lambda config, **kw: SlowModel())
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


async def _shutdown(server, serve_task) -> None:
    server.should_exit = True
    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass


def _parse_frame(line: str) -> dict:
    return json.loads(line.removeprefix("data:").strip())


async def _collect(port: int, path: str, body: dict | None) -> list[dict]:
    """一次性收流（默认非 follow 端点重放完即收流）并返回全部帧。"""
    import httpx2

    frames: list[dict] = []
    client = httpx2.AsyncClient(timeout=None)
    try:
        if body is not None:
            context = client.stream("POST", f"http://127.0.0.1:{port}{path}", json=body)
        else:
            context = client.stream("GET", f"http://127.0.0.1:{port}{path}")
        async with context as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    frames.append(_parse_frame(line))
    finally:
        await client.aclose()
    return frames


class _Follow:
    """后台持有的 follow=1 连接：持续收集帧，直到谓词满足。"""

    def __init__(self, port: int, path: str) -> None:
        self.port = port
        self.path = path
        self.frames: list[dict] = []
        self.status: int | None = None
        self.closed = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        import httpx2

        client = httpx2.AsyncClient(timeout=None)
        try:
            async with client.stream(
                "GET", f"http://127.0.0.1:{self.port}{self.path}"
            ) as response:
                self.status = response.status_code
                if response.status_code != 200:
                    return
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        self.frames.append(_parse_frame(line))
        finally:
            await client.aclose()
            self.closed.set()

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._run())
        for _ in range(200):
            if self.status is not None or self.closed.is_set():
                break
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def types(self) -> list[str]:
        return [f.get("type") for f in self.frames]

    async def wait_for(self, pred: Callable[[list[dict]], bool],
                       timeout: float = 10.0) -> None:
        async with asyncio.timeout(timeout):
            while not pred(self.frames):
                if self.closed.is_set():
                    raise AssertionError(f"流已收流但条件未满足：{self.types()}")
                await asyncio.sleep(0.02)


async def _create_idle_session(port: int) -> str:
    import httpx2

    async with httpx2.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/api/sessions?launch=false",
            # 本票考事件投递序，不考沙箱后端：显式 local，测试在无 Docker 的
            # 开发机与 CI 上同形（None ⇒ Windows 默认 docker，见 CreateSessionRequest）。
            json={"sandbox_backend": "local"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


async def _start_run_background(port: int, session_id: str) -> asyncio.Task:
    """起 run 但不等它收尾（POST 的 SSE 流由后台任务收到 run/completed 为止）。"""
    import httpx2

    async def drain() -> None:
        client = httpx2.AsyncClient(timeout=None)
        try:
            async with client.stream(
                "POST", f"http://127.0.0.1:{port}/api/sessions/{session_id}/messages",
                json={"content": "hi", "mode": "queue"},
            ) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    if _parse_frame(line).get("type") in {"run/completed", "run/failed"}:
                        return
        finally:
            await client.aclose()

    task = asyncio.create_task(drain())
    return task


@pytest.mark.asyncio
async def test_follow_receives_all_frames_when_fsync_completion_inverts(
    tmp_path, monkeypatch,
):
    """#1547 主症状（live 丢帧）在真实 HTTP 边界不复现。

    修复前（#1489 引入的回归形状）：`enqueue_session_event` 的 seq 幂等门把
    「迟到」帧当重复丢弃——本用例表现为 BYPASS_WRITERS 条旁路事件只有乱序
    抵达的前缀可见，且 seq 序颠倒。修复后：帧按 seq 严格递增、六条全到、无重复。
    """
    from agent_harness.session import USER_MESSAGE

    server, serve_task, port, app = await _start_server(tmp_path, monkeypatch)
    try:
        session_id = await _create_idle_session(port)
        async with _Follow(
            port, f"/api/sessions/{session_id}/stream?after_seq=-1&follow=1"
        ) as follower:
            assert follower.status == 200
            await follower.wait_for(lambda fs: len(fs) >= 1)  # 重放 session/started

            run_task = await _start_run_background(port, session_id)
            await follower.wait_for(
                lambda fs: "run/started" in [f.get("type") for f in fs])

            # run 的 listener 已挂上：取它正在用的 Session（每次 launch 重建，
            # 只能从 RunManager 拿——与既有 web 测试同一访问口径）。
            manager = app.state.agent.run_manager
            session = manager._runs[session_id].session

            # 旁路并发写（生产形状：记忆回写/服务层旁路写者）+ fsync 完成序倒置。
            _install_inverted_commit(monkeypatch, writers=BYPASS_WRITERS)
            persisted = await asyncio.gather(*[
                session.append_async(USER_MESSAGE, {"content": f"bypass-{i}"})
                for i in range(BYPASS_WRITERS)
            ])
            bypass_seqs = {event.seq for event in persisted}

            await follower.wait_for(
                lambda fs: "run/completed" in [f.get("type") for f in fs])
            await run_task

            seqs = [f["seq"] for f in follower.frames if f.get("seq") is not None]
            missing = bypass_seqs - set(seqs)
            assert not missing, (
                f"SSE 丢帧（幂等门吃掉迟到帧）：缺 {sorted(missing)}，"
                f"旁路 seq={sorted(bypass_seqs)}，实收={seqs}"
            )
            assert seqs == sorted(seqs), f"帧非按 seq 序：{seqs}"
            assert len(seqs) == len(set(seqs)), f"帧重复：{seqs}"

            # AC2 的重放半边：断连重连（after_seq=-1，默认非 follow 收流）必须
            # 从盘面补回全部帧——live 曾丢的帧对重放也不可见才是「永久丢帧」。
            replay = await _collect(
                port, f"/api/sessions/{session_id}/stream?after_seq=-1", None)
            replay_seqs = [f["seq"] for f in replay if f.get("seq") is not None]
            assert replay_seqs == sorted(replay_seqs)
            assert bypass_seqs <= set(replay_seqs), (
                f"重放补不回旁路帧：缺 {sorted(bypass_seqs - set(replay_seqs))}"
            )
    finally:
        await _shutdown(server, serve_task)
