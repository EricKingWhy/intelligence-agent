"""Pressure warning 文本预算记账（先计入再注入）回归测试。

W-04 压缩路径落在 [auto, hard) 带时，builder 会向 context 注入一段
pressure warning 文本（``frame:context_pressure`` 的 meta_user_text）。
修复前这段文本不计入任何预算账：``provider_estimate`` 在注入前已算好传给
``_with_providers``（context provider 的 remaining 预算因此虚高），
``usage_snapshot`` 的 "other" 桶也没有它的记账口。对照 plan/paths 块的
「先计入再注入」纪律（``_last_plan_tokens`` / ``_last_modified_paths_tokens``
先估算进估算总量与 ``_last_*`` 记账口再注入），本测试钉住：

1. warning 文本确实被注入（fixture 防漂移）；
2. ``provider_estimate``（``_with_providers`` 入参捕获）与注入后 built 的
   同编码器重算逐 token 相等；
3. ``usage_snapshot()["used_tokens"]`` 与 built 重算相等；
4. ``_last_pressure_warning_tokens`` 记账口等于 warning 文本的独立估算。

fixture 确定性：mock ``compact_now`` 返回 None（"无可压缩早期轮" no-op
路径：零 bracket 写入，走与已压缩共用的收尾装配，warning 判据是同一份）；
MODEL_COMPLETED 不带 usage（避开 #448 usage 锚对阈值判据的干扰）；不传
context providers、不设 system prompt；无 plan 事件（plan_tokens=0）、无
tool 调用（paths=0）。max_context_tokens 按实测 pre（投影消息 + 保护事实
的同一编码器估算）反解，使 provider_estimate 稳定落在 [0.7M, 0.85M) 带内；
编码器固定 cl100k_base，数字可复现。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agent_harness.context.builder import ContextBuilder
from agent_harness.context.tokens import estimate_message_tokens
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session import MODEL_COMPLETED, USER_MESSAGE
from agent_harness.session.derive import (
    derive_messages_with_source_ranges,
    derive_protected_facts,
)
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel


@pytest.mark.asyncio
async def test_pressure_warning_is_counted_into_budget(tmp_path):
    session = make_session(tmp_path)
    session.append(USER_MESSAGE, {"content": "读取旧记录并继续。"})
    # 不带 usage：usage 锚（#448）返回 0，阈值判据走纯估算路径。
    session.append(MODEL_COMPLETED, {"content": "历史分析" * 120})
    session.append(USER_MESSAGE, {"content": "next"})

    model = ScriptedModel([])
    builder = ContextBuilder(model)

    # fixture 反解预算：pre = 投影消息 + 保护事实（与 build 同一编码器口径），
    # 取 M = 4*pre//3 使 pre 落在 [0.7M, 0.85M) 带内且给 warning 文本留足
    # 上侧余量（并入 warning 后仍 < 0.85M，注入判据在修复前后都成立）。
    projected = [
        message for message, _source_range
        in derive_messages_with_source_ranges(session.events)
    ]
    facts_messages = builder._protected_facts_messages(
        derive_protected_facts(session.events)
    )
    pre = estimate_message_tokens(projected)
    if facts_messages:
        pre += estimate_message_tokens(facts_messages)
    max_context_tokens = pre * 4 // 3
    auto_band = 0.7 * max_context_tokens
    hard_band = 0.85 * max_context_tokens
    assert auto_band <= pre < hard_band, (
        f"fixture 未落入带内：pre={pre}, auto={auto_band}, hard={hard_band}"
    )
    builder.max_context_tokens = max_context_tokens

    # mock 压缩：走"无可压缩早期轮" no-op 路径（零 bracket 写入），warning
    # 逻辑与已压缩路径共用同一份收尾装配。
    async def _no_compact(session, **kwargs):
        return None

    builder.compact_now = _no_compact

    # provider_estimate 是 build 局部量：经 _with_providers 入参捕获（不改产品代码）。
    captured: dict = {}
    original_with_providers = builder._with_providers

    async def spy_with_providers(sess, msgs, token_estimate):
        captured["provider_estimate"] = token_estimate
        return await original_with_providers(sess, msgs, token_estimate)

    builder._with_providers = spy_with_providers

    built = await builder.build(session)

    pressure_text = (
        DEFAULT_REGISTRY.assemble("frame:context_pressure").meta_user_text
    )

    # ① 防漂移：warning 文本确实被注入 built（失败多因 fixture 漂移出带）。
    assert pressure_text, "fixture 失效：frame:context_pressure 文本为空"
    assert any(
        isinstance(message, HumanMessage) and message.content == pressure_text
        for message in built
    ), "fixture 未落入带内：pressure warning 未注入 built"

    provider_estimate = captured["provider_estimate"]
    built_tokens = estimate_message_tokens(built)

    # ② 注入后 built 的重算 == 捕获到的 provider_estimate（先计入再注入）。
    assert built_tokens - provider_estimate == 0, (
        f"provider_estimate 漏记 warning：built={built_tokens}, "
        f"provider_estimate={provider_estimate}, 差={built_tokens - provider_estimate}"
    )

    # ③ usage_snapshot 总量 == built 重算（other 桶有 warning 记账口）。
    snapshot = builder.usage_snapshot(session)
    assert snapshot["used_tokens"] - built_tokens == 0, (
        f"usage_snapshot 漏记 warning：used_tokens={snapshot['used_tokens']}, "
        f"built={built_tokens}, 桶={snapshot}"
    )

    # ④ 独立记账口 == warning 文本的独立估算。
    expected_pressure_tokens = estimate_message_tokens(
        [HumanMessage(content=pressure_text)]
    )
    assert getattr(builder, "_last_pressure_warning_tokens", 0) == (
        expected_pressure_tokens
    ), (
        f"_last_pressure_warning_tokens={getattr(builder, '_last_pressure_warning_tokens', 0)}, "
        f"期望={expected_pressure_tokens}"
    )
