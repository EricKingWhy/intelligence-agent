"""#1048 AC4：私有 Docker named volume + 副本导入切断 hard link 对象身份。

红阶段（TDD）：当前实现把工作区当宿主目录名授权，`DockerSandbox` 又只建空
volume、建会话时**不导入**宿主目录（registry.create docker 分支，
registry.py:107-113；docker.py:130-142）。因此：

- 指向 workspace 外的 hard link 在导入后仍与宿主源同一对象 → 容器内写会改宿主；
- 完全 workspace 内的 hard-link pair 无可验证的导入语义；
- 导入期间并发新增的外链可能被共享。

本文件用 Linux `os.link` 等价验证 NTFS hard link 的对象身份语义。
需要真实 Docker Engine；Engine 不可用时用例**真实失败**（不静默跳过）。
真实 Windows NTFS + MXC PSEC 部分如实登记未完成（见回报）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_harness.sandbox.capabilities import probe_docker_capabilities
from agent_harness.sandbox.registry import WorkspaceRegistry

SESSION_ID = "test-1048-hardlink-isolation"


def _require_docker_engine() -> None:
    caps = probe_docker_capabilities()
    if not caps.available:
        pytest.fail(
            f"AC4 需要真实 Docker Engine 验证对象身份隔离，当前不可用：{caps.reason}"
        )


def _registry(tmp_path: Path) -> WorkspaceRegistry:
    return WorkspaceRegistry(root=tmp_path / "registry", backend="docker")


@pytest.fixture(autouse=True)
def _clean_deterministic_volume():
    """用例前后清理确定性命名 volume/容器（#1048 AC4）。

    本文件用模块级常量 SESSION_ID ⇒ volume 名跨 run 确定；用例自身会向容器内
    写文件（如 late-alias.bin），残留会污染下一次 run 的"快照边界"断言（导入是
    增量 copy_in，不删旧文件）。CI 新环境恒过、共享 Engine 上跨 run 必红。
    用例前清（防上一次崩溃残留）、用例后清（不污染下一次），保证 volume 恒为
    pristine。
    """

    def _clean() -> None:
        import docker

        # Match production's client construction. The default context loader
        # decodes Docker Desktop metadata using the Windows locale encoding;
        # on Chinese Windows this can fail before stale test resources are
        # removed, and the old volume then contaminates the snapshot test.
        client = docker.from_env(use_context=False)
        try:
            try:
                client.containers.get(f"agent-harness-{SESSION_ID}").remove(
                    force=True
                )
            except docker.errors.NotFound:
                pass
            try:
                client.volumes.get(f"agent-harness-{SESSION_ID}").remove(
                    force=True
                )
            except docker.errors.NotFound:
                pass
        finally:
            client.close()

    _clean()
    yield
    _clean()


class TestNewSessionVolumeOnlyMounts:
    def test_new_session_mounts_only_private_volume_at_workspace(
        self, tmp_path: Path
    ) -> None:
        """AC1：新会话容器只挂每会话私有 named volume 到 /workspace(rw)。

        无宿主项目目录挂载、无 docker socket、无其他可写宿主路径。
        """
        _require_docker_engine()
        registry = _registry(tmp_path)
        sandbox = registry.create(SESSION_ID)
        sandbox.ensure_started()
        container = sandbox._container  # type: ignore[attr-defined]
        container.reload()
        mounts = container.attrs["Mounts"]
        assert mounts, "AC1：容器没有任何挂载（预期私有 volume）"
        for mount in mounts:
            assert mount["Destination"] == "/workspace"
            assert mount["Type"] == "volume"
            assert mount["RW"] is True
            assert "/var/run/docker.sock" not in str(mount.get("Source", ""))
        # 无宿主项目目录 bind。
        assert all(m["Type"] != "bind" for m in mounts)


class TestHardLinkObjectIdentityIsolation:
    def test_external_hardlink_guest_write_leaves_host_source_and_canary_unchanged(
        self, tmp_path: Path
    ) -> None:
        """AC4：容器内改写导入后的外链文件，宿主源文件与外部 canary 逐字节不变。"""
        _require_docker_engine()
        host_root = tmp_path / "host_ws"
        host_root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        canary = outside / "secret.bin"
        canary.write_bytes(b"EXTERNAL-CANARY-v1")

        # workspace 内一个名称 hard link 到 workspace 外文件（同 inode）。
        external_alias = host_root / "alias.bin"
        os.link(canary, external_alias)
        assert external_alias.stat().st_ino == canary.stat().st_ino

        registry = _registry(tmp_path)
        sandbox = registry.create(SESSION_ID, workspace_root=host_root)

        # 容器内改写别名。
        sandbox.write_text("alias.bin", "OVERWRITTEN-IN-CONTAINER")

        # 导入的副本必须与宿主源不同对象：宿主源与 canary 逐字节不变。
        assert canary.read_bytes() == b"EXTERNAL-CANARY-v1"
        assert external_alias.read_bytes() == b"EXTERNAL-CANARY-v1"

    def test_internal_hardlink_pair_remains_usable(self, tmp_path: Path) -> None:
        """AC4：完全 workspace 内的 hard-link pair 导入后仍可用（改其一二者一致）。

        内部 pair 不是攻击面（两名称都在 workspace 内），导入不得把它拆成
        两个互不相干的对象——那样会破坏用户数据。
        """
        _require_docker_engine()
        host_root = tmp_path / "host_ws_internal"
        host_root.mkdir()
        first = host_root / "a.txt"
        first.write_text("shared-content")
        second = host_root / "b.txt"
        os.link(first, second)
        assert first.stat().st_ino == second.stat().st_ino

        registry = _registry(tmp_path)
        sandbox = registry.create(SESSION_ID, workspace_root=host_root)

        sandbox.write_text("a.txt", "changed-in-container")
        assert sandbox.read_text("b.txt") == "changed-in-container"

    def test_external_hardlink_added_after_snapshot_is_not_shared(
        self, tmp_path: Path
    ) -> None:
        """AC4：快照边界**冻结**对象身份——导入完成后宿主工作区新增的名称（含外链）
        不进容器；容器内写同名文件不触达宿主源与外部 canary。

        导入在 `create()` 内同步完成，故「导入期间并发新增」的确定性等价形态是
        「快照已取定后新增」：其可验证判据是**容器看不到该名**（若后端是宿主 bind
        挂载/懒读，则该名可见、写它会改宿主 ⇒ 本用例失败）。这比在导入后新增再无脑
        写同名文件强：后者在不绑定挂载的副本实现下也恒真，测不出边界。
        """
        _require_docker_engine()
        host_root = tmp_path / "host_ws_late"
        host_root.mkdir()
        (host_root / "seed.txt").write_text("seed")
        outside = tmp_path / "outside_late"
        outside.mkdir()
        canary = outside / "late.bin"
        canary.write_bytes(b"LATE-EXTERNAL")

        registry = _registry(tmp_path)
        sandbox = registry.create(SESSION_ID, workspace_root=host_root)
        # create() 返回时快照已冻结；此后在宿主工作区新增硬链接别名。
        os.link(canary, host_root / "late-alias.bin")
        assert (host_root / "late-alias.bin").stat().st_ino == canary.stat().st_ino

        # 容器工作区有冻结快照，看不到导入后新增的名字。
        with pytest.raises(FileNotFoundError):
            sandbox.read_text("late-alias.bin")
        # 即便在容器内写同名文件，宿主源与外部 canary 逐字节不变。
        sandbox.write_text("late-alias.bin", "CONTAINER-WROTE")
        assert canary.read_bytes() == b"LATE-EXTERNAL"
        assert (host_root / "late-alias.bin").read_bytes() == b"LATE-EXTERNAL"
