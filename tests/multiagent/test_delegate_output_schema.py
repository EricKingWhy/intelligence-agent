"""delegate output_schema（issue #530 IMP-17）：调用方指定子代理输出 JSON Schema。

契约（docs/agents/530-imp17-design-proposal.md §3/§5）：
- output_schema=None ⇒ 现有行为逐字节不变（payload / pending_events 无新键）；
- schema 非法 / 序列化后超 4KB ⇒ INVALID_ARGUMENT，零副作用（不启动 child、
  不占委派预算）；
- child 最终回答含符合 schema 的 JSON ⇒ payload 增 "structured"，
  agent/delegation-finished 事件带 output_schema=True +
  schema_validation="passed"；
- 校验失败 ⇒ 自动重试一次（新 child，同 reserve_delegation 路径占预算）；
  第二次通过 ⇒ 成功，attempts=2 可观测；
- 两次都失败 ⇒ ToolResult.failure，metadata 带结构化错误
  {code: "schema_retry_exhausted", attempts: 2, errors: [...]}（两次错误
  supervisor 都可读）；
- child 全程不输出 JSON ⇒ missing_json 分类，同样走重试/耗尽路径；
- 重试占预算：预算打满时第二次跑被拒 ⇒ 按"第二次失败"同口径
  schema_retry_exhausted 诚实失败，不静默。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_harness.agent.factory import AgentFactory
from agent_harness.multiagent.provider import InProcessSubagentProvider
from agent_harness.multiagent.tools import DelegateTool
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session.store import JsonlSessionStore
from agent_harness.tooling import ToolRegistry
from agent_harness.tooling.result import ErrorCode
from tests.scripted_model import ScriptedModel

_VALID_JSON = '结论如下：\n```json\n{"answer": 42}\n```'
_BAD_TYPE_JSON = '```json\n{"answer": "不是数字"}\n```'
_MISSING_FIELD_JSON = '```json\n{"wrong": 1}\n```'
_NO_JSON = "纯文本回答，全程没有 JSON。"
_SIMPLE_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "integer"}},
}


def _schema_tool(
    tmp_path: Path,
    child_responses: list[AIMessage],
    *,
    max_delegations: int = 8,
) -> tuple[DelegateTool, InProcessSubagentProvider]:
    """已激活的 provider + delegate 工具；child 按剧本逐次响应（每次 run 消耗一条）。"""
    store = JsonlSessionStore(tmp_path / "sessions")
    workspace_registry = WorkspaceRegistry(root=tmp_path / "workspaces")
    workspace_registry.create("parent-0001", workspace_root=tmp_path / "ws")

    provider_impl = InProcessSubagentProvider()
    tool = DelegateTool(provider_impl, max_delegations=max_delegations)
    factory = AgentFactory(
        model=ScriptedModel(child_responses), primary_model_name="main-model",
    )
    source_registry = ToolRegistry()
    source_registry.register(tool)
    provider_impl.activate(
        factory=factory,
        source_registry=source_registry,
        session_store=store,
        workspace_registry=workspace_registry,
        parent_session_id="parent-0001",
    )
    return tool, provider_impl


def _args(target: str, task: str, output_schema: dict | None = None) -> object:
    return type("_Args", (), {"target": target, "task": task,
                              "constraints": [],
                              "output_schema": output_schema})()


def _finished_events(result) -> list[dict]:
    return [data for event_type, data in result.pending_events
            if event_type == "agent/delegation-finished"]


class TestNoneSchemaByteIdentical:
    @pytest.mark.asyncio
    async def test_none_schema_keeps_payload_and_events_unchanged(self, tmp_path):
        """§5-1：output_schema=None ⇒ payload 与 pending_events 无任何新键（逐字节不变）。"""
        tool, provider = _schema_tool(tmp_path, [AIMessage(content="child 完成")])

        result = await tool.execute(_args("coding", "实现一个函数"))

        assert result.ok
        payload = json.loads(result.data["output"])
        assert set(payload) == {"agent_id", "status", "summary"}
        child = provider.last_child_sessions[-1]
        assert result.pending_events == [
            ("agent/delegation-started", {
                "target": "coding", "task": "实现一个函数",
                "child_session_id": child.session_id,
            }),
            ("agent/delegation-finished", {
                "target": "coding", "child_session_id": child.session_id,
                "status": "completed", "summary": "child 完成",
            }),
        ]
        assert "structured" not in payload


class TestSchemaArgumentValidation:
    @pytest.mark.asyncio
    async def test_invalid_schema_rejected_without_child(self, tmp_path):
        """§5-2：schema 非法（check_schema 不过）⇒ INVALID_ARGUMENT，零 child 启动。"""
        tool, provider = _schema_tool(tmp_path, [AIMessage(content="不该被启动")])

        result = await tool.execute(_args("coding", "x", output_schema={"type": 123}))

        assert not result.ok
        assert result.error_code == ErrorCode.INVALID_ARGUMENT
        assert provider.last_child_sessions == [], "非法 schema 不得启动任何 child"
        assert "schema" in result.message

    @pytest.mark.asyncio
    async def test_oversized_schema_rejected_without_child(self, tmp_path):
        """§5-2：schema 序列化后超 4KB ⇒ INVALID_ARGUMENT，零 child 启动。"""
        oversized = {
            "type": "object",
            "properties": {"data": {"type": "string", "description": "填充" * 2000}},
        }
        tool, provider = _schema_tool(tmp_path, [AIMessage(content="不该被启动")])

        result = await tool.execute(_args("coding", "x", output_schema=oversized))

        assert not result.ok
        assert result.error_code == ErrorCode.INVALID_ARGUMENT
        assert provider.last_child_sessions == [], "超限 schema 不得启动任何 child"
        assert "4" in result.message or "4096" in result.message


class TestSchemaPassed:
    @pytest.mark.asyncio
    async def test_valid_output_yields_structured_and_passed_event(self, tmp_path):
        """§5-3：child 输出合法 JSON ⇒ payload.structured + 事件 schema_validation=passed。"""
        tool, provider = _schema_tool(tmp_path, [AIMessage(content=_VALID_JSON)])

        result = await tool.execute(_args("coding", "给结论", output_schema=_SIMPLE_SCHEMA))

        assert result.ok
        payload = json.loads(result.data["output"])
        assert payload["structured"] == {"answer": 42}
        assert payload["summary"] == _VALID_JSON
        assert result.metadata["attempts"] == 1
        finished = _finished_events(result)
        assert len(finished) == 1
        assert finished[0]["output_schema"] is True
        assert finished[0]["schema_validation"] == "passed"
        assert len(provider.last_child_sessions) == 1


class TestSchemaRetry:
    @pytest.mark.asyncio
    async def test_schema_violation_retries_once_and_succeeds(self, tmp_path):
        """§5-4：第一次不符 ⇒ 自动重试一次；第二次符合 ⇒ 成功，attempts=2 可观测。"""
        tool, provider = _schema_tool(
            tmp_path, [AIMessage(content=_BAD_TYPE_JSON), AIMessage(content=_VALID_JSON)],
        )

        result = await tool.execute(_args("coding", "给结论", output_schema=_SIMPLE_SCHEMA))

        assert result.ok, f"第二次通过必须成功：{result.message}"
        assert len(provider.last_child_sessions) == 2, "重试必须是新 child"
        payload = json.loads(result.data["output"])
        assert payload["structured"] == {"answer": 42}
        assert result.metadata["attempts"] == 2
        # 重试任务带第一次的错误清单（snapshot messages 里找实际下发任务文本）
        second_run = provider._factory._model.snapshots[1]
        second_run_task = "\n".join(
            str(m.content) for m in second_run.messages
        )
        assert "上次输出不符合 schema" in second_run_task
        assert "请只输出修正后的 JSON" in second_run_task
        finished = _finished_events(result)
        assert [e["schema_validation"] for e in finished] == ["failed_retry", "passed"]
        assert all(e["output_schema"] is True for e in finished)

    @pytest.mark.asyncio
    async def test_retry_exhausted_reports_both_errors(self, tmp_path):
        """§5-5：两次都不符 ⇒ failure + code=schema_retry_exhausted + 两次错误可读。"""
        tool, provider = _schema_tool(
            tmp_path,
            [AIMessage(content=_BAD_TYPE_JSON), AIMessage(content=_MISSING_FIELD_JSON)],
        )

        result = await tool.execute(_args("coding", "给结论", output_schema=_SIMPLE_SCHEMA))

        assert not result.ok
        assert result.metadata["code"] == "schema_retry_exhausted"
        assert result.metadata["attempts"] == 2
        errors = result.metadata["errors"]
        assert len(errors) == 2, f"两次错误都必须可读：{errors}"
        assert "answer" in errors[0], errors
        assert "answer" in errors[1], errors
        assert len(provider.last_child_sessions) == 2
        finished = _finished_events(result)
        assert [e["schema_validation"] for e in finished] == [
            "failed_retry", "retry_exhausted",
        ]


class TestMissingJson:
    @pytest.mark.asyncio
    async def test_missing_json_retries_and_exhausts(self, tmp_path):
        """§5-6：child 全程不输出 JSON ⇒ missing_json 分类，走重试/耗尽路径。"""
        tool, _provider = _schema_tool(
            tmp_path, [AIMessage(content=_NO_JSON), AIMessage(content=_NO_JSON)],
        )

        result = await tool.execute(_args("coding", "给结论", output_schema=_SIMPLE_SCHEMA))

        assert not result.ok
        assert result.metadata["code"] == "schema_retry_exhausted"
        assert result.metadata["attempts"] == 2
        errors = result.metadata["errors"]
        assert len(errors) == 2
        assert all("missing_json" in e for e in errors), errors

    @pytest.mark.asyncio
    async def test_missing_json_second_attempt_passes(self, tmp_path):
        """§5-6 反面：第一次 missing_json、第二次合法 ⇒ 重试成功。"""
        tool, _provider = _schema_tool(
            tmp_path, [AIMessage(content=_NO_JSON), AIMessage(content=_VALID_JSON)],
        )

        result = await tool.execute(_args("coding", "给结论", output_schema=_SIMPLE_SCHEMA))

        assert result.ok
        payload = json.loads(result.data["output"])
        assert payload["structured"] == {"answer": 42}
        assert result.metadata["attempts"] == 2


class TestRetryConsumesBudget:
    @pytest.mark.asyncio
    async def test_budget_rejection_counts_as_second_failure(self, tmp_path):
        """§5-7：重试占预算；预算打满时第二次被拒 ⇒ schema_retry_exhausted 诚实失败。"""
        tool, provider = _schema_tool(
            tmp_path, [AIMessage(content=_BAD_TYPE_JSON)], max_delegations=1,
        )

        result = await tool.execute(_args("coding", "给结论", output_schema=_SIMPLE_SCHEMA))

        assert not result.ok
        assert len(provider.last_child_sessions) == 1, "第二次没跑起来：child 只有一个"
        assert result.metadata["code"] == "schema_retry_exhausted"
        assert result.metadata["attempts"] == 2
        errors = result.metadata["errors"]
        assert len(errors) >= 2
        assert any("预算" in e for e in errors), errors
        finished = _finished_events(result)
        assert finished[0]["schema_validation"] == "retry_exhausted"
