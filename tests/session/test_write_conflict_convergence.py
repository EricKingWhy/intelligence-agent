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
``AttributeError``（逐入口各一条，见 ``TestSingleOrchestrationPoint``）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_harness.config import Settings
from agent_harness.session.approval import PermissionChange
from agent_harness.session.errors import SeqConflict
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

    @pytest.mark.parametrize("entry", ENTRIES)
    def test_no_duplicate_retry_loop_left_behind(self, tmp_path, monkeypatch, entry):
        """编排点被替换成「只跑一次 callback」时命令仍成功——证明副本已删干净。

        若某个入口还留着自己的重试循环，它会在编排点之外再写一次（事件条数 +2）。
        """
        state = _state(tmp_path)
        _seed(state, entry)
        before = len(state.store.read_events("sid"))

        async def single_shot(self, session_id, existing, callback):
            return callback(Session(session_id, self._store, existing))

        monkeypatch.setattr(
            SessionService, "_apply_write_with_conflict_retry", single_shot
        )

        asyncio.run(_call(entry, state))

        after = state.store.read_events("sid")
        assert len(after) == before + 1, (
            f"{entry} 在编排点之外又写了一次：{before} -> {len(after)}"
        )


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
        real_append = state.store.append_event

        def counting_append(session_id, event, *, allocate=False):
            writes.append(1)
            return real_append(session_id, event, allocate=allocate)

        monkeypatch.setattr(state.store, "append_event", counting_append)

        with pytest.raises(SeqConflict):
            asyncio.run(_call(entry, state))

        assert writes == [], f"{entry} 在损坏日志上不得尝试写入（读时冲突是终态）"
