"""Provider failure classification 的 model 层 owner（T03/#239）——参数化 golden。

审计 finding：Agent Core 直接持有 vendor/protocol failure 字符串（`agent/runtime.py`
的 `_PROVIDER_FAILURE_MARKERS` 与 DSML wire marker），新增/调整 provider 错误语义要改
Runtime。本模块锁三件事：

1. **分层（AC1）**：标记表、reason 常量、固定文案、DSML 判定都只住在
   `agent_harness.model.failure`；`agent/runtime.py` 既不再定义这些名字，源码里也不再
   出现任何 vendor 标记字面量（机械可验，防止"挪走又抄回来"）。
2. **分类矩阵（AC2）**：标记 → reason 逐项一致（顺序即优先级）、大小写不敏感、
   未命中返回 None；文案表键集与 reason 集**相等**（缺键是运行期 KeyError、多键是漂移）。
   这是**单元级** golden——整链路 golden 在 `tests/agent/test_runtime_failure_paths.py`
   （run/run_stream 两条路径的事件载荷）与 `tests/agent/test_model_fallback_runtime.py`
   （DSML 泄漏与 fallback 序列）。
3. **DSML 判定（AC3）**：真实泄漏标记 ⇒ 命中；讨论性质的文本（含 "DSML" 字样但没有
   全角协议保留标记）⇒ 不命中。

行为口径的完整叙述在 `docs/adr/0033-run-failure-attribution-surface.md` §2.1；
本文件只做逐项钉死。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.agent import runtime as runtime_module
from agent_harness.model import failure as failure_module
from agent_harness.model.failure import (
    _PROVIDER_FAILURE_MARKERS,
    CONTENT_MODERATION_REASON,
    PROVIDER_ACCOUNT_UNAVAILABLE_REASON,
    PROVIDER_AUTH_REASON,
    PROVIDER_FAILURE_MESSAGES,
    PROVIDER_MODEL_NOT_FOUND_REASON,
    classify_provider_failure,
    has_malformed_tool_call_markup,
)

#: 分类表 golden：**逐字重抄一遍**（含顺序）。改表必须显式改这里——顺序即优先级，
#: 而这张表是跨仓契约 §4 里 reason 取值的来源（前端按 reason 呈现失败归因）。
_MARKER_GOLDEN: tuple[tuple[str, str], ...] = (
    ("data_inspection_failed", CONTENT_MODERATION_REASON),
    ("billing", PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    ("insufficient_quota", PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    ("quota_exceeded", PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    ("account_deactivated", PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    ("account_suspended", PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    ("invalid_api_key", PROVIDER_AUTH_REASON),
    ("incorrect_api_key", PROVIDER_AUTH_REASON),
    ("invalid_organization", PROVIDER_AUTH_REASON),
    ("model_not_found", PROVIDER_MODEL_NOT_FOUND_REASON),
)


def test_marker_table_matches_golden() -> None:
    """表逐项（含顺序）等于 golden。"""
    assert _PROVIDER_FAILURE_MARKERS == _MARKER_GOLDEN


@pytest.mark.parametrize(("marker", "reason"), _MARKER_GOLDEN)
def test_marker_maps_to_reason(marker: str, reason: str) -> None:
    """每个标记在真实错误体形状里命中它自己的 reason。"""
    error = RuntimeError(f"Error code: 400 - {{'code': '{marker}'}}")
    assert classify_provider_failure(error) == reason


def test_matching_is_case_insensitive() -> None:
    """``str(error)`` 的大小写由供应商决定，不是契约。"""
    error = RuntimeError("Error code: 401 - {'code': 'INVALID_API_KEY'}")
    assert classify_provider_failure(error) == PROVIDER_AUTH_REASON


def test_first_marker_wins() -> None:
    """同一条错误文本命中多个标记 ⇒ 表里靠前的赢（顺序即优先级）。

    载荷里 `data_inspection_failed` 在表首、`invalid_api_key` 靠后：先赢的必须是前者。
    反过来写也一样（换个顺序的载荷判红说明这里真的在看顺序，不是恒真）。
    """
    both = RuntimeError(
        "Error code: 400 - {'code': 'data_inspection_failed', "
        "'message': 'see invalid_api_key docs'}"
    )
    assert classify_provider_failure(both) == CONTENT_MODERATION_REASON

    reversed_order = RuntimeError(
        "Error code: 400 - {'code': 'invalid_api_key', "
        "'message': 'see data_inspection_failed docs'}"
    )
    assert classify_provider_failure(reversed_order) == CONTENT_MODERATION_REASON


def test_unmatched_payload_returns_none() -> None:
    """未命中保持原行为（只带异常类型名）：限流是临时态、属 fallback 责任域，
    不做可读文案（错误码 `rate_limit_exceeded` 故意不在表里）。"""
    error = RuntimeError(
        "Error code: 429 - {'code': 'rate_limit_exceeded', 'message': "
        "'Rate limit reached for gpt-4o in organization org-x on tokens per min.'}"
    )
    assert classify_provider_failure(error) is None


def test_message_table_key_set_equals_reason_set() -> None:
    """分类表命中却缺文案 ⇒ 运行期取文案 KeyError，会被失败路径的兜底 except 吞掉、
    连 run/failed 一起丢。两个集合必须**相等**：缺键是崩溃，多出的键是漂移
    （说明有分类被删而文案留下）。"""
    assert {reason for _, reason in _PROVIDER_FAILURE_MARKERS} == set(
        PROVIDER_FAILURE_MESSAGES
    )


#: DSML 泄漏的正样本：真机冒烟实测（session 7afd328a）的碎片流形状。
_DSML_LEAK_CONTENT = (
    "\n\n<｜DSML｜tool_calls</parameter>\n"
    '<invoke name="true">{"ok"}</invoke></p></p></'
)


@pytest.mark.parametrize(
    "content",
    [
        _DSML_LEAK_CONTENT,
        "<｜DSML｜",
        "前缀<｜DSML｜tool_calls",
    ],
)
def test_malformed_markup_is_detected(content: str) -> None:
    assert has_malformed_tool_call_markup(content) is True


@pytest.mark.parametrize(
    "content",
    [
        "DSML 是 deepseek 的工具调用协议",
        "讨论：模型吐出的 <DSML> 标记应当被网关解析成结构化 tool_calls",
        # 半角竖线不是协议保留标记（全角 ｜ 是判据，杜绝误伤正常讨论文本）。
        "<|DSML|tool_calls>",
        "",
    ],
)
def test_normal_text_mentioning_markup_passes(content: str) -> None:
    assert has_malformed_tool_call_markup(content) is False


# ---- 分层闸门（AC1）----


def test_vendor_marker_table_lives_in_model_layer() -> None:
    """表与 DSML wire marker 只住在 model 层：Runtime 不再定义、也不再暴露它们。

    断言"runtime 命名空间里取不到"而不是"没赋值"——`from ... import` 形式的再导出
    同样会让 Runtime 继续持有 vendor 词汇，那正是本票要消除的耦合。
    """
    assert _PROVIDER_FAILURE_MARKERS is failure_module._PROVIDER_FAILURE_MARKERS
    assert not hasattr(runtime_module, "_PROVIDER_FAILURE_MARKERS")
    assert not hasattr(runtime_module, "_DSML_MARKUP_MARKER")


def test_runtime_source_carries_no_vendor_literals() -> None:
    """Runtime 源码里不出现任何 vendor 标记字面量（含注释）——防止"挪走又抄回来"。

    判据取自 golden 表而不是另抄一份清单：表变了这里跟着变。
    """
    source = Path(runtime_module.__file__).read_text(encoding="utf-8")
    leaked = [marker for marker, _ in _MARKER_GOLDEN if marker in source]
    assert not leaked, f"vendor 标记不得留在 Runtime：{leaked}"
