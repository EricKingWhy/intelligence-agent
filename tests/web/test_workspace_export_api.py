"""#1048 AC5/AC6：本地 REST 显式导出工作区。

红阶段（TDD）：当前**无任何工作区导出端点**（checkpoint §4 第 5 条）。
本文件钉住契约（端点缺失时全部返回 404 → 断言失败 = 红）：

- AC5：POST 显式触发导出，请求带宿主目标路径；路径授权复用现有
  host-directory 边界（projects.py 来源闸，ADR-0025 D1(b)/ADR-0028 D2）。
- AC6：导出是显式副本，只能发布到**全新**宿主目标目录，已存在拒绝；
  归档拒绝路径穿越/绝对路径/不安全特殊项；hardlink/symlink 不写穿新目标、
  不改外部 canary。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app

SID = "sess_export_1048"


def _client(tmp_path: Path, **overrides: object) -> TestClient:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        **overrides,
    )
    return TestClient(create_app(settings, enable_cors=False))


def _export(client: TestClient, body: dict) -> object:
    return client.post(f"/api/sessions/{SID}/workspace/export", json=body)


class TestWorkspaceExportApi:
    def test_export_requires_host_target_path(self, tmp_path: Path) -> None:
        """AC5：缺 host_target → 422（显式导出，必须给宿主目标路径）。"""
        client = _client(tmp_path)
        resp = _export(client, {})
        assert resp.status_code == 422, resp.text

    def test_export_target_outside_host_boundary_rejected(self, tmp_path: Path) -> None:
        """AC5：目标越出既有 host-directory 边界 → 403（复用来源闸）。"""
        client = _client(tmp_path)
        resp = _export(client, {"host_target": "/etc/agent-harness-export-1048"})
        assert resp.status_code == 403, resp.text

    def test_export_refuses_existing_target(self, tmp_path: Path) -> None:
        """AC6：目标已存在 → 拒绝，不覆盖。"""
        client = _client(tmp_path)
        existing = tmp_path / "already_there"
        existing.mkdir()
        resp = _export(client, {"host_target": str(existing)})
        assert resp.status_code in (400, 409), resp.text

    def test_export_archive_rejects_traversal_absolute_and_special(
        self, tmp_path: Path
    ) -> None:
        """AC6：归档构建拒绝 `..` / 绝对路径 / 设备特殊项（真实断言）。"""
        import os as _os

        import pytest

        from agent_harness.web.workspace_export import build_export_archive

        # 符号链接目标为绝对路径 → 拒绝。
        src_abs = tmp_path / "src_abs"
        src_abs.mkdir()
        (src_abs / "evil").symlink_to("/etc/passwd")
        with pytest.raises(ValueError):
            build_export_archive(src_abs, tmp_path / "dest_abs")

        # 符号链接目标含 `..` 穿越 → 拒绝。
        src_up = tmp_path / "src_up"
        src_up.mkdir()
        (src_up / "up").symlink_to("../outside")
        with pytest.raises(ValueError):
            build_export_archive(src_up, tmp_path / "dest_up")

        # 设备/FIFO 特殊项 → 拒绝。
        src_fifo = tmp_path / "src_fifo"
        src_fifo.mkdir()
        _os.mkfifo(src_fifo / "pipe")
        with pytest.raises(ValueError):
            build_export_archive(src_fifo, tmp_path / "dest_fifo")

    def test_export_symlink_hardlink_does_not_write_through_or_touch_canary(
        self, tmp_path: Path
    ) -> None:
        """AC6：硬链接按内容复制成独立对象；改写导出副本不改外部 canary。"""
        import os as _os

        from agent_harness.web.workspace_export import build_export_archive

        canary = tmp_path / "canary.txt"
        canary.write_text("CANARY", encoding="utf-8")
        src = tmp_path / "src"
        src.mkdir()
        # 硬链接指向工作区外的 canary：导出必须按内容复制，不保留对象身份。
        _os.link(canary, src / "linked.txt")

        dest = tmp_path / "fresh_export"
        build_export_archive(src, dest)

        exported = dest / "linked.txt"
        assert exported.read_text(encoding="utf-8") == "CANARY"
        assert exported.stat().st_ino != canary.stat().st_ino, (
            "导出保留了硬链接对象身份（与外部 canary 共享 inode）"
        )
        # 改写导出副本不得写穿外部 canary。
        exported.write_text("CHANGED", encoding="utf-8")
        assert canary.read_text(encoding="utf-8") == "CANARY"
