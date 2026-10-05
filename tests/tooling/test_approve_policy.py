"""#684 Phase 1：持久审批规则核心（approve_policy）——TDD 红阶段。

本文件引用尚不存在的符号（``PolicyGranularity`` / ``ApprovePolicyRule`` /
``ApprovePolicyStore``；``ApprovalResponse.policy_granularity``），当前应为
ImportError / AttributeError（红）。实现后按本文件的行为契约转绿。

设计依据：``docs/agents/684-design-proposal.md`` v3 §4–§9。
安全约束：F21（显式授权）/ F22（无模糊匹配）/ ADR-0041 D6（三概念分离）。

对标来源（PORT DESIGN）：Claude Code settings.json（项目级文件形态）、
Codex acceptWithExecpolicyAmendment（精确安装 / 换行拒绝）、ZCode（两档粒度）。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.session.approval import InteractiveCallbackHolder
from agent_harness.session.event import TOOL_APPROVAL_REQUESTED
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tooling.approval import (
    ApprovalIdentity,
    ApprovalRequest,
    ApprovalResponse,
    PermissionDecision,
    approval_identity,
)
from agent_harness.tooling.approval_queue import PendingApprovalQueue
from agent_harness.tooling.approve_policy import (
    ApprovePolicyRule,
    ApprovePolicyStore,
    PolicyGranularity,
)
from agent_harness.tooling.contract import PermissionPolicy, ToolPermission
from agent_harness.tools import BashTool

_POLICY_FILE = Path(".agent-harness") / "approve-policy.json"


# ── helpers ────────────────────────────────────────────────────────────────


def _identity(
    *,
    tool_name: str = "bash",
    kind: str = "COMMAND",
    canonical: str = "npm run build",
    args_hash: str = "h1",
    permission: ToolPermission = ToolPermission.DANGER,
    policy: PermissionPolicy = PermissionPolicy.WORKSPACE_WRITE,
) -> ApprovalIdentity:
    return ApprovalIdentity(
        tool_name=tool_name,
        kind=kind,
        canonical=canonical,
        args_hash=args_hash,
        permission=permission,
        policy_at_approval=policy,
        contract_version="v1",
    )


def _rule(
    identity: ApprovalIdentity, granularity: PolicyGranularity, *, rule_id: str = "r1",
) -> ApprovePolicyRule:
    key = identity.key() if granularity is PolicyGranularity.EXACT else identity.canonical
    return ApprovePolicyRule(
        id=rule_id,
        tool=identity.tool_name,
        key=key,
        granularity=granularity,
        permission_at_approval=identity.permission,
        created_at="2026-10-05T00:00:00Z",
    )


def _bash_identity(
    sandbox: LocalSubprocessSandbox, command: str, policy: PermissionPolicy,
) -> ApprovalIdentity:
    tool = BashTool(sandbox)
    return approval_identity(
        tool, tool.args_schema(command=command), sandbox, policy=policy,
    )


def _tc(name: str, args: dict, call_id: str = "c1") -> dict:
    return {"id": call_id, "name": name, "args": args}


class _CountingApproval:
    """计数回调：记录被调用次数，恒返回指定 decision。"""

    def __init__(self, decision: PermissionDecision = PermissionDecision.APPROVE_ONCE) -> None:
        self.calls = 0
        self._decision = decision

    async def __call__(self, _req: ApprovalRequest) -> ApprovalResponse:
        self.calls += 1
        return ApprovalResponse(approved=True, decision=self._decision)


class _PolicyApproval:
    """恒返回 APPROVE_POLICY 的计数回调，可指定安装粒度。"""

    def __init__(self, granularity: PolicyGranularity = PolicyGranularity.EXACT) -> None:
        self.calls = 0
        self._granularity = granularity

    async def __call__(self, _req: ApprovalRequest) -> ApprovalResponse:
        self.calls += 1
        return ApprovalResponse(
            approved=True,
            decision=PermissionDecision.APPROVE_POLICY,
            policy_granularity=self._granularity,
        )


async def _wait_for_pending(queue: PendingApprovalQueue, tries: int = 200) -> None:
    for _ in range(tries):
        if queue.pending_ids():
            return
        await asyncio.sleep(0)
    raise AssertionError("approval request never registered")


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSubprocessSandbox:
    return LocalSubprocessSandbox(workspace_root=tmp_path)


def _registry(sandbox: LocalSubprocessSandbox) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(BashTool(sandbox))
    return registry


# ── 数据模型 ────────────────────────────────────────────────────────────────


def test_granularity_values() -> None:
    assert PolicyGranularity.EXACT.value == "exact"
    assert PolicyGranularity.COMMAND.value == "command"


def test_approval_response_defaults_to_exact() -> None:
    plain = ApprovalResponse(approved=True)
    assert plain.policy_granularity is PolicyGranularity.EXACT


def test_approval_response_accepts_command_granularity() -> None:
    response = ApprovalResponse(
        approved=True,
        decision=PermissionDecision.APPROVE_POLICY,
        policy_granularity=PolicyGranularity.COMMAND,
    )
    assert response.policy_granularity is PolicyGranularity.COMMAND


# ── 存储 ────────────────────────────────────────────────────────────────────


class TestStorePersistence:
    def test_path_is_project_relative(self, tmp_path: Path) -> None:
        assert ApprovePolicyStore(tmp_path).path == tmp_path / _POLICY_FILE

    def test_missing_file_loads_empty(self, tmp_path: Path) -> None:
        assert ApprovePolicyStore(tmp_path).load() == []

    def test_roundtrip(self, tmp_path: Path) -> None:
        rule = _rule(_identity(), PolicyGranularity.EXACT)
        ApprovePolicyStore(tmp_path).add_rule(rule)
        assert ApprovePolicyStore(tmp_path).load() == [rule]

    def test_corrupt_file_fails_closed(self, tmp_path: Path) -> None:
        # 坏配置不能臆造规则放行：读不动就当成没有规则（默认逐调用审批）。
        target = tmp_path / _POLICY_FILE
        target.parent.mkdir(parents=True)
        target.write_text("{ not json", encoding="utf-8")
        assert ApprovePolicyStore(tmp_path).load() == []

    def test_invalid_utf8_fails_closed(self, tmp_path: Path) -> None:
        # P2-1：非 UTF-8 字节 → read_text 抛 UnicodeDecodeError（ValueError 子类，
        # 不是 OSError）。人工手改 / 编码损坏的文件不得把 run 顶崩，须 fail-closed。
        target = tmp_path / _POLICY_FILE
        target.parent.mkdir(parents=True)
        target.write_bytes(b"\xff\xfe{\"rules\": []}")
        assert ApprovePolicyStore(tmp_path).load() == []


class TestConcurrentWrites:
    def test_parallel_add_rules_lose_none(self, tmp_path: Path) -> None:
        """P2-2：add_rule 是 load→append→save 读-改-写，无并发保护会互相覆盖。

        8 个线程经 Barrier 同时进入 ⇒ 文件锁（flock）串行化后每条规则都在，
        零丢更新。无锁时后写者会覆盖先写者，本断言即红。
        """
        store = ApprovePolicyStore(tmp_path)
        count = 8
        barrier = threading.Barrier(count)
        failures: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait()
                store.add_rule(
                    _rule(
                        _identity(canonical=f"cmd-{index}", args_hash=f"h{index}"),
                        PolicyGranularity.COMMAND,
                        rule_id=f"r{index}",
                    )
                )
            except (OSError, ValueError) as exc:  # pragma: no cover - 失败路径
                failures.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i,)) for i in range(count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert failures == []
        assert sorted(r.id for r in store.load()) == sorted(
            f"r{i}" for i in range(count)
        )


# ── 命中判定 ────────────────────────────────────────────────────────────────


class TestMatching:
    def test_exact_hits_identical_identity(self, tmp_path: Path) -> None:
        store = ApprovePolicyStore(tmp_path)
        identity = _identity()
        store.add_rule(_rule(identity, PolicyGranularity.EXACT))
        assert store.find_match(identity) is not None

    def test_exact_misses_when_args_hash_differs(self, tmp_path: Path) -> None:
        # write：canonical（路径）相同但内容不同 → args_hash 不同 → exact 不命中。
        store = ApprovePolicyStore(tmp_path)
        approved = _identity(
            tool_name="write", kind="PATH", canonical="f.txt",
            args_hash="hashA", permission=ToolPermission.WORKSPACE_WRITE,
        )
        changed = _identity(
            tool_name="write", kind="PATH", canonical="f.txt",
            args_hash="hashB", permission=ToolPermission.WORKSPACE_WRITE,
        )
        store.add_rule(_rule(approved, PolicyGranularity.EXACT))
        assert store.find_match(changed) is None

    def test_command_ignores_args_hash(self, tmp_path: Path) -> None:
        # command 档只认 tool + canonical，不含 args_hash。
        store = ApprovePolicyStore(tmp_path)
        approved = _identity(
            tool_name="write", kind="PATH", canonical="f.txt",
            args_hash="hashA", permission=ToolPermission.WORKSPACE_WRITE,
        )
        changed = _identity(
            tool_name="write", kind="PATH", canonical="f.txt",
            args_hash="hashB", permission=ToolPermission.WORKSPACE_WRITE,
        )
        rule = store.add_rule(_rule(approved, PolicyGranularity.COMMAND))
        assert store.find_match(changed) == rule

    def test_command_hits_same_command_and_misses_other(self, tmp_path: Path) -> None:
        store = ApprovePolicyStore(tmp_path)
        store.add_rule(
            _rule(_identity(canonical="npm run build"), PolicyGranularity.COMMAND)
        )
        assert store.find_match(_identity(canonical="npm run build")) is not None
        assert store.find_match(_identity(canonical="npm run test")) is None

    def test_permission_change_invalidates(self, tmp_path: Path) -> None:
        # 命中后必须重验工具当前 permission == permission_at_approval（防提权）。
        store = ApprovePolicyStore(tmp_path)
        approved = _identity(
            canonical="echo hi", permission=ToolPermission.WORKSPACE_WRITE,
        )
        store.add_rule(_rule(approved, PolicyGranularity.COMMAND))
        assert store.find_match(
            _identity(canonical="echo hi", permission=ToolPermission.DANGER)
        ) is None
        assert store.find_match(
            _identity(canonical="echo hi", permission=ToolPermission.WORKSPACE_WRITE)
        ) is not None


# ── 安装守卫 / 撤销 ─────────────────────────────────────────────────────────


class TestInstallGuards:
    def test_command_rule_rejects_newline(self, tmp_path: Path) -> None:
        store = ApprovePolicyStore(tmp_path)
        rule = _rule(_identity(canonical="echo a\necho b"), PolicyGranularity.COMMAND)
        with pytest.raises(ValueError):
            store.add_rule(rule)
        assert store.load() == []

    def test_remove_rule_idempotent(self, tmp_path: Path) -> None:
        store = ApprovePolicyStore(tmp_path)
        store.add_rule(_rule(_identity(), PolicyGranularity.EXACT))
        assert store.remove_rule("r1") is True
        assert store.load() == []
        assert store.remove_rule("r1") is False
        assert store.remove_rule("missing") is False


# ── project 隔离 ────────────────────────────────────────────────────────────


class TestProjectIsolation:
    def test_rule_in_other_project_not_matched(self, tmp_path: Path) -> None:
        identity = _identity()
        project_a = ApprovePolicyStore(tmp_path / "a")
        project_b = ApprovePolicyStore(tmp_path / "b")
        project_a.add_rule(_rule(identity, PolicyGranularity.EXACT))
        assert project_a.find_match(identity) is not None
        assert project_b.find_match(identity) is None


# ── executor 集成 ───────────────────────────────────────────────────────────


class TestExecutorIntegration:
    @pytest.mark.asyncio
    async def test_persistent_rule_skips_callback(
        self, tmp_path: Path, sandbox: LocalSubprocessSandbox,
    ) -> None:
        identity = _bash_identity(sandbox, "echo hi", PermissionPolicy.READ_ONLY)
        ApprovePolicyStore(tmp_path).add_rule(_rule(identity, PolicyGranularity.EXACT))
        approver = _CountingApproval()
        executor = ToolExecutor(
            _registry(sandbox), policy=PermissionPolicy.READ_ONLY,
            approval_callback=approver, project_root=tmp_path,
        )
        result = await executor.execute(_tc("bash", {"command": "echo hi"}))
        assert result.result.ok is True
        assert approver.calls == 0

    @pytest.mark.asyncio
    async def test_approve_policy_installs_exact_rule(
        self, tmp_path: Path, sandbox: LocalSubprocessSandbox,
    ) -> None:
        approver = _PolicyApproval(PolicyGranularity.EXACT)
        executor = ToolExecutor(
            _registry(sandbox), policy=PermissionPolicy.READ_ONLY,
            approval_callback=approver, project_root=tmp_path,
        )
        first = await executor.execute(_tc("bash", {"command": "echo hi"}, "c1"))
        assert first.result.ok is True
        assert approver.calls == 1

        rules = ApprovePolicyStore(tmp_path).load()
        assert len(rules) == 1
        assert rules[0].granularity is PolicyGranularity.EXACT
        assert rules[0].tool == "bash"

        # 换 executor（模拟新会话）：同一命令命中持久规则，不再问人。
        second_approver = _PolicyApproval(PolicyGranularity.EXACT)
        executor2 = ToolExecutor(
            _registry(sandbox), policy=PermissionPolicy.READ_ONLY,
            approval_callback=second_approver, project_root=tmp_path,
        )
        second = await executor2.execute(_tc("bash", {"command": "echo hi"}, "c2"))
        assert second.result.ok is True
        assert second_approver.calls == 0

    @pytest.mark.asyncio
    async def test_approve_policy_installs_command_rule(
        self, tmp_path: Path, sandbox: LocalSubprocessSandbox,
    ) -> None:
        approver = _PolicyApproval(PolicyGranularity.COMMAND)
        executor = ToolExecutor(
            _registry(sandbox), policy=PermissionPolicy.READ_ONLY,
            approval_callback=approver, project_root=tmp_path,
        )
        await executor.execute(_tc("bash", {"command": "echo hi"}, "c1"))
        rules = ApprovePolicyStore(tmp_path).load()
        assert rules[0].granularity is PolicyGranularity.COMMAND
        assert rules[0].key == "echo hi"

    @pytest.mark.asyncio
    async def test_command_policy_newline_rejected_without_crash(
        self, tmp_path: Path, sandbox: LocalSubprocessSandbox,
    ) -> None:
        approver = _PolicyApproval(PolicyGranularity.COMMAND)
        executor = ToolExecutor(
            _registry(sandbox), policy=PermissionPolicy.READ_ONLY,
            approval_callback=approver, project_root=tmp_path,
        )
        result = await executor.execute(_tc("bash", {"command": "echo a\necho b"}))
        # 本次调用仍被显式批准，但含换行的命令级规则拒绝安装（不崩溃）。
        assert result.result.ok is True
        assert ApprovePolicyStore(tmp_path).load() == []

    @pytest.mark.asyncio
    async def test_persistent_rule_permission_recheck_at_executor(
        self, tmp_path: Path, sandbox: LocalSubprocessSandbox,
    ) -> None:
        # 规则 permission_at_approval=WORKSPACE_WRITE，但 bash 当前是 DANGER → 不命中。
        store = ApprovePolicyStore(tmp_path)
        stale = _identity(
            canonical="echo hi", permission=ToolPermission.WORKSPACE_WRITE,
        )
        store.add_rule(_rule(stale, PolicyGranularity.COMMAND))
        approver = _CountingApproval()
        executor = ToolExecutor(
            _registry(sandbox), policy=PermissionPolicy.READ_ONLY,
            approval_callback=approver, project_root=tmp_path,
        )
        result = await executor.execute(_tc("bash", {"command": "echo hi"}))
        assert result.result.ok is True
        assert approver.calls == 1

    @pytest.mark.asyncio
    async def test_other_project_rule_does_not_apply(
        self, tmp_path: Path, sandbox: LocalSubprocessSandbox,
    ) -> None:
        project_a = tmp_path / "a"
        ApprovePolicyStore(project_a).add_rule(
            _rule(_identity(canonical="echo hi"), PolicyGranularity.COMMAND)
        )
        approver = _CountingApproval()
        executor = ToolExecutor(
            _registry(sandbox), policy=PermissionPolicy.READ_ONLY,
            approval_callback=approver, project_root=tmp_path / "b",
        )
        result = await executor.execute(_tc("bash", {"command": "echo hi"}))
        assert result.result.ok is True
        assert approver.calls == 1


# ── 会话审批卡：allowed_decisions 提供第三档 ─────────────────────────────────


class TestAllowedDecisions:
    @pytest.mark.asyncio
    async def test_approve_policy_offered_for_cacheable_identity(
        self, tmp_path: Path,
    ) -> None:
        session = Session.start(JsonlSessionStore(root=tmp_path / "sessions"))
        queue = PendingApprovalQueue()
        holder = InteractiveCallbackHolder(queue=queue, timeout_seconds=0)
        holder.bind_session(session)
        request = ApprovalRequest(
            tool_name="bash", args={"command": "echo hi"},
            permission=ToolPermission.DANGER, policy=PermissionPolicy.WORKSPACE_WRITE,
            reason="r", tool_call_id="c1", approval_key="key-1",
        )
        task = asyncio.ensure_future(holder(request))
        await _wait_for_pending(queue)
        queue.resolve(
            queue.pending_ids()[0],
            ApprovalResponse(approved=True, decision=PermissionDecision.APPROVE_ONCE),
        )
        await task
        event = next(e for e in session.events if e.type == TOOL_APPROVAL_REQUESTED)
        allowed = event.data["allowed_decisions"]
        assert PermissionDecision.APPROVE_POLICY.value in allowed
        assert PermissionDecision.APPROVE_SESSION.value in allowed

    @pytest.mark.asyncio
    async def test_approve_policy_absent_without_approval_key(
        self, tmp_path: Path,
    ) -> None:
        session = Session.start(JsonlSessionStore(root=tmp_path / "sessions"))
        queue = PendingApprovalQueue()
        holder = InteractiveCallbackHolder(queue=queue, timeout_seconds=0)
        holder.bind_session(session)
        request = ApprovalRequest(
            tool_name="mcp_tool", args={},
            permission=ToolPermission.DANGER, policy=PermissionPolicy.WORKSPACE_WRITE,
            reason="r", tool_call_id="c1", approval_key=None,
        )
        task = asyncio.ensure_future(holder(request))
        await _wait_for_pending(queue)
        queue.resolve(
            queue.pending_ids()[0],
            ApprovalResponse(approved=True, decision=PermissionDecision.APPROVE_ONCE),
        )
        await task
        event = next(e for e in session.events if e.type == TOOL_APPROVAL_REQUESTED)
        allowed = event.data["allowed_decisions"]
        assert PermissionDecision.APPROVE_POLICY.value not in allowed
        assert PermissionDecision.APPROVE_SESSION.value not in allowed
