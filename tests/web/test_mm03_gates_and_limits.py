"""#824 / MM-03：发送端点的聚合上限、视觉门禁与双保险（AC1 / AC3 / AC4 / AC6）。

契约矩阵
--------
| 条件 | 结果 |
| --- | --- |
| 引用图片数量 > 部署 `attachment_max_images_per_message` | 422，零事件 |
| 引用图片总字节 > 部署 `attachment_max_message_image_bytes` | 413，零事件 |
| 会话（或本条一次性覆盖的）模型不支持视觉却附图 | 422，零事件 |
| 本条一次性改选视觉模型 + 附图 | 200，事件带引用、请求带 image_url |
| `/api/models` 暴露 `supports_vision`（声明模型 True，未声明模型省略） | 200 |
| 入口被绕过（发送后切到非视觉模型）| 投影降级为占位符，不发 image_url |

上限键取 DSH 一组（默认 20 张 / 200 MiB），本文件按需调小以触达失败路径。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from agent_harness.attachments.projection import IMAGE_OMITTED_PLACEHOLDER
from agent_harness.config import Settings
from agent_harness.web.app import create_app
from tests.attachments.helpers import large_png_bytes, png_bytes
from tests.scripted_model import ScriptedModel

_DATA_PREFIX = "data:"
_OCTET = {"content-type": "application/octet-stream"}

#: 两个 probe：一个显式声明支持视觉，一个显式声明不支持。provider 都用 deepseek
#: （预设不声明视觉 ⇒ 只有 catalog 显式声明才可能为 True）。**model_name 必须互异**：
#: `model_supports_vision` 按 (provider, model_name) 反查 catalog，同名会让两条目
#: 互相遮蔽（命中先声明的那条）。
_CATALOG = json.dumps(
    [
        {
            "name": "vision-probe",
            "provider": "deepseek",
            "model_name": "vision-upstream",
            "supports_vision": True,
        },
        {
            "name": "plain-probe",
            "provider": "deepseek",
            "model_name": "plain-upstream",
            "supports_vision": False,
        },
    ]
)


def _client(tmp_path: Path, **overrides: Any) -> TestClient:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        model_api_key="sk-test",
        # #786 密封守卫：本文件触及 /api/models（AC3）与 resolve_selection，须把自定义
        # provider 存储钉进 tmp_path——否则宿主 HOME 的自定义 provider 会泄进断言。
        provider_store_path=str(tmp_path / "model-providers.json"),
        **overrides,
    )
    return TestClient(create_app(settings, enable_cors=False))


def _session_id_from_sse(resp: Any) -> str:
    frames = [
        json.loads(line[len(_DATA_PREFIX) :].strip())
        for line in resp.text.splitlines()
        if line.startswith(_DATA_PREFIX)
    ]
    session_id = next((f["session_id"] for f in frames if f.get("session_id")), None)
    assert session_id, f"SSE 流里没有 session_id：{frames[:3]}"
    return str(session_id)


def _create_session(client: TestClient, *, model: str | None = None) -> str:
    body: dict[str, Any] = {"task": "hi", "budget": {"local": {"max_agent_turns": 1}}}
    if model is not None:
        body["model"] = model
    with patch(
        "agent_harness.assembly.create_chat_model",
        return_value=ScriptedModel(responses=[AIMessage(content="ok")]),
    ):
        resp = client.post("/api/sessions", json=body)
    assert resp.status_code == 200, resp.text
    return _session_id_from_sse(resp)


def _upload(client: TestClient, session_id: str, data: bytes) -> str:
    resp = client.post(
        f"/api/sessions/{session_id}/attachments",
        content=data,
        headers=_OCTET,
        params={"name": "shot.png"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["attachment_id"]


def _send(
    client: TestClient,
    session_id: str,
    *,
    content: str,
    attachments: list[str] | None = None,
    model: str | None = None,
    scripted: ScriptedModel | None = None,
) -> tuple[Any, ScriptedModel]:
    model_obj = scripted or ScriptedModel(responses=[AIMessage(content="描述完成")])
    body: dict[str, Any] = {"content": content}
    if attachments is not None:
        body["attachments"] = attachments
    if model is not None:
        body["model"] = model
    with patch("agent_harness.assembly.create_chat_model", return_value=model_obj):
        resp = client.post(f"/api/sessions/{session_id}/messages", json=body)
    return resp, model_obj


def _events_text(tmp_path: Path, session_id: str) -> str:
    return (tmp_path / "sessions" / session_id / "events.jsonl").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC1：聚合上限
# ---------------------------------------------------------------------------


def test_count_over_configured_limit_is_422_and_writes_no_event(tmp_path: Path) -> None:
    """部署上限 = 2 张；一次引用 3 张合法图片 → 422，事件流零引用。"""
    client = _client(tmp_path, attachment_max_images_per_message=2)
    session_id = _create_session(client)
    ids = [
        _upload(client, session_id, png_bytes(w, 4))
        for w in (4, 5, 6)
    ]

    resp, _ = _send(client, session_id, content="三张图", attachments=ids)

    assert resp.status_code == 422, resp.text
    text = _events_text(tmp_path, session_id)
    assert all(aid not in text for aid in ids), "被拒请求不得留下任何附件引用"


def test_total_bytes_over_configured_limit_is_413_and_writes_no_event(tmp_path: Path) -> None:
    """两张各自合法、但合计超单消息总字节上限 → 413，事件流零引用。"""
    client = _client(tmp_path, attachment_max_message_image_bytes=2500)
    session_id = _create_session(client)
    ids = [
        _upload(client, session_id, large_png_bytes(4, 4, 1600)),
        _upload(client, session_id, large_png_bytes(5, 4, 1600)),
    ]

    resp, _ = _send(client, session_id, content="两张图", attachments=ids)

    assert resp.status_code == 413, resp.text
    text = _events_text(tmp_path, session_id)
    assert all(aid not in text for aid in ids), "被拒请求不得留下任何附件引用"


def test_count_at_configured_limit_is_allowed(tmp_path: Path) -> None:
    """边界内（= 上限）放行：证明上限是"超过才拒"而不是"达到即拒"。"""
    client = _client(
        tmp_path, agent_models=_CATALOG, attachment_max_images_per_message=2
    )
    session_id = _create_session(client, model="vision-probe")
    ids = [_upload(client, session_id, png_bytes(w, 4)) for w in (4, 5)]

    resp, _ = _send(client, session_id, content="两张图", attachments=ids)

    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# AC3：模型目录暴露 supports_vision
# ---------------------------------------------------------------------------


def test_models_endpoint_exposes_supports_vision(tmp_path: Path) -> None:
    client = _client(tmp_path, agent_models=_CATALOG)

    models = client.get("/api/models").json()["models"]
    by_id = {m["id"]: m for m in models}

    assert by_id["vision-probe"]["supports_vision"] is True
    assert by_id["plain-probe"]["supports_vision"] is False
    # 未声明能力的默认链模型不猜测（字段省略，客户端据此视为不支持）。
    default = next(m for m in models if m["is_default"])
    assert "supports_vision" not in default


# ---------------------------------------------------------------------------
# AC4 / AC6：视觉门禁 + 双保险
# ---------------------------------------------------------------------------


def test_non_vision_session_model_with_attachment_is_422(tmp_path: Path) -> None:
    """会话当前模型不支持视觉（默认 deepseek 未声明）却附图 → 服务端权威 422。"""
    client = _client(tmp_path)
    session_id = _create_session(client)
    aid = _upload(client, session_id, png_bytes(4, 4))

    resp, _ = _send(client, session_id, content="看图", attachments=[aid])

    assert resp.status_code == 422, resp.text
    assert "视觉" in resp.text or "图片" in resp.text
    assert aid not in _events_text(tmp_path, session_id), "被拒请求不得落事件"


def test_explicit_vision_model_on_request_is_allowed(tmp_path: Path) -> None:
    """会话默认非视觉，但本条一次性改选视觉模型（amend.model）→ 放行并真的发图。

    证明门禁按"本条消息最终由哪个模型投影"判定，而非死盯会话旧模型。
    """
    client = _client(tmp_path, agent_models=_CATALOG)
    session_id = _create_session(client)  # 默认链（非视觉）
    aid = _upload(client, session_id, png_bytes(4, 4))

    resp, scripted = _send(
        client, session_id, content="看图", attachments=[aid], model="vision-probe"
    )

    assert resp.status_code == 200, resp.text
    assert aid in _events_text(tmp_path, session_id)
    sent = scripted.snapshots[0].messages
    assert any(
        isinstance(m, HumanMessage)
        and isinstance(m.content, list)
        and any(b.get("type") == "image_url" for b in m.content)
        for m in sent
    ), "改选视觉模型后请求应带 image_url 块"


def test_bypass_to_non_vision_model_degrades_projection(tmp_path: Path) -> None:
    """AC6 双保险：图已在历史里，之后把会话切到非视觉模型 → 投影降级为占位符。

    这是"入口被绕过"的字面场景（发送时是视觉模型，回放/续聊时模型已变），
    投影层必须降级而非把 image_url 发给非视觉模型。
    """
    client = _client(tmp_path, agent_models=_CATALOG)
    session_id = _create_session(client, model="vision-probe")
    aid = _upload(client, session_id, png_bytes(4, 4))
    resp, _ = _send(client, session_id, content="看图", attachments=[aid])
    assert resp.status_code == 200, resp.text

    # 第二条消息把会话切到非视觉模型（amend.model，无附件）→ 新 run 重新投影历史。
    resp2, scripted2 = _send(
        client, session_id, content="再说一次", model="plain-probe"
    )
    assert resp2.status_code == 200, resp2.text

    sent = scripted2.snapshots[0].messages
    assert not any(
        isinstance(m.content, list) and any(b.get("type") == "image_url" for b in m.content)
        for m in sent
    ), "非视觉模型的请求里不得出现 image_url 块（降级必须发生）"
    assert any(
        isinstance(m, HumanMessage)
        and isinstance(m.content, str)
        and IMAGE_OMITTED_PLACEHOLDER in m.content
        for m in sent
    ), "被省略的图必须留下逐字占位符"
