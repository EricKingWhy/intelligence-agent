"""`delete_local_artifacts` 窄删除契约测试（#368 W-24）。

会话硬删时按 id 精确删除本地 artifact（内容 + 旁挂元数据）。这里锁的是"只删自己
拼得出的路径"这一安全形状（ADR-0029 D2），以及一个幂等、可对账的返回字典：

1. 存在 → 内容与 `.json` 旁挂都删；缺一个也接受（幂等）；
2. 不存在 → `not_found`（重复删第二次进这里）；
3. id / session_id 不合形态 → `invalid`，一律不拼路径、不动文件系统；
4. 路径穿越（`..` / 分隔符）打不穿 artifact 根目录；
5. 返回字典固定四个键、都是 list，且保持输入顺序。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.config import Settings
from agent_harness.storage.local_artifact import (
    LocalArtifactStore,
    delete_local_artifacts,
)

SESSION = "sess-del-1"
CONTENT = "line-1\nline-2\n"


def settings(tmp_path: Path) -> Settings:
    return Settings(artifact_dir=str(tmp_path / "artifacts"))


async def saved_artifact(tmp_path: Path) -> tuple[Settings, str]:
    """存一个 artifact，返回 (settings, artifact_id)。"""
    cfg = settings(tmp_path)
    store = LocalArtifactStore(cfg, session_id=SESSION)
    artifact = await store.save(
        SESSION, CONTENT, mime_type="text/plain", source_tool="bash", tool_call_id="tc-1"
    )
    return cfg, artifact.artifact_id


@pytest.mark.asyncio
class TestDeleteExisting:
    async def test_deletes_content_and_sidecar(self, tmp_path: Path) -> None:
        cfg, aid = await saved_artifact(tmp_path)
        session_dir = tmp_path / "artifacts" / SESSION
        assert (session_dir / aid).exists()
        assert (session_dir / f"{aid}.json").exists()

        result = delete_local_artifacts(cfg, SESSION, [aid])

        assert result == {"deleted": [aid], "not_found": [], "invalid": [], "failed": []}
        assert not (session_dir / aid).exists()
        assert not (session_dir / f"{aid}.json").exists()

    async def test_missing_sidecar_still_deletes_content(self, tmp_path: Path) -> None:
        cfg, aid = await saved_artifact(tmp_path)
        (tmp_path / "artifacts" / SESSION / f"{aid}.json").unlink()

        result = delete_local_artifacts(cfg, SESSION, [aid])

        assert result["deleted"] == [aid]
        assert not (tmp_path / "artifacts" / SESSION / aid).exists()

    async def test_preserves_input_order(self, tmp_path: Path) -> None:
        cfg = settings(tmp_path)
        store = LocalArtifactStore(cfg, session_id=SESSION)
        a = await store.save(SESSION, "aaa", mime_type="text/plain", source_tool="bash", tool_call_id="tc-1")
        b = await store.save(SESSION, "bbb", mime_type="text/plain", source_tool="bash", tool_call_id="tc-2")

        result = delete_local_artifacts(cfg, SESSION, [b.artifact_id, a.artifact_id])

        assert result["deleted"] == [b.artifact_id, a.artifact_id]


@pytest.mark.asyncio
class TestIdempotency:
    async def test_second_delete_is_not_found(self, tmp_path: Path) -> None:
        cfg, aid = await saved_artifact(tmp_path)

        first = delete_local_artifacts(cfg, SESSION, [aid])
        second = delete_local_artifacts(cfg, SESSION, [aid])

        assert first["deleted"] == [aid]
        assert second == {"deleted": [], "not_found": [aid], "invalid": [], "failed": []}


class TestInvalidArtifactIds:
    @pytest.mark.parametrize("bad", ["..", "xyz", "a/b", "0" * 15, ""])
    def test_invalid_ids_are_reported_and_touch_nothing(self, tmp_path: Path, bad: str) -> None:
        root = tmp_path / "artifacts"
        session_dir = root / SESSION
        session_dir.mkdir(parents=True)
        (session_dir / "keep").write_text("must survive", encoding="utf-8")

        result = delete_local_artifacts(settings(tmp_path), SESSION, [bad])

        assert result == {"deleted": [], "not_found": [], "invalid": [bad], "failed": []}
        # 非法 id 绝不进入路径拼接，目录里没有任何文件被删
        assert (session_dir / "keep").read_text(encoding="utf-8") == "must survive"


class TestPathTraversalSafety:
    def test_dotdot_cannot_escape_artifact_root(self, tmp_path: Path) -> None:
        # artifact_dir = tmp_path/"artifacts"，`..` 的落点正是 tmp_path 这一层。
        marker = tmp_path / "secret.txt"
        marker.write_text("must survive", encoding="utf-8")
        root = tmp_path / "artifacts"
        (root / SESSION).mkdir(parents=True)

        result = delete_local_artifacts(
            settings(tmp_path), SESSION, ["..", "../secret.txt", "a/b"]
        )

        assert sorted(result["invalid"]) == sorted(["..", "../secret.txt", "a/b"])
        assert result["deleted"] == []
        assert marker.read_text(encoding="utf-8") == "must survive"


class TestInvalidSessionId:
    @pytest.mark.parametrize("bad_session", ["../evil", ""])
    def test_all_ids_invalid_and_tree_untouched(self, tmp_path: Path, bad_session: str) -> None:
        root = tmp_path / "artifacts"
        session_dir = root / SESSION
        session_dir.mkdir(parents=True)
        (session_dir / "keep").write_text("must survive", encoding="utf-8")

        result = delete_local_artifacts(settings(tmp_path), bad_session, ["0" * 16, "1" * 16])

        assert result["deleted"] == []
        assert result["not_found"] == []
        assert sorted(result["invalid"]) == sorted(["0" * 16, "1" * 16])
        assert (session_dir / "keep").read_text(encoding="utf-8") == "must survive"
        assert root.exists()


class TestReturnShape:
    def test_four_keys_are_lists(self, tmp_path: Path) -> None:
        result = delete_local_artifacts(settings(tmp_path), SESSION, [])
        assert set(result) == {"deleted", "not_found", "invalid", "failed"}
        for value in result.values():
            assert isinstance(value, list)
