"""#830 / MM-08 D1 回归：fork 之后子会话仍能**看见父会话的图片**。

缺陷：父会话上传 + 发送一张图 → `POST /api/sessions/{id}/forks` → 子会话的种子事件
逐字复制了那条带 `attachments` 引用的 `user/message`（所以读授权判据「被本会话事件
引用」对子会话成立），但附件**字节**当初落在**父会话命名空间**里，而 `fork` 从不复制
附件对象 ⇒ 子会话的读数接口 404、模型请求里图片退化成占位符。

本文件钉两条用户可见面：
1. 子会话 `GET .../attachments/{id}/content` → 200 + 字节逐字节相等；
2. 子会话里发一条后续消息 → 重建的模型请求里仍是**真图片块**（与父会话一致）。

两条都同时钉住"fork 不复制附件字节"（子会话目录里不出现附件对象）。
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


def _png(width: int = 16, height: int = 10) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        artifact_dir=str(tmp_path / "artifacts"),
        model_api_key="sk-test",
        agent_models=_VISION_CATALOG,
    )
    return TestClient(create_app(settings, enable_cors=False))


def _create_session(client: TestClient) -> str:
    with patch(
        "agent_harness.assembly.create_chat_model",
        return_value=ScriptedModel(responses=[AIMessage(content="ok")]),
    ):
        resp = client.post(
            "/api/sessions",
            json={
                "task": "hi",
                "model": "vision-probe",
                "budget": {"local": {"max_agent_turns": 1}},
            },
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


def _upload(client: TestClient, session_id: str, data: bytes) -> str:
    resp = client.post(
        f"/api/sessions/{session_id}/attachments",
        content=data,
        headers=_OCTET,
    )
    assert resp.status_code == 200, resp.text
    return str(resp.json()["attachment_id"])


def _send(
    client: TestClient,
    session_id: str,
    *,
    content: str,
    attachments: list[str] | None = None,
) -> ScriptedModel:
    scripted = ScriptedModel(responses=[AIMessage(content="描述完成")])
    body: dict[str, Any] = {"content": content}
    if attachments is not None:
        body["attachments"] = attachments
    with patch("agent_harness.assembly.create_chat_model", return_value=scripted):
        resp = client.post(f"/api/sessions/{session_id}/messages", json=body)
    assert resp.status_code == 200, resp.text
    return scripted


def _user_message_seqs(client: TestClient, session_id: str) -> list[int]:
    events = client.get(f"/api/sessions/{session_id}/events").json()
    return [e["seq"] for e in events if e["type"] == "user/message"]


def _first_image_block(messages: list[Any]) -> dict[str, Any]:
    for message in messages:
        if isinstance(message, HumanMessage) and isinstance(message.content, list):
            for block in message.content:
                if block.get("type") == "image_url":
                    return dict(block)
    raise AssertionError("请求里没有图片块")


def _fork_with_inherited_image(
    client: TestClient, tmp_path: Path
) -> tuple[str, str, str, bytes]:
    """父会话上传 + 发送一张图 → 在**第二条**用户消息处 fork（锚点消息不进 child）。

    返回 `(parent_session_id, child_session_id, attachment_id, payload)`。
    """
    parent = _create_session(client)
    payload = _png()
    attachment_id = _upload(client, parent, payload)
    _send(client, parent, content="这是什么", attachments=[attachment_id])
    _send(client, parent, content="第二条")  # fork 锚点：带图那条因此进 seed

    anchors = _user_message_seqs(client, parent)
    assert len(anchors) >= 2, anchors  # 创建会话本身就有第 1 条（task）用户消息
    resp = client.post(f"/api/sessions/{parent}/forks", json={"from_seq": anchors[-1]})
    assert resp.status_code == 200, resp.text
    child = str(resp.json()["session_id"])

    # 子会话确实继承了引用（种子事件逐字复制，读授权判据因此成立）。
    child_events = client.get(f"/api/sessions/{child}/events").json()
    referenced = {
        ref["attachment_id"]
        for event in child_events
        if event["type"] == "user/message"
        for ref in event["data"].get("attachments") or []
    }
    assert attachment_id in referenced, referenced
    # fork 零拷贝：子会话命名空间里没有任何附件字节。
    assert not (tmp_path / "artifacts" / child / "attachments").exists()
    return parent, child, attachment_id, payload


def test_forked_child_reads_inherited_attachment_bytes(tmp_path: Path) -> None:
    """子会话读数接口必须能取回父会话的附件字节（D1 用户可见面之一）。"""
    client = _client(tmp_path)
    _parent, child, attachment_id, payload = _fork_with_inherited_image(client, tmp_path)

    got = client.get(f"/api/sessions/{child}/attachments/{attachment_id}/content")

    assert got.status_code == 200, got.text
    assert got.content == payload


def test_forked_child_model_request_contains_inherited_image_block(tmp_path: Path) -> None:
    """子会话里的后续 run 必须重建出**真图片块**，且与父会话的块逐字段相同。

    D1 用户可见面之二：父会话模型看到图，子会话模型此前只看到占位符
    （`(image omitted: ...)` 一族）。两边的块必须一致——图片事实没有因 fork 降级。
    """
    client = _client(tmp_path)
    parent, child, _attachment_id, _payload = _fork_with_inherited_image(client, tmp_path)

    parent_block = _first_image_block(
        _send(client, parent, content="再说一次").snapshots[0].messages
    )
    child_block = _first_image_block(
        _send(client, child, content="再说一次").snapshots[0].messages
    )

    assert child_block == parent_block
    assert child_block["image_url"]["url"].startswith("data:image/")
