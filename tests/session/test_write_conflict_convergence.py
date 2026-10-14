"""#1081：三个非 live 命令写路径的 seq 冲突编排收敛到**一个**点。

**症状**（维护性缺陷，非用户故障）：``change_model`` /
``change_permission_mode`` / ``_apply_task_handler`` 曾各自维护同一段
「优先 live 聚合；非 live 读快照、校验 seq、追加；写时 ``SeqConflict`` 后有限次
重读」，三个副本靠互相引用注释保持同步——以后改重试次数 / 404-409 判据 / live
路径时很容易只改一处。

**不变量**：三条非 live 命令写路径共用 ``SessionService`` 内一个私有编排
（``_apply_write_with_conflict_retry``），且顺序契约一字不差：

1. 每轮先查 live（在途聚合由事件循环串行化、不可重建 ⇒ 不重试）；
2. 非 live 下空快照 = 404（``SessionNotFound``）；
3. ``validate_event_seq`` 在写入 ``try`` **之外**（读时损坏是**终态** 409，不得被
   当成可重试的写时冲突吞掉）；
4. 只捕获 callback 写入时的 ``SeqConflict``，上限 ``_WRITE_CONFLICT_ATTEMPTS``
   内**重读快照并重跑整个 callback**，超限原样抛出。

**方案依据**（`AGENTS.md` §6.1，PORT DESIGN，只借设计不抄代码）：
Kurrent/EventStoreDB 官方 append 文档的 expected version /
``WrongExpectedVersionException``（写时冲突由存储显式拒绝、调用方在自己的命令边界
重读重算；损坏日志走它自己的 fail-closed 出口）；Marten 官方并发文档的
``FetchForWriting`` / optimistic concurrency（命令以新流版本重建待追加结果，版本
不匹配明确失败——对应 Task handler 必须连同 CAS 判定重算）。本仓 store 的 seq 守卫
已承担拒写，本文件只钉「三个入口经同一编排点」这条可观察行为。

**红证**：本文件在收敛前的树上必红——``SessionService`` 没有
``_apply_write_with_conflict_retry`` 这个属性，spy 的 ``setattr`` 直接
``AttributeError``；实测 9 failed / 12 passed（``TestSingleOrchestrationPoint``
与 ``TestConflictRetrySemantics`` 的 3 个用例 × 3 个入口）。

**调用面**：票面点名三条命令；实际是**四条**——``progress_resolve_external_edit``
经 ``_apply_task_handler`` 落到同一编排点（本文件只参数化票面那三条）。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_harness.config import Settings
from agent_harness.session.approval import PermissionChange
from agent_harness.session.errors import SeqConflict, SessionNotFound
from agent_harness.session.model_switch import ModelChange
from agent_harness.session.service import SessionService
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.session.task import apply_task_definition
from tests.session.ledger_doubles import idle_operation_ledger

_CATALOG = (
    '[{"name": "gpt-4o", "provider": "deepseek", "model_name": "gpt-4o-mini"},'
    ' {"name": "mimo-v2.6-flash", "provider": "mimo", "model_name": "mimo-v2.6-flash"}]'
)

#: 三条非 live 命令写路径。收敛前它们各自持有一份逐字相同的重试循环。
ENTRIES = ["model", "permission", "task"]


def _state(tmp_path) -> MagicMock:
    """与 tests/session/test_model_change.py::_state 同构的隔离 AppState 替身。"""
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_provider="deepseek",
        model_name="deepseek-chat",
        agent_models=_CATALOG,
        provider_store_path=str(tmp_path / "model-providers.json"),
    )
    state = MagicMock()
    state.settings = settings
    state.store = JsonlSessionStore(root=tmp_path / "sessions")
    state.workspaces_root = tmp_path
    state.workspace_registry = None
    state.workspace_index = None
    state.run_manager = MagicMock()
    state.run_manager.get_active = MagicMock(return_value=None)
    state.run_manager.launch = MagicMock(return_value=(MagicMock(), MagicMock()))
    state.get_wiring = AsyncMock(return_value=(MagicMock(), MagicMock()))
    state.operation_ledger = idle_operation_ledger()
    state.ensure_stores = AsyncMock()
    state.stores = MagicMock()
    state.stores.delegation_tree_ledger.get_session_budget = AsyncMock(return_value=None)
    state.approval_queues = {}
    return state


def _service(state) -> SessionService:
    from agent_harness.web.app import session_service

    return session_service(state)


def _seed(state, entry: str) -> Session:
    """按入口造出「命令可成功」的前置状态。"""
    session = Session.start(
        state.store,
        session_id="sid",
        started_data={"provider": "deepseek", "model_id": "gpt-4o"},
    )
    if entry == "task":
        assert apply_task_definition(session, task_text="T", criteria=[{"text": "A"}]).ok
    return session


def _call(entry: str, state):
    """驱动该入口的一次非 live 写入。"""
    service = _service(state)
    if entry == "model":
        return service.change_model(
            session_id="sid", provider="mimo", model_id="mimo-v2.6-flash"
        )
    if entry == "permission":
        return service.change_permission_mode(
            session_id="sid", permission_mode="read-only", auto_approve=True
        )
    return service.task_acceptance(
        session_id="sid", decision="accepted", expected_version=0
    )


def _spy_on_orchestration_point(monkeypatch) -> list[tuple]:
    """把唯一的编排点换成记录调用的 spy，并委托真实实现。

    收敛前的树上本函数内的 ``SessionService._apply_write_with_conflict_retry``
    不存在 ⇒ ``monkeypatch.setattr`` 抛 ``AttributeError`` ⇒ 用例红。
    """
    real = SessionService._apply_write_with_conflict_retry
    calls: list[tuple] = []

    async def spy(self, session_id, existing, callback):
        calls.append((session_id, list(existing), callback))
        return await real(self, session_id, existing, callback)

    monkeypatch.setattr(SessionService, "_apply_write_with_conflict_retry", spy)
    return calls


class TestSingleOrchestrationPoint:
    """三条非 live 命令写路径都经同一个编排点，且都传当前快照 + 完整命令 callback。"""

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_entry_routes_through_one_orchestration_point(self, tmp_path, monkeypatch, entry):
        state = _state(tmp_path)
        _seed(state, entry)
        calls = _spy_on_orchestration_point(monkeypatch)

        result = asyncio.run(_call(entry, state))

        assert len(calls) == 1, f"{entry} 应恰经一次编排点，实际 {len(calls)}"
        session_id, existing, callback = calls[0]
        assert session_id == "sid"
        assert [e.seq for e in existing] == sorted(e.seq for e in existing)
        assert existing, "编排点必须收到调用方读好的当前快照（非空）"
        assert callable(callback), "第三个参数必须是「在给定 Session 上执行完整命令」的 callback"
        # 编排点返回的正是命令自己的领域结果，不是包装类型。
        expected = {"model": ModelChange, "permission": PermissionChange}.get(entry)
        if expected is not None:
            assert isinstance(result, expected), f"{entry} 返回 {type(result)}"


class TestConflictRetrySemantics:
    """一次可恢复冲突 ⇒ 重读快照并重跑整个 callback 后成功（逐入口）。"""

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_recoverable_conflict_rereads_and_succeeds(
        self, tmp_path, monkeypatch, entry
    ):
        state = _state(tmp_path)
        _seed(state, entry)
        real_append = state.store.append_event
        attempts: list[int] = []

        def conflict_once(session_id, event, *, allocate=False):
            attempts.append(1)
            if len(attempts) == 1:
                raise SeqConflict("注入：一次可恢复冲突")
            return real_append(session_id, event, allocate=allocate)

        monkeypatch.setattr(state.store, "append_event", conflict_once)

        asyncio.run(_call(entry, state))

        assert len(attempts) == 2, f"{entry} 应在一次冲突后重跑一次，实际 {len(attempts)}"
        seqs = [e.seq for e in state.store.read_events("sid")]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), seqs

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_persistent_conflict_is_bounded_and_surfaces(self, tmp_path, monkeypatch, entry):
        state = _state(tmp_path)
        _seed(state, entry)
        attempts: list[int] = []

        def always_conflict(session_id, event, *, allocate=False):
            attempts.append(1)
            raise SeqConflict("注入：持续冲突")

        monkeypatch.setattr(state.store, "append_event", always_conflict)

        with pytest.raises(SeqConflict):
            asyncio.run(_call(entry, state))

        assert len(attempts) == 3, f"{entry} 重试上界应为 3，实际 {len(attempts)}"


class TestLivePathIsNotRetried:
    """在途聚合由事件循环串行化、不可重建 ⇒ 编排点不在 live 分支额外重试。"""

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_live_append_conflict_propagates_without_retry(
        self, tmp_path, monkeypatch, entry
    ):
        state = _state(tmp_path)
        live = _seed(state, entry)
        state.run_manager.get_active = MagicMock(return_value=MagicMock(session=live))
        attempts: list[int] = []

        def always_conflict(session_id, event, *, allocate=False):
            attempts.append(1)
            raise SeqConflict("注入：live 聚合撞号")

        monkeypatch.setattr(state.store, "append_event", always_conflict)

        with pytest.raises(SeqConflict):
            asyncio.run(_call(entry, state))

        assert len(attempts) == 1, f"{entry} 的 live 分支不得重试，实际 {len(attempts)}"

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_live_path_writes_through_active_aggregate(self, tmp_path, monkeypatch, entry):
        """对照组：live 在场时命令落在在途聚合上，且编排点内部不读盘。

        计数窗口**只在编排点内部**：``task`` 入口成功后还会刷新进度文件、那里本就
        要重读事件流（W-05 触发点），把它算进来会把「编排点重读」和「进度刷新重读」
        混为一谈。
        """
        state = _state(tmp_path)
        live = _seed(state, entry)
        state.run_manager.get_active = MagicMock(return_value=MagicMock(session=live))
        real_read = state.store.read_events
        real_orchestrate = SessionService._apply_write_with_conflict_retry
        reads: list[str] = []
        inside = False

        def counting_read(session_id):
            if inside:
                reads.append(session_id)
            return real_read(session_id)

        async def spy(self, session_id, existing, callback):
            nonlocal inside
            inside = True
            try:
                return await real_orchestrate(self, session_id, existing, callback)
            finally:
                inside = False

        monkeypatch.setattr(state.store, "read_events", counting_read)
        monkeypatch.setattr(SessionService, "_apply_write_with_conflict_retry", spy)
        before = len(real_read("sid"))

        asyncio.run(_call(entry, state))

        assert len(real_read("sid")) == before + 1, f"{entry} 应恰追加一条事件"
        assert reads == [], f"{entry} live 分支不应重读快照，实际读了 {reads}"


_SERVICE_SOURCE = (
    Path(__file__).parents[2] / "src" / "agent_harness" / "session" / "service.py"
)


def _session_service_methods() -> dict[str, ast.AST]:
    tree = ast.parse(_SERVICE_SOURCE.read_text(encoding="utf-8"))
    service = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SessionService"
    )
    return {
        node.name: node
        for node in service.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_retry_loop_lives_only_in_the_orchestration_point() -> None:
    """重试循环（``attempts_left`` / ``_WRITE_CONFLICT_ATTEMPTS``）只许住在编排点里。

    这是「副本已删干净」的**结构**判据（同 ``tests/session/test_write_funnel.py``
    守「物理写点只能住在漏斗里」的手法）：某个入口若还留着自己的重试循环，它的
    方法体里必然重新出现这两个名字。行为面（重试次数 / 失败集合）由本文件其它
    用例承重，本守卫只钉「没有第二份循环」。
    """
    methods = _session_service_methods()
    helper = "_apply_write_with_conflict_retry"
    assert helper in methods, "编排点方法必须存在"

    def _retry_names(node: ast.AST) -> set[str]:
        return {
            sub.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Name)
            and sub.id in {"attempts_left", "_WRITE_CONFLICT_ATTEMPTS"}
        }

    offenders = {
        name for name, node in methods.items() if name != helper and _retry_names(node)
    }
    assert not offenders, f"重试循环副本残留在：{sorted(offenders)}"
    assert _retry_names(methods[helper]) == {
        "attempts_left",
        "_WRITE_CONFLICT_ATTEMPTS",
    }, "编排点自身必须持有重试循环"


class TestOrderContract:
    """票面「顺序铁律」的四条各自可判据，不靠「看起来一样」。"""

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_happy_path_uses_callers_snapshot_without_reread(
        self, tmp_path, monkeypatch, entry
    ):
        """非 live happy path：编排点用调用方读好的快照，自己不重读。

        变异「忽略 ``existing`` 参数、每轮重读」下本用例转红（编排点内部多一次读盘）。
        """
        state = _state(tmp_path)
        _seed(state, entry)
        real_read = state.store.read_events
        real_orchestrate = SessionService._apply_write_with_conflict_retry
        reads: list[str] = []
        inside = False

        def counting_read(session_id):
            if inside:
                reads.append(session_id)
            return real_read(session_id)

        async def spy(self, session_id, existing, callback):
            nonlocal inside
            inside = True
            try:
                return await real_orchestrate(self, session_id, existing, callback)
            finally:
                inside = False

        monkeypatch.setattr(state.store, "read_events", counting_read)
        monkeypatch.setattr(SessionService, "_apply_write_with_conflict_retry", spy)

        asyncio.run(_call(entry, state))

        assert reads == [], f"{entry} 非 live happy path 不应在编排点内重读，实际 {reads}"

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_retry_reruns_callback_on_a_fresh_snapshot(
        self, tmp_path, monkeypatch, entry
    ):
        """冲突后重跑 callback 用的是**重读的新快照**，不是旧快照。

        变异「删掉重读」/「沿用旧 existing 重跑」下本用例转红。
        """
        state = _state(tmp_path)
        _seed(state, entry)
        real_append = state.store.append_event
        real_orchestrate = SessionService._apply_write_with_conflict_retry
        injected: list[int] = []
        snapshots: list[list[int]] = []

        def conflict_after_a_concurrent_winner(session_id, event, *, allocate=False):
            if not injected:
                injected.append(1)
                # 并发写者抢先落盘同号（BUG-011 的机制本身），本写者随即撞号。
                real_append(session_id, event, allocate=allocate)
                raise SeqConflict("注入：并发写者抢先落盘同号")
            return real_append(session_id, event, allocate=allocate)

        async def spy(self, session_id, existing, callback):
            def recording(session):
                snapshots.append([e.seq for e in session.events])
                return callback(session)

            return await real_orchestrate(self, session_id, existing, recording)

        monkeypatch.setattr(
            state.store, "append_event", conflict_after_a_concurrent_winner
        )
        monkeypatch.setattr(SessionService, "_apply_write_with_conflict_retry", spy)

        asyncio.run(_call(entry, state))

        assert len(snapshots) == 2, f"{entry} 应在一次冲突后重跑一次，实际 {len(snapshots)}"
        assert snapshots[0] != snapshots[1], (
            f"{entry} 重试沿用了旧快照（未重读）：{snapshots}"
        )

    def test_live_aggregate_wins_over_empty_disk_snapshot(self, tmp_path, monkeypatch):
        """顺序第 1 条：每轮**先查 live**——磁盘快照为空但有在途聚合时命令仍成功。

        变异「把 live 检查挪到 ``if not existing`` 之后」（M6）下本用例转红（误报 404）。
        只有 ``_apply_task_handler`` 这条入口能把该顺序单独暴露出来：``change_model`` /
        ``change_permission_mode`` 在调用方侧另有一道首读 404，会先把空快照拦下。
        """
        state = _state(tmp_path)
        live = _seed(state, "task")
        state.run_manager.get_active = MagicMock(return_value=MagicMock(session=live))
        monkeypatch.setattr(state.store, "read_events", lambda session_id: [])

        outcome = asyncio.run(_call("task", state))

        assert outcome.ok, "live 聚合在场时不得因磁盘快照为空而报 404"

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_empty_snapshot_after_reread_is_not_found(
        self, tmp_path, monkeypatch, entry
    ):
        """重读后快照变空 ⇒ 404（会话在重试窗口内消失），不得写出孤立 seq=0 事件。

        变异「删掉 ``if not existing: raise SessionNotFound``」下本用例转红。
        """
        state = _state(tmp_path)
        _seed(state, entry)
        real_read = state.store.read_events
        real_append = state.store.append_event
        reads: list[int] = []
        injected: list[int] = []

        def read_then_empty(session_id):
            reads.append(1)
            if len(reads) == 1:  # 调用方的首读：会话存在
                return real_read(session_id)
            return []  # 重读：会话已被删

        def conflict_once(session_id, event, *, allocate=False):
            if not injected:
                injected.append(1)
                raise SeqConflict("注入：一次可恢复冲突")
            return real_append(session_id, event, allocate=allocate)

        monkeypatch.setattr(state.store, "read_events", read_then_empty)
        monkeypatch.setattr(state.store, "append_event", conflict_once)

        with pytest.raises(SessionNotFound):
            asyncio.run(_call(entry, state))


class TestReadCorruptionIsTerminal:
    """读时损坏（重复 / 回退 seq）是终态 409，不得被当成可重试的写时冲突。"""

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_corrupt_log_raises_without_retry(self, tmp_path, monkeypatch, entry):
        state = _state(tmp_path)
        _seed(state, entry)
        path = tmp_path / "sessions" / "sid" / "events.jsonl"
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(lines[-1] + "\n")  # 复制末行 = 重复 seq（历史损坏）

        writes: list[int] = []
        reads: list[int] = []
        real_append = state.store.append_event
        real_read = state.store.read_events

        def counting_append(session_id, event, *, allocate=False):
            writes.append(1)
            return real_append(session_id, event, allocate=allocate)

        def counting_read(session_id):
            reads.append(1)
            return real_read(session_id)

        monkeypatch.setattr(state.store, "append_event", counting_append)
        monkeypatch.setattr(state.store, "read_events", counting_read)

        with pytest.raises(SeqConflict):
            asyncio.run(_call(entry, state))

        assert writes == [], f"{entry} 在损坏日志上不得尝试写入（读时冲突是终态）"
        assert len(reads) == 1, (
            f"{entry} 读时损坏是终态、不得重读重试，实际读盘 {len(reads)} 次"
        )
