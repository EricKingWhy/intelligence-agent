"""Phase 16 Final Full E2E Gate（T1, #126, ADR-0019）。

薄编排层（D1）：单个 test_critical_path_e2e 用 ScriptedModel 跑简化 4 轮
关键路径，证明核心能力可串联：

    retrieve_knowledge（KB 不足 is_sufficient=false）
      → web_search（外部检索，带 citation）
      → add（coding 代理，统一 ToolExecutor）
      → 模型最终回答（含 web citation）

分段独立断言（ADR-0019 D1）：本文件只承载薄编排层 critical path。
其余分段（kill/restart/reconcile、sandbox restore、replay/fork、Langfuse
trace、Eval report）由各自独立 test function 或独立 gate 文件承担，不
混进本 critical path（维护成本/CI 信号噪声比的权衡见 ADR）。

跑法：
    .venv/Scripts/python.exe -m pytest tests/integration/test_phase16_gate.py -m integration -v
"""

from __future__ import annotations

import pytest

from agent_harness.agent import AgentRuntime
from agent_harness.session import (
    MODEL_COMPLETED,
    RUN_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
)
from agent_harness.tooling import ToolExecutor
from evaluation.assertions import dangling_tool_call_ids
from tests.conftest import make_session
from tests.integration._phase16_helpers import (
    PHASE16_WEB_URL,
    build_critical_path_registry,
    critical_path_scripted_model,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.asyncio
async def test_critical_path_e2e(tmp_path):
    """薄编排层：4 轮简化关键路径全链通（ADR-0019 D1）。

    断言维度（ADR-0019 D8 Gate 指标的薄编排层子集）：
    - 事件序列完整：MODEL_COMPLETED × 4 + TOOL_CALL × 3 + TOOL_RESULT × 3
    - dangling tool call = 0（所有 tool_call 都有配对 tool_result）
    - citation 在场：web citation 格式合法且出现在最终回答
    - run terminal：RUN_COMPLETED 事件 + result.status 正常终结
    """
    registry = await build_critical_path_registry(tmp_path)
    model = critical_path_scripted_model()
    session = make_session(tmp_path / "sess")
    runtime = AgentRuntime(
        model, registry, ToolExecutor(registry),
    )

    result = await runtime.run(session, "请回答：python typing 是什么？并用 add 计算 1+2。")

    events = session.events

    # ── 1. run terminal ──
    assert result.final_text, "薄编排层必须有最终回答"
    assert any(e.type == RUN_COMPLETED for e in events), "RUN_COMPLETED 事件必须在场"
    assert result.status == "completed", f"run 必须正常终结，实际 status={result.status}"

    # ── 2. 事件序列完整（分段独立断言：薄编排层只断关键路径形状）──
    tool_calls = [e for e in events if e.type == TOOL_CALL]
    tool_results = [e for e in events if e.type == TOOL_RESULT]
    model_completed = [e for e in events if e.type == MODEL_COMPLETED]
    assert len(tool_calls) == 3, f"关键路径 3 次工具调用，实际 {len(tool_calls)}"
    assert len(tool_results) == 3, f"关键路径 3 次工具结果，实际 {len(tool_results)}"
    assert len(model_completed) >= 4, (
        f"4 轮模型调用至少 4 个 MODEL_COMPLETED，实际 {len(model_completed)}"
    )

    # 工具调用顺序 = 编排剧本顺序（kb → web → add）
    tool_names = [e.data.get("tool_name") for e in tool_calls]
    assert tool_names == ["retrieve_knowledge", "web_search", "add"], (
        f"工具调用顺序必须匹配剧本，实际 {tool_names}"
    )

    # ── 3. dangling tool call = 0（Gate 指标 D8）──
    dangling = dangling_tool_call_ids(events)
    assert dangling == [], f"悬空 tool_call 必须为零，实际 {dangling}"

    # ── 4. citation 在场 + 合法 ──
    # TOOL_RESULT.content = ToolResult 外层 JSON；data.output 是再次 JSON 序列化
    # 的领域 payload（KB/web 工具同款契约）。两层都解才能拿到 is_sufficient 与
    # citation 字段——避免对转义细节做脆弱字符串匹配。
    import json

    def _tool_payload(result_event) -> dict:
        outer = json.loads(result_event.data["content"])
        return json.loads(outer["data"]["output"])

    # retrieve_knowledge：is_sufficient 必须为 False（编排前提：KB 证据不足
    # → 触发 Retrieval Fallback affordance）
    kb_payload = _tool_payload(tool_results[0])
    assert kb_payload["is_sufficient"] is False, (
        f"KB 证据必须如实标记不足，实际 is_sufficient={kb_payload['is_sufficient']}"
    )
    assert kb_payload["hint"], "证据不足时 KB 工具必须下发 web_search affordance hint"

    # web_search：citation 格式合法（web:<url>）
    web_payload = _tool_payload(tool_results[1])
    assert web_payload["hits"], "fake web provider 默认文档必须命中 Python 官方文档"
    assert web_payload["hits"][0]["citation"].startswith("web:http"), (
        f"web citation 格式必须为 web:<url>，实际 {web_payload['hits'][0]['citation']!r}"
    )

    # 最终回答引用 web 来源（编排剧本第 4 轮的 AIMessage.content）
    assert f"web:{PHASE16_WEB_URL}" in result.final_text, (
        f"最终回答必须引用 web citation，实际 final_text={result.final_text!r}"
    )
