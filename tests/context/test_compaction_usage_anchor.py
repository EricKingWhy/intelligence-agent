"""#448：压缩触发判定必须以「上一轮响应真实 input tokens」为锚（Pi 同构）。

背景与实测：`docs/agents/pi-research-2026-09/synthesis-report.md` §3 + #448 偏差实测。
触发判定原为纯 tiktoken cl100k_base 估算（`builder.py` 的 `token_estimate`），
真实 provider usage 只进账目。实测（MiMo / GLM 两 provider，同会话逐样本）：

- 常规文本：估算**高**估（安全方向，est/real 1.05–1.65）；
- **数字/十六进制密集**内容（tool 结果常见：coverage、JSON、sha 列表、ls 输出）：
  估算**低**估，8 轮真实投影 ratio 低至 0.674 → 估算读到 140k（0.70 阈值）时真实
  已 ~207k，越 200k 窗口；估算到硬护栏 170k 时真实 ~252k。

上游 Pi HEAD（`packages/agent/src/harness/compaction/compaction.ts:214-243`
`estimateContextTokens`）同构：以最近一条带 usage 的 assistant 消息的真实 token
为锚 + 其后消息估算增量；其 `overflow.ts` 还对 MiMo 系专门加了静默溢出检测。

锚实现约束（测试逐一钉住）：
- 锚经 source_ranges 反查定位（`(seq, seq)` 唯一 AIMessage 区间）⇒ **跨 bracket
  存活**（压缩后「事件数 == 消息数」永久失配，按计数配对的锚会失效）；
- `max()` 语义**只抬高不降低**——绝不把既有保守估计往下拉；
- 无 usage / 事件全被 shadow ⇒ 锚 0，回落纯估算（既有行为零回归）；
- 锚 = 该轮真实 prompt_tokens + 其响应消息估算 + 其后新增消息估算（响应消息
  不在 prompt_tokens 里，漏掉它 = 系统性低估长回复轮）。

尺寸说明（tiktoken cl100k_base 实测，estimate_message_tokens 含 dump 开销）：
摘要组装的第 0 节内嵌**全部 user 消息全文**、AI 大块内容不内嵌 ⇒ 测试把大头放
AI 消息，保证 shrink 闸门（summary < early）通过。摘要 MODEL_SECTIONS≈108；
AI「历史分析」×100≈540、×200≈1040；AI "ok"≈41；Human "next"≈27。
**保护事实同样改判定值**：`token_estimate += protected_facts_tokens` 发生在锚
之后——user_goal 内嵌首条 user 消息全文 + 包装开销 ⇒ 37-token 的 user1 计
~255、1225-token 的 user1 计 ~1453。变异测试（M2 覆盖语义 / M3 漏响应消息）
的尺寸必须给两侧都留余量，否则保护事实会把变异总数抬回阈值以上而漏网
（探针 `D:/tmp/probe_448_m2m3_sizing.py` 实测定尺寸）。
阈值统一 10000×0.3=3000；判定两侧最小余量 211 token（tiktoken 确定性）。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.session import (
    COMPACTION_END,
    MODEL_COMPLETED,
    USER_MESSAGE,
)
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

MODEL_SECTIONS = """## 已完成工作与关键决策
已完成读取历史记录，并选择直接展示内容。

## 失败方案
(none)

## 当前进行中状态
摘要覆盖的历史工作已完成。

## Next Step
等待当前请求继续。"""


def _usage(prompt_tokens: int) -> dict:
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": 1,
        "total_tokens": prompt_tokens + 1,
    }


def _has_bracket(session) -> bool:
    return any(e.type == COMPACTION_END for e in session.events)


@pytest.mark.asyncio
async def test_real_usage_anchor_triggers_when_estimate_alone_would_not(tmp_path):
    """真实 usage 超阈值、消息估算不超 ⇒ 必须触发压缩（锚被使用）。

    形态：上一轮真实 prompt 4000 token（system/tools 等不在投影里，投影只有
    604）——纯估算漏判，锚拦住。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
    session.append(MODEL_COMPLETED, {"content": "历史分析" * 100, "usage": _usage(4000)})
    session.append(USER_MESSAGE, {"content": "next"})

    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    builder = ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    )
    await builder.build(session)

    assert _has_bracket(session), (
        "真实 usage.prompt_tokens=4000 已越过阈值 3000，压缩必须触发；"
        "未触发说明触发判定仍只看本地估算、忽略真实 usage（#448）"
    )


@pytest.mark.asyncio
async def test_no_usage_falls_back_to_estimate_no_regression(tmp_path):
    """无 usage 数据 ⇒ 纯估算路径不变（既有行为零回归）。"""
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "hi"})
    session.append(MODEL_COMPLETED, {"content": "ok"})  # 无 usage
    session.append(USER_MESSAGE, {"content": "next"})

    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    builder = ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    )
    await builder.build(session)

    assert not _has_bracket(session), (
        "无 usage 数据时必须回落纯估算；估算远低于阈值却触发压缩 = 回归"
    )


@pytest.mark.asyncio
async def test_anchor_never_lowers_below_estimate(tmp_path):
    """`max()` 语义：usage 报得偏小时**不得**把估计拉低（安全方向）。

    纯投影 ~2544（+保护事实 ~684 ⇒ 3228）> 阈值；真实 usage 只报 5 —— 锚
    （5 + 响应 ~2014 + 尾部 28 ≈ 2047）若**覆盖**而非取 max，加保护事实也只有
    ~2716 < 3000，就不触发。三方余量（触发 +228 / M2 捕获 +284 / shrink
    +1199，探针 `D:/tmp/probe_448_m2m3_sizing.py` 实测）：AI 放大或 user1 缩小
    都会让保护事实或摘要内嵌把其中一边抬爆——user1 ×123 是探出来的平衡点。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "旧记录 " * 123})
    session.append(MODEL_COMPLETED, {"content": "历史分析" * 380, "usage": _usage(5)})
    session.append(USER_MESSAGE, {"content": "current request"})

    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    builder = ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    )
    await builder.build(session)

    assert _has_bracket(session), (
        "投影 ~2544 + 保护事实 ~684 = 3228 已超阈值 3000 必须照旧触发；"
        "真实 usage 偏小时锚不得把估计拉低（覆盖语义会掉到 ~2716 不触发）"
    )


@pytest.mark.asyncio
async def test_anchor_counts_the_response_message_itself(tmp_path):
    """锚必须补上响应消息自身：prompt_tokens 只覆盖输入，AI 回复不在其中。

    2500 + 响应消息(~540) + 尾部(27) = 3067，加保护事实 255 ⇒ 3322 > 3000
    触发；漏掉响应消息则 2527 + 255 = 2782 < 3000 不触发——本用例专门钉住
    这个系统性低估项。usage 取 2500 而非更高：给「漏响应」留出 218 的
    不触发余量（2900 时保护事实会把漏响应总数抬回阈值以上，变异漏网）。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
    session.append(
        MODEL_COMPLETED,
        {"content": "历史分析" * 100, "usage": _usage(2500)},
    )
    session.append(USER_MESSAGE, {"content": "next"})

    model = ScriptedModel([AIMessage(content=MODEL_SECTIONS)])
    builder = ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    )
    await builder.build(session)

    assert _has_bracket(session), (
        "锚 = prompt_tokens(2500) + 响应消息估算(~540) + 尾部(27) + 保护事实 "
        "= 3322 已越阈值，必须触发；漏算响应消息是系统性低估（长回复轮）"
    )


@pytest.mark.asyncio
async def test_anchor_survives_compaction_bracket(tmp_path):
    """bracket 之后锚必须仍然可用（range 反查，不依赖事件/消息计数配对）。

    build #1 靠锚触发压缩；build #2 的投影只剩摘要+新消息（纯估算 ~250），
    但新 model/completed 带真实 usage 3500 ⇒ 必须再次发起压缩尝试（摘要闸门
    是否放行不论——断言的是「尝试发生了」，即锚活着）。
    压缩后「事件数 == 消息数」永久失配——按计数配对的锚在这里会静默失效。
    """
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
    session.append(MODEL_COMPLETED, {"content": "历史分析" * 100, "usage": _usage(4000)})
    session.append(USER_MESSAGE, {"content": "next"})

    model = ScriptedModel([
        AIMessage(content=MODEL_SECTIONS),
        AIMessage(content=MODEL_SECTIONS),
        AIMessage(content=MODEL_SECTIONS),
    ])
    builder = ContextBuilder(
        model, max_context_tokens=10000, auto_compact_threshold=0.3,
    )
    await builder.build(session)
    assert _has_bracket(session), "前置：第一轮必须已触发压缩"
    assert len(model.snapshots) == 1

    session.append(USER_MESSAGE, {"content": "继续"})
    session.append(MODEL_COMPLETED, {"content": "ok", "usage": _usage(3500)})
    await builder.build(session)

    assert len(model.snapshots) >= 2, (
        "bracket 后新 usage(3500) 已越阈值，必须再次发起压缩尝试；"
        "未尝试说明锚在压缩后失效（事件/消息计数配对被 bracket 打破）"
    )
