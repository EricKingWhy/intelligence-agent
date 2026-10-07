"""#822 / MM-01：附件上传 + 受控读回的 HTTP 契约。

契约矩阵
--------

| 条件 | 结果 |
| --- | --- |
| 上传合法图片（octet-stream） | 200 `{attachment_id, media_type, bytes, width, height, name}` |
| `attachment_id` | 形如 `sha256:<64hex>`，不含路径 / URL |
| 同字节重复上传 | 同一 id |
| 读回本会话 id | 200 原始字节 + 正确 Content-Type |
| 读从未上传的 id | 404 |
| 读别的会话的 id | 404（不泄露存在性） |
| id 形态非法 | 422 |
| 会话不存在 | 404 |
| 声明类型 / 扩展名与字节不符 | 422 |
| 非图片字节 | 422 |
| 超单张字节上限 | 413，不留存储残留 |
| 上传路由不受 1 MiB JSON body 上限约束 | 200 放行 |
| 既有 JSON 端点的 1 MiB 行为 | 逐字不变（413 + 同 detail） |

MM-01 授权口径的**已知缺口**（登记，非本票覆盖）：本票读端点的授权单位 = **会话命名空间
归属**（"上传即归属本会话"），不是 PRD D5 的"被本 Session 事件引用"——MM-01 没有任何
"事件引用附件"的机制（那是 MM-02）。故"已上传但**从未被消息引用**的 id → 404"这条 AC
在本票**未达成**，由 **MM-02** 补回事件引用闸门（见 `web/attachments.py` 模块 docstring
与 `docs/tickets/multimodal-2026-10-07/MM-02-model-sees-image.md`）。本文件的
`test_never_uploaded_id_is_404` 只覆盖"从未上传"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.web.app import create_app
from agent_harness.web.wire_safety import BODY_TOO_LARGE_DETAIL
from tests.attachments.helpers import jpeg_bytes, large_png_bytes, png_bytes
from tests.scripted_model import ScriptedModel

_DATA_PREFIX = "data:"
_OCTET = {"content-type": "application/octet-stream"}
_JSON_HDR = {"content-type": "application/json"}


def _client(tmp_path: Path, **overrides: Any) -> TestClient:
    # 所有用例都把附件落盘根指向 tmp_path：默认 `.agent/artifacts` 是相对进程 CWD
    # 解析的（LocalArtifactStore 对相对路径做 resolve()），会让测试写穿到仓库根、
    # 残留对象跨轮累积（ADR-0038 同族的测试隔离纪律）。overrides 不得再传 artifact_dir。
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        model_api_key="sk-test",
        **overrides,
    )
    return TestClient(create_app(settings, enable_cors=False))


def _create_session(client: TestClient) -> str:
    with patch(
        "agent_harness.assembly.create_chat_model",
        return_value=ScriptedModel(responses=[AIMessage(content="ok")]),
    ):
        resp = client.post(
            "/api/sessions",
            json={"task": "hi", "budget": {"local": {"max_agent_turns": 1}}},
        )
    assert resp.status_code == 200, resp.text
    frames = [
        json.loads(line[len(_DATA_PREFIX) :].strip())
        for line in resp.text.splitlines()
        if line.startswith(_DATA_PREFIX)
    ]
    session_id = next((f["session_id"] for f in frames if f.get("session_id")), None)
    assert session_id, f"SSE 流里没有 session_id：{frames[:3]}"
    return str(session_id)


def _upload(
    client: TestClient, session_id: str, data: bytes, *, name: str | None = None,
    headers: dict[str, str] | None = None,
):
    params = {"name": name} if name is not None else None
    return client.post(
        f"/api/sessions/{session_id}/attachments",
        content=data,
        headers=headers or _OCTET,
        params=params,
    )


def test_upload_then_read_back_is_byte_equal(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_id = _create_session(client)
    payload = png_bytes(640, 480)

    up = _upload(client, session_id, payload, name="cat.png")
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["media_type"] == "image/png"
    assert body["bytes"] == len(payload)
    assert body["width"] == 640
    assert body["height"] == 480
    assert body["name"] == "cat.png"
    attachment_id = body["attachment_id"]
    # id 是不透明内容哈希：无路径、无裸 URL。
    assert attachment_id.startswith("sha256:")
    assert len(attachment_id) == len("sha256:") + 64
    assert "/" not in attachment_id and "://" not in attachment_id

    got = client.get(
        f"/api/sessions/{session_id}/attachments/{attachment_id}/content"
    )
    assert got.status_code == 200, got.text
    assert got.content == payload
    assert got.headers["content-type"] == "image/png"


def test_duplicate_upload_yields_same_id(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_id = _create_session(client)
    payload = png_bytes(10, 10)

    first = _upload(client, session_id, payload).json()["attachment_id"]
    second = _upload(client, session_id, payload).json()["attachment_id"]
    assert first == second


def test_never_uploaded_id_is_404(tmp_path: Path) -> None:
    """**从未上传过**的合法形态 id → 404。

    注意名字的诚实性：它**不**测 AC 里"已上传但从未被消息引用 → 404"那条——MM-01 没有
    事件引用机制，那条语义未达成、缺口登记为 MM-02 覆盖项（见模块 docstring）。另见
    `test_other_session_id_is_404`（跨会话 404 与之不可区分）。
    """
    client = _client(tmp_path)
    session_id = _create_session(client)
    resp = client.get(
        f"/api/sessions/{session_id}/attachments/sha256:{'0' * 64}/content"
    )
    assert resp.status_code == 404, resp.text


def test_other_session_id_is_404(tmp_path: Path) -> None:
    """别的会话拿到 id 也取不到内容，且与"从未上传"不可区分（不泄露存在性）。"""
    client = _client(tmp_path)
    session_a = _create_session(client)
    session_b = _create_session(client)
    assert session_a != session_b
    attachment_id = _upload(client, session_a, png_bytes(20, 20)).json()["attachment_id"]

    own = client.get(
        f"/api/sessions/{session_a}/attachments/{attachment_id}/content"
    )
    other = client.get(
        f"/api/sessions/{session_b}/attachments/{attachment_id}/content"
    )

    assert own.status_code == 200, own.text
    assert other.status_code == 404, "attachment_id 无归属信息，隔离只能靠 store 的会话命名空间"
    # 不泄露存在性：两种 404 的文案形状必须一致（回显的 id 是调用方已知的，需归一化后比）。
    unreferenced = client.get(
        f"/api/sessions/{session_b}/attachments/sha256:{'0' * 64}/content"
    )
    assert unreferenced.status_code == 404
    template = other.json()["detail"].replace(attachment_id, "<id>")
    assert template == unreferenced.json()["detail"].replace(
        f"sha256:{'0' * 64}", "<id>"
    ), "跨会话 404 与未上传 404 的文案形状必须一致"


def test_malformed_attachment_id_is_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_id = _create_session(client)
    resp = client.get(f"/api/sessions/{session_id}/attachments/not-an-id/content")
    assert resp.status_code == 422, resp.text


def test_unknown_session_is_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/sessions/nosuchsession/attachments/sha256:{}/content".format("0" * 64))
    assert resp.status_code == 404, resp.text


def test_fake_extension_is_rejected(tmp_path: Path) -> None:
    """声明成 .png 但字节是 JPEG → 拒绝（按字节判定，不信声明）。"""
    client = _client(tmp_path)
    session_id = _create_session(client)
    resp = _upload(client, session_id, jpeg_bytes(10, 10), name="looks_like.png")
    assert resp.status_code == 422, resp.text


def test_declared_content_type_mismatch_is_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_id = _create_session(client)
    resp = _upload(
        client,
        session_id,
        png_bytes(10, 10),
        headers={"content-type": "image/jpeg"},
    )
    assert resp.status_code == 422, resp.text


def test_declared_unsupported_image_type_mismatch_is_rejected(tmp_path: Path) -> None:
    """声明成**非支持**的 `image/*`（svg+xml）而字节是 PNG → 也拒绝（不是只查支持集）。

    边界收紧的判别点：若只在"声明 ∈ 支持集"时才比对，`image/svg+xml` 会被当成 None
    而静默接受 PNG 字节。这里按 `image/` 前缀一律比对声明 vs 字节。
    """
    client = _client(tmp_path)
    session_id = _create_session(client)
    resp = _upload(
        client,
        session_id,
        png_bytes(10, 10),
        headers={"content-type": "image/svg+xml"},
    )
    assert resp.status_code == 422, resp.text


def test_non_image_bytes_are_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_id = _create_session(client)
    resp = _upload(client, session_id, b"<<not an image>>", name="x.png")
    assert resp.status_code == 422, resp.text


def test_over_single_image_limit_is_413_and_leaves_no_residue(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    client = _client(tmp_path, attachment_max_image_bytes=1000)
    session_id = _create_session(client)

    resp = _upload(client, session_id, large_png_bytes(4, 4, 2000))

    assert resp.status_code == 413, resp.text
    session_dir = artifact_dir / session_id / "attachments"
    leftovers = (
        [p for p in session_dir.rglob("*") if p.is_file()] if session_dir.exists() else []
    )
    assert leftovers == [], "超限不得留下任何存储残留"


def test_upload_route_is_exempt_from_json_body_cap(tmp_path: Path) -> None:
    """上传路由可达 >1 MiB（否则 20 MiB 单张上限形同虚设）。"""
    client = _client(tmp_path)
    session_id = _create_session(client)
    payload = large_png_bytes(4, 4, 1_500_000)

    up = _upload(client, session_id, payload)
    assert up.status_code == 200, up.text
    attachment_id = up.json()["attachment_id"]

    got = client.get(
        f"/api/sessions/{session_id}/attachments/{attachment_id}/content"
    )
    assert got.status_code == 200
    assert got.content == payload


def test_json_endpoint_body_cap_is_unchanged(tmp_path: Path) -> None:
    """豁免只针对上传路由：既有 JSON 端点 >1 MiB 仍是 413 + 同一 detail（回归）。"""
    client = _client(tmp_path)
    oversized = b"{}" + b" " * (1024 * 1024 + 1)

    resp = client.post("/api/sessions?launch=false", content=oversized, headers=_JSON_HDR)

    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"] == BODY_TOO_LARGE_DETAIL
