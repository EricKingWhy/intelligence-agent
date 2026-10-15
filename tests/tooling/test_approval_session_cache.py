"""#526 A2 审批决策缓存：canonical 键 / 授权 TTL / policy 绑定 / 权限上界（TDD 红阶段）。

本文件引用尚不存在的符号（ApprovalIdentity / ApprovalGrant / grant_valid /
canonical_args_hash / approval_identity / APPROVAL_GRANT_TTL_SECONDS），当前应为
ImportError（红）。实现后按本文件的行为契约转绿。

设计依据：docs/agents/526-design-proposal.md §4（A2：会话内、精确操作身份、TTL、
绑定 policy_at_approval、权限上界、永不绕过 needs_approval）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.tooling.approval import (
    APPROVAL_GRANT_TTL_SECONDS,
    ApprovalGrant,
    ApprovalIdentity,
    approval_identity,
    canonical_args_hash,
    grant_valid,
    needs_approval,
)
from agent_harness.tooling.contract import PermissionPolicy, ToolPermission
from agent_harness.tools.bash import BashTool
from agent_harness.tools.write import WriteTool

_NOW = 1_000_000.0


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


def _identity(
    *,
    canonical: str = "ls /tmp",
    permission: ToolPermission = ToolPermission.WORKSPACE_WRITE,
    policy_at_approval: PermissionPolicy = PermissionPolicy.WORKSPACE_WRITE,
) -> ApprovalIdentity:
    return ApprovalIdentity(
        tool_name="bash",
        kind="COMMAND",
        canonical=canonical,
        args_hash=canonical_args_hash({"command": canonical}),
        permission=permission,
        policy_at_approval=policy_at_approval,
        contract_version="v1",
    )


def _grant(identity: ApprovalIdentity, *, expires_at: float) -> ApprovalGrant:
    return ApprovalGrant(identity=identity, expires_at=expires_at)


def test_canonical_key_command_exact():
    """只有 command 不同的两个 bash 调用 → 不同 canonical 键；完全相同 → 相同键。"""
    policy = PermissionPolicy.WORKSPACE_WRITE

    key_tmp = ApprovalIdentity(
        tool_name="bash",
        kind="COMMAND",
        canonical="ls /tmp",
        args_hash=canonical_args_hash({"command": "ls /tmp"}),
        permission=ToolPermission.READ_ONLY,
        policy_at_approval=policy,
        contract_version="v1",
    ).key()
    key_var = ApprovalIdentity(
        tool_name="bash",
        kind="COMMAND",
        canonical="ls /var",
        args_hash=canonical_args_hash({"command": "ls /var"}),
        permission=ToolPermission.READ_ONLY,
        policy_at_approval=policy,
        contract_version="v1",
    ).key()
    key_tmp_again = ApprovalIdentity(
        tool_name="bash",
        kind="COMMAND",
        canonical="ls /tmp",
        args_hash=canonical_args_hash({"command": "ls /tmp"}),
        permission=ToolPermission.READ_ONLY,
        policy_at_approval=policy,
        contract_version="v1",
    ).key()

    assert key_tmp != key_var
    assert key_tmp == key_tmp_again


def test_grant_ttl_expiry():
    """expires_at 之后 grant_valid 返回 False（TTL 内仍有效）。"""
    identity = _identity()
    fresh = _grant(identity, expires_at=_NOW + APPROVAL_GRANT_TTL_SECONDS)
    expired = _grant(identity, expires_at=_NOW - 1.0)

    assert grant_valid(
        fresh,
        permission=ToolPermission.WORKSPACE_WRITE,
        policy=PermissionPolicy.WORKSPACE_WRITE,
        now=_NOW,
    ) is True
    assert grant_valid(
        expired,
        permission=ToolPermission.WORKSPACE_WRITE,
        policy=PermissionPolicy.WORKSPACE_WRITE,
        now=_NOW,
    ) is False


def test_grant_policy_change_invalidates():
    """当前 policy ≠ policy_at_approval → grant_valid False（改档后必须重批）。"""
    identity = _identity(
        permission=ToolPermission.DANGER,
        policy_at_approval=PermissionPolicy.WORKSPACE_WRITE,
    )
    grant = _grant(identity, expires_at=_NOW + APPROVAL_GRANT_TTL_SECONDS)

    assert grant_valid(
        grant,
        permission=ToolPermission.DANGER,
        policy=PermissionPolicy.WORKSPACE_WRITE,
        now=_NOW,
    ) is True
    assert grant_valid(
        grant,
        permission=ToolPermission.DANGER,
        policy=PermissionPolicy.READ_ONLY,
        now=_NOW,
    ) is False


def test_grant_permission_upper_bound():
    """低级别授权（WORKSPACE_WRITE）不能覆盖更高级别（DANGER）调用。"""
    identity = _identity(
        permission=ToolPermission.WORKSPACE_WRITE,
        policy_at_approval=PermissionPolicy.WORKSPACE_WRITE,
    )
    grant = _grant(identity, expires_at=_NOW + APPROVAL_GRANT_TTL_SECONDS)

    assert grant_valid(
        grant,
        permission=ToolPermission.DANGER,
        policy=PermissionPolicy.WORKSPACE_WRITE,
        now=_NOW,
    ) is False
    assert grant_valid(
        grant,
        permission=ToolPermission.WORKSPACE_WRITE,
        policy=PermissionPolicy.WORKSPACE_WRITE,
        now=_NOW,
    ) is True


def test_needs_approval_unchanged(sandbox: LocalSubprocessSandbox):
    """缓存只替代人的决定，永不绕过 needs_approval（有 grant 与无 grant 判定一致）。"""
    tool_perm = BashTool(sandbox).permission  # DANGER
    policy = PermissionPolicy.WORKSPACE_WRITE

    without_grant = needs_approval(tool_perm, policy)

    # 存在一条对该调用完全有效的会话授权，needs_approval 仍不得被绕过。
    grant = _grant(
        _identity(permission=tool_perm, policy_at_approval=policy),
        expires_at=_NOW + APPROVAL_GRANT_TTL_SECONDS,
    )
    assert grant_valid(grant, permission=tool_perm, policy=policy, now=_NOW) is True

    with_grant = needs_approval(tool_perm, policy)

    assert without_grant is True
    assert with_grant == without_grant


def test_path_traversal_rejected_before_key(sandbox: LocalSubprocessSandbox):
    """write 传 ../evil → 越界在成键前被拒，approval_identity 抛 ValueError。"""
    tool = WriteTool(sandbox)
    args = tool.args_schema(path="../evil", content="x")

    with pytest.raises(ValueError):
        approval_identity(tool, args, sandbox)
