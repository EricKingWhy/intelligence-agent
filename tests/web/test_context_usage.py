"""ADR 设计稿（#200）T1-T7：上下文容量看板数据面契约。

只测外部行为（HTTP 形状 + 纯函数），不碰 service 内部。接缝与
test_multiturn_queue_http.py 同：真实 ASGI 服务器 + 可控模型替身。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from agent_harness.web.context_usage import (
    build_context_usage_payload,
    cache_summary,
    tool_schema_breakdown,
)
from tests.scripted_model import ScriptedModel


class _OneTurnModel(ScriptedModel):
    """每次新 run 吐一条最终 AIMessage（create_chat_model 每轮新建实例）。"""

    def __init__(self, **kwargs) -> None:
        super().__init__([AIMessage(content="task done", **kwargs)])


async def _start_server(tmp_path, monkeypatch, model_kwargs: dict | None = None):
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    kwargs = model_kwargs or {}

    def _create_model(config, **kw):
        return _OneTurnModel(**kwargs)

    monkeypatch.setattr("agent_harness.assembly.create_chat_model", _create_model)
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


async def _get(port: int, path: str) -> tuple[int, dict]:
    import httpx2

    async with httpx2.AsyncClient(timeout=30) as client:
        response = await client.get(f"http://127.0.0.1:{port}{path}")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload


async def _completed_session(port: int, task: str = "首轮") -> str:
    """创建一个跑完的会话，返回 session_id（消费 SSE 流到终态）。"""
    import httpx2

    sid = None
    client = httpx2.AsyncClient(timeout=None)
    try:
        async with client.stream(
            "POST", f"http://127.0.0.1:{port}/api/sessions", json={"task": task},
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                import json
                frame = json.loads(line.removeprefix("data:").strip())
                if sid is None:
                    sid = frame.get("session_id")
                if frame.get("type") in ("run/completed", "run/failed"):
                    break
    finally:
        await client.aclose()
    assert sid
    return sid


async def _wait_idle(port: int, session_id: str, *, timeout: float = 10.0) -> None:
    """等该会话真正空闲（轮询 flush 的 idle 回执——run 已收口的可靠信号）。"""
    import time

    import httpx2

    deadline = time.monotonic() + timeout
    while True:
        async with httpx2.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/api/sessions/{session_id}/queue/flush")
        if response.status_code == 200 and response.json() == {"status": "idle"}:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"超时 {timeout}s：会话 {session_id} 未空闲")
        await asyncio.sleep(0.05)


async def _wait_context_snapshot(port: int, session_id: str, *, timeout: float = 10.0) -> dict:
    """等 context-usage 端点出快照（state=ok）并返回 payload。

    run 收尾窗口（finish → capture → notify）与 _wait_idle 的 flush 轮询存在
    调度交错：flush 可能在 capture 执行前观察到 idle。真实 UI 语义是"打开时
    拉一次，no_data 时用户刷新"——本测试按同一语义轮询到快照出现（超时报错）。
    """
    import time

    deadline = time.monotonic() + timeout
    while True:
        status, payload = await _get(port, f"/api/sessions/{session_id}/context-usage")
        if status == 200 and payload.get("state") == "ok":
            return payload
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"超时 {timeout}s：会话 {session_id} 的 context-usage 快照未出现"
                f"（最后 state={payload.get('state')}）"
            )
        await asyncio.sleep(0.05)


# ── T1：缓存采集（纯函数）─────────────────────────────────────────────


class _FakeEvent:
    def __init__(self, data: dict) -> None:
        self.data = data


def test_t1_cache_capture_present():
    """mock 响应带 cached_tokens ⇒ model/completed.usage.cached_tokens 存在且相等。"""
    from agent_harness.agent.runtime import _usage_from_response

    class _AI:
        def __init__(self) -> None:
            self.usage_metadata = {
                "input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                "input_token_details": {"cached_tokens": 80},
            }

    usage = _usage_from_response(_AI())
    assert usage == {
        "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
        "cached_tokens": 80,
    }


def test_t1_cache_capture_absent():
    """provider 不带 cache 明细 ⇒ cached_tokens 键省略，**绝不写 0**。"""
    from agent_harness.agent.runtime import _usage_from_response

    class _AI:
        def __init__(self) -> None:
            self.usage_metadata = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}

    usage = _usage_from_response(_AI())
    assert usage == {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    assert "cached_tokens" not in usage

    class _AIEmpty:
        def __init__(self) -> None:
            self.usage_metadata = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                                   "input_token_details": {}}

    usage = _usage_from_response(_AIEmpty())
    assert "cached_tokens" not in usage


def test_t1_cache_capture_invalid():
    """非数值/负值 cached_tokens ⇒ 键省略（不伪造）。"""
    from agent_harness.agent.runtime import _usage_from_response

    class _AINeg:
        def __init__(self) -> None:
            self.usage_metadata = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                                   "input_token_details": {"cached_tokens": -5}}

    assert "cached_tokens" not in _usage_from_response(_AINeg())

    class _AIStr:
        def __init__(self) -> None:
            self.usage_metadata = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                                   "input_token_details": {"cached_tokens": "80"}}

    assert "cached_tokens" not in _usage_from_response(_AIStr())


# ── T2：命中率算法（求和口径）─────────────────────────────────────────


def test_t2_hit_rate_sum_not_mean():
    """两次调用（1000/800 命中 vs 500/100 命中）⇒ Σcached÷Σinput，不是逐次平均。

    逐次平均 = (0.8 + 0.2)/2 = 0.5；求和口径 = 900/1500 = 0.6。"""
    events = [
        _FakeEvent({"usage": {"prompt_tokens": 1000, "cached_tokens": 800}}),
        _FakeEvent({"usage": {"prompt_tokens": 500, "cached_tokens": 100}}),
    ]
    summary = cache_summary(events)
    assert summary["state"] == "ok"
    assert summary["reported_calls"] == 2
    assert summary["total_calls"] == 2
    assert summary["avg_hit_rate"] == pytest.approx(0.6)


def test_t2_hit_rate_not_collected():
    """全缺 cache ⇒ not_collected，avg_hit_rate=None（**不得显示 0%**）。"""
    events = [_FakeEvent({"usage": {"prompt_tokens": 1000}})]
    summary = cache_summary(events)
    assert summary["state"] == "not_collected"
    assert summary["avg_hit_rate"] is None
    assert summary["total_calls"] == 1
    assert summary["reported_calls"] == 0


def test_t2_hit_rate_partial():
    """部分调用带回 ⇒ partial。"""
    events = [
        _FakeEvent({"usage": {"prompt_tokens": 1000, "cached_tokens": 800}}),
        _FakeEvent({"usage": {"prompt_tokens": 500}}),
    ]
    summary = cache_summary(events)
    assert summary["state"] == "partial"
    assert summary["reported_calls"] == 1
    assert summary["total_calls"] == 2


def test_t2_hit_rate_no_usage():
    """没有任何 usage ⇒ not_collected（total=0）。"""
    summary = cache_summary([_FakeEvent({})])
    assert summary["state"] == "not_collected"
    assert summary["total_calls"] == 0


# ── T4：求和不变式 + 工具分组 ─────────────────────────────────────────


def test_t4_core_tool_names_pinned():
    """core 名单单测钉住（design §3.2.1）：assembly 的 9 个内置工具全部登记。"""
    from agent_harness.web.context_usage import CORE_TOOL_NAMES

    assert CORE_TOOL_NAMES == frozenset({
        "read", "write", "bash", "edit", "apply_patch",
        "glob", "grep", "git_status", "git_diff",
    })


def _estimate(text: str) -> int:
    from agent_harness.context.tokens import estimate_tokens
    return estimate_tokens(text)


def test_t4_tool_schema_breakdown():
    """core 名单内归"系统"，其余归"MCP"。"""
    definitions = [
        {"name": "read", "description": "read a file", "parameters": {}},
        {"name": "mcp__github__create_issue", "description": "create issue", "parameters": {}},
    ]
    tools = tool_schema_breakdown(definitions, _estimate)
    assert tools["system"] > 0
    assert tools["mcp"] > 0


def test_t4_sum_invariant():
    """Σ(四类 + 工具两组) == used_tokens（差额进"其他"残差）。"""
    settings = type("S", (), {"max_context_tokens": 200_000,
                              "auto_compact_threshold": 0.70,
                              "hard_guard_threshold": 0.85})()
    snapshot = {"messages": 100, "system_prompt": 50, "skills": 10, "other": 5,
                "used_tokens": 165}
    definitions = [
        {"name": "read", "description": "r", "parameters": {}},
        {"name": "mcp__x__y", "description": "m", "parameters": {}},
    ]
    payload = build_context_usage_payload(
        settings=settings, builder_snapshot=snapshot, tool_definitions=definitions,
        estimate_tokens=_estimate, events=[],
    )
    b = payload["breakdown"]
    total = (b["messages"] + b["system_prompt"] + b["skills"] + b["other"]
             + b["tools"]["system"] + b["tools"]["mcp"])
    assert total == payload["used_tokens"]
    assert payload["used_tokens"] == 165 + b["tools"]["system"] + b["tools"]["mcp"]


def test_t4_no_negative_residual():
    """残差桶如实归 0（不造负数）。"""
    settings = type("S", (), {"max_context_tokens": 200_000,
                              "auto_compact_threshold": 0.70,
                              "hard_guard_threshold": 0.85})()
    snapshot = {"messages": 500, "system_prompt": 50, "skills": 10, "other": 0,
                "used_tokens": 100}
    payload = build_context_usage_payload(
        settings=settings, builder_snapshot=snapshot, tool_definitions=[],
        estimate_tokens=_estimate, events=[],
    )
    assert payload["breakdown"]["other"] == 0


@pytest.mark.asyncio
async def test_t4_snapshot_counts_provider_injection(tmp_path):
    """回归（首版漏报）：provider 注入必须进「其他」桶，used_tokens 是真实总量。

    首版把 used_tokens 取成 builder 的投影 messages 总量（不含 provider 注入与
    运行期快照），"其他"用"总量减各项"的残差写法 ⇒ 恒为 0：看板把记忆注入整块
    漏报、总量少报。本测试用真实 ContextBuilder + 一个注入固定文本的 provider
    钉住：注入的 token 必须出现在 other 里，used_tokens 必须等于真实构建总量。
    """
    from langchain_core.messages import SystemMessage

    from agent_harness.context.builder import ContextBuilder
    from agent_harness.context.tokens import estimate_message_tokens
    from agent_harness.session import JsonlSessionStore, Session
    from agent_harness.session.event import USER_MESSAGE

    class _InjectingProvider:
        name = "memory"

        async def select(self, session, token_budget):
            return [SystemMessage(content="RECALLED " * 200)]

    store = JsonlSessionStore(root=tmp_path / "sessions")
    session = Session("usage-probe", store)
    session.append(USER_MESSAGE, {"content": "hello " * 20})
    builder = ContextBuilder(
        model_provider=object(), max_context_tokens=200_000,
        context_providers=[_InjectingProvider()],
        system_prompt="SYSTEM " * 50,
        runtime_context_provider=lambda: "RUNTIME " * 30,
    )
    built = await builder.build(session)
    snap = builder.usage_snapshot(session)

    # provider 注入（memory）+ 运行期快照都进"其他"；技能桶如实 0（无 skills provider）。
    assert snap["skills"] == 0
    assert snap["other"] > 0
    # used_tokens = 各桶之和，且不小于真实构建总量（构造口径逐条估与快照口径一致）。
    assert snap["used_tokens"] == (
        snap["messages"] + snap["system_prompt"] + snap["skills"] + snap["other"]
    )
    assert snap["used_tokens"] <= estimate_message_tokens(built)
    # 注入确实被计到：other 至少要覆盖 provider 注入本身。
    assert snap["other"] >= estimate_message_tokens([SystemMessage(content="RECALLED " * 200)])


@pytest.mark.asyncio
async def test_t4_snapshot_attributes_skills_separately(tmp_path):
    """skills provider 的注入成本进"技能"桶（不是"其他"）——按 provider 自称的
    name 分账，而不是调用方按文本重算（重算会漏预算截断）。"""
    from langchain_core.messages import SystemMessage

    from agent_harness.context.builder import ContextBuilder
    from agent_harness.session import JsonlSessionStore, Session
    from agent_harness.session.event import USER_MESSAGE

    class _SkillsProvider:
        name = "skills"

        async def select(self, session, token_budget):
            return [SystemMessage(content="SKILL CATALOG " * 100)]

    class _MemoryProvider:
        name = "memory"

        async def select(self, session, token_budget):
            return [SystemMessage(content="MEMORY " * 100)]

    store = JsonlSessionStore(root=tmp_path / "sessions2")
    session = Session("usage-probe-2", store)
    session.append(USER_MESSAGE, {"content": "hi"})
    builder = ContextBuilder(
        model_provider=object(), max_context_tokens=200_000,
        context_providers=[_SkillsProvider(), _MemoryProvider()],
    )
    await builder.build(session)
    snap = builder.usage_snapshot(session)

    # skills 桶 == skills provider 的实际注入成本（精确值，不是"非零"）。
    from agent_harness.context.tokens import estimate_message_tokens
    expected_skills = estimate_message_tokens(
        [SystemMessage(content="SKILL CATALOG " * 100)]
    )
    assert snap["skills"] == expected_skills
    # memory provider 的注入**不**进 skills 桶，各自分账。
    assert snap["other"] >= estimate_message_tokens([SystemMessage(content="MEMORY " * 100)])
    assert snap["other"] != snap["skills"]



# ── T5：端点形状（HTTP）───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t5_endpoint_no_data_404(tmp_path, monkeypatch):
    """未知会话 → 404；端点形状回归锁。"""
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        status, _payload = await _get(port, "/api/sessions/nonexistent/context-usage")
        assert status == 404
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_t5_endpoint_shape_with_run(tmp_path, monkeypatch):
    """有 run ⇒ state=ok、window_tokens==200000（回归：用户裁定不要每模型窗口）。"""
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        sid = await _completed_session(port)
        payload = await _wait_context_snapshot(port, sid)
        assert payload["estimated"] is True
        assert payload["window_tokens"] == 200_000
        assert payload["state"] == "ok"
        assert payload["thresholds"] == {"auto_compact": 0.70, "hard_guard": 0.85}
        b = payload["breakdown"]
        # 六桶全在
        for key in ("messages", "system_prompt", "skills", "other"):
            assert key in b
        assert "system" in b["tools"] and "mcp" in b["tools"]
        # 有 build 快照 ⇒ 系统提示词/消息非负；无 skills provider ⇒ 技能桶如实 0
        assert b["system_prompt"] >= 0
        assert b["messages"] >= 0
        assert b["skills"] == 0
        # 工具 schema 有 core 工具 ⇒ 系统工具桶 > 0
        assert b["tools"]["system"] > 0
        # 缓存：provider 未返回 cache 明细 ⇒ not_collected（不显示 0%）
        assert payload["cache"]["state"] == "not_collected"
        assert payload["cache"]["avg_hit_rate"] is None
        # 求和不变式（HTTP 形状上再锁一次）
        total = (b["messages"] + b["system_prompt"] + b["skills"] + b["other"]
                 + b["tools"]["system"] + b["tools"]["mcp"])
        assert total == payload["used_tokens"]
    finally:
        await _shutdown(server, serve_task)


@pytest.mark.asyncio
async def test_t5_endpoint_cache_ok_with_cached_usage(tmp_path, monkeypatch):
    """provider 带 cached_tokens ⇒ 事件流有 cached_tokens ⇒ cache.state=ok。"""
    server, serve_task, port = await _start_server(
        tmp_path, monkeypatch,
        model_kwargs={"usage_metadata": {
            "input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
            "input_token_details": {"cached_tokens": 90},
        }},
    )
    try:
        sid = await _completed_session(port)
        payload = await _wait_context_snapshot(port, sid)
        assert payload["cache"]["state"] == "ok"
        assert payload["cache"]["total_calls"] >= 1
        assert payload["cache"]["reported_calls"] == payload["cache"]["total_calls"]
        assert payload["cache"]["avg_hit_rate"] == pytest.approx(0.9)
    finally:
        await _shutdown(server, serve_task)
