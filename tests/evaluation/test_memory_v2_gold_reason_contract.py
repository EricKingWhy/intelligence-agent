"""#1440 C 案：金标 secret 探针按 `reason_code` 判定（判别力新测）。

三件事必须同时成立，缺一条这份契约就是空话：

① **文案无关性**：拒绝文案换成任意字面值，两条边界探针（direct / api_edit）仍判
   blocked=True——判定读的是 `reason_code`，不是中文串。
② **守卫仍咬人**：把 `_reject_secret` 猴补成 no-op，探针必须判 False（金标会因此变红，
   实测读数见报告）。
③ **fail-closed**：`reason_code` 缺失或未知码，两种构造都判未阻断。
④ **口径一致**：显式路径（`remember_this`）的秘密拒绝同样带机读码——`ToolResult` 无
   专用字段，落在 `metadata.reason_code`（`tools.py`）。

既有金标测试（`test_memory_v2_gold_gate.py`）的断言一字未动，本文件是新增面。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_harness.identity import (
    IdentityContext,
    identity_context_var,
    set_identity_context,
)
from agent_harness.memory.types import memory_session_var
from agent_harness.memory.v2.capability import (
    MemoryContentRejected,
    MemoryRejectionReason,
    MemoryV2Service,
)
from agent_harness.memory.v2.index import InMemoryMemoryV2Index, MemoryV2IndexRelay
from agent_harness.memory.v2.store import SqliteMemoryV2Store
from agent_harness.memory.v2.tools import RememberMemoryV2Tool, _RememberV2Args
from agent_harness.memory.v2.types import TrustedMemoryIdentity
from agent_harness.session import (
    RUN_STARTED,
    USER_MESSAGE,
    JsonlSessionStore,
    SessionEvent,
)
from agent_harness.tooling.result import ErrorCode
from evaluation.memory_v2_gold_adapter import CaseWorkspace, _run_secret_write_probe

_IDENTITY = TrustedMemoryIdentity("gold-tenant", "gold-user", "gold-project")


async def _probe(path: str, tmp_path: Path) -> bool:
    workspace = CaseWorkspace(tmp_path / f"gold-{path}")
    workspace.root.mkdir(parents=True, exist_ok=True)
    service, _jobs = await workspace.open()
    try:
        return await _run_secret_write_probe(
            path, service=service, trusted=_IDENTITY, workspace_root=workspace.root,
        )
    finally:
        await service.aclose()


def _patch_reject_secret(monkeypatch, behaviour) -> None:
    """把入口守卫换成指定行为（`draft` 进、按需抛）。"""

    def _reject(draft):
        behaviour(draft)

    monkeypatch.setattr(MemoryV2Service, "_reject_secret", staticmethod(_reject))


def _raises_on_secret(reason_code, message: str):
    """只对含哨兵的内容抛拒绝——安全写入必须照常成功，探针才有对照。"""

    def behaviour(draft) -> None:
        if "NEVER-A-REAL-CREDENTIAL" in draft.content:
            raise MemoryContentRejected(message, reason_code)

    return behaviour


# ---------------------------------------------------------------------------
# ① 文案无关性
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["direct", "api_edit"])
def test_blocked_judgement_ignores_the_rejection_text(path, tmp_path, monkeypatch):
    """拒绝文案换成任意字面值，blocked 仍为 True（direct 与 HTTP 过界两条都测）。"""
    arbitrary = "zzz 任意字面值 / not the policy message at all"
    _patch_reject_secret(monkeypatch, _raises_on_secret(
        MemoryRejectionReason.SECRET_CONTENT, arbitrary,
    ))

    assert asyncio.run(_probe(path, tmp_path)) is True


# ---------------------------------------------------------------------------
# ② 守卫仍咬人
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["direct", "api_edit"])
def test_noop_guard_makes_the_probe_fail(path, tmp_path, monkeypatch):
    """变异：`_reject_secret` 不再拒绝 ⇒ 探针必须判 False（守卫被拆掉看得见）。"""
    _patch_reject_secret(monkeypatch, lambda draft: None)

    assert asyncio.run(_probe(path, tmp_path)) is False


# ---------------------------------------------------------------------------
# ③ fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["direct", "api_edit"])
def test_missing_reason_code_is_not_blocked(path, tmp_path, monkeypatch):
    """异常没有 `reason_code`（缺省构造）⇒ 判未阻断，不按文案补判。"""
    _patch_reject_secret(monkeypatch, _raises_on_secret(
        None, "该内容匹配凭证或密钥规则，不能写入长期记忆。",
    ))

    assert asyncio.run(_probe(path, tmp_path)) is False


@pytest.mark.parametrize("path", ["direct", "api_edit"])
def test_unknown_reason_code_is_not_blocked(path, tmp_path, monkeypatch):
    """未知码 ⇒ 判未阻断（新码不会因为"看起来像拒绝"被算成阻断）。"""
    _patch_reject_secret(monkeypatch, _raises_on_secret(
        "some_future_reason", "该内容匹配凭证或密钥规则，不能写入长期记忆。",
    ))

    assert asyncio.run(_probe(path, tmp_path)) is False


# ---------------------------------------------------------------------------
# ④ 显式路径同口径
# ---------------------------------------------------------------------------


def test_explicit_remember_rejection_carries_the_same_reason_code(tmp_path):
    """`remember_this` 的秘密拒绝带同一个码，且文案仍是中文真因。"""

    async def _run() -> object:
        store = SqliteMemoryV2Store(tmp_path / "explicit.db")
        await store.initialize()
        index = InMemoryMemoryV2Index()
        service = MemoryV2Service(store, index, relay=MemoryV2IndexRelay(store, index))
        sessions = JsonlSessionStore(root=tmp_path / "sessions")
        tool = RememberMemoryV2Tool(service, sessions)
        session_id = "explicit-secret"
        text = "Please remember my api key is sk-live-abcdefghijklmnopqrstuvwxyz012345"
        sessions.append_event(session_id, SessionEvent(
            seq=0, type=USER_MESSAGE, session_id=session_id, data={"content": text},
        ))
        sessions.append_event(session_id, SessionEvent(
            seq=1, type=RUN_STARTED, session_id=session_id, run_id="run-1", data={},
        ))
        identity_token = set_identity_context(IdentityContext("t", "u", ["user"]))
        session_token = memory_session_var.set(session_id)
        try:
            return await tool.execute(_RememberV2Args(content=text[len("Please remember "):]))
        finally:
            memory_session_var.reset(session_token)
            identity_context_var.reset(identity_token)
            await service.aclose()

    result = asyncio.run(_run())

    assert result.ok is False
    assert result.error_code is ErrorCode.PERMISSION_DENIED
    assert result.metadata == {"reason_code": MemoryRejectionReason.SECRET_CONTENT.value}
    assert "凭证" in result.message and "身份无权" not in result.message
