"""#785：CORS expose_headers 守卫——自定义响应头对前端 JS 的可见性契约。

机制：starlette CORSMiddleware 仅在 ``expose_headers`` 非空时下发
``Access-Control-Expose-Headers``（cors.py:44-45）；缺省时浏览器对 JS 隐藏
全部自定义响应头。影响面：默认 dev 走 Vite proxy 同源（web/vite.config.ts
proxy），不经 CORS；本契约修的是 5173 直连 / 跨域部署拓扑下的可见性。
两个守卫：

① 行为钉：CORS 简单响应实际下发的暴露头清单必须与
   ``EXPOSED_CUSTOM_RESPONSE_HEADERS`` **精确相等**——钉的是 middleware
   配置与登记清单的漂移：缺失（新增头漏登记/漏喂 expose_headers → 跨域
   JS 不可见）与多出（没登记的头混进暴露配置）都算偏离契约。边界：某头
   登记进集合但无端点实际下发时本钉不红（"登记但不下发"需另设端点响应
   头实测，本守卫不覆盖）；
② 防漏登：``src/agent_harness/web/`` 全部模块源码里引号包裹的 ``X-…`` 头
   字面量必须登记进 ``EXPOSED_CUSTOM_RESPONSE_HEADERS``。域界定（有意为之）：
   - 库产出的 ``X-Accel-Buffering``（sse-starlette SSE 反缓冲头）刻意不登记——
     消费方是 nginx 等反代，不是前端 JS；
   - 守卫只认 X- 前缀——非 X- 自定义响应头（如 app.py CSPHeaderMiddleware
     产出的 ``Content-Security-Policy``，无 JS 消费者）不在此契约内。

请求侧读头先例目前为零；未来若出现，把该头加进 ``_NON_RESPONSE_X_HEADERS``
豁免映射并写明理由（dict 值即理由——结构强制，无理由写不进去）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.config import Settings
from agent_harness.web import app as app_module
from agent_harness.web.app import create_app

# 请求侧 X- 头豁免映射：头名 → 豁免理由（当前为空：web/ 无任何请求侧 X- 头
# 读取）。新增请求侧头时加入此映射并写明理由——结构强制：无理由写不进去
# （dict 值即理由）；响应头则必须登记进 EXPOSED_CUSTOM_RESPONSE_HEADERS。
_NON_RESPONSE_X_HEADERS: dict[str, str] = {}


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
    registered = set(app_module.EXPOSED_CUSTOM_RESPONSE_HEADERS)
    # 精确相等而非子集：多出 / 缺失都算偏离契约（#808 C-F2）。
    assert listed == registered, (
        f"多出 {sorted(listed - registered)} / 缺失 {sorted(registered - listed)}——"
        "Access-Control-Expose-Headers 必须与 EXPOSED_CUSTOM_RESPONSE_HEADERS "
        "精确相等，多出或缺失都算偏离契约"
    )


def _quoted_x_header_literals(web_dir: Path) -> dict[str, set[str]]:
    """收集 web 目录全部 ``*.py`` 源码里引号包裹的 ``X-…`` 头字面量（按文件分组）。

    单/双引号都认；``rglob`` 递归覆盖子包（排除 ``__pycache__``）。已知漏报
    口径：只认引号内完整字面量，f-string/拼接的动态头名（``f"X-Tenant-{tid}"``）
    不命中——动态头名处应显式登记或豁免，勿依赖本守卫。
    """
    literals: dict[str, set[str]] = {}
    for py_file in sorted(web_dir.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        source = py_file.read_text(encoding="utf-8")
        for match in re.finditer(r'[\'"](X-[A-Za-z0-9-]+)[\'"]', source):
            literals.setdefault(
                py_file.relative_to(web_dir).as_posix(), set()
            ).add(match.group(1))
    return literals


def test_every_x_header_literal_in_web_is_registered():
    registered = set(app_module.EXPOSED_CUSTOM_RESPONSE_HEADERS)
    exempted = set(_NON_RESPONSE_X_HEADERS)
    found = _quoted_x_header_literals(Path(app_module.__file__).parent)
    unregistered = [
        (file_name, header)
        for file_name, headers in found.items()
        for header in sorted(headers - registered - exempted)
    ]
    assert not unregistered, (
        f"web/ 模块源码出现未登记的 X- 头字面量: {sorted(unregistered)}——"
        "响应头请登记进 EXPOSED_CUSTOM_RESPONSE_HEADERS（#785）；"
        "请求侧读头请加进本文件的 _NON_RESPONSE_X_HEADERS 豁免映射并写明理由"
        "（豁免必须带理由字符串：dict 值即理由，无理由写不进去）"
    )
