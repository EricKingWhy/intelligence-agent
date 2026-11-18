"""#1534 AC2：共享起服务 helper 的 teardown 路径必须真的回收资源。

看门狗 `os._exit(1)` 的进程级回收不属本票（#1702）；这里只钉**调用方 teardown 路径**：
用例体断言失败 / 抛错时，`try/finally` 里的 `shutdown_test_server` 必须让 serve_task
真正结束、端口可再绑定——否则失败用例会把服务器留在后台，后续用例看着像自己在挂。
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from tests.web import _harness as harness


def _free_port(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


@pytest.mark.asyncio
async def test_assertion_failure_still_reaps_server(tmp_path, monkeypatch):
    """用例体断言失败 ⇒ finally 仍拆服务（serve_task 结束、端口可再绑）。"""
    server, serve_task, port, _app = await harness.start_test_server(
        tmp_path, monkeypatch,
    )
    try:
        assert not serve_task.done()
        raise AssertionError("模拟用例体断言失败")
    except AssertionError:
        pass
    finally:
        await harness.shutdown_test_server(server, serve_task)

    assert serve_task.done(), "失败路径必须让 serve_task 结束，不能留孤儿服务"
    assert _free_port(port), f"端口 {port} 未释放"


@pytest.mark.asyncio
async def test_cancellation_still_reaps_server(tmp_path, monkeypatch):
    """用例体被取消（CancelledError）⇒ finally 仍拆服务。"""
    server, serve_task, port, _app = await harness.start_test_server(
        tmp_path, monkeypatch,
    )
    try:
        raise asyncio.CancelledError()
    except asyncio.CancelledError:
        pass
    finally:
        await harness.shutdown_test_server(server, serve_task)

    assert serve_task.done()
    assert _free_port(port)
