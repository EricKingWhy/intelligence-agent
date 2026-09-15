"""上下文容量看板数据面（#200）：只读端点 `GET /api/sessions/{id}/context-usage`。

看板是 UI 查询，不是运行事实 ⇒ 分类明细**不进** SessionEvent（不变量 #4：
Event ≠ Diagnostic Log），只经本端点暴露。三份硬约束（设计稿 §3 诚实原则）：

1. ``estimated`` 恒为 true——除 provider usage 外一切数字都是估算；
2. ``cache.state`` 三态：ok / partial / not_collected；**一次都没带回缓存
   明细时不得显示 0%**（not_collected 语义才是诚实口径）；
3. 求和不变式：六桶之和 = used_tokens，差额一律进"其他"残差桶。

数据来源：在途 run 的 builder 最近一次 build 快照（``ContextBuilder.usage_snapshot``，
launch 后未 build 时 used=0 是诚实的"未 build"信号）+ 该会话事件流里的
``model/completed.usage`` / ``run/completed.usage_total`` 汇总。
"""

from __future__ import annotations

import json
from typing import Any

# 工具 schema 估算的 core 名单（设计稿 §3.2.1：不靠名字前缀猜，显式常量集合）。
# assembly.py 的 9 个内置 coding 工具；新增 core 工具必须来这里登记，否则会被
# 算进"工具 · MCP"（单测 test_core_tool_names_pinned 钉住）。
CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "read", "write", "bash", "edit", "apply_patch",
    "glob", "grep", "git_status", "git_diff",
})


def tool_schema_breakdown(definitions: list[dict[str, Any]], estimate_tokens: Any) -> dict[str, int]:
    """工具 schema 的 token 估算（设计稿 §3.2.1）：core 名单内的归"系统"，
    其余（MCP/插件）归"MCP"。估算只读 name/description/parameters 的 JSON——
    只在本端点被调用时计算（不在每轮 build 热路径，design §10）。"""
    system = 0
    mcp = 0
    for definition in definitions:
        cost = estimate_tokens(json.dumps(definition, ensure_ascii=False))
        if definition.get("name") in CORE_TOOL_NAMES:
            system += cost
        else:
            mcp += cost
    return {"system": system, "mcp": mcp}


def cache_summary(events: list[Any]) -> dict[str, Any]:
    """会话事件流的缓存命中汇总（设计稿 §3.1 口径：Σcached ÷ Σinput，求和
    而非逐调用算术平均——调用大小差异会让算术平均失真）。

    三态（诚实口径）：``ok``（全部调用带回）/ ``partial``（部分带回）/
    ``not_collected``（一次都没有——**不得显示 0%**）。分母为 0 视为无数据。
    """
    total_calls = 0
    reported_calls = 0
    cached_sum = 0
    input_sum = 0
    for event in events:
        usage = event.data.get("usage") if isinstance(event.data, dict) else None
        if not isinstance(usage, dict):
            continue
        prompt = usage.get("prompt_tokens")
        if not isinstance(prompt, int) or isinstance(prompt, bool) or prompt <= 0:
            continue
        total_calls += 1
        input_sum += prompt
        cached = usage.get("cached_tokens")
        if isinstance(cached, int) and not isinstance(cached, bool):
            reported_calls += 1
            cached_sum += cached
    if total_calls == 0 or reported_calls == 0:
        return {"state": "not_collected", "reported_calls": reported_calls,
                "total_calls": total_calls, "avg_hit_rate": None}
    avg = cached_sum / input_sum if input_sum > 0 else None
    state = "ok" if reported_calls == total_calls else "partial"
    return {"state": state, "reported_calls": reported_calls,
            "total_calls": total_calls, "avg_hit_rate": avg}


def build_context_usage_payload(
    *,
    settings: Any,
    builder_snapshot: dict[str, Any] | None,
    tool_definitions: list[dict[str, Any]],
    estimate_tokens: Any,
    events: list[Any],
) -> dict[str, Any]:
    """组装端点响应（设计稿 §3.3 形状）。``builder_snapshot is None`` ⇒
    ``state="no_data"``（会话还没有任何 build 快照，各数为 0）。"""
    window_tokens = settings.max_context_tokens
    if builder_snapshot is None:
        return {
            "estimated": True,
            "window_tokens": window_tokens,
            "used_tokens": 0,
            "thresholds": {
                "auto_compact": settings.auto_compact_threshold,
                "hard_guard": settings.hard_guard_threshold,
            },
            "breakdown": {"messages": 0, "system_prompt": 0, "skills": 0,
                          "other": 0, "tools": {"system": 0, "mcp": 0}},
            "cache": {"state": "not_collected", "reported_calls": 0,
                      "total_calls": 0, "avg_hit_rate": None},
            "state": "no_data",
        }

    tools = tool_schema_breakdown(tool_definitions, estimate_tokens)
    messages = builder_snapshot["messages"]
    system_prompt = builder_snapshot["system_prompt"]
    skills = builder_snapshot["skills"]
    # 残差桶（design §3.2"其他"）：builder 上报的**非工具**未归类部分（provider
    # 注入 + 运行期快照）。工具两组是**独立桶**（T4：六桶互斥、可求和），不进残差。
    # 负值（memo 与投影偏差）如实归 0，不造负数。
    other = max(builder_snapshot["other"], 0)
    # 求和不变式（T4）：used_tokens = Σ六桶。builder 口径的 used 不含工具 schema
    # （端点层新算），故在**这里**把工具两组并入（builder 是唯一知道真实注入的地方，
    # 它给不出工具桶——工具定义在 registry，属于装配层）。
    tools_total = tools["system"] + tools["mcp"]
    used_tokens = messages + system_prompt + skills + other + tools_total

    return {
        "estimated": True,
        "window_tokens": window_tokens,
        "used_tokens": used_tokens,
        "thresholds": {
            "auto_compact": settings.auto_compact_threshold,
            "hard_guard": settings.hard_guard_threshold,
        },
        "breakdown": {
            "messages": messages,
            "system_prompt": system_prompt,
            "skills": skills,
            "other": other,
            "tools": tools,
        },
        "cache": cache_summary(events),
        "state": "ok",
    }
