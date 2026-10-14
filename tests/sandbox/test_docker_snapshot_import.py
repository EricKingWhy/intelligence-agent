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

    @property
    def workspace_root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    def ensure_started(self) -> None:
        self.started = True

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
