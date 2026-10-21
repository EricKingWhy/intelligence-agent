"""记忆写入来源标记（ContextVar）——M5 后台 review 与 M6 写审批门共用。

来源决定两件事：`add` 的软校验是否生效（后台 review 提炼的内容不可能逐字出现在
某一条 user message 里），以及 `evaluate_gate` 读到的分流依据。默认 ``foreground``
（模型主动调用），所以没打标的调用方行为不变。

打标点（#1438 接线，均为成对 ``set_origin`` / ``reset_origin``）：

- 前台 ``memory`` 工具：`memory_tool.MemoryTool._gate`；
- 后台 review job：`executor.MemoryJobExecutor._apply` 的写入事务；
- 垫片：`tools.RememberMemoryV2Tool` / `ForgetMemoryV2Tool` 的显式写入。

**没有** ``approval_replay`` 这个值（#1438 §4.1 兜底，2026-10-21 用户裁决后删除）：
审批重放没有可落标的地方——`MemoryRecordV2` 无 origin 字段，`memory_v2_staged` 加列
会改动 `StagedWriteStore` 的公开面（AC7 白名单钉住），而 `evaluate_gate` 在重放段
根本不被调用（`approve()` 直接走 `markdown` 重放）。留一个无写入点、无消费方的常量
就是死代码，正是本票要清掉的那一类。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

FOREGROUND = "foreground"
BACKGROUND_REVIEW = "background_review"

ORIGINS = frozenset({FOREGROUND, BACKGROUND_REVIEW})

_origin: ContextVar[str] = ContextVar("memory_write_origin", default=FOREGROUND)


def current_origin() -> str:
    return _origin.get()


def set_origin(origin: str) -> Token[str]:
    """设置来源；返回的 token 必须交给 `reset_origin`（asyncio task 内成对使用）。"""
    if origin not in ORIGINS:
        raise ValueError(f"unknown memory write origin {origin!r}")
    return _origin.set(origin)


def reset_origin(token: Token[str]) -> None:
    _origin.reset(token)


__all__ = [
    "BACKGROUND_REVIEW",
    "FOREGROUND",
    "ORIGINS",
    "current_origin",
    "reset_origin",
    "set_origin",
]
