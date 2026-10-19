"""Session ↔ WorkspaceRegistry 集成测试。

验证 Session.start/resume 通过 workspace_registry 绑定 Sandbox，
以及不传 registry 时的向后兼容性。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.sandbox import LocalSubprocessSandbox
from agent_harness.sandbox.paths import canonical_workspace_path
from agent_harness.sandbox.registry import WorkspaceBindingError, WorkspaceRegistry
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from tests.workspace_fixtures import rewrite_workspace_mapping


@pytest.fixture
def store_and_registry(tmp_path: Path):
    """返回 (JsonlSessionStore root, WorkspaceRegistry) 共享同一 root 目录。"""
    store = JsonlSessionStore(root=tmp_path)
    registry = WorkspaceRegistry(root=tmp_path, backend="local")
    return store, registry


class TestStartWithRegistry:
    def test_start_binds_sandbox(self, store_and_registry):
        """Session.start(registry) → session.sandbox 不是 None。"""
        store, registry = store_and_registry
        session = Session.start(store, workspace_registry=registry)

        assert session.sandbox is not None
        assert isinstance(session.sandbox, LocalSubprocessSandbox)
        assert Path(session.sandbox.workspace_root).exists()

    def test_start_without_registry_sandbox_is_none(self, tmp_path: Path):
        """不传 registry → session.sandbox 是 None（向后兼容）。"""
        store = JsonlSessionStore(root=tmp_path)
        session = Session.start(store)

        assert session.sandbox is None


class TestResumeWithRegistry:
    def test_resume_restores_sandbox(self, store_and_registry):
        """Session.resume(registry) → session.sandbox 恢复，workspace 文件还在。"""
        store, registry = store_and_registry

        # start → 写文件 → resume
        session1 = Session.start(store, workspace_registry=registry)
        session1.sandbox.write_text("data.txt", "important")
        session_id = session1.session_id

        # resume：新 Session 实例，同一 registry
        session2 = Session.resume(store, session_id, workspace_registry=registry)

        assert session2.sandbox is not None
        assert session2.sandbox.read_text("data.txt") == "important"

    def test_resume_without_registry_sandbox_is_none(
        self, store_and_registry, tmp_path: Path
    ):
        """resume 不传 registry → sandbox 是 None（向后兼容）。"""
        store, registry = store_and_registry
        session1 = Session.start(store, workspace_registry=registry)
        session_id = session1.session_id

        session2 = Session.resume(store, session_id)
        assert session2.sandbox is None

    def test_resume_across_simulated_restart(self, store_and_registry):
        """Session.resume 在模拟进程重启后（新 Registry 实例）仍能恢复 workspace。"""
        store, registry = store_and_registry

        session1 = Session.start(store, workspace_registry=registry)
        session1.sandbox.write_text("persist.py", "x = 42")
        session_id = session1.session_id

        # 模拟进程重启：新 Registry 实例指向同一个 root
        new_registry = WorkspaceRegistry(root=registry._root, backend="local")
        session2 = Session.resume(store, session_id, workspace_registry=new_registry)

        assert session2.sandbox is not None
        assert session2.sandbox.read_text("persist.py") == "x = 42"


class TestExistingBehaviorUnchanged:
    def test_start_without_registry_works_as_before(self, tmp_path: Path):
        """不传 registry 时 Session.start 行为完全不变。"""
        store = JsonlSessionStore(root=tmp_path)
        session = Session.start(store)

        assert session.sandbox is None
        assert len(session.events) >= 1  # session/started 事件已追加

    def test_resume_without_registry_works_as_before(self, tmp_path: Path):
        """不传 registry 时 Session.resume 行为完全不变。"""
        store = JsonlSessionStore(root=tmp_path)
        session1 = Session.start(store)
        session_id = session1.session_id

        session2 = Session.resume(store, session_id)
        assert session2.sandbox is None
        # session/resumed 事件已追加
        assert any(e.type == "session/resumed" for e in session2.events)


class TestLoadWithoutWorkspaceMapping:
    """#1235：映射缺席时 `Session.load` 降级为 `sandbox=None`，而不是抛 `KeyError`。

    可达形态：早于 WorkspaceRegistry 的历史会话、映射卷被清空、测试/手工构造的
    会话。写路径端点（workflow-mode / approvals/revoke / context-settings）只用
    `Session.load` 追加事件、从不碰 sandbox——一次缺席的映射不该把 200 变成 500。

    注册表自身的契约不变：`get()` 对缺席仍抛 `KeyError`
    （`tests/sandbox/test_workspace_registry.py::test_nonexistent_raises`）。
    """

    def test_load_returns_none_sandbox_when_mapping_absent(self, store_and_registry):
        store, registry = store_and_registry
        session = Session.start(store)  # 故意不建映射
        session_id = session.session_id

        # 测试前提：映射确实缺席，且 registry.get 仍按契约抛 KeyError。
        with pytest.raises(KeyError):
            registry.get(session_id)

        loaded = Session.load(store, session_id, workspace_registry=registry)

        assert loaded.sandbox is None
        assert [e.type for e in loaded.events] == [e.type for e in session.events]

    def test_resume_with_absent_mapping_still_works(self, store_and_registry):
        """`resume` 委托 `load`：映射缺席不应阻断恢复本身。"""
        store, registry = store_and_registry
        session_id = Session.start(store).session_id

        resumed = Session.resume(store, session_id, workspace_registry=registry)

        assert resumed.sandbox is None
        assert any(e.type == "session/resumed" for e in resumed.events)

    def test_binding_error_still_propagates(self, store_and_registry):
        """只降级「没有映射」；硬删除进行中是「映射此刻不可用」，必须继续响亮失败。"""
        store, registry = store_and_registry
        session_id = Session.start(store).session_id
        registry._write_mapping(
            session_id, {"session_id": session_id, "workspace_delete_in_progress": True}
        )

        with pytest.raises(WorkspaceBindingError, match="hard-delete"):
            Session.load(store, session_id, workspace_registry=registry)


class TestRecordedWorkspaceRoots:
    """#266：注册表把「它登记的会话工作目录」只读暴露给会话层对账。

    对账需要**两侧事实**：会话层拿 durable cwd，注册表拿 mapping + cache。只读访问器
    的意义在于它不实例化 Sandbox——`get()`/`create()` 都会 mkdir 工作目录，对一个
    已被用户删掉的外部目录，那等于把它凭空复活后再宣布"一切正常"。
    """

    def test_create_honors_explicit_workspace_root(self, tmp_path: Path):
        """显式 `workspace_root` 是**断言**：新建沙箱必须真的落在该目录。

        变异「`create()` 忽略 workspace_root」（把 requested 换回 mapping 的默认值）
        ⇒ 本用例必红：沙箱与映射都会变成 `<root>/workspaces/<sid>`。
        """
        registry = WorkspaceRegistry(root=tmp_path, backend="local")
        external = tmp_path / "external"
        external.mkdir()

        sandbox = registry.create("sid", workspace_root=external)

        expected = canonical_workspace_path(external)
        assert str(sandbox.workspace_root) == expected
        mapping = json.loads(
            (tmp_path / "workspaces" / "sid.json").read_text(encoding="utf-8")
        )
        assert mapping["workspace_root"] == expected

    def test_no_records_is_an_empty_list(self, tmp_path: Path):
        """无映射、无 cache → 空列表（"没登记"与"登记在别处"必须可区分）。"""
        registry = WorkspaceRegistry(root=tmp_path, backend="local")

        assert registry.recorded_workspace_roots("nobody") == []

    def test_records_cover_mapping_and_cache(self, tmp_path: Path):
        """两条记录都在：映射被改指别处后，cache 里那份仍然被列出来。

        这就是"两个都要对账"的理由：只看映射会漏掉 cache 里那个真正会被 runtime
        使用的目录，只看 cache 会漏掉映射（进程重启后它才是事实）。
        """
        external = tmp_path / "external"
        external.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        registry = WorkspaceRegistry(root=tmp_path, backend="local")
        registry.create("sid", workspace_root=external)

        # 同值时去重：cache 与映射是同一条事实，不必报两遍。
        assert registry.recorded_workspace_roots("sid") == [
            canonical_workspace_path(external)
        ]

        rewrite_workspace_mapping(tmp_path / "workspaces", "sid", other)

        assert registry.recorded_workspace_roots("sid") == [
            str(other),
            canonical_workspace_path(external),
        ]
        # 模拟进程重启：新实例 cache 为空，只剩映射文件这条事实。
        restarted = WorkspaceRegistry(root=tmp_path, backend="local")
        assert restarted.recorded_workspace_roots("sid") == [str(other)]

    def test_reading_records_never_creates_the_directory(self, tmp_path: Path):
        """只读访问器不得 mkdir：已删除的外部目录读完后仍然不存在。

        对照组：`get()` 会把同一个目录建回来（LocalSubprocessSandbox 构造时 mkdir），
        这正是"外部 cwd 被删后静默复活"的机制。
        """
        registry = WorkspaceRegistry(root=tmp_path, backend="local")
        external = tmp_path / "external"
        external.mkdir()
        registry.create("sid", workspace_root=external)
        external.rmdir()

        assert registry.recorded_workspace_roots("sid") == [
            canonical_workspace_path(external)
        ]
        assert not external.exists()
