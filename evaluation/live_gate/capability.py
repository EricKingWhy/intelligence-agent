"""运行前的能力面：凭证**在场性**（只出键名）+ 端点三态探测（`#307` Must Do / R1 前置）。

## 复用而不是重写（Reuse First）

探测逻辑**完全复用** `tests/live_model_guard.py` 的公开面：`chain_from_settings`（配置链）、
`probe_endpoint`（一次最小真实调用，`max_tokens=1`）、`skip_reason`（三态裁决）、
`EndpointProbe`（**唯一对外文本面**已逐字段脱敏）。

依赖方向是刻意的：`evaluation/` 与 `tests/` 都属**开发/验证面**（都不进 wheel，见
`pyproject.toml` 的 `module-name = "agent_harness"`），所以"开发工具复用测试侧的守卫"不构成
产品对测试的反向依赖。重写一份的代价是判据分叉 —— 会出现"pytest 说端点可用、Live Gate 说
不可用"这种没人能裁的僵局（`AGENTS.md` §16.1）。

## 三态裁决在这里的含义

| 探测结果 | pytest 守卫（`skip_reason`） | 本 Gate |
| --- | --- | --- |
| 任一端点可用 | 放行 | `READY`，照跑 |
| 全部**已分类**不可用 | skip | **`BLOCKED`**（不发任何 run —— 但探测请求本身已经发过，见下） |
| 任一**未分类**失败 | 放行（fail-closed：可能是代码回归） | `READY`（同样 fail-closed：**不许**用"探测没判出来"换来一个 skip 免跑） |

**`BLOCKED` 的"不发请求"要读准**：凭证不足时**一个请求都不发**（在构造配置链之前就返回）；
端点不可用那条路**必然已经发过**探测请求（`probe_endpoint`，一次 `max_tokens=1` 的最小调用）——
它不发的是 **run**（那 3 次真实尝试）。把 `BLOCKED` 一概说成"零网络请求"是错的，而"零字节"
与"零次真实 run"是两件不同的事。

`BLOCKED` 与 `SKIPPED` 的区别：前者是**外部条件不具备**（缺凭证 / 端点全不可用），后者是
**操作者显式跳过**。两者都不产出 `PASS`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_harness.model.config import ConfigError
from evaluation.live_gate.schema import CapabilityRecord, ProbeRecord, ProviderRecord
from evaluation.live_gate.secrets import credential_names, mask_text

#: 能力面两态（写进 `CapabilityRecord.verdict`）：`READY` 可以跑；`BLOCKED` 不跑。
READY = "READY"
BLOCKED = "BLOCKED"


@dataclass
class ProviderCapability:
    """能力面结论 + 证据字段（`provider` / `capability` 两块直接进证据）。"""

    verdict: str
    reason: str = ""
    provider: ProviderRecord | None = None
    probes: list[ProbeRecord] = field(default_factory=list)
    credential_names: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.verdict == READY

    def capability_record(self) -> CapabilityRecord:
        return CapabilityRecord(verdict=self.verdict, reason=self.reason, probes=list(self.probes))


def _host(base_url: str) -> str:
    """只留主机名 —— 路径、query、fragment、**userinfo** 一律不进证据。

    `https://user:pass@api.example.com/v1?k=<key>` → `api.example.com`。三种回显面都真的
    出现过：路径里带 tenant 与 key、query 里带签名、userinfo 里带 `user:pass`（
    `tests/live_model_guard._redact_base_url` 也是因为这个才存在）。**判据是"证据里只该有
    主机名"**，所以这里一律切掉，而不是"掩掉"。
    """
    text = base_url or ""
    authority = text.split("//", 1)[-1]  # 去 scheme（没有 scheme 时原样）
    for separator in ("/", "?", "#"):
        authority = authority.split(separator, 1)[0]
    host = authority.rsplit("@", 1)[-1]  # 去 userinfo（user:pass@host）
    return host or "(empty)"


def _probe_records(probes: list[Any]) -> list[ProbeRecord]:
    # `EndpointProbe.message()` 是固定文案（不含上游回显原文），`detail` 一律**不进**证据。
    return [
        ProbeRecord(label=p.label, ok=p.ok, reason=p.reason, error_type=p.error_type, message=p.message())
        for p in probes
    ]


async def check_capability(settings: Any, *, timeout: float | None = None) -> ProviderCapability:
    """探测运行环境是否能跑真实模型。**缺凭证时绝不发请求**（在构造链之前就返回）。"""
    from tests.live_model_guard import chain_from_settings, probe_endpoint, skip_reason

    names = credential_names(settings)
    if not names:
        return ProviderCapability(
            verdict=BLOCKED,
            reason="未配置任何模型凭证（Settings 上所有 SecretStr 均为空）——不发模型请求",
            credential_names=names,
        )
    try:
        chain = chain_from_settings(settings)
    except ConfigError as error:
        # 配置链**声明了却建不起来**（缺 provider / 缺 fallback 键 / 名字写错）：不是"没配"，
        # 而是配置缺陷 ⇒ 报 BLOCKED 并如实带上脱敏后的原始报错（判据与 pytest 守卫同源）。
        masked, _ = mask_text(str(error), where="capability.reason")
        return ProviderCapability(
            verdict=BLOCKED, reason=f"配置链建不起来：{masked}", credential_names=names,
        )
    primary = chain[0][1]
    effective_timeout = float(
        timeout if timeout is not None
        else getattr(settings, "model_test_timeout_seconds", 15.0)
    )
    probes = [await probe_endpoint(config, label=label, timeout=effective_timeout) for label, config in chain]
    records = _probe_records(probes)
    provider = ProviderRecord(
        provider_id=primary.provider,
        model_name=primary.model_name,
        base_url_host=_host(primary.base_url),
        credential_names=names,
        fallback_provider_id=(primary.fallback.provider if primary.fallback is not None else ""),
        fallback_model_name=(primary.fallback.model_name if primary.fallback is not None else ""),
    )
    blocked_reason = skip_reason(probes)
    if blocked_reason is not None:
        masked, _ = mask_text(blocked_reason, where="capability.reason")
        return ProviderCapability(
            verdict=BLOCKED, reason=masked, provider=provider, probes=records,
            credential_names=names,
        )
    return ProviderCapability(verdict=READY, provider=provider, probes=records, credential_names=names)
