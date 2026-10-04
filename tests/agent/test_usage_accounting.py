"""Gap 1 (P1)：model/completed 与 run/completed 携带 model / usage / cost。

契约（docs/BACKEND_GAP_PROMPT.md，前端 StepDetail MODEL 空槽消费）：
- model/completed.data 新增 `model`（本次推理所用模型名）与 `usage`
  {prompt_tokens, completion_tokens, total_tokens}——从模型响应如实抽取，
  响应没带就省略（绝不伪造，缺失时前端显示「—」）。
- run/completed.data 新增 `usage_total`（本轮聚合，省略当未消费任何 token）、
  `cost_usd`（spec 12 未定义费率表 → 恒 null + TODO，不伪造）、
  `trace_id`（Phase 15 接入 Langfuse 前恒 null）。
"""

from __future__ import annotations

from typing import Annotated

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from agent_harness.agent import AgentRuntime
from agent_harness.session import MODEL_COMPLETED, RUN_COMPLETED
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


class _AddArgs(BaseModel):
    first_number: Annotated[float, Field(...)]
    second_number: Annotated[float, Field(...)]


class AddTool(Tool):
    @property
    def name(self) -> str:
        return "add"

    @property
    def description(self) -> str:
        return "求和。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return _AddArgs

    async def execute(self, args: _AddArgs) -> ToolResult:
        return ToolResult.success(message="ok", data={"sum": args.first_number + args.second_number})


def _usage(input_tokens: int, output_tokens: int) -> dict:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens}


def _events_of_type(session, event_type: str) -> list:
    return [e for e in session.events if e.type == event_type]


def _runtime(scripted: ScriptedModel) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(AddTool())
    return AgentRuntime(scripted, registry, ToolExecutor(registry))


@pytest.mark.asyncio
async def test_model_completed_carries_model_and_usage(tmp_path):
    """响应带 usage_metadata / model_name 时如实映射进 model/completed。"""
    scripted = ScriptedModel([AIMessage(
        content="你好",
        response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata=_usage(1234, 567),
    )])
    session = make_session(tmp_path)
    await _runtime(scripted).run(session, "打个招呼")

    completed = _events_of_type(session, MODEL_COMPLETED)
    assert len(completed) == 1
    assert completed[0].data["model"] == "qwen-plus-0911"
    assert completed[0].data["usage"] == {
        "prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801,
    }

    finished = _events_of_type(session, RUN_COMPLETED)[0]
    assert finished.data["usage_total"] == {
        "prompt_tokens": 1234, "completion_tokens": 567, "total_tokens": 1801,
    }
    # spec 12 未定义费率表：cost 恒 null（TODO），Langfuse 未接入：trace_id 恒 null。
    assert finished.data["cost_usd"] is None
    assert finished.data["trace_id"] is None


@pytest.mark.asyncio
async def test_run_usage_accumulates_across_turns(tmp_path):
    """多轮模型调用：run/completed.usage_total 是各轮之和。"""
    round1 = AIMessage(
        content="",
        response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata=_usage(100, 10),
        tool_calls=[{"name": "add", "args": {"first_number": 1, "second_number": 2},
                     "id": "call_u1", "type": "tool_call"}],
    )
    round2 = AIMessage(content="1 + 2 = 3", response_metadata={"model_name": "qwen-plus-0911"},
                       usage_metadata=_usage(150, 20))
    session = make_session(tmp_path)
    await _runtime(ScriptedModel([round1, round2])).run(session, "计算")

    totals = [e.data["usage"] for e in _events_of_type(session, MODEL_COMPLETED)]
    assert totals == [
        {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        {"prompt_tokens": 150, "completion_tokens": 20, "total_tokens": 170},
    ]
    finished = _events_of_type(session, RUN_COMPLETED)[0]
    assert finished.data["usage_total"] == {
        "prompt_tokens": 250, "completion_tokens": 30, "total_tokens": 280,
    }


@pytest.mark.asyncio
async def test_missing_usage_is_omitted_never_fabricated(tmp_path):
    """响应没带 usage / model：字段省略而不是填 0；usage_total 同理。"""
    scripted = ScriptedModel([AIMessage(content="纯文本，无元数据")])
    session = make_session(tmp_path)
    await _runtime(scripted).run(session, "hi")

    completed = _events_of_type(session, MODEL_COMPLETED)[0]
    assert "usage" not in completed.data
    assert "model" not in completed.data

    finished = _events_of_type(session, RUN_COMPLETED)[0]
    assert "usage_total" not in finished.data
    assert finished.data["cost_usd"] is None
    assert finished.data["trace_id"] is None


@pytest.mark.asyncio
async def test_negative_usage_values_are_dropped_not_aggregated(tmp_path):
    """网关吐负值 token（如 total_tokens=-5）时不得入账聚合。

    AIMessage 自身 pydantic 校验已把非数值 usage 挡在构造期（归因 model 失败，
    语义正确），能流到 usage 聚合的病态形状只剩负整数。负 token 数对账无效，
    入账会污染 usage_total——按"缺失/无效时省略"语义丢弃该条目（不伪造、
    不猜补），同条响应里的合法条目照常入账。
    """
    round1 = AIMessage(
        content="",
        usage_metadata={"input_tokens": -5, "output_tokens": 7, "total_tokens": -5},
        tool_calls=[{"name": "add", "args": {"first_number": 1, "second_number": 2},
                     "id": "call_neg", "type": "tool_call"}],
    )
    round2 = AIMessage(content="1 + 2 = 3", usage_metadata=_usage(10, 5))
    session = make_session(tmp_path)
    await _runtime(ScriptedModel([round1, round2])).run(session, "计算")

    # 第一轮：负值条目被丢弃，合法条目（output_tokens=7）保留。
    first = _events_of_type(session, MODEL_COMPLETED)[0]
    assert first.data["usage"] == {"completion_tokens": 7}

    finished = _events_of_type(session, RUN_COMPLETED)[0]
    # 聚合只含合法条目之和（10+0 / 5+7 / 15+0），绝无负值。
    assert finished.data["usage_total"] == {"completion_tokens": 12,
                                            "prompt_tokens": 10,
                                            "total_tokens": 15}


# ── #552：越界（> 2**63-1）usage 必须转未知，不得 clamp / 记 0 / 让 run 炸 ──

INT64_MAX = 2**63 - 1


def test_usage_extraction_accepts_int64_max_boundary():
    """锚：恰好 `2**63-1` 仍是可记账的合法值（存储上限，不得误伤）。"""
    from types import SimpleNamespace

    from agent_harness.agent.runtime import _usage_from_response

    ai = SimpleNamespace(usage_metadata={"total_tokens": INT64_MAX})
    assert _usage_from_response(ai) == {"total_tokens": INT64_MAX}


def test_usage_extraction_drops_oversize_dimension_to_unknown():
    """越界的 token 维不产出（省略 = 未知），同响应的合法维照常保留；不 clamp、不记 0。"""
    from types import SimpleNamespace

    from agent_harness.agent.runtime import _usage_from_response

    ai = SimpleNamespace(usage_metadata={
        "input_tokens": 5, "output_tokens": 7, "total_tokens": 10**30,
    })
    usage = _usage_from_response(ai)
    assert usage == {"prompt_tokens": 5, "completion_tokens": 7}
    assert "total_tokens" not in usage

    # 全是越界值 ⇒ 整份 usage 无法证明，省略（None），绝不编出 total_tokens
    ai_only = SimpleNamespace(usage_metadata={"total_tokens": 10**30})
    assert _usage_from_response(ai_only) is None


@pytest.mark.asyncio
async def test_oversize_usage_is_omitted_from_run_ledger_never_fabricated(tmp_path):
    """run 级聚合：越界的 `total_tokens` 不得出现（既非 10**30 也非 0）。"""
    scripted = ScriptedModel([AIMessage(
        content="完成",
        response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 10**30},
    )])
    session = make_session(tmp_path)
    await _runtime(scripted).run(session, "hi")

    completed = _events_of_type(session, MODEL_COMPLETED)[0]
    assert completed.data["usage"] == {"prompt_tokens": 100, "completion_tokens": 10}
    assert "total_tokens" not in completed.data["usage"]

    finished = _events_of_type(session, RUN_COMPLETED)[0]
    assert finished.data["usage_total"] == {"prompt_tokens": 100, "completion_tokens": 10}
    assert "total_tokens" not in finished.data["usage_total"]


@pytest.mark.asyncio
async def test_accumulated_usage_overflowing_int64_is_unknown_not_clamped(tmp_path):
    """#552（C1）：两步**各自合法**的 usage 相加溢出 int64 ⇒ 该维转未知。

    与 `storage/delegation_tree.py::record_session_model_requests`（`total if
    total <= INT64_MAX else None`）同口径：合法值相加溢出 ⇒ 该维转未知（`None`，
    粘性），**不 clamp、不记 0**。此前 run 级累加点用 Python bigint 无溢出，
    `usage_total["total_tokens"]` 会永久停在 `2**63` 而 session 账为 `None`
    ——两本账分裂，正是 #552 BUG-R4-02 的同一症状（单步用例覆盖不到）。
    """
    # 每步：total_tokens=2**62（< INT64_MAX ⇒ 单值合法），两步相加 = 2**63 越界。
    # 形状与审查方探针一致：只有 total_tokens 巨大，input/output 保持小值
    # （否则巨量 prompt_tokens 会先撞上无关的 context 收口路径）。
    def _big() -> dict:
        return {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2**62}

    round1 = AIMessage(
        content="",
        response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata=_big(),
        tool_calls=[{"name": "add", "args": {"first_number": 1, "second_number": 2},
                     "id": "call_o1", "type": "tool_call"}],
    )
    round2 = AIMessage(
        content="",
        response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata=_big(),
        tool_calls=[{"name": "add", "args": {"first_number": 3, "second_number": 4},
                     "id": "call_o2", "type": "tool_call"}],
    )
    round3 = AIMessage(
        content="1 + 2 = 3", response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata=_usage(5, 5),
    )
    session = make_session(tmp_path)
    await _runtime(ScriptedModel([round1, round2, round3])).run(session, "计算")

    finished = _events_of_type(session, RUN_COMPLETED)[0]
    total = finished.data["usage_total"]
    # 越界维转未知（`is None`——同时排除 记 0 / clamp 到 INT64_MAX / 不收口留 2**63）
    assert total["total_tokens"] is None
    # 未越界的维照常精确累加（1+1+5 = 7）
    assert total["prompt_tokens"] == 7
    assert total["completion_tokens"] == 7
    # 粘性：转未知后第 3 次的合法加数 10 **补不回来**（非粘性会回归成 10 ——
    # 一个"少算但看着正常"的假账）
    assert total["total_tokens"] is None


# ── #552 C6：本次**缺席**某维 ⇒ run 级该维转未知（与 session 侧同口径）───


def test_accumulate_usage_missing_dim_becomes_unknown():
    """本次响应不带某维 ⇒ 该维**总计不再可知**（`None`），不是"不动这一维"。

    与 session 侧同一规则（`storage/delegation_tree.py::record_session_model_requests`
    的 `usage is None or "total_tokens" not in usage ⇒ 该列 NULL`）。改前 run 级会把
    已知的**旧值**当成完整总计报出去——一个少算的假精确数：run 账停在旧值、session
    账转 `NULL`，两本账**值级**分裂（#552 C6；不是"形状不同"）。
    """
    from agent_harness.agent.runtime import _accumulate_usage

    total: dict[str, int | None] = {}
    _accumulate_usage(total, {"total_tokens": 100, "prompt_tokens": 90})
    assert total == {"total_tokens": 100, "prompt_tokens": 90}

    # 第 2 次不报 total_tokens：该维转未知；本次报的 prompt_tokens 照常累加
    _accumulate_usage(total, {"prompt_tokens": 7})
    assert total == {"total_tokens": None, "prompt_tokens": 97}

    # 粘性：第 3 次重新报上该维也补不回缺的那一块
    _accumulate_usage(total, {"total_tokens": 3, "prompt_tokens": 1})
    assert total == {"total_tokens": None, "prompt_tokens": 98}


def test_accumulate_usage_absent_usage_poisons_tracked_dims_only():
    """整份 usage 缺席 ⇒ 已跟踪维全部转未知；**不**凭空造出 provider 没报过的维。"""
    from agent_harness.agent.runtime import _accumulate_usage

    total: dict[str, int | None] = {"total_tokens": 110}
    _accumulate_usage(total, None)
    assert total == {"total_tokens": None}

    fresh: dict[str, int | None] = {}
    _accumulate_usage(fresh, None)
    assert fresh == {}  # 从未跟踪过任何维 ⇒ 也不该长出一个 None 维


@pytest.mark.asyncio
async def test_absent_usage_in_later_response_turns_run_total_unknown(tmp_path):
    """端到端：第 2 轮响应**整份 usage 缺席** ⇒ run 账已跟踪维全部转 `None`。

    改前 RED：`usage_total` 停在第 1 轮的值 `{"prompt_tokens": 100,
    "completion_tokens": 10, "total_tokens": 110}` —— 一个少算的假精确数，而 session
    树账同一步已转 `NULL`（两本账值级分裂，正是 #552 BUG-R4-02 的同一症状）。
    与 `tests/web/test_budget_int64_bounds_api.py::test_dim_missing_in_later_response_
    keeps_two_ledgers_consistent`（某维**被丢弃**，只毒该维）互补：这里走"整份缺席"支。
    """
    round1 = AIMessage(
        content="",
        response_metadata={"model_name": "qwen-plus-0911"},
        usage_metadata=_usage(100, 10),
        tool_calls=[{"name": "add", "args": {"first_number": 1, "second_number": 2},
                     "id": "call_c6", "type": "tool_call"}],
    )
    round2 = AIMessage(content="1 + 2 = 3")  # provider 一个字都没报
    session = make_session(tmp_path)
    await _runtime(ScriptedModel([round1, round2])).run(session, "计算")

    finished = _events_of_type(session, RUN_COMPLETED)[0]
    assert finished.data["usage_total"] == {
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
    }
