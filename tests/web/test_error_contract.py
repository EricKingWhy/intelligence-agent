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

from pathlib import Path
from unittest.mock import AsyncMock, patch

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


def test_openapi_documents_503_on_newly_guaranteed_endpoints(
    tmp_path: Path,
) -> None:
    """#515 存储写重试耗尽与 #517 构造失败的 503 必须如实声明。"""
    api = _openapi(_client(tmp_path))
    expectations = {
        ("/api/sessions/{session_id}/archive", "post"),
        ("/api/sessions/{session_id}/archive", "delete"),
        ("/api/sessions/{session_id}", "delete"),
        ("/api/sessions/{session_id}/permission", "post"),
        ("/api/sessions/{session_id}/forks", "post"),
        ("/api/sessions", "post"),
    }
    for path, method in expectations:
        op = api["paths"][path][method]
        assert "503" in op["responses"], f"{method.upper()} {path} 未声明 503"
        schema = op["responses"]["503"]["content"]["application/json"]["schema"]
        assert schema.get("$ref", "").endswith("ErrorEnvelope"), (
            f"{method.upper()} {path} 的 503 schema：{schema}"
        )


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
