"""Fork workspace 纯整拷的 5 类隔离/失效测试（#527，IMP-05 裁决 A）。

裁决 2026-10-05（选项 A）：一期 fork 工作区复制 = 纯整拷，禁止 CoW / hardlink /
robocopy /CREATE 冒充；5 类测试：① child 原地写不改 parent；② 删除 parent 后
child 独立；③ 跨卷；④ kill/中断不留半成品；⑤ 盘满明确失败。

与 #555 的 test_fork_copy_failure_compensates_and_leaves_marked_child 互补：
彼测 durable 可见性（意图标记、映射保留），此测纯整拷中断语义（child 根目录零写入）。
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from agent_harness.sandbox.registry import WorkspaceRegistry
from agent_harness.session.event import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    RUN_STARTED,
    USER_MESSAGE,
)
from agent_harness.session.fork import fork_session
from agent_harness.session.session import Session
from agent_harness.storage.sqlite import SqliteSessionMetaStore


def _store(tmp_path):
    from agent_harness.session.store import JsonlSessionStore

    return JsonlSessionStore(tmp_path / "sessions")


async def _meta(tmp_path) -> SqliteSessionMetaStore:
    meta = SqliteSessionMetaStore(tmp_path / "harness.db")
    await meta.initialize()
    return meta


def _registry(tmp_path) -> WorkspaceRegistry:
    return WorkspaceRegistry(root=tmp_path / "sandbox", backend="local")


def _build_parent_with_assets(store, registry: WorkspaceRegistry) -> Session:
    """两轮对话 + 带资产的父会话（workspace 写两个文件，含子目录）。"""
    parent = Session.start(store, session_id="parent", workspace_registry=registry)
    parent.append(USER_MESSAGE, {"content": "第一条"})
    parent.append(RUN_STARTED, {})
    parent.append(MODEL_COMPLETED, {"content": "好的"})
    parent.append(RUN_COMPLETED, {})
    parent.append(USER_MESSAGE, {"content": "第二条"})
    root = registry.default_workspace_root("parent")
    (root / "keep.txt").write_text("父资产", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "nested.bin").write_bytes(b"\x00\x01\x02nested")
    return parent


def _snapshot(root: Path) -> dict[str, bytes]:
    """目录逐字节快照（相对路径 → 内容）。"""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ── ① child 原地写（改/增/删）不改 parent ─────────────────────────────────


@pytest.mark.asyncio
async def test_child_writes_do_not_touch_parent(tmp_path) -> None:
    """child 在副本上改内容 / 新增 / 删除，parent 逐字节不变。"""
    store = _store(tmp_path)
    meta = await _meta(tmp_path)
    registry = _registry(tmp_path)
    parent = _build_parent_with_assets(store, registry)
    parent_snapshot = _snapshot(registry.default_workspace_root("parent"))

    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=parent.events[-1].seq,
        child_session_id="child", workspace_registry=registry,
    )
    child_root = registry.default_workspace_root(child.session_id)

    # 改内容
    (child_root / "keep.txt").write_text("child 改过", encoding="utf-8")
    # 新增
    (child_root / "new.txt").write_text("child 新增", encoding="utf-8")
    # 删除
    (child_root / "sub" / "nested.bin").unlink()
    # child 侧观察到自己写的状态
    assert (child_root / "keep.txt").read_text(encoding="utf-8") == "child 改过"
    assert (child_root / "new.txt").exists()
    assert not (child_root / "sub" / "nested.bin").exists()

    # parent 逐字节不变
    assert _snapshot(registry.default_workspace_root("parent")) == parent_snapshot


# ── ② 删除 parent 根目录后 child 仍完整独立可读 ────────────────────────────


@pytest.mark.asyncio
async def test_child_survives_parent_workspace_deletion(tmp_path) -> None:
    store = _store(tmp_path)
    meta = await _meta(tmp_path)
    registry = _registry(tmp_path)
    parent = _build_parent_with_assets(store, registry)
    expected = _snapshot(registry.default_workspace_root("parent"))

    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=parent.events[-1].seq,
        child_session_id="child", workspace_registry=registry,
    )

    shutil.rmtree(registry.default_workspace_root("parent"))
    child_root = registry.default_workspace_root(child.session_id)
    assert _snapshot(child_root) == expected


# ── ③ 跨卷：staging 与目标不在同卷时降级为纯整拷 ───────────────────────────


@pytest.mark.asyncio
async def test_cross_volume_replace_falls_back_to_full_copy(tmp_path, monkeypatch) -> None:
    """`os.replace` 抛 EXDEV（跨卷）时降级为直接 copytree（裁决 A 的等价 API）。

    纯整拷不引入 CoW / hardlink；child 拿到父的完整副本，暂存回收。
    """
    store = _store(tmp_path)
    meta = await _meta(tmp_path)
    registry = _registry(tmp_path)
    parent = _build_parent_with_assets(store, registry)
    expected = _snapshot(registry.default_workspace_root("parent"))

    real_replace = os.replace
    exdev_hits: list[str] = []

    def _cross_volume_replace(src, dst):
        # 只对 fork 暂存目录的发布模拟跨卷；registry 内部的原子写不受影响
        if ".fork-tmp" in str(src):
            exdev_hits.append(str(src))
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(src, dst)

    monkeypatch.setattr("os.replace", _cross_volume_replace)

    child = await fork_session(
        store, meta, "parent",
        boundary_user_message_seq=parent.events[-1].seq,
        child_session_id="child", workspace_registry=registry,
    )

    # fail-closed：EXDEV 降级分支必须真实走过，否则测试空转
    assert exdev_hits, "EXDEV 未触发，降级分支未被演练"
    child_root = registry.default_workspace_root(child.session_id)
    assert _snapshot(child_root) == expected
    staging = registry.fork_staging_root()
    assert not staging.exists() or not any(staging.iterdir())


# ── ④ 中断不留半成品 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_interrupted_copy_leaves_no_partial_workspace(tmp_path, monkeypatch) -> None:
    """拷贝中途异常：child 根目录无半份拷贝、暂存已回收、异常上抛。"""
    store = _store(tmp_path)
    meta = await _meta(tmp_path)
    registry = _registry(tmp_path)
    parent = _build_parent_with_assets(store, registry)
    parent_snapshot = _snapshot(registry.default_workspace_root("parent"))

    calls: list[str] = []

    real_copytree = shutil.copytree

    def _interrupted_copy(src, dst, **kwargs):
        calls.append(str(dst))
        if ".fork-tmp" in str(dst):
            # 暂存拷贝中途失败：先落半份再炸
            dst_path = Path(dst)
            dst_path.mkdir(parents=True, exist_ok=True)
            (dst_path / "half.txt").write_text("半份", encoding="utf-8")
            raise RuntimeError("copy interrupted")
        return real_copytree(src, dst, **kwargs)

    monkeypatch.setattr(shutil, "copytree", _interrupted_copy)

    with pytest.raises(RuntimeError, match="copy interrupted"):
        await fork_session(
            store, meta, "parent",
            boundary_user_message_seq=parent.events[-1].seq,
            child_session_id="child", workspace_registry=registry,
        )

    # 只动过暂存，从未写 child 根目录
    assert all(".fork-tmp" in c for c in calls)
    assert not registry.default_workspace_root("child").exists()
    staging = registry.fork_staging_root()
    assert not staging.exists() or not any(staging.iterdir())
    # 父侧不变
    assert _snapshot(registry.default_workspace_root("parent")) == parent_snapshot


# ── ⑤ 盘满明确失败 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enospc_fails_loudly_with_clear_message(tmp_path, monkeypatch) -> None:
    """OSError(ENOSPC) 向上传播、信息明确、不静默吞掉；暂存回收。"""
    store = _store(tmp_path)
    meta = await _meta(tmp_path)
    registry = _registry(tmp_path)
    parent = _build_parent_with_assets(store, registry)
    parent_snapshot = _snapshot(registry.default_workspace_root("parent"))

    def _disk_full(src, dst, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", _disk_full)

    with pytest.raises(OSError, match="No space left on device"):
        await fork_session(
            store, meta, "parent",
            boundary_user_message_seq=parent.events[-1].seq,
            child_session_id="child", workspace_registry=registry,
        )

    assert not registry.default_workspace_root("child").exists()
    staging = registry.fork_staging_root()
    assert not staging.exists() or not any(staging.iterdir())
    assert _snapshot(registry.default_workspace_root("parent")) == parent_snapshot


# ── ③b 跨卷降级路径自身失败：双清理 + 异常上抛 ────────────────────────────


@pytest.mark.asyncio
async def test_cross_volume_fallback_failure_cleans_up(tmp_path, monkeypatch) -> None:
    """EXDEV 降级后 fallback copytree 失败：child_root 与暂存均回收、异常上抛。

    覆盖 fork.py 内层 except BaseException 分支（Correctness P2）。
    """
    store = _store(tmp_path)
    meta = await _meta(tmp_path)
    registry = _registry(tmp_path)
    parent = _build_parent_with_assets(store, registry)
    parent_snapshot = _snapshot(registry.default_workspace_root("parent"))

    real_replace = os.replace
    real_copytree = shutil.copytree

    def _cross_volume_replace(src, dst):
        if ".fork-tmp" in str(src):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(src, dst)

    def _fallback_fails(src, dst, *args, **kwargs):
        # 暂存阶段走真实拷贝；降级阶段（staging→child_root）抛错
        if ".fork-tmp" in str(src):
            raise OSError(errno.EIO, "fallback copy failed")
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr("os.replace", _cross_volume_replace)
    monkeypatch.setattr(shutil, "copytree", _fallback_fails)

    with pytest.raises(OSError, match="fallback copy failed"):
        await fork_session(
            store, meta, "parent",
            boundary_user_message_seq=parent.events[-1].seq,
            child_session_id="child", workspace_registry=registry,
        )

    # 内层清理：child_root 与暂存均已回收
    child_root = registry.default_workspace_root("child")
    assert not child_root.exists() or not any(child_root.iterdir())
    staging = registry.fork_staging_root()
    assert not staging.exists() or not any(staging.iterdir())
    # 父侧不变
    assert _snapshot(registry.default_workspace_root("parent")) == parent_snapshot


# ── #1048 AC7：Docker 后端 fork 不经过宿主 shutil.copytree ──────────────────


class _FakeDockerSandbox:
    """代表 DockerSandbox 的语义面（供无 daemon 的 AC7 编排测试）。"""

    def __init__(self, *, container_name=None, volume_name=None, image=None):
        self.container_name = container_name
        self.volume_name = volume_name
        self.files: dict[str, str] = {}
        self.copied: list[tuple[str, str]] = []
        self.started = False
        self.volume_present = True

    @property
    def workspace_root(self):
        from pathlib import PurePosixPath

        return PurePosixPath("/workspace")

    def ensure_started(self) -> None:
        self.started = True

    def workspace_volume_exists(self) -> bool:
        return self.volume_present

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content

    def read_text(self, path: str) -> str:
        return self.files[path]

    def copy_in(self, host_path, workspace_path: str) -> None:
        self.copied.append((str(host_path), str(workspace_path)))
        # 真实导入：把宿主源内容读进容器内（否则测试无法验证"复制了什么"）。
        src = Path(host_path)
        if src.is_dir():
            for item in src.rglob("*"):
                if item.is_file():
                    rel = item.relative_to(src).as_posix()
                    self.files[rel] = item.read_text(encoding="utf-8")

    def export_to_host(self, dest) -> None:
        # 真实导出：把容器内文件物化到宿主暂存目录（AC7 副本复制的父侧）。
        out = Path(dest)
        out.mkdir(parents=True, exist_ok=True)
        for rel, content in self.files.items():
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


def _docker_registry(tmp_path, monkeypatch) -> WorkspaceRegistry:
    import agent_harness.sandbox.registry as registry_mod
    from agent_harness.sandbox.capabilities import SandboxCapabilities

    monkeypatch.setattr(
        registry_mod,
        "probe_docker_capabilities",
        lambda **_k: SandboxCapabilities(backend="docker", available=True),
    )
    monkeypatch.setattr(registry_mod, "require_available", lambda caps: None)
    monkeypatch.setattr(
        "agent_harness.sandbox.docker.DockerSandbox", _FakeDockerSandbox
    )
    return WorkspaceRegistry(root=tmp_path / "docker_reg", backend="docker")


def test_docker_fork_copies_volume_without_host_copytree(tmp_path, monkeypatch) -> None:
    """AC7：docker fork 不得对 /workspace 走宿主 shutil.copytree；子得独立 volume。"""
    from types import SimpleNamespace

    from agent_harness.session.fork import _copy_workspace

    registry = _docker_registry(tmp_path, monkeypatch)
    parent = registry.create("parent")
    parent.write_text("keep.txt", "parent-asset")
    child_sandbox = registry.create("child")

    copied_to: list[tuple[str, str]] = []
    real_copytree = shutil.copytree

    def _record(src, dst, *args, **kwargs):
        copied_to.append((str(src), str(dst)))
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", _record)

    child = SimpleNamespace(session_id="child", sandbox=child_sandbox)
    _copy_workspace(registry, "parent", child)

    assert not any(
        src == "/workspace" or dst == "/workspace" for src, dst in copied_to
    ), "docker fork 仍在对宿主 /workspace 走 shutil.copytree"
    assert child_sandbox.volume_name != parent.volume_name
    assert child_sandbox.copied, "子 volume 未收到父 workspace 复制"
    # 复制必须是**有内容**的副本，不是空目录（否则 fork 静默丢数据）。
    assert child_sandbox.read_text("keep.txt") == "parent-asset", (
        "子 volume 未真正收到父文件内容（空复制）"
    )


def test_docker_child_write_isolated_from_parent(tmp_path, monkeypatch) -> None:
    """AC7：子会话沿用 docker；子写不影响父 workspace。"""
    from types import SimpleNamespace

    from agent_harness.session.fork import _copy_workspace

    registry = _docker_registry(tmp_path, monkeypatch)
    parent = registry.create("parent")
    parent.write_text("shared.txt", "parent-content")
    child_sandbox = registry.create("child")

    child = SimpleNamespace(session_id="child", sandbox=child_sandbox)
    _copy_workspace(registry, "parent", child)

    child_sandbox.write_text("shared.txt", "child-content")
    assert parent.read_text("shared.txt") == "parent-content"
