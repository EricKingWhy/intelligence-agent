"""#1324 AC2(a)：steer 打空的 409 必须在后端留下可归因的日志。

背景：续聊首次 409 → 前端改投 queue → 200，是**设计内**的重试（ADR-0030 §4.6 /
§13），但此前全链路没有任何归因记录，现场只能看到「一次 409 被吞掉」。

本文件只锁外部可观测事实：空转会话上 `mode=steer` 返回 409 的同时，
`agent_harness.session.service` 记一条 WARNING，含异常类型、session_id、
run_id（此处诚实地是 None：没有在途 run 正是判据）与原因标记。
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app


def _client(tmp_path) -> TestClient:
    app = create_app(
        Settings(
            _env_file=None,
            workspace_dir=str(tmp_path),
            model_api_key="sk-test",
            enable_cors=False,
        ),
        enable_cors=False,
    )
    return TestClient(app)


def test_idle_steer_409_is_attributed_in_the_log(tmp_path, caplog):
    client = _client(tmp_path)
    created = client.post("/api/sessions", json={}, params={"launch": "false"})
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]

    with caplog.at_level(logging.WARNING, logger="agent_harness.session.service"):
        resp = client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "probe", "mode": "steer"},
        )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == (
        "steer requires an active run; use mode='queue' to enqueue"
    )

    # 建会话本身也会在同一个 logger 上留 WARNING（task 形状校验），所以按
    # 本票的归因标记收窄，只钉「这条 409 恰好一条记录」。
    records = [
        record
        for record in caplog.records
        if record.name == "agent_harness.session.service"
        and record.levelno == logging.WARNING
        and "SteerTargetNotFound" in record.getMessage()
    ]
    assert len(records) == 1, [record.getMessage() for record in caplog.records]
    message = records[0].getMessage()
    assert "SteerTargetNotFound" in message
    assert session_id in message
    assert "run_id=None" in message
    # 复审补的副作用面维度：该用例的请求不带 queue_id ⇒ 无排队项被取消。
    assert "queue_id=None" in message
    assert "no_active_run" in message
