"""`tests/web` 真实 uvicorn 服务器接缝（#1534）。

按**等价生命周期**收容重复 helper：起服务（启动 deadline + 启动失败 reap）、
拆服务、建会话。只收容证实等价的实现；带专属 monkeypatch / 请求载荷差异的
文件保留一层薄 wrapper，调用点不动（差异逐组记在
`docs/1534-test-harness-cohesion.md`）。

回收边界：本模块只负责**调用方 teardown 路径**（启动失败 / 断言失败 / 取消时
经 try/finally + `shutdown_test_server` 真实回收 serve_task 与 lifespan task）。
看门狗 `os._exit(1)` 的进程级回收不在本票范围（#1702）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

#: SSE 帧前缀（`test_progress_api` / `test_artifacts_api` /
#: `test_session_list_workspace` 等仍自带的 `_DATA_PREFIX = "data:"` 同值）。
DATA_PREFIX = "data:"

#: 起服务的总 deadline。取代 15 份裸 `for _ in range(100): sleep(0.05)`：那些
#: 写法起不来时只 `assert server.started`，既无归因也不 reap 已经起来的任务。
SERVER_START_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.05


def make_settings(tmp_path, **overrides: Any):
    """测试服务端统一 Settings：不读 .env、工作区进 tmp_path、假 API key。"""
    from agent_harness.config import Settings

    return Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test", **overrides,
    )


async def start_test_server(
    tmp_path,
    monkeypatch=None,
    *,
    app=None,
    model_factory=None,
    settings_kwargs: dict[str, Any] | None = None,
    allow_testserver: bool = True,
):
    """起真实 uvicorn（port=0），返回 `(server, serve_task, port, app)`。

    启动失败 / 超时：在函数内 reap 已起的 serve_task 与 lifespan task、并关掉
    可能已绑定的监听 socket，再把捕获到的异常抛给调用方——deadline 到期时是
    `TimeoutError`；serve_task 自己失败时是它抛的那个异常（如 `RuntimeError`）；
    取消路径是 `CancelledError`。

    已知边界（uvicorn 决定，非本模块可改）：lifespan 启动失败时 uvicorn 于
    `Server.startup` 内 `sys.exit(3)`，这个 `SystemExit(3)` 由事件循环直接抛出
    （不经本函数的 `except`），pytest 把它记在当前用例上、进程继续。所以这里的
    reap 只管 `Exception` / `CancelledError`。
    """
    import uvicorn

    if app is None:
        from agent_harness.web.app import create_app

        if model_factory is not None:
            assert monkeypatch is not None, "注入模型工厂时必须给 monkeypatch"
            monkeypatch.setattr(
                "agent_harness.assembly.create_chat_model", model_factory
            )
        app = create_app(
            make_settings(tmp_path, **(settings_kwargs or {})),
            allow_testserver=allow_testserver,
        )

    lifespan_task = None

    async def _track_lifespan_task(scope, receive, send):
        nonlocal lifespan_task
        if scope["type"] == "lifespan":
            lifespan_task = asyncio.current_task()
        await app(scope, receive, send)

    config = uvicorn.Config(
        _track_lifespan_task, host="127.0.0.1", port=0,
        log_level="error", lifespan="on",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    startup_timeout = asyncio.timeout(SERVER_START_TIMEOUT_SECONDS)
    try:
        async with startup_timeout:
            while not server.started:
                if serve_task.done():
                    await serve_task
                    raise RuntimeError("Uvicorn exited before startup completed")
                await asyncio.sleep(_POLL_SECONDS)
    except (Exception, asyncio.CancelledError) as exc:
        server.should_exit = True
        if not serve_task.done():
            serve_task.cancel()
        await asyncio.gather(serve_task, return_exceptions=True)
        if lifespan_task is not None and not lifespan_task.done():
            lifespan_task.cancel()
            await asyncio.gather(lifespan_task, return_exceptions=True)
        # deadline 可能落在「已绑定、未 started」的窗口里：那时 serve_task 被 cancel
        # 掉，uvicorn 的 `serve()` 来不及关 `asyncio.Server`，端口会一直占着。
        _close_listening_sockets(server)
        if startup_timeout.expired():
            raise TimeoutError(
                f"Uvicorn did not start within {SERVER_START_TIMEOUT_SECONDS:g}s"
            ) from exc
        raise
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, serve_task, port, app


async def shutdown_test_server(server, serve_task, *, grace: float = 1.0) -> None:
    """拆服务：先优雅停，退不干净再取消。

    原先 15 份 `_shutdown` 一律 `should_exit = True` 后立刻 `cancel()`：serve_task
    被取消时 uvicorn 来不及关监听 socket、也跑不到 lifespan shutdown，于是
    watchtower / ask-user ticker 这类后台任务留在事件循环里，端口在用例结束后
    仍不可重绑（#1534 AC2 的"回收"正是在这里不成立）。先给 `should_exit` 一次
    机会（无在途连接时约 0.2s，顺带跑完 lifespan shutdown）；有在途 SSE/WS
    连接不退时在 `grace` 秒后 cancel——**取消来源与旧实现相同**（uvicorn
    shutdown → `AppState.shutdown()` → `RunManager.aclose()`），只是时序推迟至多
    `grace` 秒，可观察断言不变（代价：这类用例的 teardown 多约 1s）。

    已知边界：有在途连接时走的是 cancel 分支，**只保证 serve_task 结束与监听
    socket 关闭，不保证 lifespan shutdown 跑完**（uvicorn 侧 `LifespanOn.main`
    可能仍是取消收尾）。需要精确 lifespan 收尾的用例应在调用前先关掉自己的流。
    """
    server.should_exit = True
    try:
        await asyncio.wait_for(asyncio.shield(serve_task), grace)
        return
    except TimeoutError:
        # 只有 serve_task **自己**抛的 TimeoutError 才上抛；grace 到期（`wait_for`
        # 抛的）走下面的 cancel 兜底。按异常种类判，不按 `done()` 判——上一版在
        # serve_task 以超时完成时也会错误地上抛。
        if serve_task.done() and not serve_task.cancelled():
            exc = serve_task.exception()
            if isinstance(exc, TimeoutError):
                # 上抛前同样补关：serve_task 以超时结束不等于监听 socket 已关。
                _close_listening_sockets(server)
                raise
    except asyncio.CancelledError:
        # 调用方自己被取消：仍要收掉 serve_task，再把取消传出去。
        serve_task.cancel()
        await asyncio.gather(serve_task, return_exceptions=True)
        _close_listening_sockets(server)
        raise
    serve_task.cancel()
    await asyncio.gather(serve_task, return_exceptions=True)
    _close_listening_sockets(server)


def _close_listening_sockets(server) -> None:
    """取消路径上补关监听 socket。

    `uvicorn.Server.serve()` 的 `finally` 只在 `shutdown()` 跑过时关 socket；在
    `main_loop()` 里被 cancel 时它来不及关，端口就一直占着（旧 15 份 `_shutdown`
    的"端口不可重绑"正是这个）。这里同步关——不 await，所以在被取消的上下文里
    也能执行。uvicorn 自己关的也是 `asyncio.Server.close()`（不是 socket 对象）。
    """
    for srv in getattr(server, "servers", None) or []:
        try:
            srv.close()
        except (AttributeError, OSError):
            pass


def session_id_from_sse(resp) -> str:
    """从完整 SSE 响应体里取第一个带 session_id 的帧。"""
    frames = [
        json.loads(line[len(DATA_PREFIX):].strip())
        for line in resp.text.splitlines()
        if line.startswith(DATA_PREFIX)
    ]
    session_id = next((f["session_id"] for f in frames if f.get("session_id")), None)
    assert session_id, f"SSE 流里没有 session_id：{frames[:3]}"
    return str(session_id)


def create_session(
    client,
    *,
    task: str = "hi",
    model: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """真 runtime + 替身模型建会话（跑完一轮），返回 session_id。

    `extra` 并入请求体（cwd / workspace / sandbox_backend / permission_mode 等）。
    """
    from unittest.mock import patch

    from langchain_core.messages import AIMessage

    from tests.scripted_model import ScriptedModel

    body: dict[str, Any] = {"task": task, "budget": {"local": {"max_agent_turns": 1}}}
    if model is not None:
        body["model"] = model
    if extra:
        body.update(extra)
    with patch(
        "agent_harness.assembly.create_chat_model",
        return_value=ScriptedModel(responses=[AIMessage(content="ok")]),
    ):
        resp = client.post("/api/sessions", json=body)
    assert resp.status_code == 200, resp.text
    return session_id_from_sse(resp)


def create_session_no_launch(client, payload: dict[str, Any] | None = None) -> str:
    """只建会话记录、不启动 run（`?launch=false`）。"""
    resp = client.post("/api/sessions?launch=false", json=payload or {})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]
