"""#526 A2：会话级审批授予 / 撤回的 durable 投影（grant / revoke last-wins）。

接缝：``append_approval_grant`` / ``append_approval_revoke`` 写事件，
``derive_approval_grants`` / ``revoked_keys`` 正序重放做只读投影。构造方式沿用
``tests/session`` 既有范式（真 ``JsonlSessionStore`` + ``Session.start``）。
"""

from __future__ import annotations

import time

import pytest

from agent_harness.session.approval import (
    append_approval_grant,
    append_approval_revoke,
    derive_approval_grants,
    revoked_keys,
)
from agent_harness.session.event import PERMISSION_GRANTED
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling.approval import ApprovalGrant, ApprovalIdentity
from agent_harness.tooling.contract import PermissionPolicy, ToolPermission


@pytest.fixture
def session(tmp_path) -> Session:
    """真 store 上的空会话（append-only 投影的读源）。"""
    return Session.start(JsonlSessionStore(root=tmp_path / "sessions"))


def _identity(*, tool_name: str = "bash", canonical: str = "ls /tmp") -> ApprovalIdentity:
    return ApprovalIdentity(
        tool_name=tool_name,
        kind="COMMAND",
        canonical=canonical,
        args_hash="abc",
        permission=ToolPermission.WORKSPACE_WRITE,
        policy_at_approval=PermissionPolicy.WORKSPACE_WRITE,
    )


def _grant(identity: ApprovalIdentity | None = None) -> ApprovalGrant:
    return ApprovalGrant(
        identity=identity or _identity(),
        expires_at=time.time() + 100,
    )


def test_grant_roundtrip(session: Session) -> None:
    grant = _grant()

    append_approval_grant(session, grant)

    grants = derive_approval_grants(session.events)
    key = grant.identity.key()
    assert key in grants
    derived = grants[key]
    assert derived.identity == grant.identity
    assert derived.identity.tool_name == "bash"
    assert derived.identity.kind == "COMMAND"
    assert derived.identity.canonical == "ls /tmp"
    assert derived.identity.args_hash == "abc"
    assert derived.identity.permission is ToolPermission.WORKSPACE_WRITE
    assert derived.identity.policy_at_approval is PermissionPolicy.WORKSPACE_WRITE
    assert derived.expires_at == grant.expires_at


def test_revoke_last_wins(session: Session) -> None:
    grant = _grant()
    key = grant.identity.key()

    # grant → revoke：投影里该 key 消失。
    append_approval_grant(session, grant)
    append_approval_revoke(session, key)
    assert derive_approval_grants(session.events) == {}

    # revoke 之后再 grant 同一 key：后写覆盖，key 重新存在。
    append_approval_grant(session, grant)
    grants = derive_approval_grants(session.events)
    assert key in grants
    assert grants[key].identity == grant.identity


def test_bad_data_skipped(session: Session) -> None:
    # 缺身份字段的 grant 事件：derive 不抛异常、跳过。
    session.append(PERMISSION_GRANTED, {"approval_key": "broken"})
    # 字段齐全但值不可解析（非法 permission）：同样跳过。
    session.append(
        PERMISSION_GRANTED,
        {
            "approval_key": "bad-perm",
            "tool_name": "bash",
            "kind": "COMMAND",
            "canonical": "ls /tmp",
            "args_hash": "abc",
            "permission": "not-a-permission",
            "policy_at_approval": "workspace-write",
            "expires_at": time.time() + 100,
        },
    )

    grants = derive_approval_grants(session.events)

    assert grants == {}


def test_revoked_keys(session: Session) -> None:
    append_approval_revoke(session, "key-a")
    append_approval_revoke(session, "key-b")

    assert revoked_keys(session.events) == {"key-a", "key-b"}
