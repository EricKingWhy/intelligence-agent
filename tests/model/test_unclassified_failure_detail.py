"""未分类 provider 失败的结构化细节（#996）：HTTP 状态码 + 故障类别，**不回显**。

#222 起未分类失败的 run/failed 只有 ``运行失败（{error_type}），未分类异常；…``。
真机（2026-10-11 integration gate）三条失败分别是 ``APIConnectionError`` /
``BadRequestError`` / ``APIError``，事件里分不清「连不上 / 超时 / 被拒（4xx）/
网关 5xx」，也看不到状态码。本文件钉三件事：

1. **状态码**：异常带 HTTP status（openai ``APIStatusError.status_code``、
   ``httpx.HTTPStatusError.response.status_code``）⇒ :func:`provider_http_status`
   给出整数，文案带 ``HTTP <code>``；没有状态码 ⇒ ``None``、文案逐字是 #222 的兜底。
2. **类别**：连接失败（``APIConnectionError`` 且非超时）/ 超时（``APITimeoutError``，
   它是 ``APIConnectionError`` 的子类 ⇒ 必须先判）/ 请求被拒（4xx）/ 供应商服务端错误
   （5xx）各一句**固定**文案。异常用真实 openai SDK 类构造（不是同名替身）。
3. **脱敏（ADR-0033 §2.1/§2.4，OBS-008）**：文案只由项目常量 + 异常类名 + 整数状态码
   拼成，``str(error)`` / 错误体 / 响应正文里植入的假凭证与回显文本一个字都不得出现。

整链路（run/failed 载荷的 ``http_status`` 键、run/run_stream 两条入口）在
``tests/agent/test_runtime_failure_paths.py`` 的 ``TestUnclassifiedFailureDetail``。
"""

from __future__ import annotations

import httpx
import openai
import pytest

from agent_harness.model.failure import (
    UNCLASSIFIED_FAILURE_MESSAGE,
    provider_http_status,
    unclassified_failure_message,
)

#: 植入的"假凭证 / 回显文本"：真实供应商错误体会回显请求内容与 key 前缀（ADR-0033 §1.2）。
PLANTED_SECRETS: tuple[str, ...] = (
    "sk-PLANTED-0123456789abcdef",
    "PLANTED_PROMPT_ECHO",
    "planted-org-zz9",
)
_ECHO = " ".join(PLANTED_SECRETS)
_REQUEST = httpx.Request("POST", "https://gateway.invalid/v1/chat/completions")


def _status_error(cls: type[openai.APIStatusError], code: int) -> openai.APIStatusError:
    response = httpx.Response(code, request=_REQUEST, text=f'{{"error": "{_ECHO}"}}')
    return cls(
        f"Error code: {code} - {{'message': '{_ECHO}'}}",
        response=response,
        body={"message": _ECHO, "code": "planted_code"},
    )


def _assert_no_echo(text: str) -> None:
    for token in (*PLANTED_SECRETS, "planted_code", "gateway.invalid"):
        assert token not in text, f"回显 / 凭证片段 {token!r} 不得进入文案：{text!r}"


@pytest.mark.parametrize(
    ("error", "expected_status", "category_phrase"),
    [
        (_status_error(openai.BadRequestError, 400), 400, "模型供应商拒绝了请求"),
        (_status_error(openai.AuthenticationError, 401), 401, "模型供应商拒绝了请求"),
        (_status_error(openai.RateLimitError, 429), 429, "模型供应商拒绝了请求"),
        (_status_error(openai.InternalServerError, 500), 500, "模型供应商服务端错误"),
        (_status_error(openai.APIStatusError, 502), 502, "模型供应商服务端错误"),
    ],
    ids=["400", "401", "429", "500", "502"],
)
def test_status_error_carries_http_status_and_category(
    error: BaseException, expected_status: int, category_phrase: str,
) -> None:
    assert provider_http_status(error) == expected_status
    message = unclassified_failure_message(error)
    assert f"HTTP {expected_status}" in message
    assert category_phrase in message
    assert type(error).__name__ in message
    _assert_no_echo(message)


def test_httpx_status_error_carries_http_status() -> None:
    """``httpx.HTTPStatusError`` 的状态码在 ``response`` 上（与 ``fallback.py`` 同口径）。"""
    response = httpx.Response(503, request=_REQUEST, text=_ECHO)
    error = httpx.HTTPStatusError(f"Server error '503' {_ECHO}", request=_REQUEST, response=response)
    assert provider_http_status(error) == 503
    message = unclassified_failure_message(error)
    assert "HTTP 503" in message and "模型供应商服务端错误" in message
    _assert_no_echo(message)


def test_connection_error_is_connection_category_without_status() -> None:
    error = openai.APIConnectionError(message=f"Connection error. {_ECHO}", request=_REQUEST)
    assert provider_http_status(error) is None
    message = unclassified_failure_message(error)
    assert "无法连接模型供应商" in message
    assert "超时" not in message
    assert "HTTP" not in message
    assert "APIConnectionError" in message
    _assert_no_echo(message)


def test_timeout_is_not_reported_as_connection_failure() -> None:
    """``APITimeoutError`` 是 ``APIConnectionError`` 的子类：先判超时，否则会被误报成连不上。"""
    error = openai.APITimeoutError(request=_REQUEST)
    assert isinstance(error, openai.APIConnectionError)  # 仪器自检：子类关系真实存在
    assert provider_http_status(error) is None
    message = unclassified_failure_message(error)
    assert "模型供应商请求超时" in message
    assert "无法连接" not in message
    assert "APITimeoutError" in message


def test_connection_subclass_by_name_is_recognised() -> None:
    """按 MRO 类名识别（不硬 import openai，与 ``fallback._TRANSIENT_ERROR_NAMES`` 同口径）：
    项目外的子类（例如 SDK 包装层）同样归入连接失败。"""

    class WrappedConnectionError(openai.APIConnectionError):
        pass

    error = WrappedConnectionError(request=_REQUEST)
    assert "无法连接模型供应商" in unclassified_failure_message(error)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(f"boom {_ECHO}"),
        openai.APIError(f"stream error {_ECHO}", request=_REQUEST, body={"m": _ECHO}),
        ValueError(_ECHO),
    ],
    ids=["RuntimeError", "openai.APIError", "ValueError"],
)
def test_no_status_no_known_category_keeps_222_fallback_verbatim(error: BaseException) -> None:
    """无状态码、非连接 / 超时类：文案逐字等于 #222 的兜底（既有口径不变）。"""
    assert provider_http_status(error) is None
    assert unclassified_failure_message(error) == UNCLASSIFIED_FAILURE_MESSAGE.format(
        error_type=type(error).__name__,
    )


class _OddStatus(Exception):
    def __init__(self, status_code: object) -> None:
        super().__init__(_ECHO)
        self.status_code = status_code


@pytest.mark.parametrize(
    "status_code", [True, "400", 400.0, None, 99, 600, -1],
    ids=["bool", "str", "float", "none", "99", "600", "neg"],
)
def test_non_http_status_values_are_ignored(status_code: object) -> None:
    """``status_code`` 只认 100–599 的 int（bool 是 int 子类，靠范围排除）：形状不对就当没有，
    不把任意属性值（可能是供应商给的字符串）拼进文案。"""
    error = _OddStatus(status_code)
    assert provider_http_status(error) is None
    assert unclassified_failure_message(error) == UNCLASSIFIED_FAILURE_MESSAGE.format(
        error_type="_OddStatus",
    )


@pytest.mark.parametrize("code", [301, 418, 451, 599])
def test_other_valid_status_codes_keep_status_in_message(code: int) -> None:
    """1xx–3xx 走「带状态码的未分类」句；4xx / 5xx 边界值进各自类别。"""
    error = _OddStatus(code)
    message = unclassified_failure_message(error)
    assert f"HTTP {code}" in message
    if 400 <= code < 500:
        assert "模型供应商拒绝了请求" in message
    elif code >= 500:
        assert "模型供应商服务端错误" in message
    else:
        assert "未分类异常" in message
    _assert_no_echo(message)
