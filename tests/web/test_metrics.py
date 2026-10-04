"""/metrics 进程健康观测面（#612）：形状钉 + 降级钉 + 不阻塞钉。

只测外部行为（HTTP 形状 + 并发语义），采集纯函数不 mock 顺序。接缝与
test_context_usage.py 同：真实 ASGI 服务器（uvicorn port=0）+ 可控请求。

三个钉对应票面 AC6：
- 形状钉：Prometheus 文本暴露格式（HELP/TYPE/value 三行一组）+ 指标名集合钉 +
  值可解析且物理合理；
- 降级钉：单项采集失败 ⇒ 该指标整行缺席、其余照常、端点仍 200（AC2 缺席≠占位值）；
- 不阻塞钉：重采样（gc 口径堆扫描）在 worker 线程执行 ⇒ 慢采集进行中事件循环
  仍能响应其他请求（AC3「重采样在 worker 线程或 O(1) 读数」）。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from agent_harness.web import metrics as metrics_mod

# ────────────────────────────────────────────────────────────────────────────
# 服务器接缝（与 test_context_usage.py 同型：真实 uvicorn + httpx）
# ────────────────────────────────────────────────────────────────────────────

EXPECTED_METRICS = {
    "agent_process_rss_bytes",
    "agent_process_heap_gc_bytes",
    "agent_asyncio_tasks_active",
    "agent_process_start_time_seconds",
    "agent_process_uptime_seconds",
}


async def _start_server(tmp_path, monkeypatch):
    import uvicorn

    from agent_harness.config import Settings
    from agent_harness.web.app import create_app

    def _create_model(config, **kw):  # /metrics 与 /api/health 不触模型；占位防呆
        raise AssertionError("metrics tests must not create a chat model")

    monkeypatch.setattr("agent_harness.assembly.create_chat_model", _create_model)
    app = create_app(Settings(
        _env_file=None, workspace_dir=str(tmp_path),
        model_api_key="sk-test", enable_cors=False,
    ))
    config = uvicorn.Config(app, host="127.0.0.1", port=0,
                            log_level="error", lifespan="on")
    server = uvicorn.Server(config)
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


async def _get_raw(port: int, path: str):
    import httpx2

    async with httpx2.AsyncClient(timeout=30) as client:
        response = await client.get(f"http://127.0.0.1:{port}{path}")
        return response.status_code, response.headers, response.text


def _parse_metrics(text: str) -> dict[str, float]:
    """把 Prometheus 文本暴露格式解析成 {metric_name: value}（value 全 float）。"""
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, raw = line.split()
        values[name] = float(raw)
    return values


def _assert_text_format_shape(text: str) -> None:
    """HELP/TYPE/value 三行一组的形状：每个值行前面各有且各有一句 HELP/TYPE。"""
    lines = text.splitlines()
    value_lines = [i for i, ln in enumerate(lines) if ln and not ln.startswith("#")]
    assert value_lines, "至少要有一个指标值行"
    for i in value_lines:
        assert lines[i - 2].startswith(f"# HELP {lines[i].split()[0]} ")
        assert lines[i - 1].startswith(f"# TYPE {lines[i].split()[0]} gauge")


# ────────────────────────────────────────────────────────────────────────────
# 形状钉（AC1/AC2/AC6）
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_shape_pin(tmp_path, monkeypatch):
    """/metrics：200 + 文本暴露格式 + 五个指标齐全 + 值物理合理。"""
    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        status, headers, text = await _get_raw(port, "/metrics")
    finally:
        await _shutdown(server, serve_task)

    assert status == 200
    assert headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in headers["content-type"]
    _assert_text_format_shape(text)

    values = _parse_metrics(text)
    assert set(values) == EXPECTED_METRICS, f"指标名集合漂移：{set(values)}"

    assert values["agent_process_rss_bytes"] > 0
    assert values["agent_process_heap_gc_bytes"] > 0
    assert values["agent_asyncio_tasks_active"] >= 1  # 处理本请求的 server task
    assert values["agent_process_uptime_seconds"] >= 0
    assert values["agent_process_start_time_seconds"] > 0
    # start_time 是 wall-clock 秒（UNIX 时间量级）且不在未来；uptime 是自
    # metrics 模块导入（≈进程启动）起的单调时长——套件语境下进程可能已活
    # 数分钟，不得设拍脑袋上限（教训：`< 60` 在全量套件里把诚实读数打成红）。
    assert values["agent_process_start_time_seconds"] <= time.time()


# ────────────────────────────────────────────────────────────────────────────
# 降级钉（AC2/AC4/AC6）
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_single_collector_failure_degrades_to_absence(
    tmp_path, monkeypatch
):
    """单项采集炸 ⇒ 该指标整行缺席（不是 0/占位值），其余照常，端点仍 200。"""

    def _boom() -> int:
        raise OSError("GetProcessMemoryInfo failed (injected)")

    monkeypatch.setattr(metrics_mod, "_rss_bytes", _boom)

    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        status, _headers, text = await _get_raw(port, "/metrics")
    finally:
        await _shutdown(server, serve_task)

    assert status == 200
    values = _parse_metrics(text)
    assert "agent_process_rss_bytes" not in values  # 缺席 ≠ 占位
    assert EXPECTED_METRICS - {"agent_process_rss_bytes"} <= set(values)


# ────────────────────────────────────────────────────────────────────────────
# 不阻塞钉（AC3/AC6）
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_slow_resampling_does_not_block_event_loop(
    tmp_path, monkeypatch
):
    """重采样在 worker 线程：慢堆扫描进行中，事件循环仍能响应 /api/health。

    把线程侧堆扫描换成 1.5s 慢函数，并做**确定性编排**（审查 P3-1：/api/health
    handler 全同步单步完成，若先发 health，变异成循环内同步扫描时 health 早已
    返回、钉会漏——所以必须先发 /metrics 并确认扫描确实已开始，再发 health，
    两条断言共用同一时间轴 t0）：
    - 轮询 threading.Event 直到慢扫描开跑（正确实现：worker 线程 ~立即置位，
      循环空闲，轮询 ~50ms 内通过；变异实现：扫描占住循环，轮询直到 1.5s 后才醒）；
    - 再发 /api/health：正确实现 ~0.2s 内返回（health_elapsed < 0.8 保持绿）；
      变异实现此时循环仍被扫描占住 ⇒ health ≥ 1.5s，确定性红。
    metrics_elapsed ≥ 1.4 证明慢扫描真的拖住了 /metrics 响应（钉有区分度）。
    """

    scan_started = threading.Event()

    def _slow_scan() -> int:
        scan_started.set()
        time.sleep(1.5)
        return 7

    monkeypatch.setattr(metrics_mod, "_heap_gc_bytes", _slow_scan)

    server, serve_task, port = await _start_server(tmp_path, monkeypatch)
    try:
        started = time.monotonic()
        metrics_task = asyncio.create_task(_get_raw(port, "/metrics"))
        while not scan_started.is_set():
            await asyncio.sleep(0.05)
        assert time.monotonic() - started < 5.0, "扫描 5s 内没开始（服务器没接住请求）"
        health_status, _h, _t = await _get_raw(port, "/api/health")
        health_elapsed = time.monotonic() - started
        metrics_status, _h, _t = await metrics_task
        metrics_elapsed = time.monotonic() - started
    finally:
        await _shutdown(server, serve_task)

    assert health_status == 200
    assert metrics_status == 200
    assert metrics_elapsed >= 1.4, "慢采集应真的拖住 /metrics（否则钉无区分度）"
    assert health_elapsed < 0.8, (
        f"/api/health 在扫描开始后 {health_elapsed:.2f}s 才返回——事件循环被慢采集阻塞了"
    )
