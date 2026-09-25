"""Live Gate 机器证据的 schema（#307 的**稳定对外契约**，runner 内部组织属于 FREE）。

## 为什么要有这一层

`#305` / `#319` 要求五类真实场景在**同一 commit/tree 与配置**上各连跑三次、3/3 才算过，
且每次失败尝试都要保留。这份 JSON 就是"过"的唯一载体 —— 后续票（`#312`–`#320`）与
集成前的重车道读数都引用它，**不从终端输出手抄任何数字**（`#213` 的失效形状）。

## 三条不可让步的判据（写进类型，不靠调用方自觉）

1. **`PASS` 只能是 3/3**：`attempts_planned` 恒为 `GATE_ATTEMPTS`，`verdict` 由
   `Verdict` 计算函数给出（见 `schema.decide_verdict`），不由调用方传入。
2. **注入/替身一律不得产出 `PASS`**：`seams` 里出现 `injected` 或 `injected_failure` 非空
   ⇒ 判定上限是 `FAIL`（"用假模型/假工具当完成证据"是 `#305` 明文禁止的）。
3. **凭据零输出**：本 schema 里**没有**任何 api_key / token / 完整授权 header 字段；
   provider 面只留 `provider_id` / `model_name` / `base_url_host` 与**键名**清单。
   `SecretScanRecord` 的 finding 只存 `rule` + `where` + **脱敏样本**。

## `verdict` 语义（四态，缺一不可）

- `PASS`  —— 计划的三次尝试全部成功，且没有任何注入/替身参与；
- `FAIL`  —— 跑了但没到 3/3（含注入失败；**成功样本不得覆盖失败样本**，全部保留在 `attempts`）；
- `BLOCKED` —— 外部条件不具备（缺凭证 / 端点全不可用 / Sandbox 起不来）⇒ **一个模型请求都没发**；
- `SKIPPED` —— 操作者显式跳过（`--skip <reason>`）⇒ 同样不产出 `PASS`。

`BLOCKED` 与 `FAIL` 的区别是**归因面**：前者"没跑"，后者"跑了没通过"。两者都不算通过。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

#: 证据 schema 版本。字段增删必须同时改 `validator.py` 的判据与本文档段的说明。
SCHEMA_VERSION = 1

#: 一次 Gate invocation 的计划尝试次数（`#305`/`#319` 的 3/3）。**不是可配置项**：
#: 允许调小就等于允许"1/1 也算过"，那正是闸门要堵的形状。
GATE_ATTEMPTS = 3


class Verdict(str, Enum):
    """总判定 / 单次尝试状态。四态语义见模块 docstring（`PASS` 只由 3/3 得到）。"""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


def now_utc() -> str:
    """证据里的时间戳一律 UTC + 毫秒（与 `evaluation/smoke.py::regression_metadata` 同口径）。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def decide_verdict(
    *,
    attempt_statuses: list[Verdict],
    attempts_planned: int = GATE_ATTEMPTS,
    injected_failure: str = "",
    injected_seams: tuple[str, ...] = (),
) -> Verdict:
    """由事实算总判定。**纯函数**：调用方无法"传一个 PASS 进来"。

    判据顺序（每一步都是 fail-closed）：
    1. 有替身/注入（`injected_seams` 非空或 `injected_failure` 非空）⇒ 最多 `FAIL`
       —— 假模型/假工具不得计入 Live Gate；
    2. 尝试数不足或有非 PASS 的尝试 ⇒ `FAIL`（缺的尝试**不得**当成成功）；
    3. 全部 PASS 且次数 == 计划数 ⇒ `PASS`。
    """
    if injected_seams or injected_failure:
        return Verdict.FAIL
    if len(attempt_statuses) != attempts_planned:
        return Verdict.FAIL
    if any(status is not Verdict.PASS for status in attempt_statuses):
        return Verdict.FAIL
    return Verdict.PASS


class AssertionResult(BaseModel):
    """场景内的结构性断言（`evaluation/assertions.py` 那类**代码判断**，不是 LLM judge）。"""

    name: str
    ok: bool
    detail: str = ""


class AttemptRecord(BaseModel):
    """一次尝试的完整记录。失败尝试与成功尝试**同构**（都留全字段）。"""

    index: int
    started_at: str
    ended_at: str
    status: Verdict
    duration_ms: int
    session_id: str = ""
    run_id: str = ""
    run_status: str = ""
    steps: int = 0
    event_count: int = 0
    tool_calls: list[str] = Field(default_factory=list)
    events_ref: str = ""
    events_sha256: str = ""
    assertions: list[AssertionResult] = Field(default_factory=list)
    error: str = ""
    secret_scan_rules: list[str] = Field(default_factory=list)
    output_tail: str = ""
    #: 本次尝试那个一次性工作区的身份（sha256 前缀，见 `SandboxRecord`）。
    workspace_id: str = ""
    #: 落盘前做过的**非凭证**替换（只有 `workspace_path` 一种：把宿主绝对路径替成
    #: `<workspace>`）。与 `secret_scan_rules` 分开列：那是"扫到凭证了"，这是"去掉本机路径"。
    redactions: list[str] = Field(default_factory=list)


class WorktreeProof(BaseModel):
    """工作树输入证明：车道跑在**工作树**上，读数只能记 `HEAD` 的 sha/tree。

    偏离判据与 `scripts/gate0.py::worktree_divergence` **同一实现**（三条独立检查：
    追踪文件偏离 / `assume-unchanged`·`skip-worktree` 位 / 未跟踪的车道输入后缀）。
    """

    head_sha: str
    tree: str
    tracked_matches_head: bool
    untracked: list[str] = Field(default_factory=list)
    hidden: list[str] = Field(default_factory=list)
    risky: list[str] = Field(default_factory=list)


class ProbeRecord(BaseModel):
    """一个端点的能力探测结果。`message` 走 `EndpointProbe.message()`（固定文案、无回显原文）。"""

    label: str
    ok: bool
    reason: str | None = None
    error_type: str = ""
    message: str = ""


class ProviderRecord(BaseModel):
    """Provider/model 标识 —— **不含** token / secret / 完整授权 header。"""

    provider_id: str
    model_name: str
    base_url_host: str
    credential_names: list[str] = Field(default_factory=list)
    fallback_provider_id: str = ""
    fallback_model_name: str = ""


class CapabilityRecord(BaseModel):
    """运行前的能力面：凭证**在场性**（键名，不是值）+ 端点三态探测结论。"""

    verdict: str  # "READY" | "BLOCKED"
    reason: str = ""
    probes: list[ProbeRecord] = Field(default_factory=list)


class SandboxRecord(BaseModel):
    """Sandbox 类型 + 一次性 workspace identity（**证据级**：配置面 + 全部身份）。

    一次尝试一个工作区 ⇒ 逐次身份在 `AttemptRecord.workspace_id` 上，本块记聚合事实
    （`deleted` = 每个都核实删干净了）。`workspace_ids` 是宿主绝对路径的 sha256 前缀 ——
    一次性身份可核对，但**不把本机用户目录路径写进入库证据**。
    """

    backend: str
    disposable: bool
    created: bool
    deleted: bool
    env_allowlisted: bool
    teardown: str = ""
    workspace_ids: list[str] = Field(default_factory=list)


class SecretScanRecord(BaseModel):
    """凭证扫描结论：扫了什么、命中什么规则。**不含任何原值**（样本一律脱敏）。"""

    exact_value_scan: str  # "ran" | "unavailable"
    scanned: list[str] = Field(default_factory=list)
    findings: list[dict[str, str]] = Field(default_factory=list)


class RunnerRecord(BaseModel):
    """跑这次 Gate 的工具语境（引用证据时能复现命令与版本）。"""

    tool_versions: dict[str, str] = Field(default_factory=dict)
    argv: list[str] = Field(default_factory=list)
    attempts_planned: int = GATE_ATTEMPTS


class LiveEvidence(BaseModel):
    """一次 Live Gate invocation 的机器证据（字段名即对外契约，见模块 docstring）。"""

    schema_version: int = SCHEMA_VERSION
    scenario_id: str
    scenario_version: int
    verdict: Verdict
    reason: str = ""
    missing_preconditions: list[str] = Field(default_factory=list)
    created_at: str
    sha: str
    tree: str
    worktree: WorktreeProof
    provider: ProviderRecord
    sandbox: SandboxRecord
    capability: CapabilityRecord
    attempts: list[AttemptRecord] = Field(default_factory=list)
    seams: dict[str, str] = Field(default_factory=dict)
    injected_failure: str = ""
    secret_scan: SecretScanRecord
    runner: RunnerRecord
    scope: dict[str, list[str]] = Field(default_factory=dict)

    def to_json(self) -> str:
        """稳定序列化（键序 = 字段声明序，非 ASCII 原样）——便于人读与 diff。"""
        return self.model_dump_json(indent=2)


def load_evidence(payload: dict[str, Any]) -> LiveEvidence:
    """把落盘 JSON 读回模型。schema 版本不认识 ⇒ 抛 `ValueError`（fail-closed）。"""
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"证据 schema_version={version!r} 不在本实现支持范围内（={SCHEMA_VERSION}）")
    return LiveEvidence.model_validate(payload)
