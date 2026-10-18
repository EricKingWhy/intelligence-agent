"""记忆写入来源标记（ContextVar）——M5 后台 review 与 M6 审批重放共用。

来源决定两件事：`add` 的软校验是否生效（后台 review 提炼的内容不可能逐字出现在
某一条 user message 里），以及 M6 的 gate 走 stage 还是行内审批。默认 ``foreground``
（模型主动调用），所以没打标的调用方行为不变。
"""

from __future__ import annotations

from contextvars import ContextVar, Token

FOREGROUND = "foreground"
BACKGROUND_REVIEW = "background_review"
APPROVAL_REPLAY = "approval_replay"

_ORIGINS = frozenset({FOREGROUND, BACKGROUND_REVIEW, APPROVAL_REPLAY})

_origin: ContextVar[str] = ContextVar("memory_write_origin", default=FOREGROUND)


def current_origin() -> str:
    return _origin.get()


def set_origin(origin: str) -> Token[str]:
    """设置来源；返回的 token 必须交给 `reset_origin`（asyncio task 内成对使用）。"""
    if origin not in _ORIGINS:
        raise ValueError(f"unknown memory write origin {origin!r}")
    return _origin.set(origin)


def reset_origin(token: Token[str]) -> None:
    _origin.reset(token)


__all__ = [
    "APPROVAL_REPLAY",
    "BACKGROUND_REVIEW",
    "FOREGROUND",
    "current_origin",
    "reset_origin",
    "set_origin",
]
