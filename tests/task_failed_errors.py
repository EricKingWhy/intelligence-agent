"""共享测试 helper：提取 task_failed 诊断日志承载的 error 原文。

OBS-008：错误文案只进诊断日志（事件侧只有类型名），因此对文案做钉住的测试
都要从 caplog 记录里提取同一段过滤逻辑。放平铺共享模块而不进 conftest：pytest
官方口径是从 conftest 导入属不良实践（writing_plugins——"never import anything
from a conftest.py file"）；本仓先例是 tests/ 根下平铺共享模块
（scripted_model / live_model_guard / langmem_doubles）。
"""

from __future__ import annotations

import pytest


def task_failed_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    """提取 task_failed 诊断日志承载的 error 原文（OBS-008：文案只进日志）。"""
    return [
        str(getattr(r, "error", ""))
        for r in caplog.records
        if r.name == "agent_harness.agent" and getattr(r, "event_type", "") == "task_failed"
    ]
