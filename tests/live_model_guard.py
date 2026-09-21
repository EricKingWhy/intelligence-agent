"""真实模型环境守卫（`-m integration` 全集里的 17 项红，2026-09-22）。

背景：`-m integration` 的 40 个选中用例里有 17 个依赖真实模型端点。供应商账户不可用时
（实测：primary 400 `billing`「计费账户已被冻结」、fallback 429「余额不足或无可用资源包」），
它们全部以「真实 run 落 failed」的形状变红——红的是**环境**，不是代码。这类红点
**不可修**：任何代码改动都改不动供应商的账户状态；把它们一直挂在红里，真正的代码回归
就淹在 17 条噪声里。

判据只有一条：**配置链里有没有任何一个端点能真的完成一次最小调用**。

- 有 ⇒ 放行，用例照常跑（代码回归仍然会红，门禁强度一条不减）；
- 没有 ⇒ `pytest.skip`，理由逐端点列出分类结果（响亮，不静默）；
- 判不出（未分类失败）⇒ 放行（**fail-closed**：可能是代码回归的失败绝不被吞成 skip）；
- 完全没配（没有 `MODEL_PROVIDER` / `MODEL_API_KEY`）⇒ 放行，由用例里既有的 `skipif`
  说话（缺配置有自己的话要说，守卫不该把它变成夹具层 ERROR）；
- 链**声明了却建不起来**（fallback 侧缺键 / provider 名写错）⇒ `pytest.skip`（响亮）：
  这一路既跑不了真实模型、用例的 `skipif` 又不会说话（它只看主模型键），放行只能以
  夹具层 ERROR 收场——B-33 登记的第 2 条残余（"半配置"），2026-09-22 收口。

覆盖面：挂守卫的用例集合 = **依赖 `.env` 主模型链**的那些真实调用。别处的真实依赖不在此列
（例如 Phase 6/11 的真实 embedding / 存储端点、Docker 门控用例），它们的可用性由各自的门控
表达——本守卫**不声称**覆盖 `-m integration` 里的全部外部依赖。

分类复用产品代码的分类表（`classify_provider_failure`，Reuse First）：账户不可用 /
鉴权失败 / 模型不存在直接命中。网络层（连接失败 / 超时 / 5xx / 429）在本模块补齐——
它们描述的是「端点此刻能不能服务」，属产品语义之外，故**不进** `model/failure.py`
的归因表（那是 run 失败归因的 owner，Scope Lock）。内容审查（`data_inspection_failed`）
**不算**不可用：端点答了话、只是拒了这个请求，不构成环境判据。

与 `web/model_providers.py::_classify_failure` 的分工：那个是**连接测试 UI** 的
HTTP 状态归类（用户点了「测试」按钮看结果），词表与消费者都不同；本模块是**测试门禁**的
判据，只回答「能不能 skip」。两者不合并。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import pytest

from agent_harness.model.config import ConfigError, ModelConfig
from agent_harness.model.failure import (
    CONTENT_MODERATION_REASON,
    PROVIDER_ACCOUNT_UNAVAILABLE_REASON,
    PROVIDER_AUTH_REASON,
    PROVIDER_FAILURE_MESSAGES,
    PROVIDER_MODEL_NOT_FOUND_REASON,
    classify_provider_failure,
)
from agent_harness.web.model_providers import _redact_detail

#: 探测请求：与连接测试同一形状（`web/model_providers.py` 的 `ping` / `max_tokens=1`）。
#: 只问「认证 + 地址 + 模型名可用吗」，不烧 token。
_PROBE_PROMPT = "ping"
_PROBE_MAX_TOKENS = 1
#: 单端点探测的外层硬上限 = 内层请求超时 + 这段余量（秒）。SDK 的超时是请求级的，
#: DNS 挂死等更早的失败面不受它保护，故外面再罩一层 `asyncio.wait_for`；余量保证正常
#: 超时路径**永远由内层先抛**（否则外层先砍，归因会从「端点超时」退化成无信息的取消）。
#: 缺省 `model_test_timeout_seconds = 15.0` ⇒ 外层 20.0。
_PROBE_BUDGET_MARGIN_SECONDS = 5.0

# ── 环境不可用的 reason 词表（本模块 own）────────────────────────────────────
# 产品分类表里的三个「端点不可用」reason（内容审查不在此列，见模块 docstring）。
_PROVIDER_UNAVAILABLE_REASONS = frozenset({
    PROVIDER_ACCOUNT_UNAVAILABLE_REASON,
    PROVIDER_AUTH_REASON,
    PROVIDER_MODEL_NOT_FOUND_REASON,
})
REASON_UNREACHABLE = "provider_unreachable"
REASON_TIMEOUT = "provider_timeout"
REASON_SERVER_ERROR = "provider_server_error"
REASON_RATE_LIMITED = "provider_rate_limited"
_ENVIRONMENT_REASONS = frozenset({REASON_UNREACHABLE, REASON_TIMEOUT, REASON_SERVER_ERROR,
                                  REASON_RATE_LIMITED})
#: 全部「端点不可用」判据（skip 决策只看这个集合的成员资格）。
UNAVAILABLE_REASONS = _PROVIDER_UNAVAILABLE_REASONS | _ENVIRONMENT_REASONS

#: 本模块 own 的四条归类的可读文案（产品表里没有它们的文案）。
_ENVIRONMENT_MESSAGES: dict[str, str] = {
    REASON_TIMEOUT: "端点无响应（超时）",
    REASON_UNREACHABLE: "端点不可达（连接失败 / DNS / 证书 / 代理拒绝）",
    REASON_SERVER_ERROR: "端点服务端错误（5xx）",
    REASON_RATE_LIMITED: "端点拒绝服务（429 限流 / 配额耗尽）",
}

#: 文本标记 → 环境 reason（顺序即优先级）。大小写不敏感，匹配
#: `f"{type(error).__name__}: {error}"`（与 `web/model_providers._classify_failure`
#: 同一匹配面：大小写与措辞由 SDK / 供应商决定，不是契约）。
#:
#: 词表刻意**只用带上下文的短语**，不用裸数字 / 裸动词：`"connect"`、`"429"`、`"503"`
#: 这类裸词在字段名（`request_timeout`、`connect_timeout`）、错误码正文、量纲里到处出现，
#: 一命中就把「我们自己的请求错了」判成「端点不可用」。数字形态统一走带前缀的
#: `error code: <n>`（OpenAI 兼容端点的正文形状）。
_ENVIRONMENT_MARKERS: tuple[tuple[str, str], ...] = (
    ("timed out", REASON_TIMEOUT),
    ("timeout", REASON_TIMEOUT),
    ("connection refused", REASON_UNREACHABLE),
    ("connection error", REASON_UNREACHABLE),
    ("connecterror", REASON_UNREACHABLE),
    ("cannot connect", REASON_UNREACHABLE),
    ("failed to establish", REASON_UNREACHABLE),
    ("name or service not known", REASON_UNREACHABLE),
    ("nodename nor servname", REASON_UNREACHABLE),
    ("getaddrinfo", REASON_UNREACHABLE),
    ("certificate verify failed", REASON_UNREACHABLE),
    ("sslcertverificationerror", REASON_UNREACHABLE),
    ("error code: 429", REASON_RATE_LIMITED),
    ("rate limit", REASON_RATE_LIMITED),
    ("too many requests", REASON_RATE_LIMITED),
    ("error code: 5", REASON_SERVER_ERROR),
    ("internal server error", REASON_SERVER_ERROR),
    ("service unavailable", REASON_SERVER_ERROR),
    ("bad gateway", REASON_SERVER_ERROR),
    ("gateway timeout", REASON_SERVER_ERROR),
)

#: 本侧代码的异常族：它们描述**我们自己的代码 / 配置**，不是端点状态。第 3 层
#: （文本标记）对它们整体关闭——一个 `TypeError(...'request_timeout'...)` 是重构漏改，
#: 判成 `provider_timeout` 去 skip 等于把代码回归吞掉（fail-closed 的反面）。
_OWN_CODE_ERROR_TYPES: tuple[type[BaseException], ...] = (
    AssertionError, AttributeError, IndexError, KeyError, TypeError, ValueError, ConfigError,
)

#: 网络 / TLS 层异常的基类（`OSError` 及其子类）。**必须优先于 `_OWN_CODE_ERROR_TYPES`
#: 判定**：CPython 里 `ssl.SSLCertVerificationError` 同时继承 `SSLError` 与 `ValueError`
#: （`class SSLCertVerificationError(SSLError, ValueError)`），只按自侧族拦会把真实的
#: 证书失败也判成「本侧代码问题」而留在红里。
_TRANSPORT_ERROR_TYPES: tuple[type[BaseException], ...] = (OSError,)

#: HTTP 状态 → 环境 reason 的确定性映射（SDK 异常带 `status_code` 时优先于文本匹配；4xx 里
#: 只有这几个是「端点 / 网络路径状态」语义，其余（400/422…）是「这个请求不对」）。
#: 407 由代理产生（网络路径上我们过不去），402 由供应商产生（余额耗尽）——两者都不是
#: 本侧载荷问题，故整类归入环境。
#: 408（Request Timeout）**故意不映射**（已知取舍）：它同属"端点此刻状态"语义，映射成
#: `provider_timeout` 也说得通；但 408 也可能是"我们发得太慢/载荷太大"这类本侧症状，
#: 判成环境就等于把一种可能的代码回归吞成 skip。两害相权取**留在红里**（fail-closed 那一侧）。
_STATUS_REASONS: dict[int, str] = {
    401: PROVIDER_AUTH_REASON,
    403: PROVIDER_AUTH_REASON,
    404: PROVIDER_MODEL_NOT_FOUND_REASON,
    402: PROVIDER_ACCOUNT_UNAVAILABLE_REASON,
    407: REASON_UNREACHABLE,
    429: REASON_RATE_LIMITED,
}

#: ConfigError 文本里的密钥形 token。**不能**直接把整段文本喂 `_redact_detail`（实测）：
#: 它的 `api[-_]?key` 规则会把 `FALLBACK_MODEL_API_KEY` 这类 **env 名**一并打码成
#: `FALLBACK_MODEL_***`，把"缺哪一个 env"这条唯一可操作的信息毁掉。可触达的
#: `ConfigError` 全是我们自己的固定模板（provider id + env 名 + 预设表），不含配置值；
#: 唯一从 `.env` 抄来的自由文本是 provider id，故这里只兜底 key 形 token
#: （有人把 key 贴错到 provider 字段里的情形）。
_CONFIG_ERROR_KEY = re.compile(r"(?i)\b(?:sk|pk)-[A-Za-z0-9_-]{4,}")

# ── base_url 回显前的脱敏（B-33 残余①，2026-09-22）──────────────────────────
#: userinfo 形凭据（`https://user:pass@`）：与 `transport/contract.py` 的
#: `_GIT_URL_USERINFO` 同一口径——仓库里既有的"审计文本不带凭据"判据，不另立一套。
_URL_USERINFO = re.compile(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@")
#: query 形凭据（`?api_key=…` / `?token=…`）：URL 里另一处常见的塞 key 位置。
_URL_CREDENTIAL_QUERY = re.compile(
    r"(?i)([?&](?:api[-_]?key|key|token|access[-_]?token|auth|password|secret)=)[^&\s]*"
)


def _redact_base_url(url: str) -> str:
    """URL 回显前的脱敏（skip 理由会被 pytest 原样打印，还带一条 warning）。

    用户自有配置的 base_url 可能把凭据塞在 userinfo 或 query 里；最后再过一遍
    `_redact_detail`（密钥形 token 的兜底，ADR-0032 §6.3 的同一条判据）。副作用是
    含 `sk` / `pk` 的主机名也可能被打码——**可接受的 fail-closed** 取舍：provider /
    model 名仍在本行里，读者定位得到端点；公开面（issue / CI 日志）少一段 URL 比
    多一段凭据划算。
    """
    masked = _URL_USERINFO.sub(r"\1***@", url)
    masked = _URL_CREDENTIAL_QUERY.sub(r"\1***", masked)
    return _redact_detail(masked)


def classify_environment_failure(error: BaseException) -> str | None:
    """异常 → 「端点不可用」reason；判不出返回 None（fail-closed 的输入）。

    三层，第一层命中即返回：

    1. HTTP 状态（`status_code` 属性）：只认 401/402/403/404/407/429 与 5xx。**其余 4xx
       一律不走第 3 层**——400/422 是「这个请求不对」，可能是我们自己的代码发错了载荷，
       正是必须留在红里的那一类；
    2. 产品分类表（`classify_provider_failure`）：账户 / 鉴权 / 模型不存在 ⇒ 端点不可用；
       内容审查 ⇒ 端点**可用**（答了话，只是拒了这个请求），返回 None；
    3. 网络 / 额度文本标记（DNS、连接失败、超时、5xx、429）。**只对「不带 4xx 状态码、
       且不属于本侧异常族」的异常生效**（`_OWN_CODE_ERROR_TYPES`）：一个
       `TypeError: create_chat_model() got unexpected keyword argument 'request_timeout'`
       是重构漏改，判成 `provider_timeout` 去 skip 就等于把代码回归吞掉。
    """
    status = getattr(error, "status_code", None)
    has_status = isinstance(status, int)
    if has_status:
        if status in _STATUS_REASONS:
            return _STATUS_REASONS[status]
        if status >= 500:
            return REASON_SERVER_ERROR

    classified = classify_provider_failure(error)
    if classified == CONTENT_MODERATION_REASON:
        return None
    if classified in _PROVIDER_UNAVAILABLE_REASONS:
        return classified

    # 到这里的 4xx 是「产品表也不认识的状态码」：正文里恰好出现某个标记词不足以
    # 把端点判成不可用（那正是上面 docstring 承诺的那条界线），直接交给 fail-closed。
    own_code = (isinstance(error, _OWN_CODE_ERROR_TYPES)
                and not isinstance(error, _TRANSPORT_ERROR_TYPES))
    if has_status or own_code:
        return None
    text = f"{type(error).__name__}: {error}".lower()
    for marker, reason in _ENVIRONMENT_MARKERS:
        if marker in text:
            return reason
    return None


@dataclass(frozen=True)
class EndpointProbe:
    """一个端点的探测结果（三态：可用 / 不可用（已分类）/ 未分类）。"""

    label: str
    provider: str
    model_name: str
    base_url: str
    ok: bool
    reason: str | None = None
    error_type: str = ""
    detail: str = ""

    @property
    def unavailable(self) -> bool:
        """已分类的不可用——**只有**这一态参与 skip 决策。"""
        return (not self.ok) and self.reason is not None

    @property
    def unknown(self) -> bool:
        """未分类的失败（fail-closed：它让整条链的 skip 决策失效）。"""
        return (not self.ok) and self.reason is None

    def message(self) -> str:
        """本端点的可读归因（固定文案；**不含** provider 回显原文的未脱敏版本）。"""
        if self.ok:
            return "可用"
        if self.reason is None:
            return f"未分类失败（{self.error_type}）"
        copy = PROVIDER_FAILURE_MESSAGES.get(self.reason, _ENVIRONMENT_MESSAGES.get(self.reason, ""))
        suffix = f"：{copy}" if copy else ""
        return f"{self.reason}{suffix}（{self.error_type}）"

    def line(self) -> str:
        """skip 理由里的一行：`· primary provider/model @ base_url → 归因（脱敏详情）`。

        base_url 也**必须**脱敏后再回显（`_redact_base_url`）：它来自用户配置，凭据可能
        塞在 userinfo / query 里，而本行会被 pytest 原样打印并进 warning。
        """
        text = (f"  · {self.label} {self.provider}/{self.model_name} @ "
                f"{_redact_base_url(self.base_url)} → {self.message()}")
        if self.detail:
            text += f"\n    {self.detail}"
        return text


def chain_from_settings(settings: Any) -> list[tuple[str, ModelConfig]]:
    """`settings` → 探测链（primary + fallback，按 provider/模型/地址去重）。

    去重是实打实的省事：单级配置（无 fallback）只探一次；fallback 与 primary 配成同一
    组（合法但无用）时也不打两次。
    """
    config = ModelConfig.from_settings(settings)
    chain: list[tuple[str, ModelConfig]] = [("primary", config)]
    fallback = config.fallback
    if fallback is not None:
        same = (fallback.provider == config.provider
                and fallback.model_name == config.model_name
                and fallback.base_url == config.base_url)
        if same:
            chain[0] = ("primary(=fallback)", config)
        else:
            chain.append(("fallback", fallback))
    return chain


async def probe_endpoint(
    config: ModelConfig, *, label: str, timeout: float,
) -> EndpointProbe:
    """一次最小真实调用：走**真实构造路径**（`create_chat_model`），返回三态探测结果。

    成功判定 = 调用返回且没抛异常（内容为空也算成功）——与连接测试同一判据：
    本探测只证明「认证 + 地址 + 模型名可用」。
    """
    from langchain_core.messages import HumanMessage

    from agent_harness.model.provider import create_chat_model

    def _result(**kwargs: Any) -> EndpointProbe:
        return EndpointProbe(
            label=label, provider=config.provider, model_name=config.model_name,
            base_url=config.base_url, **kwargs,
        )

    try:
        model = create_chat_model(config, request_timeout=timeout)
        await asyncio.wait_for(
            model.ainvoke([HumanMessage(content=_PROBE_PROMPT)], max_tokens=_PROBE_MAX_TOKENS),
            timeout=timeout + _PROBE_BUDGET_MARGIN_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 — 探测的失败面就是「任何失败」
        return _result(
            ok=False, reason=classify_environment_failure(error),
            error_type=type(error).__name__,
            # 错误正文可能回显请求头 / URL query：一律先脱敏再进理由
            # （ADR-0032 §6.3 的同一判据，复用同一个 helper）。
            detail=_redact_detail(str(error)),
        )
    return _result(ok=True)


def _configured_chain(settings: Any) -> list[tuple[str, ModelConfig]] | None:
    """`chain_from_settings` 的**直呼**版：配置不全（`ConfigError`）⇒ None。

    「缺配置」不是「端点不可用」⇒ 这里一律放行（空链 = 探测不了），供 `probe_chain`
    这类直呼组合的调用方使用。守卫路径（`chain_verdict`）对**声明了却建不起来**的链有
    更细的裁决（响亮 skip），见 `_incomplete_chain_reason`。
    """
    try:
        return chain_from_settings(settings)
    except ConfigError:
        return None


def _primary_declared(settings: Any) -> bool:
    """主模型是否**真的被声明了**（`MODEL_PROVIDER` + `MODEL_API_KEY` 都非空）。

    这是"缺配置"与"配置有缺陷"的分界：没声明 ⇒ 用例既有 `skipif` 有自己的话说
    （放行）；声明了却建不起整条链 ⇒ 那条链的缺陷必须由守卫说出口（响亮 skip）。
    """
    provider = str(getattr(settings, "model_provider", "") or "")
    key = getattr(settings, "model_api_key", None)
    secret = key.get_secret_value() if key is not None else ""
    return bool(provider.strip()) and bool(secret.strip())


def _incomplete_chain_reason(settings: Any, error: ConfigError) -> str | None:
    """链建不起来时的裁决：`None` = 放行（没声明）；字符串 = 响亮 skip 理由。

    覆盖的是**整条链**，不只看主模型那一级：`MODEL_API_KEY` 给全了、fallback 侧声明了
    却发现缺键（或 provider 名写错）时，用例的 `skipif` **不会**说话（它只看主模型键），
    放行只能以夹具层 ERROR 收场——B-33 登记的第 2 条残余就是这个形状。
    """
    if not _primary_declared(settings):
        return None
    detail = _CONFIG_ERROR_KEY.sub("***", str(error))
    return (
        "[live-model guard] 配置链**声明了却建不起来** ⇒ skip 依赖真实模型的用例"
        "（环境问题，不是代码回归）：\n"
        f"  · {detail}\n"
        "  处置：按上面的 .env 指引补全配置后原命令重跑；本 skip 不构成门禁通过——"
        "真实模型路径在本轮**未被验证**。"
    )


async def probe_chain(settings: Any) -> list[EndpointProbe]:
    """探测整条配置链（超时取 `settings.model_test_timeout_seconds`，与连接测试同源）。

    配置不全 ⇒ 空链（不是异常；`skip_reason` 的「链为空」规则随之放行）。**直呼组合**
    的语义就到这里；守卫路径（`chain_verdict`）对"声明了却建不起来"的链另有裁决
    （响亮 skip），见 `_incomplete_chain_reason`。
    """
    timeout = float(getattr(settings, "model_test_timeout_seconds", 15.0))
    return [await probe_endpoint(config, label=label, timeout=timeout)
            for label, config in (_configured_chain(settings) or [])]


def skip_reason(probes: list[EndpointProbe]) -> str | None:
    """三态探测结果 → skip 理由（None = 放行）。

    四条规则，缺一不可：

    - 链为空 ⇒ 放行：那是**缺配置**，用例里的既有 skipif 有自己的话要说。
      注意这条规则的**可达面**：经由 `chain_verdict` 的守卫路径**走不到它**——配置不全时
      `chain_verdict` 在探测前就返回 None（`_configured_chain` 把 `ConfigError` 折成 `None`，
      见其 docstring）。这里收的是**直呼组合**的调用方（本模块的用例，
      以及将来别的工具）：`probe_chain` 对不全配置返回空链（不是异常），空链没有
      "可用 / 不可用"可判，唯一正确的语义就是放行；
    - 任一端点可用 ⇒ 放行：环境能跑真实模型，后来的红必然是别的；
    - 任一未分类失败 ⇒ 放行（**fail-closed**）：判不出环境的失败可能是代码回归；
    - 全部端点都是「已分类的不可用」⇒ skip。
    """
    if not probes:
        return None
    if any(probe.ok for probe in probes):
        return None
    if any(probe.unknown for probe in probes):
        return None
    lines = "\n".join(probe.line() for probe in probes)
    return (
        "[live-model guard] 配置链里没有任何可用端点 ⇒ skip 依赖真实模型的用例"
        "（环境问题，不是代码回归）：\n"
        f"{lines}\n"
        "  处置：修好供应商账户 / 配额后原命令重跑；本 skip 不构成门禁通过——"
        "真实模型路径在本轮**未被验证**。"
    )


#: 一次进程内的探测结论缓存（键 = 链签名）。**省的是网络往返**：链签名不变时重复探测
#: 只会再打几次真实请求，所以同签名的结论在进程内只取一次。pytest 自己会缓存**夹具**
#: 的 setup 结果（含 setup 里 `pytest.skip` 抛出后的结局，`_pytest/fixtures.py` 的
#: finalizer / cache 两层），本字典**不是**正确性依赖——它只是让探测器在夹具缓存失效的
#: 路径上（例如用例显式自建链）也别重复打网络。代价：一次跑很久的 run 里账户状态若中途
#: 翻转，本进程仍按首次结论行事（读秒级的账户翻转不在本守卫的判据面内）。
_VERDICTS: dict[str, str | None] = {}


async def chain_verdict(settings: Any) -> str | None:
    """链签名 → skip 理由（None = 放行）；同签名只真探一次。

    配置面两条出口（覆盖整条链，见 `_incomplete_chain_reason`）：完全没配 ⇒ None
    （放行，交给用例既有 `skipif`）；声明了却建不起来 ⇒ 响亮 skip 理由。两者都不花
    网络往返，故不进缓存。
    """
    try:
        chain = chain_from_settings(settings)
    except ConfigError as error:
        return _incomplete_chain_reason(settings, error)
    signature = "|".join(
        f"{config.provider}/{config.model_name}@{config.base_url}" for _, config in chain
    )
    if signature not in _VERDICTS:
        _VERDICTS[signature] = skip_reason(await probe_chain(settings))
    return _VERDICTS[signature]


async def ensure_live_model(settings: Any) -> None:
    """守卫主体：环境无可用端点时 `pytest.skip`（理由响亮），否则放行。

    第一次判定为 skip 时额外发一条 warning：skip 默认只在 `-rs` 下显示理由，
    而「本轮真实模型路径未被验证」这件事不该需要额外开关才看得见。
    """
    reason = await chain_verdict(settings)
    if reason is None:
        return
    if not _WARNED:
        _WARNED.append(True)
        import warnings

        warnings.warn(reason, UserWarning, stacklevel=1)
    pytest.skip(reason)


#: 进程内「warning 已发过」标记（理由只念一遍，别刷屏）。
_WARNED: list[bool] = []
