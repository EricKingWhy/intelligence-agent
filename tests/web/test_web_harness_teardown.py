"""#1534 AC2：共享起服务 helper 的 teardown 路径必须真的回收资源。

看门狗 `os._exit(1)` 的进程级回收不属本票（#1702）；这里只钉**调用方 teardown 路径**：
用例体断言失败 / 抛错时，`try/finally` 里的 `shutdown_test_server` 必须让 serve_task
真正结束、端口可再绑定——否则失败用例会把服务器留在后台，后续用例看着像自己在挂。

三条路径各一条用例：断言失败、调用方自己被取消、拆服务过程中被取消（覆盖
`shutdown_test_server` 内 `except asyncio.CancelledError` 分支）。
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from tests.web import _harness as harness


def _free_port(port: int) -> bool:
    """端口可再绑 ⇒ 监听 socket 已关。

    `SO_REUSEADDR`：TIME_WAIT 残留不应被读成"监听器泄漏"。
    """
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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


@pytest.mark.asyncio
async def test_shutdown_cancelled_midway_still_reaps_serve_task(
    tmp_path, monkeypatch,
):
    """拆服务自身被取消（外层 task.cancel()）⇒ 取消外传，serve_task 仍被收掉。

    这条覆盖 `shutdown_test_server` 的 `except asyncio.CancelledError` 分支：
    只有取消**发生在 shutdown 内部**时才会走到，用例体自觉 `raise` 不会。
    """
    server, serve_task, port, _app = await harness.start_test_server(
        tmp_path, monkeypatch,
    )
    # 钉住"还在优雅停的 grace 窗口里"，让取消必然落在 shutdown 内部。
    # 用不结束的假 serve_task 不可行（要测真实 socket 回收），改成把 grace 拉长：
    # 无在途连接时优雅停 ~0.2s，grace=5 保证 cancel 一定在 wait_for 里发生。
    shutdown_task = asyncio.create_task(
        harness.shutdown_test_server(server, serve_task, grace=5.0)
    )
    await asyncio.sleep(0.01)
    shutdown_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown_task

    assert serve_task.done(), "拆服务被取消时也必须收掉 serve_task"
    assert _free_port(port), f"端口 {port} 未释放"
