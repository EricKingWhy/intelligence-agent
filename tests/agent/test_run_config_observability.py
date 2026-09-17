"""#198：档位与生效工具清单的可回溯可观测性。

钉三条契约（docs/TICKET_BATCH_PLAN.md §4）：
  T1：`run/started.data.agent_profile` **总是**写生效档位（未指定 = "main"——
      "缺字段"正是今天不可回溯的根因）；
  T2：`run_config` 结构化日志含 `agent_profile` / `model_id` / `tool_names[]`
      （全量生效）/ `dropped_tools[]`（被 tool_scope 剔除的，含 write 等）；
  T3：日志行带 `session_id`（此前 llm_call 只能靠正文指纹检索）；日志不含密钥。
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from agent_harness.agent import AgentRuntime
from agent_harness.assembly import build_runtime, initialize_stores, recovery_stores
from agent_harness.capability.wiring import CapabilityWiring
from agent_harness.config import Settings
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import RUN_STARTED, JsonlSessionStore, Session
from agent_harness.tooling import ToolExecutor, ToolRegistry
from agent_harness.tools import ReadTool
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


class _Sandbox:
    """最小 sandbox 替身（ReadTool 构造需要）；本测试不执行工具。"""


def _runtime(profile: str = "main", dropped: tuple[str, ...] = ()) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(ReadTool(_Sandbox()))
    return AgentRuntime(
        ScriptedModel([AIMessage(content="done")]), registry, ToolExecutor(registry),
        agent_profile=profile, dropped_tools=dropped,
    )


# ── T1：run/started.data.agent_profile ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,expected", [
    ("research_review", "research_review"),
    (None, "main"),  # 未指定 ⇒ "main"（缺字段正是不可回溯的根因）
])
async def test_run_started_always_carries_agent_profile(tmp_path, profile, expected):
    runtime = _runtime(profile) if profile else _runtime()
    session = make_session(tmp_path)
    await runtime.run(session, "hello")
    started = next(e for e in session.events if e.type == RUN_STARTED)
    assert started.data["agent_profile"] == expected


@pytest.mark.asyncio
async def test_default_runtime_writes_main_profile(tmp_path):
    """不传 agent_profile（既有调用方/测试）⇒ 默认 "main"，行为添加性。"""
    registry = ToolRegistry()
    runtime = AgentRuntime(ScriptedModel([AIMessage(content="done")]), registry,
                           ToolExecutor(registry))
    session = make_session(tmp_path)
    await runtime.run(session, "hello")
    started = next(e for e in session.events if e.type == RUN_STARTED)
    assert started.data["agent_profile"] == "main"


# ── T2：run_config 结构化日志 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_config_log_has_profile_model_and_tool_lists(tmp_path, caplog):
    registry = ToolRegistry()
    runtime = AgentRuntime(
        ScriptedModel([AIMessage(content="done")]),
        registry, ToolExecutor(registry),
        agent_profile="research_review",
        dropped_tools=("write", "edit", "apply_patch"),
    )
    registry.register(ReadTool(_Sandbox()))
    session = make_session(tmp_path)
    with caplog.at_level(logging.INFO, logger="agent_harness.agent"):
        await runtime.run(session, "hello")

    records = [r for r in caplog.records if getattr(r, "event_type", None) == "run_config"]
    assert records, "run_config 日志必须存在"
    record = records[0]
    assert record.agent_profile == "research_review"
    # model_id 是本次生效的主模型名。
    assert record.model_id == "primary"
    # tool_names 是全量生效清单；dropped_tools 含被剔除的写工具——
    # 这就是「模型说没有 write」的答案本身。
    assert "read" in record.tool_names
    assert set(record.dropped_tools) == {"write", "edit", "apply_patch"}
    assert record.session_id == session.session_id


@pytest.mark.asyncio
async def test_run_config_log_without_drops(tmp_path, caplog):
    """全量档位：dropped_tools 为空列表（键在场、值为空——口径统一）。"""
    runtime = _runtime("main")
    session = make_session(tmp_path)
    with caplog.at_level(logging.INFO, logger="agent_harness.agent"):
        await runtime.run(session, "hello")
    [record] = [r for r in caplog.records if getattr(r, "event_type", None) == "run_config"]
    assert record.dropped_tools == []
    assert record.session_id == session.session_id


# ── T3：日志不含密钥 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_config_log_contains_no_secrets(tmp_path, caplog):
    runtime = _runtime()
    session = make_session(tmp_path)
    with caplog.at_level(logging.INFO, logger="agent_harness.agent"):
        await runtime.run(session, "hello")
    assert "sk-test" not in caplog.text


# ── 装配层：dropped_tools 计算 + profile 传递 ────────────────────────


class ScriptedModelFactory:
    def bind_tools(self, tools, **kwargs):
        return self

    async def astream(self, messages, **kwargs):
        yield AIMessageChunk(content="ok")


async def _build(tmp_path, agent_profile: str | None,
                 model_name: str | None = None) -> AgentRuntime:
    settings = Settings(_env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test",
                        **({"model_name": model_name} if model_name is not None else {}))
    stores = recovery_stores(tmp_path / "harness.db")
    await initialize_stores(stores)
    workspace_registry = WorkspaceRegistry(root=tmp_path, backend="local")
    with patch("agent_harness.assembly.create_chat_model", return_value=ScriptedModelFactory()):
        return await build_runtime(
            settings=settings, wiring=CapabilityWiring(), stores=stores,
            workspace_registry=workspace_registry,
            session_id="sess-198",
            workspace=tmp_path / "workspaces" / "sess-198",
            max_steps=10,
            agent_profile=agent_profile,
        )


# ── #226：run/started.data.model（请求侧模型标识） ───────────────────


@pytest.mark.asyncio
async def test_run_started_carries_model_from_runtime_name(tmp_path):
    """#226：跑一个 run ⇒ run/started 落请求侧模型标识（此处是调用方给的运行时名）。"""
    registry = ToolRegistry()
    runtime = AgentRuntime(
        ScriptedModel([AIMessage(content="done")]), registry, ToolExecutor(registry),
        primary_model_name="deepseek-chat",
    )
    session = make_session(tmp_path)
    await runtime.run(session, "hello")
    started = next(e for e in session.events if e.type == RUN_STARTED)
    assert started.data["model"] == "deepseek-chat"


@pytest.mark.asyncio
async def test_run_started_model_is_the_wire_model_id_not_placeholder(tmp_path):
    """#226：装配路径下落的是**发往 provider 的 model 值**（`ModelConfig.model_name`），
    不是 AgentRuntime 的占位默认名 "primary"——后者只是直接构造路径的缺省。"""
    runtime = await _build(tmp_path, "main", model_name="qwen-plus")
    session = make_session(tmp_path)
    await runtime.run(session, "hello")
    started = next(e for e in session.events if e.type == RUN_STARTED)
    assert started.data["model"] == "qwen-plus"
    # 同一事实落盘可重读（durable，不是内存对象的偶然形状）——刷新/回放后仍在。
    reloaded = Session.resume(JsonlSessionStore(root=tmp_path), session.session_id)
    replayed = next(e for e in reloaded.events if e.type == RUN_STARTED)
    assert replayed.data["model"] == "qwen-plus"


@pytest.mark.asyncio
async def test_assembly_research_profile_records_dropped_write_tools(tmp_path):
    """research_review 收窄 ⇒ dropped_tools 含 write/edit/apply_patch/bash——
    「模型说没有 write」可从日志回溯。"""
    runtime = await _build(tmp_path, "research_review")
    assert {"write", "edit", "apply_patch", "bash"} <= set(runtime.dropped_tools)
    assert runtime.agent_profile == "research_review"
    # 生效清单与收窄后 registry 一致（从 registry 实际取，不设第二份真相）。
    registered = {t.name for t in runtime.registry.list()}
    assert "read" in registered and "write" not in registered


@pytest.mark.asyncio
async def test_assembly_main_profile_has_no_drops(tmp_path):
    """main/None 全量 ⇒ dropped_tools 为空。"""
    runtime = await _build(tmp_path, "main")
    assert runtime.dropped_tools == ()
    assert runtime.agent_profile == "main"

    default_runtime = await _build(tmp_path, None)
    assert default_runtime.dropped_tools == ()
    assert default_runtime.agent_profile == "main"
