"""#352 [W-08]：证据投影的 REST 传输面。

POST /api/sessions/{id}/evidence（记录：422 形状非法 / 409 evidence_id 冲突 /
404 会话不存在）；GET /api/sessions/{id}/evidence（只回服务端投影 + 陈旧标识；
刷新重建同样结果；无任务定义 → 404）。

领域层见 tests/session/test_evidence.py；fixture 策略对齐
tests/web/test_task_delivery_api.py（patch build_runtime/launch，
`POST /api/sessions?launch=false` 造空会话再打新端点）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.session import service as service_module
from agent_harness.session.evidence import compute_evidence_manifest
from agent_harness.web.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def _fake_build(**kwargs):
        return object()

    monkeypatch.setattr(service_module, "build_runtime", _fake_build)

    from agent_harness.session.runmanager import RunManager, Subscriber

    launched: list = []

    class _FakeRun:
        task = None

        def unsubscribe(self, _sub):
            pass

    def _fake_launch(self, session, runtime, user_input, user_input_metadata=None):
        launched.append(session)
        sub = Subscriber()
        sub.queue.put_nowait(self.DONE)
        return _FakeRun(), sub

    monkeypatch.setattr(RunManager, "launch", _fake_launch)

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        enable_cors=False,
    )
    app = create_app(settings, enable_cors=False)
    app.state._launched_sessions = launched
    return TestClient(app), app


def _create_session(client) -> str:
    resp = client.post("/api/sessions?launch=false", json={})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _define(client, session_id: str) -> list[str]:
    resp = client.post(
        f"/api/sessions/{session_id}/task/definition",
        json={"task_text": "修登录", "criteria": [{"text": "登录成功跳转"}]},
    )
    assert resp.status_code == 200, resp.text
    return [c["item_id"] for c in resp.json()["task"]["criteria"]]


def _session_cwd(client, session_id: str) -> Path:
    resp = client.get(f"/api/sessions/{session_id}/task")
    assert resp.status_code == 200, resp.text
    return Path(resp.json()["task"]["cwd"])


def _dto(session_id: str, criterion_id: str, **overrides) -> dict:
    dto = {
        "evidence_id": "ev-001",
        "task_session_id": session_id,
        "run_id": "run-1",
        "criterion_id": criterion_id,
        "kind": "test",
        "source_event_seq": 7,
        "tool_call_id": None,
        "captured_at": "2026-10-06T15:00:00.000+00:00",
        "result": "pass",
        "command_or_action": "pytest -q tests/auth",
        "exit_code_or_observation": 0,
        "artifact_ref": None,
        "base_head": None,
        "workspace_manifest": {
            "files": [],
            "manifest_hash": "c" * 64,
            "progress_md_sha256": None,
        },
    }
    dto.update(overrides)
    return dto


def _post(client, session_id: str, dto: dict):
    return client.post(f"/api/sessions/{session_id}/evidence", json={"evidence": dto})


class TestPostEvidence:
    def test_records_and_get_returns_projection(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        resp = _post(client, sid, _dto(sid, cids))
        assert resp.status_code == 200, resp.text

        got = client.get(f"/api/sessions/{sid}/evidence")
        assert got.status_code == 200, got.text
        records = got.json()["evidence"][cids]
        assert len(records) == 1
        rec = records[0]
        assert rec["evidence_id"] == "ev-001"
        assert rec["kind"] == "test" and rec["result"] == "pass"
        assert rec["freshness"]["status"] == "fresh"

    def test_bad_shape_422(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        resp = _post(client, sid, _dto(sid, cids, kind="video"))
        assert resp.status_code == 422, resp.text

    def test_duplicate_evidence_id_409(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        assert _post(client, sid, _dto(sid, cids)).status_code == 200
        resp = _post(client, sid, _dto(sid, cids))
        assert resp.status_code == 409, resp.text

    def test_unknown_session_404(self, client) -> None:
        client, _ = client
        resp = _post(client, "does-not-exist", _dto("does-not-exist", "ac-x"))
        assert resp.status_code == 404, resp.text

    def test_post_returns_record_with_freshness(self, client) -> None:
        """POST 与 GET 同口径：返回的 record 带 freshness 陈旧标识。"""
        client, _ = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        resp = _post(client, sid, _dto(sid, cids))
        assert resp.status_code == 200, resp.text
        record = resp.json()["record"]
        assert record["evidence_id"] == "ev-001"
        assert record["freshness"]["status"] == "fresh"


class TestGetEvidence:
    def test_no_task_definition_404(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        resp = client.get(f"/api/sessions/{sid}/evidence")
        assert resp.status_code == 404, resp.text

    def test_empty_when_no_evidence(self, client) -> None:
        client, _ = client
        sid = _create_session(client)
        _define(client, sid)
        resp = client.get(f"/api/sessions/{sid}/evidence")
        assert resp.status_code == 200, resp.text
        assert resp.json()["evidence"] == {}

    def test_refresh_rebuilds_same_projection(self, client) -> None:
        """票面验收：API 刷新重建同样证据与 stale 标识。"""
        client, _ = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        assert _post(client, sid, _dto(sid, cids)).status_code == 200
        first = client.get(f"/api/sessions/{sid}/evidence").json()
        second = client.get(f"/api/sessions/{sid}/evidence").json()
        assert first == second

    def test_stale_after_source_change(self, client) -> None:
        """票面验收：测试后改一行源码 → stale，明确列出变动文件。"""
        client, _ = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        cwd = _session_cwd(client, sid)
        target = cwd / "src" / "auth.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"v1\n")
        manifest = compute_evidence_manifest(cwd, ["src/auth.py"]).to_payload()
        dto = _dto(sid, cids, workspace_manifest=manifest)
        # manifest_hash 由真实文件算出：重算一次填入（DTO 自洽）
        assert _post(client, sid, dto).status_code == 200

        fresh = client.get(f"/api/sessions/{sid}/evidence").json()
        assert fresh["evidence"][cids][0]["freshness"]["status"] == "fresh"

        target.write_bytes("v2 // 改了一行\n".encode())
        stale = client.get(f"/api/sessions/{sid}/evidence").json()
        rec = stale["evidence"][cids][0]
        assert rec["freshness"]["status"] == "stale"
        assert any("src/auth.py" in r for r in rec["freshness"]["reasons"])
        # 陈旧的是投影状态，当初记录的 result 不被改写
        assert rec["result"] == "pass"

    def test_missing_artifact_is_stale(self, client) -> None:
        """票面验收：Artifact 丢失 → stale（不可读回）。"""
        client, _ = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        dto = _dto(sid, cids, evidence_id="ev-art", artifact_ref="ab" * 8,
                   tool_call_id="tc-1")
        assert _post(client, sid, dto).status_code == 200
        rec = client.get(f"/api/sessions/{sid}/evidence").json()["evidence"][cids][0]
        assert rec["freshness"]["status"] == "stale"
        assert any("ab" * 8 in r for r in rec["freshness"]["reasons"])

    def test_base_head_stale_in_real_git_repo(self, client) -> None:
        """一次真实仓库 diff/命令记录：base_head 变动 → stale。"""
        client, _ = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        cwd = _session_cwd(client, sid)

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                env={"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd)},
            ).stdout.strip()

        (cwd / "a.txt").write_bytes(b"v1\n")
        git("init", "-q")
        git("-c", "user.email=t@t", "-c", "user.name=t", "add", "a.txt")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        head = git("rev-parse", "HEAD")
        assert len(head) == 40

        manifest = compute_evidence_manifest(cwd, ["a.txt"]).to_payload()
        dto = _dto(sid, cids, workspace_manifest=manifest, base_head=head,
                   kind="diff", command_or_action="git diff HEAD",
                   exit_code_or_observation="diff --git a/a.txt ...")
        assert _post(client, sid, dto).status_code == 200
        rec = client.get(f"/api/sessions/{sid}/evidence").json()["evidence"][cids][0]
        assert rec["freshness"]["status"] == "fresh"

        (cwd / "a.txt").write_bytes(b"v2\n")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "change")
        rec = client.get(f"/api/sessions/{sid}/evidence").json()["evidence"][cids][0]
        assert rec["freshness"]["status"] == "stale"
        assert any("base_head" in r for r in rec["freshness"]["reasons"])

    def test_sensitive_values_roundtrip_verbatim(self, client) -> None:
        """票面验收：敏感输出值不进正文——记录侧脱敏后的值原样往返。"""
        client, _ = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        dto = _dto(
            sid, cids,
            command_or_action="curl -H 'Authorization: <已脱敏>' https://api.example.com",
            exit_code_or_observation="<响应体已脱敏>",
        )
        assert _post(client, sid, dto).status_code == 200
        rec = client.get(f"/api/sessions/{sid}/evidence").json()["evidence"][cids][0]
        assert rec["command_or_action"] == dto["command_or_action"]
        assert rec["exit_code_or_observation"] == dto["exit_code_or_observation"]

    @pytest.mark.asyncio
    async def test_artifact_readable_with_matching_attribution_stays_fresh(
        self, client
    ) -> None:
        """artifact 真实可读回 + tool_call_id 归属一致 → fresh（显式归属校验正路）。"""
        from agent_harness.storage.local_artifact import LocalArtifactStore

        client, app = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        store = LocalArtifactStore(app.state.agent.settings, session_id=sid)
        artifact = await store.save(
            sid, "12 passed", mime_type="text/plain",
            source_tool="bash", tool_call_id="tc-1",
        )
        dto = _dto(
            sid, cids, evidence_id="ev-ok", artifact_ref=artifact.artifact_id,
            tool_call_id="tc-1",
        )
        assert _post(client, sid, dto).status_code == 200
        rec = client.get(f"/api/sessions/{sid}/evidence").json()["evidence"][cids][0]
        assert rec["freshness"]["status"] == "fresh", rec["freshness"]["reasons"]

    @pytest.mark.asyncio
    async def test_artifact_attribution_mismatch_stale_via_api(
        self, client
    ) -> None:
        """artifact 旁挂 tool_call_id 与证据对不上 → stale（归属校验反路）。"""
        from agent_harness.storage.local_artifact import LocalArtifactStore

        client, app = client
        sid = _create_session(client)
        (cids,) = _define(client, sid)
        store = LocalArtifactStore(app.state.agent.settings, session_id=sid)
        artifact = await store.save(
            sid, "12 passed", mime_type="text/plain",
            source_tool="bash", tool_call_id="tc-1",
        )
        dto = _dto(
            sid, cids, evidence_id="ev-mismatch", artifact_ref=artifact.artifact_id,
            tool_call_id="tc-other",
        )
        assert _post(client, sid, dto).status_code == 200
        rec = client.get(f"/api/sessions/{sid}/evidence").json()["evidence"][cids][0]
        assert rec["freshness"]["status"] == "stale"
        assert any("归属" in r for r in rec["freshness"]["reasons"])
