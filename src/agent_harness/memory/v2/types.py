"""Memory V2 记录契约：typed envelope、per-kind 判别 payload、生命周期枚举。

# 契约来源

- `docs/PRD_PRODUCTION_LONG_TERM_MEMORY_V2.md` §4（产品模型）与 §6.1（durable envelope）
- GitHub Issue #297 / MEM-V2-1（R1、R2、R5、R6、R8）

# 与 V1 的关系（本模块是**新增**，不是替换）

- V1 = `agent_harness.memory.types.MemoryEntry` + `MemoryScope`(USER/SESSION)，
  它由 #156/#158/#159 冻结，并在 MEM-V2-7 clean-slate cutover 之前**继续运行**
  （#297 的 Must Not Do：不改 V1 行为、不激活 V2 自动写回、不删任何真实记忆）。
- 本模块的 `MemoryScope` **与 V1 同名不同义**：V1 是"谁能看见"的授权档位，
  这里是 PRD §4.3 的长期记忆归属（`user_global` / `project`）。两者不复用同一枚举，
  因为它们各自的合法值集合不同，合并会让任一侧的校验被另一侧的历史包袱拖松。
- `session` 在任何情况下都**不是** V2 的 scope（PRD §12：旧假设"SERVER 侧 session 记忆
  是长期记忆的一个档位"已被本 PRD 明确取代）。`source_session_id` 只是 provenance（R6）。

# 校验哲学：fail closed（§6.1）

未识别的枚举值、缺失的必需字段、超出上界的 content、kind 与 payload 不匹配、
请求侧身份与可信上下文冲突、非法的 scope/tier 组合——全部**拒绝且不写入**。
契约模型一律 `extra="forbid"`：为索引或迁移服务的内部字段属于**存储层**，
不得混进契约模型（否则"模型输出里多带一个字段"会静默变成"合法记录"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: PRD §6.1 钉死的信封版本字面量。改它等于换契约，必须走新的 PRD/ADR。
SCHEMA_VERSION_V2 = 2

#: §6.1 `content` 上界（Unicode 字符，即 Python `len` 的口径）。
CONTENT_MAX_CHARS = 500

#: §6.1 `evidence[].excerpt` 上界（Unicode 字符）。
EVIDENCE_EXCERPT_MAX_CHARS = 300

#: per-kind payload 字段的上界。PRD 只钉了 content/evidence 的上界，未钉 payload 字段；
#: 这里收在 content 同一量级，是为了让"把整段对话塞进 payload 绕过 content 限制"不成立。
#: 收紧（而非放宽）不违反 §6.1 的"不得弱化或替换固定字段"。
PAYLOAD_FIELD_MAX_CHARS = 500


class MemoryKind(str, Enum):
    """§4.1：三种有长期价值的记忆形态。没有"其他"档——不吻合就应当 abstain。"""

    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


class MemoryTier(str, Enum):
    """§4.2：访问层级，不是第二份真相；两者共用同一批记录与生命周期。"""

    PROFILE = "profile"
    COLLECTION = "collection"


class MemoryScope(str, Enum):
    """§4.3：长期记忆的归属。**与 V1 同名的 `MemoryScope` 不同义**（见模块文档）。"""

    USER_GLOBAL = "user_global"
    PROJECT = "project"


class MemoryStatus(str, Enum):
    """§6.1 的生命周期状态。

    #297 只**产生** `active` / `superseded` / `invalidated`；`deleted` 是 tombstone
    契约的占位（§6.1 "deleted tombstones retain hash only"），其实现在 MEM-V2-7
    的删除路径里——#297 的 Must Not Do 明令不实现删除/tombstone API。
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"
    DELETED = "deleted"


class SourceType(str, Enum):
    """§4.4：来源权威。`user_edit` 是 R8 的"用户权威"在契约里的表达——
    后续票据据此阻止助手/工具证据单独推翻或失效一条用户编辑过的记忆。
    """

    AUTOMATIC = "automatic"
    EXPLICIT_COMMAND = "explicit_command"
    USER_EDIT = "user_edit"


class SemanticCategory(str, Enum):
    """§4.1：semantic 的四个类目。"""

    PREFERENCE = "preference"
    PROFILE = "profile"
    PROJECT_FACT = "project_fact"
    CONSTRAINT = "constraint"


class _PayloadBase(BaseModel):
    """payload 的公共配置：不可变、拒绝额外字段。

    `kind` 字面量同时是判别器的键（`Field(discriminator="kind")`），所以"种类"这件事
    在 payload 与信封上各写了一次；信封侧的一致性由 `MemoryRecordV2` 的 after-validator
    强制，而不是靠调用方自觉。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticPayload(_PayloadBase):
    """§4.1：稳定偏好 / 画像事实 / 项目事实 / 约束 / 已被接受的更正。"""

    kind: Literal["semantic"] = "semantic"
    subject: str = Field(min_length=1, max_length=PAYLOAD_FIELD_MAX_CHARS)
    fact: str = Field(min_length=1, max_length=PAYLOAD_FIELD_MAX_CHARS)
    category: SemanticCategory


class EpisodicPayload(_PayloadBase):
    """§4.1：可复用的"情境 → 行动 → 结果 → 教训"。"""

    kind: Literal["episodic"] = "episodic"
    situation: str = Field(min_length=1, max_length=PAYLOAD_FIELD_MAX_CHARS)
    action: str = Field(min_length=1, max_length=PAYLOAD_FIELD_MAX_CHARS)
    outcome: str = Field(min_length=1, max_length=PAYLOAD_FIELD_MAX_CHARS)
    lesson: str = Field(min_length=1, max_length=PAYLOAD_FIELD_MAX_CHARS)


class ProceduralPayload(_PayloadBase):
    """§4.1：可复用的操作规则 / 已被验证成功的过程。"""

    kind: Literal["procedural"] = "procedural"
    trigger: str = Field(min_length=1, max_length=PAYLOAD_FIELD_MAX_CHARS)
    procedure: str = Field(min_length=1, max_length=PAYLOAD_FIELD_MAX_CHARS)
    success_condition: str = Field(min_length=1, max_length=PAYLOAD_FIELD_MAX_CHARS)


#: §4.1 的判别联合。未识别的 `kind` 值在解析期即失败（不会落到任何分支）。
MemoryPayload = Annotated[
    SemanticPayload | EpisodicPayload | ProceduralPayload,
    Field(discriminator="kind"),
]


class EvidenceItem(_PayloadBase):
    """§6.1：一条证据摘录。`excerpt` 上界 300；tombstone 只留 `hash`（excerpt 置空）。

    `role` 刻意是自由字符串而不是枚举：PRD 只钉了形状 `{role, excerpt, hash}`，
    没有给合法值集合。把未规定的值域硬编成枚举会造出"PRD 没要求的第二个契约"，
    并在上游多带一个角色名时报错。
    """

    role: str = Field(min_length=1, max_length=64)
    excerpt: str = Field(default="", max_length=EVIDENCE_EXCERPT_MAX_CHARS)
    hash: str = Field(min_length=1)


class _MemoryContentFields(BaseModel):
    """信封里"由内容决定、与生命周期无关"的那部分字段，draft 与 record 共用。

    单独抽出来是因为 §4.1/§4.2/§4.3 的组合规则必须在**写入前**就生效：
    如果只有完整 envelope 校验它们，调用方就必须先瞎编一个 id/version/timestamp
    才能让记录过校验——那等于把服务器拥有的字段变成调用方的输入。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MemoryKind
    tier: MemoryTier = MemoryTier.COLLECTION
    scope: MemoryScope
    project_id: str | None = Field(default=None, min_length=1)
    content: str = Field(min_length=1, max_length=CONTENT_MAX_CHARS)
    payload: MemoryPayload
    importance: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    strength: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    source_type: SourceType
    source_session_id: str | None = None
    source_event_ids: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(min_length=1)

    def _check_common_contract(self) -> None:
        """§4.1/§4.2/§4.3/§6.1 里"单字段看不出错"的那部分规则。

        每条都以 `ValueError` 结束（pydantic 会包成 ValidationError），
        并带上足以定位的字段值——这是给"校验失败"留证据，不是给调用方做控制流。
        """
        if self.payload.kind != self.kind.value:
            raise ValueError(
                f"payload kind {self.payload.kind!r} does not match record kind {self.kind.value!r}")
        if self.tier is MemoryTier.PROFILE:
            # §4.2：profile 是"每次可自动注入的紧凑画像"，只有 user_global 的
            # active semantic 能进。project 作用域的事实注入到所有会话会越界。
            if self.kind is not MemoryKind.SEMANTIC:
                raise ValueError(f"profile tier requires semantic kind, got {self.kind.value!r}")
            if self.scope is not MemoryScope.USER_GLOBAL:
                raise ValueError(f"profile tier requires user_global scope, got {self.scope.value!r}")
        if self.scope is MemoryScope.PROJECT:
            if self.project_id is None:
                raise ValueError("project scope requires project_id")
        elif self.project_id is not None:
            raise ValueError("user_global scope must not carry project_id")
        if self.source_type is SourceType.AUTOMATIC and not self.source_event_ids:
            # §6.1：自动形成的记忆必须指得出它是从哪些事件推出来的；
            # 空 provenance 的"自动记忆"无法被审计，也就无法被撤回。
            raise ValueError("automatic source requires at least one source_event_id")


class MemoryDraftV2(_MemoryContentFields):
    """写入意图：**只有内容**，没有 id / root_id / version / 身份 / 时间戳。

    这是 §6.2「Runtime replaces all identity fields with trusted request/session identity」
    在类型上的落地——身份与版本由存储层从可信上下文补齐，调用方**没有位置**去伪造它们。
    `project_id` 是唯一的例外字段：它描述"这条内容属于哪个项目"，
    仍须与可信上下文核对（`assert_trusted_identity`），核对失败即拒绝。
    """

    @model_validator(mode="after")
    def _enforce(self) -> MemoryDraftV2:
        self._check_common_contract()
        return self


class MemoryRecordV2(_MemoryContentFields):
    """§6.1 的 durable memory envelope。

    字段顺序与 PRD 表格一致，便于逐行对账。`schema_version` 是字面量 2：
    这里不存在"未知版本也能解析"的宽松路径——未知版本必须 fail closed，
    否则 V3 的载荷会被 V2 的读者当成合法数据。
    """

    id: str = Field(min_length=1)
    schema_version: Literal[2] = SCHEMA_VERSION_V2
    root_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    status: MemoryStatus = MemoryStatus.ACTIVE
    valid_at: str | None = None
    invalidated_at: str | None = None
    superseded_by: str | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _enforce(self) -> MemoryRecordV2:
        self._check_common_contract()
        return self


@dataclass(frozen=True, slots=True)
class TrustedMemoryIdentity:
    """由**可信入口**（请求上下文 / 会话绑定）解析出的身份，不是请求体里的自述。

    `project_id` 为 None 表示本次调用没有可信项目上下文——此时只允许操作
    `user_global` 记录。项目事实的 `project_id` 必须来自可信绑定（MEM-V2-1 由
    调用方注入；具体解析链在后续票据接线），绝不能取用户提交的字段。
    """

    tenant_id: str
    user_id: str
    project_id: str | None = None


class UntrustedIdentityError(PermissionError):
    """记录的归属字段与可信身份冲突（R1 末条）。

    继承 `PermissionError` 是刻意的：入口层据此给 403 而不是 500，
    与 V1 的 `MemoryRecordStore.delete` 口径同源（见 `memory/record_store.py`）。
    """


def assert_trusted_identity(record: MemoryRecordV2, trusted: TrustedMemoryIdentity) -> None:
    """核对记录的身份字段就是可信上下文的身份；不一致即拒绝（fail closed）。

    这是**防伪**而不是授权查询：调用方在写盘前用它挡住"请求体自带 tenant/user/project"
    这条路径（PRD §6.2：Runtime replaces all identity fields with trusted identity）。
    `user_global` 记录只比 tenant/user——它的 `project_id` 必为 None 已由契约保证。
    """
    if record.tenant_id != trusted.tenant_id or record.user_id != trusted.user_id:
        raise UntrustedIdentityError("Memory record identity does not match the trusted context")
    if record.scope is MemoryScope.PROJECT and record.project_id != trusted.project_id:
        raise UntrustedIdentityError("Memory record project does not match the trusted context")
