"""#939 M-13：只读回执删除语义的**单一归属模块**（`storage/readonly_fs.py`）。

簇 C（`discard_local_artifacts` + rmtree 只读位重试 + 回执→全局对象推导）此前住在
`storage/local_artifact.py`，而它与该宿主的 `ArtifactStore` 抽象**零交集**：不 import
任何 `storage.artifact` 的内容符号、不碰字节、不知道"附件"是什么。它只做三件事——
清只读位、`shutil.rmtree`、按 sha 从路径反推。拆出后两边可各自演进。

本文件钉两件事：
1. **符号归属**（簇 C 在新模块、且**不再**从宿主模块导出——不留双份出口）；
2. **行为不变**（新模块的公开入口可跑；细粒度行为由 `tests/attachments/test_local_byte_store.py`
   的 12 例覆盖，那里已按新模块路径重指）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.storage import local_artifact, readonly_fs
from agent_harness.storage.local_artifact import LocalArtifactStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        model_api_key="sk-test",
    )


def test_cluster_c_symbols_live_in_readonly_fs() -> None:
    """簇 C 的五个符号归属 `readonly_fs`（只读位重试 + 回执→对象推导 + 会话丢弃）。"""
    for name in (
        "discard_local_artifacts",
        "_read_only_retry_handler",
        "_global_object_for_receipt",
        "_rmtree_clearing_read_only",
        "_restore_object_read_only",
    ):
        assert hasattr(readonly_fs, name), f"{name} 应在 storage.readonly_fs"


def test_host_module_no_longer_exports_cluster_c_entry() -> None:
    """宿主模块**不再**是簇 C 的出口（不留双份出口；调用方按新路径 import）。

    `delete_local_artifacts`（按文本 artifact id 精确删）是本模块的既有领域操作，
    按审计判定留在原地——它不是簇 C。
    """
    assert not hasattr(local_artifact, "discard_local_artifacts")
    assert not hasattr(local_artifact, "_read_only_retry_handler")
    assert not hasattr(local_artifact, "_global_object_for_receipt")
    assert not hasattr(local_artifact, "_rmtree_clearing_read_only")
    assert not hasattr(local_artifact, "_restore_object_read_only")
    assert hasattr(local_artifact, "delete_local_artifacts")


def test_discard_via_new_module_still_drops_session_dir(tmp_path: Path) -> None:
    """行为不变：经新路径的 `discard_local_artifacts` 仍删掉会话目录。"""
    store = LocalArtifactStore(_settings(tmp_path), session_id="sess-a")
    asyncio.run(store.save_bytes("sess-a", b"readonly-fs-move", mime_type="image/png"))
    session_dir = tmp_path / "artifacts" / "sess-a"
    assert session_dir.is_dir()

    readonly_fs.discard_local_artifacts(_settings(tmp_path), "sess-a")

    assert not session_dir.exists()


def test_empty_artifact_dir_is_a_noop(tmp_path: Path) -> None:
    """拼不出安全路径就不动手（ADR-0029 D2 的既有安全形状，逐字不变）。"""
    readonly_fs.discard_local_artifacts(Settings(_env_file=None, artifact_dir="", model_api_key="sk-test"), "sess-a")


@pytest.mark.parametrize("bad", ["../escape", "a/b", ""])
def test_malformed_session_id_is_a_noop(tmp_path: Path, bad: str) -> None:
    """非法 session_id 不拼路径、不抛（纵深防御）。"""
    readonly_fs.discard_local_artifacts(_settings(tmp_path), bad)
