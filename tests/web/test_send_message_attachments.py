"""#823 / MM-02：续聊端点接受附件引用 → user/message 事件 → 模型看到图。

覆盖：事件形状（只带引用、无 base64）、发送侧校验（存在/归属/形态）、视觉模型的
provider 载荷（OpenAI `image_url` data URL + `detail`）、图片只出现在 user 消息、
重放/续聊由事件前缀重建出同一图片块。
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from PIL import Image

from agent_harness.config import Settings
from agent_harness.model.multimodal import DEFAULT_IMAGE_DETAIL
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel

_OCTET = {"content-type": "application/octet-stream"}
_DATA_PREFIX = "data:"

_VISION_CATALOG = json.dumps(
    [
        {
            "name": "vision-probe",
            "provider": "deepseek",
            "model_name": "deepseek-chat",
            "supports_vision": True,
        }
    ]
)


def _png(width: int = 12, height: int = 8) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _client(tmp_path: Path, **overrides: Any) -> TestClient:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        model_api_key="sk-test",
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


def _upload(client: TestClient, session_id: str, data: bytes, *, name: str = "shot.png"):
    return client.post(
        f"/api/sessions/{session_id}/attachments",
        content=data,
        headers=_OCTET,
        params={"name": name},
    )


def _send(
    client: TestClient, session_id: str, *, content: str,
    attachments: list[str] | None = None, model: ScriptedModel | None = None,
) -> tuple[Any, ScriptedModel]:
    scripted = model or ScriptedModel(responses=[AIMessage(content="描述完成")])
    body: dict[str, Any] = {"content": content}
    if attachments is not None:
        body["attachments"] = attachments
    with patch("agent_harness.assembly.create_chat_model", return_value=scripted):
        resp = client.post(f"/api/sessions/{session_id}/messages", json=body)
    return resp, scripted


def _events_text(tmp_path: Path, session_id: str) -> str:
    return (tmp_path / "sessions" / session_id / "events.jsonl").read_text(encoding="utf-8")


def _user_message_with_attachments(tmp_path: Path, session_id: str) -> dict[str, Any]:
    for line in _events_text(tmp_path, session_id).splitlines():
        event = json.loads(line)
        if event["type"] == "user/message" and event["data"].get("attachments"):
            return event
    raise AssertionError("没有找到带附件引用的 user/message 事件")


def test_send_records_attachment_ref_and_never_base64(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_id = _create_session(client)
    payload = _png(40, 30)
    attachment_id = _upload(client, session_id, payload).json()["attachment_id"]

    resp, _ = _send(client, session_id, content="看看这张图", attachments=[attachment_id])
    assert resp.status_code == 200, resp.text

    event = _user_message_with_attachments(tmp_path, session_id)
    (ref,) = event["data"]["attachments"]
    assert ref["kind"] == "image"
    assert ref["attachment_id"] == attachment_id
    assert ref["media_type"] == "image/png"
    assert ref["bytes"] == len(payload)
    assert ref["width"] == 40 and ref["height"] == 30
    # content 仍是 str（加法式扩展，消费方 isinstance 校验不破坏）。
    assert isinstance(event["data"]["content"], str)

    raw = _events_text(tmp_path, session_id)
    assert "base64" not in raw
    assert "data:image" not in raw


def test_send_with_unknown_attachment_is_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_id = _create_session(client)
    resp, _ = _send(
        client, session_id, content="x", attachments=["sha256:" + "0" * 64]
    )
    assert resp.status_code == 422, resp.text


def test_send_with_malformed_attachment_id_is_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_id = _create_session(client)
    resp, _ = _send(client, session_id, content="x", attachments=["not-an-id"])
    assert resp.status_code == 422, resp.text


def test_send_with_other_session_attachment_is_422(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_a = _create_session(client)
    session_b = _create_session(client)
    attachment_id = _upload(client, session_a, _png()).json()["attachment_id"]

    resp, _ = _send(client, session_b, content="x", attachments=[attachment_id])
    assert resp.status_code == 422, resp.text


def test_duplicate_attachment_ids_are_deduped(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_id = _create_session(client)
    attachment_id = _upload(client, session_id, _png()).json()["attachment_id"]

    resp, _ = _send(
        client, session_id, content="x", attachments=[attachment_id, attachment_id]
    )
    assert resp.status_code == 200, resp.text
    event = _user_message_with_attachments(tmp_path, session_id)
    assert [ref["attachment_id"] for ref in event["data"]["attachments"]] == [attachment_id]


def test_vision_model_request_carries_image_url_block(tmp_path: Path) -> None:
    client = _client(tmp_path, agent_models=_VISION_CATALOG)
    session_id = _create_session(client, model="vision-probe")
    attachment_id = _upload(client, session_id, _png(16, 10)).json()["attachment_id"]

    resp, scripted = _send(
        client, session_id, content="这是什么", attachments=[attachment_id]
    )
    assert resp.status_code == 200, resp.text

    sent = scripted.snapshots[0].messages
    image_messages = [
        m for m in sent
        if isinstance(m, HumanMessage) and isinstance(m.content, list)
        and any(b.get("type") == "image_url" for b in m.content)
    ]
    assert len(image_messages) == 1, "图片块必须只出现在一条 user 消息里（AC6）"
    blocks = image_messages[0].content
    assert blocks[0] == {"type": "text", "text": "这是什么"}
    image = next(b for b in blocks if b.get("type") == "image_url")
    assert image["image_url"]["detail"] == DEFAULT_IMAGE_DETAIL
    # opaque RGB PNG 归一化为 JPEG（AC7）。
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # 没有别的消息类型带图片块。
    assert all(
        not (isinstance(m.content, list) and any(b.get("type") == "image_url" for b in m.content))
        for m in sent
        if m is not image_messages[0]
    )


def test_followup_run_rebuilds_same_image_block_from_events(tmp_path: Path) -> None:
    """AC9：由事件前缀重建的请求与首次一致（图片块形状相同）。"""
    client = _client(tmp_path, agent_models=_VISION_CATALOG)
    session_id = _create_session(client, model="vision-probe")
    attachment_id = _upload(client, session_id, _png(16, 10)).json()["attachment_id"]
    _, first = _send(client, session_id, content="这是什么", attachments=[attachment_id])
    first_image = _first_image_block(first.snapshots[0].messages)

    # 第二条（纯文本）消息触发新 run：历史 prefix 重新投影，应重建出同一图片块。
    _, second = _send(client, session_id, content="再说一次")
    rebuilt = _first_image_block(second.snapshots[0].messages)
    assert rebuilt == first_image


def _first_image_block(messages: list[Any]) -> dict[str, Any]:
    for message in messages:
        if isinstance(message, HumanMessage) and isinstance(message.content, list):
            for block in message.content:
                if block.get("type") == "image_url":
                    return block
    raise AssertionError("重建的请求里没有图片块")


def test_ws_send_message_accepts_attachments(tmp_path: Path) -> None:
    """WS 帧（`send_message`）与 HTTP 端点同形地接受附件 id 列表（AC4）。"""
    client = _client(tmp_path)
    session_id = _create_session(client)
    attachment_id = _upload(client, session_id, _png()).json()["attachment_id"]

    with patch(
        "agent_harness.assembly.create_chat_model",
        return_value=ScriptedModel(responses=[AIMessage(content="ok")]),
    ), client.websocket_connect("/api/ws") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "send_message",
                    "session_id": session_id,
                    "content": "看图",
                    "attachments": [attachment_id],
                }
            )
        )
        payload = json.loads(ws.receive_text())

    assert payload["type"] == "launched", payload
    event = _user_message_with_attachments(tmp_path, session_id)
    assert [ref["attachment_id"] for ref in event["data"]["attachments"]] == [attachment_id]


def test_ws_send_message_rejects_non_list_attachments(tmp_path: Path) -> None:
    client = _client(tmp_path)
    session_id = _create_session(client)

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "send_message",
                    "session_id": session_id,
                    "content": "看图",
                    "attachments": "sha256:oops",
                }
            )
        )
        payload = json.loads(ws.receive_text())

    assert payload["type"] == "error", payload


def test_ws_send_message_rejects_unknown_attachment(tmp_path: Path) -> None:
    """WS 与 HTTP 同形：存在性/归属校验失败（未知 id）同样回错误帧（AC4）。"""
    client = _client(tmp_path)
    session_id = _create_session(client)

    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "send_message",
                    "session_id": session_id,
                    "content": "看图",
                    "attachments": ["sha256:" + "0" * 64],
                }
            )
        )
        payload = json.loads(ws.receive_text())

    assert payload["type"] == "error", payload
    # 校验失败发生在任何落盘之前：事件流里没有引用。
    assert "attachment" not in _events_text(tmp_path, session_id)


def _events_line_count(tmp_path: Path, session_id: str) -> int:
    text = _events_text(tmp_path, session_id)
    return len([line for line in text.splitlines() if line.strip()])


def test_upload_appends_no_event(tmp_path: Path) -> None:
    """AC1：**上传本身不产生事件**——字节落存储，事件只在发送时写。

    上传走内容寻址落盘（persist-before-event 的"persist"半），不 append 任何
    SessionEvent；引用数组只在 `user/message` 事件里出现。
    """
    client = _client(tmp_path)
    session_id = _create_session(client)
    before = _events_line_count(tmp_path, session_id)

    up = _upload(client, session_id, _png(24, 18))
    assert up.status_code == 200, up.text

    assert _events_line_count(tmp_path, session_id) == before
    # 上传后事件流里没有任何附件引用（引用只在发送时进 user/message）。
    assert "attachment" not in _events_text(tmp_path, session_id)


def test_sent_attachment_bytes_are_readable_after_event(tmp_path: Path) -> None:
    """AC2 persist-before-event：事件引用落盘后，其指向的字节必然可读且逐字节相等。"""
    client = _client(tmp_path)
    session_id = _create_session(client)
    payload = _png(24, 18)
    attachment_id = _upload(client, session_id, payload).json()["attachment_id"]

    resp, _ = _send(client, session_id, content="看图", attachments=[attachment_id])
    assert resp.status_code == 200, resp.text

    # 事件已写（引用已落盘）⇒ 此刻受控读回必须成功且字节相等。
    event = _user_message_with_attachments(tmp_path, session_id)
    assert event["data"]["attachments"][0]["attachment_id"] == attachment_id
    got = client.get(
        f"/api/sessions/{session_id}/attachments/{attachment_id}/content"
    )
    assert got.status_code == 200, got.text
    assert got.content == payload
