"""Prompt Registry：集中登记 prompt section，按 scope 组装。

`builtin.py`（内置 section 正文）由 T3 加入；`DEFAULT_REGISTRY` 同理。
"""

from __future__ import annotations

from agent_harness.prompt.errors import PromptError
from agent_harness.prompt.registry import (
    AssembledPrompt,
    PromptRegistry,
    run_self_check,
)
from agent_harness.prompt.section import SECTION_ORDERS, PromptSection, Target
from agent_harness.prompt.template import extract_variables, render

__all__ = [
    "SECTION_ORDERS",
    "AssembledPrompt",
    "PromptError",
    "PromptRegistry",
    "PromptSection",
    "Target",
    "extract_variables",
    "render",
    "run_self_check",
]
