"""#1353 markdown 记忆移植测试的共享层（不被 pytest 收集）。

票面 §2/§3 已把权威存储钉死到 `agent_harness.memory.v2.markdown_store.MarkdownMemoryStore`
——模块路径、类名、构造签名全部固定。本文件按真实符号直连，不做任何命名/位置探测
（旧版的多候选适配逻辑会"让实现怎么写错位置都能过"，已删）。

只做三件事：

1. 按真实签名构造实例，并守住"根目录必须在 tmp_path 下"的安全判据；
2. 把 store 的同步/async 方法调用统一成 awaitable 桥；
3. 提供条目解析等文件断言的小工具、加载好的 store（`loaded_store`）与
   memory Tool（`build_tool`）。

它不实现任何记忆语义，也不替被测代码做断言。
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from agent_harness.memory.v2.commit import MemoryCommit, build_markdown_commit
from agent_harness.memory.v2.markdown_store import ENTRY_DELIMITER, MarkdownMemoryStore
from agent_harness.memory.v2.memory_tool import MemoryTool


class AuthorityHarness:
    """#1494 测试适配层：读走引擎、写走唯一 seam（`MemoryCommit`）。

    生产代码里引擎已经没有 add/remove/replace；旧测试的写法是 `store.add(...)` +
    `store.entries(...)`。这个适配层把前者转到 commit、后者原样委托给引擎，
    测试判据不变，但写路径不再绕过 seam。
    """

    def __init__(self, engine: MarkdownMemoryStore, commit: MemoryCommit) -> None:
        self.engine = engine
        self.commit = commit

    def __getattr__(self, name: str):
        return getattr(self.engine, name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in ("engine", "commit"):
            object.__setattr__(self, name, value)
            return
        setattr(self.engine, name, value)

    async def add(self, target: str, content: str, **kwargs):
        return await self.commit.add(target, content, **kwargs)

    async def replace(self, target: str, old_text: str, content: str, **kwargs):
        return await self.commit.replace(target, old_text, content, **kwargs)

    async def remove(self, target: str, old_text: str, **kwargs):
        return await self.commit.remove(target, old_text, **kwargs)

    async def apply_batch(self, target: str, operations, **kwargs):
        return await self.commit.apply_batch(target, operations, **kwargs)

    async def resolve_entry(self, target: str, old_text: str, verb: str):
        return await self.commit.resolve_entry(target, old_text, verb)

    async def resolve_batch_entries(self, target: str, operations):
        return await self.commit.resolve_batch_entries(target, operations)

    async def remove_by_digest(self, digests, **kwargs):
        return await self.commit.remove_by_digest(digests, **kwargs)


def build_store(
    tmp_path: Path,
    *,
    memory_char_limit: int | None = None,
    user_char_limit: int | None = None,
) -> AuthorityHarness:
    """按真实签名构造引擎 + commit（写路径与生产同一个 seam）。"""
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
    return AuthorityHarness(store, build_markdown_commit(store, tmp_path))


async def call(method: Any, *args: Any, **kwargs: Any) -> Any:
    """调用同步或 async 存储方法，统一返回结果。"""
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def load(store: MarkdownMemoryStore) -> Any:
    """调用 ``load_from_disk``（同步方法；返回值形态保留以兼容既有调用方）。"""
    return store.load_from_disk()


async def loaded_store(tmp_path: Path) -> AuthorityHarness:
    """构造 store 并完成首次 load（各用例都要的那段样板，只此一份）。"""
    store = build_store(tmp_path)
    pending = load(store)
    if inspect.isawaitable(pending):
        await pending
    return store


def build_tool(store: Any) -> Any:
    """按真实符号构造 memory Tool（#1494：工具拿 commit，不是引擎）。"""
    commit = store.commit if isinstance(store, AuthorityHarness) else store
    return MemoryTool(commit)


def path_of(store: MarkdownMemoryStore, target: str) -> Path:
    return store.path_for(target)


def read_raw(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_entries(path: Path) -> list[str]:
    raw = read_raw(path)
    return [entry.strip() for entry in raw.split(ENTRY_DELIMITER) if entry.strip()]
