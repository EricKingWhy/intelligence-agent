"""SSE keepalive：注释帧必须真的上线，且**先于**任何数据帧到达。

## 为什么这条测试值得独立存在

这类问题的失败面**只在部署层可见**：本地直连时 uvicorn 会把每个 chunk 立刻写出去，
有没有 keepalive 看起来都一样；而中间交付层（`CloudStudio Gateway` / 腾讯 EdgeOne）
会把**整个响应**攒到流结束才下发——实测 `POST /api/sessions` 的响应头 44.158s 才到
（≈ run 全长），326 帧数据全在其后 0.094s 内到齐，前端于是拿不到任何字节直到 run
跑完（打字机效果不可能存在）。

持续有字节流动是让中间层及时 flush 的前提，所以这里断言的是**线序**：第一个到达客户端的
帧必须是 keepalive 注释帧（`: ping`），而不是等模型出话之后才来的 data 帧。断言内部标志位
（比如"EventSourceResponse 收到了 ping=2"）挡不住「库版本默认值变了 / 有人改成 ping=0」
这类回归——只有真实端点上的字节时序能。

## 反缓冲头

`X-Accel-Buffering: no` / `Connection: keep-alive` 由 sse-starlette 默认带上，
一并钉住：它们哪天从默认集里消失，中间层攒包就会复发，而本地测试**看不见**这种退化。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from langchain_core.messages import AIMessage

from tests.scripted_model import ScriptedModel

#: 模型"思考"时长——必须显著大于 keepalive 间隔，否则 ping 与数据帧会挤在一起，
#: 断言就变成看运气。2s 间隔 → 4s 留出一个完整周期还多。
_MODEL_DELAY_SECONDS = 4.0


class _SlowModel(ScriptedModel):
    """延迟一会儿再出话的模型：把 SSE 流撑开，让 keepalive 有机会先上线。

    真实场景对应「模型在长思考 / 工具在跑」——那正是用户最需要看到"还活着"的窗口，
    也正是 keepalive 存在的理由。
    """

    async def ainvoke(self, messages, **kwargs):  # type: ignore[override]
        await asyncio.sleep(_MODEL_DELAY_SECONDS)
        return await super().ainvoke(messages, **kwargs)

    async def astream(self, messages, **kwargs):  # type: ignore[override]
        await asyncio.sleep(_MODEL_DELAY_SECONDS)
        async for chunk in super().astream(messages, **kwargs):
            yield chunk


async def _start_server(tmp_path, monkeypatch):
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    monkeypatch.setattr(
        "agent_harness.assembly.create_chat_model",
        lambda config, **kw: _SlowModel([AIMessage(content="done")]),
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
    return server, serve_task, port


async def _shutdown(server, serve_task) -> None:
    server.should_exit = True
    serve_task.cancel()
    try:
        await serve_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_keepalive_ping_precedes_first_data_frame(tmp_path, monkeypatch):
    """真端点上的线序：模型还在思考时，keepalive 注释帧已经在上线。"""
    import httpx2

    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        # 必须是**异步**客户端：服务端与测试跑在同一个事件循环里，同步 httpx
        # 会把循环堵住，请求永远等不到响应（表现为 ReadTimeout，看起来像后端不发流）。
        async with httpx2.AsyncClient(timeout=30) as client, client.stream(
            "POST", f"http://127.0.0.1:{port}/api/sessions",
            json={"task": "长思考任务"},
        ) as response:
            assert response.status_code == 200
            # 反缓冲头：中间层据此决定是否攒包。库默认带上，这里钉住。
            assert response.headers.get("x-accel-buffering") == "no"
            assert response.headers.get("cache-control") == "no-store"
            assert response.headers.get("connection") == "keep-alive"
            assert response.headers.get("content-type", "").startswith(
                "text/event-stream"
            )

            # 读到模型**首个输出**为止（run/started、model/started 这些
            # 在模型出话前就发的事件是合法的即时帧，不能拿它们当判据）。
            started = time.monotonic()
            pings: list[tuple[float, str]] = []
            model_spoke_at: float | None = None
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                elapsed = time.monotonic() - started
                if line.startswith(":"):
                    pings.append((elapsed, line))
                elif "text/delta" in line:
                    model_spoke_at = elapsed
                    break

        # 这一条是整条测试的重点：模型还在"思考"（delay=4s）时，线上已经有
        # keepalive 字节在流动——这正是让中间层及时 flush 的唯一手段。
        assert pings, (
            f"模型出话之前没有任何 keepalive 注释帧（首个输出在 "
            f"{model_spoke_at}s）——交付层会因此攒到流结束才下发"
        )
        first_ping_at, first_ping = pings[0]
        assert "ping" in first_ping, first_ping
        assert first_ping_at < _MODEL_DELAY_SECONDS, (
            f"keepalive 必须在模型出话之前到达（实际 {first_ping_at:.2f}s，"
            f"模型延迟 {_MODEL_DELAY_SECONDS}s）——否则等于没有 keepalive"
        )
        assert model_spoke_at is not None and model_spoke_at > first_ping_at
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_keepalive_ping_is_not_an_event(tmp_path, monkeypatch):
    """keepalive 不得被客户端当成事件：注释帧没有 `data:` 前缀。

    前端 `lib/sse.ts::parseFrame` 只取 `data:` 行，停摆检测（`RECONNECT_STALL_MS`）
    看的是真实帧——若 ping 被写成 `data:` 帧，就会给停摆检测喂**假进展**，
    把「连接僵死」伪装成「run 在推进」。
    """
    import httpx2

    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        async with httpx2.AsyncClient(timeout=30) as client, client.stream(
            "POST", f"http://127.0.0.1:{port}/api/sessions",
            json={"task": "长思考任务"},
        ) as response:
            assert response.status_code == 200
            pings: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith(":"):
                    pings.append(line)
                elif "text/delta" in line:
                    break  # 模型出话即止（之前的即时事件帧不是 keepalive）

        assert pings, "模型出话之前应当已经收到至少一个 keepalive 注释帧"
        for ping in pings:
            assert not ping.startswith("data:"), ping
    finally:
        await _shutdown(server, serve_task)
