"""`tests/live_model_guard.py` 的判据闸门（离线，零网络）。

守卫的全部价值在于「**判错方向的代价不对称**」：

- 该 skip 却放行 ⇒ 17 条真实模型红回到门禁里（噪声淹没代码回归）；
- 不该 skip 却 skip ⇒ **代码回归被静默**（门禁形同虚设）。

所以本文件的重点在第二类：**未分类失败绝不构成 skip**。用例里的错误载荷取自实测
（2026-09-22 整仓 `-m integration` 的 17 项红的真实回显），不是编的字符串：

- primary：HTTP 400，body ``{'code':'billing','message':'计费账户已被冻结','ref_code':400901,'ref_scope':'common'}``；
- fallback：HTTP 429，body ``{'error':{'code':'1113','message':'余额不足或无可用资源包,请充值。'}}``
  （注意：这条**没有**命中 `model/failure.py` 的分类表——中文措辞不含表里的 ASCII 标记，
  它的归档全靠 429 这一层，正是本模块补齐环境层判据的理由）。

`probe_endpoint` / `probe_chain` / `chain_verdict` / `ensure_live_model` 这一段（真实构造
路径 + 缓存 + skip 出口）用**替换掉 `create_chat_model` 的离线替身**覆盖：探测器的调用
形状（超时、`max_tokens`）与「成功 ⇒ 放行」这条正向路径不能只靠真跑实测来保证。
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import Settings
from agent_harness.model.config import ModelConfig
from agent_harness.model.failure import (
    CONTENT_MODERATION_REASON,
    PROVIDER_ACCOUNT_UNAVAILABLE_REASON,
    PROVIDER_AUTH_REASON,
    PROVIDER_MODEL_NOT_FOUND_REASON,
)
from tests import live_model_guard as guard
from tests.live_model_guard import (
    REASON_RATE_LIMITED,
    REASON_SERVER_ERROR,
    REASON_TIMEOUT,
    REASON_UNREACHABLE,
    UNAVAILABLE_REASONS,
    EndpointProbe,
    chain_verdict,
    classify_environment_failure,
    ensure_live_model,
    probe_chain,
    probe_endpoint,
    skip_reason,
)

# ── 实测载荷：两条真实回显 ──────────────────────────────────────────────────


class _HttpError(Exception):
    """带 `status_code` 的假 SDK 异常（openai SDK 的 `APIStatusError` 形状）。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


#: primary 的真实回显（HTTP 400，计费账户冻结）。
_PRIMARY_BODY = (
    "Error code: 400 - {'code': 'billing', 'message': '计费账户已被冻结', "
    "'ref_code': 400901, 'ref_scope': 'common'}"
)
#: fallback 的真实回显（HTTP 429，配额耗尽；中文措辞不命中产品分类表）。
_FALLBACK_BODY = "Error code: 429 - {'error': {'code': '1113', 'message': '余额不足或无可用资源包,请充值。'}}"


def test_primary_real_payload_is_classified_as_account_unavailable() -> None:
    """primary 实测载荷 → 账户不可用（走产品分类表，不是本模块自己的词表）。"""
    error = _HttpError(_PRIMARY_BODY, status_code=400)
    assert classify_environment_failure(error) == PROVIDER_ACCOUNT_UNAVAILABLE_REASON


def test_fallback_real_payload_is_classified_as_rate_limited() -> None:
    """fallback 实测载荷（中文配额文案）→ 429 这一层兜住它。"""
    error = _HttpError(_FALLBACK_BODY, status_code=429)
    assert classify_environment_failure(error) == REASON_RATE_LIMITED


def test_rate_limit_is_caught_by_text_when_status_is_missing() -> None:
    """没有 `status_code` 属性时，文本里的 429 同样是判据（SDK 措辞不是契约）。"""
    assert classify_environment_failure(RuntimeError(_FALLBACK_BODY)) == REASON_RATE_LIMITED


# ── fail-closed：判不出环境 ⇒ 绝不 skip ────────────────────────────────────


@pytest.mark.parametrize("error", [
    # 4xx 但不是「端点不可用」的几类：可能是我们自己的代码发错了请求。
    _HttpError("Error code: 400 - {'error': {'code': 'invalid_request_error', "
               "'message': 'max_tokens must be positive'}}", status_code=400),
    _HttpError("Error code: 422 - {'error': {'code': 'invalid_payload'}}", status_code=422),
    # 4xx 正文里**恰好**出现环境层的词：400 已经定性为「这个请求不对」，
    # 正文措辞不足以把它翻成环境判据（否则 skip 会盖住我们自己的载荷 bug）。
    _HttpError("Error code: 400 - {'error': {'message': "
               "'unknown field connect_timeout, expected request_timeout'}}", status_code=400),
    _HttpError("Error code: 400 - retry after 429", status_code=400),
    # 完全没有可识别信息的失败。
    ValueError("出了问题"),
    KeyError("model_config"),
    RuntimeError(""),  # 空消息
    # 本侧代码的异常族：文本层对它们整体关闭（重构漏改的 TypeError 不是端点故障）。
    TypeError("create_chat_model() got an unexpected keyword argument 'request_timeout'"),
    AttributeError("'ModelConfig' object has no attribute 'model_name'"),
    AssertionError("assert 429 == 200"),
])
def test_unclassified_failures_are_not_environment_verdicts(error: BaseException) -> None:
    """未分类失败 → None（fail-closed 的输入）。**这条是守卫的安全底线。**

    400/422 这类「请求本身不对」的失败**必须**留在红里：把它当环境跳过，等于用
    skip 盖住真实 bug。
    """
    assert classify_environment_failure(error) is None


def test_content_moderation_is_not_an_availability_verdict() -> None:
    """内容审查 ⇒ 端点**可用**（答了话，只是拒了这个请求），不构成 skip 理由。"""
    error = _HttpError("Error code: 400 - {'code': 'data_inspection_failed'}", status_code=400)
    assert classify_environment_failure(error) is None
    # 反向确认这个载荷确实会被产品分类表认出来（否则本用例证明力为零）。
    from agent_harness.model.failure import classify_provider_failure

    assert classify_provider_failure(error) == CONTENT_MODERATION_REASON


# ── 环境层其余归类 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("error", "expected"), [
    (_HttpError("Error code: 500 - {'error': 'internal server error'}", status_code=500),
     REASON_SERVER_ERROR),
    (_HttpError("Error code: 503 - service unavailable", status_code=503), REASON_SERVER_ERROR),
    (_HttpError("Error code: 504 - gateway timeout", status_code=504), REASON_SERVER_ERROR),
    # 无状态码的 5xx 文本形态（SDK 把状态包进正文时）。
    (RuntimeError("Error code: 502 - bad gateway"), REASON_SERVER_ERROR),
    (RuntimeError("httpx.ConnectError: [Errno 111] Connection refused"), REASON_UNREACHABLE),
    (RuntimeError("ConnectError: getaddrinfo failed for api.example.com"), REASON_UNREACHABLE),
    (ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer"),
     REASON_UNREACHABLE),
    (_HttpError("Error code: 407 - proxy authentication required", status_code=407),
     REASON_UNREACHABLE),
    (_HttpError("Request timed out.", status_code=None), REASON_TIMEOUT),
    (_HttpError("Error code: 402 - Insufficient Balance", status_code=402),
     PROVIDER_ACCOUNT_UNAVAILABLE_REASON),
    (_HttpError("Error code: 401 - {'error': {'code': 'invalid_api_key'}}", status_code=401),
     PROVIDER_AUTH_REASON),
    (_HttpError("Error code: 404 - model_not_found", status_code=404),
     PROVIDER_MODEL_NOT_FOUND_REASON),
])
def test_environment_layer_classification(error: BaseException, expected: str) -> None:
    assert classify_environment_failure(error) == expected


def test_every_availability_reason_has_a_readable_line() -> None:
    """每个「端点不可用」reason 都能渲染成一行可读归因（不许出现空理由）。

    可读性是有断言面的：文案必须**真的渲染进 message**——只断 `reason in line` 的话，
    文案表被清空也照样绿。
    """
    from agent_harness.model.failure import PROVIDER_FAILURE_MESSAGES
    from tests.live_model_guard import _ENVIRONMENT_MESSAGES

    for reason in sorted(UNAVAILABLE_REASONS):
        probe = EndpointProbe(label="primary", provider="p", model_name="m",
                              base_url="https://example.invalid/v1", ok=False,
                              reason=reason, error_type="SomeError", detail="")
        copy = PROVIDER_FAILURE_MESSAGES.get(reason) or _ENVIRONMENT_MESSAGES.get(reason)
        assert copy, f"{reason} 没有可读文案（产品表与本模块都查不到）"
        message = probe.message()
        assert reason in message and copy in message and "SomeError" in message
        line = probe.line()
        assert reason in line and "SomeError" in line
        assert probe.unavailable and not probe.unknown


# ── skip 决策（三态 × 四条规则）─────────────────────────────────────────────


def _ok(label: str) -> EndpointProbe:
    return EndpointProbe(label=label, provider="p", model_name="m",
                         base_url="https://example.invalid/v1", ok=True)


def _unavailable(label: str, reason: str = PROVIDER_ACCOUNT_UNAVAILABLE_REASON) -> EndpointProbe:
    return EndpointProbe(label=label, provider="p", model_name="m",
                         base_url="https://example.invalid/v1", ok=False,
                         reason=reason, error_type="BadRequestError", detail="…")


def _unknown(label: str) -> EndpointProbe:
    return EndpointProbe(label=label, provider="p", model_name="m",
                         base_url="https://example.invalid/v1", ok=False,
                         reason=None, error_type="ValueError", detail="…")


def test_all_endpoints_unavailable_skips() -> None:
    """整条链都不可用 ⇒ skip（本守卫存在的唯一理由）。"""
    reason = skip_reason([_unavailable("primary"), _unavailable("fallback", REASON_RATE_LIMITED)])
    assert reason is not None
    assert "primary" in reason and "fallback" in reason
    # 理由里必须写明「这不算门禁通过」——skip 不能被读成绿。
    assert "未被验证" in reason


def test_one_usable_endpoint_releases_the_guard() -> None:
    """只要有一个端点可用 ⇒ 放行（代码回归仍然会红）。"""
    assert skip_reason([_unavailable("primary"), _ok("fallback")]) is None


def test_unknown_failure_blocks_the_skip_even_with_all_endpoints_down() -> None:
    """全链失败但有一个**未分类** ⇒ 放行（fail-closed）。"""
    assert skip_reason([_unavailable("primary"), _unknown("fallback")]) is None


def test_empty_chain_does_not_skip() -> None:
    """没配端点 = 缺配置，不是环境坏——那是用例自己的 skipif 要说的话。"""
    assert skip_reason([]) is None


def test_skip_reason_renders_every_endpoint_line() -> None:
    """两端点都不可用 ⇒ 理由里两条 `·` 行都在（逐端点归因，不是一句「环境坏了」）。"""
    reason = skip_reason([_unavailable("primary"), _unavailable("fallback", REASON_TIMEOUT)])
    assert reason is not None
    assert reason.count("  · ") == 2
    assert "provider_account_unavailable" in reason and REASON_TIMEOUT in reason


def test_skip_reason_masks_credentials_in_the_base_url() -> None:
    """base_url 里的凭据不进 skip 理由（B-33 残余①）：userinfo 与 query 两处都打码。

    理由会被 pytest 原样打印、还会以 warning 再念一遍——用户自有配置的 URL 完全可能
    写成 `https://user:pass@host/v1?api_key=…`，回显前必须先过 `_redact_base_url`。
    """
    probe = EndpointProbe(
        label="primary", provider="p", model_name="m",
        base_url="https://user:pass@api.example.com/v1?api_key=sk-live-ZZZZ9999",
        ok=False, reason=REASON_TIMEOUT, error_type="APITimeoutError", detail="",
    )

    line = probe.line()

    assert "pass" not in line and "sk-live-ZZZZ9999" not in line
    assert "user:pass@" not in line
    assert "***" in line
    # host / path 留着：那正是排查时要看的东西（脱敏不该把整行变成星号）。
    assert "api.example.com" in line and "/v1" in line


def test_skip_reason_masks_the_credential_shaped_writes_the_first_rule_missed() -> None:
    """两轴审查实测出的三种漏法各钉一条：不带冒号的 userinfo、无 scheme 的 userinfo、
    非白名单参数名的 query 值（`client_secret` / `X-Amz-Signature` 这类派生命名）。

    出处：B-35 两轴审查的 B 轴 findings（P2-3 / P2-4）——首版规则只认 `user:pass@` 与
    六个固定参数名，实测这几种写法**原样回显**。每条都断言"值不在理由里"，否则这条用例
    只剩"输出是字符串"这一层。
    """
    lines = [
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://tok1234@a.invalid/v1", ok=False,
                      reason=REASON_TIMEOUT, error_type="APITimeoutError", detail="").line(),
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="user:secret@a.invalid/v1", ok=False,
                      reason=REASON_TIMEOUT, error_type="APITimeoutError", detail="").line(),
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://a.invalid/v1?client_secret=cs-9999", ok=False,
                      reason=REASON_TIMEOUT, error_type="APITimeoutError", detail="").line(),
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://a.invalid/v1?X-Amz-Signature=sig-8888", ok=False,
                      reason=REASON_TIMEOUT, error_type="APITimeoutError", detail="").line(),
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://a.invalid/v1;token=semi-7777", ok=False,
                      reason=REASON_TIMEOUT, error_type="APITimeoutError", detail="").line(),
    ]

    for line in lines:
        assert "***" in line
    assert "tok1234" not in lines[0] and "secret" not in lines[1]
    assert "cs-9999" not in lines[2] and "sig-8888" not in lines[3]
    assert "semi-7777" not in lines[4]


def test_skip_reason_masks_userinfo_on_dotless_hosts() -> None:
    """单标签主机 / IPv6 字面量上的 userinfo **也要打码**（B-35 B 轴 P1）。

    B 轴实测的漏法：首版主机名前瞻写成"必须含点"，于是 `localhost` / `ollama` /
    `[::1]` 这几类**主机名不带点**的 URL 整段凭据原样回显。这不是纸上情形——产品
    `validate_base_url` 只校验 scheme + netloc，自建 OpenAI 兼容端点（本地 / 内网容器）
    正是这种写法，而 skip 理由会进 CI 日志与被贴进 issue 的输出。
    """
    lines = [
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="http://user:pass1@localhost:8000/v1", ok=False,
                      reason=REASON_UNREACHABLE, error_type="ConnectError", detail="").line(),
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://tok2-abc@ollama:11434/v1", ok=False,
                      reason=REASON_UNREACHABLE, error_type="ConnectError", detail="").line(),
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://u:sec3@[::1]:8000/v1", ok=False,
                      reason=REASON_UNREACHABLE, error_type="ConnectError", detail="").line(),
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://tok4@vllm-svc/v1", ok=False,
                      reason=REASON_UNREACHABLE, error_type="ConnectError", detail="").line(),
    ]

    for line in lines:
        assert "***" in line and "@" in line
    for line, leaked in zip(lines, ("pass1", "tok2-abc", "sec3", "tok4")):
        assert leaked not in line, f"凭据 {leaked!r} 漏进理由：{line}"


def test_a_dotless_host_stays_readable_after_masking() -> None:
    """反向：打码只吃 userinfo，**主机名与端口留着**（排查要看的就是它）。

    `_redact_detail` 的尾巴会吃到空白 / 引号，所以这一条同时钉住"单标签主机不被它
    连带吃掉"——B 轴 P1 的修法如果顺手把主机一起遮了，理由就失去可操作性。
    """
    probe = EndpointProbe(
        label="primary", provider="p", model_name="m",
        base_url="http://user:pass@localhost:8000/v1/chat", ok=False,
        reason=REASON_UNREACHABLE, error_type="ConnectError", detail="",
    )

    line = probe.line()

    assert "user:pass" not in line
    assert "localhost:8000" in line and "/chat" in line


def test_skip_reason_masks_key_shaped_tokens_in_the_url_and_the_detail() -> None:
    """本模块自己的"密钥形"词表**也必须作用于 base_url / detail**（B-35 B 轴 P2）。

    B 轴实测：这两个回显面原先只过 `_redact_detail` 的窄词表（`sk|pk|api_key` 等），
    于是一个 32 位 hex 或 `AKIA…` 形式的 token 能原样穿过——本模块**已经有**这套形状
    判据（`_KEY_SHAPED_TOKEN`），却只拿它扫 provider / model 名。修法是把它并入
    `_redact_base_url`（四层之一）。
    """
    lines = [
        # 32 位 hex：`_redact_detail` 的窄词表不认（没有 key 字样的上下文）
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://h.invalid/v1/0123456789abcdef0123456789abcdef",
                      ok=False, reason=REASON_TIMEOUT, error_type="APITimeoutError",
                      detail="").line(),
        # AWS 形：detail 是上游错误正文，最容易原样带回
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://a.invalid/v1", ok=False,
                      reason=PROVIDER_AUTH_REASON, error_type="AuthenticationError",
                      detail="boto3 error: AKIAIOSFODNN7EXAMPLE rejected").line(),
        # 前缀形：`hf_` 一族（首版词表只查 provider / model 名）
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://a.invalid/v1", ok=False,
                      reason=PROVIDER_AUTH_REASON, error_type="AuthenticationError",
                      detail="token hf_ABCDEFGHIJKLMNOP was rejected").line(),
    ]

    for line in lines:
        assert "***" in line
    assert "0123456789abcdef" not in lines[0]
    assert "AKIAIOSFODNN7EXAMPLE" not in lines[1]
    assert "hf_ABCDEFGHIJKLMNOP" not in lines[2]


def test_skip_reason_keeps_a_non_credential_query_readable() -> None:
    """反向：普通参数名**不许**被误伤（`?model=` 是排查时的关键信息）。"""
    probe = EndpointProbe(
        label="primary", provider="p", model_name="m",
        base_url="https://api.example.com/v1?model=deepseek-chat", ok=False,
        reason=REASON_RATE_LIMITED, error_type="RateLimitError", detail="",
    )

    assert "?model=deepseek-chat" in probe.line()


def test_skip_reason_masks_a_long_credential_entirely() -> None:
    """长凭据**整段**打码（回归钉：量词曾写成 `{1,64}` ⇒ 前 16 位明文留在理由里）。

    这条是两轴审查的 A 轴实测出来的：`{1,64}` 的上限是**脱敏自己造的漏洞**——超过 64
    字符的 token 只遮住尾部，前缀照样进 pytest 输出与 warning。断言必须检查"那段前缀
    不在输出里"，只断言 `"***" in line` 是空转（旧实现同样满足）。
    """
    token = "P" * 80  # 现实中：JWT / 长 API key / base64 段
    probe = EndpointProbe(
        label="primary", provider="p", model_name="m",
        base_url=f"https://user:{token}@a.invalid/v1", ok=False,
        reason=REASON_TIMEOUT, error_type="APITimeoutError", detail="",
    )

    line = probe.line()

    assert "PPPP" not in line, f"长凭据的前缀漏了：{line}"
    assert "***@a.invalid" in line, "打码后主机名仍可读（定位端点要看这个）"


def test_a_bare_email_in_the_text_is_masked_too_disclosed_tradeoff() -> None:
    """邮件地址**也**会被打码——这是披露过的取舍，不是 bug。

    主机名前瞻只排除 `@decorator` / `100@2026-09-22` 这类非主机形态，分不出"人名 + 域名"。
    方向是安全的（少一段可读文本 < 多一段凭据），故按现状钉住；要改成保护邮件地址，
    必须同时改 `_URL_USERINFO` 的注释与这条用例。
    """
    probe = EndpointProbe(
        label="primary", provider="p", model_name="m",
        base_url="https://a.invalid/v1", ok=False,
        reason=REASON_UNREACHABLE, error_type="ConnectError",
        detail="contact ops@a.invalid for help",
    )

    assert "***@a.invalid" in probe.line()


def test_probe_repr_does_not_echo_the_sensitive_fields() -> None:
    """`repr(probe)` 不许回显配置值 / 错误正文（断言失败与 `-l` 会自动打印它）。

    只把 `base_url` 关掉不够：`provider` / `model_name` 有人会贴错成 key，`detail` 是
    上游错误正文（凭据回显面）。这条钉住四个字段全关。
    """
    probe = EndpointProbe(
        label="primary", provider="sk-live-ABCD1234", model_name="AKIAIOSFODNN7EXAMPLE",
        base_url="https://u:p@a.invalid/v1", ok=False,
        reason=REASON_UNREACHABLE, error_type="ConnectError",
        detail="raw detail with https://u:p@a.invalid/v1",
    )

    text = repr(probe)

    for secret in ("ABCD1234", "AKIAIOSFODNN7EXAMPLE", "a.invalid", "raw detail"):
        assert secret not in text, f"repr 里漏了 {secret!r}：{text}"
    assert "primary" in text and "reason=" in text, "非敏感的定位字段要留着"


def test_skip_reason_masks_key_shaped_provider_and_model_names() -> None:
    """provider / model 名也过同一道脱敏：**有人会把 key 贴错字段**。

    这两个字段没有"形状"判据（任意字符串都合法），所以回显前的兜底只剩"密钥形 token"
    这一层。没有这条用例，`_KEY_SHAPED_TOKEN` 在 `line()` 上的两处调用是无人看守的。
    """
    probe = EndpointProbe(
        label="primary", provider="sk-live-ABCD1234", model_name="AKIAIOSFODNN7EXAMPLE",
        base_url="https://a.invalid/v1", ok=False,
        reason=PROVIDER_AUTH_REASON, error_type="AuthenticationError",
        detail="",
    )

    line = probe.line()

    assert "ABCD1234" not in line and "AKIAIOSFODNN7EXAMPLE" not in line
    assert "***" in line


def test_skip_reason_keeps_a_plain_base_url_readable() -> None:
    """没有凭据的 URL 原样保留（脱敏不许误伤：否则排查时看不出打的是哪个地址）。"""
    probe = EndpointProbe(
        label="primary", provider="p", model_name="m",
        base_url="https://api.deepseek.com/v1", ok=False,
        reason=REASON_RATE_LIMITED, error_type="RateLimitError", detail="",
    )

    assert "https://api.deepseek.com/v1" in probe.line()


def test_skip_reason_leaves_a_credential_shaped_reason_line_clean() -> None:
    """整条理由（多端点拼接）同样不带明文——单行脱敏漏了前缀拼接也过不了这条。"""
    reason = skip_reason([
        EndpointProbe(label="primary", provider="p", model_name="m",
                      base_url="https://user:pw@a.invalid/v1", ok=False,
                      reason=REASON_UNREACHABLE, error_type="ConnectError", detail=""),
        EndpointProbe(label="fallback", provider="q", model_name="n",
                      base_url="https://b.invalid/v1?token=tok-1234", ok=False,
                      reason=REASON_TIMEOUT, error_type="APITimeoutError", detail=""),
    ])

    assert reason is not None
    assert "pw@" not in reason and "tok-1234" not in reason


# ── 探测器本体（离线替身；零网络）──────────────────────────────────────────


class _FakeModel:
    """`create_chat_model` 的替身：记录调用形状，按需返回或抛错。"""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return "pong"


@pytest.fixture
def _fake_provider(monkeypatch) -> dict[str, Any]:
    """替换 `probe_endpoint` 内部 import 的 `create_chat_model`（零网络）。"""
    state: dict[str, Any] = {}

    def _factory(config: ModelConfig, **kwargs: Any) -> _FakeModel:
        state["config"] = config
        state["factory_kwargs"] = kwargs
        model = _FakeModel(state.get("error"))
        state["model"] = model
        return model

    monkeypatch.setattr("agent_harness.model.provider.create_chat_model", _factory)
    return state


def _config() -> ModelConfig:
    return ModelConfig(provider="deepseek", model_name="m", base_url="https://x.invalid/v1",
                       api_key="sk-fake", temperature=0.0)


def _settings() -> Settings:
    """显式完整的 Settings（不依赖机器上有没有 `.env`——init kwargs 优先级最高）。

    本文件的用例在**没有 `.env`** 的机器上也必须给出同样的读数：依赖环境的用例会把
    "我这里缺配置"混进"守卫判得对不对"。
    """
    return Settings(
        model_provider="deepseek", model_name="m", model_api_key="sk-fake",
        model_base_url="https://x.invalid/v1", fallback_model_provider="",
    )


def _half_config_settings() -> Settings:
    """半配置：主模型给全了，fallback 侧**声明了**却没给键（B-33 残余②的注入形状）。

    必须显式传 `fallback_model_api_key=""`：init kwargs 才压得住机器上真 `.env` 里的
    同名键——否则这条用例在配了 fallback 的机器上会退化成"全配置"而失去判据。
    """
    return Settings(
        model_provider="deepseek", model_name="m", model_api_key="sk-fake",
        model_base_url="https://x.invalid/v1",
        fallback_model_provider="deepseek", fallback_model_name="m",
        fallback_model_api_key="",
    )


@pytest.mark.asyncio
async def test_probe_endpoint_releases_when_the_call_succeeds(_fake_provider) -> None:
    """成功路径：返回 ⇒ 可用（内容为空也算）⇒ `skip_reason` 放行。

    顺带钉住探测的调用形状：内层请求超时用 `settings` 的值、`max_tokens=1`
    （探测不烧 token），且外层预算比内层大（否则内层来不及抛就被外层砍掉）。
    """
    probe = await probe_endpoint(_config(), label="primary", timeout=15.0)

    assert probe.ok and probe.reason is None
    assert not probe.unavailable and not probe.unknown
    assert skip_reason([probe]) is None
    assert _fake_provider["factory_kwargs"] == {"request_timeout": 15.0}
    call = _fake_provider["model"].calls[0]
    assert call["max_tokens"] == 1
    assert len(call["messages"]) == 1


@pytest.mark.asyncio
async def test_probe_endpoint_classifies_a_real_payload_and_redacts_the_detail(
    _fake_provider,
) -> None:
    """失败路径：真实回显 → 三态里的「已分类的不可用」，详情一律经脱敏 helper 产出。"""
    _fake_provider["error"] = _HttpError(_PRIMARY_BODY, status_code=400)

    probe = await probe_endpoint(_config(), label="primary", timeout=15.0)

    assert probe.unavailable and probe.reason == PROVIDER_ACCOUNT_UNAVAILABLE_REASON
    assert probe.error_type == "_HttpError"
    assert probe.detail and "计费账户已被冻结" in probe.detail
    assert skip_reason([probe]) is not None


@pytest.mark.asyncio
async def test_probe_endpoint_masks_a_key_shaped_token_in_the_detail(_fake_provider) -> None:
    """脱敏**有牙**：正文里塞一个密钥形 token，它必须变成 `***`。

    这条单独成立的理由：`_PRIMARY_BODY`（实测回显）里根本没有密钥形子串，拿它断言
    "key 不在 detail 里"是空转——换成"忘了调 `_redact_detail`"的实现照样绿。要测出
    脱敏这一层，载荷里就必须**真的**有一个会被它命中的 token。
    """
    _fake_provider["error"] = _HttpError(
        "Error code: 401 - {'error': {'message': 'invalid api_key=sk-live-ZZZZ9999'}}",
        status_code=401,
    )

    probe = await probe_endpoint(_config(), label="primary", timeout=15.0)

    assert probe.unavailable and probe.reason == PROVIDER_AUTH_REASON
    assert "sk-live-ZZZZ9999" not in probe.detail
    assert "api_key=sk-live-ZZZZ9999" not in probe.detail
    assert "***" in probe.detail
    # 归因行（最终进 skip 理由的那段文字）同样不许带明文。
    assert skip_reason([probe]) is not None
    assert "sk-live-ZZZZ9999" not in skip_reason([probe])


@pytest.mark.asyncio
async def test_probe_endpoint_masks_credentials_inside_a_url_in_the_detail(_fake_provider) -> None:
    """错误正文里**带凭据的 URL** 也过脱敏——`detail` 走 `_redact_base_url` 的唯一守卫。

    SDK 的报错经常把请求 URL 原样回显（`... connection to https://<userinfo>@host/v1 ...`），
    这里的 userinfo 没有冒号（token 直接当用户名），首版"只认 `user:pass@` 与六个参数名"
    的规则实测漏它。顺带钉住反向：host / path 必须留着，否则排查时看不出打的是哪个地址。
    """
    _fake_provider["error"] = _HttpError(
        "Error code: 401 - connection to https://tok-1234@api.example.com/v1 timed out",
        status_code=401,
    )

    probe = await probe_endpoint(_config(), label="primary", timeout=15.0)

    assert probe.unavailable
    assert "tok-1234" not in probe.detail
    assert "***" in probe.detail
    assert "api.example.com" in probe.detail and "/v1" in probe.detail


@pytest.mark.asyncio
async def test_probe_endpoint_arms_the_outer_budget_above_the_inner_timeout(
    _fake_provider, monkeypatch,
) -> None:
    """外层硬上限 = 内层请求超时 + 余量（**方向与数值都钉住**）。

    为什么要断言：`asyncio.wait_for` 的 timeout 只决定"谁先抛"。若它与内层相等或更小，
    正常超时路径会先被外层砍掉，归因就从"端点超时"退化成无信息的 `TimeoutError`
    （还会连带改变 TLS/连接类失败的归类面）。这个耦合跨了两个模块（`Settings` 超时 →
    探测预算），只靠真跑是看不出来的。
    """
    seen: list[float] = []
    real_wait_for = guard.asyncio.wait_for

    async def _spy(awaitable: Any, timeout: float) -> Any:  # 原样转发，形状与 wait_for 一致
        seen.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(guard.asyncio, "wait_for", _spy)

    probe = await probe_endpoint(_config(), label="primary", timeout=15.0)

    assert probe.ok
    assert len(seen) == 1
    assert seen[0] > 15.0
    assert seen[0] == 15.0 + guard._PROBE_BUDGET_MARGIN_SECONDS


@pytest.mark.asyncio
async def test_probe_chain_releases_when_the_config_is_incomplete(monkeypatch) -> None:
    """**直呼** `probe_chain`：配置不全（`ConfigError`）⇒ 空链 ⇒ 放行。

    直呼组合的语义就是"空链 = 探测不了"，两条不全形态一视同仁：没给主模型键、
    以及"主模型给全了但链上另一级建不起来"（半配置）。
    """
    monkeypatch.setattr("agent_harness.model.provider.create_chat_model",
                        lambda *a, **k: pytest.fail("配置不全时不该发起探测"))

    for settings in (Settings(model_api_key=""), _half_config_settings()):
        probes = await probe_chain(settings)
        assert probes == []
        assert skip_reason(probes) is None


@pytest.mark.asyncio
async def test_chain_verdict_and_ensure_release_on_incomplete_config(monkeypatch) -> None:
    """**没声明**主模型（key 为空）在两层都只是放行——覆盖三条形态。

    这是守卫与用例既有 `skipif` 的分界：**key 为空** ⇒ 由 `skipif` 说话。判据与用例侧
    同一件事（`get_secret_value()` 非空，不 strip、不看 provider；措辞不同、语义等价——
    精确边界见 `live_model_guard._primary_key_present` 的 docstring），所以放行面恰好是
    "用例自己会 skip"的那一片；其余形态一律走响亮 skip——放行它们只会以夹具层 ERROR
    收场（B-33 残余②的形状）。

    三个空值全是显式钉的：`Settings(...)` 会读**机器上真实的** `.env`（pydantic-settings 的
    env_file 来源），不钉就等于让本机的 key 决定这条用例往哪条出口走——实测踩过。
    """
    monkeypatch.setattr(guard, "_VERDICTS", {})
    undeclared = [
        Settings(model_api_key="", fallback_model_provider=""),           # 什么都没配
        Settings(model_provider="", model_api_key=""),                    # provider 也空
        Settings(model_provider="no_such_provider", model_api_key=""),    # provider 名错，但没 key
    ]

    for settings in undeclared:
        assert await chain_verdict(settings) is None
        await ensure_live_model(settings)  # 不抛 = 放行


def test_primary_key_present_matrix_matches_the_case_side_skipif() -> None:
    """`_primary_key_present` 的判据面 = 用例 `skipif` 的判据面（**逐行对照**）。

    用例侧的表达式就是这个矩阵的期望列：`bool(settings.model_api_key.get_secret_value())`
    （`tests/integration/test_phase13_gate.py` / `test_phase14_gate.py` 的 `_gate_settings`）。
    期望列是**手抄**的，所以下面还有一条文本级漂移闸
    （`test_case_side_skipif_text_still_matches_the_guards_predicate`）钉住用例侧原文。
    守卫若在这里加了 `.strip()` 或"provider 也要非空"，**这批本该放行的形态会变成响亮
    skip**——方向是安全的（不静默代码回归），但口径与用例分叉，同一台机器上会出现
    "用例 skipif 说没配、守卫却报配置缺陷"的矛盾读数。所以两处判据必须一起改。
    """
    rows = [
        (Settings(model_api_key="", fallback_model_provider=""), False),
        (Settings(model_provider="", model_api_key=""), False),
        (Settings(model_provider="", model_api_key="sk-fake"), True),   # 有 key，provider 空
        (Settings(model_provider="no_such_provider", model_api_key="sk-fake"), True),
        (Settings(model_api_key="   "), True),                          # 空白 key：两层都算"在场"
    ]

    for settings, expected in rows:
        key = settings.model_api_key.get_secret_value()
        assert bool(key) is expected, "夹具值与期望列不一致（先修表）"
        assert guard._primary_key_present(settings) is expected


#: 用例侧放行判据的**文本**（两轴审查 B 轴 P3：期望列是手写的，改动用例侧不会让上面那条
#: 变红）。这里按文本再钉一道：用例侧表达式一旦改写，本用例立刻红，逼着回头看等价口径。
_CASE_SIDE_PREDICATE = "if not settings.model_api_key.get_secret_value():"


def test_case_side_skipif_text_still_matches_the_guards_predicate() -> None:
    """上面那张矩阵的"等价"不是自证的：**用例侧原文**也得还是同一个判据（文本级漂移闸）。

    局限（如实记）：文本匹配钉的是**拼写**，不是语义——把 `not` 挪走这类等价改写它会漏。
    它挡的是最常见的漂移（有人把用例侧改成 `.strip()` / 加 provider 条件 / 抽成别的
    表达式），那时本用例红，而上面的矩阵仍会绿——正是要吵醒的那种分歧。
    """
    cases = [
        Path(__file__).parent / "integration" / "test_phase13_gate.py",
        Path(__file__).parent / "integration" / "test_phase14_gate.py",
    ]

    for path in cases:
        text = path.read_text(encoding="utf-8")
        assert _CASE_SIDE_PREDICATE in text, (
            f"{path.name} 的放行判据变了——守卫的等价口径（`_primary_key_present`）"
            f"必须跟着一起改，或把这里改成新的原文"
        )


@pytest.mark.asyncio
async def test_a_whitespace_only_key_becomes_a_named_skip_not_a_fixture_error(monkeypatch) -> None:
    """空白 key（`"   "`）：用例 `skipif` 也算"在场" ⇒ 守卫必须开口，不许放行。

    放行它 = 把用例交给夹具层 ERROR：构造器拒**strip 后为空**的 key
    （`ModelConfig` ⟶ "缺少 API key：空白 key 只会把失败推到首次 ainvoke"），于是
    `from_settings` 在建 runtime 时抛 `ConfigError`——"环境噪声冒充代码回归"的形状。
    守卫在探测前就把它转成**点名这条缺陷的 skip**（不花网络往返，故这里禁止发探测）。

    注意实现面：这条出口与"provider 名写错"共用同一条响亮 skip，**不是**探测结果的
    那条（空白 key 的链根本建不起来）。
    """
    monkeypatch.setattr("agent_harness.model.provider.create_chat_model",
                        lambda *a, **k: pytest.fail("链建不起来，不该发起探测"))
    monkeypatch.setattr(guard, "_VERDICTS", {})

    reason = await chain_verdict(Settings(model_api_key="   "))

    assert reason is not None
    assert "未被验证" in reason, "skip 不能被读成门禁通过"
    assert "空白 key" in reason, "理由要带原始报错（这里正是空白 key 的判据）"


@pytest.mark.asyncio
async def test_chain_verdict_skips_loudly_when_the_declared_chain_is_incomplete(
    monkeypatch,
) -> None:
    """半配置（主模型给全、fallback 声明了缺键）⇒ 响亮 skip，**不**放行到夹具层 ERROR。

    B-33 残余②的形状：用例的 `skipif` 只看主模型键 ⇒ 放行只会以夹具层 ERROR 收场
    （`ModelConfig.from_settings` 在建 runtime 时抛 `ConfigError`）。守卫把整条链纳入
    配置谓词后，这条出口变成"指名缺哪个 env 的 skip"。

    顺带钉住：段内的脱敏**不许误伤 env 名**——理由必须留全 `FALLBACK_MODEL_API_KEY`
    （喂 `_redact_detail` 会把它打成 `FALLBACK_MODEL_***`，用户就不知道该补哪个键了）。
    反方向（真有密钥形 token 时必须打码）由下一条覆盖：本条的报错正文里根本没有
    主模型 key，在这里断言"key 不在理由里"是**空转**——两轴审查实测指出后改掉了。
    """
    monkeypatch.setattr("agent_harness.model.provider.create_chat_model",
                        lambda *a, **k: pytest.fail("半配置在建链期就失败，不该发起探测"))
    monkeypatch.setattr(guard, "_VERDICTS", {})

    reason = await chain_verdict(_half_config_settings())

    assert reason is not None
    assert "FALLBACK_MODEL_API_KEY" in reason, "理由要指名缺的是哪一个 env"
    assert "未被验证" in reason, "skip 不能被读成门禁通过"

    monkeypatch.setattr(guard, "_WARNED", [True])  # warning 已发过，用例不重复发
    with pytest.raises(pytest.skip.Exception):
        await ensure_live_model(_half_config_settings())


@pytest.mark.asyncio
async def test_the_defect_reason_masks_a_key_shaped_provider_name() -> None:
    """反向的牙：provider 名写成密钥形（**贴错字段**）时，理由里只剩 `***`。

    这条也是 `_KEY_SHAPED_TOKEN` 在理由生成处唯一的守卫——没有它，把那行 `.sub(...)`
    删掉照样全绿。载荷走的是真路径：`_fallback_from` 先查 provider 是否在预设里，
    未知名直接进 `ConfigError` 正文，于是密钥形的名字会原样出现在理由中。
    """
    reason = await chain_verdict(Settings(
        model_provider="deepseek", model_api_key="sk-fake",
        fallback_model_provider="sk-live-ABCD1234", fallback_model_api_key="",
    ))

    assert reason is not None
    assert "ABCD1234" not in reason
    assert "***" in reason


@pytest.mark.asyncio
async def test_unknown_provider_with_a_key_is_a_config_defect_not_a_release() -> None:
    """provider 名写错**且 key 给全了** ⇒ 也算"声明了却建不起来"（响亮 skip）。

    与上一条同源：这种链在建 runtime 时同样抛 `ConfigError`，放行只能变成夹具层 ERROR。
    """
    reason = await chain_verdict(
        Settings(model_provider="no_such_provider", model_api_key="sk-fake"),
    )

    assert reason is not None and "no_such_provider" in reason


@pytest.mark.asyncio
async def test_ensure_live_model_skips_when_the_whole_chain_is_unavailable(monkeypatch) -> None:
    """全链不可用 ⇒ `pytest.skip`（skip 出口本身也要有覆盖，否则守可能恒放行）。"""
    monkeypatch.setattr(guard, "_VERDICTS", {})
    monkeypatch.setattr(guard, "_WARNED", [True])  # warning 已发过，用例不重复发

    async def _down(settings: Any) -> list[EndpointProbe]:
        return [_unavailable("primary")]

    monkeypatch.setattr(guard, "probe_chain", _down)

    with pytest.raises(pytest.skip.Exception) as excinfo:
        await ensure_live_model(_settings())

    assert "未被验证" in str(excinfo.value)


@pytest.mark.asyncio
async def test_chain_verdict_caches_one_probe_per_signature(monkeypatch) -> None:
    """同链签名只真探一次（省网络往返；缓存不是正确性依赖，见模块注释）。"""
    monkeypatch.setattr(guard, "_VERDICTS", {})
    calls: list[int] = []

    async def _count(settings: Any) -> list[EndpointProbe]:
        calls.append(1)
        return [_ok("primary")]

    monkeypatch.setattr(guard, "probe_chain", _count)
    settings = _settings()

    assert await chain_verdict(settings) is None
    assert await chain_verdict(settings) is None

    assert len(calls) == 1
