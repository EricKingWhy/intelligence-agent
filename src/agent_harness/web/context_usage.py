"""上下文容量看板数据面（#200）：只读端点 `GET /api/sessions/{id}/context-usage`。

看板是 UI 查询，不是运行事实 ⇒ 分类明细**不进** SessionEvent（不变量 #4：
Event ≠ Diagnostic Log），只经本端点暴露。三份硬约束（设计稿 §3 诚实原则）：

1. ``estimated`` 恒为 true——除 provider usage 外一切数字都是估算；
2. ``cache.state`` 三态：ok / partial / not_collected；**一次都没带回缓存
   明细时不得显示 0%**（not_collected 语义才是诚实口径）；
3. 求和不变式：六桶之和 = used_tokens（**仅当** ``state="ok"``——分类可
   分解时；``usage_only`` 下六桶如实为 0 而总数非 0，理由见下）。

``state`` 三态（#212）：

- ``ok`` —— 有 builder 快照 ⇒ 六桶分类与 used_tokens 同源、可求和；
- ``usage_only`` —— **没有** builder 快照，但事件流里有可用 usage ⇒ 报窗口占用的
  **下界**（最近一次调用的输入规模）+ ``usage_source`` 说明来源，六桶如实为 0
  （分类**不可**分解——不用"其他"桶假装知道）；
- ``no_data`` —— 两者都没有 ⇒ 各数为 0（诚实口径，不伪造）。

为什么需要 ``usage_only``：builder 快照是**进程内**缓存（``AppState.context_snapshots``），
后端一重启就全没了 ⇒ 历史会话的**分类**必然缺席，而事实
（``model/completed.usage``）就在 durable 事件流里。实测（#212）：16 次调用全带
usage 的会话，端点回的是 ``used_tokens=0, state="no_data"`` ⇒ 看板对真实用量
谎报"后端未上报"。

数据来源：在途 run 的 builder 最近一次 build 快照（``ContextBuilder.usage_snapshot``，
launch 后未 build 时 used=0 是诚实的"未 build"信号）+ 该会话事件流里的
``model/completed.usage`` / ``run/completed.usage_total`` 汇总。
"""

from __future__ import annotations

import json
from typing import Any

from agent_harness.session import MODEL_COMPLETED

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


def _usable_usage(event: Any) -> dict[str, Any] | None:
    """事件的 provider usage 能否作为事实（**唯一**判据，两处汇总共用）。

    两条闸门，缺一条就会数错调用：

    1. **类型必须是 ``model/completed``**——"一次模型调用"在事件流里的载体只有它。
       ``model/fallback`` 也带 ``usage``，但那是**同一个** usage 的副本
       （``runtime.py`` 让切换事件自洽，值 = 切换后实际产出本步回答的那次调用），
       按事件数求和会把发生过 fallback 的每一步算成两次调用；
    2. ``prompt_tokens`` 缺失 / 非正整数 ⇒ 不是事实，返回 None（**不猜、不补 0**）。
       ``isinstance(x, bool)`` 排除是必要的：``True`` 也是 ``int``。

    两处口径必须逐字相同——分开各写一遍正是 ``cache`` 与 ``usage_source``
    对同一事件流给出不同调用数的来源。
    """
    if getattr(event, "type", None) != MODEL_COMPLETED:
        return None
    usage = event.data.get("usage") if isinstance(event.data, dict) else None
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    if not isinstance(prompt, int) or isinstance(prompt, bool) or prompt <= 0:
        return None
    return usage


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
        usage = _usable_usage(event)
        if usage is None:
            continue
        total_calls += 1
        input_sum += usage["prompt_tokens"]
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


def last_call_usage(events: list[Any]) -> dict[str, Any] | None:
    """最近一次可用 usage 的取数（#212 ``usage_only`` 的数据面）。

    口径（本票决议，已写进设计稿 §3.3）：``used_tokens`` 用**最近一次调用的
    ``prompt_tokens``**——它就是那次调用真正发出去的输入规模，是"当前窗口占用"
    能给出的**最小真值**（下一轮的输入只会 ≥ 它：还要加本轮回答与新输入）。
    取 ``total_tokens`` 会把本轮回答算进"占用"，量纲变成"上一轮消耗"（Inspector
    的 run tokens 就是那个数，两者同值不同义 = 更难解释的假一致）。两个数都
    返回，将来翻案只改一行 ``used_tokens`` 的取值，不动契约形状。

    没有任何可用 usage ⇒ None（调用方据此落 ``no_data``，不伪造）。
    """
    last: dict[str, Any] | None = None
    calls = 0
    for event in events:
        usage = _usable_usage(event)
        if usage is None:
            continue
        calls += 1
        total = usage.get("total_tokens")
        last = {
            "last_prompt_tokens": usage["prompt_tokens"],
            "last_total_tokens": (
                total if isinstance(total, int) and not isinstance(total, bool) else None
            ),
        }
    if last is None:
        return None
    return {"kind": "last_call_prompt_tokens", "calls_with_usage": calls, **last}


def build_context_usage_payload(
    *,
    settings: Any,
    builder_snapshot: dict[str, Any] | None,
    tool_definitions: list[dict[str, Any]],
    estimate_tokens: Any,
    events: list[Any],
) -> dict[str, Any]:
    """组装端点响应（设计稿 §3.3 形状）。``builder_snapshot is None`` ⇒
    ``usage_only``（事件流里有 usage）或 ``no_data``（两者都没有）。"""
    window_tokens = settings.max_context_tokens
    thresholds = {
        "auto_compact": settings.auto_compact_threshold,
        "hard_guard": settings.hard_guard_threshold,
    }
    empty_breakdown = {"messages": 0, "system_prompt": 0, "skills": 0,
                       "other": 0, "tools": {"system": 0, "mcp": 0}}
    if builder_snapshot is None:
        # cache 与 usage 都从**同一条**事件流汇总（同一 `_usable_usage` 闸门）。
        # ⚠ 本分支的两种态**不对称**，别把功劳记错（2026-09-17 审查澄清）：
        #   - `usage_only`（事件流里有 usage）：`cache` 是**真汇总**——旧版在这里
        #     硬编码 not_collected/0，把"有 12 次调用、其中 5 次带缓存"一并丢掉；
        #   - `no_data`（`usage is None`）：`cache_summary` **结构上恒等于**
        #     not_collected/reported 0/total 0（两个函数共用同一闸门 ⇒ 没有任何
        #     事件过闸），与旧版硬编码逐字段相同。此处仍走同一个函数只为两态**同源**，
        #     它在这个分支拿不出新事实。
        cached = cache_summary(events)
        usage = last_call_usage(events)
        if usage is None:
            return {
                "estimated": True,
                "window_tokens": window_tokens,
                "used_tokens": 0,
                "thresholds": thresholds,
                "breakdown": empty_breakdown,
                "cache": cached,
                "state": "no_data",
            }
        return {
            "estimated": True,
            "window_tokens": window_tokens,
            "used_tokens": usage["last_prompt_tokens"],
            "thresholds": thresholds,
            "breakdown": empty_breakdown,
            "cache": cached,
            "state": "usage_only",
            "usage_source": usage,
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
        "thresholds": thresholds,
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
