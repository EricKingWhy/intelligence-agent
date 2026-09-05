"""Agent CLI（Phase 9 CLI Renderer）：驱动 AgentRuntime.run_stream 渲染事件流。

CLI 与 SSE 是同一 AgentEvent 流的两个消费端（spec 11 §1）：渲染是事件流的
纯函数，只挑人要看的（流式正文 / 工具行 / 终态），完整事实源是 Session
JSONL；Diagnostic Log 由 runtime 的 _log 统一产出（CLI 只负责 setup_logging，
不再手搓 llm_call 链路）。最小 CLI 不装配工具（registry 为空——模型直接答复）。

渲染约定借鉴 pi-mono / oh-my-pi（均为 MIT License，设计级借用 + 小工具重实现）：
- 状态行语法 `glyph 标题 折叠参数 · meta`（oh-my-pi tui/status-line.ts）
- 参数折叠 key=value、结果尾部预览 + `... +N more lines`（pi renderers/bash.ts）
- 时长徽章、token 用量页脚 + K/M 压缩（pi footer.ts formatTokens/formatDuration）
- ascii 符号路线（oh-my-pi theme/symbols.ts 的 ascii preset）——Windows GBK
  控制台对 ✔/⏳ 等 glyph 会抛 UnicodeEncodeError，ascii 永远可打印。
License 署名：pi-mono © 2025 Mario Zechner（MIT）；oh-my-pi © 2025-2026
Can Bölük、© 2026 Stencil Labs, Inc.（MIT）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from agent_harness.agent import AgentEvent
from agent_harness.assembly import (
    assemble_wiring,
    build_runtime,
    initialize_stores,
    recovery_stores,
)
from agent_harness.config import Settings
from agent_harness.logging import LogContext, log_context, setup_logging
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import (
    MODEL_DELTA,
    RUN_COMPLETED,
    RUN_FAILED,
    TOOL_CALL,
    TOOL_RESULT,
    JsonlSessionStore,
    Session,
)

_ARGS_LINE_LIMIT = 120
_PREVIEW_LINES = 3


class StreamRenderer:
    """AgentEvent → 终端文本（事件流的纯函数；write 注入便于测试）。

    行式追加输出（无差分重绘）：delta 原样续写；工具块 = 空行 + 状态行 +
    结果预览；终态行补齐换行。model/completed、user/message 等持久化镜像
    一律静默——终端不是第二份事件日志。
    """

    def __init__(self, write: Callable[[str], None]) -> None:
        self._write = write
        self._delta_open = False  # 流式正文输出中：工具行/终态行前先补换行

    def handle(self, event: AgentEvent) -> None:
        if event.type == MODEL_DELTA:
            self._write(event.data["delta"])
            self._delta_open = True
        elif event.type == TOOL_CALL:
            self._end_delta()
            args = _collapse_args(event.data.get("args") or {})
            suffix = f" {args}" if args else ""
            self._write(f"\n[tool] {event.data['tool_name']}{suffix}\n")
        elif event.type == TOOL_RESULT:
            self._render_result(event.data)
        elif event.type == RUN_COMPLETED:
            self._end_delta()
            self._write("\n")
            usage = event.data.get("usage_total") or {}
            if usage:
                self._write(f"tokens: in {_format_tokens(usage.get('prompt_tokens'))}, "
                            f"out {_format_tokens(usage.get('completion_tokens'))}\n")
        elif event.type == RUN_FAILED:
            self._end_delta()
            reason = event.data.get("reason")
            suffix = f" ({reason})" if reason else ""
            self._write(f"\n[run failed]{suffix}\n")

    def _render_result(self, data: dict) -> None:
        try:
            result = json.loads(data["content"])
        except (KeyError, ValueError):
            self._write("  [fail] (unparseable result)\n")
            return
        status = "[ok]" if result.get("ok") else "[fail]"
        duration = result.get("metadata", {}).get("duration_ms")
        suffix = f" ({duration / 1000:.1f}s)" if isinstance(duration, (int, float)) else ""
        self._write(f"  {status}{suffix}\n")
        message = result.get("message") or ""
        lines = message.splitlines()
        for line in lines[:_PREVIEW_LINES]:
            self._write(f"  {line}\n")
        if len(lines) > _PREVIEW_LINES:
            self._write(f"  ... +{len(lines) - _PREVIEW_LINES} more lines\n")

    def _end_delta(self) -> None:
        if self._delta_open:
            self._write("\n")
            self._delta_open = False


def _collapse_args(args: dict) -> str:
    """一行折叠工具参数：key=value，字符串含空格才加引号；整体超限截断。

    折叠约定借鉴 oh-my-pi formatArgsInline（key=value 预算内联）；嵌套结构
    压成紧凑 JSON（本地快失败用不到嵌套语义，终端只要能认出调用形状）。
    """
    parts: list[str] = []
    for key, value in args.items():
        if isinstance(value, str):
            text = f'"{value}"' if (" " in value or not value) else value
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value)
        parts.append(f"{key}={text}")
    line = " ".join(parts)
    if len(line) > _ARGS_LINE_LIMIT:
        line = line[:_ARGS_LINE_LIMIT] + "..."
    return line


def _format_tokens(count: int | None) -> str:
    """token 数 → 紧凑文本（借鉴 pi footer.ts formatTokens 的 K/M 压缩）。"""
    if not isinstance(count, int) or count < 0:
        return "?"
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.1f}M"


async def run(message: str, *, write: Callable[[str], None] | None = None) -> str:
    """跑一次 Agent Loop：流式渲染到 write，返回最终回答文本。

    与 web 共享 assembly.build_runtime 全栈装配（coding 工具 + Ledger/
    Checkpoint + capability 工具）——CLI 不再是削弱装配，耐久性语义一致。
    失败的 run 不抛异常（runtime 契约：失败事实由 run/failed 终结事件 +
    结构化日志承载）——返回空 final_text，main() 据此转 SystemExit(1)。
    成功的 run final_text 恒非空：空响应在 runtime 被拒为失败（R6-2），
    不存在"成功但空回答"的歧义态。
    """
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    # LogContext 提供 trace_id/task_id 关联列——没有它 runtime 的结构化日志
    # 整条链都缺关联键（一次 CLI 运行 = 一个可对账的 trace）。
    with log_context(LogContext.create(service="agent-harness", env="local")):
        workspace_root = Path(settings.workspace_dir)
        _, wiring = await assemble_wiring(settings)
        stores = recovery_stores(workspace_root / "harness.db")
        await initialize_stores(stores)
        workspace_registry = WorkspaceRegistry(root=workspace_root, backend="local")
        session_id = str(uuid4())
        workspace = workspace_root / "workspaces" / session_id
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=workspace_registry,
            session_id=session_id, workspace=workspace,
            max_steps=10, auto_approve=True,
        )
        store = JsonlSessionStore(root=workspace_root / "sessions")
        session = Session.start(store, session_id=session_id)
        renderer = StreamRenderer(write if write is not None else sys.stdout.write)
        final_text = ""
        async for event in runtime.run_stream(session, message):
            renderer.handle(event)
            if event.type == RUN_COMPLETED:
                final_text = event.data.get("final_text", "")
        return final_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Harness CLI")
    parser.add_argument("message", help="发送给 Agent 的任务")
    args = parser.parse_args()
    final_text = asyncio.run(run(args.message))
    if not final_text:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
