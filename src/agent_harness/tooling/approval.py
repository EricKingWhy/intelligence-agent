"""Approval 类型：审批关卡的数据模型。

05_SANDBOX_CODING_TOOLS.md §6 的 REQUIRE_APPROVAL 机制：
- ApprovalRequest：ToolExecutor 在执行高风险 Tool 前产生的审批请求。
- ApprovalResponse：人类（或自动 callback）的批准/拒绝决定。
- ApprovalCallback：可插拔的审批接口（CLI / Web UI / 自动批准 / 自动拒绝）。

审批只针对当次 Tool Call（per-call scoping 由设计保证：
每次 execute 都独立检查，不存储任何"已批准"状态）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from agent_harness.tooling.contract import PermissionPolicy, ToolPermission


class PermissionDecision(str, Enum):
    """审批决策的四种粒度（03_RUNTIME_EVENT_CONTRACT.md §9 PermissionDecision）。

    当前 runtime 只兑现 deny / approve_once（per-call scoping，不存状态）。
    approve_session / approve_policy 留后续批次（需要 session 级授权缓存）。
    """

    DENY = "deny"
    APPROVE_ONCE = "approve_once"
    APPROVE_SESSION = "approve_session"
    APPROVE_POLICY = "approve_policy"


@dataclass(frozen=True)
class ApprovalRequest:
    """ToolExecutor 向审批方发出的请求。

    包含足够让人类做判断的信息：工具名、参数、授权级别、当前策略、风险原因，
    以及 tool_call_id（前端把审批事件与随后的 tool/call + tool/result 配对用）。
    """

    tool_name: str
    args: dict
    permission: ToolPermission
    policy: PermissionPolicy
    reason: str
    tool_call_id: str | None = None
    # #526 A2：可缓存身份的键（Runtime 从已校验参数派生；None = 不可缓存，回落逐调用）。
    approval_key: str | None = None


@dataclass(frozen=True)
class ApprovalResponse:
    """审批方的决定。

    approved 是兼容字段（现有 runtime 读它决定放行/拒绝）。
    decision 是 spec 契约（03 §9 PermissionDecision），用于审计 trail。
    不传 decision 时由 __post_init__ 从 approved 推导（True→approve_once，
    False→deny）。后续批次支持 approve_session/approve_policy 时显式传 decision。
    """

    approved: bool
    reason: str = ""
    decision: PermissionDecision | None = None

    def __post_init__(self) -> None:
        if self.decision is None:
            # frozen dataclass：用 object.__setattr__ 在 post_init 里设默认
            object.__setattr__(
                self, "decision",
                PermissionDecision.APPROVE_ONCE if self.approved else PermissionDecision.DENY,
            )


#: 可插拔审批回调：接收 ApprovalRequest，返回 ApprovalResponse。
#: 不提供时 ToolExecutor 对超级别或 DANGER 级别默认拒绝（安全默认值）。
#:
#: Phase 5：async 化以支持交互式审批（run 暂停等外部 /approve 决策）。
ApprovalCallback = Callable[[ApprovalRequest], Awaitable[ApprovalResponse]]


def needs_approval(
    tool_permission: ToolPermission, policy: PermissionPolicy
) -> bool:
    """检查工具的授权级别是否超出 Session 当前策略允许的范围。

    层级关系：READ_ONLY ≤ WORKSPACE_WRITE ≤ DANGER_FULL_ACCESS（policy），
    对应 READ_ONLY ≤ WORKSPACE_WRITE ≤ DANGER（tool permission）。

    policy 覆盖范围内的工具直接放行，超出范围的需审批：
    - DANGER_FULL_ACCESS → 任何工具都放行。
    - WORKSPACE_WRITE → READ_ONLY 和 WORKSPACE_WRITE 放行，DANGER 需审批。
    - READ_ONLY → 只有 READ_ONLY 放行，WORKSPACE_WRITE 和 DANGER 需审批。
    """
    if policy == PermissionPolicy.DANGER_FULL_ACCESS:
        return False

    if tool_permission == ToolPermission.READ_ONLY:
        return False

    if tool_permission == ToolPermission.WORKSPACE_WRITE:
        # WORKSPACE_WRITE 工具在 WORKSPACE_WRITE policy 下放行，在 READ_ONLY 下需审批
        return policy == PermissionPolicy.READ_ONLY

    # tool_permission == DANGER：在非 DANGER_FULL_ACCESS policy 下都需审批
    return True


def approval_reason(
    tool_permission: ToolPermission, policy: PermissionPolicy
) -> str:
    """生成人类可读的审批原因。"""
    if policy == PermissionPolicy.READ_ONLY:
        return f"工具授权级别为 {tool_permission.value}，但当前策略为只读（{policy.value}），操作被拒绝。"
    return f"工具授权级别为 {tool_permission.value}，当前策略为 {policy.value}，此操作需要审批。"
# ============================================================================
# #526 A2: approval decision cache (session-scoped approval grant)
#
# Design: docs/agents/526-design-proposal.md section 4 (session-scoped, exact
# operation identity, TTL, policy_at_approval binding, permission upper bound,
# never bypass needs_approval).
# The cache only replaces "the human's repeated decision"; it does not change
# the needs_approval verdict nor relax Runtime boundaries.
# ============================================================================

# Default TTL (seconds) for a session-scoped approval grant = 30 minutes.
# This is a DEFAULT and configurable (design doc section 4.5); callers may
# override per session/source.
# TTL semantics port the design of ZCode's "Allow for session" (session-scoped
# grant) and OpenAI Codex's acceptForSession: one approval may be reused within
# the session, but with a time bound, policy binding and permission upper bound.
APPROVAL_GRANT_TTL_SECONDS = 1800

# Permission rank (follows ToolPermission definition order/values in contract.py):
# READ_ONLY < WORKSPACE_WRITE < DANGER. Used by grant_valid for the permission
# upper-bound check -- a lower-level grant must not cover a higher-level call.
_PERMISSION_RANK: dict[ToolPermission, int] = {
    ToolPermission.READ_ONLY: 0,
    ToolPermission.WORKSPACE_WRITE: 1,
    ToolPermission.DANGER: 2,
}


@dataclass(frozen=True)
class ApprovalIdentity:
    """An exact cacheable operation identity.

    canonical is the denoised canonical string of the operation
    (bash = full command string stripped; write = workspace-relative POSIX
    path); args_hash is the stable hash of all validated params. Together they
    pin the cache granularity to "same tool + same exact operation" with no
    prefix/wildcard relaxation.
    """

    tool_name: str
    kind: str  # 'COMMAND' / 'PATH' / 'MCP' / 'RESOURCE'
    canonical: str
    args_hash: str
    permission: ToolPermission
    policy_at_approval: PermissionPolicy
    contract_version: str = "v1"

    def key(self) -> str:
        """Stable cache key: same identity always yields the same key."""
        return (
            f"{self.tool_name}:{self.kind}:{self.canonical}:{self.args_hash}:"
            f"{self.permission.value}:{self.policy_at_approval.value}:"
            f"{self.contract_version}"
        )


@dataclass(frozen=True)
class ApprovalGrant:
    """One session-scoped grant: identity + absolute expiry (epoch seconds)."""

    identity: ApprovalIdentity
    expires_at: float
    granted_at: float = 0.0


def canonical_args_hash(validated: dict) -> str:
    """sha256 hex of sorted-key JSON serialization of validated params.

    Fixed sort_keys / separators / ensure_ascii so the same logical params
    always hash identically regardless of dict insertion order (a precondition
    for cache hits).
    """
    import hashlib
    import json

    payload = json.dumps(
        dict(validated), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def grant_valid(
    grant: ApprovalGrant,
    *,
    permission: ToolPermission,
    policy: PermissionPolicy,
    now: float,
) -> bool:
    """Whether a session grant is still valid for the current call.

    Three gates (any failure -> False, caller falls back to per-call approval):
    1. TTL: now > grant.expires_at;
    2. policy binding: current policy != policy_at_approval (ADR-0041 D6);
    3. permission upper bound: call's permission rank > grant's permission rank.
    """
    identity = grant.identity
    if now > grant.expires_at:
        return False
    if policy != identity.policy_at_approval:
        return False
    return _PERMISSION_RANK[permission] <= _PERMISSION_RANK[identity.permission]


def approval_identity(
    tool,
    validated_args,
    sandbox,
    *,
    policy: PermissionPolicy = PermissionPolicy.WORKSPACE_WRITE,
) -> ApprovalIdentity | None:
    """Derive a cacheable identity for one validated call; None if not cacheable.

    Tools that do not declare a cacheable identity (incl. MCP / RESOURCE) always
    return None -> fall back to per-call approval.
    The policy default (WORKSPACE_WRITE) only lets callers that merely want the
    path-traversal check omit it; the normal approval path passes the current
    Session policy explicitly from Runtime.

    [Key MUST be generated by Runtime] canonical / args_hash may only be derived
    by Runtime from validated params: the model must never submit, override or
    reuse approval keys. This function is the single key-derivation entry point;
    model-controlled input never serves as a key directly, preventing forged
    cache hits.
    """
    name = tool.name  # string comparison to avoid circular imports of Tool classes

    # The runtime main chain passes validated Pydantic instances
    # (contract.py: model_validate -> execute); tolerate plain dicts too.
    if isinstance(validated_args, dict):
        args = validated_args
    elif hasattr(validated_args, "model_dump"):
        args = validated_args.model_dump()
    else:
        args = dict(validated_args)

    if name == "bash":
        # canonical = full exact command string (only strip() edge whitespace).
        canonical = str(args["command"]).strip()
        kind = "COMMAND"
    elif name == "write":
        from pathlib import Path

        path = str(args["path"])
        root = Path(sandbox.workspace_root).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise ValueError("path escapes workspace")
        # canonical = workspace-relative posix path (traversal rejected above).
        canonical = target.relative_to(root).as_posix()
        kind = "PATH"
    else:
        return None

    return ApprovalIdentity(
        tool_name=name,
        kind=kind,
        canonical=canonical,
        args_hash=canonical_args_hash(args),
        permission=tool.permission,
        policy_at_approval=policy,
        contract_version="v1",
    )
