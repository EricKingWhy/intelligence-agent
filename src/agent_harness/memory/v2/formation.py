"""#298 / MEM-V2-2：Formation 与 Adjudication 的结果契约与严格校验（PRD §6.2 / §6.3）。

# 两阶段的形状

Formation 把一次 run 压成 `CANDIDATES` 或 `NO_MEMORY`；Adjudication 对**每个候选**给出
`ADD` / `UPDATE` / `INVALIDATE` / `NOOP`。本模块只负责"模型输出 → 经过校验的结构化结果"，
不含模型调用、不含预算、不含落盘（那些各有其主）。

契约与**政策**的边界是本模块最重要的一条线：形状（schema）必须合法，否则解析失败；
政策（候选上限、秘密/敏感、用户事实来源）由 `policy.py` 在同一批对象上独立执法。
分开的理由是 R7——"模型分类不足以作为秘密策略"，所以策略不能长在 schema 里靠模型自查。

# fail closed 的三层

1. **传输层**：只剥一层 markdown 围栏 + 首尾空白，然后 `json.loads`。**刻意不复用** V1 的
   `_repair_json`（那会做字符级修复）：V1 修不好就降级到启发式，V2 没有降级路径，所以"修"
   只会把"模型没按契约输出"变成"我们猜它的意思"。R3：解析失败就是一次**失败尝试**，
   既不是 `NO_MEMORY` 也不是 abstention。
2. **模式层**：pydantic，`extra="forbid"`。模型多写一个 `tenant_id` 不是"被忽略"而是
   解析失败——见下一节。
3. **语义层**：跨字段搭配（`decision` 与 candidates/skip_reason、`action` 与
   target/result、kind↔payload、tier/scope/project_id）。

# 身份字段不在契约里（§6.2 末句）

"Runtime replaces all identity fields with trusted request/session identity" 在类型上的
落地方式是**根本不给模型位置**：候选里没有 `tenant_id` / `user_id` / `id` / `version` /
时间戳，`extra="forbid"` 让模型一旦写了就解析失败（"forged identity" 因此不是被忽略，
而是被拒绝）。

§6.3 说 adjudication 的 `result` 是 "complete MemoryRecordV2 candidate"。本模块读作
**内容字段齐全**的 draft 形状，而不是含服务器拥有字段的完整信封：§6.1 把 id / version /
时间戳都定义为服务器所有（"Server-owned timestamps"），模型不可能合法地生产它们。
这条读法连同理由登记在 ADR-0043。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Self

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict, Field, ValidationError, model_validator

from agent_harness.memory.v2.types import (
    CONTENT_MAX_CHARS,
    EVIDENCE_EXCERPT_MAX_CHARS,
    MemoryKind,
    MemoryPayload,
    MemoryScope,
    MemoryTier,
    assert_content_contract,
)

# --------------------------------------------------------------------------------------
# 枚举（§6.2 / §6.3 / §5.7 的固定词表）
# --------------------------------------------------------------------------------------


class Sensitivity(str, Enum):
    """§6.2：模型对内容敏感度的自陈。**它不是执法依据**（R7），只是给政策层一个输入。"""

    ORDINARY = "ordinary"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class SensitiveCategory(str, Enum):
    """§5.7 的敏感类目（"需显式记住请求才可存"的那几类）。"""

    HEALTH = "health"
    FINANCIAL = "financial"
    GOVERNMENT_ID = "government_id"
    PRECISE_LOCATION = "precise_location"
    BIOMETRIC = "biometric"
    INTIMATE = "intimate"
    POLITICAL_RELIGIOUS = "political_religious"
    LEGAL = "legal"
    MINORS = "minors"


class FormationDecision(str, Enum):
    """§6.2 的两个顶层决定。字面量用大写，与 PRD 的线格式一致。"""

    CANDIDATES = "CANDIDATES"
    NO_MEMORY = "NO_MEMORY"


class ModelSkipReason(str, Enum):
    """§6.2 的 `skip_reason`：**模型**对"这次内容没有值得记的"判断。

    与 `eligibility.FormationSkipReason`（**运行时**对"该不该跑"的判断）刻意分开：
    合成一套会让"模型说没有值得记的"与"这次压根不该跑"在观测里无法区分。
    """

    NO_DURABLE_VALUE = "no_durable_value"
    TRANSIENT_ONLY = "transient_only"
    UNSUPPORTED_EVIDENCE = "unsupported_evidence"
    EXPLICIT_OPT_OUT = "explicit_opt_out"
    NO_USER_INPUT = "no_user_input"
    SENSITIVE_WITHOUT_CONSENT = "sensitive_without_consent"
    SECRET_DETECTED = "secret_detected"


class AdjudicationAction(str, Enum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    INVALIDATE = "INVALIDATE"
    NOOP = "NOOP"


class AdjudicationReasonCode(str, Enum):
    """§6.3 的 `reason_code`。"""

    DURABLE_NEW = "durable_new"
    ENRICH_EXISTING = "enrich_existing"
    CONTRADICTS_EXISTING = "contradicts_existing"
    USER_AUTHORITY_WINS = "user_authority_wins"
    DUPLICATE = "duplicate"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    PROCEDURAL_THRESHOLD_NOT_MET = "procedural_threshold_not_met"
    POLICY_REJECTED = "policy_rejected"


# --------------------------------------------------------------------------------------
# 契约模型
# --------------------------------------------------------------------------------------


class ModelOutputError(ValueError):
    """模型输出不是一份合法契约。

    R3 钉死语义：这是一次**失败尝试**（消耗重试预算、最终可能 degraded），
    而**不是** abstention——把"模型没按契约说话"当成"模型说没有可记的"，会用一次
    格式错误换掉一轮真正的尝试。
    """


class _ContractModel(_BaseModel):
    """契约模型的公共配置：不可变、拒绝额外字段（见模块 docstring 第 2 层）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateEvidence(_ContractModel):
    """模型给出的**证据引用**：指到本 run 的某条事件 + 该事件的摘录。

    `hash` 刻意不在契约里：§6.1 的 `EvidenceItem.hash` 是完整性凭据，由运行时按摘录算出。
    让模型供给哈希等于让它自己给自己盖章——一条伪造的摘录可以配一个自洽的哈希。
    """

    event_id: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=64)
    excerpt: str = Field(min_length=1, max_length=EVIDENCE_EXCERPT_MAX_CHARS)


class AdjudicatedContent(_ContractModel):
    """一条"内容字段齐全"的记忆内容（adjudication 的 `result` 用的就是它）。

    身份（tenant/user/project 之外的 id）、版本、时间戳都不在这里——它们由存储层从可信
    上下文与既有版本补齐（见模块 docstring 末节）。
    """

    kind: MemoryKind
    tier: MemoryTier = MemoryTier.COLLECTION
    scope: MemoryScope
    project_id: str | None = Field(default=None, min_length=1)
    content: str = Field(min_length=1, max_length=CONTENT_MAX_CHARS)
    payload: MemoryPayload
    importance: float = Field(ge=0.0, le=1.0)
    strength: float = Field(ge=0.0, le=1.0)
    evidence: list[CandidateEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def _enforce(self) -> Self:
        assert_content_contract(
            kind=self.kind, tier=self.tier, scope=self.scope,
            project_id=self.project_id, payload=self.payload)
        return self


class FormationCandidate(AdjudicatedContent):
    """§6.2 的一个候选：在上面那层内容字段之外，多一组**敏感度自陈**。

    自陈只是政策层的输入：secret 一律拒绝、sensitive 需显式记住请求——两条都由运行时
    独立判定，不看模型的结论就能成立（R7）。
    """

    sensitivity: Sensitivity = Sensitivity.ORDINARY
    sensitive_category: SensitiveCategory | None = None

    @model_validator(mode="after")
    def _enforce_sensitivity(self) -> Self:
        if self.sensitivity is Sensitivity.SENSITIVE:
            if self.sensitive_category is None:
                raise ValueError("a sensitive candidate must name an applicable category")
        elif self.sensitive_category is not None:
            # 类目是"这条为什么敏感"的说明；非 sensitive/secret 却带着类目是自相矛盾，
            # 放行等于让模型用"打上类目"的方式绕过后面的一致性判断。
            raise ValueError(
                f"sensitive_category is only meaningful for sensitive content, "
                f"got sensitivity {self.sensitivity.value!r}")
        return self


class FormationResult(_ContractModel):
    """§6.2 的顶层结果。两个方向都要满足，缺一不可（fail closed）。"""

    decision: FormationDecision
    candidates: list[FormationCandidate] = Field(default_factory=list)
    skip_reason: ModelSkipReason | None = None

    @model_validator(mode="after")
    def _enforce(self) -> Self:
        if self.decision is FormationDecision.CANDIDATES:
            if not self.candidates:
                raise ValueError("CANDIDATES requires at least one candidate")
            if self.skip_reason is not None:
                raise ValueError("CANDIDATES must not carry a skip_reason")
        else:
            if self.candidates:
                raise ValueError("NO_MEMORY must not carry candidates")
            if self.skip_reason is None:
                raise ValueError("NO_MEMORY requires a skip_reason")
        return self


class AdjudicationResult(_ContractModel):
    """§6.3 的**单候选**裁决结果（模型每个候选返回一个对象）。"""

    action: AdjudicationAction
    target_memory_id: str | None = Field(default=None, min_length=1)
    result: AdjudicatedContent | None = None
    reason_code: AdjudicationReasonCode

    @model_validator(mode="after")
    def _enforce(self) -> Self:
        if self.action is AdjudicationAction.ADD:
            if self.target_memory_id is not None:
                raise ValueError("ADD must not name a target")
            if self.result is None:
                raise ValueError("ADD requires a complete result")
        elif self.action is AdjudicationAction.UPDATE:
            if self.target_memory_id is None:
                raise ValueError("UPDATE requires a target")
            if self.result is None:
                raise ValueError("UPDATE requires a complete result")
        elif self.action is AdjudicationAction.INVALIDATE:
            if self.target_memory_id is None:
                raise ValueError("INVALIDATE requires a target")
            if self.result is not None:
                raise ValueError("INVALIDATE must not carry a replacement result")
        else:
            # NOOP 不写任何东西：带着目标或替换结果都说明模型在自相矛盾。
            if self.target_memory_id is not None or self.result is not None:
                raise ValueError("NOOP must not carry a target or a result")
        return self


# --------------------------------------------------------------------------------------
# 解析（第 1 层：传输归一；第 2/3 层交给上面的模型）
# --------------------------------------------------------------------------------------

#: 只用于剥 markdown 围栏——**不是** JSON 修复器（见模块 docstring 第 1 层）。
_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", flags=re.IGNORECASE)


def parse_formation_result(raw: str | Mapping[str, Any]) -> FormationResult:
    """把模型的原始输出解析成 `FormationResult`；失败抛 `ModelOutputError`。"""
    return _validate(raw, FormationResult)


def parse_adjudication_result(raw: str | Mapping[str, Any]) -> AdjudicationResult:
    """把模型的原始输出解析成**一条** `AdjudicationResult`；失败抛 `ModelOutputError`。"""
    return _validate(raw, AdjudicationResult)


def _validate(raw: str | Mapping[str, Any], model: type[_ContractModel]):
    payload = _loads(raw)
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        # 只带前若干条错误：整份 pydantic 报告会很长，而这段文字会进日志与事件。
        raise ModelOutputError(f"{model.__name__}: {_summarize(error)}") from None


def _loads(raw: str | Mapping[str, Any]) -> Any:
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str):
        raise ModelOutputError(f"expected a JSON object, got {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise ModelOutputError("empty model output")
    try:
        return json.loads(_JSON_FENCE_RE.sub("", text).strip())
    except json.JSONDecodeError as error:
        raise ModelOutputError(f"model output is not JSON: {error}") from None


def _summarize(error: ValidationError, *, limit: int = 3) -> str:
    """错误摘要：`位置: 消息`，最多 `limit` 条（其余折叠计数）。"""
    lines = [
        f"{'.'.join(str(part) for part in item['loc']) or '<root>'}: {item['msg']}"
        for item in error.errors()[:limit]
    ]
    extra = len(error.errors()) - len(lines)
    if extra > 0:
        lines.append(f"(+{extra} more)")
    return "; ".join(lines)
