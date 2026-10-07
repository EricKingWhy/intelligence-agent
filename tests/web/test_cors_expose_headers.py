"""#785：CORS expose_headers 守卫——跨域 dev 下自定义响应头对 JS 必须可见。

背景：starlette CORSMiddleware 仅在 ``expose_headers`` 非空时下发
``Access-Control-Expose-Headers``（cors.py:44-45）；缺省时浏览器对 JS 隐藏
全部自定义响应头（同源部署不受影响）。两个守卫：

① 行为钉：CORS 简单响应必须暴露 app 全部自定义响应头；
② 防漏登：app.py 源码里的 ``"X-…"`` 头字面量必须全部登记进
   ``EXPOSED_CUSTOM_RESPONSE_HEADERS``。请求侧读头先例目前为零；未来若
   出现，把该头加进 ``_NON_RESPONSE_X_HEADERS`` 豁免集并写明理由。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web import app as app_module
from agent_harness.web.app import create_app

# 请求侧 X- 头豁免集（当前为空：app.py 无任何请求侧 X- 头读取；
# 新增请求侧头时加入此集合并写明理由，响应头则必须登记进
# EXPOSED_CUSTOM_RESPONSE_HEADERS）。
_NON_RESPONSE_X_HEADERS: frozenset[str] = frozenset()


@pytest.fixture
def cors_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
        model_provider="deepseek",
        model_name="deepseek-chat",
        provider_store_path=str(tmp_path / "model-providers.json"),
    )
    return TestClient(create_app(settings, enable_cors=True))


def test_cors_simple_response_exposes_all_custom_headers(cors_client: TestClient):
    resp = cors_client.get(
        "/api/models", headers={"Origin": "http://localhost:5173"}
    )
    assert resp.status_code == 200
    exposed = resp.headers.get("access-control-expose-headers")
    assert exposed is not None, (
        "CORS 简单响应缺 Access-Control-Expose-Headers——跨域 dev 下"
        "自定义响应头对 JS 全部不可见（#785）"
    )
    listed = {header.strip() for header in exposed.split(",")}
    assert set(app_module.EXPOSED_CUSTOM_RESPONSE_HEADERS) <= listed


def test_every_x_header_literal_in_app_is_registered():
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    found = set(re.findall(r'"(X-[A-Za-z0-9-]+)"', source))
    registered = set(app_module.EXPOSED_CUSTOM_RESPONSE_HEADERS)
    unregistered = found - registered - _NON_RESPONSE_X_HEADERS
    assert not unregistered, (
        f"app.py 出现未登记的 X- 头字面量: {sorted(unregistered)}——"
        "响应头请登记进 EXPOSED_CUSTOM_RESPONSE_HEADERS（#785）；"
        "请求侧读头请加进本文件的 _NON_RESPONSE_X_HEADERS 豁免集并写明理由"
    )
