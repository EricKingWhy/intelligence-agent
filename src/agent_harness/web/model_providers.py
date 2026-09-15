"""#203 / ADR-0032：model-providers CRUD + 连接测试的路由（web 层薄适配）。

端点层只做 HTTP 语义（状态码 / 422 翻译 / 零密钥序列化）；领域逻辑在
`model/provider_store.py`（校验、凭据生命周期、合并规则）。所有响应**永不**
携带 api_key 字段或任何 key 子串（ADR-0032 §7.1；`has_api_key` 是唯一状态通道）。

连接测试（ADR-0032 §6）：**必须**走真实构造路径（`create_chat_model` +
`ainvoke`）——自拼 HTTP 测的是另一条线，会出现"测试通过但实际不能用"。
参数固定：max_tokens=1、不带 tools、超时 settings.model_test_timeout_seconds。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_harness.logging import log_event
from agent_harness.model.config import ModelConfig
from agent_harness.model.provider import create_chat_model
from agent_harness.model.provider_store import (
    CredentialError,
    ProviderStore,
    ProviderStoreError,
    validate_base_url,
)

logger = logging.getLogger("agent_harness.model")

#: 连接测试的固定参数（ADR-0032 §6.2，不得自行加料）。
_TEST_MAX_TOKENS = 1
_TEST_PROMPT = "ping"
_DETAIL_LIMIT = 500

#: 失败归类（ADR-0032 §6.3）→ UI 文案。key 不在映射里（原文可能含密钥/URL，
#: 摘要只截断不脱敏地回传是不可接受的——所以正文摘要**始终**经过
#: _redact_detail 过滤）。
_FAILURE_COPY: dict[str, str] = {
    "auth_failed": "认证失败：API Key 无效或无权限",
    "model_or_route_not_found": "模型或路径不存在：检查 Base URL 与模型名",
    "timeout": "连接超时",
    "rate_limited": "被限流（429）：配置本身可能是对的，稍后再试",
}


class ProviderPayload(BaseModel):
    """创建/更新请求体。`api_key` 只在此处出现（写路径是唯一能见明文的地方）。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    label: str = ""
    base_url: str
    models: list[dict[str, Any]]
    # None = 省略（不改密钥）；"" = 显式清除（ADR-0032 §7.1 第 2 条）。
    api_key: str | None = Field(default=None, max_length=4096)

    @field_validator("base_url")
    @classmethod
    def _base_url_scheme(cls, value: str) -> str:
        if not validate_base_url(value):
            raise ValueError(f"base_url 必须是 http/https URL: {value!r}")
        return value

    @field_validator("models")
    @classmethod
    def _models_non_empty(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not value:
            raise ValueError("models 必须是非空数组")
        for model in value:
            if not str(model.get("model_id", "")).strip():
                raise ValueError("models 每条必须有非空 model_id")
        return value

    @field_validator("id")
    @classmethod
    def _id_slug(cls, value: str | None) -> str | None:
        if value is not None and not re.match(r"^[a-z0-9][a-z0-9-_]{0,63}$", value):
            raise ValueError(f"provider id 必须是 slug（^[a-z0-9][a-z0-9-_]{{0,63}}$）: {value!r}")
        return value


class ProviderUpdatePayload(BaseModel):
    """更新请求体：id 不可改；字段全可选（省略 = 不改）。"""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    base_url: str | None = None
    models: list[dict[str, Any]] | None = None
    api_key: str | None = Field(default=None, max_length=4096)

    @field_validator("base_url")
    @classmethod
    def _base_url_scheme(cls, value: str | None) -> str | None:
        if value is not None and not validate_base_url(value):
            raise ValueError(f"base_url 必须是 http/https URL: {value!r}")
        return value


def _redact_detail(detail: str) -> str:
    """截断 500 字符 + 摘要脱敏（密钥形 token 打码；ADR-0032 §6.3）。

    错误正文可能回显请求头/URL query——凡是长得像 key 的子串一律打码，
    **绝不**依赖调用方"记得别把 key 放进异常"。
    """
    redacted = re.sub(r"(sk|pk|api[-_]?key)[^\s'\"]*", "***", detail, flags=re.IGNORECASE)
    return redacted[:_DETAIL_LIMIT]


def _classify_failure(error: Exception) -> tuple[str, str]:
    """异常 → (reason, UI 文案)。HTTP 状态从异常文本/类型归类（§6.3）。"""
    text = f"{type(error).__name__}: {error}"
    lower = text.lower()
    if "timeout" in lower or "timed out" in lower:
        return "timeout", _FAILURE_COPY["timeout"]
    if "429" in text or "rate limit" in lower:
        return "rate_limited", _FAILURE_COPY["rate_limited"]
    if "401" in text or "403" in text or "unauthorized" in lower or "forbidden" in lower:
        return "auth_failed", _FAILURE_COPY["auth_failed"]
    if "404" in text or "not found" in lower:
        return "model_or_route_not_found", _FAILURE_COPY["model_or_route_not_found"]
    if "authentication" in lower or "api key" in lower:
        return "auth_failed", _FAILURE_COPY["auth_failed"]
    return "network_error", f"网络不可达：{type(error).__name__}"


def _provider_store(request: Request) -> ProviderStore:
    return request.app.state.agent.provider_store


def register_model_provider_routes(app: FastAPI) -> None:
    """把 model-providers 路由挂到既有 app（create_app 里一行调用的接入面）。

    #172 同款来源闸：凭据读写是宿主侧不可逆/敏感操作，与项目/记忆端点共用
    同一条 `require_trusted_origin`，不复制安全规则。
    """
    from agent_harness.web.projects import require_trusted_origin

    @app.get("/api/model-providers")
    async def list_providers(request: Request) -> dict[str, Any]:
        """列出全部自定义供应商（含派生字段 kind / has_api_key / is_available / last_test）。

        **永不**返回 api_key 或任何 key 片段（§7.1 第 3 条：连末 4 位也不给）。
        不做网络探测（不变量 #21：列表接口不能被慢 provider 拖垮）。
        """
        store = _provider_store(request)
        providers = []
        for entry in store.list_entries():
            item = dict(entry)
            # 凭据状态以 has_api_key 为准（前端不维护第二套真相，#22）。
            item["has_api_key"] = store.credentials.get(entry["id"]) is not None
            providers.append(item)
        return {"providers": providers}

    @app.post("/api/model-providers", status_code=201)
    async def create_provider(request: Request, payload: ProviderPayload,
                             _: None = Depends(require_trusted_origin)) -> dict[str, Any]:
        """创建（id 冲突 = 覆盖更新；与内置同名 = 覆盖内置，ADR-0032 §3.2）。"""
        store = _provider_store(request)
        if payload.id is None:
            raise HTTPException(status_code=422, detail="缺少 provider id")
        try:
            entry = store.create(payload.model_dump())
        except CredentialError as error:
            # 无可用凭据后端：503 + 可读原因（§7.3），绝不落明文。
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ProviderStoreError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        item = dict(entry)
        item["has_api_key"] = payload.api_key is not None and payload.api_key != ""
        log_event(logger, "system_log", "供应商已创建", component="model_provider",
                  outcome="provider_created", provider_id=payload.id)
        return item

    @app.put("/api/model-providers/{provider_id}")
    async def update_provider(provider_id: str, request: Request,
                              payload: ProviderUpdatePayload,
                              _: None = Depends(require_trusted_origin)) -> dict[str, Any]:
        """更新（api_key 省略 = 不改；空串 = 显式清除凭据）。"""
        store = _provider_store(request)
        patch = payload.model_dump(exclude_none=True, exclude_unset=True)
        try:
            entry = store.update(provider_id, patch)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=f"供应商 {provider_id} 不存在") from error
        except CredentialError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ProviderStoreError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        item = dict(entry)
        if payload.api_key is None:
            item["has_api_key"] = store.credentials.get(provider_id) is not None
        else:
            item["has_api_key"] = payload.api_key != ""
        return item

    @app.delete("/api/model-providers/{provider_id}")
    async def delete_provider(provider_id: str, request: Request,
                             _: None = Depends(require_trusted_origin)) -> dict[str, str]:
        """删除（先删凭据、失败中止；被删 provider 的会话下一轮明确报错，不静默 fallback）。"""
        store = _provider_store(request)
        try:
            store.delete(provider_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=f"供应商 {provider_id} 不存在") from error
        except CredentialError as error:
            # 凭据删除失败 ⇒ 中止，配置保持原样（D6）。
            raise HTTPException(status_code=500, detail=str(error)) from error
        log_event(logger, "system_log", "供应商已删除", component="model_provider",
                  outcome="provider_deleted", provider_id=provider_id)
        return {"status": "deleted"}

    @app.post("/api/model-providers/{provider_id}/test")
    async def test_provider(provider_id: str, request: Request,
                           _: None = Depends(require_trusted_origin)) -> dict[str, Any]:
        """连接测试：走**真实构造路径** + 一次最小 chat completion（§6）。

        成功判定 = 调用返回且没抛异常（内容为空也算成功——本测试只证明
        "认证 + 地址 + 模型名可用"）。结果写回 last_test（非密）。
        """
        import asyncio
        import time

        store = _provider_store(request)
        try:
            entry = store.get_entry(provider_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=f"供应商 {provider_id} 不存在") from error
        api_key = store.credentials.get(provider_id)
        if not api_key:
            raise HTTPException(status_code=400, detail="未配置 API Key")
        model_id = entry["models"][0]["model_id"]

        settings = request.app.state.agent.settings
        started = time.monotonic()
        try:
            config = ModelConfig(
                provider=provider_id, model_name=model_id, api_key=api_key,
                base_url=entry["base_url"], temperature=settings.temperature,
            )
            # 真实构造路径（§6.1）：create_chat_model + ainvoke，max_tokens=1、
            # 不带 tools。request_timeout 用测试专用超时（15s，§6.2）。
            model = create_chat_model(
                config, request_timeout=settings.model_test_timeout_seconds,
            )
            from langchain_core.messages import HumanMessage

            await asyncio.wait_for(
                model.ainvoke([HumanMessage(content=_TEST_PROMPT)],
                              max_tokens=_TEST_MAX_TOKENS),
                timeout=settings.model_test_timeout_seconds,
            )
        except Exception as error:  # noqa: BLE001 — 失败必须归类成可读结果
            reason, copy = _classify_failure(error)
            detail = _redact_detail(str(error))
            elapsed = int((time.monotonic() - started) * 1000)
            log_event(logger, "system_log", "连接测试失败", component="model_provider",
                      outcome="provider_test_failed", provider_id=provider_id,
                      reason=reason, duration_ms=elapsed)
            store.update(provider_id, {"last_test": {
                "ok": False, "at": entry.get("updated_at"), "reason": reason, "detail": detail,
            }})
            return {"ok": False, "reason": reason, "detail": detail,
                    "message": _FAILURE_COPY.get(reason, copy), "duration_ms": elapsed}
        elapsed = int((time.monotonic() - started) * 1000)
        log_event(logger, "system_log", "连接测试通过", component="model_provider",
                  outcome="provider_test_ok", provider_id=provider_id, duration_ms=elapsed)
        store.update(provider_id, {"last_test": {
            "ok": True, "at": entry.get("updated_at"), "detail": f"200 · {elapsed}ms",
        }})
        return {"ok": True, "message": f"连接正常 · {elapsed}ms", "duration_ms": elapsed}



__all__ = [
    "ProviderPayload",
    "ProviderUpdatePayload",
    "register_model_provider_routes",
]
