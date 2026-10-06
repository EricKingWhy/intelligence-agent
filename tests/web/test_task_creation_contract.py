"""#367 [W-23] 选项 A：创建 API 的 autonomy + on_conflict 契约 TDD（先红）。

三档自主度抄 Copilot Interactive/Plan/Autopilot（≈ Codex 三档 ≈ Claude Code
modes）；目录冲突默认 worktree（六家共识），排队作次选项。
"""
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo,
                   check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo,
                   check=True, capture_output=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True,
                   capture_output=True)
    return repo


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path / "ws"),
        model_api_key="sk-test",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


class TestAutonomyField:
    """autonomy 三档被接受并映射到底层（ask→逐次问 / plan→计划模式 /
    auto→全自动）。"""

    def test_autonomy_ask_accepted(self, client, tmp_path: Path):
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(tmp_path),
            "autonomy": "ask",
        })
        assert r.status_code == 200, r.text

    def test_autonomy_plan_accepted(self, client, tmp_path: Path):
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(tmp_path),
            "autonomy": "plan",
        })
        assert r.status_code == 200, r.text

    def test_autonomy_auto_accepted(self, client, tmp_path: Path):
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(tmp_path),
            "autonomy": "auto",
        })
        assert r.status_code == 200, r.text

    def test_autonomy_invalid_422(self, client, tmp_path: Path):
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(tmp_path),
            "autonomy": "yolo",
        })
        assert r.status_code == 422


class TestOnConflictField:
    """on_conflict 默认 worktree；显式 queue 被接受。"""

    def test_on_conflict_defaults_to_worktree(self, client, tmp_path: Path):
        # 不传 on_conflict：目录未被占时正常创建（默认 worktree 不影响无冲突路径）
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(tmp_path),
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("on_conflict", "worktree") == "worktree"

    def test_on_conflict_queue_accepted(self, client, tmp_path: Path):
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(tmp_path),
            "on_conflict": "queue",
        })
        assert r.status_code == 200, r.text

    def test_on_conflict_invalid_422(self, client, tmp_path: Path):
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(tmp_path),
            "on_conflict": "block",
        })
        assert r.status_code == 422


class TestWorktreeOnConflict:
    """目录被租约占用 + on_conflict=worktree（默认）→ 自动建 worktree。"""

    def _make_session(self, client, cwd: Path) -> str:
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(cwd), "on_conflict": "queue"})
        assert r.status_code == 200, r.text
        return r.json()["session_id"]

    def test_locked_dir_creates_worktree(self, client, git_repo: Path):
        holder = self._make_session(client, git_repo)
        # 占住租约
        r = client.post(
            f"/api/sessions/{holder}/task/lease/acquire", json={})
        assert r.status_code == 200, r.text
        assert r.json()["granted"] is True

        # 默认 on_conflict=worktree：自动建 worktree，不排队
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(git_repo)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "worktree_path" in body
        wt = Path(body["worktree_path"])
        assert wt.is_dir() and wt != git_repo
        assert (wt / "a.txt").exists()

    def test_locked_dir_queue_keeps_original(self, client, git_repo: Path):
        holder = self._make_session(client, git_repo)
        r = client.post(
            f"/api/sessions/{holder}/task/lease/acquire", json={})
        assert r.json()["granted"] is True

        # on_conflict=queue：不建 worktree（次选项，走既有排队）
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(git_repo), "on_conflict": "queue"})
        assert r.status_code == 200, r.text
        assert "worktree_path" not in r.json()
