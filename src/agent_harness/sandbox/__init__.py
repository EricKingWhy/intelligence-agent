"""Sandbox：Coding Tool 的隔离执行环境。

对外暴露抽象契约（Sandbox + ExecResult）和具体后端。
其他实现细节不对外导出。
"""

from agent_harness.sandbox.base import ExecResult, Sandbox
from agent_harness.sandbox.local import LocalSubprocessSandbox

__all__ = [
    "ExecResult",
    "LocalSubprocessSandbox",
    "Sandbox",
]
