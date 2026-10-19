"""#1353 markdown 记忆移植测试的共享层（不被 pytest 收集）。

票面 §2/§3 已把权威存储钉死到 `agent_harness.memory.v2.markdown_store.MarkdownMemoryStore`
——模块路径、类名、构造签名全部固定。本文件按真实符号直连，不做任何命名/位置探测
（旧版的多候选适配逻辑会"让实现怎么写错位置都能过"，已删）。

只做三件事：

1. 按真实签名构造实例，并守住"根目录必须在 tmp_path 下"的安全判据；
2. 把 store 的同步/async 方法调用统一成 awaitable 桥；
3. 提供条目解析等文件断言的小工具。

它不实现任何记忆语义，也不替被测代码做断言。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from agent_harness.memory.v2.markdown_store import ENTRY_DELIMITER, MarkdownMemoryStore


def build_store(
    tmp_path: Path,
    *,
    memory_char_limit: int | None = None,
    user_char_limit: int | None = None,
) -> MarkdownMemoryStore:
    """按真实签名 ``MarkdownMemoryStore(memory_dir=…)`` 构造实例。"""
    kwargs: dict[str, int] = {}
    if memory_char_limit is not None:
        kwargs["memory_char_limit"] = memory_char_limit
    if user_char_limit is not None:
        kwargs["user_char_limit"] = user_char_limit
    store = MarkdownMemoryStore(memory_dir=tmp_path, **kwargs)

    memory_path = store.path_for("memory")
    try:
        under = memory_path.resolve().relative_to(tmp_path.resolve())
    except (OSError, ValueError):
        under = None
    if under is None:
        pytest.fail(
            f"#1353 MEMORY.md 根目录不在 tmp_path 下（防止写到真实 home）：{store.path_for('memory')}",
            pytrace=False,
        )
    return store


async def call(method: Any, *args: Any, **kwargs: Any) -> Any:
    """调用同步或 async 存储方法，统一返回结果。"""
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def load(store: MarkdownMemoryStore) -> Any:
    """调用 ``load_from_disk``（同步方法；返回值形态保留以兼容既有调用方）。"""
    return store.load_from_disk()


def path_of(store: MarkdownMemoryStore, target: str) -> Path:
    return store.path_for(target)


def read_raw(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_entries(path: Path) -> list[str]:
    raw = read_raw(path)
    return [entry.strip() for entry in raw.split(ENTRY_DELIMITER) if entry.strip()]
