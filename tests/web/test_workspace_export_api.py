"""#1048 AC5/AC6：本地 REST 显式导出工作区。

契约（见 ADR-0054）：

- AC5：POST 显式触发导出，请求带宿主目标路径（**绝对**，非绝对即 422）；路径授权
  沿用现有 host-directory 来源闸（projects.py `require_trusted_origin`，
  ADR-0025 D1(b)/ADR-0028 D2）——**不设** `Settings.workspace_dir` 根限制，与既有
  host-directory API 口径一致（协调员裁决 2026-10-14）。会话不存在 / 无 workspace
  映射 → 404。
- AC6：导出是显式副本，只能发布到**全新**宿主目标目录，已存在拒绝；
  导出树拒绝路径穿越/绝对路径/不安全特殊项；hardlink/symlink 不写穿新目标、
  不改外部 canary。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel

_DATA_PREFIX = "data:"


def _client(tmp_path: Path, **overrides: object) -> TestClient:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        **overrides,
    )
    return TestClient(create_app(settings, enable_cors=False))


def _create_session(client: TestClient) -> str:
    """建一个真会话（真 runtime + 替身模型），返回 session_id（复用 #191 口径）。"""
    with patch(
        "agent_harness.assembly.create_chat_model",
        return_value=ScriptedModel(responses=[AIMessage(content="ok")]),
    ):
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"local": {"max_agent_turns": 1}}},
        )
    assert resp.status_code == 200, resp.text
    frames = [
        json.loads(line[len(_DATA_PREFIX):].strip())
        for line in resp.text.splitlines()
        if line.startswith(_DATA_PREFIX)
    ]
    session_id = next((f["session_id"] for f in frames if f.get("session_id")), None)
    assert session_id, f"SSE 流里没有 session_id：{frames[:3]}"
    return str(session_id)


def _export(client: TestClient, session_id: str, body: dict) -> object:
    return client.post(
        f"/api/sessions/{session_id}/workspace/export", json=body
    )


class TestWorkspaceExportApi:
    def test_export_requires_host_target_path(self, tmp_path: Path) -> None:
        """AC5：缺 host_target → 422（显式导出，必须给宿主目标路径）。"""
        client = _client(tmp_path)
        resp = _export(client, "sess_export_1048", {})
        assert resp.status_code == 422, resp.text

    def test_export_requires_absolute_host_target(self, tmp_path: Path) -> None:
        """AC5：host_target 必须是绝对路径（相对路径 → 422）。"""
        client = _client(tmp_path)
        resp = _export(client, "sess_export_1048", {"host_target": "relative/dir"})
        assert resp.status_code == 422, resp.text

    def test_export_missing_session_returns_404(self, tmp_path: Path) -> None:
        """AC5：会话不存在 → 404（不吞成"导出空目录"的 200）。"""
        client = _client(tmp_path)
        target = tmp_path.parent / f"{tmp_path.name}-nonexistent-session-export"
        resp = _export(client, "sess_does_not_exist", {"host_target": str(target)})
        assert resp.status_code == 404, resp.text
        assert not target.exists(), "会话不存在却创建了导出目录"

    def test_export_accepts_absolute_target_outside_workspace_root(
        self, tmp_path: Path
    ) -> None:
        """AC5：**不设** workspace_dir 根限制（协调员裁决）——绝对目标即使不在
        `Settings.workspace_dir` 下也被接受（与既有 host-directory API 口径一致）。"""
        client = _client(tmp_path)
        sid = _create_session(client)
        root = Path(
            client.app.state.agent.workspace_registry.get(sid).workspace_root
        )
        (root / "hello.txt").write_text("EXPORT-ME\n", encoding="utf-8")

        outside = tmp_path.parent / f"{tmp_path.name}-outside-export"
        try:
            resp = _export(client, sid, {"host_target": str(outside)})
            assert resp.status_code == 200, resp.text
            assert (outside / "hello.txt").read_text(encoding="utf-8") == "EXPORT-ME\n"
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_export_refuses_existing_target(self, tmp_path: Path) -> None:
        """AC6：目标已存在 → 拒绝，不覆盖。"""
        client = _client(tmp_path)
        existing = tmp_path / "already_there"
        existing.mkdir()
        resp = _export(client, "sess_export_1048", {"host_target": str(existing)})
        assert resp.status_code in (400, 409), resp.text

    def test_export_archive_rejects_traversal_absolute_and_special(
        self, tmp_path: Path
    ) -> None:
        """AC6：导出树构建拒绝 `..` / 绝对路径 / 设备特殊项（真实断言）。"""
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
        os.mkfifo(src_fifo / "pipe")
        with pytest.raises(ValueError):
            build_export_archive(src_fifo, tmp_path / "dest_fifo")

    def test_export_symlink_recreated_and_hardlink_copied_without_canary_write_through(
        self, tmp_path: Path
    ) -> None:
        """AC6：符号链接按链接文本原样重建（不解析、不写穿）；硬链接按内容复制成
        独立对象（改写导出副本不改外部 canary）。"""
        from agent_harness.web.workspace_export import build_export_archive

        canary = tmp_path / "canary.txt"
        canary.write_text("CANARY", encoding="utf-8")
        src = tmp_path / "src"
        src.mkdir()
        # 硬链接指向工作区外的 canary：导出必须按内容复制，不保留对象身份。
        os.link(canary, src / "linked.txt")
        # 安全的相对符号链接：导出必须原样重建链接（不解析成实体文件）。
        (src / "real.txt").write_text("REAL", encoding="utf-8")
        (src / "alias.txt").symlink_to("real.txt")

        dest = tmp_path / "fresh_export"
        build_export_archive(src, dest)

        # 硬链接：内容复制成独立 inode，改写副本不写穿外部 canary。
        exported = dest / "linked.txt"
        assert exported.read_text(encoding="utf-8") == "CANARY"
        assert exported.stat().st_ino != canary.stat().st_ino, (
            "导出保留了硬链接对象身份（与外部 canary 共享 inode）"
        )
        exported.write_text("CHANGED", encoding="utf-8")
        assert canary.read_text(encoding="utf-8") == "CANARY"

        # 符号链接：仍是链接、链接文本不变（未被解析、未写穿目标）。
        alias = dest / "alias.txt"
        assert alias.is_symlink(), "导出把符号链接解析成了实体文件"
        assert os.readlink(alias) == "real.txt"
