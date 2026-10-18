"""#1353 markdown 记忆移植测试的共享适配层（不被 pytest 收集）。

票面 §2/§3 把权威存储固定到 `agent_harness.memory.v2.markdown_store`，但没有
锁死类名与构造参数形态（“锁文件名等实现细节以本票 §2–§3 为准，不以 Hermes
源码字面为准”）。本文件只做两件事：

1. 按票面路径找到实现模块；
2. 用几种自然构造形态建一个**根目录确实在 tmp_path 下**的实例。

它不实现任何记忆语义，也不替被测代码做断言。构造形态不匹配时直接
``pytest.fail`` 并列出尝试过的形态，避免测试静默写到真实 home。
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

DELIMITER = "\n§\n"

_MODULE_CANDIDATES = (
    "agent_harness.memory.v2.markdown_store",
    "agent_harness.memory.markdown_store",
)

_THREAT_MODULE_CANDIDATES = (
    "agent_harness.memory.v2.threat_patterns",
    "agent_harness.memory.threat_patterns",
    "agent_harness.tooling.threat_patterns",
    "agent_harness.tools.threat_patterns",
)

_CLASS_CANDIDATES = (
    "MarkdownMemoryStore",
    "MarkdownStore",
    "MemoryStore",
    "CuratedMemoryStore",
)


def _import_first(candidates: tuple[str, ...], *, purpose: str) -> Any:
    failures: list[str] = []
    for name in candidates:
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            if name == missing or name.startswith(missing + "."):
                failures.append(name)
                continue
            raise
    pytest.fail(
        f"#1353 未找到{purpose}实现模块；已尝试: {', '.join(failures) or candidates}",
        pytrace=False,
    )


def store_module() -> Any:
    return _import_first(_MODULE_CANDIDATES, purpose="markdown 存储")


def threat_module() -> Any:
    return _import_first(_THREAT_MODULE_CANDIDATES, purpose="threat_patterns")


def store_class() -> type:
    module = store_module()
    for name in _CLASS_CANDIDATES:
        candidate = getattr(module, name, None)
        if isinstance(candidate, type):
            return candidate
    pytest.fail(
        f"#1353 `{module.__name__}` 未暴露存储类；已尝试: {', '.join(_CLASS_CANDIDATES)}",
        pytrace=False,
    )


def _path_of(store: Any, target: str) -> Path | None:
    for method_name in ("_path_for", "path_for", "path"):
        method = getattr(store, method_name, None)
        if callable(method):
            try:
                return Path(method(target))
            except (AttributeError, KeyError, OSError, TypeError, ValueError):
                continue
    attr = getattr(store, "memory_path" if target == "memory" else "user_path", None)
    if attr is not None:
        return Path(attr)
    return None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def build_store(
    tmp_path: Path,
    *,
    memory_char_limit: int | None = None,
    user_char_limit: int | None = None,
) -> Any:
    """构造一个根目录落在 ``tmp_path`` 下的存储实例。"""
    cls = store_class()
    kwargs: dict[str, int] = {}
    if memory_char_limit is not None:
        kwargs["memory_char_limit"] = memory_char_limit
    if user_char_limit is not None:
        kwargs["user_char_limit"] = user_char_limit

    attempts = (
        ("positional", lambda: cls(tmp_path, **kwargs)),
        ("memory_dir=", lambda: cls(memory_dir=tmp_path, **kwargs)),
        ("memories_dir=", lambda: cls(memories_dir=tmp_path, **kwargs)),
        ("workspace_dir=", lambda: cls(workspace_dir=tmp_path, **kwargs)),
        ("root=", lambda: cls(root=tmp_path, **kwargs)),
        ("base_dir=", lambda: cls(base_dir=tmp_path, **kwargs)),
    )
    errors: list[str] = []
    for label, factory in attempts:
        try:
            store = factory()
        except TypeError as exc:
            errors.append(f"{label}: {exc}")
            continue
        memory_path = _path_of(store, "memory")
        if memory_path is None or not _is_under(memory_path, tmp_path):
            errors.append(f"{label}: 解析出的 MEMORY.md 不在 tmp_path 下 ({memory_path})")
            continue
        return store

    pytest.fail(
        "#1353 无法用票面契约构造 markdown 存储（防止写到真实 home）；尝试记录：\n"
        + "\n".join(errors),
        pytrace=False,
    )


async def call(method: Any, *args: Any, **kwargs: Any) -> Any:
    """调用同步或 async 存储方法，统一返回结果。"""
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def load(store: Any) -> Any:
    """调用可选的 ``load_from_disk``（同步或 async 均可，返回 awaitable 时由调用方 await）。"""
    method = getattr(store, "load_from_disk", None)
    if method is None:
        return None
    return method()


def path_of(store: Any, target: str) -> Path:
    path = _path_of(store, target)
    if path is None:
        pytest.fail(f"#1353 存储未暴露 {target} 文件路径", pytrace=False)
    return path


def read_raw(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def read_entries(path: Path) -> list[str]:
    raw = read_raw(path)
    return [entry.strip() for entry in raw.split(DELIMITER) if entry.strip()]
