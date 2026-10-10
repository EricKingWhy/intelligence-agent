"""#517：错误面一致性（BUG-05 代理裸 500 / BUG-06 schema 系统性不符 / BUG-07 超长 id）。

三条钉子：

1. **模型 client 构造失败 → 结构化 503 JSON**（BUG-05）：代理变量（corporate/k8s
   风格 `NO_PROXY` 里的 `[::1]`）让 vendored httpx2 在构造期抛 `InvalidURL`——
   任何带这类部署的环境都会在 `POST /api/sessions` 裸 500。构造失败是**环境/配置
   暂时不可用**（503，可修好重试），不是服务端 bug（500）。
2. **OpenAPI 与真实错误体一致**（BUG-06）：业务 4xx 返回 `{"detail": "<字符串>"}`，
   FastAPI 自动文档却把 422 写成 `HTTPValidationError`（detail 数组）→ 按 spec 生成的
   强类型客户端解析失败。修法是**如实声明现实**：422 = 校验数组与业务信封二选一
   （oneOf）；SSE 路由声明 `text/event-stream`；/api 操作补声明 500；本票新增的
   503 逐路由补声明。**不改任何真实响应体**——前端消费的是 detail 字符串，
   改形状是另一票的事。
3. **session_id 长度上限**（BUG-07）：字符集白名单 + 无长度上限 = 超长 id 直达
   文件系统（Linux errno 36 → 500；Windows → 404，同样偏离校验意图）。`{1,128}`
   与 uuid4 hex 生成形态同量级；128 及以内 + 白名单字符集在两个平台的文件名上限
   内（255），结构上保证任何通过校验的 id 都不会再把文件系统错误带到用户面。
"""

from __future__ import annotations

import errno
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.model.provider import ModelClientConstructionError
from agent_harness.web.app import create_app


def _client(tmp_path: Path, **client_kwargs) -> TestClient:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return TestClient(create_app(settings, enable_cors=False), **client_kwargs)


# ── BUG-07：长度上限 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/sessions/{sid}/events"),
        ("post", "/api/sessions/{sid}/archive"),
        ("get", "/api/sessions/{sid}/lineage"),
    ],
)
def test_oversized_session_id_is_422_not_500_or_404(
    tmp_path: Path, method: str, path: str
) -> None:
    """10000 字符 id：三平台/三端点一律 422——校验层拒绝，不直达文件系统。"""
    client = _client(tmp_path)
    resp = getattr(client, method)(path.format(sid="a" * 10_000))
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], str)


def test_session_id_length_boundary_128_legal_129_rejected(tmp_path: Path) -> None:
    """128 字符合法（过校验 → 404 不存在）；129 字符 422。"""
    client = _client(tmp_path)

    ok = client.get(f"/api/sessions/{'a' * 128}/events")
    assert ok.status_code == 404, ok.text  # 通过校验，只是会话不存在

    bad = client.get(f"/api/sessions/{'a' * 129}/events")
    assert bad.status_code == 422, bad.text


def test_lineage_invalid_session_id_is_422(tmp_path: Path) -> None:
    """票面复现：`/lineage` 对同一非法 id 曾是 500（`/events`、`/budget` 是 422）。

    `validate_session_id` 抛出的 `InvalidSessionId` 必须像其他端点一样经
    `http_error` 翻译——同类输入一种结果。
    """
    client = _client(tmp_path)
    resp = client.get("/api/sessions/%1D%2C%C3%AE%C3%B5/lineage")
    assert resp.status_code == 422, resp.text


# ── BUG-05：模型 client 构造失败 → 结构化 503 ────────────────────────


def test_proxy_failure_at_create_maps_to_503_json(tmp_path: Path) -> None:
    """构造期崩溃（代理环境 httpx2 InvalidURL 的替身）→ 503 JSON，不再裸 500。"""
    client = _client(tmp_path)
    payload = {
        "task": "hi",
        "budget": {"local": {"max_agent_turns": 1}},
    }
    with patch(
        "agent_harness.assembly.create_chat_model",
        side_effect=ModelClientConstructionError(
            "模型 client 构造失败（代理/网络环境）：Invalid port: ':1]'"
        ),
    ):
        resp = client.post("/api/sessions", params={"launch": "true"}, json=payload)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert "Invalid port" in body["detail"]


def test_provider_wraps_construction_failure_with_cause() -> None:
    """provider 层把构造异常包装成 `ModelClientConstructionError`，cause 保留。

    构造是纯本地操作（字段校验 + client 初始化），此处的失败来自配置/环境
    （代理 URL、key 形态），不是网络调用——统一包一层让 web 层可按类型映射。
    """
    from agent_harness.model.config import ModelConfig
    from agent_harness.model.provider import create_chat_model

    class _Boom:
        def __init__(self, **kwargs):
            raise ValueError("Invalid port: ':1]'")

    config = ModelConfig(
        provider="custom-openai",
        model_name="test-model",
        base_url="http://127.0.0.1:9",
        api_key="sk-test",
        temperature=0.0,
    )
    with (
        patch("agent_harness.model.provider.ReasoningChatOpenAI", _Boom),
        pytest.raises(ModelClientConstructionError) as excinfo,
    ):
        create_chat_model(config)
    assert isinstance(excinfo.value.__cause__, ValueError)


# ── BUG-06：OpenAPI 与真实错误体一致 ─────────────────────────────────


def _openapi(client: TestClient) -> dict:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    return resp.json()


def test_openapi_422_documents_both_error_shapes(tmp_path: Path) -> None:
    """422 = HTTPValidationError（detail 数组）与业务信封（detail 字符串）二选一。

    现实里两种都有：FastAPI 请求校验出数组、业务 http_error 出字符串。声明成
    oneOf 后按 spec 生成的强类型客户端两种都能解析。
    """
    api = _openapi(_client(tmp_path))
    op = api["paths"]["/api/sessions/{session_id}/archive"]["post"]
    schema = op["responses"]["422"]["content"]["application/json"]["schema"]
    assert "oneOf" in schema, f"422 必须声明两种形态，实际：{schema}"
    refs = {item.get("$ref", "") for item in schema["oneOf"]}
    assert any("HTTPValidationError" in ref for ref in refs), refs
    assert any("ErrorEnvelope" in ref for ref in refs), refs


def test_openapi_declares_event_stream_on_streaming_routes(tmp_path: Path) -> None:
    """SSE 路由的 200 必须声明 text/event-stream（票面：3 次 Undocumented CT）。"""
    api = _openapi(_client(tmp_path))

    stream = api["paths"]["/api/sessions/{session_id}/stream"]["get"]
    assert "text/event-stream" in stream["responses"]["200"]["content"]

    create = api["paths"]["/api/sessions"]["post"]
    # launch=true 时该端点回 SSE 流、launch=false 回 JSON——两种都如实声明
    assert "text/event-stream" in create["responses"]["200"]["content"]
    assert "application/json" in create["responses"]["200"]["content"]


def test_openapi_documents_500_on_api_operations(tmp_path: Path) -> None:
    """任何 /api 操作都可能 500——文档必须声明，且 schema 是 JSON 信封。"""
    api = _openapi(_client(tmp_path))
    for path, item in api["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method, op in item.items():
            assert "500" in op["responses"], f"{method.upper()} {path} 未声明 500"
            schema = op["responses"]["500"]["content"]["application/json"]["schema"]
            assert schema.get("$ref", "").endswith("ErrorEnvelope"), (
                f"{method.upper()} {path} 的 500 schema：{schema}"
            )


def test_openapi_documents_413_on_body_bearing_operations(tmp_path: Path) -> None:
    """#562 残余（1 MiB body 上限）：带体方法族必须声明 413，信封同 ErrorEnvelope。

    `BodyDepthGuardMiddleware` 全局挂载（`app.add_middleware`），任何带体请求都
    可能超限——机制可机械判定，与 503（靠 handler 逻辑、不全局撒）不同。反向锚：
    GET 语义上不带体，声明面收缩到 POST/PUT/PATCH/DELETE（抽查钉住口径）。
    """
    api = _openapi(_client(tmp_path))
    body_bearing = {"post", "put", "patch", "delete"}
    for path, item in api["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method, op in item.items():
            if method not in body_bearing:
                continue
            responses = op["responses"]
            assert "413" in responses, f"{method.upper()} {path} 未声明 413"
            schema = responses["413"]["content"]["application/json"]["schema"]
            assert schema.get("$ref", "").endswith("ErrorEnvelope"), (
                f"{method.upper()} {path} 的 413 schema：{schema}"
            )
    # 反向锚（抽查）：GET 不声明 413（本仓不消费 GET 请求体，不全局撒）。
    stream = api["paths"]["/api/sessions/{session_id}/stream"]["get"]
    assert "413" not in stream["responses"]


def test_openapi_documents_503_on_newly_guaranteed_endpoints(
    tmp_path: Path,
) -> None:
    """#515 存储写重试耗尽与 #517 构造失败的 503 必须如实声明。

    审查 P2-2 修正：resume / messages / flush 的 launched 路径同样武装了两类
    503 臂——这些端点必须声明 503。本用例断言声明**在场**与 **schema 形状**与被声明
    端点真实返回的信封一致；「except 臂本身在场」由各端点自己的行为用例守
    （如 `tests/web/test_memory_api.py` 的写锁耗尽→503 用例），不在这里重复。

    #376-1 复审 STD-2：memory 族写端点的 503 **带机读码**（`{code, message}`，见
    ADR-0035 §3），所以它们声明成 `ErrorCodeEnvelope` 而不是字符串 `ErrorEnvelope`——
    两张表、两种 schema，各自与被声明端点真实返回的形状一致。
    """
    api = _openapi(_client(tmp_path))
    string_envelope = {
        ("/api/sessions/{session_id}/archive", "post"),
        ("/api/sessions/{session_id}/archive", "delete"),
        ("/api/sessions/{session_id}", "delete"),
        ("/api/sessions/{session_id}/permission", "post"),
        ("/api/sessions/{session_id}/forks", "post"),
        ("/api/sessions/{session_id}/resume", "post"),
        ("/api/sessions/{session_id}/messages", "post"),
        ("/api/sessions/{session_id}/queue/flush", "post"),
        ("/api/sessions", "post"),
        # projects 族写端点（修后重审 P2-A）：_translated() 的 StorageBusyError
        # 臂包住全部项目写操作——resolve/list/get 是读，不在其中。
        ("/api/projects", "post"),
        ("/api/projects/{project_id}", "patch"),
        ("/api/projects/{project_id}", "delete"),
        ("/api/projects/{project_id}/sessions", "post"),
        ("/api/projects/{project_id}/sessions/{session_id}", "delete"),
        ("/api/projects/{project_id}/sessions/{session_id}/order", "post"),
    }
    # #376-1：memory 族写端点。它们的 503 一律带码（重试耗尽的 storage_busy /
    # 派生索引待删 / 装配降级），schema 是 ErrorCodeEnvelope。
    coded_envelope = {
        ("/api/memories/{memory_id}", "delete"),
        ("/api/memories/{memory_id}", "patch"),
        ("/api/memories/bulk-delete", "post"),
        ("/api/memory-settings", "patch"),
    }
    assert "ErrorCodeEnvelope" in api["components"]["schemas"], (
        "带码信封的组件 schema 必须注册（memory 族 503 引用它）"
    )
    for path, method in string_envelope:
        op = api["paths"][path][method]
        assert "503" in op["responses"], f"{method.upper()} {path} 未声明 503"
        schema = op["responses"]["503"]["content"]["application/json"]["schema"]
        assert schema.get("$ref") == "#/components/schemas/ErrorEnvelope", (
            f"{method.upper()} {path} 的 503 schema：{schema}"
        )
    for path, method in coded_envelope:
        op = api["paths"][path][method]
        assert "503" in op["responses"], f"{method.upper()} {path} 未声明 503"
        schema = op["responses"]["503"]["content"]["application/json"]["schema"]
        assert schema.get("$ref") == "#/components/schemas/ErrorCodeEnvelope", (
            f"{method.upper()} {path} 的 503 应为带码信封：{schema}"
        )


def test_openapi_declares_event_stream_on_flush_resume_messages(tmp_path: Path) -> None:
    """审查 P2-2：resume / messages / flush 的 launched 路径回 SSE，必须声明。"""
    api = _openapi(_client(tmp_path))
    for path, method in (
        ("/api/sessions/{session_id}/resume", "post"),
        ("/api/sessions/{session_id}/messages", "post"),
        ("/api/sessions/{session_id}/queue/flush", "post"),
    ):
        op = api["paths"][path][method]
        assert "text/event-stream" in op["responses"]["200"]["content"], (
            f"{method.upper()} {path} 200 未声明 text/event-stream"
        )


def test_flush_route_maps_construction_failure_to_503(tmp_path: Path) -> None:
    """审查 P2-1：flush 在 idle 时走 resume_and_launch 构造 client——BUG-05
    的第 4 个构造调用点。代理坏环境下投递排队消息不再裸 500。

    直接 patch service 方法（真实构造链已由 create 的 503 测试端到端证明）：
    会话不存在时会先 404，测不到本臂。
    """
    client = _client(tmp_path)
    with patch(
        "agent_harness.session.service.SessionService.deliver_next_undelivered",
        new_callable=AsyncMock,
        side_effect=ModelClientConstructionError(
            "模型 client 构造失败（代理/网络环境）：Invalid port: ':1]'"
        ),
    ):
        resp = client.post("/api/sessions/abc123/queue/flush")
    assert resp.status_code == 503, resp.text
    assert "Invalid port" in resp.json()["detail"]


def test_project_write_maps_storage_busy_to_503(tmp_path: Path) -> None:
    """审查 P2-3：workspace store 写锁耗尽 → 503（projects 路由面同款映射）。

    rename 的写路径是 `index.set_title → store.set_title`（无 begin_change，
    那是 create/delete 的崩溃标记协议）；patch 它让锁竞争在第一个写操作
    就耗尽，验证 `_translated()` 的新臂。
    """
    from agent_harness.storage.sqlite import StorageBusyError

    client = _client(tmp_path)
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    created = client.post("/api/projects", json={"path": str(proj_dir)})
    assert created.status_code == 200, created.text

    with patch(
        "agent_harness.workspace.store.SqliteWorkspaceStore.set_title",
        new_callable=AsyncMock,
        side_effect=StorageBusyError("SQLite 写锁竞争重试后仍超时"),
    ):
        resp = client.patch(
            f"/api/projects/{created.json()['id']}", json={"title": "renamed"}
        )
    assert resp.status_code == 503, resp.text
    assert "写锁" in resp.json()["detail"]


def test_unexpected_exception_returns_json_500(tmp_path: Path) -> None:
    """未映射异常 → 500 且 body 是 JSON 信封（原来 Starlette 默认是 text/plain）。"""
    client = _client(tmp_path, raise_server_exceptions=False)
    with patch(
        "agent_harness.session.service.SessionService.get_events",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        resp = client.get("/api/sessions/abc123/events")
    assert resp.status_code == 500
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {"detail": "Internal Server Error"}


@pytest.mark.parametrize(
    ("method", "path", "service_method", "kwargs"),
    [
        ("post", "/api/sessions?launch=true", "create_and_launch",
         {"json": {"task": "hi", "budget": {"local": {"max_agent_turns": 1}}}}),
        ("post", "/api/sessions/abc123/resume", "resume_and_launch",
         {"json": {"task": "hi"}}),
        ("post", "/api/sessions/abc123/messages", "send_message",
         {"json": {"content": "hi", "mode": "steer"}}),
        ("post", "/api/sessions/abc123/queue/flush", "deliver_next_undelivered", {}),
    ],
)
def test_launch_arms_map_storage_busy_to_503(
    tmp_path: Path, method: str, path: str, service_method: str, kwargs: dict
) -> None:
    """修后重审（Standards P3-2）：create/resume/messages/flush 四个新
    `StorageBusyError` 臂的行为红证——launch 路径写锁耗尽是 503，不裸 500。

    在 service 边界 patch（异常映射是路由层契约；真实重试链由
    test_sqlite_write_retry 的行为测试证明）。
    """
    from agent_harness.storage.sqlite import StorageBusyError

    client = _client(tmp_path)
    with patch(
        f"agent_harness.session.service.SessionService.{service_method}",
        new_callable=AsyncMock,
        side_effect=StorageBusyError("SQLite 写锁竞争重试后仍超时"),
    ):
        resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 503, resp.text
    assert "写锁" in resp.json()["detail"]


def _sqlite_storage_error(code: int, name: str, message: str) -> sqlite3.Error:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    error.sqlite_errorname = name
    return error


@pytest.mark.parametrize(
    "storage_error",
    [
        OSError(errno.EFBIG, "File too large", "private/backend/path"),
        OSError(errno.ENOSPC, "No space left on device", "private/backend/path"),
        _sqlite_storage_error(
            sqlite3.SQLITE_FULL, "SQLITE_FULL", "database or disk is full"
        ),
        _sqlite_storage_error(sqlite3.SQLITE_IOERR, "SQLITE_IOERR", "disk I/O error"),
        _sqlite_storage_error(
            sqlite3.SQLITE_IOERR_WRITE, "SQLITE_IOERR_WRITE", "disk I/O error"
        ),
    ],
    ids=["efbig", "enospc", "sqlite-full", "sqlite-ioerr", "sqlite-ioerr-write"],
)
def test_messages_storage_errors_map_to_503_before_side_effects(
    tmp_path: Path, storage_error: Exception
) -> None:
    """#569：错误族在真实逃逸点做分类注入；不是物理满盘模拟。"""
    from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger

    client = _client(tmp_path, raise_server_exceptions=False)
    created = client.post("/api/sessions?launch=false", json={})
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]
    events = tmp_path / "sessions" / session_id / "events.jsonl"
    events_before = events.read_bytes()
    operation_ledger = client.app.state.agent.operation_ledger
    operations_before = anyio.run(operation_ledger.list_for_session, session_id)
    budget_ledger = client.app.state.agent.stores.delegation_tree_ledger
    budget_before = anyio.run(budget_ledger.get_session_budget, session_id)
    budget_events_before = anyio.run(budget_ledger.session_event_kinds, session_id)

    with (
        patch.object(
            SqliteDelegationTreeLedger,
            "get_session_budget",
            new_callable=AsyncMock,
            side_effect=storage_error,
        ) as budget_read,
        patch("agent_harness.assembly.create_chat_model") as model_factory,
    ):
        response = client.post(
            f"/api/sessions/{session_id}/messages", json={"content": "continue"}
        )

    assert budget_read.await_count == 1
    assert model_factory.call_count == 0
    assert events.read_bytes() == events_before
    assert anyio.run(operation_ledger.list_for_session, session_id) == operations_before
    assert anyio.run(budget_ledger.get_session_budget, session_id) == budget_before
    assert anyio.run(budget_ledger.session_event_kinds, session_id) == budget_events_before
    assert response.status_code == 503, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Persistent storage is unavailable"}


def test_messages_unclassified_operational_error_remains_500(tmp_path: Path) -> None:
    from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger

    client = _client(tmp_path, raise_server_exceptions=False)
    created = client.post("/api/sessions?launch=false", json={})
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]

    with patch.object(
        SqliteDelegationTreeLedger,
        "get_session_budget",
        new_callable=AsyncMock,
        side_effect=sqlite3.OperationalError("no such table: broken_path"),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/messages", json={"content": "continue"}
        )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Internal Server Error"}
