"""#367 [W-23] 选项 A：worktree API 端点 TDD（先红）。

目录冲突默认自动 worktree（六家共识），"排队等"作次选项。
"""
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

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
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


class TestPostWorktrees:
    def test_create_worktree(self, client, git_repo: Path):
        r = client.post("/api/worktrees",
                        json={"repo_path": str(git_repo)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "worktree_path" in body
        wt = Path(body["worktree_path"])
        assert wt.is_dir()
        assert (wt / "a.txt").exists()

    def test_not_a_repo_422(self, client, tmp_path: Path):
        r = client.post("/api/worktrees",
                        json={"repo_path": str(tmp_path)})
        assert r.status_code == 422

    def test_missing_path_422(self, client):
        r = client.post("/api/worktrees", json={})
        assert r.status_code == 422

    def test_nonexistent_path_422(self, client, tmp_path: Path):
        r = client.post(
            "/api/worktrees",
            json={"repo_path": str(tmp_path / "nope")})
        assert r.status_code == 422
