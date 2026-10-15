"""#1048 AC2/AC3：快照导入编排 + legacy PSEC 迁移 + 导入完成标记门。

红阶段（TDD）：当前完全没有导入编排——
- `registry.create` 的 docker 分支只建空 volume、不导入宿主目录（registry.py:107-113）；
- legacy PSEC → docker 的迁移代码为零；
- 无「导入完成标记」，无「未完成不可运行」门（AC3）。

本文件用替身 DockerSandbox + 探针替身，在**无 daemon** 下钉住编排契约。
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest

import agent_harness.sandbox.registry as registry_mod
from agent_harness.sandbox.capabilities import SandboxCapabilities
from agent_harness.sandbox.registry import WorkspaceRegistry


class FakeDockerSandbox:
    """记录 copy_in / 文件的最小替身，代表「容器内 /workspace」。"""

    def __init__(self, *, container_name=None, volume_name=None, image=None):
        self.container_name = container_name
        self.volume_name = volume_name
        self.files: dict[str, str] = {}
        self.copied: list[tuple[str, str]] = []
        self.started = False
        self.import_fails = False
        # #1132：恢复路径会先查具名卷是否仍在（缺失即 fail-closed，不重建空卷）。
        # 替身默认"卷在"，由用例显式翻转来钉住缺失那一支。
        self.volume_present = True
        self.ensure_started_calls = 0

    @property
    def workspace_root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    def ensure_started(self) -> None:
        self.started = True
        self.ensure_started_calls += 1

    def workspace_volume_exists(self) -> bool:
        return self.volume_present

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content

    def read_text(self, path: str) -> str:
        return self.files[path]

    def copy_in(self, host_path, workspace_path: str) -> None:
        if self.import_fails:
            raise RuntimeError("simulated import failure")
        self.copied.append((str(host_path), str(workspace_path)))


@pytest.fixture
def docker_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        registry_mod,
        "probe_docker_capabilities",
        lambda **_k: SandboxCapabilities(backend="docker", available=True),
    )
    monkeypatch.setattr(
        registry_mod,
        "require_available",
        lambda caps: None,
    )
    monkeypatch.setattr(
        "agent_harness.sandbox.docker.DockerSandbox",
        FakeDockerSandbox,
    )


def _write_legacy_psec_mapping(
    registry_root: Path, session_id: str, host_root: Path
) -> Path:
    ws_dir = registry_root / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    path = ws_dir / f"{session_id}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "backend": "windows_psec",
                "workspace_root": str(host_root),
                "workspace_identity": {"root": str(host_root)},
                "created_at": "2026-10-14T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return path


class TestLegacyPsecMigration:
    def test_legacy_psec_mapping_snapshot_migrates_to_docker(
        self, tmp_path: Path, docker_env
    ) -> None:
        """AC2：旧 PSEC 映射恢复 → 快照复制到私有 volume → 原子改 mapping → 返回 docker。"""
        host = tmp_path / "legacy_ws"
        host.mkdir()
        (host / "keep.txt").write_text("legacy-data", encoding="utf-8")

        registry = WorkspaceRegistry(root=tmp_path / "reg", backend="local")
        mapping_path = _write_legacy_psec_mapping(
            tmp_path / "reg", "legacy_migrate_1", host
        )

        sandbox = registry.get("legacy_migrate_1")

        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        assert mapping["backend"] == "docker"
        assert isinstance(sandbox, FakeDockerSandbox)
        assert sandbox.copied, "legacy 宿主目录未被导入到私有 volume"

    def test_legacy_psec_migration_failure_keeps_old_mapping_and_host_files(
        self, tmp_path: Path, docker_env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC2：迁移失败 → 抛错、旧 mapping 未改、宿主文件与 canary 未动、不返回半成品。"""
        host = tmp_path / "legacy_ws_fail"
        host.mkdir()
        (host / "keep.txt").write_text("legacy-data", encoding="utf-8")

        registry = WorkspaceRegistry(root=tmp_path / "reg2", backend="local")
        mapping_path = _write_legacy_psec_mapping(
            tmp_path / "reg2", "legacy_migrate_fail", host
        )

        real = FakeDockerSandbox
        migrated_attempts: list[str] = []

        def failing_factory(**kw):
            migrated_attempts.append(str(kw.get("volume_name")))
            sandbox = real(**kw)
            sandbox.import_fails = True
            return sandbox

        monkeypatch.setattr(
            "agent_harness.sandbox.docker.DockerSandbox", failing_factory
        )

        with pytest.raises(Exception):  # noqa: B017 — 迁移失败必须响亮
            registry.get("legacy_migrate_fail")

        # 迁移必须真的被尝试过（否则"旧 mapping 未改"是没走迁移的假绿）。
        assert migrated_attempts, "恢复 legacy PSEC 映射时未尝试迁移到私有 volume"
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        assert mapping["backend"] == "windows_psec"
        assert (host / "keep.txt").read_text(encoding="utf-8") == "legacy-data"


class TestImportCompletionGate:
    def test_new_docker_session_import_is_marked_complete(
        self, tmp_path: Path, docker_env
    ) -> None:
        """AC2/AC3：导入完成后写完成标记，会话才可运行。"""
        host = tmp_path / "ws_marker"
        host.mkdir()
        (host / "data.txt").write_text("payload", encoding="utf-8")

        registry = WorkspaceRegistry(root=tmp_path / "reg3", backend="docker")
        sandbox = registry.create("sess_marker", workspace_root=host)

        assert ".harness-import-complete" in sandbox.files

    def test_session_not_runnable_until_import_complete_marker(
        self, tmp_path: Path, docker_env
    ) -> None:
        """AC3：快照导入映射（`snapshot_import`）无完成标记时取 sandbox → 拒绝
        （不把半份当已初始化）。"""
        from agent_harness.sandbox.registry import WorkspaceImportIncompleteError

        registry = WorkspaceRegistry(root=tmp_path / "reg4", backend="docker")
        ws_dir = tmp_path / "reg4" / "workspaces"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "sess_half.json").write_text(
            json.dumps(
                {
                    "session_id": "sess_half",
                    "backend": "docker",
                    "workspace_root": str(tmp_path / "nowhere"),
                    "container_name": "agent-harness-sess_half",
                    "volume_name": "agent-harness-sess_half",
                    "snapshot_import": True,
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(WorkspaceImportIncompleteError):
            registry.get("sess_half")

    def test_pre_snapshot_docker_mapping_recovers_without_marker(
        self, tmp_path: Path, docker_env
    ) -> None:
        """#1048：pre-#1048 的 docker 映射（无 `snapshot_import` 字段、无完成标记）其
        volume 从未经过快照导入，恢复时**不得**被误判为半份导入而永久卡死。"""
        registry = WorkspaceRegistry(root=tmp_path / "reg5", backend="docker")
        ws_dir = tmp_path / "reg5" / "workspaces"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "sess_legacy.json").write_text(
            json.dumps(
                {
                    "session_id": "sess_legacy",
                    "backend": "docker",
                    "workspace_root": str(tmp_path / "nowhere"),
                    "container_name": "agent-harness-sess_legacy",
                    "volume_name": "agent-harness-sess_legacy",
                }
            ),
            encoding="utf-8",
        )

        sandbox = registry.get("sess_legacy")

        assert sandbox.volume_name == "agent-harness-sess_legacy"


class TestRestoreRefusesMissingVolume:
    """#1132：具名卷已不存在时，恢复必须 fail-closed，**绝不**重建空卷。

    动机（真 Docker 实测）：`containers.run(volumes=...)` 在具名卷缺失时会静默
    重建一个空卷，于是恢复"成功"在空 workspace 上、原文件不可达。
    """

    def _mapping(
        self, root: Path, session_id: str, *, snapshot_import: bool = True,
    ) -> Path:
        ws_dir = root / "workspaces"
        ws_dir.mkdir(parents=True, exist_ok=True)
        path = ws_dir / f"{session_id}.json"
        mapping = {
            "session_id": session_id,
            "backend": "docker",
            "workspace_root": str(root / "nowhere"),
            "container_name": f"agent-harness-{session_id}",
            "volume_name": f"agent-harness-{session_id}",
        }
        if snapshot_import:
            mapping["snapshot_import"] = True
        path.write_text(json.dumps(mapping), encoding="utf-8")
        return path

    def test_missing_volume_fails_closed_before_starting_the_container(
        self, tmp_path: Path, docker_env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agent_harness.sandbox.registry import WorkspaceBindingError

        registry = WorkspaceRegistry(root=tmp_path / "reg_vol", backend="docker")
        # 不带 snapshot_import：让本用例**只**能被「卷存在性检查」判红。
        # 带上它的话，未修复的树会先撞上 #1048 的导入完成标记门而失败——那是**另一个**
        # 闸门，红得不是本用例要钉的那件事（红证签名必须指向被测机制）。
        self._mapping(tmp_path / "reg_vol", "sess_novol", snapshot_import=False)

        created: list[FakeDockerSandbox] = []

        class MissingVolumeDockerSandbox(FakeDockerSandbox):
            """具名卷已被删（外部删除 / 硬删除）的替身。"""

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.volume_present = False
                created.append(self)

        # 必须是**类**而不是工厂函数：恢复路径里 `_assert_volume_present` 会对它做
        # `isinstance(..., DockerSandbox)`；打桩成函数会让 isinstance 直接 TypeError。
        # 又因为 `from agent_harness.sandbox.docker import DockerSandbox` 在**函数体内**
        # 解析 ⇒ 打桩**源模块**的属性（打 registry 模块的属性没有效果）。
        monkeypatch.setattr(
            "agent_harness.sandbox.docker.DockerSandbox", MissingVolumeDockerSandbox,
        )

        with pytest.raises(WorkspaceBindingError, match="持久卷不存在"):
            registry.get("sess_novol")

        assert created, "恢复路径没有构造 sandbox"
        assert created[0].ensure_started_calls == 0, (
            "卷缺失时**不得**调用 ensure_started——那会重建空卷"
        )

    def test_cached_sandbox_with_deleted_volume_fails_closed(
        self, tmp_path: Path, docker_env,
    ) -> None:
        """#1132（审阅 P1-B）：缓存命中的实例同样要过卷存在性闸门。

        `cleanup_docker_workspace_resources` 删掉卷后到 `discard_session_artifacts`
        摘映射之间，缓存仍持有那个 sandbox。直接把它交出去，调用方下一次
        `read_text`/`write_text`/`exec` 就会 `ensure_started()` 把**空卷**建回来。
        """
        from agent_harness.sandbox.registry import WorkspaceBindingError

        registry = WorkspaceRegistry(root=tmp_path / "reg_cache", backend="docker")
        sandbox = registry.create("sess_cached", workspace_root=tmp_path / "ws_cache")
        assert sandbox.ensure_started_calls == 1
        # 模拟硬删除已删掉容器与私有卷（映射此刻还在——删除顺序如此）。
        sandbox.volume_present = False

        with pytest.raises(WorkspaceBindingError, match="持久卷不存在"):
            registry.get("sess_cached")

        assert sandbox.ensure_started_calls == 1, (
            "卷已被删时不得再交还缓存实例——那会重建空卷"
        )

    def test_legacy_restore_uses_the_in_lock_mapping_and_never_resurrects_it(
        self, tmp_path: Path, docker_env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1132（审阅 P1-A）：legacy 分支也必须用**锁内重读**的映射。

        此前它用的是 `get()` 锁外读到的旧映射：等锁期间硬删除摘掉映射后，恢复照样
        `_migrate_legacy_mapping` → `ensure_started` → `_import_snapshot` →
        `_write_mapping`，把已删会话的**映射与容器/卷重建回来**（红证实测：
        `mapping RESURRECTED: True / container+volume RECREATED: True`）。
        """
        import threading

        host = tmp_path / "legacy_host"
        host.mkdir()
        (host / "keep.txt").write_text("legacy-data", encoding="utf-8")
        root = tmp_path / "reg_legacy"
        _write_legacy_psec_mapping(root, "leg_race", host)

        registry = WorkspaceRegistry(root=root, backend="local")
        created: list[FakeDockerSandbox] = []

        class RecordingDockerSandbox(FakeDockerSandbox):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                created.append(self)

        monkeypatch.setattr(
            "agent_harness.sandbox.docker.DockerSandbox", RecordingDockerSandbox,
        )

        # 让恢复线程在「已读到锁外映射、正等锁」处停住。
        original_lock = registry._workspace_lock
        restore_thread: list[threading.Thread] = []
        paused = threading.Event()
        release = threading.Event()
        paused_once = []

        def lock_hook(owner_session_id: str):
            if threading.current_thread() in restore_thread and not paused_once:
                paused_once.append(True)
                paused.set()
                assert release.wait(30), "test never released the cold restore"
            return original_lock(owner_session_id)

        monkeypatch.setattr(registry, "_workspace_lock", lock_hook)

        result: dict[str, object] = {}

        def restore() -> None:
            try:
                result["sandbox"] = registry.get("leg_race")
            except BaseException as exc:  # noqa: BLE001 — 传播工作线程失败
                result["error"] = exc

        thread = threading.Thread(target=restore, name="1132-legacy", daemon=True)
        restore_thread.append(thread)
        thread.start()
        assert paused.wait(30), "restore never reached the workspace lock"

        # 硬删除在恢复等锁期间跑完（映射被摘除）。
        registry.discard_session_artifacts("leg_race")
        assert not (root / "workspaces" / "leg_race.json").exists()

        release.set()
        thread.join(timeout=30)

        assert isinstance(result.get("error"), KeyError), result
        assert not (root / "workspaces" / "leg_race.json").exists(), (
            "恢复把已硬删会话的映射重建回来了"
        )
        assert not created, "恢复在映射已删后仍然构造了 sandbox（会重建容器与空卷）"

    def test_present_volume_recovers_normally(
        self, tmp_path: Path, docker_env,
    ) -> None:
        registry = WorkspaceRegistry(root=tmp_path / "reg_ok", backend="docker")
        # 不带 snapshot_import：本用例只钉「卷在 ⇒ 照常恢复」，导入完成标记门
        # 由上面 TestImportCompletionGate 的用例单独覆盖。
        self._mapping(tmp_path / "reg_ok", "sess_ok", snapshot_import=False)

        sandbox = registry.get("sess_ok")

        assert isinstance(sandbox, FakeDockerSandbox)
        assert sandbox.ensure_started_calls == 1
