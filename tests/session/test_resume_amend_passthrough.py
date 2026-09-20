"""Q2 spec tests: amend 字段透传（service 层）。

spec 要求（docs/TECH_DEBT_FIX_SPEC.md §Q2）：
  - 新增测试：验证 ``resume_and_launch`` 透传 amend 字段到 ``build_runtime``
    （mock build_runtime，断言参数传递）
  - 新增测试：验证 ``send_message`` idle 分支带 amend 字段时正确透传

端点层（``POST /api/sessions/{id}/resume`` 请求体 → AmendOptions）见
tests/web/test_web_amend_passthrough.py。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_harness.config import Settings
from agent_harness.sandbox.paths import canonical_workspace_path
from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session.cwd import session_cwd
from agent_harness.session.errors import WorkspaceBindingConflict, WorkspaceNotFound
from agent_harness.session.event import SESSION_FORKED, USER_MESSAGE
from agent_harness.session.service import AmendOptions, SessionService
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.web.app import session_service
from tests.workspace_fixtures import rewrite_workspace_mapping


def _make_state(tmp_path):
    """构造最小 AppState mock。"""
    settings = Settings(
        workspace_dir=str(tmp_path),
        model_api_key="test-key",
        model_name="test-model",
        model_provider="openai",
    )
    state = MagicMock()
    state.settings = settings
    state.store = MagicMock()
    state.run_manager = MagicMock()
    # launch 返回 (run, subscriber) tuple
    fake_run = MagicMock()
    fake_sub = MagicMock()
    state.run_manager.launch = MagicMock(return_value=(fake_run, fake_sub))
    state.workspace_registry = MagicMock()
    state.workspaces_root = tmp_path
    state.get_wiring = AsyncMock(return_value=(MagicMock(), MagicMock()))
    state.ensure_stores = AsyncMock()
    state.stores = MagicMock()
    return state


class TestResumeWorkspace:
    """续聊 runtime 必须沿用会话创建时不可变的 cwd 锚，并与沙箱映射机械对账（#266）。

    本类用**真实** ``WorkspaceRegistry`` + 真实 ``JsonlSessionStore``：映射/缓存与
    durable cwd 的对账正是本票要证明的东西，而 MagicMock 的 ``get()`` 不 mkdir、
    ``create()`` 也不命中缓存——两侧的失真恰好都落在被测点上（#266 Red/green
    evidence：禁止只 mock ``build_runtime(workspace=...)`` 参数）。
    """

    def _real_state(self, tmp_path):
        state = _make_state(tmp_path)
        state.store = JsonlSessionStore(root=tmp_path / "sessions")
        # 与 AppState 同构：workspaces_root == <workspace_dir>/workspaces，注册表根
        # 也是 <workspace_dir>（映射文件就写在 workspaces_root 下）。
        state.workspaces_root = tmp_path / "workspaces"
        state.workspaces_root.mkdir(parents=True, exist_ok=True)
        state.workspace_registry = WorkspaceRegistry(root=tmp_path)
        state.message_queues = MagicMock()
        state.approval_queues = {}
        state.run_manager.get_active = MagicMock(return_value=None)
        return state

    @staticmethod
    def _start_with_cwd(state, session_id, cwd):
        """按**创建路径的真实顺序**落一条带 cwd 的会话。

        `create_and_launch` 里 `build_runtime`（→ `create(workspace_root=...)`）先于
        `Session.start`，所以映射与 cwd 锚同值；顺序反过来（先 start 再 create）会
        得到"映射=默认目录 + cwd=外部目录"的 fork 形状，那是另一回事。
        """
        state.workspace_registry.create(session_id, workspace_root=cwd)
        return Session.start(
            state.store, session_id=session_id, cwd=cwd,
            workspace_registry=state.workspace_registry,
        )

    @staticmethod
    def _resume(state, session_id):
        with patch(
            "agent_harness.session.service.build_runtime", new_callable=AsyncMock,
        ) as mock_build:
            asyncio.run(
                session_service(state).resume_and_launch(
                    session_id=session_id, task="hello",
                )
            )
        return mock_build

    def test_resume_uses_persisted_external_cwd(self, tmp_path):
        state = self._real_state(tmp_path)
        external = tmp_path / "external-project"
        external.mkdir()
        self._start_with_cwd(state, "test-sid", external)

        mock_build = self._resume(state, "test-sid")

        persisted = session_cwd(state.store.read_events("test-sid"))
        assert persisted is not None
        assert mock_build.call_args.kwargs["workspace"] == external.resolve()
        assert session_cwd(state.store.read_events("test-sid")) == persisted
        assert len([
            event for event in state.store.read_events("test-sid")
            if event.type == "session/started"
        ]) == 1

    def test_missing_persisted_cwd_fails_without_recreating_it(self, tmp_path):
        """durable 外部 cwd 被删 → 类型化失败，且**不得**把它凭空建回来。

        真 registry 下这条曾经是绿的假象：`Session.resume` → `registry.get()` →
        `LocalSubprocessSandbox.__init__` 无条件 mkdir，目录被复活后服务层的
        `is_dir()` 检查自然通过（旧用例用 MagicMock registry，正好绕开了 mkdir）。
        """
        state = self._real_state(tmp_path)
        external = tmp_path / "removed-project"
        external.mkdir()
        self._start_with_cwd(state, "test-sid", external)
        external.rmdir()
        # 进程重启后的形态（cache 空、只剩映射文件）才是这条路径的真实场景：
        # 目录是在服务没跑的时候被删掉的。cache 命中时不会重建目录，测不到缺口。
        state.workspace_registry = WorkspaceRegistry(root=tmp_path)

        with pytest.raises(WorkspaceNotFound, match="cwd 不存在或不是目录"):
            self._resume(state, "test-sid")

        assert not external.exists()

    def test_legacy_session_without_cwd_keeps_default_workspace(self, tmp_path):
        """历史遗留（无 cwd 锚）：兼容语义逐字不变——用默认目录，且可续聊。"""
        state = self._real_state(tmp_path)
        state.workspace_registry.create("test-sid")
        Session.start(state.store, session_id="test-sid")

        mock_build = self._resume(state, "test-sid")

        expected = state.workspaces_root / "test-sid"
        assert mock_build.call_args.kwargs["workspace"] == expected
        assert expected.is_dir()

    def test_mapping_conflict_fails_typed(self, tmp_path):
        """mapping=B、durable cwd=A（**新 Registry 实例**：cache 空）→ 类型化冲突。

        旧行为：`Session.resume` → `get()` 读 mapping B 并缓存 → `build_runtime` →
        `create(workspace_root=A)` 命中 cache 直接返回 B —— run 静默落在 B。
        """
        state = self._real_state(tmp_path)
        cwd = tmp_path / "project-a"
        cwd.mkdir()
        other = tmp_path / "project-b"
        other.mkdir()
        self._start_with_cwd(state, "test-sid", cwd)
        rewrite_workspace_mapping(state.workspaces_root, "test-sid", other)
        state.workspace_registry = WorkspaceRegistry(root=tmp_path)  # 新实例，cache 空

        with pytest.raises(WorkspaceBindingConflict, match="工作目录绑定冲突"):
            self._resume(state, "test-sid")

        assert self._started_count(state, "test-sid") == 1
        assert not (state.workspaces_root / "test-sid").exists()

    def test_conflict_is_detected_before_any_sandbox_instantiation(self, tmp_path):
        """对账必须在**任何 Sandbox 实例化之前**（#266 的核心性质，不只是"会拒绝"）。

        漂移目标是**不存在**的目录：`get()` / `create()` 构造 Sandbox 时会 mkdir，
        所以对账一旦被挪到 `Session.resume`（那一步会 `registry.get()`）之后，这个
        目录就会被凭空建出来——正是票面要杜绝的"先建回来、再宣布一切正常"。

        上面几条冲突用例的漂移目标都是**已存在**的目录，mkdir 是 no-op：把对账点挪到
        后面它们照样全绿（审查实测），钉不住这条性质，所以这条用幽灵目录单独钉。
        """
        state = self._real_state(tmp_path)
        cwd = tmp_path / "project-a"
        cwd.mkdir()
        ghost = tmp_path / "project-b"  # 故意不创建：任何 mkdir 都会留下痕迹
        self._start_with_cwd(state, "test-sid", cwd)
        rewrite_workspace_mapping(state.workspaces_root, "test-sid", ghost)
        state.workspace_registry = WorkspaceRegistry(root=tmp_path)  # 新实例，cache 空

        with pytest.raises(WorkspaceBindingConflict, match="工作目录绑定冲突"):
            self._resume(state, "test-sid")

        assert not ghost.exists(), "对账之前不得实例化 Sandbox（会 mkdir 漂移目标）"
        assert self._started_count(state, "test-sid") == 1

    def test_cached_sandbox_conflict_fails_typed(self, tmp_path):
        """**进程内 cache 命中**路径：cache=B（mapping 后来改回 A、cwd=A）→ 冲突。

        与上一条的区别是事实来源：这里对账必须看 cache（`get()` 读过的映射被
        改回后，cache 里仍是 B），只读映射文件会得出"两侧一致"的错误结论。
        """
        state = self._real_state(tmp_path)
        cwd = tmp_path / "project-a"
        cwd.mkdir()
        other = tmp_path / "project-b"
        other.mkdir()
        self._start_with_cwd(state, "test-sid", cwd)
        rewrite_workspace_mapping(state.workspaces_root, "test-sid", other)
        # 进程重启后的形态：新实例从映射读到 B 并把它装进 cache（此后 cache=B）。
        restarted = WorkspaceRegistry(root=tmp_path)
        restarted.get("test-sid")
        state.workspace_registry = restarted
        rewrite_workspace_mapping(state.workspaces_root, "test-sid", cwd)  # 映射改回 A：cache 成为唯一异见

        with pytest.raises(WorkspaceBindingConflict, match="工作目录绑定冲突"):
            self._resume(state, "test-sid")

        assert self._started_count(state, "test-sid") == 1

    def test_drifted_mapping_fails_even_when_cache_agrees_with_cwd(self, tmp_path):
        """映射指向别处、cache 与 durable cwd 一致 → 仍然冲突（两条记录都要对账）。

        cache 一致只说明**本次**运行会落在正确目录；映射是持久事实，进程重启后它是
        唯一记录——那时才拒绝等于把问题推迟到下一次启动，而用户已经以为它好了。
        """
        state = self._real_state(tmp_path)
        cwd = tmp_path / "project-a"
        cwd.mkdir()
        other = tmp_path / "project-b"
        other.mkdir()
        self._start_with_cwd(state, "test-sid", cwd)
        rewrite_workspace_mapping(state.workspaces_root, "test-sid", other)  # cache 仍是 cwd

        with pytest.raises(WorkspaceBindingConflict, match="工作目录绑定冲突"):
            self._resume(state, "test-sid")

        assert self._started_count(state, "test-sid") == 1

    def test_fork_child_copy_on_fork_mapping_is_not_a_conflict(self, tmp_path):
        """fork 子会话的 copy-on-fork 目录 ≠ cwd 锚（父的项目目录）是**设计如此**。

        cwd 锚记的是项目归属、mapping 记的是子会话自己那份副本（ADR-0017 决策 5 /
        `test_session_cwd.py::TestForkInheritance` 的语义边界）；把它判成冲突会让
        每个 fork 子会话再也无法续聊——真机上 3 条 fork 子会话正是这个形状。
        """
        from agent_harness.session.fork import fork_session
        from agent_harness.storage.sqlite import SqliteSessionMetaStore

        state = self._real_state(tmp_path)
        project = tmp_path / "project-a"
        project.mkdir()
        parent = self._start_with_cwd(state, "parent", project)
        parent.append(USER_MESSAGE, {"content": "分叉点"})

        async def _fork():
            meta = SqliteSessionMetaStore(tmp_path / "harness.db")
            await meta.initialize()
            return await fork_session(
                state.store, meta, "parent",
                boundary_user_message_seq=parent.events[-1].seq,
                workspace_registry=state.workspace_registry,
                with_tail_summary=False,
            )

        child = asyncio.run(_fork())

        assert session_cwd(state.store.read_events(child.session_id)) == (
            canonical_workspace_path(project)
        )
        recorded = state.workspace_registry.recorded_workspace_roots(child.session_id)
        assert recorded == [str(state.workspaces_root / child.session_id)]
        assert SESSION_FORKED in [e.type for e in child.events]

        mock_build = self._resume(state, child.session_id)

        assert mock_build.call_args.kwargs["workspace"] == project.resolve()

    @staticmethod
    def _started_count(state, session_id):
        return len([
            event for event in state.store.read_events(session_id)
            if event.type == "session/started"
        ])


class TestResumeAndLaunchPassthrough:
    """``resume_and_launch`` 透传 amend 字段到 ``build_runtime``。"""

    def test_resume_and_launch_passes_amend_fields(self, tmp_path):
        """reasoning_effort / agent_profile / context_providers / model
        全部透传给 build_runtime。"""
        state = _make_state(tmp_path)

        # store.read_events 返回非空列表 → session 存在
        from agent_harness.session.event import SESSION_STARTED, SessionEvent

        existing_event = SessionEvent(
            seq=0,
            type=SESSION_STARTED,
            session_id="test-sid",
            data={},
        )
        state.store.read_events = MagicMock(return_value=[existing_event])
        state.run_manager.get_active = MagicMock(return_value=None)

        # Session.resume 需要 store + workspace_registry
        from agent_harness.sandbox.base import Sandbox

        sandbox = MagicMock(spec=Sandbox)
        state.workspace_registry.get = MagicMock(return_value=sandbox)

        with (
            patch(
                "agent_harness.session.service.build_runtime",
                new_callable=AsyncMock,
            ) as mock_build,
            patch("agent_harness.session.service.Session") as mock_session_cls,
        ):
            mock_session = MagicMock()
            mock_session.session_id = "test-sid"
            mock_session_cls.resume = MagicMock(return_value=mock_session)

            service = session_service(state)
            asyncio.run(
                service.resume_and_launch(
                    session_id="test-sid",
                    task="hello",
                    max_steps=5,
                    amend=AmendOptions(
                        reasoning_effort="deep",
                        agent_profile="coding",
                        context_providers=["memory"],
                        model="gpt-4o",
                    ),
                )
            )

        # 断言 build_runtime 收到了全部 amend 字段
        call_kwargs = mock_build.call_args.kwargs
        assert call_kwargs["model_name"] == "gpt-4o"
        assert call_kwargs["reasoning_effort"] == "deep"
        assert call_kwargs["agent_profile"] == "coding"
        assert call_kwargs["context_providers"] == ["memory"]

    def test_resume_and_launch_defaults_none(self, tmp_path):
        """不传 amend 字段时，build_runtime 收到 None（当前行为不变）。"""
        state = _make_state(tmp_path)

        from agent_harness.session.event import SESSION_STARTED, SessionEvent

        existing_event = SessionEvent(
            seq=0,
            type=SESSION_STARTED,
            session_id="test-sid",
            data={},
        )
        state.store.read_events = MagicMock(return_value=[existing_event])
        state.run_manager.get_active = MagicMock(return_value=None)

        from agent_harness.sandbox.base import Sandbox

        sandbox = MagicMock(spec=Sandbox)
        state.workspace_registry.get = MagicMock(return_value=sandbox)

        with (
            patch(
                "agent_harness.session.service.build_runtime",
                new_callable=AsyncMock,
            ) as mock_build,
            patch("agent_harness.session.service.Session") as mock_session_cls,
        ):
            mock_session = MagicMock()
            mock_session.session_id = "test-sid"
            mock_session_cls.resume = MagicMock(return_value=mock_session)

            service = session_service(state)
            asyncio.run(
                service.resume_and_launch(
                    session_id="test-sid",
                    task="hello",
                    max_steps=5,
                )
            )

        call_kwargs = mock_build.call_args.kwargs
        assert call_kwargs["model_name"] is None
        assert call_kwargs["reasoning_effort"] is None
        assert call_kwargs["agent_profile"] is None
        assert call_kwargs["context_providers"] is None


class TestSendMessageIdlePassthrough:
    """``send_message`` idle 分支把 amend 透传给 ``resume_and_launch``。"""

    def test_send_message_idle_forwards_amend(self, tmp_path):
        """idle（无在途 run）→ resume_and_launch 收到完整 amend 对象。"""
        state = _make_state(tmp_path)
        state.run_manager.get_active = MagicMock(return_value=None)

        amend = AmendOptions(
            reasoning_effort="deep",
            agent_profile="coding",
            context_providers=["memory"],
            model="gpt-4o",
        )
        launched = MagicMock()
        launched.session.session_id = "test-sid"
        launched.run = MagicMock()
        launched.subscriber = MagicMock()

        service = session_service(state)
        with (
            patch.object(
                SessionService, "has_session", new_callable=AsyncMock
            ) as mock_has,
            patch.object(
                SessionService, "resume_and_launch", new_callable=AsyncMock
            ) as mock_resume,
        ):
            mock_has.return_value = True
            mock_resume.return_value = launched
            result = asyncio.run(
                service.send_message(
                    session_id="test-sid",
                    content="继续",
                    mode="queue",
                    amend=amend,
                )
            )

        assert result.status == "launched"
        assert mock_resume.call_args.kwargs["task"] == "继续"
        assert mock_resume.call_args.kwargs["amend"] is amend
