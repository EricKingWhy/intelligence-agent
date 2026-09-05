"""JSONL 结构化日志与最小 Agent 执行链路测试。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from agent_harness import cli
from agent_harness.config import Settings
from agent_harness.logging import LogContext, log_context, log_event, setup_logging


def read_jsonl(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines
    return [json.loads(line) for line in lines]


def test_third_party_message_is_system_log_in_shanghai_time(tmp_path: Path):
    log_path = setup_logging(workspace_dir=str(tmp_path / "workspace"))

    logging.getLogger("third_party").warning("一段会变化的第三方消息")

    [entry] = read_jsonl(log_path)
    assert entry["event_type(事件类型)"] == "system_log"
    assert entry["message(消息)"] == "一段会变化的第三方消息"
    assert entry["level(级别)"] == "warn"
    assert entry["schema_version(协议版本)"] == "1.0"
    assert datetime.fromisoformat(entry["timestamp(时间戳)"]).utcoffset().total_seconds() == 8 * 3600


def test_tool_event_keeps_operation_fields_and_omits_none(tmp_path: Path):
    log_path = setup_logging(workspace_dir=str(tmp_path / "workspace"))
    context = LogContext.create()

    with log_context(context):
        log_event(
            logging.getLogger("test_agent"),
            "tool_operation",
            "读取文件完成",
            tool_call_id="call_123",
            operation_id="op_123",
            idempotency_key="read:file.txt",
            tool_name="read_file",
            tool_input={"path": "file.txt"},
            tool_output="内容",
            read_only=True,
            execution_mode="parallel",
            attempt=1,
            max_attempts=1,
            timeout_ms=1000,
            duration_ms=2.5,
            output_truncated=False,
            checkpoint_id=None,
            outcome="success",
        )

    [entry] = read_jsonl(log_path)
    assert entry["event_type(事件类型)"] == "tool_operation"
    assert entry["outcome(结果)"] == "success"
    assert entry["attempt(尝试次数)"] == 1
    assert entry["execution_mode(执行模式)"] == "parallel"
    assert "checkpoint_id" not in entry
    assert entry["trace_id(追踪ID)"] == context.trace_id
    assert all(value is not None for value in entry.values())


@pytest.mark.asyncio
async def test_minimal_agent_success_chain(monkeypatch, tmp_path: Path):
    class FakeModel:
        async def ainvoke(self, messages):
            assert messages[0].content == "只回复 ok"
            return AIMessage(
                content="ok",
                id="lc_run--internal-id",
                response_metadata={
                    "finish_reason": "stop",
                    "id": "provider-response-123",
                },
                usage_metadata={
                    "input_tokens": 4,
                    "output_tokens": 1,
                    "total_tokens": 5,
                    "input_token_details": {"cache_read": 2},
                    "output_token_details": {"reasoning": 0},
                },
            )

    settings = Settings(
        model_api_key="sk-test",
        workspace_dir=str(tmp_path / "workspace"),
        _env_file=None,
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "create_chat_model", lambda config: FakeModel())

    result = await cli.run("只回复 ok")

    assert result == "ok"
    entries = read_jsonl(tmp_path / "logs" / "agent.jsonl")
    assert [entry["event_type(事件类型)"] for entry in entries] == [
        "task_start",
        "agent_start",
        "llm_call",
        "agent_decision",
        "task_completed",
    ]
    assert [entry["step(步骤)"] for entry in entries] == [0, 1, 2, 3, 4]
    assert len({entry["trace_id(追踪ID)"] for entry in entries}) == 1
    assert len({entry["task_id(任务ID)"] for entry in entries}) == 1

    llm_entry = entries[2]
    assert llm_entry["provider(模型提供商)"] == "deepseek"
    assert llm_entry["provider_request_id(提供商请求ID)"] == "provider-response-123"
    assert llm_entry["token_usage(Token用量)"] == {
        "input": 4,
        "output": 1,
        "total": 5,
        "cached_input": 2,
        "reasoning": 0,
    }
    assert llm_entry["outcome(结果)"] == "success"
    assert entries[3]["decision(决策)"] == "finish"


@pytest.mark.asyncio
async def test_minimal_agent_failure_chain(monkeypatch, tmp_path: Path):
    class FailingModel:
        async def ainvoke(self, messages):
            raise TimeoutError("模型请求超时")

    settings = Settings(
        model_api_key="sk-test",
        workspace_dir=str(tmp_path / "workspace"),
        _env_file=None,
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr(cli, "create_chat_model", lambda config: FailingModel())

    with pytest.raises(TimeoutError, match="模型请求超时"):
        await cli.run("触发失败")

    entries = read_jsonl(tmp_path / "logs" / "agent.jsonl")
    assert [entry["event_type(事件类型)"] for entry in entries] == [
        "task_start",
        "agent_start",
        "llm_call",
        "error",
        "task_failed",
    ]
    assert entries[2]["attempt(尝试次数)"] == 1
    assert entries[2]["max_attempts(最大尝试)"] == 1
    assert entries[3]["retryable(可重试)"] is False
    assert entries[3]["error_type(错误类型)"] == "TimeoutError"
    assert "stack_trace(调用栈)" in entries[3]
    assert entries[-1]["outcome(结果)"] == "failure"


# ── B 组加固：formatter 对未知 event_type 显式告警（R4-6）──


def test_formatter_warns_on_unknown_event_type(caplog):
    """绕过 log_event 直接写 extra 的拼错 event_type：仍归一成 system_log，
    但必须发一条 warning（此前静默改写——诊断协议被旁路时无任何痕迹）。"""
    import json
    import logging as std_logging

    from agent_harness.logging import JsonlFormatter

    formatter = JsonlFormatter()
    record = std_logging.LogRecord(
        "agent_harness.test", std_logging.INFO, __file__, 1,
        "msg", None, None,
    )
    record.event_type = "llm_cal"  # 拼错：不在 EVENT_TYPES 白名单
    formatted = formatter.format(record)
    assert json.loads(formatted)["event_type(事件类型)"] == "system_log"
    warnings = [r for r in caplog.records if r.levelno == std_logging.WARNING
                and "llm_cal" in r.getMessage()]
    assert warnings, "未知 event_type 必须产生告警"
