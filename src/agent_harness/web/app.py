"""FastAPI 应用工厂 + 路由（Phase 9/10 精简版）。

create_app() 是单一入口——传入 Settings，返回装配好的 FastAPI。
测试用 test settings 注入；生产用 Settings() 从 .env 读。

路由契约见模块 docstring（web/__init__.py）。
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import importlib.metadata
import json
import logging
import sqlite3
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import anyio
import jwt
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    Field,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError
from sse_starlette.sse import EventSourceResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, Response

from agent_harness.agent import AgentEvent
from agent_harness.agent.budget import BudgetConflict, BudgetRejection
from agent_harness.agent.run_budget import (
    INT64_MAX,
    SessionLimits,
    validate_tool_call_limits_registered,
)
from agent_harness.assembly import (
    RecoveryStores,
    initialize_stores,
    root_registry_reconcile_info,
    root_registry_tool_names,
)
from agent_harness.capability.base import CapabilityError, CapabilityRegistry
from agent_harness.capability.config import parse_capabilities_config
from agent_harness.capability.wiring import CapabilityWiring, wire_capabilities
from agent_harness.config import Settings
from agent_harness.context.project_instructions import (
    empty_project_instruction_status,
    project_instruction_store,
    release_project_instruction_store,
)
from agent_harness.context.tokens import estimate_tokens
from agent_harness.host_service import (
    HOST_PROTOCOL_VERSION,
    HOST_SKILLS_NONCE_HEADER,
    HOST_SKILLS_PROOF_HEADER,
    HOST_SKILLS_PROOF_WINDOW_SECONDS,
    HOST_SKILLS_TIMESTAMP_HEADER,
    host_skills_request_proof,
)
from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.instance_lock import InstanceLock
from agent_harness.logging import setup_logging
from agent_harness.model.config import (
    ConfigError,
    ModelConfig,
    UnsupportedReasoningEffort,
)
from agent_harness.model.provider import ModelClientConstructionError
from agent_harness.model.provider_store import ProviderStore
from agent_harness.observability import flush_process_sink
from agent_harness.sandbox import (
    SUPPORTED_BACKENDS,
    SandboxUnavailableError,
    WorkspaceRegistry,
    probe_all_capabilities,
)
from agent_harness.session import JsonlSessionStore, SessionEvent
from agent_harness.session.cwd import session_cwd
from agent_harness.session.derive import validate_user_protected_fact_annotations
from agent_harness.session.projects import ProjectService
from agent_harness.session.queue import MessageQueueManager
from agent_harness.session.runmanager import RunManager
from agent_harness.session.service import (
    ARCHIVE_ENTRY_API,
    COMPACT_ENTRY_API,
    PURGE_ENTRY_API,
    ActiveRunConflict,
    AmendOptions,
    ApprovalAlreadyResolved,
    ApprovalQueueMissing,
    ApprovalRequestMissing,
    AttachmentMessageTooLarge,
    AttachmentReferenceInvalid,
    EventLogCorruptError,
    InvalidDecision,
    InvalidSessionId,
    ModelDoesNotSupportImages,
    PendingApprovalConflict,
    ProtectedFactReferenceInvalid,
    QueueItemNotFound,
    ReconcileDecision,
    RecoveryConflict,
    SeqConflict,
    SessionHasChildren,
    SessionNotFound,
    SessionService,
    SteerTargetNotFound,
    SupersedeTargetInvalid,
    TooManyAttachments,
    UnknownModel,
    WorkspaceBindingConflict,
    WorkspaceNameInvalid,
    WorkspaceNotFound,
    validate_session_id,
)
from agent_harness.session.workflow import WorkflowMode
from agent_harness.storage import (
    SqliteCheckpointStore,
    SqliteOperationLedger,
    SqliteSessionMetaStore,
)
from agent_harness.storage.artifact import SESSION_KEY_PATTERN
from agent_harness.storage.checkpoint import checkpoint_save_failure_count
from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger
from agent_harness.storage.sqlite import StorageBusyError
from agent_harness.tooling.approval_queue import PendingApprovalQueue
from agent_harness.tooling.approve_policy import ApprovePolicyStore
from agent_harness.tooling.contract import PermissionPolicy
from agent_harness.transport import SqliteTransportLedger
from agent_harness.web import artifacts
from agent_harness.web import catalog as catalog_router
from agent_harness.web.context_usage import build_context_usage_payload
from agent_harness.web.domain_errors import (
    http_error,
    model_http_error,
    storage_http_error,
    storage_http_status,
)
from agent_harness.web.metrics import METRICS_CONTENT_TYPE, collect_process_metrics
from agent_harness.web.serialization import (
    build_event_payload,
    build_session_event_payload,
    build_truncated_control,
)
from agent_harness.web.wire_safety import _safe_text
from agent_harness.workspace import (
    SqliteLeaseStore,
    SqliteWorkspaceStore,
    WorkspaceIndex,
    WorkspaceLeaseManager,
)
from agent_harness.workspace.lease_paths import (
    LeasePathError,
    normalize_dir_key,
)
from agent_harness.workspace.worktree import WorktreeError, create_worktree

# Read-only catalog facts live with their router. Re-export them here for the
# existing request validators and compatibility imports.
AGENT_PROFILE_DESCRIPTIONS = catalog_router.AGENT_PROFILE_DESCRIPTIONS
CATALOG_ICON_NAMES = catalog_router.CATALOG_ICON_NAMES
CONTEXT_PROVIDER_DESCRIPTIONS = catalog_router.CONTEXT_PROVIDER_DESCRIPTIONS
REASONING_EFFORT_DESCRIPTIONS = catalog_router.REASONING_EFFORT_DESCRIPTIONS
register_catalog_routes = catalog_router.register_catalog_routes

# 自定义响应头清单（#785）：响应侧新增 X- 头必须同步登记进本清单
# （tests/web/test_cors_expose_headers.py 守卫强制）；请求侧读头不入清单。
# 契约范围、机制依据（含 cors.py 出处）与豁免口径见该测试 docstring。
EXPOSED_CUSTOM_RESPONSE_HEADERS: frozenset[str] = frozenset(
    {
        "X-Local-Fuse-Source",
        "X-Local-Max-Agent-Turns",
        "X-Permission-Mode",
        "X-Worktree-Path",
        "X-Worktree-Path-Encoded",
    }
)

# ── Request / Response schemas ──


class _AmendValueValidators(BaseModel):
    """amend 字段的静态取值校验（三个请求体共用一份，ADR-0020b / ADR-0021）。

    ``check_fields=False``：字段声明在子类（CreateSessionRequest / ResumeRequest /
    SendMessageRequest），校验逻辑只写一遍——三个入口对同一份取值集合负责。
    未知 ``context_providers`` id 的判定依赖运行时 wiring，不在这一层
    （见 ``_validate_wired_context_providers``）。
    """

    @field_validator("sandbox_backend", check_fields=False)
    @classmethod
    def _validate_sandbox_backend(cls, v: str | None) -> str | None:
        # #363 / W-19：未知后端名在请求层响亮 422（registry 层 ValueError 是
        # 第二道防线）。None = 未显式选择 ⇒ 部署默认。
        if v is not None and v not in SUPPORTED_BACKENDS:
            valid = ", ".join(SUPPORTED_BACKENDS)
            raise ValueError(f"sandbox_backend must be one of: {valid}")
        return v

    @field_validator("reasoning_effort", check_fields=False)
    @classmethod
    def _validate_reasoning_effort(cls, v: str | None) -> str | None:
        if v is not None and v not in REASONING_EFFORT_DESCRIPTIONS:
            valid = ", ".join(REASONING_EFFORT_DESCRIPTIONS)
            raise ValueError(f"reasoning_effort must be one of: {valid}")
        return v

    @field_validator("agent_profile", check_fields=False)
    @classmethod
    def _validate_agent_profile(cls, v: str | None) -> str | None:
        if v is not None and v not in AGENT_PROFILE_DESCRIPTIONS:
            valid = ", ".join(AGENT_PROFILE_DESCRIPTIONS)
            raise ValueError(f"agent_profile must be one of: {valid}")
        return v

    @field_validator("context_providers", check_fields=False)
    @classmethod
    def _validate_context_providers_shape(
        cls, v: list[str] | None
    ) -> list[str] | None:
        # 只校验形状（每项非空字符串，空 list 合法）。是否已装配由 handler 对照
        # wiring 判定——静态校验没法知道 CAPABILITIES 运行时状态。
        if v is None:
            return v
        for item in v:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    "context_providers entries must be non-empty strings"
                )
        return v


class LocalBudgetRequest(BaseModel):
    """`budget.local` 子对象（#308）：local AgentRuntime fuse 的**请求覆盖**。

    刻意 `extra="forbid"`：未知键响亮失败（422 "Extra inputs are not permitted"）
    而不是被静默忽略——"不静默截断"是 ADR-0044 D1/D8 的明文要求
    （`budget.session` 由 `#318` 的 `SessionBudgetRequest` 承接）。
    """

    model_config = {"extra": "forbid"}

    max_agent_turns: int | None = Field(default=None, ge=1)


class RunBudgetRequest(BaseModel):
    """`budget.run` 子对象（`#312` 建 / `#313` 扩到四维 / `#314` 加 per-tool，`11 §6.1`）。

    `#313` 起四维可用：turns / model_requests / total_tokens / cost_usd；
    `#314` 起 `tool_call_limits` 也可用（工具名 → 正整数绝对 ceiling）。**可执行性**
    不在这一层判（pydantic 只管 JSON 形状）：本链强制不了某个维度时由
    `agent/run_budget.validate_ceiling_enforceability` 在**首个 Provider 请求之前**
    给 422——web 层不重述那条判定（`budget_claims` 只摊平）。

    `deadline_at`（`#315`）：RFC 3339 UTC 绝对时刻（`11 §6.1`）或 `null`。**形状**由领域层
    `parse_deadline_at` 判（本层只声明为字符串，不写第二份规则）：朴素时间 / 非字符串 /
    空串一律 422。一个**已经过去**的时刻不是形状错误——开工给过去时刻等于立刻到点
    （即时暂停），恢复给过去时刻由 `validate_resume` 按 409 拒。

    剩余一维（`max_tool_calls`）**照样声明**，理由是 PRD §3 把公开形状冻结成"全形 + 可空"：
    只设 run 的 PRD 全形请求体是**合法形状**，用 `extra="forbid"` 把
    `max_tool_calls: null` 打成 422 会把合法客户端拒之门外。反过来，给它赋**非空值**
    才是 "客户端以为设了、运行时没实现" —— 那必须 422 而不是静默忽略（ADR-0044 D1/D8），
    所以下面用 `model_validator` 挡下。

    `tool_call_limits` 的**形态**（正整数、工具名非空且无首尾空白）由领域层的
    `parse_tool_call_limits` 判（web 层不写第二份规则）；「工具名已注册」要到装配层
    （`build_runtime` 的注册表）才判得了，两处都在首个 Provider 请求之前 ⇒ 不违反
    `11 §6.1` 的"无副作用"。
    """

    model_config = {"extra": "forbid"}

    max_agent_turns_total: int | None = Field(default=None, ge=1, le=INT64_MAX, strict=True)
    max_model_requests: int | None = Field(default=None, ge=1, le=INT64_MAX, strict=True)
    max_total_tokens: int | None = Field(default=None, ge=1, le=INT64_MAX, strict=True)
    #: 成本 ceiling：非负十进制（`11 §6.1`）。wire 上字符串最稳、数也收——
    #: **二进制浮点相等不是契约**，所以这一维在事件与投影里一律是十进制字符串，
    #: 算术只在 `Decimal` 里做（见 `agent/run_budget.py` 的 `_decimal_text`）。
    max_cost_usd: Decimal | None = Field(default=None, ge=0)
    #: 工具名 → 正整数绝对 ceiling（`#314` / `04 §9.1`；`#564`：两作用域口径统一）。
    #: 值用 `StrictInt`：pydantic 2.13 的 `Field(strict=True)` 只约束 dict 本身、
    #: **不级联到值类型**（"3"→3 / true→1 仍被 lax 强制），StrictInt 才逐值拒绝
    #: 字符串与布尔——与领域层 `parse_tool_call_limits` 同口径（对齐 #548 C3）。
    tool_call_limits: dict[str, StrictInt] | None = Field(default=None, strict=True)
    #: 绝对截止时刻（`#315` / `11 §6.1`）：RFC 3339 UTC 文本或 `null`（= 不设）。
    #: 声明为 `str`：wire 上的时刻是文本，解析与归一化到 UTC 由领域层
    #: `parse_deadline_at` 一处完成（朴素时间 / 空串 / 非字符串在那里 422）。
    deadline_at: str | None = None
    # `max_tool_calls`：`null`（PRD 的"没设"字面量）合法但无效，非空 ⇒ 422。
    max_tool_calls: int | None = None

    @field_validator(
        "max_agent_turns_total",
        "max_model_requests",
        "max_total_tokens",
        mode="before",
    )
    @classmethod
    def _ceiling_must_be_integer(cls, value: Any) -> Any:
        """`#548` C3 裁决（2026-10-03）：`strict=True` **不放松**，只把拒绝文案说清。

        判据必须是「整数」而不是「能转成整数的数」：JSON 里 `100.0` 与 `100` 是两种
        输入，`strict` 只收后者。改前浮点落的是英文 `Input should be a valid integer`，
        调用方看不出该传什么。

        用 ``PydanticCustomError("int_type", …)`` 而非 ``ValueError``：**保住 wire 上的
        `type` 值**（仍是 `int_type`）且不带 pydantic 的 ``"Value error, "`` 前缀
        ⇒ 对调用方而言只有 `msg` 变、其余字段逐字不变。`None` 照常放行（"不设上限"）。

        与 `SessionBudgetRequest` 的同名校验器**规则必须逐字一致**；
        `tests/web/test_budget_int64_bounds_api.py` 以 `parametrize` 把两个模型钉在
        同一组期望上 ⇒ 单边漂移当场变红。
        """
        if value is None or (isinstance(value, int) and not isinstance(value, bool)):
            return value
        raise PydanticCustomError(
            "int_type",
            'ceiling 必须是整数（不接受小数 100.0 / 布尔 / 字符串 "100"），请传 100',
        )

    @model_validator(mode="after")
    def _reject_unimplemented_dimensions(self) -> RunBudgetRequest:
        if self.max_tool_calls is not None:
            raise ValueError(
                "budget.run.max_tool_calls 尚未实现（#313 实现了 turns / model_requests / "
                "total_tokens / cost_usd；#314 实现了 tool_call_limits；"
                "#315 实现了 deadline_at；总量 tool 配额见后续票）；"
                "收到 max_tool_calls 不静默忽略，请去掉它"
            )
        return self


class SessionBudgetRequest(BaseModel):
    """`budget.session` 子对象（`#318`）：跨 run 持久的 SessionBudget 声明。

    各维与 `RunBudgetRequest` 同形同判（`11 §6.1` 的 session 作用域）：turns /
    model_requests / total_tokens / cost_usd / deadline_at / per-tool；`max_delegations`
    是整棵会话树的委派上限（正整数；未点名沿用域默认 8，`10 §5.1`）。可执行性 /
    形态细则都由领域层 `session_limits_from_request` 一处判（web 层只摊平）。

    `expected_version` 是**这条 durable 账行**的 CAS 版本（`03 §3.4` 的乐观锁，
    真源 = `session_budgets.version`）。它与顶层的 `budget.expected_version`（run
    账的 CAS）是两把锁、两个计数器：session 账跨 run 存续，所以在**新建会话以外的
    一切入口**都可能合法出现——点名了 `budget.session.*` 的恢复/续聊必须带它
    （缺失 422），版本不符 / ceiling 低于已消耗 / headroom 不足 409（账本单事务，
    零副作用）。新建会话的账行必然不存在，这里带版本是矛盾请求 ⇒ 422
    （`budget_claims` 的 `create_surface` 挡）。
    """

    model_config = {"extra": "forbid"}

    max_agent_turns_total: int | None = Field(default=None, ge=1, le=INT64_MAX, strict=True)
    max_model_requests: int | None = Field(default=None, ge=1, le=INT64_MAX, strict=True)
    max_total_tokens: int | None = Field(default=None, ge=1, le=INT64_MAX, strict=True)
    max_cost_usd: Decimal | None = Field(default=None, ge=0)
    #: 工具名 → 正整数绝对 ceiling（`#314` / `04 §9.1`；`#564`：两作用域口径统一）。
    #: 值用 `StrictInt`：pydantic 2.13 的 `Field(strict=True)` 只约束 dict 本身、
    #: **不级联到值类型**（"3"→3 / true→1 仍被 lax 强制），StrictInt 才逐值拒绝
    #: 字符串与布尔——与领域层 `parse_tool_call_limits` 同口径（对齐 #548 C3）。
    tool_call_limits: dict[str, StrictInt] | None = Field(default=None, strict=True)
    deadline_at: str | None = None
    max_delegations: int | None = Field(default=None, ge=1, le=INT64_MAX, strict=True)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator(
        "max_agent_turns_total",
        "max_model_requests",
        "max_total_tokens",
        "max_delegations",
        mode="before",
    )
    @classmethod
    def _ceiling_must_be_integer(cls, value: Any) -> Any:
        """C3 裁决：与 `RunBudgetRequest._ceiling_must_be_integer` **规则逐字一致**。

        多一个字段名（`max_delegations`，F3 已定「与其余整数 ceiling 同一上界」），
        规则本身不重述第二遍。两处一致性由 `tests/web/test_budget_int64_bounds_api.py`
        的 `parametrize` 钉住。
        """
        if value is None or (isinstance(value, int) and not isinstance(value, bool)):
            return value
        raise PydanticCustomError(
            "int_type",
            'ceiling 必须是整数（不接受小数 100.0 / 布尔 / 字符串 "100"），请传 100',
        )


class BudgetRequest(BaseModel):
    """请求体里的可选 `budget` 对象（`11 §6.1` 的公开形状）。

    作用域就位情况（谁实现谁加）：`local`（#308）、`run`（#312）、`session`
    （`#318`）——三个作用域全部就位，`extra="forbid"` 只挡真正的未知键。
    """

    model_config = {"extra": "forbid"}

    #: 预算版本（CAS，`03 §3.4`）：同 run 恢复暂停预算时**必填**（客户端必须证明自己
    #: 看到的暂停是当前的那一份），首次创建省略。它是"这次预算变更"的属性、
    #: 不是某个作用域的 ceiling，所以按 PRD §3 的冻结形状与 `local`/`run`/`session`
    #: **平级**——别挪进 `run` 里。session 账行的 CAS 版本**不**复用这一位：
    #: 两把锁对应两个持久对象，见 `SessionBudgetRequest.expected_version`。
    expected_version: int | None = Field(default=None, ge=1)
    local: LocalBudgetRequest | None = None
    #: #422：`run` 是启动 run 的请求上的 per-run 绝对上限（`run/started.data.budget`
    #: 是它唯一的宿主）。`launch=false` 的只建会话没有 run 可挂 ⇒ 非空 `run` 一律 422。
    #: 对照：`session` 在 launch=false 合法（session 账行就是宿主，#318）；`local` 是
    #: 请求级熔断，launch=false 下不投影（`_local_fuse_headers`）。
    run: RunBudgetRequest | None = Field(
        default=None,
        description=(
            "per-run 绝对上限；仅随启动 run 的请求（launch=true）合法。"
            "launch=false（只建会话不启动）时传非空 run → 422（#422）"
        ),
    )
    session: SessionBudgetRequest | None = None


def budget_claims(
    budget: BudgetRequest | None,
    *,
    resume_surface: bool = False,
    create_surface: bool = False,
) -> dict[str, Any]:
    """把请求体的预算声明摊平成领域入口的关键字参数。

    刻意**只摊平、不判定**语义：越权的规则住在
    `agent_harness.agent.budget.resolve_local_fuse`，暂停恢复的 CAS 与 ceiling 判定
    住在 `agent_harness.agent.run_budget.validate_resume`（各自单一规则来源，HTTP 与
    WS 共用同一份判定，web 层不再解释一遍）。迁移期 alias `max_steps` 已随 #320
    移除（模型 `extra="forbid"` 挡未知字段），本函数不再有第二条摊平输入。

    `resume_surface` 是**形状级**的唯一例外：`budget.expected_version` 只在
    "更新已持久化的暂停预算"时有意义（PRD §3）。创建会话与投递消息都会启动**新**
    run——没有可比较的版本，所以那两个入口收到它就是矛盾请求 ⇒ 422（交给
    `BudgetRejection` 走既有 422 映射），不静默丢掉一个客户端明确表达过的意图。

    `create_surface`（`#318`）同理只管 `budget.session.expected_version`：session
    账行随首个 run 才建出，**新建**会话没有可比较的行版本 ⇒ 422；其余入口
    （/resume、/messages、WS）都面向已存在会话，session CAS 合法（它是跨 run
    的 durable 行，与 run 账的"仅暂停恢复面"不同）。
    """
    local = budget.local if budget is not None else None
    run = budget.run if budget is not None else None
    session = budget.session if budget is not None else None
    claims: dict[str, Any] = {
        "local_max_agent_turns": local.max_agent_turns if local is not None else None,
        # `#312` 一维 + `#313` 三维：**只摊平**（四个领域参数名与 `budget.run.*`
        # 一一对应）。形态由 pydantic 挡，可执行性由领域层
        # `validate_ceiling_enforceability` 在首个 Provider 请求之前挡
        # （生产链 reports_cost=False ⇒ 显式 max_cost_usd 恒 422——那是规格要求的
        # 诚实行为，不是缺陷；见 ADR-0044 D2 第 3 条 + D9 的 422 清单）。
        "run_max_agent_turns_total": (
            run.max_agent_turns_total if run is not None else None
        ),
        "run_max_model_requests": (
            run.max_model_requests if run is not None else None
        ),
        "run_max_total_tokens": run.max_total_tokens if run is not None else None,
        "run_max_cost_usd": run.max_cost_usd if run is not None else None,
        # `#315`：绝对截止时刻（RFC 3339 UTC 文本）。形态由领域层 `parse_deadline_at`
        # 判（本层只摊平）；"已过去"不是 422——开工给过去时刻= 立刻到点，恢复给过去
        # 时刻由 `validate_resume` 按 409 拒（`11 §6.1` 的 422/409 分界）。
        "run_deadline_at": run.deadline_at if run is not None else None,
        # `#314`：per-tool 绝对配额（工具名 → 正整数）。形态由领域层
        # `parse_tool_call_limits` 判（本层只摊平），"名字已注册"由装配层判
        # （`validate_tool_call_limits_registered`）——两处都在首个 Provider 请求之前。
        "run_tool_call_limits": run.tool_call_limits if run is not None else None,
        # `#318`：session 作用域（跨 run 持久账行）。同名一一对应，判定全在领域层
        # （`session_limits_from_request` 422 / 账本 CAS 409）——本层只摊平。
        "session_max_agent_turns_total": (
            session.max_agent_turns_total if session is not None else None
        ),
        "session_max_model_requests": (
            session.max_model_requests if session is not None else None
        ),
        "session_max_total_tokens": (
            session.max_total_tokens if session is not None else None
        ),
        "session_max_cost_usd": session.max_cost_usd if session is not None else None,
        "session_deadline_at": session.deadline_at if session is not None else None,
        "session_tool_call_limits": (
            session.tool_call_limits if session is not None else None
        ),
        "session_max_delegations": (
            session.max_delegations if session is not None else None
        ),
    }
    expected = budget.expected_version if budget is not None else None
    if expected is not None:
        if not resume_surface:
            raise BudgetRejection(
                "budget.expected_version 只在恢复暂停 run 时才有意义"
                "（本入口会启动新 run，没有可比较的预算版本）；请去掉它"
            )
        claims["expected_version"] = expected
    session_expected = session.expected_version if session is not None else None
    if session_expected is not None:
        if create_surface:
            raise BudgetRejection(
                "budget.session.expected_version 在新建会话上没有意义"
                "（session 账行随首个 run 才建出，没有可比较的版本）；请去掉它"
            )
        claims["session_expected_version"] = session_expected
    return claims


def ws_budget_claims(msg: dict[str, Any]) -> dict[str, Any]:
    """WS `send_message` 帧里的 budget（#308）：与 HTTP 入口同一套字段与规则。

    WS 帧是裸 dict（没有 pydantic 模型做解析），所以形状校验在这里显式做：
    **未知键 / 非法形状当场拒绝**（`ValueError` → 错误帧），不静默丢弃——
    静默忽略等于让客户端以为设了预算。三个作用域（`local` / `run` / `session`）
    都由 `BudgetRequest` 承接（`budget.session` 自 `#318` 起就位）。

    `expected_version` 走与 HTTP 消息入口同一条规则（`resume_surface=False`）：
    WS 帧只能投递消息（不是恢复面），带版本是矛盾请求 ⇒ 错误帧。

    `budget.local.max_agent_turns` 的**语义**判定（越权 422）仍在领域层
    `resolve_local_fuse`，本函数只摊平。迁移期 alias `max_steps` 已随 #320 移除：
    WS 帧带它按未知键拒绝（与 HTTP 请求模型的 `extra="forbid"` 同一纪律）。
    """
    raw = msg.get("budget")
    budget: BudgetRequest | None = None
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError("budget must be an object")
        try:
            budget = BudgetRequest.model_validate(raw)
        except ValidationError as error:
            first = error.errors()[0] if error.errors() else {}
            where = ".".join(str(part) for part in first.get("loc", ()))
            raise ValueError(f"budget 形状非法：{where} {first.get('msg', '')}".strip()) from error
    if "max_steps" in msg:
        raise ValueError("max_steps 不再被接受：请改用 budget.local.max_agent_turns（#320）")
    try:
        return budget_claims(budget)
    except BudgetRejection as error:
        # WS 层只回错误帧（同一个 except 通道），把领域异常翻成帧文本。
        raise ValueError(str(error)) from error


class CreateSessionRequest(_AmendValueValidators):
    """POST /api/sessions 的请求体。"""

    # 迁移期 alias `max_steps` 已由 #320 移除（`02 §5.1` contract 收口）。本模型
    # 刻意 `extra="forbid"`：旧客户端发 `max_steps` 必须按**未知字段响亮 422**，
    # 不允许静默忽略（静默 = 旧客户端以为设了保险丝）。前端三个入口只发已声明
    # 键（`web/src/lib/api.ts` 的字段表逐键对照过），收紧不影响受支持调用方。
    model_config = {"extra": "forbid"}

    # #204：task 从"必填"变为"可选"——**刻意放宽，不是偷偷放宽**。空会话入口
    # （"在项目中新建任务"弹窗）只需要"创建文件 + 设好默认权限"，然后在 chat
    # 输入框里发第一条消息；此前的契约把 task 锁成必填，正是该弹窗做不出来的
    # 原因（裁定 §2 原文）。语义：launch=true（默认）时 task 仍必填（由下方
    # handler 校验，422 不变）；launch=false 时 task 可省略——给了 task 又
    # launch=false 是矛盾组合（给了任务却静默不执行）⇒ 422。纯空白 task 容忍
    # （runtime 侧无意义但不危险）；max_length 封顶原因不变：task 会逐字持久化
    # 进 JSONL（user/message）并整体进模型上下文。R5 显式规则标记只接受非空白 task。
    task: str | None = Field(default=None, min_length=1, max_length=100_000)
    # #298 R5：仅用户显式勾选时，Runtime 才把该真实输入作为单事件规则证据。
    remember_as_procedural_rule: bool = False
    workspace: str | None = None  # None → 用默认 workspace；只接受单段目录名（校验在 SessionService._validate_workspace_name，路径形态走 POST /api/projects）
    # ADR-0027 / #169：任意**已存在**的绝对目录，会话直接以它为操作目录（不创建、
    # 不复制），并自动注册为项目 + 归组。与 `workspace` 互斥（同时非空 → 422）。
    # 形态/存在性/是否目录的校验在 SessionService._resolve_cwd（领域层，与 CLI 共用）。
    cwd: str | None = None
    # local fuse（#308）：公共字段 `budget.local.max_agent_turns`（迁移期 alias
    # `max_steps` 已随 #320 移除；规则见 `agent/budget.py`，这里只挡形状：非正数 → 422）。
    #
    # 这里刻意**没有** `le=` 上限：生效上限 = min(Deployment, 档位声明)（判定见
    # `agent/budget.py`）；一个与策略无关的数字只会造成两种假象——"200 以内随便传"与
    # "策略允许 500 却被 200 挡住"。
    budget: BudgetRequest | None = None
    # Phase 5：permission_mode 是会话级「审批阈值」声明（不是硬墙）。三档真实
    # PermissionPolicy；未知值 → 422。permission_mode 决定 ToolExecutor 的 policy
    # 上限，审批本身仍走 ApprovalCallback（默认 auto-approve）。
    permission_mode: str = Field(default="workspace-write")
    # auto_approve 保留为 deprecated alias（向后兼容）：true ≡ workspace-write
    # + auto-approve callback；false ≡ workspace-write + 交互式审批（#423）。
    # 两者同传时 permission_mode 优先。#358（W-14，D2 默认翻转）：缺省从 True 改为
    # False——两个字段都缺省 → workspace-write + ask（读免问、写/Bash 逐次问）；
    # 显式 auto_approve=true 仍走 auto-approve（向后兼容）。
    auto_approve: bool = False
    # #363 / W-19：显式选择的 sandbox 后端（"local" | "docker"）；None ⇒ 部署默认
    # （当前 "local"，向后兼容）。未知值 → 422（字段校验器响亮拒绝）。
    # docker 不可用 → 409 SandboxUnavailableError（结构化诊断，绝不静默降级）。
    sandbox_backend: str | None = None
    # amend contract fields（Phase 5 staged → RUNTIME 子批次全部消费：reasoning_effort
    # / agent_profile / context_providers）
    reasoning_effort: str | None = None
    agent_profile: str | None = None
    context_providers: list[str] | None = None

    # 会话级模型选择（ADR-0016 §5，C6）：None = 默认链（现行为不变）；
    # 命名 = AGENT_MODELS catalog 条目，未知名字 422。fallback 链不受影响。
    model: str | None = None

    # #367 / W-23 选项 A：三档自主度（抄 Copilot Interactive/Plan/Autopilot ≈
    # Codex 三档 ≈ Claude Code modes）。None = 不声明（沿用 auto_approve /
    # permission_mode 既有语义，向后兼容）；显式声明则优先于 auto_approve：
    # - "ask"：逐次问（auto_approve=false 显式）
    # - "plan"：先计划后执行（workflow_mode="plan"，创建后切换）
    # - "auto"：全自动（auto_approve=true 显式）
    autonomy: str | None = Field(default=None)
    # #367 / W-23 选项 A：目录冲突策略（六家成熟产品共识：任务级排队不存在，
    # 冲突→自动 worktree 并行隔离）。"worktree"（默认）：cwd 被占时自动建
    # worktree 并用其路径建会话；"queue"：走既有租约排队（次选项）。
    on_conflict: str = Field(default="worktree")

    @field_validator("autonomy")
    @classmethod
    def _validate_autonomy(cls, v: str | None) -> str | None:
        if v is None:
            return v
        valid = {"ask", "plan", "auto"}
        if v not in valid:
            raise ValueError(
                f"autonomy must be one of {sorted(valid)}; got {v!r}"
            )
        return v

    @field_validator("on_conflict")
    @classmethod
    def _validate_on_conflict(cls, v: str) -> str:
        valid = {"worktree", "queue"}
        if v not in valid:
            raise ValueError(
                f"on_conflict must be one of {sorted(valid)}; got {v!r}"
            )
        return v

    @field_validator("permission_mode")
    @classmethod
    def _validate_permission_mode(cls, v: str) -> str:
        valid = {p.value for p in PermissionPolicy}
        if v not in valid:
            raise ValueError(
                f"permission_mode must be one of {sorted(valid)}; got {v!r}"
            )
        return v


class ModelChangeRequest(BaseModel):
    """POST /api/sessions/{id}/model 的请求体（T7 #137，PRD §2.3）。

    provider 必须与 catalog 条目一致；model_id 命中条目名或上游模型名。
    """

    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)


class PermissionChangeRequest(BaseModel):
    """POST /api/sessions/{id}/permission 的请求体（F18-A #282，ADR-0041 §2 D1/D2）。

    两者**都无默认值**：漏传即 422——否则缺省值会悄悄把批准策略翻掉。合法档位由领域层
    校验（``InvalidDecision`` 422），故此处不加 ``field_validator``（规则单一来源）。
    """

    permission_mode: str = Field(min_length=1)
    auto_approve: bool


class RecoverDecisionRequest(BaseModel):
    """POST /api/sessions/{id}/recover 的单条用户裁决（#547 四裁决合同）。

    ``verdict`` 用字符串承载，合法值由领域层校验（``InvalidDecision`` 422）——
    与 PermissionChangeRequest 同一条纪律：规则单一来源，传输层不复述。

    ``source``（#357 W-13 契约 3）：用户来源自陈（如「我查了外部系统」），
    可选——省略 = 合法（#547 形状零迁移），有值时逐字进 ``reconcile_meta``。
    上限 2000 **字符**，与 ``ApproveRequest.reason`` 同口径（防重复放大）。
    """

    tool_call_id: str = Field(min_length=1)
    verdict: str = Field(min_length=1)
    source: str | None = Field(default=None, max_length=2000)


class RecoverRequest(BaseModel):
    """POST /api/sessions/{id}/recover 的可选请求体（#547 裁决合同）。

    省略 body（或空 ``decisions``）＝纯恢复尝试：有 UNKNOWN 行时 409，
    ``detail`` 附机器可读 ``pending_decisions`` 清单指引裁决；携带覆盖全部待
    裁决行的 ``decisions`` 才开工（预检在领域层，被拒请求零写入）。
    """

    decisions: list[RecoverDecisionRequest] = Field(default_factory=list)


class ApproveRequest(BaseModel):
    """POST /api/sessions/{id}/approve 的请求体（Phase 5 + Batch 5.1）。

    decision 是 spec 契约（03 §9 PermissionDecision）；approved 是兼容字段。
    两者都传时 decision 优先；只传 approved 时从它推导（True→approve_once，
    False→deny）。decision 必须命中 requested 事件里 allowed_decisions。

    `policy_granularity`（#684 第三档「以后都允许」）：仅当
    ``decision=approve_policy`` 时有意义，取值 ``exact`` / ``command``；缺省
    由领域层归一为 ``exact``。非法值 → 422（与非法 decision 同口径，不静默回落）。
    """

    approval_id: str | None = None
    approved: bool = True
    decision: str | None = None
    # #684：第三档「以后都允许」的安装粒度（exact / command）。Shape-only——
    # 合法性判定在领域层 `SessionService.resolve_approval`（单一规则来源）。
    policy_granularity: str | None = None
    # #562 F6：reason 逐字写入 permission/resolved 事件日志，自由文本无上限 =
    # 每次授权可重复放大（实测 50KB 原样入库）。上限 2000 **字符**（pydantic
    # ``max_length`` 计的是 Python ``str`` 码点数，不是 UTF-8 字节——2000 个中文
    # = 6000 字节仍合法）。超限经安全 422 出口拒绝，不回显原文（recon 核查：
    # 前端 ``postApproval`` 根本不发 ``reason`` ⇒ 零误伤）。
    reason: str = Field(default="", max_length=2000)


class InputRequestAnswer(BaseModel):
    model_config = {"extra": "forbid"}

    request_id: str = Field(min_length=1, max_length=200)
    choice: Literal[
        "replace_persistently", "current_task_only", "keep_existing", "custom",
    ]
    custom_text: str | None = Field(default=None, min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def validate_custom_text(self) -> InputRequestAnswer:
        if self.choice == "custom":
            if self.custom_text is None or not self.custom_text.strip():
                raise ValueError("custom choice requires non-blank custom_text")
        elif self.custom_text is not None:
            raise ValueError("custom_text is only allowed for custom choice")
        return self


class ResumeRequest(_AmendValueValidators):
    """POST /api/sessions/{id}/resume 的请求体。

    **两种恢复形态（#312）**，按 `task` 是否存在区分：

      * 给了 `task`——既有语义，逐字不变：把这段文本当**新任务**续聊（新
        user/message + 新 run_id）。此时再带 `run_id` / `resume_basis` /
        `budget.expected_version` 是矛盾组合 ⇒ 422（判定在领域层，不静默选一边）。
      * 不给 `task`——**同 run 续跑**（暂停恢复）：必须带被暂停的 `run_id`、
        `resume_basis`，以及 `budget.run.max_agent_turns_total` + `budget.expected_version`
        （绝对 ceiling + CAS 版本）。缺声明 ⇒ 422；状态对不上（版本过期 / 不是暂停
        的那个 run / ceiling 没真提高 / 有在途 run）⇒ 409，且零副作用。
    """

    # 与 CreateSessionRequest 同款 `extra="forbid"`（#320：alias `max_steps` 移除后，
    # 旧客户端发它必须 422，规则全局一致——`02 §5.1` D8 的恢复面也不例外）。
    model_config = {"extra": "forbid"}

    task: str | None = Field(default=None, min_length=1, max_length=100_000)
    remember_as_procedural_rule: bool = False
    # 同 run 续跑的三个声明（同形恢复面，PRD §9）。合法值集合 / CAS 比较都在领域层
    # `agent_harness.agent.run_budget.validate_resume`（单一规则来源）——这里只挡形状。
    run_id: str | None = Field(default=None, min_length=1)
    resume_basis: str | None = Field(default=None, min_length=1)
    input_request: InputRequestAnswer | None = None
    # local fuse（#308 / `11 §6.1`：显式恢复也接受 budget）。
    budget: BudgetRequest | None = None
    # staged amend 字段（可选，None = 默认行为）
    reasoning_effort: str | None = None
    agent_profile: str | None = None
    context_providers: list[str] | None = None
    model: str | None = None

    @model_validator(mode="after")
    def validate_procedural_rule_requires_task(self) -> ResumeRequest:
        if self.remember_as_procedural_rule and (
            self.task is None or not self.task.strip() or self.run_id is not None
        ):
            raise ValueError(
                "remember_as_procedural_rule requires a non-blank new task, not same-run resume"
            )
        if self.input_request is not None and (
            self.task is not None or self.resume_basis != "user_input"
        ):
            raise ValueError("input_request requires same-run resume_basis=user_input")
        if self.resume_basis == "user_input" and self.input_request is None:
            raise ValueError("resume_basis=user_input requires input_request")
        return self


class ProtectedFactAnnotation(BaseModel):
    """Explicit fact metadata; the server binds it to the accepted user event."""

    model_config = {"extra": "forbid"}

    fact_type: Literal[
        "user_instruction",
        "user_goal",
        "constraint",
        "authorization",
        "acceptance_criterion",
        "exact_identifier",
        "confirmed_decision",
        "task_progress",
    ]
    value: str = Field(min_length=1, max_length=10_000)
    supersedes_fact_id: str | None = Field(default=None, min_length=1, max_length=200)


#: `attachments` 的静态兜底上限（#823 / MM-02，A3/B7）。取部署上限键
#: `attachment_max_images_per_message` 的**默认值**（`ImageAttachmentLimits` 同源，
#: 不硬编码魔法数）——只是 pydantic 层的最后一道拒绝面，防止 1 MiB body 里塞进成千
#: 上万条合法 id 触发等量 `load_bytes`+`detect_image`（同步/IO 放大 + 巨量引用）。
#: **完整聚合上限**（按部署配置的数量/总字节，`_resolve_attachment_refs` 逐条之外的
#: 聚合校验）归 MM-03。
_MAX_ATTACHMENTS_PER_MESSAGE = Settings.model_fields[
    "attachment_max_images_per_message"
].default


class SendMessageRequest(_AmendValueValidators):
    """POST /api/sessions/{id}/messages 的请求体（PRD §5.3 续聊入口）。

    ``mode`` 取自 PRD 锁定决策 D-7：

      * ``queue``（默认）——空闲 → 直接拉起新 run；在途 → 入队等待
        （不抢断不丢消息，下个 run 自然消费）。
      * ``steer``——仅在途 run 时合法：注入引导请求，被当前 step 边界
        的 run 读取（不重启 run、不改写历史事件）。

    编辑语义（ADR-0030 §4.4，两个可选字段，默认 None = 现有行为逐字不变）：

      * ``supersedes_seq``——取代 seq 为它的那条 user/message **及其整轮**（只
        影响模型可见投影与界面，历史事件照旧保留）。目标必须是本会话最新一条
        非注入用户消息，否则 409。与 ``mode`` 正交：取代之后新内容按 mode 投递。
      * ``queue_id``——本条内容**替换**某条排队项：旧项被取消
        （``queue/cancelled``），新内容按 ``mode`` 重新投递。
    """

    content: str = Field(min_length=1, max_length=100_000)
    mode: str = Field(default="queue", pattern="^(queue|steer)$")
    # local fuse（#308）：与创建端点同一套字段与规则。生效值只有 idle → launched
    # 消费；queued / steer 只判定、丢弃（`11 §6.1`——判定照跑是为了让状态码与运行态无关）。
    # 与 CreateSessionRequest 同款 `extra="forbid"`（#320：alias 移除后统一响亮 422）。
    model_config = {"extra": "forbid"}
    budget: BudgetRequest | None = None
    supersedes_seq: int | None = Field(default=None, ge=0)
    queue_id: str | None = None
    revoke_fact_id: str | None = Field(default=None, min_length=1, max_length=200)
    refutes_event_id: str | None = Field(default=None, min_length=1, max_length=200)
    remember_as_procedural_rule: bool = False
    # staged amend 字段（可选，None = 默认行为）
    reasoning_effort: str | None = None
    agent_profile: str | None = None
    context_providers: list[str] | None = None
    model: str | None = None
    protected_facts: list[ProtectedFactAnnotation] = Field(
        default_factory=list, max_length=32
    )
    # #823 / MM-02：附件 id 列表（内容寻址 `sha256:<hex>`）。默认空 = 纯文本，既有
    # 行为逐字不变；服务端逐条校验"存在且属本会话"，不合法 → 422。
    # A3/B7：`max_length` 是 pydantic 层兜底（取值见 `_MAX_ATTACHMENTS_PER_MESSAGE`），
    # 完整聚合上限（部署可配的数量/总字节）归 MM-03。
    attachments: list[str] = Field(
        default_factory=list, max_length=_MAX_ATTACHMENTS_PER_MESSAGE
    )

    @model_validator(mode="after")
    def validate_protected_facts_match_user_text(self) -> SendMessageRequest:
        if self.remember_as_procedural_rule and not self.content.strip():
            raise ValueError("remember_as_procedural_rule requires non-blank content")
        validate_user_protected_fact_annotations(
            self.content,
            [fact.model_dump(exclude_none=True) for fact in self.protected_facts],
        )
        return self


class WorkspaceRef(BaseModel):
    """会话摘要里的项目引用（WS-3 / #153 AC2）：`id` 做请求/重命名，`title` 做显示。

    形状由实现者定、但**必须写死在契约里**（票面 AC2）；前端 `types.ts::WorkspaceRef`
    用同一个形状做编译期锁。
    """

    id: str
    title: str


class SessionSummary(BaseModel):
    """GET /api/sessions 返回的单条摘要。"""

    session_id: str
    event_count: int
    first_event_time: str | None = None
    last_event_time: str | None = None
    # Gap 3 (P0)：首条 user/message content 截断 128 字符——前端 SessionList
    # 零额外请求渲染标题（保留 events 扫描作为后端未返回时的降级路径）。
    first_user_message: str | None = None
    # Gap 2 (P2)：Langfuse trace 关联。OBS-010 起**真实回填**：**末事件恰为
    # run 终结事件**（run/completed|failed|interrupted）时取其 trace_id；末事件
    # 非终结（run 在途，或上一轮已完成后新轮的 user/message/run-started 垫在末尾）
    # 或未配置可观测性时为 null——绝不伪造，前端显示「未追踪」。
    # 有意只认末事件（不回溯）以保住列表页快路径，取舍见
    # `JsonlSessionStore._terminal_trace_field`；不变量 #21：可观测性缺席
    # 不致命也不造假。
    trace_id: str | None = None
    # ARCH-4b：与 `trace_id` 同源（同一个 run 终结事件、同一套守卫）的可点击
    # Langfuse URL（契约 2d7f87a / ADR-0018 D7）。前端 `types.ts::SessionSummary`
    # 把该字段声明为**非可选** `string | null`——本字段存在即让那条声明为真。
    trace_url: str | None = None
    # WS-3 / #153：会话所属项目；未分组（历史遗留 / 未命名 workspace / 装配里没有
    # workspace 索引）为 `None`，**绝不伪造**（不变量 #21 同族）。
    #
    # 刻意**不给默认值**：给了默认值的话，将来某个构造点漏传 `workspace=` 会静默
    # 变成"未分组"——那是一条假事实。必填 → 构造响应时漏传**响亮失败**。
    # 注意本字段锁住的是**构造点**，不是"服务层忘了回填"：领域层
    # `SessionSummaryStats.workspace` 仍有 `= None` 默认值，服务层漏查映射照样会
    # 序列化成 null。真正抓漏映射的是断言**值**的测试
    # （`tests/web/test_session_list_workspace.py::test_rows_carry_real_workspace_and_ungrouped_is_null`）。
    workspace: WorkspaceRef | None
    # #171：是否已归档——前端「已归档」徽标的**唯一**来源（`?include_archived=true`
    # 时那些行必须能被认出来）。与 `workspace` 同样**刻意不给默认值**：默认 `False`
    # 会让漏映射的构造点把"已归档"谎报成未归档，徽标静默消失（假事实，不变量 #21 同族）。
    archived: bool
    # #752：事件日志是否损坏（零可解析事件但有损坏行）。损坏是可观测状态，
    # 不是"不存在"——前端据此渲染损坏徽标并引导至恢复入口。与 `archived`
    # 同样刻意不给默认值，漏映射响亮失败。
    corrupted: bool


class SessionArchived(BaseModel):
    """`POST/DELETE /api/sessions/{id}/archive` 的成功响应（#171）。

    形状就是领域动作本身：`{id, archived}`——两个动词各自只表达一个终态，
    `archived` 是**动作后**的真值（幂等：重复归档仍是 `true`）。
    """

    id: str
    #: 动作后的状态。归档可逆，没有"半归档"可表达，所以就是一个布尔。
    archived: bool


class SessionToolLimitsPurged(BaseModel):
    """`POST /api/sessions/{id}/budget/purge-stale-tools` 的成功响应（#616）。

    形状就是存储层结果 `SessionToolLimitsPurge` 的四字段透传：`purged`（被清名 →
    原 ceiling）、`remaining`（清后的 `tool_call_limits` 表）、`rows`（受影响账行数，
    无变更 = 0）、`version`（清后的账行版本，无变更 = 清前版本）。幂等：重跑无陈旧名
    仍 200，`purged={}`、`rows=0`、`version` 不变。
    """

    purged: dict[str, int]
    remaining: dict[str, int]
    rows: int
    version: int


class SessionContextCompacted(BaseModel):
    """`POST /api/sessions/{id}/context/compact` 的成功响应（#635）。

    形状是服务层结果 `SessionContextCompaction` 的字段透传（`dry_run` 不在 Web
    契约里——端点只走真实压缩）：`bracket_id` 指向新落的 bracket；低水位
    （无可压缩早期轮 / **写前**校验闸门未过）时 `bracket_id=null`、
    `compacted_turn_count=0`，**仍 200 且零写入**（"没有可压的"不是错误）。
    **写后**复核失败（bracket 已落盘）不返回该形状——服务层抛
    `CompactionPostWriteError`，端点不捕获 → 500（fail-closed，不谎报"未改动"）。
    `tokens_before/after` 是 messages-only 投影估算，同一口径可直接相减展示。
    """

    bracket_id: str | None
    source_seq_start: int | None
    source_seq_end: int | None
    tokens_before: int
    tokens_after: int
    compacted_turn_count: int
    summary_model: str | None


class SessionDeleted(BaseModel):
    """`DELETE /api/sessions/{id}` 的成功响应（#172 / ADR-0029）。

    与项目软删除的 `ProjectDeleted` 刻意不同形：那个要带 `sessions_detached` 与
    `detail`（向用户解释"会话没被删"），这里 `deleted=True` 就是字面意思——**东西没了**。
    """

    id: str
    #: 走到 200 就一定是真删了（不存在 → 404、形态非法 → 422、状态冲突 → 409），
    #: 所以这里没有"半删"可表达。字面量 `True` 让这件事在 schema 里就成立。
    deleted: Literal[True] = True
    #: 删除前日志里的事件条数。前端用它写确认回执（"已删除 N 条事件"）。
    events: int
    #: 本次从多少个项目的账本里摘掉了它（正常 0/1）。
    detached_from_projects: int


class ApprovalGrantRevoked(BaseModel):
    """`POST /api/sessions/{id}/approvals/revoke` 的成功响应（#526 A2）。

    形状就是领域动作本身：`{id, approval_key, revoked}`——`revoked` 是**动作后**
    的真值（幂等：重复撤回仍 200，`revoked=False` 表示该 key 当时本就不存在）。
    """

    id: str
    approval_key: str
    revoked: bool


class RevokeApprovalGrantRequest(BaseModel):
    """撤回一条会话级审批授权的请求体（#526 A2）。"""

    approval_key: str


class WorkflowModeChanged(BaseModel):
    """`POST /api/sessions/{id}/workflow-mode` 的成功响应（#526 B1）。

    `mode` 是**动作后**的真值（幂等：重复切同档仍 200）。Executor 每次调用现派生
    `effective_workflow_mode`，切换立即生效。
    """

    id: str
    mode: str


class ClientExitSignaled(BaseModel):
    """`POST /api/sessions/{id}/client-exit` 的成功响应（#360 W-17 / W-12 #356）。

    `status` 取 `ClientExitOutcome.status` 三值之一（`paused` /
    `ignored_not_managed` / `ignored_already_settled`）；`detail` 为人类可读收口说明。
    幂等：重复信号按 outcome 语义返回，不双写。
    """

    id: str
    status: str
    run_id: str | None = None
    detail: str


class SetWorkflowModeRequest(BaseModel):
    """切换会话工作流档的请求体（#526 B1）。"""

    mode: Literal["normal", "plan"]


class ApprovePolicyRuleOut(BaseModel):
    """`GET /api/approve-policy/rules` 的一行（#684 Phase 2）。

    形状 = `ApprovePolicyRule.to_dict()` 逐字段透传（规则是项目配置，字段即真相，
    HTTP 层不裁剪、不重命名）：`id` / `tool` / `key` / `granularity`
    （`exact`|`command`）/ `permission_at_approval`（命中时重验的权限）/ `created_at`。
    """

    id: str
    tool: str
    key: str
    granularity: str
    permission_at_approval: str
    created_at: str


class ApprovePolicyRules(BaseModel):
    """`GET /api/approve-policy/rules` 的成功响应（#684 Phase 2）：本项目规则列表。

    无规则 / 文件缺失 / 文件损坏都返回 `{"rules": []}`（`ApprovePolicyStore.load`
    fail-closed 成空列表）——**不**用 404 表达"没有配置"：空列表是唯一真相，与
    "规则不存在 ⇒ 撤销幂等成功"同一口径。
    """

    rules: list[ApprovePolicyRuleOut]


class ApprovePolicyRuleRevoked(BaseModel):
    """`POST /api/approve-policy/rules/{id}/revoke` 的成功响应（#684 Phase 2）。

    `revoked` 是**动作后**的真值（幂等：重复撤销仍 200，`revoked=False` 表示该 id
    当时本就不存在）。与 #526 `ApprovalGrantRevoked` 同形，只是作用域从会话级换成
    项目级持久规则。
    """

    id: str
    revoked: bool


class AppState:
    """app 内部共享状态的薄容器——避免全局变量。

    V1：单进程内存里的 runtime 工厂 + session store 根目录。
    Capability 子系统按 CAPABILITIES 配置惰性装配一次（wire_capabilities），
    进程退出时统一关闭；配置为空 = 零行为变化。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # sessions 和 workspace 默认放在 .agent/ 下（跟现有诊断日志一致）
        self.sessions_root = Path(settings.workspace_dir) / "sessions"
        self.workspaces_root = Path(settings.workspace_dir) / "workspaces"
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.store = JsonlSessionStore(root=self.sessions_root)
        # Phase Multiturn T2：续聊排队 + steer 请求注册表（PRD §5.3 / §6）。
        # 必须在 RunManager **之前**建：下面的 on_run_terminal 回调要用它（闭包按
        # 引用捕获 self，顺序其实无妨，但先建可读性更好）。
        self.message_queues = MessageQueueManager()
        # detached-run 托管（ADR-0016 §2.1，D-A）：run 生命周期与 HTTP 请求
        # 解耦——SSE 订阅者离开只 unsubscribe，取消只经 POST /cancel 或
        # 孤儿回收（宽限期 Settings.run_disconnect_grace_seconds）。
        #
        # on_run_terminal（ADR-0030 D4）：run 收口后接力投递下一条未投递输入。
        # 回调**唯一**实现点是 SessionService.on_run_terminal（Web/CLI 不各写一份）；
        # 这里用 lambda 延迟构造 service——AppState 构造期还没有 app，而
        # `session_service(self)` 只需要按属性取几个 collaborator。
        self.run_manager = RunManager(
            disconnect_grace_seconds=settings.run_disconnect_grace_seconds,
            on_run_terminal=lambda session_id: session_service(self).on_run_terminal(
                session_id
            ),
            # #200：run 收口时缓存该会话的 builder 快照（context-usage 端点读
            # 它——run 终结后 get_active=None，缓存是"最后 build"的当前事实）。
            on_context_snapshot=self._cache_context_snapshot,
        )
        # #200：会话 → (builder 快照, 工具定义)。工具定义与快照在**同一次收口**
        # 时取（registry 与 builder 属于同一个 runtime，分开取会得到两个真相）。
        # 只保留最近一次（同一会话再次收口时覆盖）。
        self.context_snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        # Phase 5：会话级待审批队列——当 permission_mode 非 danger-full-access
        # 且 ToolExecutor 触发 needs_approval 时，callback 经此 queue 与前端 /approve
        # 对接。key 是 session_id；安全默认下（auto-approve）callback 不挂 queue。
        self.approval_queues: dict[str, PendingApprovalQueue] = {}
        self.budget_recovery_failed_sessions: set[str] = set()
        # 恢复基础设施（R8-1，用户拍板接线）：三 Store 共享同一 SQLite 文件
        # （ADR-0004 布局），WorkspaceRegistry 持久化 session↔sandbox 映射。
        # initialize 是异步的 → 惰性执行（ensure_stores），兼容不走 lifespan
        # 的测试路径。
        self.harness_db = Path(settings.workspace_dir) / "harness.db"
        self.operation_ledger = SqliteOperationLedger(self.harness_db)
        self.transport_ledger = SqliteTransportLedger(self.harness_db)
        self.checkpoint_store = SqliteCheckpointStore(self.harness_db)
        self.session_meta_store = SqliteSessionMetaStore(self.harness_db)
        self.delegation_tree_ledger = SqliteDelegationTreeLedger(self.harness_db)
        self.workspace_registry = WorkspaceRegistry(
            root=Path(settings.workspace_dir), backend="local"
        )
        # WS-2 / ADR-0025：项目实体 + 有序会话账本（同 harness.db 的另 5 张表）。
        # 只构造一次：它持有内存缓存（AC10 同步读），每次 stores 属性都新建会丢掉缓存。
        self.workspace_index = WorkspaceIndex(
            SqliteWorkspaceStore(self.harness_db), self.store
        )
        # W-10（#354）：单目录写入租约 + 持久 FIFO 队列（同 harness.db 的另 2 张
        # 表）。presence 只读合同缺省 NoPresenceReader——W-12 在场登记落地后
        # 由装配处替换真实现，本层绝不反向登记（拆 W-10 ↔ W-12 依赖环）。
        self.lease_manager = WorkspaceLeaseManager(SqliteLeaseStore(self.harness_db))
        self._stores_lock = asyncio.Lock()
        self._stores_ready = False
        # Capability 装配：只在首次使用时执行（含 Memory / Skills / demo 等）。
        # asyncio.Lock 守住 once 语义：并发首请求都看到 _wiring is None 时，
        # 只有一个真正执行 wire_capabilities，另一个等锁后复用结果
        # （否则第二个会在重复注册上炸掉 / 被降级吞掉）。
        self._wiring_lock = asyncio.Lock()
        self._registry: CapabilityRegistry | None = None
        self._wiring: CapabilityWiring | None = None
        # #203 / ADR-0032：自定义供应商存储（全局配置实体，与 env catalog 并存）。
        # 单一构造入口 ProviderStore.for_settings：真实凭据后端（keyring），
        # 测试可经 patch 换 MemoryCredentialStore。
        self.provider_store = ProviderStore.for_settings(settings)
        self._closed = False  # shutdown 后置位：get_wiring 拒绝在关停后新装配
        # `#564` 裁决 (a)：session 声明的 pre-CAS 校验端口。组合根负责判据的
        # 同源装配（`root_registry_tool_names` 零副作用取根 registry 名字集，
        # 审查 P2-1：不实例化 sandbox），`session_service()` 把它与其他
        # collaborator 一样原样搬进领域层。
        self.validate_session_declaration = _session_declaration_validator(
            settings=settings,
            store=self.store,
            get_wiring=self.get_wiring,
        )
        # `#616`：陈旧账行名清除通道（`RegisteredToolNamesProvider`）的根 registry
        # 名字集端口。与上面的 validator 同源装配（`root_registry_tool_names`
        # 零副作用取根 registry 名字集，P2-1：不实例化 sandbox），`session_service()`
        # 把它与其他 collaborator 一样原样搬进领域层。
        self.registered_tool_names = _registered_tool_names_provider(
            settings=settings,
            store=self.store,
            get_wiring=self.get_wiring,
        )
        # `#357` W-13（契约 1/6/7）：恢复裁决呈现元数据端口。与上面两个端口
        # 同源装配（`root_registry_reconcile_info` 零副作用投影、与名字集投影
        # 逐分支同构，P2-1：不实例化 sandbox），`session_service()` 原样搬入领域层。
        self.reconcile_info = _reconcile_info_provider(
            settings=settings,
            store=self.store,
            get_wiring=self.get_wiring,
        )
        # `#357` W-13（契约 5）：启动崩溃扫描的**快照**（lifespan 内只扫一次并
        # 存这里；`GET /api/recovery/interrupted` 只读它，绝不重跑扫描——扫描
        # 会写 run/interrupted + 跑 reconcile）。None = lifespan 未跑过。
        self.interrupted_scan: list | None = None

    def _cache_context_snapshot(
        self, session_id: str, snapshot: dict[str, Any], tool_definitions: list[dict[str, Any]],
    ) -> None:
        """run 收口时缓存 builder 快照 + 工具定义（#200 context-usage 数据面）。

        两者取自**同一次收口的 runtime**（RunManager 的 `_capture_context_snapshot`
        在 run 收尾时传入）——分开取会得到两个真相。只保留最近一次（同一会话
        再次收口时覆盖）。
        """
        self.context_snapshots[session_id] = (snapshot, tool_definitions)

    async def ensure_stores(self) -> None:
        """惰性初始化恢复三 Store（幂等；并发首请求由锁守 once 语义）。"""
        if self._stores_ready:
            return
        async with self._stores_lock:
            if not self._stores_ready:
                await initialize_stores(self.stores)
                await self.transport_ledger.initialize()
                # W-10：租约/队列表 + 重启对账（与 Session 状态对齐；有效会话
                # 的持有租约原样保留——租约不因重启释放，PRD §3）。
                await self.lease_manager.initialize()
                dropped = await self.lease_manager.reconcile_on_restart(
                    set(self.store.list_session_ids())
                )
                if dropped:
                    logging.getLogger("agent_harness.web").info(
                        "lease reconcile dropped %d row(s) for deleted sessions",
                        dropped,
                    )
                self._stores_ready = True

    @property
    def stores(self) -> RecoveryStores:
        """恢复三 Store 束（factory 消费；实例归 AppState 所有）。"""
        return RecoveryStores(
            operation_ledger=self.operation_ledger,
            checkpoint_store=self.checkpoint_store,
            session_meta_store=self.session_meta_store,
            delegation_tree_ledger=self.delegation_tree_ledger,
            workspace_index=self.workspace_index,
        )

    async def get_wiring(self) -> tuple[CapabilityRegistry, CapabilityWiring]:
        """惰性装配 Capability 子系统并缓存；返回 (registry, wiring)。

        shutdown 可能在 wire await 期间发生：入口和拿锁后都检查 _closed，
        wire 完成后再查一次——在途调用以 RuntimeError 失败，但刚装配好的
        wiring 仍留在字段上，由随后拿到锁的 shutdown 关闭（连接不泄露）。
        """
        if self._closed:
            raise RuntimeError("AppState is shut down")
        if self._wiring is not None and self._registry is not None:
            return self._registry, self._wiring
        async with self._wiring_lock:
            if self._closed:
                raise RuntimeError("AppState is shut down")
            if self._wiring is None or self._registry is None:
                config = parse_capabilities_config(self.settings.capabilities)
                registry = CapabilityRegistry()
                # sessions=自己的 store（#298 T7b）：V2 记忆形成执行 job 时要按
                # (session_id, run_id) 从**运行时空正在写的那一份**日志切本轮事件。
                wiring = await wire_capabilities(
                    registry, config, settings=self.settings, sessions=self.store,
                    workspace_index=self.workspace_index,
                )
                # 先落字段再查 _closed：锁在手上，shutdown 必然排在本次释放之后，
                # 它会从字段上取走这份 wiring 并关闭——绝不静默丢弃。
                self._registry, self._wiring = registry, wiring
                if self._closed:
                    raise RuntimeError("AppState is shut down")
            return self._registry, self._wiring

    @property
    def wiring(self) -> CapabilityWiring | None:
        """已装配的 wiring（未装配时 None；测试与 shutdown 用）。"""
        return self._wiring

    async def shutdown(self) -> None:
        """进程退出时关闭后台 run task、relay 与外部连接。幂等：重复调用只关闭一次。

        必须拿 _wiring_lock：否则在途 get_wiring 可能在 swap 之后才完成装配，
        装配出的 wiring 永远没人关（泄露 Milvus / embedding 连接）。
        先置位 _closed 再拿锁——让在途装配在 wire 完成后立刻失败，而不是
        把 wiring 交给一个已关停的 app。
        """
        self._closed = True
        # 先停 detached run（它们引用 wiring 的工具/模型），再关 wiring。
        await self.run_manager.aclose()
        async with self._wiring_lock:
            wiring, self._wiring, self._registry = self._wiring, None, None
        # 关闭知识在 CapabilityWiring.aclose（批次 A 候选 4）：memory 组件 +
        # lifecycle 通道逐项隔离关闭，web 层不再懂每种 capability 的关闭姿势。
        if wiring is not None:
            await wiring.aclose()
        release_project_instruction_store(self.settings)


# ── 领域服务的组合根适配（#248）──────────────────────────────────────
#
# 领域层（`session/service.py` / `session/projects.py`）的构造契约是**它自己拥有的
# 显式 collaborators**；「容器长什么样」翻译成「领域要什么」只在这两个函数里发生。
# **新增 AppState 成员不会自动流进领域层**，必须在这里显式搬一次——那正是要的边界。
#
# 为什么是模块级函数而不是 AppState 方法：测试里有大量 duck-typed 的局部假 state
# （只提供 store / run_manager / settings 等**被真正访问**的成员）。方法形态要求假对象
# 自己也提供 `session_service()`，等于把组合根重新塞回容器类型；函数形态只按属性取
# 所需成员——真容器与假对象一视同仁，缺谁就是 AttributeError，不静默兜底。
#
# 每次调用现取属性（不缓存）：与原先 `SessionService(state)` 的行为逐字一致，
# 构造后替换 `state.run_manager` 等打桩仍然生效。


def _session_declaration_validator(
    *,
    settings: Settings,
    store: JsonlSessionStore,
    get_wiring: Callable[[], Awaitable[tuple[CapabilityRegistry, CapabilityWiring]]],
) -> Any:
    """组合根适配：`#564` 裁决 (a) 的 pre-CAS 校验端口（`SessionDeclarationValidator`）。

    判据 = **根 registry**（树级语义）：名字集由 `root_registry_tool_names`
    **零副作用**计算，与 `build_runtime` 的真实装配同源（漂移由
    `tests/test_assembly_root_registry_names.py` 三方对账钉住），喂给
    `validate_tool_call_limits_registered(scope="session")`。审查 P2-1：validator
    不得构造 sandbox——那会在 #266 归属对账之前对 workspace `mkdir`（坏名 422
    重建已删 cwd、合法名 resume 掩蔽守卫），所以这里不碰 `_build_tooling` /
    workspace_registry。存为 `AppState.validate_session_declaration` 成员——
    `session_service()` 与其他 collaborator 一样**原样搬入**领域层（AC4：没有
    静默丢字段、没有派生转换）。CLI 侧复用同一容器，两条入口同一校验。
    """

    async def _validate(
        limits: SessionLimits,
        *,
        session_id: str,
        workspace: Any,
        agent_profile: str | None,
        include_constraint_resolution_tool: bool = True,
    ) -> None:
        # workspace / agent_profile 不参与判定（前者 = P2-1 零副作用要求；后者
        # 只影响 delegate 的配额值，不影响名字集）：参数保留是端口形状（Protocol）。
        _, wiring = await get_wiring()
        validate_tool_call_limits_registered(
            limits,
            registered=sorted(root_registry_tool_names(
                settings, wiring, session_id=session_id, session_store=store,
                include_constraint_tools=True,
                include_constraint_resolution_tool=include_constraint_resolution_tool,
            )),
            scope="session",
        )

    return _validate


def _registered_tool_names_provider(
    *,
    settings: Settings,
    store: JsonlSessionStore,
    get_wiring: Callable[[], Awaitable[tuple[CapabilityRegistry, CapabilityWiring]]],
) -> Any:
    """组合根适配：`#616` 根 registry 名字集端口（`RegisteredToolNamesProvider`）。

    判据 = **根 registry**（树级语义）：名字集由 `root_registry_tool_names`
    **零副作用**计算，与 `build_runtime` 的真实装配同源（漂移由
    `tests/test_assembly_root_registry_names.py` 三方对账钉住）。与 `#564` 的
    `_session_declaration_validator` 同源、同一条 P2-1 取舍：不碰 `_build_tooling`
    / workspace_registry——否则会在归属对账之前对 workspace `mkdir`。
    存为 `AppState.registered_tool_names` 成员——`session_service()` 与其他
    collaborator 一样**原样搬入**领域层。CLI 侧复用同一容器，两条入口同一判据。
    """

    async def _names(session_id: str) -> frozenset[str]:
        _, wiring = await get_wiring()
        return root_registry_tool_names(
            settings, wiring, session_id=session_id, session_store=store,
        )

    return _names


def _reconcile_info_provider(
    *,
    settings: Settings,
    store: JsonlSessionStore,
    get_wiring: Callable[[], Awaitable[tuple[CapabilityRegistry, CapabilityWiring]]],
) -> Any:
    """组合根适配：恢复裁决呈现元数据端口（`ToolReconcileInfoProvider`，#357）。

    判据 = **根 registry**（树级语义）：由 `root_registry_reconcile_info`
    **零副作用**计算（读工具类元数据，不实例化 sandbox、不 mkdir——P2-1 同款
    取舍），与 `root_registry_tool_names` 逐分支同构，一致性由
    `tests/test_assembly_root_registry_names.py` 同型对账钉住。存为
    `AppState.reconcile_info` 成员，`session_service()` 原样搬入领域层。
    """

    async def _info(session_id: str) -> dict[str, Any]:
        _, wiring = await get_wiring()
        return root_registry_reconcile_info(
            settings, wiring, session_id=session_id, session_store=store,
        )

    return _info


def session_service(state: AppState) -> SessionService:
    """用容器的成员构造 `SessionService`（传输侧唯一适配点）。"""
    return SessionService(
        store=state.store,
        run_manager=state.run_manager,
        settings=state.settings,
        workspace_registry=state.workspace_registry,
        session_meta_store=state.session_meta_store,
        message_queues=state.message_queues,
        approval_queues=state.approval_queues,
        workspaces_root=state.workspaces_root,
        workspace_index=state.workspace_index,
        operation_ledger=state.operation_ledger,
        transport_ledger=state.transport_ledger,
        checkpoint_store=state.checkpoint_store,
        harness_db=state.harness_db,
        stores=state.stores,
        ensure_stores=state.ensure_stores,
        get_wiring=state.get_wiring,
        validate_session_declaration=state.validate_session_declaration,
        budget_recovery_failed_sessions=state.budget_recovery_failed_sessions,
        registered_tool_names=state.registered_tool_names,
        reconcile_info=state.reconcile_info,
    )


def approve_policy_store() -> ApprovePolicyStore:
    """本项目持久审批规则存储（#684 Phase 2 管理面）。

    项目根 = 进程当前工作目录，与 `ToolExecutor.__init__` 的 `project_root` 回落口径
    逐字一致（`project_root is None ⇒ Path.cwd()`）：管理面读写的必须正是执行域用来
    命中规则的那份 `.agent-harness/approve-policy.json`——否则会出现"撤销了却还在
    放行"这类两套真相（不变量 #22 同源）。
    """
    return ApprovePolicyStore(Path.cwd())


def project_service(state: AppState) -> ProjectService:
    """用容器的成员构造 `ProjectService`（传输侧唯一适配点）。"""
    return ProjectService(
        store=state.store,
        workspace_index=state.workspace_index,
        ensure_stores=state.ensure_stores,
    )


async def _is_dir_locked(state: AppState, path: str) -> bool:
    """`path` 是否被写租约占用（#367 / W-23 选项 A：目录冲突判定）。

    用 `normalize_dir_key` 与活跃租约的 dir_key 比对（同一物理目录恒同键）；
    路径非法 → LeasePathError，调用方按 422 处理（fail-closed）。
    """
    dir_key = normalize_dir_key(path)
    leases = await state.lease_manager.leases()
    return any(l.dir_key == dir_key for l in leases)


async def _validate_wired_context_providers(
    state: AppState, ids: list[str] | None
) -> None:
    """``context_providers`` 对照 wiring 真实装配集校验 → 422（ADR-0021）。

    Pydantic 只能校验形状；某个 provider 是否装配取决于 CAPABILITIES 运行时
    状态，必须在这里看 wiring。三个 amend 入口（create / resume / messages）
    共用同一份判定。
    """
    if ids is None:
        return
    _, wiring = await state.get_wiring()
    wired_ids = {getattr(p, "name", None) for p in wiring.context_providers}
    wired_ids.discard(None)
    unknown = [pid for pid in ids if pid not in wired_ids]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"context_providers contains unknown ids {unknown}; "
                f"available: {sorted(wired_ids)}"
            ),
        )


async def _validate_amend_for_existing_session(
    state: AppState, amend: AmendOptions
) -> None:
    """``/resume`` 与 ``/messages`` 的 amend 校验（与 POST /api/sessions 对齐）。

    create 路径的 ``model`` 校验在 service 里（落盘前避免孤儿 session）；这两个
    端点没有那道闸门——不在此拦截的话，未知 model 会让 ``build_runtime`` 抛
    ``ConfigError`` 且无人捕获 → 500，未知 context_providers 则被静默跳过。
    ``reasoning_effort`` / ``agent_profile`` 已由 Pydantic 在 parse 期 422
    （``_AmendValueValidators``）。
    """
    await _validate_wired_context_providers(state, amend.context_providers)
    if amend.model is not None:
        try:
            # 统一解析点（catalog + 自定义供应商 fallback）：/api/models 广告的
            # 自定义条目必须在这里就能解析，否则"UI 能选、一提交就 422"。
            store = ProviderStore.for_settings(state.settings)
            ModelConfig.resolve_selection(state.settings, amend.model, store)
        except ConfigError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


#: 重放 backlog 阈值（durable 事件数，ADR-0016 §2.3）：after_seq 落后超过
#: 该值 → 单帧 stream/truncated 控制事件后收流，客户端走 GET /events 全量
#: 重建后带 after_seq=latest_seq 重连（02 §10.4 简化版；snapshot 层 DEFER）。
STREAM_REPLAY_MAX_EVENTS = 1000

#: SSE keepalive 间隔（秒）——周期性下发注释帧（`: ping - <ts>`）。
#:
#: 为什么必须显式设：部署交付层（`Server: CloudStudio Gateway`，前置腾讯 EdgeOne）
#: 会把**整个响应**攒到流结束才下发——实测 `POST /api/sessions` 的响应头要
#: 44.158s 才到（≈ run 全长），而 326 帧数据全在其后 0.094s 内到齐。也就是说
#: 前端拿不到任何字节直到 run 跑完，打字机效果不可能存在。
#:
#: 持续有字节流动是让中间层及时 flush 的前提，而 sse-starlette 的默认间隔是
#: **15s**——比一个交互式 run 还长，等于没有 keepalive。2s 对齐 WS 通道既有的
#: `WS_PING_INTERVAL`（websocket.py）：两条通道同一拍，观测/调优不必记两套数。
#:
#: 注意注释帧**不是**事件：客户端（`lib/sse.ts::parseFrame`）只取 `data:` 行，
#: 注释帧永远不会被投影成对话事件。但它是**链路活性**证据（#440 定口径：心跳 /
#: 任何到达字节 = 链路活着）：前端停摆检测（`RECONNECT_STALL_MS`，10s 无帧判僵死）
#: 的活性侧信道把 WS 心跳帧（`server_ping`）与 SSE 降级流读到的字节都计入同一
#: 基准（`wsStream.ts` 的 `onLiveness`）——审批等待期后端零事件、两条通道唯一的
#: 下行就是这份 2s 心跳，没有它，看门狗会把「人在决策」误判成「连接僵死」而
#: 掐断一条被心跳证明存活的连接。字节到不了的部署（交付层攒包）自然也没有活性
#: 证据，停摆检测照旧生效——活性只来自真实到达的字节，不存在「喂假进展」。
SSE_PING_INTERVAL_SECONDS = 2


def _sse_response(
    generator: Any, *, headers: dict[str, str] | None = None,
) -> EventSourceResponse:
    """SSE 响应的**唯一**构造点：keepalive 只在这里定义。

    三个端点共用（live run / 重连续传 / truncated 控制帧）。各写一遍就会在
    「哪条忘了开 keepalive」上漂移——而那正是「某些请求流式、某些不流式」
    这类只在部署层才暴露的 bug 形态（本地直连看不出区别，因为没有中间层攒包）。

    反缓冲头不在这里重复声明：`X-Accel-Buffering: no` / `Connection: keep-alive`
    / `Cache-Control: no-store` 由 sse-starlette 在 `EventSourceResponse.__init__`
    里默认带上；抄一份会在库改默认值时变成两处不一致，而真正生效的是库那一份。
    """
    return EventSourceResponse(
        generator, headers=headers, ping=SSE_PING_INTERVAL_SECONDS,
    )


def _local_fuse_headers(fuse: Any | None) -> dict[str, str]:
    """生效 local fuse 的**只读投影**：响应头形式（`#308` Must Do）。

    为什么是响应头：SSE 响应没有 JSON 体可承载元数据（`X-Permission-Mode` 就是为解决
    同一个问题立的先例），而 JSON 分支与 SSE 分支用同一条通道才能"两处一致"。

    值一律取自 `agent.budget.LocalFuse.as_projection()`（**单一形状**：客户端可见的字段与
    它们的取值只定义一次，这里只做"字段名 → 响应头名"的映射，不重新组装事实）。迁移期
    `max_steps` 的 `Deprecation` / `Warning: 299` 信号已随 #320 移除——alias 不再被接受，
    没有"还在用废弃字段"的请求需要提示。

    `fuse is None` ⇒ 空 dict（没有生效 fuse 的响应不投影：queued / steered 分支）。反向
    的抑制发生在**调用点**：`launch=False` 的只建会话**有**生效 fuse，但那个值是请求级、
    没有任何 run 消费它（`11 §6.1` 只把 budget 挂在启动 run 上），所以调用点刻意不投影。
    """
    if fuse is None:
        return {}
    projection = fuse.as_projection()
    return {
        "X-Local-Max-Agent-Turns": str(projection["max_agent_turns"]),
        "X-Local-Fuse-Source": projection["source"],
    }


def _worktree_headers(worktree_path: str) -> dict[str, str]:
    """worktree 回执头（`#367` / `X-Permission-Mode` 同型先例）。

    为什么不能直接放裸值：HTTP 头字段值只能 latin-1（starlette `init_headers`
    硬编码 `v.encode("latin-1")`），而 worktree 路径继承仓库位置
    （`<toplevel-parent>/worktrees/<repo>-<hex>`）——仓库在中文用户名/中文
    目录下（Windows 常态）会让裸值在响应构造期 `UnicodeEncodeError`，整个
    创建请求 500（`#765` locked_dir 红）。latin-1 可编码时保持
    `X-Worktree-Path` 裸值逐字节不变（既有消费者零影响）；否则改发
    `X-Worktree-Path-Encoded`（RFC 5987 ext-value：`UTF-8''<percent-encoded>`，
    `safe=''` 全量转义——裸 Windows 路径里合法的 `%` 会让"同一头名混装裸值
    与编码值"产生歧义），前端 `worktreePathFromResponse` 按该约定兜底解码。
    """
    try:
        worktree_path.encode("latin-1")
    except UnicodeEncodeError:
        return {
            "X-Worktree-Path-Encoded": f"UTF-8''{quote(worktree_path, safe='')}"
        }
    return {"X-Worktree-Path": worktree_path}


def _run_stream_response(
    state: Any, run: Any, subscriber: Any, session_id: str,
    *, headers: dict[str, str] | None = None,
) -> EventSourceResponse:
    """detached run 的 live SSE 响应（**唯一**实现）。

    三个调用点共用：`POST /api/sessions`（创建即跑）、`POST /messages` 的
    launched 分支、`POST /queue/flush`。语义必须一致（ADR-0030 §4.6 明确要求
    flush 与 messages 同形），各写一遍就会在「谁 unsubscribe、谁处理 DONE、
    断连怎么收尾」这些细节上漂移——而这正是上一版留下的三份逐字副本。

    断连时 EventSourceResponse 取消本 generator → finally unsubscribe（run 不受
    影响，ADR-0016 detached）；run 终结 → DONE 哨兵 → 流干净收尾。
    """
    async def event_generator():
        try:
            while True:
                event = await subscriber.queue.get()
                if event is state.run_manager.DONE:
                    break
                yield _event_to_sse_dict(event, session_id)
        finally:
            run.unsubscribe(subscriber)

    return _sse_response(event_generator(), headers=headers)


def _event_to_sse_dict(event: AgentEvent, session_id: str) -> dict[str, str]:
    """AgentEvent → SSE 帧（信封构建在 web/serialization.py，SSE/WS 共用）。"""
    return {"data": _safe_text(json.dumps(
        build_event_payload(event, session_id), ensure_ascii=False
    ))}


def _session_event_to_sse_dict(event: SessionEvent, session_id: str) -> dict[str, str]:
    """SessionEvent → SSE 帧（重放通道，信封构建在 web/serialization.py）。"""
    return {"data": _safe_text(json.dumps(
        build_session_event_payload(event, session_id), ensure_ascii=False
    ))}


#: CSP（集成 AI 移交，INTEGRATION_NOTES §4.1）：静态 HTML 的纵深防御——
#: 脚本/样式只认同源构建产物，img 放行 data:。所有响应统一携带（浏览器
#: 仅对 HTML 文档执行，JSON 响应带此头无害），避免漏掉任何静态入口。
_CSP_POLICY = "default-src 'self'; img-src 'self' data:"


class CSPHeaderMiddleware:
    """纯 ASGI 中间件：所有响应统一携带 CSP 头（行为契约同旧实现）。

    为什么不用 @app.middleware（BaseHTTPMiddleware）：SSE 流过其 anyio 内存
    流 machinery 时，客户端断连的取消会在嵌套中间件间传播污染请求栈（全量
    回归下曾放大为逐请求 500 "No response returned"）。纯 ASGI 直通流式
    帧，无缓冲无任务组（ADR-0016 §2.1 生产级加固）。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Content-Security-Policy"] = _CSP_POLICY
            await send(message)

        await self.app(scope, receive, send_wrapper)


#: 承载 Bearer 的子协议前缀（#890，用户裁决走 k8s 同款形状）。
#:
#: 浏览器原生 WebSocket API **不能**设置 `Authorization` 头，子协议是它唯一能带凭据的
#: 通道。值形如 ``base64url.bearer.authorization.agent-harness.<base64url 无 padding 的 token>``：
#: 前缀里的 `agent-harness` 是命名空间（k8s 用 `k8s.io`），避免与其他产品的同名子协议
#: 碰撞；token 用 base64url 无 padding 编码，因为它必须落在 RFC 6455 的 HTTP token 字符
#: 集内——padding 的 `=` 不是合法 token 字符（`.` 合法，故前缀里的点可用），编码体逐字符
#: 落在 `[A-Za-z0-9_-]`。
#: 前端对侧在 `web/src/lib/wsStream.ts` 的同名常量。
WS_BEARER_SUBPROTOCOL_PREFIX = "base64url.bearer.authorization.agent-harness."

#: 业务子协议：只为满足"浏览器请求了子协议就必须收到一个回显"这条规范。
#: 出处是 **WHATWG** 的 `establish a WebSocket connection`（响应里没有可回显的子协议 ⇒
#: 客户端自己 fail 掉连接）。⚠ 这一条**不在** RFC 6455 里：RFC 6455 §4.1 第 6 步只对
#: "服务端回显了客户端**没请求过**的子协议"要求 MUST fail，对"请求了却没回显"没有规定
#: —— WHATWG 在该步的 note 里明确点出了这个差别。RFC 6455 一侧对应的是 §4.2.2 的
#: **服务端**义务：不同意客户端任何一个请求时 MUST NOT 回显。本仓据此**从不**回显承载
#: token 的那一个（防泄漏），所以客户端必须再带一个可回显的。k8s 同款：它要求客户端
#: "至少再带一个真实子协议"。
WS_BUSINESS_SUBPROTOCOL = "agent-harness.v1"

#: 跨源拒的 close reason。**固定短 ASCII**：ASGI 规定 `reason` ≤123 字节且限可打印
#: ASCII，而 `Origin` 是客户端可控、无长度上界的——原样拼进去既越界又是注入面。
#: 真实来源只进日志（截断后）。
WS_ORIGIN_DENIED_REASON = "cross-origin websocket handshake denied"

#: 凭据被拒的 close reason。与上面那条同受一条 ASGI 约束（≤123 字节可打印 ASCII），
#: 且同样**到不了客户端**——uvicorn 对握手前的 close 固定回 403。抽成具名常量是为了
#: 两个拒绝出口对称：分支里各写一条字面量，改一处忘一处就会漂移。
WS_CREDENTIAL_REJECTED_REASON = "websocket credential rejected"


def _token_subprotocol(subprotocols: list[str]) -> str | None:
    """命中 Bearer 前缀的那个子协议（没有则 None）。"""
    for proto in subprotocols:
        if proto.startswith(WS_BEARER_SUBPROTOCOL_PREFIX):
            return proto
    return None


def _bearer_from_subprotocols(subprotocols: list[str]) -> str | None:
    """从子协议列表解出 `Authorization` 头的**等价形状**（`"Bearer <token>"`）。

    解出来直接喂同一个 `_resolve_identity`，因此两条通道的 fail-closed 语义
    完全一致（缺 token / 坏签名 / 过期 / 无 exp 都是同一个出口），不存在第二套判据。

    解码用 `base64url` 且**不带 padding**（k8s 的 `base64.RawURLEncoding` 同款）：
    padding 的 `=` 不是合法 HTTP token 字符，浏览器会直接拒发这个子协议。
    token 本身不是 UTF-8 文本的先验（JWT 是 ASCII，但这里不假定）⇒ 解码失败
    一律返回 None（当作"没给凭据"），交给 `_resolve_identity` 的 fail-closed 处理。

    解码是**部分宽容的**：多余字符数恰在 padding 补齐窗口内时被忽略；
    其它尾部字符可能抛 `binascii.Error`（`ValueError` 子类，落 fail-closed 分支）
    或被解成另一 token。缺的 padding 由上面那行 `=` 补齐，故不同子协议串可解出
    **同一 token**。
    结果等价，仍走同一 `_resolve_identity` 鉴权路径，不构成绕过（实跑确认）。
    """
    proto = _token_subprotocol(subprotocols)
    if proto is None:
        return None
    encoded = proto[len(WS_BEARER_SUBPROTOCOL_PREFIX):]
    if not encoded:
        return None
    try:
        token = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        return f"Bearer {token.decode('utf-8')}"
    except (ValueError, UnicodeDecodeError):
        return None


def _negotiate_subprotocol(subprotocols: list[str]) -> str | None:
    """挑一个要在 101 响应里回显的子协议：**只可能是业务子协议**，其余一律 None。

    回显即"服务端声称实现了这个协议"，所以此处必须有白名单：原实现回显"第一个非 token
    前缀的子协议"，等于客户端自带什么就声称支持什么（`subprotocols=["other.product.v9"]`
    会拿到 `Sec-WebSocket-Protocol: other.product.v9`）——违反子协议协商的基本约定。

    不回显承载 token 的那一个则是防泄漏（k8s 同款：校验成功后把它从协商列表剔除）。

    返回 None 即"**业务子协议不在请求列表里**"，穷尽所有情形：没请求任何子协议；只请求了
    承载 token 的那一个；只请求了第三方子协议（`other.product.v9`）；第三方 + token 而**没**
    带业务子协议。后三种里浏览器都会因"请求了子协议却没收到回显"自己判握手失败
    （k8s 直接报 `missing additional subprotocol` 错误；本项目靠浏览器这条语义兜住，
    我们自己的客户端永远会带业务子协议）。
    """
    return WS_BUSINESS_SUBPROTOCOL if WS_BUSINESS_SUBPROTOCOL in subprotocols else None


class _IdentityRejected(Exception):
    """凭据判定失败（`AuthSeamMiddleware._resolve_identity` 的唯一失败出口）。

    HTTP 面缺 / 坏凭据**恒 401**（tests/test_identity.py 与前端 `lib/auth.ts` 的 401
    引导横幅钉住那条契约），由调用处写死；WS 侧不在此处理——它没有 HTTP 状态行，
    只能以 ASGI `websocket.close` 收场。
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class AuthSeamMiddleware:
    """纯 ASGI 中间件：身份认证 + IdentityContext 绑定（行为契约同旧实现）。

    HTTP 面的 fail-open/fail-closed 语义、claims 校验、401 形状逐字节不变
    （tests/test_identity.py / test_web_api.py 钉住）；差异仅在传输层：
    不经 BaseHTTPMiddleware 的任务组，SSE 断连取消不再跨请求传染。

    #890：**websocket scope 不再直通**，而是走同一个 `_resolve_identity`——
    配置密钥时无凭据握手在 `websocket.accept()` 之前被拒（`websocket.close`），
    这正是缺陷的修复点：此前 WS 是无条件放行的旁路，Bearer 边界在它上面是空操作。
    """

    def __init__(
        self,
        app: Any,
        settings: Settings,
        host_service_token: str | None = None,
    ) -> None:
        self.app = app
        self._settings = settings
        self._host_service_token = host_service_token
        self._host_skills_nonce_lock = threading.Lock()
        self._host_skills_used_nonces: dict[str, float] = {}

    def _consume_host_skills_nonce(self, nonce: str) -> bool:
        now = time.monotonic()
        with self._host_skills_nonce_lock:
            self._host_skills_used_nonces = {
                used: expires_at
                for used, expires_at in self._host_skills_used_nonces.items()
                if expires_at > now
            }
            if nonce in self._host_skills_used_nonces:
                return False
            if len(self._host_skills_used_nonces) >= 4096:
                return False
            self._host_skills_used_nonces[nonce] = now + HOST_SKILLS_PROOF_WINDOW_SECONDS
            return True

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            await self._authenticate_websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/api/health":
            # W-11（#355）：健康探针匿名可达——附着核验发生在鉴权之前（附着方拿到
            # 凭据前就要判断服务是否活着、形状是否相符）。载荷只含非秘密字段，
            # tests/host_service/ 钉住不泄密。
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        identity: IdentityContext | None = None
        if (
            scope.get("method") == "GET"
            and scope.get("path") == "/api/skills"
            and self._host_service_token
            and self._settings.jwt_secret
        ):
            nonce = headers.get(HOST_SKILLS_NONCE_HEADER)
            proof = headers.get(HOST_SKILLS_PROOF_HEADER, "")
            proof_timestamp = headers.get(HOST_SKILLS_TIMESTAMP_HEADER, "")
            if nonce:
                try:
                    claims = jwt.decode(
                        self._host_service_token,
                        self._settings.jwt_secret.get_secret_value(),
                        algorithms=["HS256"],
                        options={"require": ["tenant_id", "user_id", "service_uuid", "exp"]},
                    )
                    service_uuid = claims.get("service_uuid")
                    expected = host_skills_request_proof(
                        self._host_service_token, nonce, service_uuid, proof_timestamp
                    )
                    scopes = claims.get("scopes", ["user", "session"])
                    try:
                        timestamp_is_fresh = (
                            abs(int(time.time()) - int(proof_timestamp))
                            <= HOST_SKILLS_PROOF_WINDOW_SECONDS
                        )
                    except ValueError:
                        timestamp_is_fresh = False
                    valid_identity = (
                        claims.get("tenant_id") == "local"
                        and isinstance(claims.get("user_id"), str)
                        and bool(claims["user_id"].strip())
                        and isinstance(service_uuid, str)
                        and isinstance(scopes, list)
                        and all(isinstance(item, str) for item in scopes)
                    )
                except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
                    expected = ""
                    valid_identity = False
                    timestamp_is_fresh = False
                    service_uuid = None
                if (
                    expected
                    and valid_identity
                    and timestamp_is_fresh
                    and hmac.compare_digest(proof, expected)
                    and self._consume_host_skills_nonce(nonce)
                ):
                    identity = IdentityContext("local", claims["user_id"], scopes)
                    state = scope.setdefault("state", {})
                    state["host_skills_challenge_nonce"] = nonce
                    state["host_skills_service_uuid"] = service_uuid
                    state["host_skills_proof_timestamp"] = proof_timestamp
        if identity is None:
            try:
                identity = self._resolve_identity(headers.get("authorization"))
            except _IdentityRejected as rejected:
                response: Response = JSONResponse(
                    {"detail": rejected.detail}, status_code=401)
                await response(scope, receive, send)
                return
        token = set_identity_context(identity)
        try:
            await self.app(scope, receive, send)
        finally:
            identity_context_var.reset(token)

    async def _authenticate_websocket(
        self, scope: Any, receive: Any, send: Any,
    ) -> None:
        """WS 握手前的凭据 + 来源判定（#890）。**必须在 accept 之前**。

        两条分支与 HTTP 面同源，不是第二套判据：

        - **已配置 `jwt_secret`**：凭据走同一个 `_resolve_identity`，来源有两个等价
          通道（任一通过即放行）：`Authorization: Bearer`（桌面外壳 loopback 代理
          注入的那条）与 `Sec-WebSocket-Protocol` 子协议（浏览器唯一能用的那条，
          见 `WS_BEARER_SUBPROTOCOL_PREFIX`）。
          此处**不看 `Origin`**——本机任意进程都能伪造 `Origin: http://localhost`，
          把它当边界是假边界；HTTP 面的 `require_trusted_origin` 在配了密钥时也是
          直接返回（认证层才是边界）。
        - **未配置 `jwt_secret`**：本地信任模式 + 来源闸。无 `Origin` ⇒ 非浏览器
          发起（第三方网页**无法**构造不带 Origin 的浏览器握手）⇒ 放行；带 Origin
          则只接受本机 hostname，其余拒绝。这一条封的是 **WS 通道**的 drive-by 形态：
          WS 握手不受 CORS 约束，服务端不判 Origin 就等于允许用户访问的任意网页连上来
          读写会话。**范围仅限本路由**——跨源 HTTP 读写面（会话事件流 / 消息入口等）
          仍无来源闸而 CORS 为 `*`，那是既有缺口、不在本票 Scope。判据与
          `projects.require_trusted_origin` 共用一份策略实现（出口文案与 close reason 各自一份）。

        拒 = `websocket.close`（未 accept）⇒ uvicorn 回 **403** 且不建连
        （`websockets_impl.py:296-304`）。语义与 Django Channels 的
        `WebsocketDenier`（deny 走 `close()`）、Phoenix `check_origin` 的
        握手前 `forbidden` 一致；socket.io 亦是"连接建立之前"鉴权，
        故不采用"先 accept 再发 error 帧"的形状（那会把未认证连接先建起来）。
        """
        headers = Headers(scope=scope)
        subprotocols = list(scope.get("subprotocols") or [])
        # 两种来源等价（任一通过即放行）；都缺席时退化成一次 `None`，让
        # `_resolve_identity` 仍走一遍它的 fail-closed（配了密钥 ⇒ 缺凭据即拒；
        # 未配置密钥 ⇒ 本地信任模式照常放行）。
        credentials = [c for c in (headers.get("authorization"),
                                   _bearer_from_subprotocols(subprotocols))
                       if c is not None] or [None]
        identity: IdentityContext | None = None
        for credential in credentials:
            try:
                identity = self._resolve_identity(credential)
                break
            except _IdentityRejected:
                continue
        if identity is None:
            # uvicorn 对握手前的 close 固定回 403，code / reason 传不过去；
            # 这里给的形状只是 ASGI 层语义表达，不声称到达客户端。
            await send({"type": "websocket.close", "code": 1008,
                        "reason": WS_CREDENTIAL_REJECTED_REASON})
            return
        # 函数内 import：`projects` 反向 import 本模块（`project_service` 等），
        # 模块级引会成环。策略正文（含 `origin_is_local`）与 HTTP 面共用一份——
        # 配了密钥时它自己短路放行（认证层才是边界），所以此处无条件调用。
        from agent_harness.web.projects import check_trusted_origin

        # 判据取自 `projects`（`check_trusted_origin`）：HTTP 面把它放进 403 的
        # `detail`，这里把它截断记进日志。注意：WS 的 close reason **不是**这条
        # 文案，而是本模块自有常量 `WS_ORIGIN_DENIED_REASON`（固定短 ASCII，
        # 受 ASGI ≤123 字节可打印 ASCII 约束）——`projects` 返回的文案只进日志。
        cross_origin = check_trusted_origin(
            headers.get("origin"),
            jwt_secret_configured=bool(self._settings.jwt_secret),
        )
        if cross_origin is not None:
            # 文案里含客户端可控、无长度上界的 `Origin` ⇒ 按长度截断，别让它灌水。
            logging.getLogger("agent_harness.web").warning(
                "拒绝跨源 WebSocket 握手：%s", cross_origin[:120],
            )
            # reason 固定短 ASCII（ASGI 限 ≤123 字节可打印 ASCII），来源只进日志。
            await send({"type": "websocket.close", "code": 1008,
                        "reason": WS_ORIGIN_DENIED_REASON})
            return
        # k8s 同款：承载 token 的子协议校验成功后**不回显**（防泄漏）——`negotiated`
        # 只可能是业务子协议（或 None）。浏览器请求了子协议却收不到回显会自己判
        # 握手失败（WHATWG establish-a-WebSocket-connection），故客户端另带一个。
        negotiated = _negotiate_subprotocol(subprotocols)

        async def send_with_subprotocol(message: Any) -> None:
            # `handle_websocket` 的 `accept()` 不带 subprotocol（它不关心协商），
            # 而浏览器**必须**收到一个回显——否则客户端自己判握手失败。在此补齐。
            if message["type"] == "websocket.accept" and negotiated is not None:
                message = {**message, "subprotocol": negotiated}
            await send(message)

        token = set_identity_context(identity)
        try:
            await self.app(scope, receive, send_with_subprotocol)
        finally:
            identity_context_var.reset(token)

    def _resolve_identity(self, authorization: str | None) -> IdentityContext:
        """**HTTP 与 WS 共用的唯一凭据判定点**（#890）。

        同一份 fail-closed 语义、同一份 claims 校验、同一份文案——两条传输面不
        各写一份，是"WS 与 HTTP 同口径"这条验收的结构性保证。未配置密钥 ⇒ 本地
        信任模式（local 身份），零行为变化。
        """
        if not self._settings.jwt_secret:
            return IdentityContext("local", "local", ["user", "session"])
        # R6-4/R8-3（用户拍板 fail-closed）：配置了密钥 = 需要认证。
        # 匿名请求不再静默降级为 trusted local（此前配合 CORS * 等于把
        # agent API 开放给任意网页）；无 exp 的 token 一并拒绝（强制
        # 过期语义，永不过期的签名 token 等于永久凭证）。
        if not authorization:
            raise _IdentityRejected("Missing identity token")
        try:
            scheme, encoded = authorization.split(" ", 1)
            if scheme.lower() != "bearer":
                raise ValueError("Expected Bearer token")
            # SecretStr 取明文给 jwt.decode；truthiness 判断仍基于密钥值
            # （SecretStr("") 为 falsy，未配置语义不变）。
            claims = jwt.decode(
                encoded, self._settings.jwt_secret.get_secret_value(),
                algorithms=["HS256"],
                options={"require": ["tenant_id", "user_id", "exp"]})
            tenant, user = claims["tenant_id"], claims["user_id"]
            scopes = claims.get("scopes", ["user", "session"])
            if (not isinstance(tenant, str) or not tenant.strip()
                    or not isinstance(user, str) or not user.strip()
                    or not isinstance(scopes, list)
                    or any(not isinstance(s, str) for s in scopes)):
                raise ValueError("Invalid identity claims")
            return IdentityContext(tenant, user, scopes)
        except (jwt.InvalidTokenError, ValueError) as invalid:
            raise _IdentityRejected("Invalid identity token") from invalid


def _package_version() -> str:
    """服务版本（health/version/capability 统一面，W-11 #355）。只读元数据，无副作用。"""
    try:
        return importlib.metadata.version("intelligence-agent")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def create_app(
    settings: Settings | None = None,
    *,
    enable_cors: bool = True,
    host_service_token: str | None = None,
) -> FastAPI:
    """装配 FastAPI 应用。测试可注入 test settings；生产默认从 .env 读。"""
    if settings is None:
        settings = Settings()

    state = AppState(settings)

    # lifespan 替代 on_event：进程退出时关闭 Memory 子系统，避免泄露外部连接。
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # web 部署必须自己接诊断日志：uvicorn 默认只配 uvicorn.* logger，root
        # 无 handler 时 runtime/executor 的 log_event 全部 no-op——llm_call /
        # tool_operation / task_failed 审计链路整条消失（cli.py 有 setup_logging，
        # web 之前漏接）。幂等（重复调用先清 handlers）。
        setup_logging(settings.log_level, settings.workspace_dir)
        # ARCH-7（#150）：启动期单实例锁。同一 session root 的第二个进程必须
        # 响亮失败——跨进程同时 append 会话 JSONL 会产出重复 seq / 交错写，
        # RunManager 的 run 归属共识也只在进程内有效；CLI 与 Web 并发同样被
        # 拒绝（有意行为，见 instance_lock 模块 docstring）。取在 setup_logging
        # **之后**：逃生门降级时那条 WARNING 才能落进 agent.jsonl，而不是只掉到
        # stderr（AC7 要求逃生门在日志里显著留痕）。
        instance_lock = InstanceLock(settings.workspace_dir).acquire()
        try:
            # Phase Multiturn T8（#138）：启动崩溃扫描——无终态 run 补记
            # run/interrupted + 强制 Ledger reconcile。失败不阻塞启动（单个坏会话
            # 不该让服务起不来），但必须响亮落日志。
            try:

                scan_results = await session_service(state).scan_interrupted()
                # #357 W-13（契约 5）：快照存 state，供只读端点消费——端点绝不
                # 重跑 scan_interrupted（后者写 run/interrupted + 跑 reconcile）。
                state.interrupted_scan = scan_results
                for result in scan_results:
                    logging.getLogger("agent_harness.web").warning(
                        "启动崩溃扫描：session=%s recovery=%s detail=%s",
                        result.session_id, result.recovery, result.detail,
                    )
            except Exception:
                logging.getLogger("agent_harness.web").exception(
                    "启动崩溃扫描失败（不阻塞启动）"
                )
            # #555：未完成 fork 扫描——`fork/in-progress` 无 `session/forked` 的
            # child，回收 harness 自建工件（暂存根 + 默认形态工作区），JSONL/
            # 映射保留作可读事实。与崩溃扫描同序：失败不阻塞启动，但响亮落日志。
            try:

                fork_results = await session_service(state).scan_unfinished_forks()
                for result in fork_results:
                    logging.getLogger("agent_harness.web").warning(
                        "启动 fork 扫描：child=%s reclaimed=%s detail=%s",
                        result.session_id, result.reclaimed, result.detail,
                    )
            except Exception:
                logging.getLogger("agent_harness.web").exception(
                    "启动 fork 扫描失败（不阻塞启动）"
                )
            # Phase Multiturn（ADR-0030 §4.8 / D5）：按事件流重建"未投递输入"的
            # 内存镜像。**不自动起 run**：刚启动没有订阅者，起了会被 orphan 回收，
            # 用户回来时会话已被跑掉（用户不在场时自动消耗 token 更不可接受）。
            # 用户在界面上看到「待发送 N」，点「立即发送」走 POST /queue/flush。
            try:

                rebuilt = await session_service(state).rebuild_message_queues()
                if rebuilt:
                    logging.getLogger("agent_harness.web").info(
                        "启动重建未投递输入：%d 个会话有待发送项", rebuilt,
                    )
            except Exception:
                # 失败同样不阻塞启动：未投递输入的事实仍在事件流里，用户随时能让
                # 它重新投递（flush 读的是事件流，不依赖这份镜像），所以降级安全。
                logging.getLogger("agent_harness.web").exception(
                    "启动重建未投递输入失败（不阻塞启动）"
                )
            try:
                yield
            finally:
                await state.shutdown()
                # 旁路收尾（ADR-0018 D3）：服务停机前尽力发送剩余 Langfuse span
                # （有超时上限，不阻塞退出）。
                flush_process_sink()
        finally:
            # 放在 shutdown 之后：仍在关连接时不该让第二个进程进来接手。
            instance_lock.release()

    app = FastAPI(title="Agent Harness Inspector", version="0.1.0", lifespan=lifespan)
    app.state.agent = state  # 挂在 app.state 上，路由通过 request.app.state 取
    app.state.host_service_token = host_service_token

    # #517 BUG-06：未捕获异常的全局兜底。Starlette 默认给 text/plain 的
    # "Internal Server Error"（TestClient 则直接 re-raise），前端按 JSON 解析
    # 错误体时拿到纯文本。注册 Exception handler 后换回 JSON 信封——状态码
    # 仍是诚实的 500，这层只换**表示**，不把任何意外异常洗成 4xx。
    # #753：磁盘满（ENOSPC/EFBIG）与 SQLite 存储满（SQLITE_FULL/SQLITE_IOERR）
    # 的集中转换点。`storage_http_status` 已定义映射（#515/#569：存储类错误 → 503），
    # 此处将其接入全局异常处理，使所有写磁盘的 API 路径（会话创建、事件落盘、
    # artifact 写入等）都能返回明确的 503 而非泛化的 500。未识别的 OSError /
    # sqlite3.Error 交还通用 500 处理（不洗成 4xx/5xx 的误报）。
    @app.exception_handler(OSError)
    async def _oserror_to_storage_status(request: Any, exc: OSError):
        status = storage_http_status(exc)
        if status is not None:
            logging.getLogger("agent_harness.web").warning(
                "存储写入失败（%s）：%s %s", exc, request.method, request.url.path
            )
            return JSONResponse(
                status_code=status,
                content={"detail": "磁盘空间不足，无法完成写入"},
            )
        return await _unhandled_exception_to_json(request, exc)

    @app.exception_handler(sqlite3.Error)
    async def _sqlite_error_to_storage_status(request: Any, exc: sqlite3.Error):
        status = storage_http_status(exc)
        if status is not None:
            logging.getLogger("agent_harness.web").warning(
                "存储写入失败（%s）：%s %s", exc, request.method, request.url.path
            )
            return JSONResponse(
                status_code=status,
                content={"detail": "磁盘空间不足，无法完成写入"},
            )
        return await _unhandled_exception_to_json(request, exc)

    @app.exception_handler(Exception)
    async def _unhandled_exception_to_json(request: Any, exc: Exception):
        logging.getLogger("agent_harness.web").exception(
            "未处理异常：%s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=500, content={"detail": "Internal Server Error"}
        )

    # #517 BUG-06：OpenAPI 错误面对齐（422 oneOf / SSE CT / 500+503 声明）。
    from agent_harness.web.error_contract import apply_error_contract

    apply_error_contract(app)

    # #548 / #562：422 校验错误出口换成不回显原文的安全实现。默认的
    # `jsonable_encoder(exc.errors())` 会把攻击者原文（`input`）带进响应体，
    # lone surrogate / `inf` / 深嵌套三种被判非法的输入都会让**错误处理器自己**
    # 抛异常 → 500（「正确地拒绝」退化成「拒绝时崩溃」）。位置紧挨 OpenAPI 对齐：
    # 两者都是「错误面」的护栏，且本函数不改任何真实成功响应的形状。
    from agent_harness.web.wire_safety import (
        BodyDepthGuardMiddleware,
        install_wire_safety,
    )

    install_wire_safety(app)

    # Phase 14 lineage 路由（独立 router 文件——流式改造重刀 app.py 时的最小接入面）
    from agent_harness.web.lineage import register_lineage_routes

    register_lineage_routes(app, validate_session_id=validate_session_id)

    # W-07 / #351 Task 交付状态路由（独立 router：查询 + 定义/修订/验证/接受/
    # 释放命令；投影单源在 session/task.py，本模块只留一行接入面）
    from agent_harness.web.task_delivery import register_task_routes

    register_task_routes(app, validate_session_id=validate_session_id)

    # W-08 / #352 证据投影路由（独立 router：记录 + 服务端投影查询；
    # 投影单源在 session/evidence.py，本模块只留一行接入面）
    from agent_harness.web.evidence_delivery import register_evidence_routes

    register_evidence_routes(app, validate_session_id=validate_session_id)

    # #368 / W-24 会话保留 / 空间显示 / 显式清理路由（独立 router：usage + 清理
    # 预览/执行；语义单源在 session/service.py，本模块只留一行接入面）
    from agent_harness.web.retention import register_retention_routes

    register_retention_routes(app, validate_session_id=validate_session_id)

    # W-06 / #350 进度文件重读对账路由（独立 router：对账状态查询 + 外部编辑
    # 冲突两出口；对账单源在 session/progress.py，本模块只留一行接入面）
    from agent_harness.web.progress_status import register_progress_routes

    register_progress_routes(app, validate_session_id=validate_session_id)
    # W-10 / #354 单目录写入租约路由（独立 router：状态/取得/释放/排队撤销；
    # 语义单源在 workspace/lease.py，本模块只留一行接入面）
    from agent_harness.web.task_lease import register_task_lease_routes

    register_task_lease_routes(app, validate_session_id=validate_session_id)

    # #367 / W-23 选项 A：git worktree 路由（独立 router：目录冲突默认自动
    # worktree 并行隔离，"排队等"作次选项走既有租约 API）
    from agent_harness.web.worktrees import register_worktree_routes

    register_worktree_routes(app)

    # #367 / W-23 选项 A：sandbox 后端列表（`probe_all_capabilities` 明确标注
    # "给选择器 UI 用"；创建表单的"运行位置"选择器只显示探针真实结果，不画饼）
    @app.get("/api/sandbox-backends")
    async def list_sandbox_backends() -> dict:
        """可用 sandbox 后端 + 探针结果（不可用带 reason，前端据此置灰）。"""
        return {
            "backends": [
                {
                    "backend": c.backend,
                    "available": c.available,
                    "reason": c.reason,
                    "details": c.details,
                }
                for c in probe_all_capabilities()
            ]
        }

    # #937 / M-08：附件上限的服务端权威下发（前端 `IMAGE_LIMITS` 手写镜像就此退役）。
    # 值经 `resolve_image_limits` 单一解析点读出，不许在此直接逐个拼
    # `settings.attachment_max_*`（第二份解析会让两处规则漂移）。
    from agent_harness.attachments.types import resolve_image_limits

    @app.get("/api/attachments/limits")
    async def get_attachment_limits() -> dict:
        """附件图片上限（前端上传闸门用，部署者改配置后刷新页面即生效）。

        纯查询、零副作用：只把部署者的非秘密配置值（字节数 / 张数 / 类型名）
        映射成前端 `ImageIntakeLimits`（camelCase）形状；不含密钥、路径、
        用户信息。鉴权走既有 `AuthSeamMiddleware`，与 `/api/sandbox-backends`
        完全一致——不新增豁免，也不新增鉴权逻辑。
        """
        limits = resolve_image_limits(settings)
        return {
            "maxImageBytes": limits.max_image_bytes,
            "maxImagesPerMessage": limits.max_images_per_message,
            "maxMessageImageBytes": limits.max_message_image_bytes,
            "allowedMediaTypes": list(limits.media_types),
        }

    # #362 / W-18：MCP server 状态与断开（Chrome DevTools MCP 可选 capability）。
    # 来源闸与下方项目路由共用 `require_trusted_origin`（ADR-0025 D1），此处先导入。
    from agent_harness.web.projects import require_trusted_origin

    @app.get("/api/mcp/servers")
    async def list_mcp_servers() -> dict:
        """已装配的 MCP server + 连接状态（未装配时空列表，不 503——
        "没配 MCP" 是缺省态，不是故障）。"""
        from agent_harness.mcp.capability import MCPCapability

        _, wiring = await state.get_wiring()
        capability = next(
            (c for c in wiring.lifecycle if isinstance(c, MCPCapability)), None
        )
        if capability is None:
            return {"servers": []}
        return {
            "servers": [
                {"name": name, "connected": capability.is_connected(name)}
                for name in capability.server_names()
            ],
            "errors": list(capability.errors),
        }

    @app.post("/api/mcp/servers/{server_name}/disconnect")
    async def disconnect_mcp_server(
        server_name: str,
        _: None = Depends(require_trusted_origin),
    ) -> dict:
        """断开指定 MCP server（#362 / W-18：Chrome MCP 手动断开）。

        关闭其连接；该 server 的工具此后调用按既有语义失败（不静默）。
        语义：200 → `{name, disconnected}`（幂等，无此 server 也 200）；
        来源闸（ADR-0025 D1）：连接管理是宿主侧动作，只接受本机来源。
        """
        from agent_harness.mcp.capability import MCPCapability

        _, wiring = await state.get_wiring()
        capability = next(
            (c for c in wiring.lifecycle if isinstance(c, MCPCapability)), None
        )
        if capability is None:
            return {"name": server_name, "disconnected": False}
        disconnected = await capability.disconnect_server(server_name)
        return {"name": server_name, "disconnected": disconnected}

    # WS-4 / #154 项目 CRUD 路由（同为独立 router：本模块只留这一行接入面）
    # `require_trusted_origin` 已在上方 #362 处导入（ADR-0025 D1 共用来源闸）。
    from agent_harness.web.projects import register_project_routes

    register_project_routes(app)

    # MEM-4 / #159 记忆入口（列出 / 硬删；同为独立 router）
    from agent_harness.web.memory import register_memory_routes

    register_memory_routes(app)

    # #529 T-529-5：skill 目录只读展示（catalog 列表 + 待审草稿；无管理按钮）
    from agent_harness.web.skills import register_skill_routes

    register_skill_routes(app)

    # WS-7 / #170 宿主只读目录列举（目录选择器的唯一可行路径，ADR-0028）
    from agent_harness.web.host_dirs import register_host_dir_routes

    register_host_dir_routes(app)

    # #191 会话工作区只读浏览（列文件 / 读文件 / git status / 单文件 diff）。
    # 同样是独立 router + 一行接入：路径边界与来源闸都用既有的那一份（Sandbox /
    # `require_trusted_origin`），本模块不新造校验。
    from agent_harness.web.workspace_files import register_workspace_file_routes

    register_workspace_file_routes(app, validate_session_id=validate_session_id)

    # #203 / ADR-0032 自定义供应商管理（CRUD + 连接测试；独立 router）。
    # 凭据读写是宿主侧敏感操作，来源闸用既有的 `require_trusted_origin`（同款）。
    from agent_harness.web.model_providers import register_model_provider_routes

    register_model_provider_routes(app)

    # #822 / MM-01 用户图片附件入站（独立 router：上传 + 受控读回）。上传请求体走
    # 流式字节、可达单张图上限，故该路由从 BodyDepthGuardMiddleware 的 1 MiB 配额中
    # 豁免（下方 add_middleware 处传入 `ATTACHMENT_UPLOAD_PATH_RE`）；既有 JSON 端点
    # 行为逐字不变。
    from agent_harness.web.attachments import (
        ATTACHMENT_UPLOAD_PATH_RE,
        register_attachment_routes,
    )

    register_attachment_routes(app, validate_session_id=validate_session_id)

    if not settings.jwt_secret:
        # R6-4：未配置密钥 = 本地信任模式（fail-open）。保留开发便利，但必须
        # 响亮告知——静默降级是原审计的核心危害。
        logging.getLogger("agent_harness.web").warning(
            "JWT_SECRET 未配置：API 以本地信任模式运行（所有请求视为 local 身份，"
            "不做身份校验）。生产部署必须配置 JWT_SECRET。"
        )

    # CSP（集成 AI 移交，INTEGRATION_NOTES §4.1）：静态 HTML 的纵深防御——
    # 脚本/样式只认同源构建产物，img 放行 data:。所有响应统一携带（浏览器
    # 仅对 HTML 文档执行，JSON 响应带此头无害），避免漏掉任何静态入口。
    _CSP_POLICY = "default-src 'self'; img-src 'self' data:"

    # 纯 ASGI 中间件（见类 docstring）：body 配额守卫（嵌套深度 + 字节体积）最先
    # 添加 = 最内层，落点**在认证内层**——未认证请求先被 401 挡下，不做无谓的
    # body 扫描；先 csp（内层）后 auth（外层），与旧 BaseHTTPMiddleware 版注册
    # 顺序逐层一致；CORS 仍最后添加 = 最外层。
    app.add_middleware(
        BodyDepthGuardMiddleware, exempt_path_pattern=ATTACHMENT_UPLOAD_PATH_RE
    )
    app.add_middleware(CSPHeaderMiddleware)
    app.add_middleware(
        AuthSeamMiddleware,
        settings=settings,
        host_service_token=host_service_token,
    )

    if enable_cors:
        # V1 本地单用户：宽松 CORS 让 Vite dev server (5173) 能直连。
        # 多用户时收紧到已知 origin（接缝点）。
        # 必须最后添加 = 中间件栈最外层：浏览器预检（OPTIONS）天然不携带
        # Bearer，认证层在内层时预检直接 401，配置 JWT_SECRET 后跨域 dev
        # 模式整体失效。预检放行不削弱认证——数据请求仍逐个过认证层。
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=sorted(EXPOSED_CUSTOM_RESPONSE_HEADERS),
        )

    # ── 路由 ──

    @app.get("/api/sandbox/capabilities")
    async def sandbox_capabilities() -> dict[str, object]:
        """#363 / W-19：Sandbox 后端能力探针（给选择器 UI 用）。

        返回各后端可用性 + 不可用原因 + 修复提示。纯查询，无副作用
        （不创建容器、不拉镜像）。前端据此渲染选择器：可用项可选，
        不可用项**置灰保留并显示原因**（抄 Cline "rather than silently
        disappearing"，不隐藏）。
        """
        return {
            "backends": [
                {
                    "backend": c.backend,
                    "available": c.available,
                    "reason": c.reason,
                    "details": c.details,
                }
                for c in probe_all_capabilities()
            ],
        }

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        """存活探针 + durability 观测（#515）+ 附着协议形状（W-11 #355）。

        `checkpoint_save_failures` 是进程级累计的 checkpoint 维护失败次数（重启归零）：
        非零说明有 checkpoint 帧丢失，之后的 resume 可能回到更旧的稳定边界——这是
        ADR-0004 Round 5 之下 checkpoint 故障唯一的对外口径（不进 SessionEvent）。

        W-11：本端点**匿名可达**（AuthSeamMiddleware 豁免），是附着核验的第一站；
        载荷只含非秘密字段——协议版本 / 服务版本 / capability 名称（静态配置名，
        非秘密）+ 是否强制鉴权，token/路径/用户信息一概不进。
        """
        jwt_configured = bool(settings.jwt_secret and settings.jwt_secret.get_secret_value())
        try:
            capability_names: list[str] = sorted(parse_capabilities_config(settings.capabilities))
        except CapabilityError:
            # 配置非法由装配路径显式报错（init_failed）；健康面不能因此塌掉。
            capability_names = []
        return {
            "status": "ok",
            "checkpoint_save_failures": checkpoint_save_failure_count(),
            "protocol_version": HOST_PROTOCOL_VERSION,
            "version": _package_version(),
            "capabilities": capability_names,
            "auth_required": jwt_configured,
        }

    @app.get("/metrics")
    async def metrics() -> Response:
        """进程健康观测面（#612）：RSS / gc 堆 / 存活 task / uptime。

        Prometheus 文本暴露格式（v0.0.4）；Diagnostic 层，Event≠Log，不进
        SessionEvent。读取零副作用（gc 扫描在 worker 线程），单项采集失败
        ⇒ 该指标整行缺席（缺席≠占位值），不影响任何业务路径（不变量 #21）。
        默认无鉴权——与 /api/health 同一口径（AC5，不引入开关）。
        """
        return Response(
            content=await collect_process_metrics(),
            media_type=METRICS_CONTENT_TYPE,
        )

    @app.get("/api/sessions")
    async def list_sessions(
        workspace_id: str | None = None,
        include_archived: bool = False,
        limit: int | None = Query(default=None, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[SessionSummary]:
        """列历史 session。

        - 不带参数：全部会话，按**最近活动**倒序（既有契约与快路径取舍不变）。
        - `?workspace_id=<项目 id>`：只列该项目的会话，顺序 = **账本的手工序**
          （AC4：不按活动时间重排）；项目未注册 → 404（不伪装成空列表）。
        - `?include_archived=true`（#171）：把已归档的会话也列出来（前端"显示已归档"
          开关）。**默认 false 即不列**；两条路径（默认列表 / 项目视图）同一规则。
          非布尔值 → 422（FastAPI 的 bool query 语义，不自造一套）。
        - `?limit=N&offset=M`（#516）：分页窗口——N 是行数上限（1–500），M 是起点。
          校验交给 FastAPI Query（ge/le），非法值 422。切片在读摘要之前 ⇒ 摘要扫描
          是 O(limit)；归档过滤先行，已归档行不占 limit 预算；不传 limit 保持
          全量语义（既有调用方零迁移）。**id 枚举仍是每请求 O(N) 文件 stat**——
          mtime 倒序契约会响应外部 utime 重排（承重测试钉住）⇒ id 列表不可缓存，
          4000 会话下这是并发 p95 的主项（§9.1.1 已登记待裁决）。
        每行都带 `workspace`（`null` = 未分组）与 `archived`（徽标真值）。

        列表页只需摘要字段——store.read_session_summary 单趟流式扫描
        （头部早退 + 末行），不再全量解析每个 JSONL（30 会话 × 2000 事件
        曾需秒级串行解析，现约几十 ms）；#516 起同会话重复读由文件戳缓存兜住，
        翻页/刷新不再重扫未变化的文件。损坏行走 store 内全量回退，摘要
        语义与旧实现严格一致。同步磁盘 I/O 仍走 to_thread 卸载。
        """
        service = session_service(app.state.agent)
        try:
            summaries = await service.list_sessions(
                workspace_id=workspace_id,
                include_archived=include_archived,
                limit=limit,
                offset=offset,
            )
        except WorkspaceNotFound as e:
            raise http_error(e) from e
        return [
            SessionSummary(
                session_id=s.session_id,
                event_count=s.event_count,
                first_event_time=s.first_event_time,
                last_event_time=s.last_event_time,
                first_user_message=s.first_user_message,
                trace_id=s.trace_id,
                trace_url=s.trace_url,
                # 领域值对象 → 传输模型（两者刻意同名不同物：前者无校验、不依赖
                # Pydantic；后者是契约与 OpenAPI schema 的定义点）。
                workspace=(
                    WorkspaceRef(id=s.workspace.id, title=s.workspace.title)
                    if s.workspace is not None
                    else None
                ),
                archived=s.archived,
                corrupted=s.corrupted,
            )
            for s in summaries
        ]

    @app.get("/api/sessions/{session_id}/events")
    async def get_session_events(session_id: str) -> list[dict]:
        """读历史 SessionEvent——前端刷新后从此重建视图（不变量 #22）。

        session_id 先过安全校验（名字段，不是路径）；store 读是同步磁盘 I/O，
        走 to_thread 卸载（同 list_sessions）。
        """
        service = session_service(app.state.agent)
        try:
            events = await service.get_events(session_id)
        except (InvalidSessionId, SessionNotFound, EventLogCorruptError) as e:
            raise http_error(e) from e
        return [e.to_dict() for e in events]

    @app.get("/api/sessions/{session_id}/project-instructions")
    async def get_project_instructions_status(
        session_id: str,
        _: None = Depends(require_trusted_origin),
    ) -> dict[str, object]:
        """Report which repository instruction files were loaded for this session."""
        service = session_service(app.state.agent)
        try:
            await service.get_events(session_id)
        except (InvalidSessionId, SessionNotFound) as error:
            raise http_error(error) from error
        return project_instruction_store(
            app.state.agent.settings,
        ).status_for_session(session_id)

    @app.post("/api/sessions/{session_id}/project-instructions/reload")
    async def reload_project_instructions(
        session_id: str,
        _: None = Depends(require_trusted_origin),
    ) -> dict[str, object]:
        """Explicitly reload repository instructions for subsequent model requests."""
        service = session_service(app.state.agent)
        try:
            events = await service.get_events(session_id)
        except (InvalidSessionId, SessionNotFound) as error:
            raise http_error(error) from error
        cwd = session_cwd(events)
        if cwd is None:
            return empty_project_instruction_status("no_cwd")
        store = project_instruction_store(app.state.agent.settings)
        await asyncio.to_thread(store.reload_for_session, session_id, cwd)
        return store.status_for_session(session_id)

    # Read-only model/profile catalogs use a narrow dependency seam and are
    # registered as one explicit router instead of being captured by this factory.
    register_catalog_routes(app)

    @app.post("/api/sessions")
    async def create_session(
        req: CreateSessionRequest, launch: bool = True,
    ):
        """起新 session + 跑任务，流式返回 AgentEvent（SSE）。

        ADR-0016 §2.1（D-A）：run 由 RunManager 以 detached task 驱动，与
        本次 HTTP 请求生命周期解耦——断连（本 generator 被取消）只做
        unsubscribe，run 继续跑到终态；显式取消走 POST /cancel。

        `launch`（#204，query 参数，默认 true ⇒ 既有行为逐字不变）：
        - true：现有路径（create_and_launch，SSE 直驱 run）；task 必填。
        - false：**只建会话**——返回会话 JSON（非 SSE），不启动 run、不返回
          SSE；task 可省略。给了 task 又 launch=false ⇒ 422（"给了任务却
          静默不执行"的矛盾组合必须显式拒绝）。
        会话级 `permission_mode` 通过 `X-Permission-Mode` 响应头回传（launch=true
        的 SSE 响应没有 JSON 体可承载元数据；launch=false 的 JSON 体里也带
        同名字段）——前端用它初始化 composer 权限 pill（#204 裁定 §3：不要
        各自取默认值，那正是不一致的来源）。
        """
        # launch/Task 互斥（#204 裁定 §2）：给了任务却静默不执行是最坏的一种
        # "宽容"——矛盾组合必须显式拒绝，而不是挑一个语义执行。
        if not launch and req.task is not None:
            raise HTTPException(
                status_code=422,
                detail="task 与 launch=false 互斥：要么带 task 启动 run（launch=true），"
                       "要么只建会话（省略 task）",
            )
        if req.remember_as_procedural_rule and (
            not launch or req.task is None or not req.task.strip()
        ):
            raise HTTPException(
                status_code=422,
                detail="remember_as_procedural_rule requires a launched non-blank user task",
            )
        # run 预算与 launch=false 互斥（#422）：`budget.run` 是启动 run 的请求上的
        # per-run 绝对上限（`run/started.data.budget` 是它唯一的宿主）；launch=false
        # 没有 run 可挂——静默收下等于客户端以为设了防失控闸而实际什么都没设。
        # 对照：`budget.session` 在 launch=false 合法（session 账行就是宿主，#318）；
        # `budget.local` 是请求级熔断，launch=false 下不投影（见 `_local_fuse_headers`）。
        if not launch and req.budget is not None and req.budget.run is not None:
            raise HTTPException(
                status_code=422,
                detail="budget.run 与 launch=false 互斥：run 级预算属于启动 run 的请求"
                       "（POST /messages 或 launch=true）；只建会话请省略 budget.run",
            )
        if launch and req.task is None:
            # launch=true 恢复既有契约：task 必填（422，行为与原 min_length 校验一致）。
            raise HTTPException(status_code=422, detail="Field required (task)")
        service = session_service(app.state.agent)
        state = app.state.agent

        # context_providers handler-level 422（ADR-0021 模式，适配 ADR-0020b 的
        # name 属性机制）：validator 无法访问 AppState/wiring（Pydantic parse 早于
        # handler），故在 handler 内对 wiring 真实装配的 id 集合校验——与 model
        # 字段的 from_catalog 422 模式一致。未知 id → 422 + 可用清单。
        await _validate_wired_context_providers(state, req.context_providers)

        permission_mode = PermissionPolicy(req.permission_mode)
        permission_mode_explicit = "permission_mode" in req.model_fields_set
        auto_approve_explicit = "auto_approve" in req.model_fields_set

        # #367 / W-23 选项 A：三档自主度映射到底层（显式声明优先于 auto_approve）。
        auto_approve = req.auto_approve
        workflow_mode_plan = False
        if req.autonomy == "ask":
            auto_approve, auto_approve_explicit = False, True
        elif req.autonomy == "auto":
            auto_approve, auto_approve_explicit = True, True
        elif req.autonomy == "plan":
            # P2 修复（#367 审查）：plan 档声明 auto_approve=True。计划阶段变更
            # 工具本来就被 Plan 档只读门禁拦截（executor 阶段 2.35b，先于审批
            # 闸门 2.5），声明 True 不影响计划期安全；用户批完计划（PLAN→NORMAL）
            # 后执行不再逐次问——"先计划后执行"档的语义就是"只审一次计划"。
            auto_approve, auto_approve_explicit = True, True
            workflow_mode_plan = True

        # #367 / W-23 选项 A：目录冲突默认 worktree。cwd 被租约占用且
        # on_conflict=worktree（默认）时，自动建 worktree 并用其路径建会话；
        # on_conflict=queue 则走既有租约排队（次选项，本处不干预）。
        # worktree 建不出（非 git 仓库等）→ 422 fail-closed：不静默回落到排队。
        worktree_path: str | None = None
        cwd = req.cwd
        if cwd and req.on_conflict == "worktree":
            # PRD WS6/WS7：cwd 格式校验先于租约检查，文案走 PRD 契约
            # （`cwd 必须是绝对路径` / `目录不存在` / `不是目录`），不透传
            # lease_paths 的通用文案。
            from agent_harness.session.service import SessionService
            try:
                SessionService._resolve_cwd(cwd)
            except Exception as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
            await state.ensure_stores()
            try:
                dir_locked = await _is_dir_locked(state, cwd)
            except LeasePathError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
            if dir_locked:
                try:
                    wt = await anyio.to_thread.run_sync(
                        partial(create_worktree, Path(cwd)))
                except WorktreeError as e:
                    raise HTTPException(
                        status_code=422, detail=str(e)) from e
                worktree_path = str(wt)
                cwd = worktree_path

        try:
            result = await service.create_and_launch(
                task=req.task,
                workspace_name=req.workspace,
                cwd=cwd,
                # `#318`：新建会话没有 session 账行 ⇒ session CAS 版本是矛盾请求。
                **budget_claims(req.budget, create_surface=True),
                permission_mode=permission_mode,
                permission_mode_explicit=permission_mode_explicit,
                auto_approve_explicit=auto_approve_explicit,
                auto_approve=auto_approve,
                amend=AmendOptions.from_request(req),
                launch=launch,
                remember_as_procedural_rule=req.remember_as_procedural_rule,
                # #363 / W-19：显式 sandbox 后端选择。
                sandbox_backend=req.sandbox_backend,
            )
            # #367 / W-23 选项 A："plan" 档：创建后切 workflow_mode=plan
            #（计划闸门 W-07：先出计划、用户批准后执行）。
            if workflow_mode_plan:
                await service.set_workflow_mode(
                    result.session.session_id, WorkflowMode.PLAN)
        except (WorkspaceNameInvalid, InvalidDecision, BudgetRejection) as e:
            raise http_error(e) from e
        except UnsupportedReasoningEffort as e:
            raise model_http_error(e) from e
        except ModelClientConstructionError as e:
            # #517 BUG-05：client 构造期失败（代理环境/配置问题）→ 503。
            raise model_http_error(e) from e
        except StorageBusyError as e:
            # #515（审查 P2-3）：launch 路径的 transport/delegation 写锁耗尽 → 503。
            raise storage_http_error(e) from e
        except SandboxUnavailableError as e:
            # #363 / W-19：选定的后端不可用 → 409 + 结构化诊断（绝不静默降级）。
            # detail 带 remediation 与可用后端列表，前端据此渲染"切换"动作。
            raise HTTPException(
                status_code=409,
                detail={
                    "message": str(e),
                    "backend": e.backend,
                    "reason": e.reason,
                    "details": e.details,
                    "remediation": e.remediation,
                    "available_backends": [
                        c.backend
                        for c in probe_all_capabilities()
                        if c.available
                    ],
                },
            ) from e

        session, run, subscriber = result.session, result.run, result.subscriber

        # 只读投影（#308 Must Do）：生效 local fuse + 来源，SSE 与 JSON 两条路径都带
        # （SSE 没有 JSON 体可承载元数据，`X-Permission-Mode` 是同一条先例）。
        #
        # 只建会话（`launch=False`）**不投影** fuse：那个值按"本次请求若启动 run 会生效几轮"
        # 解析，而它既不持久化、也没被任何 run 消费（后续 `/messages` 会按当时的 Deployment
        # 重新解析）——回一个请求级数字当会话级 ceiling 是假事实。
        fuse_headers = _local_fuse_headers(result.local_fuse) if launch else {}
        headers = {"X-Permission-Mode": permission_mode.value, **fuse_headers}
        # #367 / W-23 选项 A：launch=true 走 SSE（无 JSON 体）→ worktree 信息走
        # 响应头（`X-Permission-Mode` 同型先例）；#765：非 latin-1 路径改发编码
        # 伴随头（`_worktree_headers`），不在响应构造期 500。
        if worktree_path is not None:
            headers.update(_worktree_headers(worktree_path))

        # #204：只建路径——返回会话 JSON（非 SSE）。形状刻意小：只回传前端
        # 初始化 composer 状态所需的字段（id + 权限档位），不伪造事件数/标题
        # （那些是列表页的投影字段，这里没有数据来源）。
        # #367 / W-23 选项 A：worktree 建出时回传路径 + 冲突策略（前端据此提示
        # "已在 worktree 中创建"；不画饼——只报实际生效的）。
        if not launch:
            content: dict[str, Any] = {
                "session_id": session.session_id,
                "permission_mode": permission_mode.value,
                "on_conflict": req.on_conflict,
            }
            if worktree_path is not None:
                content["worktree_path"] = worktree_path
                content["cwd"] = worktree_path
            return JSONResponse(
                status_code=200,
                headers=headers,
                content=content,
            )

        return _run_stream_response(
            state, run, subscriber, session.session_id, headers=headers,
        )

    @app.get("/api/sessions/{session_id}/stream")
    async def stream_session(session_id: str, after_seq: int = -1):
        """重连续传（ADR-0016 §2.3，C4/C5）：重放 durable 事实 + 接上在途流。

        客户端维护 lastAppliedSeq，断线后带 after_seq 重连：
        1. session 无事件 → 404；
        2. 重放 durable 事件（after_seq < seq ≤ replay_upto，按 seq 序）；
        3. backlog 超过 STREAM_REPLAY_MAX_EVENTS → 单帧 stream/truncated
           控制事件（无 seq，非运行事实）后收流——客户端走 GET /events
           全量重建后带 after_seq=latest_seq 重连；
        4. run 在途 → 接上 live 流（先订阅后取游标，保证重放与 live 无缝
           无重复）；run 已终态/不在途 → 重放到 latest 后正常收尾
           （崩溃遗留的悬空 run 不伪造终态，修复走 POST /recover）。
        客户端对重放帧与 live 帧做同一 seq 幂等投影（C5）。
        """
        service = session_service(app.state.agent)
        try:
            handle = await service.stream_reconnect(
                session_id=session_id,
                after_seq=after_seq,
                max_replay_events=STREAM_REPLAY_MAX_EVENTS,
            )
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e

        events = handle.events
        latest_seq = handle.latest_seq
        run = handle.run
        subscriber = handle.subscriber
        replay_upto = handle.replay_upto
        state = app.state.agent

        if latest_seq - after_seq > STREAM_REPLAY_MAX_EVENTS:
            # 截断分支必须**先退订**（与 WS 的 `_push_snapshot` 同一条纪律）：
            # `stream_reconnect` 按协议先订阅后取游标（重放与 live 无缝无重复），
            # 所以走到这里时 subscriber 已经在 `run.subscribers` 里了。这一支直接
            # return，那个队列**永远没人消费**——subscriber 留在字典里 ⇒ 孤儿计时
            # 也不会被激活（它只在 subscribers 为空时武装），run 之后每次 fanout
            # 都会往里写。客户端收到控制帧就去重建了，不会有人来读它。
            if subscriber is not None and run is not None:
                run.unsubscribe(subscriber)

            async def truncated_generator():
                # 帧形状来自 serialization 的**单一构建点**（#208）：WS 快照
                # 超限时发的是同一个函数产出的帧，两条通道一字不差。
                control = build_truncated_control(
                    session_id, after_seq=after_seq, latest_seq=latest_seq,
                )
                yield {"data": json.dumps(control, ensure_ascii=False)}

            return _sse_response(truncated_generator())

        async def event_generator():
            try:
                for event in events:
                    if after_seq < event.seq <= replay_upto:
                        yield _session_event_to_sse_dict(event, session_id)
                if subscriber is None:
                    return
                while True:
                    item = await subscriber.queue.get()
                    if item is state.run_manager.DONE:
                        break
                    if item.seq is not None and item.seq <= replay_upto:
                        continue  # 重放已覆盖（订阅与取游标窗口内的入队）
                    yield _event_to_sse_dict(item, session_id)
            finally:
                if subscriber is not None and run is not None:
                    run.unsubscribe(subscriber)

        return _sse_response(event_generator())

    @app.post("/api/sessions/{session_id}/resume")
    async def resume_session(session_id: str, req: ResumeRequest):
        """恢复会话：新任务续聊（带 task）或同 run 续跑（不带 task，`#312`）。

        带 task 时是「续跑」而非精确恢复中断 run：Session.resume() 重建 append-only
        历史与 dangling 修复，RunManager.launch 驱动一轮新的 Agent Loop。
        不带 task 时接上被暂停的逻辑 run：锁内 CAS 校验（run_id + expected_version +
        resume_basis + 绝对 ceiling）通过后落 `run/resumed`，再以**同一 run_id** 启动
        一次新执行；被拒请求（422/409）不建目录、不落事件、不启动任何模型/工具工作。
        在途 session 一律 409，避免同一 session 并发两轮。
        """
        service = session_service(app.state.agent)
        amend = AmendOptions.from_request(req)
        await _validate_amend_for_existing_session(app.state.agent, amend)
        try:
            result = await service.resume_and_launch(
                session_id=session_id,
                task=req.task,
                user_input_metadata=(
                    {"remember_as_procedural_rule": True}
                    if req.remember_as_procedural_rule else None
                ),
                resume_run_id=req.run_id,
                resume_basis=req.resume_basis,
                input_request=(
                    req.input_request.model_dump(exclude_none=True)
                    if req.input_request is not None else None
                ),
                **budget_claims(req.budget, resume_surface=True),
                amend=amend,
            )
        except (
            InvalidSessionId,
            SessionNotFound,
            ActiveRunConflict,
            RecoveryConflict,
            EventLogCorruptError,
            SeqConflict,
            WorkspaceBindingConflict,
            WorkspaceNotFound,
            BudgetRejection,
            BudgetConflict,
        ) as e:
            # RecoveryConflict → 409（T8 #138）：崩溃遗留需人工裁决的 UNKNOWN
            # tool_call，不伪造结果（不变量 #14）。
            # EventLogCorruptError → 409（#565）：events.jsonl 完整性闸门拒绝——
            # 完整坏行 / seq 断层 / seq 重复，不可重试，需人工修复。
            # WorkspaceBindingConflict → 409（#266）：cwd 锚与沙箱映射互相矛盾，
            # 拒绝静默选边（判定见 `service._reconcile_workspace_binding`）。
            # WorkspaceNotFound → 404（中央映射既有条目）：#266 守卫对 cwd 已删
            # 的会话在 resume 路径本来就在抛，#564 审查 P2-1 修复前它被 validator
            # 的 sandbox mkdir 掩蔽（守卫赶不上 mkdir），修复后真正可见——补进
            # 元组让既存映射生效，不再以裸 500 呈现。
            # BudgetConflict → 409（#312）：CAS 版本过期 / 不是暂停的那个 run /
            # ceiling 没真提高 / 有在途 run——请求形状合法但状态对不上，且
            # **零副作用**（判定在任何落盘之前，见 `validate_resume`）。
            raise http_error(e) from e
        except ModelClientConstructionError as e:
            # #517 BUG-05：同 create——构造期失败 → 503，不冒充 500。
            raise model_http_error(e) from e
        except UnsupportedReasoningEffort as e:
            raise model_http_error(e) from e
        except StorageBusyError as e:
            # #515（审查 P2-3）：同 create——launch 路径写锁耗尽 → 503。
            raise storage_http_error(e) from e

        session_id = result.session.session_id
        return _run_stream_response(
            app.state.agent, result.run, result.subscriber, session_id,
            headers=_local_fuse_headers(result.local_fuse),
        )

    @app.get("/api/sessions/{session_id}/budget")
    async def get_session_budget(session_id: str) -> dict[str, Any]:
        """run 预算投影（`#313`，`11 §6.1`）：identity / version / 绝对 ceilings /
        consumed / remaining / **可执行性** / 暂停原因 + continuation。

        只读、幂等、**不启 run、不写事件**（`11 §6.1` 把投影定位成"客户端读当前
        账本"，不是另一条会改状态的通道）。真相全部来自 append-only 事件
        （`SessionEvent` 派生，`derive_run_budget`）——刷新 / 重启 / replay 之后
        同一个会话给出同一份投影（不变量 #22：Web 不维护第二套 Session 真相）。

        四维里某维不可得时是 `null`（unavailable），**永不** 0；`enforcement` 说明
        本部署的 Provider 链能不能真的强制某一维（生产链 `max_cost_usd=unavailable`
        ⇒ 显式配它会被 422 拒绝，见 `model/accounting.py`）。
        """
        service = session_service(app.state.agent)
        try:
            return await service.budget_projection(session_id)
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str) -> dict[str, str]:
        """显式取消在途 run（ADR-0016 §2.2，D-A）：前端 Esc/停止的唯一取消通道。

        detached-run 下断连不再取消——本端点是仅有的两个外部终止路径之一
        （另一个是孤儿回收）。语义：在途 → 200 cancelling；无在途 run →
        200 no_active_run（幂等成功：用户按 Esc 与 run 恰好刚终结的竞态是
        常态不是错误）；session 不存在 → 404。取消与失败不混淆（02 §17）：
        run/failed data.reason=cancelled，与异常臂（reason=分类码或异常类型名）、
        孤儿回收（reason=orphaned）区分。
        """
        service = session_service(app.state.agent)
        try:
            cancelled = await service.cancel(session_id)
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        return {"status": "cancelling" if cancelled else "no_active_run"}

    @app.post("/api/sessions/{session_id}/archive")
    async def archive_session(
        session_id: str, _: None = Depends(require_trusted_origin)
    ) -> SessionArchived:
        """归档会话（#171）：把它从默认列表里收起来，**可逆**、不删任何东西。

        与 `DELETE /api/sessions/{id}`（硬删）刻意分成两个动作：归档只写
        `session_meta.archived` 一个标记——事件日志、项目账本、checkpoint、沙箱工件
        全部原样（spec 03 的 Full SessionEvent History 约束），所以入口层不需要二次
        确认；硬删不可逆，才需要确认面。

        语义：200 → `{id, archived: true}`（**幂等**：已归档再归档仍是 200）；
        404 → 没有这个会话；409 → 有在途 run（`get_active`，详情说明"运行中的会话不能
        归档"）；422 → id 形态非法。取消归档走 `DELETE`（同路径），且**不**因在途 run
        拒绝——它只是把行放回列表。

        动词选择：本仓既有会话端点一律显式动词（`resume`/`cancel`/`approve`/`recover`/
        `model`），不用泛化 PATCH。

        来源闸（ADR-0025 D1）：归档改的是宿主侧列表可见性，只接受本机来源
        （与项目 / 目录 / 记忆 / 工作区端点同一份实现）。
        """
        service = session_service(app.state.agent)
        try:
            archived = await service.set_archived(
                session_id, archived=True, entry_point=ARCHIVE_ENTRY_API
            )
        except (InvalidSessionId, SessionNotFound, ActiveRunConflict) as e:
            raise http_error(e) from e
        except StorageBusyError as e:
            # #515：共享 harness.db 写锁重试耗尽——暂时性故障报 503，不伪装成 500。
            raise storage_http_error(e) from e
        return SessionArchived(id=session_id, archived=archived)

    @app.delete("/api/sessions/{session_id}/archive")
    async def unarchive_session(
        session_id: str, _: None = Depends(require_trusted_origin)
    ) -> SessionArchived:
        """取消归档（#171）：把会话放回默认列表。

        语义：200 → `{id, archived: false}`（幂等）；404 → 没有这个会话；422 → id
        形态非法。**没有 409**：把行放回列表不破坏任何人的前提，在途 run 也无所谓
        （见 `SessionService.set_archived` 里那条非对称的理由）。
        """
        service = session_service(app.state.agent)
        try:
            archived = await service.set_archived(
                session_id, archived=False, entry_point=ARCHIVE_ENTRY_API
            )
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        except StorageBusyError as e:
            raise storage_http_error(e) from e
        return SessionArchived(id=session_id, archived=archived)

    @app.post("/api/sessions/{session_id}/budget/purge-stale-tools")
    async def purge_stale_tools(
        session_id: str,
        tool: str | None = None,
        _: None = Depends(require_trusted_origin),
    ) -> SessionToolLimitsPurged:
        """清除陈旧工具 ceiling（#616）：摘掉 `session_budgets.tool_call_limits` 里
        **不在根 registry** 的账行名。

        陈旧性是 registry-relative 概念，只有服务端持有根 registry（树级语义），故
        判据在 `SessionService.purge_stale_session_tool_limits`：它只删 ceiling 表里的
        陈旧键，**不动**消耗事实表 `tool_calls_by_tool` / `tool_attempts_by_tool`，
        也**不落** typed SessionEvent（预算变更本就不进会话真相，不变量 #16/#22）；清除
        痕迹只落一条 `session_budget_events(kind="tool_limits_purged")` 审计并 bump version。

        可选 query `?tool=<name>` 收窄到单名（对应 CLI `--tool`）：服务端仍按根 registry
        权威判 stale，点名**正常注册名 / 从未存在名**都是安全 no-op，只有点名**确实陈旧**
        的名字才真清。

        语义：200 → `{purged, remaining, rows, version}` 回执（**幂等**：无陈旧名仍 200，
        `purged={}`、`rows=0`、`version` 不变）；404 → 没有这个会话；422 → id 形态非法
        （先于 404，路径穿越防线）。

        确认面**不在 API**：本端点的 POST 本体即显式动作；整会话重整的二次确认只在
        CLI（`--yes`）。来源闸（ADR-0025 D1）：账行写操作属宿主侧管理动作，只接受本机
        来源（与 `archive` / `delete_session` 同一实现，ADR-0028）。
        """
        service = session_service(app.state.agent)
        try:
            result = await service.purge_stale_session_tool_limits(
                session_id, tool=tool, entry_point=PURGE_ENTRY_API
            )
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        except StorageBusyError as e:
            raise storage_http_error(e) from e
        return SessionToolLimitsPurged(
            purged=result.purged,
            remaining=result.remaining,
            rows=result.rows,
            version=result.version,
        )

    @app.post("/api/sessions/{session_id}/context/compact")
    async def compact_session_context(
        session_id: str,
        model: str | None = None,
        _: None = Depends(require_trusted_origin),
    ) -> SessionContextCompacted:
        """手动触发一次上下文压缩（#635）。

        走**唯一实现** `SessionService.compact_session_context`（不变量 #22）：与自动
        路径同一 `ContextCompactor` 管线、同一 bracket 三事件、同一重投影确认。压缩
        append-only（旧事件 shadow 保留、可 replay），故本端点的 POST 本体即用户的
        显式动作，**不设二次确认**。

        可选 query `?model=<name>` 透传摘要模型（对标 CLI `--model`）：优先级为显式
        参数 > `settings.summary_model` > 主模型；非法模型名在**任何副作用之前**经
        统一解析点抛错 → 422（detail 原样上抛）。

        状态码（顺序即服务层的校验顺序）：422 → id 形态非法（先于 404，路径穿越防线）；
        404 → 没有这个会话；422 → 非法 `?model=`；409 → 在途 run（`ActiveRunConflict`，
        零写入）/ 该会话已有压缩在途（`CompactionInProgress`，零写入）/ 落盘窗口并发
        （`CompactionConcurrentWrite`：`run_busy`/`event_drift`，历史可能已变，非零写入）；
        500 → bracket 已写入但复核未通过
        （`CompactionPostWriteError`，fail-closed；历史已多出 bracket，不谎报"未改动"）。

        **非严格幂等**：每次调用都是用户显式请求的一次新压缩，追加新 bracket；重复调用
        安全但**非 no-op**（与 `purge-stale-tools` 的幂等不同——那是一次清理，这是一次
        动作）。低水位时返回 `bracket_id=null`、`compacted_turn_count=0`，仍 200。

        来源闸（ADR-0025 D1）：压缩会写会话事件日志，属宿主侧管理动作，只接受本机来源
        （与 `archive` / `delete_session` 同一实现，ADR-0028）。
        """
        service = session_service(app.state.agent)
        try:
            result = await service.compact_session_context(
                session_id, model=model, entry_point=COMPACT_ENTRY_API
            )
        except (InvalidSessionId, SessionNotFound, ActiveRunConflict) as e:
            raise http_error(e) from e
        except ConfigError as e:
            # 非法摘要模型名（统一解析点抛出）：请求形状非法 → 422，detail 原样上抛。
            raise HTTPException(status_code=422, detail=str(e)) from e
        except StorageBusyError as e:
            raise storage_http_error(e) from e
        return SessionContextCompacted(
            bracket_id=result.bracket_id,
            source_seq_start=result.source_seq_start,
            source_seq_end=result.source_seq_end,
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
            compacted_turn_count=result.compacted_turn_count,
            summary_model=result.summary_model,
        )

    @app.post("/api/sessions/{session_id}/approvals/revoke")
    async def revoke_approval_grant(
        session_id: str,
        body: RevokeApprovalGrantRequest,
        _: None = Depends(require_trusted_origin),
    ) -> ApprovalGrantRevoked:
        """撤回一条会话级审批授权（#526 A2）。

        追加 `permission/approval-revoked`（last-wins）。语义：200 →
        `{id, approval_key, revoked}`（**幂等**：重复撤回仍 200，`revoked=False`
        表示该 key 当时本就不存在）；404 → 没有这个会话；422 → id 形态非法
        （先于 404，路径穿越防线）。

        来源闸（ADR-0025 D1）：授权撤回是宿主侧管理动作，只接受本机来源
        （与 `archive` / `purge-stale-tools` 同一实现）。
        """
        service = session_service(app.state.agent)
        try:
            result = await service.revoke_approval_grant(
                session_id, body.approval_key
            )
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        except StorageBusyError as e:
            raise storage_http_error(e) from e
        return ApprovalGrantRevoked(
            id=result["id"],
            approval_key=result["approval_key"],
            revoked=result["revoked"],
        )

    @app.get("/api/approve-policy/rules")
    async def list_approve_policy_rules(
        _: None = Depends(require_trusted_origin),
    ) -> ApprovePolicyRules:
        """列出本项目持久审批规则（#684 Phase 2，管理面）。

        数据源是项目根 `.agent-harness/approve-policy.json`（`ApprovePolicyStore`），
        与执行域命中规则时读的是**同一份文件**。无规则 / 文件缺失 / 文件损坏都返回
        `{"rules": []}`（fail-closed：读不动就当没有规则，绝不臆造放行）——空列表
        不是 404，因为"没有配置"不是错误。

        只读端点，无 422/404：路径无参数。来源闸（ADR-0025 D1）：规则含工具名 /
        命令键 / 相对路径等宿主侧信息，只接受本机来源（与 `project-instructions` /
        `host/dirs` 的 GET 同一实现）。
        """
        rules = approve_policy_store().load()
        return ApprovePolicyRules(
            rules=[ApprovePolicyRuleOut(**rule.to_dict()) for rule in rules]
        )

    @app.post("/api/approve-policy/rules/{rule_id}/revoke")
    async def revoke_approve_policy_rule(
        rule_id: str,
        _: None = Depends(require_trusted_origin),
    ) -> ApprovePolicyRuleRevoked:
        """撤销一条项目级持久审批规则（#684 Phase 2，管理面）。

        语义：200 → `{id, revoked}`（**幂等**：重复撤销仍 200，`revoked=False` 表示
        该 id 当时本就不存在）；422 → `rule_id` 形态非法（先于任何读写，路径穿越防线）。
        **没有 404**：资源是项目级配置文件而非按 id 寻址的实体，规则缺席是幂等成功。
        二次确认由前端做（与 #526 的 revoke 端点同一口径：POST 本体即显式动作）。

        撤销只删规则、**不删审计**：规则是配置，git 历史即审计链（设计稿 §6）。
        来源闸（ADR-0025 D1）：撤销是宿主侧管理动作，只接受本机来源。
        """
        if not SESSION_KEY_PATTERN.fullmatch(rule_id):
            raise HTTPException(
                status_code=422,
                detail=f"rule_id 只接受单个安全名字段：{rule_id!r}",
            )
        removed = approve_policy_store().remove_rule(rule_id)
        return ApprovePolicyRuleRevoked(id=rule_id, revoked=removed)

    @app.post("/api/sessions/{session_id}/workflow-mode")
    async def set_workflow_mode(
        session_id: str,
        body: SetWorkflowModeRequest,
        _: None = Depends(require_trusted_origin),
    ) -> WorkflowModeChanged:
        """切换会话工作流档（#526 B1：normal/plan）。

        追加 `workflow/mode-changed`（last-wins）；Executor 每次调用现派生
        `effective_workflow_mode`，切换立即生效。语义：200 → `{id, mode}`
        （**幂等**）；404 → 没有这个会话；422 → id 形态非法 / mode 非法
        （先于 404）。

        来源闸（ADR-0025 D1）：档位切换是宿主侧管理动作，只接受本机来源。
        """
        from agent_harness.session.workflow import WorkflowMode

        service = session_service(app.state.agent)
        try:
            result = await service.set_workflow_mode(
                session_id, WorkflowMode(body.mode)
            )
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e
        except StorageBusyError as e:
            raise storage_http_error(e) from e
        return WorkflowModeChanged(id=result["id"], mode=result["mode"])

    @app.post("/api/sessions/{session_id}/client-exit")
    async def signal_client_exit(
        session_id: str,
        request: Request,
        _: None = Depends(require_trusted_origin),
    ) -> ClientExitSignaled:
        """客户端明确退出信号（#360 W-17 / W-12 #356 选项 B 写侧）。

        TUI/桌面客户端退出时调此端点声明"我走了"：服务端走
        `RunManager.signal_client_exit` 权威硬信号——接受即承诺只走
        `run/paused(reason=client_absent)`，永不在途杀 Tool（decision 5），
        幂等（decision 7），fail-closed（decision 4：严格写失败抛 500，
        run 维持原状）。

        语义：200 → `{id, status, run_id, detail}`（幂等）；
        404 → 没有这个会话；422 → id 形态非法（先于 404）；
        500 → 严格 W-05 写失败（run 未被暂停，维持原状继续跑）。

        来源闸（ADR-0025 D1）：在场信号是宿主侧管理动作，只接受本机来源。
        """
        from agent_harness.session.client_exit import ClientExitError
        from agent_harness.session.service import InvalidSessionId

        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        # client_id 仅用于诊断/日志归因（W-12 decision：不构成在场登记）；
        # body 缺失/非 JSON 时回落 "unknown"，不因此 422。
        client_id = "unknown"
        try:
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("client_id"), str):
                client_id = body["client_id"][:64]
        except Exception:  # noqa: BLE001, S110 — body 解析失败不是请求失败
            pass
        run_manager = app.state.agent.run_manager
        try:
            outcome = await run_manager.signal_client_exit(
                session_id, client_id=client_id,
            )
        except ClientExitError as e:
            raise HTTPException(status_code=500, detail=e.detail) from e
        return ClientExitSignaled(
            id=outcome.session_id,
            status=outcome.status,
            run_id=outcome.run_id,
            detail=outcome.detail,
        )

    @app.get("/api/sessions/{session_id}/context-usage")
    async def get_context_usage(session_id: str):
        """上下文容量看板数据面（#200）：只读端点，六桶分类 + 缓存命中率。

        分类明细**不进** SessionEvent（不变量 #4：Event ≠ Diagnostic Log），
        只经本端点暴露。三份硬约束（设计稿 §3 诚实原则）：``estimated`` 恒为
        true；cache 三态（not_collected 时**不显示 0%**）；六桶之和 = used_tokens
        （差额进"其他"残差，**仅 state="ok" 时成立**）。

        数据来源：在途 run 的 builder 快照（``ContextBuilder.usage_snapshot``，
        实时读——最近一次 build 是当前事实）+ 会话事件流 usage 汇总 + 该 run 的
        ToolRegistry 工具 schema 估算；run 已终结 ⇒ 从收口时缓存的快照读（
        `_cache_context_snapshot` 在 run 收尾时取，registry 与快照同一真相）；
        快照缺席但事件流有 usage ⇒ state="usage_only"（#212：报窗口占用下界 +
        usage_source，分类如实为 0）；两者都没有 ⇒ state="no_data"（不伪造）。

        注意快照是**进程内**缓存：后端重启后历史会话必然走到 usage_only/no_data
        ——这不是异常分支，是常态分支（#212 实测踩到）。
        """
        service = session_service(app.state.agent)
        try:
            events = await service.get_events(session_id)
        except (InvalidSessionId, SessionNotFound) as e:
            raise http_error(e) from e

        # builder 快照 + 工具定义：优先在途 run；run 已终结 ⇒ 收口缓存；都没有
        # ⇒ 交给 build_context_usage_payload 按事件流落 usage_only/no_data
        # （不伪造，也不重建一个假 registry 来算工具桶）。两者必须同源取
        # （同一个 runtime 的 builder + registry），分开取会得到两个真相。
        builder_snapshot = None
        tool_definitions: list[dict[str, Any]] = []
        active = app.state.agent.run_manager.get_active(session_id)
        if active is not None and active.runtime is not None:
            builder = active.runtime._context_builder
            if builder is not None:
                builder_snapshot = builder.usage_snapshot(active.session)
            tool_definitions = active.runtime.registry.export_model_definitions()
        else:
            cached = app.state.agent.context_snapshots.get(session_id)
            if cached is not None:
                builder_snapshot, tool_definitions = cached

        payload = build_context_usage_payload(
            settings=app.state.agent.settings,
            builder_snapshot=builder_snapshot,
            tool_definitions=tool_definitions,
            estimate_tokens=estimate_tokens,
            events=events,
        )
        return payload

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(
        session_id: str, _: None = Depends(require_trusted_origin)
    ) -> SessionDeleted:
        """硬删会话（#172 / ADR-0029）：不可撤销，无墓碑。

        用户显式要求的删除——与「系统不得静默丢弃历史」（spec 03 §Full SessionEvent
        History）不冲突：那条约束管的是**系统**不许偷删，不是用户不许删自己的会话。

        语义：200 → 事件日志 + 辅助行 + harness 自造的沙箱工件都清了，回执带事件数与
        解除的项目数；404 → 没有这个会话（第二次删除即此，不伪装成"又删了一次"）；
        409 → 有在途 run，或有 fork 子会话（detail 带子会话数量）；422 → id 形态非法。
        项目归属只解账本，**项目本身与目录一个字不动**（与软删项目的口径一致）。

        来源闸（ADR-0025 D1）：删除是宿主侧不可逆操作，只接受本机来源。
        """
        service = session_service(app.state.agent)
        try:
            stats = await service.delete_session(session_id)
        except (
            InvalidSessionId,
            SessionNotFound,
            ActiveRunConflict,
            SessionHasChildren,
        ) as e:
            raise http_error(e) from e
        except StorageBusyError as e:
            # #515：硬删要写多张共享表（ledger/checkpoint/meta/工件），锁竞争重试
            # 耗尽时报 503——删除未开始，客户端稍后重试即可。
            raise storage_http_error(e) from e
        project_instruction_store(app.state.agent.settings).forget_session(session_id)
        return SessionDeleted(
            id=stats.session_id,
            events=stats.events,
            detached_from_projects=stats.detached_from_projects,
        )

    @app.get("/api/sessions/{session_id}/artifacts/{artifact_id}")
    async def read_artifact_content(
        session_id: str,
        artifact_id: str,
        start_line: int | None = None,
        end_line: int | None = None,
        keyword: str | None = None,
        max_lines: int = artifacts.MAX_LINES_CAP,
        max_chars_per_line: int = artifacts.MAX_CHARS_PER_LINE_CAP,
    ) -> dict[str, Any]:
        """读取外置 artifact 的局部内容（#185）。

        外置产物是**被截断的大工具输出 / 大 diff**：工具结果超过
        `artifact_overflow_chars` 时原文落到对象存储，会话里只留"摘要 + ref"（不变量
        #15：大内容外置，模型只拿 summary + ref）。本端点把模型侧同一个读入口
        （`ArtifactStore.inspect`）暴露给 Web，让界面能真的看到那段内容。

        状态码语义：

        - 422 → `session_id` 或 `artifact_id` 形态非法（客户端 bug，不是冲突）；
        - 404 → 会话不存在，或该 artifact 不在这个会话的命名空间里。**别的会话的
          产物也走这条**：`artifact_id` 是内容哈希、跨会话可重复，区分"不存在"与
          "存在但不可读"只会把归属变成可探测的信息；
        - 503 → 本部署**确实没有可读取的存储**（`artifact_dir` 置空、或对象存储半配置）
          ——**如实上报**，不假装成 404：那会让用户以为"这个产物不存在"；
        - 200 → 切片，`truncated` 如实表示返回内容是否完整。

        ⚠ #192 之后 404 的含义变宽了：未配对象存储的部署现在走**本地**默认 Provider
        （spec 06 §3），它**能读**，只是里面没有这个 id ⇒ 404。503 只留给"真的没有可读
        存储"这一种情形。

        隔离靠"**用 URL 里的 session_id 构造 store**"：provider 的 key 前缀是
        `{session_id}/{artifact_id}`，而 artifact_id 不携带归属，归属只能由
        session_id 决定。

        体积上限由**服务端**兜底：客户端给再大也会被夹进上限。`max_lines` 夹取后的
        **实际生效值**在 `query.max_lines` 里回显；`max_chars_per_line` 同样按服务端
        上限执行（`ArtifactSlice.query` 不携带该字段，故不回显）。行号从 1 开始，
        `start_line` / `end_line` < 1 一律 422——与模型侧 `inspect_artifact` 的
        schema（`ge=1`）保持同一口径。
        """
        state = app.state.agent
        try:
            validate_session_id(session_id)
        except InvalidSessionId as e:
            raise http_error(e) from e
        if not artifacts.ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
            raise HTTPException(
                status_code=422,
                detail=(
                    "artifact_id 必须是 16 位小写十六进制（内容哈希前 16 位）："
                    f"{artifact_id!r}"
                ),
            )
        for name, value in (("start_line", start_line), ("end_line", end_line)):
            if value is not None and value < 1:
                raise HTTPException(
                    status_code=422,
                    detail=f"{name} 必须 >= 1（行号从 1 开始计数）：{value}",
                )
        if not await session_service(state).has_session(session_id):
            raise http_error(SessionNotFound(f"session '{session_id}' not found"))
        store = artifacts.build_read_artifact_store(state.settings, session_id)
        if store is None:
            raise HTTPException(
                status_code=503,
                # #227：带机读码（前端只认码，不按 503 猜原因）。文案与码的分工见
                # docs/adr/0035-*.md：`message` 给人看，`code` 给程序判。
                detail={
                    "code": artifacts.ARTIFACT_STORAGE_UNAVAILABLE,
                    "message": (
                        "本部署没有可读取的 artifact 存储：artifact_dir 为空，"
                        "或对象存储只配了一半"
                    ),
                },
            )
        try:
            slice_ = await store.inspect(
                artifact_id,
                start_line=start_line,
                end_line=end_line,
                keyword=keyword,
                max_lines=artifacts.clamp_to_cap(max_lines, artifacts.MAX_LINES_CAP),
                max_chars_per_line=artifacts.clamp_to_cap(
                    max_chars_per_line, artifacts.MAX_CHARS_PER_LINE_CAP
                ),
            )
        except KeyError as e:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"artifact {artifact_id!r} 不在会话 {session_id!r} 的命名空间里"
                    "（不存在，或属于别的会话）"
                ),
            ) from e
        return slice_.model_dump()

    @app.post("/api/sessions/{session_id}/approve")
    async def approve_tool_call(session_id: str, req: ApproveRequest) -> dict[str, str]:
        """交互式审批决策入口（Phase 5 切片 C + Batch 5.1）：前端拿到
        tool/approval-requested 事件后，调本端点注入批准/拒绝决策，唤醒 run
        内阻塞的 callback。

        语义：
          成功 resolve → resolved 事件先 durable append，再返回 200 并唤醒 run
          decision 不在 requested 事件的 allowed_decisions 内 → 422（违反契约）
          approval_id 已 resolved 或请求已失效 → 409（防重复/过期决策）
          approval_id 不存在于 durable 请求事件 → 404（非本 session 的 id）
          无 approval_id（旧 seam 调用）→ 200 received（向后兼容）
          session 不存在 → 404
        """
        if req.approval_id is None:
            # 旧 seam 入口（Phase 2 阶段性占位）：不解析任何决策，仅回 200 信号；
            # 不要求 session 存在——Phase 2 seam 测试用任意 id 探活。
            return {
                "status": "received",
                "note": "auto-approve is default; interactive approval via approval_id",
            }
        service = session_service(app.state.agent)
        try:
            result = await service.resolve_approval(
                session_id=session_id,
                approval_id=req.approval_id,
                approved=req.approved,
                decision=req.decision,
                reason=req.reason,
                policy_granularity=req.policy_granularity,
            )
        except (
            InvalidSessionId,
            SessionNotFound,
            ApprovalQueueMissing,
            ApprovalRequestMissing,
            InvalidDecision,
            ApprovalAlreadyResolved,
        ) as e:
            # 三个 404（SessionNotFound / ApprovalQueueMissing /
            # ApprovalRequestMissing）**有意不可区分**；ApprovalAlreadyResolved
            # 是 409 幂等已决（OBS-015）。状态码见 web/domain_errors.py。
            raise http_error(e) from e
        return {
            "status": "resolved",
            "approval_id": req.approval_id,
            "decision": result.decision.value,
        }

    @app.post("/api/sessions/{session_id}/recover")
    async def recover_session(
        session_id: str, req: RecoverRequest | None = None
    ) -> list[dict]:
        """恢复崩溃 session（R8-1 接线）：RecoveryCoordinator 唯一入口（07 §9）。

        修复 dangling tool_call（配对合成）、按 Ledger 终态精确回填结果、
        PENDING 默认 skip。#547 裁决合同：RUNNING/UNKNOWN 需要人工裁决时，
        无裁决 → 409（detail 附机器可读 ``pending_decisions`` 清单）；携带覆盖
        全部待裁决行的 ``decisions`` → 用户显式裁决结清后继续（四值词表，
        不伪造、不盲跑，不变量 #14）。幂等：重复调用靠事件配对自然跳过已修复项。
        """
        service = session_service(app.state.agent)
        decisions = [
            ReconcileDecision(
                tool_call_id=d.tool_call_id, verdict=d.verdict, source=d.source,
            )
            for d in (req.decisions if req is not None else [])
        ]
        try:
            events = await service.recover(session_id, decisions=decisions)
        except (
            InvalidSessionId,
            SessionNotFound,
            InvalidDecision,
            RecoveryConflict,
            EventLogCorruptError,
            SeqConflict,
        ) as e:
            # RecoveryConflict → 409：RUNNING/UNKNOWN 需人工裁决，不伪造不盲跑
            # （不变量 #14）；#547 起该分支 detail 附 pending_decisions 清单。
            # InvalidDecision → 422：裁决值/目标/重复提交非法（#547 预检）。
            # SeqConflict → 409：日志 seq 冲突（BUG-011）。
            # EventLogCorruptError → 409（#565）：完整性闸门（完整坏行 / seq
            # 断层 / seq 重复），不可重试，需按定位记录人工修复。
            raise http_error(e) from e
        return [e.to_dict() for e in events]

    @app.get("/api/recovery/interrupted")
    async def list_interrupted_recoveries() -> dict[str, Any]:
        """恢复列表（#357 W-13 契约 5，**只读**）：启动扫描快照的四要素呈现。

        lifespan 启动时跑过一次 ``scan_interrupted`` 并把结论快照存进
        ``app.state``；本端点只读快照 + 只读富化（Task / 无终态 run / 工作目录
        锚 / 进度文件版本），**绝不重跑扫描**——``scan_interrupted`` 会补写
        ``run/interrupted`` 并跑 reconcile，有写副作用（07 §9 的恢复编排只属于
        显式 recover / resume 入口）。lifespan 未跑 → ``snapshot_available=false``
        + 空列表（不伪造扫描结论）。#22：列表由后端单点驱动，前端不维护第二套
        真相。
        """
        state: AppState = app.state.agent
        snapshot = state.interrupted_scan
        if snapshot is None:
            return {"snapshot_available": False, "items": []}
        items = await session_service(state).interrupted_recovery_rows(snapshot)
        return {"snapshot_available": True, "items": items}

    # ── 模型切换（T7 #137，PRD §2.3）───────────────────────────────────
    # fork 创建端点在 web/lineage.py（ADR-0017 决策 6 的独立 router 面）。

    @app.post("/api/sessions/{session_id}/model")
    async def change_session_model(
        session_id: str, req: ModelChangeRequest
    ) -> dict[str, str]:
        """切换会话当前模型并写 ``model/changed``（PRD §2.3）。

        下一轮 run 从事件流派生当前模型生效（不打断在途 run）。
        404 = session 不存在；422 = provider/model_id 不在 catalog
        （``GET /api/models`` 的默认条目也是合法目标 = 切回默认链）。
        """
        service = session_service(app.state.agent)
        try:
            change = await service.change_model(
                session_id=session_id,
                provider=req.provider,
                model_id=req.model_id,
            )
        except (InvalidSessionId, SessionNotFound, UnknownModel, SeqConflict) as e:
            raise http_error(e) from e
        # 回传规范 model_id（service 解析出的 picker id）：catalog 条目名，或默认链
        # 的默认模型名——不能回显请求值，否则上游 model_name / "default" 别名会与
        # 事件里的 to_model_id 及 GET /api/models 的 id 对不上。
        return {
            "status": "changed",
            "provider": change.to_provider,
            "model_id": change.effective_model_id,
        }

    @app.post("/api/sessions/{session_id}/permission")
    async def change_session_permission(
        session_id: str, req: PermissionChangeRequest
    ) -> dict[str, str | bool]:
        """会话内改权限档 + ``auto_approve`` 并写 ``permission/changed``（F18-A #282）。

        对齐 ``POST /api/sessions/{id}/model``：只写事件、不打断在途 run（下一轮生效）。
        422 = 非法档位；404 = session 不存在；**409 = 有未裁决审批或 seq 冲突**。
        """
        service = session_service(app.state.agent)
        try:
            change = await service.change_permission_mode(
                session_id=session_id,
                permission_mode=req.permission_mode,
                auto_approve=req.auto_approve,
            )
        except (
            InvalidSessionId,
            SessionNotFound,
            InvalidDecision,
            PendingApprovalConflict,
            SeqConflict,
        ) as e:
            raise http_error(e) from e
        except StorageBusyError as e:
            # #515：改档要持久化权限设置（session_meta 写），锁竞争重试耗尽报 503。
            raise storage_http_error(e) from e
        # 回传**改后**的当下生效值（service 解析出的档位），前端按回执对齐即可，不必
        # 本地推导（不引入乐观状态；见 F18-B）。
        return {
            "status": "changed",
            "permission_mode": change.permission_mode.value,
            "auto_approve": change.auto_approve,
        }

    # ── 续聊入口（PRD §5.3）───────────────────────────────────────────
    # 双模式：queue（默认）= 入队/直接拉起；steer = 注入在途 run。
    # launched 分支返回 SSE 流（PRD 锁定 D-10：续聊端点响应与创建端点一致）；
    # queued / steered 分支返回 JSON 确认（不打开流，前端订阅既有 SSE/WS）。
    @app.post("/api/sessions/{session_id}/messages")
    async def send_message(session_id: str, req: SendMessageRequest):
        """续聊消息入口（Phase Multiturn T2 / PRD §5.3）。

        ``mode=queue``：空闲 → 直接 resume_and_launch 拉起新 run，返回
        SSE 流（同创建端点语义）；在途 → 入队并返回 JSON 确认。
        ``mode=steer``：仅在途 run 时合法——注册 SteerRequest 并返回
        JSON 确认；无在途 run → 409（steer 必须有目标）。
        """
        service = session_service(app.state.agent)
        amend = AmendOptions.from_request(req)
        # 契约（handoff §3.1 / P3）：只有 idle → launched 才**消费** amend；在途 run
        # 的 queued 消息与 steer 一律忽略这些字段。因此引用类字段（model /
        # context_providers，取值集合来自运行时 catalog / wiring，可能已失效）
        # 只在这条路径上校验——否则一个失效引用会 422 掉用户刚敲的消息。
        # 值/形状类字段（reasoning_effort / agent_profile / context_providers 形状）
        # 由 Pydantic 在 parse 期校验，与是否消费无关（静态集合，非法值即客户端 bug）。
        if (
            req.mode == "queue"
            and app.state.agent.run_manager.get_active(session_id) is None
        ):
            await _validate_amend_for_existing_session(app.state.agent, amend)
        else:
            # 未被消费。但**只丢弃消费性字段**，`agent_profile` 要留给预算判定：它同时是
            # 生效 local fuse 的一个输入（档位 ceiling），丢掉它会让同一份 body 的状态码
            # 取决于"此刻有没有 run"——#308 的判定与运行态无关要求这条轴也可判。
            # 不消费 = 不启动 run ⇒ 没有任何别处会读它（`SessionService.send_message`
            # 只在判定与 idle 分支用 amend）。
            amend = AmendOptions(agent_profile=req.agent_profile)
        try:
            result = await service.send_message(
                session_id=session_id,
                content=req.content,
                mode=req.mode,
                **budget_claims(req.budget),
                amend=amend,
                supersedes_seq=req.supersedes_seq,
                queue_id=req.queue_id,
                revoke_fact_id=req.revoke_fact_id,
                refutes_event_id=req.refutes_event_id,
                remember_as_procedural_rule=req.remember_as_procedural_rule,
                protected_facts=[
                    fact.model_dump(exclude_none=True)
                    for fact in req.protected_facts
                ],
                attachments=req.attachments or None,
            )
        except (
            InvalidSessionId,
            SessionNotFound,
            ActiveRunConflict,
            RecoveryConflict,
            EventLogCorruptError,
            QueueItemNotFound,
            SteerTargetNotFound,
            ProtectedFactReferenceInvalid,
            AttachmentReferenceInvalid,
            TooManyAttachments,
            AttachmentMessageTooLarge,
            ModelDoesNotSupportImages,
            SupersedeTargetInvalid,
            SeqConflict,
            WorkspaceBindingConflict,
            WorkspaceNotFound,
            BudgetRejection,
            BudgetConflict,
        ) as e:
            # RecoveryConflict → 409（T8 #138）：崩溃遗留（UNKNOWN 高风险
            # tool_call）需人工裁决——拒绝续跑而不是伪造「结果未知」（不变量 #14）。
            # EventLogCorruptError → 409（#565）：idle→launched 会按需走
            # self.recover，完整性闸门拒绝损坏日志（不可重试）。
            # SupersedeTargetInvalid → 409（ADR-0030 §4.6）：目标不对，不是会话不存在。
            # WorkspaceBindingConflict → 409（#266）：idle 分支会走 resume_and_launch，
            # 工作目录归属冲突同样拒绝静默选边。
            # WorkspaceNotFound → 404（#615①）：同样经 resume_and_launch——外部 cwd
            # 在两次请求之间被删时中央映射既有条目本就要接住，/resume 元组同款
            # （#564 审查 P2-1），不新增状态码语义。
            raise http_error(e) from e
        except ModelClientConstructionError as e:
            # #517 BUG-05：idle→launched 分支会构造 client——同 create/resume，503。
            raise model_http_error(e) from e
        except UnsupportedReasoningEffort as e:
            raise model_http_error(e) from e
        except StorageBusyError as e:
            # #515（审查 P2-3）：同 create——launched 分支的写锁耗尽 → 503。
            raise storage_http_error(e) from e
        except (OSError, sqlite3.Error) as e:
            # #569：只把明确的容量 / SQLite I/O 错误翻译为 storage 503；坏路径、
            # 约束冲突和没有原生 SQLite 错误码的 OperationalError 继续走 500。
            if storage_http_status(e) is None:
                raise
            raise storage_http_error(e) from e

        if result.status == "launched":
            # 与创建端点同形：SSE 直驱 run（ADR-0016 detached-run）。
            return _launched_response(
                app, result, session_id,
                headers=_local_fuse_headers(result.local_fuse),
            )
        # queued / steered：JSON 确认（不打开流——前端订阅既有 SSE/WS）。
        # 这两条分支**没有**生效 local fuse 可投影（本请求没产生 run，`11 §6.1` 只把
        # budget 挂在 idle 消息启动上）——所以刻意不给响应头：一个 None 投影成 0 或
        # 默认值都是假事实（不变量 #21 同族）。
        return result.to_response()

    def _launched_response(
        app: FastAPI, result, session_id: str,
        *, headers: dict[str, str] | None = None,
    ) -> EventSourceResponse:
        """launched 分支的统一 SSE 响应（/messages 与 /queue/flush 共用）。

        与创建端点同一段实现（`_run_stream_response`）：ADR-0030 §4.6 要求 flush
        与 messages 的 launched 语义完全一致（打开同样的 detached-run 流）。
        """
        return _run_stream_response(
            app.state.agent, result.run, result.subscriber, session_id,
            headers=headers,
        )

    @app.get("/api/sessions/{session_id}/queue")
    async def get_session_queue(session_id: str) -> dict[str, list[dict[str, str]]]:
        """待发送输入（ADR-0030 §4.6 / D11）。

        数据源是**事件流**而非内存队列：只有事件流跨崩溃存活、也只有它同时
        看得见 queue 与 steer 的到达顺序（§4.8 / §5.2 的"事件流是唯一事实"）。
        前端用它做首屏 / 重连补齐，实时增量仍由 SSE 事件流驱动。
        """
        service = session_service(app.state.agent)
        try:
            pending = await service.list_undelivered_inputs(session_id)
        except (InvalidSessionId, SessionNotFound, SeqConflict) as e:
            raise http_error(e) from e
        return {
            "items": [
                {
                    "queue_id": item.input_id,
                    "content": item.content,
                    "created_at": item.created_at,
                }
                for item in pending
                if item.kind == "queue"
            ],
            "steers": [
                {
                    "steer_id": item.input_id,
                    "content": item.content,
                    "created_at": item.created_at,
                }
                for item in pending
                if item.kind != "queue"
            ],
        }

    @app.post("/api/sessions/{session_id}/queue/flush")
    async def flush_session_queue(session_id: str):
        """立刻投递队首的待发送输入（ADR-0030 §4.6）。

        空队列 → ``{"status": "idle"}``（幂等，不报错）；有 → 与 `/messages` 的
        launched 分支**同一段代码**返回 SSE 流。用途：① 前端在会话恢复时主动投递；
        ② 重启后手动投递（§4.8 说不自动起 run，就靠这个入口）。

        只投递**一条**：后续输入在下一个 run 终态继续接力（§4.5.5）。
        """
        service = session_service(app.state.agent)
        try:
            launched = await service.deliver_next_undelivered(session_id=session_id)
        except (
            InvalidSessionId,
            SessionNotFound,
            ActiveRunConflict,
            RecoveryConflict,
            EventLogCorruptError,
            SeqConflict,
            WorkspaceBindingConflict,
            WorkspaceNotFound,
            BudgetRejection,
            BudgetConflict,
        ) as e:
            # WorkspaceNotFound → 404（#615①）：投递走 resume_and_launch，外部 cwd
            # 在排队之后被删 → 中央映射既有条目，与 /messages、/resume 同口径。
            raise http_error(e) from e
        except ModelClientConstructionError as e:
            # #517 BUG-05（审查 P2-1）：flush 在 idle 时走 resume_and_launch 构造
            # client——BUG-05 的第 4 个构造调用点，同 create/resume/messages → 503。
            raise model_http_error(e) from e
        except UnsupportedReasoningEffort as e:
            raise model_http_error(e) from e
        except StorageBusyError as e:
            # #515（审查 P2-3）：投递路径的写锁耗尽 → 503。
            raise storage_http_error(e) from e
        if launched is None:
            return {"status": "idle"}
        # 与 `/messages` 的 launched 分支**同一投影**（同一段响应组装）：重投的 run 也消费
        # 了生效 fuse，客户端在两条入口上读到的 ceiling 必须是同一个事实。
        return _launched_response(
            app, launched, session_id,
            headers=_local_fuse_headers(launched.local_fuse),
        )

    @app.post("/api/sessions/{session_id}/queue/{queue_id}/cancel")
    async def cancel_queue_item(session_id: str, queue_id: str) -> dict[str, str]:
        """取消尚未消费的排队消息（PRD §5.3 / D-7）。

        语义：取消成功 → 200 cancelled；queue_id 已取消 / 已消费 /
        不存在 → 404；session 不存在 → 404。幂等失败（防覆盖式重置语义）。
        """
        service = session_service(app.state.agent)
        try:
            cancelled = await service.cancel_queue(
                session_id=session_id, queue_id=queue_id
            )
        except (InvalidSessionId, SessionNotFound, QueueItemNotFound, SeqConflict) as e:
            raise http_error(e) from e
        return {"status": "cancelled" if cancelled else "already_consumed"}

    # ── WebSocket 多路复用通道（T2 / PRD §5.1）──────────────────────
    # 主 streaming 通道：单连接订阅多个 session、推增量事件；
    # 心跳 ping/pong；断线重连先推快照再增量。SSE 保留为灰度兼容路径。
    # 不维护第二套 session 真相（不变量 #22）——所有事件源于 RunManager 订阅。
    from agent_harness.web.websocket import handle_websocket

    @app.websocket("/api/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """WS 主入口（PRD §5.1）：接受连接后交由 handle_websocket 多路复用。

        WS 只做传输——业务决策一律走 SessionService。WebSocketDisconnect
        是正常客户端断开，吞掉不打日志。
        """
        try:
            await handle_websocket(websocket, app.state.agent)
        except WebSocketDisconnect:
            # 正常断开：客户端关页 / 重连切换。
            return

    return app


def _repo_web_dist() -> Path:
    """仓库内的前端产物：``<repo>/web/dist``（开发与生产部署的既有默认）。"""
    return Path(__file__).resolve().parent.parent.parent.parent / "web" / "dist"


def _bundled_web_dist() -> Path:
    """打包形态的前端产物：与随包运行时同级的 ``<resources>/web``。

    W-21 D5（#817）：桌面外壳把运行时放在 ``<resources>/python/python.exe``、
    渲染层放在 ``<resources>/web``。这里只用**运行时自身位置**推导，不查注册表
    也不猜安装根，因此任何由该运行时启动的服务（外壳、TUI、手工命令行）都能直接
    服务桌面窗口；dev 形态下该目录不存在，自然回落到 ``<repo>/web/dist``。
    """
    return Path(sys.executable).resolve().parent.parent / "web"


def _default_web_dist_candidates() -> list[Path]:
    """未显式指定产物目录时的候选顺序：随包产物优先，其次仓库产物。"""
    return [_bundled_web_dist(), _repo_web_dist()]


def mount_static(app: FastAPI, web_dist_dir: str | None = None) -> None:
    """挂载前端构建产物为静态资源。

    ``web_dist_dir`` 非空时只用该目录（调用方说了算：Electron 外壳经
    ``Settings.web_dist_dir`` / env ``WEB_DIST_DIR`` 传安装目录里的产物，W-21 D3）；
    缺省/空串时按 ``_default_web_dist_candidates()`` 取第一个存在的目录——随包
    运行时旁的 ``<resources>/web``，否则 ``<repo>/web/dist``（W-21 D5，让任何由
    该运行时启动的服务都能服务桌面窗口）。

    独立于 ``create_app`` —— 测试在 ``create_app`` 返回后追加的自定义路由
    （如 ``/identity-probe``）不会被 StaticFiles Mount 遮蔽。生产部署由
    uvicorn ``--factory`` 调 ``create_prod_app``（= ``create_app`` +
    ``mount_static``)，或由反向代理直接服务静态资源、仅将 ``/api``
    转发到本服务。

    部署约束：静态挂载只适配本地信任模式（未配置 JWT_SECRET）。fail-closed
    生效时全量默认拒绝（test_auth_fail_closed 契约），而浏览器顶层导航无法
    携带 Bearer——index.html 都会 401。生产 + JWT 的支持形态是反向代理：
    静态资源在代理层直出，仅 /api 转发到本服务（前端带 Bearer 调用）。桌面
    形态下这个「反向代理」就是外壳自持的 loopback 代理（W-21 D3）。
    """
    candidates = [Path(web_dist_dir)] if web_dist_dir else _default_web_dist_candidates()
    for web_dist in candidates:
        if web_dist.exists():
            app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="static")
            return


def create_prod_app(settings: Settings | None = None) -> FastAPI:
    """生产工厂：``create_app`` + ``mount_static``。

    dev.sh / Dockerfile 用 ``uvicorn agent_harness.web.app:create_prod_app
    --factory``；测试仍直调 ``create_app``——不挂静态资源，避免 Mount
    遮蔽测试后加的 probe 路由。
    """
    app = create_app(settings, enable_cors=True)
    mount_static(app, settings.web_dist_dir if settings is not None else None)
    return app
