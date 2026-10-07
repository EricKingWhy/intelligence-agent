"""#367 [W-23] 选项 A：创建 API 的 autonomy + on_conflict 契约 TDD（先红）。

三档自主度抄 Copilot Interactive/Plan/Autopilot（≈ Codex 三档 ≈ Claude Code
modes）；目录冲突默认 worktree（六家共识），排队作次选项。
"""
import subprocess
from pathlib import Path
from urllib.parse import unquote

import pytest
from starlette.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app


def _init_git_repo(repo: Path) -> Path:
    repo.mkdir(parents=True)
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
def git_repo(tmp_path: Path) -> Path:
    return _init_git_repo(tmp_path / "repo")


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


class TestPlanAutonomyAutoApprove:
    """P2 回归（#367 审查）：autonomy=plan 创建时声明 auto_approve=True。

    计划阶段变更工具本来就被 Plan 档只读门禁拦截（executor 阶段 2.35b，
    先于审批闸门），声明 True 不影响计划期安全；用户批完计划（PLAN→NORMAL）
    后执行不再逐次问——"先计划后执行"档的语义就是"只审一次计划"。
    """

    def test_plan_declares_auto_approve_true(self, client, tmp_path: Path):
        from agent_harness.session.approval import effective_auto_approve
        from agent_harness.session.store import JsonlSessionStore

        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(tmp_path),
            "autonomy": "plan",
        })
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        store = JsonlSessionStore(root=tmp_path / "ws" / "sessions")
        events = store.read_events(sid)
        assert effective_auto_approve(events) is True

    def test_ask_still_declares_auto_approve_false(self, client, tmp_path: Path):
        from agent_harness.session.approval import effective_auto_approve
        from agent_harness.session.store import JsonlSessionStore

        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(tmp_path),
            "autonomy": "ask",
        })
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        store = JsonlSessionStore(root=tmp_path / "ws" / "sessions")
        events = store.read_events(sid)
        assert effective_auto_approve(events) is False


class TestWorktreeHeaderEncoding:
    """#765 回归钉：`X-Worktree-Path` 响应头字段值只能 latin-1（starlette
    `init_headers` 硬约束）。worktree 路径继承仓库位置（`<toplevel-parent>/
    worktrees/<repo>-<hex>`）——仓库在中文用户名/中文目录下（Windows 常态，
    本机 tmp_path 就含「王浩宇」）时，裸路径在响应构造期 `UnicodeEncodeError`
    → 整个创建请求 500。契约：latin-1 可编码 ⇒ 裸头逐字节不变（既有消费者
    零影响）；不可编码 ⇒ 改发 `X-Worktree-Path-Encoded`（RFC 5987 ext-value
    `UTF-8''<percent-encoded>`，safe='' 全量转义），前端
    `worktreePathFromResponse` 兜底解码。"""

    @staticmethod
    def _holder_with_lease(client: TestClient, repo: Path) -> None:
        r = client.post("/api/sessions?launch=false", json={
            "cwd": str(repo), "on_conflict": "queue"})
        assert r.status_code == 200, r.text
        holder = r.json()["session_id"]
        r = client.post(f"/api/sessions/{holder}/task/lease/acquire", json={})
        assert r.status_code == 200, r.text
        assert r.json()["granted"] is True

    def test_non_ascii_worktree_path_uses_encoded_header(
            self, client, tmp_path: Path):
        repo = _init_git_repo(tmp_path / "数据仓库")
        self._holder_with_lease(client, repo)
        # 修复前：worktree 路径含「数据仓库」→ X-Worktree-Path 裸值 latin-1
        # 编码崩在响应构造期 ⇒ 请求 500（TestClient 处原样抛 UnicodeEncodeError）。
        r = client.post("/api/sessions?launch=false", json={"cwd": str(repo)})
        assert r.status_code == 200, r.text
        wt = r.json()["worktree_path"]
        assert "数据仓库" in wt  # JSON 体不受 latin-1 限制，路径仍是原文
        encoded = r.headers["X-Worktree-Path-Encoded"]
        assert encoded.startswith("UTF-8''")
        assert unquote(encoded[len("UTF-8''"):], encoding="utf-8") == wt
        assert "X-Worktree-Path" not in r.headers  # 不可编码时不得发裸头

    def test_header_helper_latin1_safe_path_uses_raw_header(self):
        # 纯函数钉：ASCII 路径保持裸头逐字节不变（既有消费者零影响）。
        from agent_harness.web.app import _worktree_headers
        assert _worktree_headers("D:\\work\\repo-x") == {
            "X-Worktree-Path": "D:\\work\\repo-x"}

    def test_header_helper_non_ascii_path_uses_encoded_header(self):
        from agent_harness.web.app import _worktree_headers
        headers = _worktree_headers("C:\\Users\\王浩宇\\repo")
        assert set(headers) == {"X-Worktree-Path-Encoded"}
        value = headers["X-Worktree-Path-Encoded"]
        assert value.startswith("UTF-8''")
        assert unquote(value[len("UTF-8''"):],
                       encoding="utf-8") == "C:\\Users\\王浩宇\\repo"
