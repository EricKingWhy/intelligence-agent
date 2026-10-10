#!/usr/bin/env python3
"""#830 / MM-08 验证专用启动器：真实 uvicorn + 确定性替身模型（**非产品代码**）。

为什么存在：MM-08 要验的是**跨端与跨生命周期的真实行为**——真实 HTTP、真实会话
JSONL、真实 Local 附件字节、真实进程 kill/restart、真实压缩落盘。本机没有可用的
视觉模型凭证（见证据包缺口①），故只有「模型 provider」这一层换成确定性替身——
传输层、存储层、事件层、压缩管线全部是真的。

**这不是产品代码**；只有 `scripts/verify_830_mm08.py` 以子进程方式拉起它。

`MM08_REAL_MODEL=1` 时**不打桩**，直接走真 `create_chat_model`——AC6-VISION 要求
「真实视觉模型回答」（#830 用户裁决：lighthouse `deepseek-ai/DeepSeek-V4.1-Flash`
本就是视觉模型，无需新凭证）。真实 provider 的 base_url / key / 模型名 / 能力位全部由
父进程经环境注入（`MM08_MODEL_BASE_URL` / `MM08_MODEL_API_KEY` / `MM08_MODEL_NAME` /
`MM08_AGENT_MODELS`），**本文件不写死任何 key**。

替身接线（两个独立 seam，必须都换，否则压缩会打真实 API 而鉴权失败）：
  * `agent_harness.assembly.create_chat_model`      —— run 的主模型（assembly 顶层 import）
  * `agent_harness.model.provider.create_chat_model` —— 摘要模型 / `_compact_context_builder`
    的主模型（`session/service.py` 在函数内 `from ... import`，按模块属性即时查找）

环境变量（全部由 `verify_830_mm08.py` 注入）：
    MM08_WORKSPACE_DIR  → `Settings.workspace_dir`（会话 JSONL 根 = <dir>/sessions）
    MM08_ARTIFACT_DIR   → `Settings.artifact_dir` （附件字节根）
    MM08_AGENT_MODELS   → `AGENT_MODELS` JSON（须含 supports_vision=true 的条目）
    MM08_SUMMARY_MODEL  → `Settings.summary_model`（指向目录里 `MM08_SUMMARY_NAME` 那条）
    MM08_SUMMARY_NAME   → 摘要替身标记用的 `model_name`（与主模型区分）
    MM08_MODEL_SINK     → 替身每次被调用时追加一行请求快照的 JSONL 路径
    MM08_PORT           → uvicorn 监听端口（只绑 loopback）
    MM08_REAL_MODEL     → "1" = 不打桩，走真 provider（AC6-VISION）
    MM08_MODEL_BASE_URL → 真 provider base_url（仅真实模式；不写死)
    MM08_MODEL_API_KEY  → 真 provider key（仅真实模式；由父进程从环境注入，非本文件常量）
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from unittest.mock import patch

import uvicorn
from langchain_core.messages import AIMessage, AIMessageChunk

from agent_harness.config import Settings
from agent_harness.model.scripted import ScriptedModel

#: 主模型固定回答（脚本化模型耗尽会报错，故覆写响应选择恒回同一句）。
_STUB_REPLY = "ok"

#: 摘要替身必须产出**精确**的 4 节结构（`_MODEL_SUMMARY_HEADINGS`）——压缩管线对
#: 章节标题做逐字契约校验，且第 0/1/6/7 节由程序化生成、逐字比对。内容刻意保持
#: 短（远小于待压缩段）以满足「摘要必须比被压缩段更小」的闸门。
_SUMMARY_TEMPLATE = (
    "## 已完成工作与关键决策\n"
    "- 用户发来消息（含图片附件），助手已回应。\n"
    "\n## 失败方案\n"
    "(none)\n"
    "\n## 当前进行中状态\n"
    "- 会话空闲，可继续。\n"
    "\n## Next Step\n"
    "- 等待用户下一步指令。"
)


def _compact_image_block(block: dict[str, Any]) -> dict[str, Any]:
    """图片块只留引用/尺寸证据：base64 载荷压成 `<base64 N chars>`（AC5 的可判据形状）。"""
    compact = {
        key: value for key, value in block.items() if key not in ("source", "image_url")
    }
    source = block.get("source")
    if isinstance(source, dict):
        compact["source"] = {
            key: (
                f"<base64 {len(value)} chars>"
                if key == "data" and isinstance(value, str)
                else value
            )
            for key, value in source.items()
        }
    url = block.get("image_url")
    if isinstance(url, dict) and isinstance(url.get("url"), str):
        raw = url["url"]
        compact["image_url"] = {
            "url": f"<data-url {len(raw)} chars>" if raw.startswith("data:") else raw,
            **{k: v for k, v in url.items() if k != "url"},
        }
    return compact


def _dump_message(message: Any) -> dict[str, Any]:
    """把一条消息压成可落 JSONL 的紧凑形状（图片块只留引用 + base64 长度）。"""
    data: dict[str, Any] = {"type": type(message).__name__}
    name = getattr(message, "name", None)
    if name is not None:
        data["name"] = name
    content = getattr(message, "content", None)
    if isinstance(content, str):
        data["content"] = content[:8000]
    elif isinstance(content, list):
        blocks: list[Any] = []
        for block in content:
            if not isinstance(block, dict):
                blocks.append(str(block)[:1000])
                continue
            if block.get("type") in ("image", "image_url"):
                blocks.append(_compact_image_block(block))
                continue
            blocks.append(block)
        data["content"] = blocks
    else:
        data["content"] = str(content)[:8000]
    return data


class VerifyModel(ScriptedModel):
    """确定性替身模型：主模型恒回 `ok`；摘要模型恒回合法 4 节摘要。

    每次调用把请求快照追加进 `MM08_MODEL_SINK`（JSONL，一行一次）——这是"送给模型的
    内容里到底有没有那张图"的唯一可观测面（AC2/AC4/AC5 的证据来源）。

    `MM08_MODEL_DELAY_MS` 给每次调用注入人为延时：kill/restart 与「在途 run」恢复
    需要一个可命中的慢窗口（真实 provider 是秒级，替身否则毫秒级返回、窗口打不中）。
    """

    def __init__(self, sink: str | None, role: str, delay_ms: int = 0) -> None:
        super().__init__([AIMessage(content=_STUB_REPLY)])
        self._sink = sink
        self._role = role
        self._delay_ms = delay_ms

    def _record(self, messages: list[Any]) -> None:
        if not self._sink:
            return
        row = {"role": self._role, "messages": [_dump_message(m) for m in messages]}
        with open(self._sink, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _reply(self) -> AIMessage:
        return AIMessage(
            content=_SUMMARY_TEMPLATE if self._role == "summary" else _STUB_REPLY
        )

    def bind_tools(self, tools: list, **kwargs: Any) -> VerifyModel:
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self._record(list(messages))
        if self._delay_ms:
            await asyncio.sleep(self._delay_ms / 1000.0)
        return self._reply()

    async def astream(self, messages: list[Any], **kwargs: Any):
        self._record(list(messages))
        if self._delay_ms:
            await asyncio.sleep(self._delay_ms / 1000.0)
        content = str(self._reply().content)
        chunks = [
            content[index:index + self.chunk_size]
            for index in range(0, len(content), self.chunk_size)
        ]
        for piece in chunks:
            yield AIMessageChunk(content=piece)


def build_settings() -> Settings:
    """从注入的环境变量构造 Settings（不吃仓库根 `.env`：`_env_file=None`）。

    真实视觉模型模式（`MM08_REAL_MODEL=1`）下 `MM08_MODEL_API_KEY` / `MM08_MODEL_BASE_URL`
    指向真实 provider；这两个值由父进程从环境读取后注入，**本文件不写死任何 key**。
    """
    kwargs: dict[str, Any] = {}
    summary = os.environ.get("MM08_SUMMARY_MODEL")
    if summary:
        kwargs["summary_model"] = summary
    return Settings(
        _env_file=None,
        workspace_dir=os.environ["MM08_WORKSPACE_DIR"],
        artifact_dir=os.environ["MM08_ARTIFACT_DIR"],
        model_api_key=os.environ.get("MM08_MODEL_API_KEY", "sk-mm08-verify"),
        model_base_url=os.environ.get("MM08_MODEL_BASE_URL", ""),
        model_provider=os.environ.get("MM08_MODEL_PROVIDER", "deepseek"),
        model_name=os.environ.get("MM08_MODEL_NAME", "vision-probe"),
        agent_models=os.environ.get("MM08_AGENT_MODELS", ""),
        **kwargs,
    )


def main() -> None:
    import agent_harness.assembly as assembly_module
    from agent_harness.model import provider as provider_module
    from agent_harness.web.app import create_app

    sink = os.environ.get("MM08_MODEL_SINK") or None
    summary_name = os.environ.get("MM08_SUMMARY_NAME", "")
    delay_ms = int(os.environ.get("MM08_MODEL_DELAY_MS", "0"))

    def factory(config: Any, **kwargs: Any) -> VerifyModel:
        role = "summary" if getattr(config, "model_name", None) == summary_name else "main"
        return VerifyModel(sink, role, delay_ms)

    settings = build_settings()
    app = create_app(settings, enable_cors=False)
    port = int(os.environ.get("MM08_PORT", "0"))
    if os.environ.get("MM08_REAL_MODEL") == "1":
        # AC6-VISION：真实 provider（lighthouse 视觉模型），不打桩。
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
        return
    with patch.object(assembly_module, "create_chat_model", factory), patch.object(
        provider_module, "create_chat_model", factory,
    ):
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
