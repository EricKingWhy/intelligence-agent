"""#517 BUG-06：OpenAPI 错误面对齐现实（`apply_error_contract`）。

FastAPI 的自动 schema 有四处与真实响应体不符：

1. **422 形态**：一律声明成 `HTTPValidationError`（`detail` 是**数组**），但
   业务路径经 `http_error` 抛出的 422 是 `{"detail": "<字符串>"}` 信封——
   按 spec 生成的强类型客户端解析业务 422 直接失败。修法：已有 422 声明
   改成 oneOf（原形态 ∪ 信封），两种都如实声明。
2. **SSE content-type**：流式路由的 200 只声明 `application/json`，真实体是
   `text/event-stream`（票面：调用方按文档解码 3 次 Undocumented CT）。
3. **500 缺声明**：/api 操作一个 500 都不声明，而任何 handler 都可能漏出
   未映射异常（全局 handler 兜底成 JSON 信封）——未声明的 500 让客户端按
   "永不出错"写解析。另把 #515/#517 已保证的 503 逐端点补上。
4. **413 缺声明**（#562 残余）：`BodyDepthGuardMiddleware` 经 `add_middleware`
   全局挂载，对每个请求累计 body 字节，任何带体请求都可能超 1 MiB 回 413——
   未声明的 413 让客户端按"体积永不越界"写解析。与 503（依赖 handler 的
   except 元组、静态后处理推断不了）不同，413 的声明面**可机械判定**：方法族
   语义上携带请求体（POST/PUT/PATCH/DELETE）的 /api 操作全部如实补上。

## 为什么是 openapi 后处理而不是逐路由 responses={}

逐路由加 `responses=` 参数要动 ~40 个 handler，且每个新路由还得记得补；
后处理在一处统一补齐，FastAPI 自己生成的部分原样保留。挂载用 `app.openapi`
包装（FastAPI 文档的 custom openapi 模式）：首次调用跑后处理并写
`app.openapi_schema`，后续走 FastAPI 既有缓存语义。

## 只动声明、不动真实响应体

BUG-06 的修法是**如实声明**，不是改形状：业务 4xx 仍是 detail 字符串信封，
前端消费不变；改统一响应形状是另一票的事。同理本模块**只增不删**——
`GET .../stream` 的 200 真实恒为 SSE，但 FastAPI 生成的 `application/json`
声明原样保留（删声明是收缩契约，不混进本票）。

## 刻意不做

- 不给没声明 422 的操作补 422：哪些操作会出业务 422 由各 handler 的
  except 元组决定，静态后处理推断不了，硬补是二次谎报。
- 503 只声明在真实会出现的端点上（#515 的存储写锁耗尽 + #517 的模型
  构造失败两条根因各自武装的 15 个字符串信封端点，外加 #376-1 memory 族
  4 个**带机读码**端点，见 `_503_CODED_OPERATIONS`），不全局撒。
"""

from __future__ import annotations

from typing import Any

_ENVELOPE_REF = "#/components/schemas/ErrorEnvelope"

#: 业务错误信封：`http_error` / 全局 500 handler / Starlette 默认 500 的
#: 共同形状——`{"detail": "<字符串>"}`。组件 schema 由本模块注册。
_ERROR_ENVELOPE_SCHEMA: dict[str, Any] = {
    "title": "ErrorEnvelope",
    "type": "object",
    "required": ["detail"],
    "properties": {"detail": {"title": "Detail", "type": "string"}},
}

_ENVELOPE = {"$ref": _ENVELOPE_REF}
_CODE_ENVELOPE_REF = "#/components/schemas/ErrorCodeEnvelope"

#: 带机读码的错误信封：`{"detail": {"code": "<码>", "message": "<文本>"}}`。memory 族的 503
#: 用这个形状（装配降级 `{code: <DegradeReason>}` / `memory_index_delete_pending` /
#: #376-1 的 `storage_busy`）——同一状态码下已有多个原因 ⇒ 按 ADR-0035 §3 必须带码。
#: 组件 schema 由本模块注册。
_ERROR_CODE_ENVELOPE_SCHEMA: dict[str, Any] = {
    "title": "ErrorCodeEnvelope",
    "type": "object",
    "required": ["detail"],
    "properties": {
        "detail": {
            "title": "Detail",
            "type": "object",
            "required": ["code", "message"],
            "properties": {
                "code": {"title": "Code", "type": "string"},
                "message": {"title": "Message", "type": "string"},
            },
        }
    },
}
_CODE_ENVELOPE = {"$ref": _CODE_ENVELOPE_REF}
_HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

#: 语义上携带请求体的方法族——413 的声明面。`BodyDepthGuardMiddleware` 对每个
#: HTTP 请求累计 body 字节（全局挂载，机制可机械判定），这些方法的 /api 操作
#: 都可能超 1 MiB 回 413；GET/HEAD 等本仓不消费请求体，不撒网。
_BODY_BEARING_METHODS = frozenset({"post", "put", "patch", "delete"})

#: (path, method) → 该操作真实可能返回 503 的声明清单（**字符串 detail** 信封）。
#: #515：存储写重试耗尽（`StorageBusyError` → 503）已武装的写端点——
#: sessions 族 8 个 + projects 族 6 个写端点（`_translated()` 的
#: StorageBusyError 臂包住全部项目写操作，修后重审 P2-A）；
#: #517：会构造模型 client 的端点（`ModelClientConstructionError` → 503）。
_503_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("/api/sessions/{session_id}/archive", "post"),
        ("/api/sessions/{session_id}/archive", "delete"),
        ("/api/sessions/{session_id}", "delete"),
        ("/api/sessions/{session_id}/permission", "post"),
        ("/api/sessions/{session_id}/forks", "post"),
        ("/api/sessions/{session_id}/resume", "post"),
        ("/api/sessions/{session_id}/messages", "post"),
        ("/api/sessions/{session_id}/queue/flush", "post"),
        ("/api/sessions", "post"),
        ("/api/projects", "post"),
        ("/api/projects/{project_id}", "patch"),
        ("/api/projects/{project_id}", "delete"),
        ("/api/projects/{project_id}/sessions", "post"),
        ("/api/projects/{project_id}/sessions/{session_id}", "delete"),
        ("/api/projects/{project_id}/sessions/{session_id}/order", "post"),
    }
)

#: (path, method) → 真实可能返回 503 且**带机读码**（`ErrorCodeEnvelope`）的写端点。
#: #376-1：memory-v2 族 4 个写端点。它们的 503 一律是 `{code, message}` 形状——重试耗尽
#: 的 `storage_busy`、派生索引待删的 `memory_index_delete_pending`、装配降级的
#: `<DegradeReason>`——而非 #515 那种字符串 detail，所以**另立一张表**，声明形状与
#: `_503_OPERATIONS` 不同（ADR-0035；不加进上面那张会让声明与真实体不符）。
_503_CODED_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("/api/memories/{memory_id}", "delete"),
        ("/api/memories/{memory_id}", "patch"),
        ("/api/memories/bulk-delete", "post"),
        ("/api/memory-settings", "patch"),
    }
)

#: 200 恒为（或可为）`text/event-stream` 的流式路由。`POST /api/sessions`
#: 的 `application/json`（launch=false 只建路径）由 FastAPI 已声明，保留。
_SSE_200_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("/api/sessions", "post"),
        ("/api/sessions/{session_id}/stream", "get"),
        ("/api/sessions/{session_id}/resume", "post"),
        ("/api/sessions/{session_id}/messages", "post"),
        ("/api/sessions/{session_id}/queue/flush", "post"),
    }
)


def _json_envelope_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": dict(_ENVELOPE)}},
    }


def _json_code_envelope_response(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": dict(_CODE_ENVELOPE)}},
    }


def _post_process(schema: dict[str, Any]) -> dict[str, Any]:
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    components.setdefault("ErrorEnvelope", dict(_ERROR_ENVELOPE_SCHEMA))
    components.setdefault("ErrorCodeEnvelope", dict(_ERROR_CODE_ENVELOPE_SCHEMA))

    for path, path_item in schema.get("paths", {}).items():
        if not path.startswith("/api/"):
            continue
        for method, op in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            responses = op.setdefault("responses", {})
            # 500：任何 /api 操作都可能漏出未映射异常（全局 handler 只换
            # 表示不改状态码），未声明会让客户端按"永不出错"写解析。
            responses.setdefault(
                "500", _json_envelope_response("Internal Server Error")
            )
            # 413（#562 残余）：体积守卫中间件全局挂载，带体方法族如实声明
            # （detail 是固定字符串，与 ErrorEnvelope 同形）。
            if method in _BODY_BEARING_METHODS:
                responses.setdefault(
                    "413", _json_envelope_response("Request Body Too Large")
                )
            if (path, method) in _503_OPERATIONS:
                responses.setdefault(
                    "503", _json_envelope_response("Service Unavailable")
                )
            if (path, method) in _503_CODED_OPERATIONS:
                responses.setdefault(
                    "503", _json_code_envelope_response("Service Unavailable")
                )
            # 422：FastAPI 的校验数组形态与业务字符串信封并存——oneOf 二选一。
            # 只改已有声明的 schema；没有 422 声明的操作不发明。
            declared_422 = responses.get("422")
            if declared_422 is not None:
                media = (
                    declared_422.setdefault("content", {})
                    .setdefault("application/json", {})
                )
                original = media.get("schema")
                if original is None:
                    media["schema"] = dict(_ENVELOPE)
                elif "oneOf" not in original and original != _ENVELOPE:
                    media["schema"] = {"title": "ValidationError or ErrorEnvelope",
                                       "oneOf": [original, dict(_ENVELOPE)]}
            if (path, method) in _SSE_200_OPERATIONS:
                ok = responses.get("200")
                if ok is not None:
                    ok.setdefault("content", {}).setdefault(
                        "text/event-stream", {"schema": {}}
                    )
    return schema


def apply_error_contract(app: Any) -> None:
    """把上文的错误面声明后处理挂到 app 的 openapi() 上（幂等，只增不删）。"""
    original_openapi = app.openapi

    def openapi_with_error_contract() -> dict[str, Any]:
        if not app.openapi_schema:
            app.openapi_schema = _post_process(original_openapi())
        return app.openapi_schema

    app.openapi = openapi_with_error_contract
