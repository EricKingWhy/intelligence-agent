"""W-22（#366）真实子进程探针：产品客户端离开后的准入计数与崩溃窗口。

由 test_w22_client_absent_subprocess.py 以真实 Python 子进程启动：全栈装配
（build_runtime：真实 bash 工具 / Ledger / Checkpoint / workspace 映射），只有
模型是 FakeModel（ScriptedModel——AC 原文「真实子进程与 FakeModel」）。不是
pytest 收集对象（文件名不带 test_ 前缀）。

argv[1] 是 JSON 配置：
    root:     沙盘根目录（sessions/ harness.db ws/ 都建在其下）
    mode:     pause（干净缺席暂停）| crash_before_pause（暂停落盘前真实崩溃）
    command:  交给真实 bash 工具的命令（默认 sleep）

剧本首条让模型调 bash（真实子进程命令）；其后 5 条是「不该被消费」的哨兵——
若缺席闸门失效，它们会被逐条烧掉，admission 计数（snapshots）当即超标。

离场路径走 RunManager 真实孤儿计时臂（unsubscribe → 宽限到期 → 置缺席闸门，
不是测试手工 mark_absent），即 W-12 未来要接的同一条缝。

mode=pause 正常退出并打印一行 JSON 结果（ASCII）；mode=crash_before_pause 在
缺席已置位、真实 bash 仍在执行时 os._exit(137)——run/paused 永远来不及落盘。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage

from agent_harness.assembly import (
    assemble_wiring,
    build_runtime,
    initialize_stores,
    recovery_stores,
)
from agent_harness.config import Settings
from agent_harness.model.scripted import ScriptedModel
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import TOOL_CALL, JsonlSessionStore, Session
from agent_harness.session.runmanager import RunManager

GRACE_SECONDS = 0.2


async def main() -> None:
    config = json.loads(sys.argv[1])
    root = Path(config["root"])
    mode = config["mode"]
    command = config.get(
        "command", 'python -c "import time; time.sleep(1)"',
    )

    settings = Settings(
        _env_file=None, workspace_dir=str(root), model_api_key="sk-test",
    )
    _, wiring = await assemble_wiring(settings)
    stores = recovery_stores(root / "harness.db")
    await initialize_stores(stores)
    workspace_registry = WorkspaceRegistry(root=root, backend="local")
    store = JsonlSessionStore(root / "sessions")
    session = Session.start(store)
    workspace = workspace_registry.create(session.session_id)

    # 剧本：第 1 条真的调 bash（真实子进程）；后面全是闸门失效才会烧到的哨兵。
    script = [
        AIMessage(content="", tool_calls=[
            {"id": "call-1", "name": "bash", "args": {"command": command}},
        ]),
    ] + [AIMessage(content="sentinel-after-absence")] * 5
    model = ScriptedModel(script)

    with patch("agent_harness.assembly.create_chat_model", return_value=model):
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=workspace_registry,
            session_id=session.session_id, workspace=workspace,
            max_agent_turns=8,
        )

    manager = RunManager(disconnect_grace_seconds=GRACE_SECONDS)
    try:
        run, subscriber = manager.launch(
            session, runtime, "在客户端缺席前开始干活", presence_managed=True,
        )
        # 等真实 bash 调用进入在途（TOOL_CALL 落盘）——离开必须发生在工具执行中。
        for _ in range(500):
            if any(e.type == TOOL_CALL for e in session.events):
                break
            await asyncio.sleep(0.02)
        assert any(e.type == TOOL_CALL for e in session.events), "bash 未进入在途"

        run.unsubscribe(subscriber)  # 产品客户端离开 → 真实宽限计时臂

        if mode == "crash_before_pause":
            # 宽限到期（0.2s）置缺席后、真实 bash（sleep 3）仍在执行时硬崩：
            # run/paused 永远来不及写——父进程按既有 interrupted/reconcile 恢复。
            await asyncio.sleep(0.4)
            os._exit(137)

        await asyncio.wait_for(run.task, timeout=15)
        assert run.paused is True, "run 应以暂停收口"
        assert len(model.snapshots) == 1, (
            f"离开后新请求数必须为 0（总请求数=缺席前那次）：实际 {len(model.snapshots)}"
        )
        print(json.dumps({
            "ok": True,
            "session_id": session.session_id,
            "run_id": run.run_id,
            "model_requests": len(model.snapshots),
        }))
    finally:
        await manager.aclose()


if __name__ == "__main__":
    asyncio.run(main())
