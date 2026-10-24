"""#1333 `client_request_id` 的 HTTP 层验收（2026-10-20 已批准方案）。

本文件锁 HTTP 边界的四件事（认领谓词与前端编码在 `web/src` 侧测试）：

1. **持久化**：带 `client_request_id` 的创建请求把关联值落进 SessionMeta 索引，
   `GET /api/sessions?client_request_id=<uuid>` 精确命中本请求创建的会话；
2. **不暴露**：普通列表/关联查询的响应行都**不带** `client_request_id` 键
   （票面：普通列表响应不暴露该字段，也不写进 SessionEvent）；
3. **fail-closed 的数据前提**：同一关联值对应多条会话时查询原样返回两条
   （「不任意挑选」由前端认领层执行，见 `web/src/lib/wsStream.test.ts`）；
4. **零副作用拒绝**：非法形态（空串 / 超 128 字符）422，且不产生会话 / run 副作用。

`launch=false`（#204 只建会话）即可驱动全部四条——持久化发生在创建路径
（`_finish_initial_creation`，先于 launch），不依赖 run 启动。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web.app import create_app

_CID = "01234567-89ab-4cde-8f01-23456789abcd"


def _web(tmp_path) -> tuple[Any, TestClient]:
    app = create_app(
        Settings(
            _env_file=None,
            workspace_dir=str(tmp_path),
            model_api_key="sk-test",
            enable_cors=False,
        ),
        enable_cors=False,
        allow_testserver=True,
    )
    return app, TestClient(app)


def _create(client: TestClient, body: dict[str, Any]) -> Any:
    # sandbox_backend 显式 local：平台默认（Windows ⇒ docker）在本机不可用，
    # 与既有创建类 web 测试（test_1193 等）同一约定；持久化不依赖 sandbox。
    return client.post(
        "/api/sessions?launch=false",
        json={"sandbox_backend": "local", **body},
    )


def test_create_persists_and_correlation_query_is_exact(tmp_path) -> None:
    _, client = _web(tmp_path)

    res = _create(client, {"client_request_id": _CID})
    assert res.status_code == 200, res.text
    created_id = res.json()["session_id"]

    other = _create(client, {})
    assert other.status_code == 200, other.text

    hits = client.get("/api/sessions", params={"client_request_id": _CID})
    assert hits.status_code == 200
    rows = hits.json()
    assert [r["session_id"] for r in rows] == [created_id]
    # 不暴露：关联查询的响应行也不携带该键
    for row in rows:
        assert "client_request_id" not in row

    # 精确匹配：前缀/子串不命中
    prefix = client.get("/api/sessions", params={"client_request_id": _CID[:8]})
    assert prefix.json() == []

    # 普通列表两条都在，但同样不暴露关联值
    plain = client.get("/api/sessions")
    assert plain.status_code == 200
    assert {r["session_id"] for r in plain.json()} == {created_id, other.json()["session_id"]}
    for row in plain.json():
        assert "client_request_id" not in row


def test_correlation_query_without_match_returns_empty(tmp_path) -> None:
    _, client = _web(tmp_path)
    assert _create(client, {}).status_code == 200

    hits = client.get("/api/sessions", params={"client_request_id": "no-such-cid"})
    assert hits.status_code == 200
    assert hits.json() == []


def test_duplicate_client_request_id_returns_both_rows(tmp_path) -> None:
    """同一关联值两条会话（ buggy 客户端复用 UUID）：查询原样返回两条——
    前端据「多条 = fail-closed 不认领」执行票面裁决。"""
    _, client = _web(tmp_path)
    for _ in range(2):
        assert _create(client, {"client_request_id": _CID}).status_code == 200

    rows = client.get("/api/sessions", params={"client_request_id": _CID}).json()
    assert len(rows) == 2


@pytest.mark.parametrize("bad_cid", ["", "x" * 129])
def test_invalid_client_request_id_rejected_before_side_effects(tmp_path, bad_cid) -> None:
    _, client = _web(tmp_path)

    res = _create(client, {"client_request_id": bad_cid})
    assert res.status_code == 422, res.text
    # 零副作用：没有会话被创建（AC——拒绝发生在产生会话/run 副作用之前）
    assert client.get("/api/sessions").json() == []
    # 合法请求随后照常工作（排除"整个端点被打坏"的假绿）
    assert _create(client, {"client_request_id": _CID}).status_code == 200
