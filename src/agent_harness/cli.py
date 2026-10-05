"""Agent CLI（Phase 9 CLI Renderer）：驱动 AgentRuntime.run_stream 渲染事件流。

CLI 与 SSE 是同一 AgentEvent 流的两个消费端（spec 11 §1）：渲染是事件流的
纯函数，只挑人要看的（流式正文 / 工具行 / 终态），完整事实源是 Session
JSONL；Diagnostic Log 由 runtime 的 _log 统一产出（CLI 只负责 setup_logging，
不再手搓 llm_call 链路）。最小 CLI 不装配工具（registry 为空——模型直接答复）。

渲染约定借鉴 pi-mono / oh-my-pi（均为 MIT License，设计级借用 + 小工具重实现）：
- 状态行语法 `glyph 标题 折叠参数 · meta`（oh-my-pi tui/status-line.ts）
- 参数折叠 key=value、结果尾部预览 + `... +N more lines`（pi renderers/bash.ts）
- 时长徽章、token 用量页脚 + K/M 压缩（pi footer.ts formatTokens/formatDuration）
- ascii 符号路线（oh-my-pi theme/symbols.ts 的 ascii preset）——Windows GBK
  控制台对 ✔/⏳ 等 glyph 会抛 UnicodeEncodeError，ascii 永远可打印。
License 署名：pi-mono © 2025 Mario Zechner（MIT）；oh-my-pi © 2025-2026
Can Bölük、© 2026 Stencil Labs, Inc.（MIT）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from agent_harness.agent import AgentEvent
from agent_harness.agent.budget import (
    BudgetConflict,
    BudgetRejection,
    resolve_local_fuse,
)
from agent_harness.agent.profiles import declared_turn_ceiling
from agent_harness.agent.resume_evidence import evidence_port
from agent_harness.agent.run_budget import (
    REASON_STUCK,
    RESUME_BASIS_BUDGET_INCREASE,
    RESUME_BASIS_RELEVANT_STEER,
    STUCK_RESUME_REQUIREMENTS,
    TRIGGER_RUN_DEADLINE,
    TRIGGER_SESSION_DEADLINE,
    LaunchRunBudget,
    latest_paused_run,
    run_limits_from_request,
    session_limits_from_request,
    tool_name_of_dimension,
)
from agent_harness.assembly import (
    assemble_wiring,
    build_runtime,
    initialize_stores,
    recovery_stores,
)
from agent_harness.config import Settings
from agent_harness.identity import IdentityContext
from agent_harness.instance_lock import InstanceLock, InstanceLockError
from agent_harness.logging import LogContext, log_context, setup_logging
from agent_harness.memory.types import memory_session_var
from agent_harness.model.accounting import HARNESS_MODEL_ACCOUNTING
from agent_harness.model.config import ConfigError, ModelConfig
from agent_harness.observability import flush_process_sink
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import (
    AGENT_DELEGATION_FINISHED,
    AGENT_DELEGATION_STARTED,
    ARTIFACT_CREATED,
    ARTIFACT_EXTERNALIZED,
    CONTEXT_COMPACTED,
    GUARD_STUCK,
    MODEL_COMPLETED,
    MODEL_FAILED,
    MODEL_FALLBACK,
    OPERATION_RECONCILE_REQUIRED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RESUMED,
    SESSION_FORKED,
    TEXT_DELTA,
    TOOL_CALL,
    TOOL_FAILURE_GUARD,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
    SessionEvent,
)
from agent_harness.session.errors import (
    ActiveRunConflict,
    InvalidSessionId,
    SessionNotFound,
)
from agent_harness.session.fork import (
    ForkBoundaryError,
    TailSummarizer,
    find_fork_boundaries,
    fork_session,
)
from agent_harness.session.lineage import (
    build_lineage_index,
    build_lineage_tree,
    render_lineage_tree,
)
from agent_harness.session.service import SessionContextCompaction
from agent_harness.storage.delegation_tree import SessionBudgetHandle
from agent_harness.storage.sqlite import SqliteSessionMetaStore
from agent_harness.tooling.approve_policy import ApprovePolicyStore
from agent_harness.tooling.contract import PermissionPolicy

_ARGS_LINE_LIMIT = 120
_PREVIEW_LINES = 3

#: 陈旧账行名清除通道（#616）的 CLI 入口标识，写进 `session_budget_events` 审计的
#: `source` 字段（与 Web 的 `PURGE_ENTRY_API` 同款，只是入口不同）。
PURGE_ENTRY_CLI = "cli"

#: 手动上下文压缩通道（#635）的 CLI 入口标识，与 `COMPACT_ENTRY_API`（Web）同款、
#: 值不同（`agent_harness/session/service.py` 的注释预告了这一对应关系）。
COMPACT_ENTRY_CLI = "cli"


class StreamRenderer:
    """AgentEvent → 终端文本（事件流的纯函数；write 注入便于测试）。

    行式追加输出（无差分重绘）：delta 原样续写；工具块 = 空行 + 状态行 +
    结果预览；终态行补齐换行。model/completed、user/message 等持久化镜像
    一律静默——终端不是第二份事件日志。
    """

    def __init__(self, write: Callable[[str], None]) -> None:
        self._write = write
        self._delta_open = False  # 流式正文输出中：工具行/终态行前先补换行

    def handle(self, event: AgentEvent) -> None:
        if event.type == TEXT_DELTA:
            self._write(event.data["delta"])
            self._delta_open = True
        elif event.type == TOOL_CALL:
            self._end_delta()
            args = _collapse_args(event.data.get("args") or {})
            suffix = f" {args}" if args else ""
            self._write(f"\n[tool] {event.data['tool_name']}{suffix}\n")
        elif event.type == TOOL_RESULT:
            self._render_result(event.data)
        elif event.type == RUN_COMPLETED:
            self._end_delta()
            self._write("\n")
            usage = event.data.get("usage_total") or {}
            if usage:
                self._write(f"tokens: in {_format_tokens(usage.get('prompt_tokens'))}, "
                            f"out {_format_tokens(usage.get('completion_tokens'))}\n")
        elif event.type == RUN_FAILED:
            self._end_delta()
            reason = event.data.get("reason")
            suffix = f" ({reason})" if reason else ""
            self._write(f"\n[run failed]{suffix}\n")
        elif event.type == RUN_PAUSED:
            # `#312`：暂停**不是**失败——独立成块，让终端能把 paused 与 failed
            # 分开（同一份 durable data，与 replay / Web 显示的事实一致）。
            self._end_delta()
            self._write(render_pause_block(event.data))
        elif event.type == RUN_RESUMED:
            self._end_delta()
            self._write(render_resume_block(event.data))

    def _render_result(self, data: dict) -> None:
        try:
            result = json.loads(data["content"])
        except (KeyError, ValueError):
            self._write("  [fail] (unparseable result)\n")
            return
        status = "[ok]" if result.get("ok") else "[fail]"
        duration = result.get("metadata", {}).get("duration_ms")
        suffix = f" ({duration / 1000:.1f}s)" if isinstance(duration, (int, float)) else ""
        self._write(f"  {status}{suffix}\n")
        message = result.get("message") or ""
        lines = message.splitlines()
        for line in lines[:_PREVIEW_LINES]:
            self._write(f"  {line}\n")
        if len(lines) > _PREVIEW_LINES:
            self._write(f"  ... +{len(lines) - _PREVIEW_LINES} more lines\n")

    def _end_delta(self) -> None:
        if self._delta_open:
            self._write("\n")
            self._delta_open = False


def _collapse_args(args: dict) -> str:
    """一行折叠工具参数：key=value，字符串含空格才加引号；整体超限截断。

    折叠约定借鉴 oh-my-pi formatArgsInline（key=value 预算内联）；嵌套结构
    压成紧凑 JSON（本地快失败用不到嵌套语义，终端只要能认出调用形状）。
    """
    parts: list[str] = []
    for key, value in args.items():
        if isinstance(value, str):
            text = f'"{value}"' if (" " in value or not value) else value
        elif isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value)
        parts.append(f"{key}={text}")
    line = " ".join(parts)
    if len(line) > _ARGS_LINE_LIMIT:
        line = line[:_ARGS_LINE_LIMIT] + "..."
    return line


def _format_tokens(count: int | None) -> str:
    """token 数 → 紧凑文本（借鉴 pi footer.ts formatTokens 的 K/M 压缩）。"""
    if not isinstance(count, int) or count < 0:
        return "?"
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.1f}M"


# ── 暂停 / 恢复渲染（#312，PRD §11 CLI behavior）──────────────────────────


def _continuation_lines(continuation: dict) -> list[str]:
    """`continuation` → 缩进后的若干行（`completed` / `remaining` / `blockers` /
    `next_safe_action`；空字段不渲染——不拿空行冒充信息）。"""
    lines: list[str] = []
    for key, label in (("completed", "completed"), ("remaining", "remaining"),
                       ("blockers", "blockers")):
        items = continuation.get(key)
        if isinstance(items, list) and items:
            lines.append(f"    {label}: {items[0]}")
            for item in items[1:]:
                lines.append(f"      {item}")
    action = continuation.get("next_safe_action")
    if action:
        lines.append(f"    next: {action}")
    return lines


def _pause_facts(data: dict) -> dict:
    """从 `run/paused` / `run/resumed` 的 data 里取两档数字（缺键 = unavailable）。"""
    limits = data.get("limits") or {}
    run_limits = limits.get("run") or {}
    local_limits = limits.get("local") or {}
    consumed = data.get("consumed") or {}
    turns = consumed.get("agent_turns")
    ceiling = run_limits.get("max_agent_turns_total")
    # 缺 consumed 就不能算 remaining：`11 §6.1` 的「不可得 ≠ 0」对**推导量**同样成立
    # （否则畸形事件下会同时打印 consumed=unavailable 与一个像模像样的 remaining）。
    remaining = None if ceiling is None or turns is None else max(ceiling - turns, 0)
    return {
        "turns": turns,
        "ceiling": ceiling,
        "remaining": remaining,
        "local": local_limits.get("max_agent_turns"),
        # 缺失一律显示 unavailable，**永不**用 0 顶替（`11 §6.1`）。
        "consumed_text": "unavailable" if turns is None else str(turns),
        "ceiling_text": "unlimited" if ceiling is None else str(ceiling),
        "remaining_text": (
            "unavailable" if remaining is None else str(remaining)
        ),
    }


#: run 作用域另外三个维度（`#313`）：`(data 里的 consumed 键, limits 里的 ceiling 键)`。
#: 顺序与 `agent/run_budget.TRIGGER_ORDER` 一致（turns 已单独渲染，见 `_pause_facts`）。
_EXTRA_RUN_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("model_requests", "max_model_requests"),
    ("total_tokens", "max_total_tokens"),
    ("cost_usd", "max_cost_usd"),
)


def _tool_dimension_lines(data: dict, *, carried: bool = False) -> list[str]:
    """per-tool 配额摘要行（`#314`）：**只**渲染有事实可说的工具。

    "有事实可说" = 配过 ceiling（`limits.run.tool_call_limits`）或账上已有接纳计数
    （`consumed.tool_calls_by_tool`）。老事件（`#314` 之前）两个键都没有 ⇒ 零行
    ——与 `_extra_dimension_lines` 同一条纪律（不拿一片 `unavailable` 当信息）。

    `tool_calls`（逻辑调用）与 `tool_attempts`（真实尝试，含 retry）**分别**打出来：
    `02 §5.1` 明文不许把它们混同，挤进一格会让人以为它们是同一个计数器。

    读数的判据是"**表在不在**"，不是"键在不在"（`agent/run_budget.BudgetConsumed.calls_for`
    的同一口径：`{}` = 已知且一个都没调用，`None` = 未知）。反例（两轴审查共同发现）：
    配了 ceiling 却从未调用过的工具（`{"bash": 3}` + `{}`）曾被渲染成 `unavailable calls`，
    而**同一份** durable 事件的服务端投影给的是 `remaining 3`——同一事实两个互相矛盾的读数，
    正是 `11 §6.1` 要消灭的那种不一致。

    表在而**那一格的值**形状不合（非整数 / 负数 / 布尔）⇒ 仍报 `unavailable`，**不**印那个
    值、也不当 0：认不出的读数不可得（与 `_dimension_remaining` 同一条收窄纪律）。这不是
    可达输入——唯一的写入者是接纳点的 `budget_delta`（恒为非负整数）——而是"不编数字"的
    兜底；前端在同一格上会显示 0（它的解析器把畸形条目整条丢掉），差异登记在 ADR-0045 §6。
    """
    limits = ((data.get("limits") or {}).get("run") or {}).get("tool_call_limits") or {}
    consumed = data.get("consumed") or {}
    calls = _per_tool_table(consumed, "tool_calls_by_tool")
    attempts = _per_tool_table(consumed, "tool_attempts_by_tool")
    names = set(limits)
    for table in (calls, attempts):
        if table is not None:
            names |= set(table)
    lines: list[str] = []
    for name in sorted(names):
        ceiling = limits.get(name)
        used = _per_tool_count(calls, name)
        tried = _per_tool_count(attempts, name)
        lines.append(
            f"  tool {name}: {'carried ' if carried else ''}consumed "
            f"{'unavailable' if used is None else used} calls"
            f" / {'unavailable' if tried is None else tried} attempts"
            f" / limit {'unlimited' if ceiling is None else ceiling}"
            f" (remaining {_dimension_remaining(used, ceiling)})\n"
        )
    return lines


def _per_tool_table(consumed: dict, key: str) -> dict | None:
    """从 durable `consumed` 里取一张 per-tool 表：**认不出形状一律 None（未知）**。

    三种情况各不相同，`or {}` 会把前两种合成一种：
      * 键缺席（`#314` 之前的老事件）⇒ 未知；
      * 值是 `None`（显式未知）⇒ 未知；
      * 值是 `{}` ⇒ **已知**且一个都没调用。
    """
    table = consumed.get(key)
    return table if isinstance(table, dict) else None


def _per_tool_count(table: dict | None, name: str) -> object:
    """per-tool 表里某工具的读数：表未知 ⇒ None（unavailable），缺名 ⇒ 0，值畸形 ⇒ None。

    `None` 与 0 是两件事（`11 §6.1`：不可得 ≠ 0），所以**只**在"表在、这个名字也在、
    值是非负整数"三者同时成立时才给出数字。
    """
    if table is None:
        return None
    value = table.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _dimension_remaining(consumed: object, ceiling: object) -> str:
    """某一维度的 remaining 文案（不可得 / 不可算一律 unavailable，**永不** 0）。

    形参类型是 `object`：读数来自 durable 事件的 `data`（JSON 形状不受类型系统
    约束），本函数**逐个 `isinstance` 收窄**，认不出的形状一律 unavailable。

    cost 在 wire 上是十进制**字符串**：差值必须在 `Decimal` 里算（`float()` 会引入
    与 wire 不等价的近似，`11 §6.1`）。任一侧形状不合 ⇒ unavailable——推算不出
    剩余时如实说不知道，比编一个 0 更接近事实。
    """
    if ceiling is None or consumed is None:
        return "unavailable"
    try:
        if isinstance(consumed, str) or isinstance(ceiling, str):
            left, right = Decimal(str(ceiling)), Decimal(str(consumed))
            return format(max(left - right, Decimal(0)), "f")
        if isinstance(consumed, bool) or isinstance(ceiling, bool):
            return "unavailable"
        if isinstance(consumed, int) and isinstance(ceiling, int):
            return str(max(ceiling - consumed, 0))
    except (InvalidOperation, ValueError):
        return "unavailable"
    return "unavailable"


def _extra_dimension_lines(data: dict, *, carried: bool = False) -> list[str]:
    """另外三个 run 维度的摘要行（**只**渲染有事实可说的维度）。

    "有事实可说" = 配了 ceiling 或消耗快照里有这个键。老暂停事件（`#313` 之前）
    只有 turns 一维，多打三行 `unavailable / unlimited` 是噪声不是信息——而 CLI
    显示的是同一份 durable 投影，过去与现在的输出都必须是同一份事实的忠实渲染。

    `carried=True` 只用于 `run/resumed` 块：那几行读的是**沿用过来**的计数与新的
    ceiling（恢复不重置，`03 §5`），与同一块里 `turns` 行的 "carried" 同一语义；
    暂停块里它们则是本轮的即时读数，不加这个字。
    """
    limits = ((data.get("limits") or {}).get("run") or {})
    consumed = data.get("consumed") or {}
    lines: list[str] = []
    for consumed_key, ceiling_key in _EXTRA_RUN_DIMENSIONS:
        if consumed_key not in consumed and ceiling_key not in limits:
            continue
        raw_consumed = consumed.get(consumed_key)
        raw_ceiling = limits.get(ceiling_key)
        lines.append(
            f"  {consumed_key}: {'carried ' if carried else ''}consumed "
            f"{'unavailable' if raw_consumed is None else raw_consumed}"
            f" / limit {'unlimited' if raw_ceiling is None else raw_ceiling}"
            f" (remaining {_dimension_remaining(raw_consumed, raw_ceiling)})\n"
        )
    return lines


def _deadline_dimension_lines(data: dict) -> list[str]:
    """deadline 维的摘要行（`#315`）：**只**在快照带了这一维时渲染。

    这一维进不了 `_EXTRA_RUN_DIMENSIONS` 那张 `(consumed键, ceiling键)` 表——判的是
    时刻先后，没有 consumed 与 ceiling 可比——但它有**一个**事实要说，而且是暂停摘要里
    唯一能说明"到的是哪个点"的读数。Web 的 `PausedPanel` 打同一份**值**（同格的**词**
    两端不同：这里 `None` 报 `unlimited`，面板报 `unavailable`，登记在 ADR-0045 §6.1）。
    恢复块也打这一行：deadline 暂停的恢复点上它是**新**时刻（恢复必须点名一个未来时刻），
    其余恢复（如 turns 维）给的是**沿用**值——两种情形都读 `run/resumed` 的同一份快照。

    判据与 `_extra_dimension_lines` 同源：**键在不在**。键缺席（`#315` 之前的老事件）
    ⇒ 零行（不拿一片 unavailable 当信息）；`None` ⇒ `unlimited`（这一维没配，与同块
    `limit unlimited` 同一个词）；形状认不出（数 / 空串 / 带首尾空白）⇒ `unavailable`
    ——`agent/run_budget._deadline_or_none` 对同一份事件读作"没配"（它不 strip），
    这里跟着它读：不印那个形状，也不编一个时刻。
    """
    limits = ((data.get("limits") or {}).get("run") or {})
    if "deadline_at" not in limits:
        return []
    value = limits.get("deadline_at")
    if value is None:
        return ["  deadline: unlimited\n"]
    if isinstance(value, str) and value and value.strip() == value:
        return [f"  deadline: {value}\n"]
    return ["  deadline: unavailable\n"]


def render_pause_block(data: dict) -> str:
    """`run/paused` 的 data → 多行暂停摘要（`#312` / PRD §11）。

    只读**事件 data**（durable 投影），不读进程内状态——重启 / 刷新 / replay 之后
    同一事件给出同一段文本（"CLI obtains state from SessionEvent/projection,
    not process-local memory"）。Web 与 CLI 显示的是同一份事实，不是两份近似。

    为什么摘要与恢复指令分开（`resume_hint`）：摘要是事件的纯函数（事件里没有
    会话上下文），而恢复指令必须指名 session_id——`run` 与 `replay` 都能给出它，
    渲染层给不出。
    """
    facts = _pause_facts(data)
    lines = [
        (
            f"\n[run paused] reason={data.get('reason', '')}"
            f" dimension={data.get('trigger_dimension', '')}"
            f" version={data.get('budget_version', '')}\n"
        ),
        (
            f"  turns: consumed {facts['consumed_text']} / limit {facts['ceiling_text']}"
            f" (remaining {facts['remaining_text']})"
            f" · local fuse {facts['local']} · closeout={data.get('closeout_source', '')}\n"
        ),
    ]
    lines.extend(_extra_dimension_lines(data))
    lines.extend(_deadline_dimension_lines(data))
    lines.extend(_tool_dimension_lines(data))
    continuation = data.get("continuation")
    if isinstance(continuation, dict) and continuation:
        lines.append("  continuation:\n")
        lines.extend(line + "\n" for line in _continuation_lines(continuation))
    requirements = data.get("resume_requirements")
    if requirements:
        lines.append(
            f"  resume requirements: {', '.join(str(r) for r in requirements)}\n"
        )
    return "".join(lines)


#: 触发维度 → 抬高它的 CLI 开关（`#313`：四维各自可抬；`#318`：session 四维同表——
#: 提示必须指向**真存在**的开关）。
_RESUME_FLAGS: dict[str, str] = {
    "run.max_agent_turns_total": "--run-turns-total",
    "run.max_model_requests": "--run-model-requests",
    "run.max_total_tokens": "--run-total-tokens",
    "run.max_cost_usd": "--run-cost-usd",
    TRIGGER_RUN_DEADLINE: "--run-deadline",
    "session.max_agent_turns_total": "--session-turns-total",
    "session.max_model_requests": "--session-model-requests",
    "session.max_total_tokens": "--session-total-tokens",
    "session.max_cost_usd": "--session-cost-usd",
    TRIGGER_SESSION_DEADLINE: "--session-deadline",
}

#: deadline 维的开关值**不是数字**：提示里给 `N` 会得到一句抄了跑不起来的假指令
#: （`--run-deadline N` 会被形状校验按"不是 RFC 3339 时刻"拒掉）。示例值与前端
#: `runBudget.ts::DEADLINE_EXAMPLE` 逐字一致——同一份提示在两个入口说同一件事。
_DEADLINE_PLACEHOLDER = "2026-09-26T04:30:00Z"


def parse_run_tool_limits(
    raw: list[str] | None, *, flag: str = "--run-tool-limit",
) -> dict[str, int]:
    """`--run-tool-limit NAME=N`（可重复）→ `budget.run.tool_call_limits` 的形状输入。

    `flag`（`#318`）：session 作用域的 `--session-tool-limit` 走同一切分/整数化，
    报错文案跟着开关名走——CLI 不写第二份形状规则（`run` / `resume` / Web 三个
    入口共用同一份 422 口径）。

    只做 **argv 层**的切分与整数化（`"N"` → int）；"工具名是否合法、值是否为正整数"
    仍由领域层 `parse_tool_call_limits` 判。

    缺 `=`（连名字和值都分不出来）与重复点名同一个工具都在这里拒绝：两者都是
    **命令行用法**问题，而"重复"若按 argv 惯例静默取最后一个，用户会以为自己设的是
    另一个数字（配额上不允许这种静默覆盖）。
    """
    if not raw:
        return {}
    parsed: dict[str, int] = {}
    for item in raw:
        name, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"{flag} 需要 NAME=N 形式，收到 {item!r}")
        if name in parsed:
            raise ValueError(f"{flag} 重复点名了工具 {name!r}（不静默取最后一个）")
        try:
            parsed[name] = int(value)
        except ValueError:
            raise ValueError(
                f"{flag} {item!r} 的值必须是整数（收到 {value!r}）"
            ) from None
    return parsed


def _resume_command_tail(dimension: str) -> str:
    """触发维度 → 恢复命令行里"抬高它"的那半截（值的位置留 `N`）。

    per-tool 维度的开关形状与四维**不同**：它是 `--run-tool-limit <name>=N`（名字与值
    在同一个 argv 里），四维是 `--flag N`（开关与值分列）。所以这里连值一起给——
    只给开关名再拼一个空格 + `N` 会得到 `--run-tool-limit bash N`，那是**跑不起来**的
    假指令（提示的价值全在"照抄能跑"）。
    """
    tool_name = tool_name_of_dimension(dimension)
    if tool_name is not None:
        return f"--run-tool-limit {tool_name}=N"
    if dimension.startswith("session.tool_call_limits."):
        # session 侧 per-tool 维（`#318`）：值在同一个 argv 里，与 run 侧同因
        session_tool = dimension.removeprefix("session.tool_call_limits.")
        return f"--session-tool-limit {session_tool}=N"
    if dimension in (TRIGGER_RUN_DEADLINE, TRIGGER_SESSION_DEADLINE):
        # 值在"同一个 argv 里"这一点与 per-tool 维同因：`--run-deadline N` 的 `N`
        # 会被读成一个整数（或直接被拒），照抄的人拿到的是一条跑不通的命令。
        return f"{_RESUME_FLAGS[dimension]} {_DEADLINE_PLACEHOLDER}"
    return f"{_RESUME_FLAGS.get(dimension, '--run-turns-total')} N"


def _resume_ceiling_rule(dimension: str) -> str:
    """尾句里"N 该比什么大"的判据：**per-tool 维与四维不是同一条**。

    四维（turns / requests）的 ceiling 判定含一次 closeout 预留（`02 §5.2`：保证暂停时
    还收得了口），所以"N 必须高于 consumed + 预留 closeout 轮"成立。per-tool 维**不**预留
    ——closeout 是一次模型请求、不产生任何工具调用，给它留一格会让"配额 = 3"实际允许 4 次
    调用（`agent/run_budget._dimension_reached` 的第三类临界点，ADR-0045 D4）⇒ 那条尾句在
    工具维上是一句与实现相反的话（前端同级提示给的是"至少 calls+1"）。
    """
    if tool_name_of_dimension(dimension) is not None:
        return (
            "N 是**绝对** ceiling，必须严格大于已接纳的调用数（该维不预留 closeout）；"
            "不是增量"
        )
    if dimension == TRIGGER_RUN_DEADLINE:
        # deadline 没有"抬高"这回事：要的是**另一个未来时刻**。写"必须高于 consumed"
        # 会让 operator 去比一个与停止原因无关的数（`02 §5.1`：三层控制互不替代）。
        return (
            "给的是**新的未来时刻**（RFC 3339 UTC 绝对时刻，不是数字）；"
            "沿用一个已到点的时刻会被拒（409）"
        )
    return "N 是**绝对** ceiling，必须高于 consumed + 预留 closeout 轮；不是增量"


def resume_hint(session_id: str, *, data: dict) -> str:
    """暂停之后"接下来怎么做"的一行指令（`#312` 建 / `#313` 按维度点名开关）。

    ceiling 用占位符 `N`：抬高多少是用户/运维的决定，CLI **不替它猜**一个数字
    （猜出来的"建议值"会被当成策略，且绝对 ceiling 与增量是两种语义）。

    开关与**判据**都按 `trigger_dimension` 取（不是写死 turns）：暂停可能落在
    requests / token / cost / **per-tool 配额**（`#314`）维度上，提示里给一个抬不动它的
    开关、或给一条与该维相反的判据，都是**假指令**（PRD §11：CLI 显示的就是 durable
    事实本身）。未知维度（本票之外的暂停原因）回落到 turns 开关——那是 `#308` 起一直
    存在的维度，也是唯一一个任何 run 都读得懂的。

    **stuck 暂停走另一条**：它的 `trigger_dimension` 是模式名，按维度回落就会给出
    `--run-turns-total N`——一条恒被 409 挡死的假指令（stuck 不接受 `budget_increase`，
    ADR-0048 D7）。所以那一类按事件自己列的可用依据给 `--basis`。
    """
    if data.get("reason") == REASON_STUCK:
        return _stuck_resume_hint(session_id, data)
    dimension = str(data.get("trigger_dimension", ""))
    if dimension.startswith("session."):
        # session 维触发的暂停（`#318`）：抬高发生在 **durable 的 session 账行**上，
        # CAS 用的是行版本（`data.session.version`），不是 run 的 budget_version——
        # 给错版本的提示就是一条恒 409 的假指令。
        session = data.get("session") or {}
        return (
            f"  resume: agent-harness resume {session_id}"
            f" {_resume_command_tail(dimension)}"
            f" --session-expected-version {session.get('version', '')}"
            f" --expected-version {data.get('budget_version', '')}"
            f"  ({_resume_ceiling_rule(dimension)})\n"
        )
    return (
        f"  resume: agent-harness resume {session_id}"
        f" {_resume_command_tail(dimension)}"
        f" --expected-version {data.get('budget_version', '')}"
        f"  ({_resume_ceiling_rule(dimension)})\n"
    )


def _stuck_resume_hint(session_id: str, data: dict) -> str:
    """stuck 暂停的恢复指令：依据是**变更**，不是 ceiling（`02 §5.3` / ADR-0048 D7）。

    可用依据逐条读事件自己的 `resume_requirements`，不在这里重算：CLI 再算一份就等于
    给"CLI 以为的可用集"与判据之间留一个漂移点（`#317` 三轮审查钉的正是这个形状）。
    依据可以是多条 ⇒ 命令行里放一条**今天真能走通**的，判据行里把全部可用的都点名。

    `relevant_steer` 要**跳过**：它是唯一不看快照的依据（所以常在），但暂停之后登记 steer
    的入口今天不存在（`steer/requested` 只在有在途 run 时可投，残余 6 / 9）——把它印成
    命令行就是一条照抄必被 409 挡死的假指令（`#317` 四轮审查 P3）。
    """
    available = [str(item) for item in (data.get("resume_requirements") or ())]
    version = data.get("budget_version", "")
    if not available:
        # 本代码产出的 stuck 暂停至少列 `relevant_steer`（`stuck_resume_requirements` 对非空
        # 快照无条件带上它）⇒ 走到这里的是外来 / 手改过的载荷（那一格缺失或为空）：宁可说
        # "没有列出任何依据"，也不打印一条必然 409 的 `--basis`。
        return (
            f"  resume: 这次暂停的载荷里没有列出任何可用依据"
            f"（`resume_requirements` 缺失或为空）· expected-version {version}\n"
        )
    actionable = [basis for basis in available if basis != RESUME_BASIS_RELEVANT_STEER]
    if not actionable:
        # 只剩 `relevant_steer`（子 run 的形状，或快照两格缺席）：它不是在判据上被恒拒，
        # 是入口层取不到 ⇒ 如实说"现在没有可执行的依据"，而不是给一条走不通的命令。
        return (
            f"  resume: 现在没有可执行的恢复依据（本次列出的只有 "
            f"{', '.join(available)}）——那条需要暂停之后登记一条 steer，"
            f"而那个入口今天不存在（残余 6 / 9；子会话见 #372）· "
            f"expected-version {version}\n"
        )
    return (
        f"  resume: agent-harness resume {session_id}"
        f" --basis {actionable[0]}"
        f" --expected-version {version}"
        f"  (stuck 暂停不以 ceiling 为依据，可以不给 --run-* 开关；可用的依据只有："
        f"{', '.join(available)}——每条都必须是**被观测到的**变更，声明不算)\n"
    )


def render_resume_block(data: dict) -> str:
    """`run/resumed` 的 data → 一行摘要（同 run 续跑：run_id 不变、版本 +1）。"""
    facts = _pause_facts(data)
    lines = [
        (
            f"\n[run resumed] basis={data.get('resume_basis', '')}"
            f" version={data.get('previous_budget_version', '')}"
            f"→{data.get('budget_version', '')}"
            f" from_pause_seq={data.get('from_pause_seq', '')}\n"
        ),
        (
            f"  turns: carried consumed {facts['consumed_text']}"
            f" / limit {facts['ceiling_text']} (remaining {facts['remaining_text']})\n"
        ),
    ]
    lines.extend(_extra_dimension_lines(data, carried=True))
    lines.extend(_deadline_dimension_lines(data))
    lines.extend(_tool_dimension_lines(data, carried=True))
    return "".join(lines)


@dataclass(frozen=True)
class RunOutcome:
    """CLI 一次 `run` 的结果（`#312`）。

    `paused` 必须与失败**可区分**：两者都拿不到 `final_text`（暂停不是终结，
    没有最终回答；失败有 `run/failed` 但同样没有回答），把它们都压成"空字符串"
    会让退出码把一次预算暂停报成失败（票面 Must Do：`run/paused` 必须与
    completed / failed / interrupted / NEED_RECONCILE 区分显示）。
    """

    final_text: str
    paused: bool = False


async def run(
    message: str,
    *,
    run_turns_total: int | None = None,
    run_model_requests: int | None = None,
    run_total_tokens: int | None = None,
    run_cost_usd: str | None = None,
    run_deadline_at: str | None = None,
    run_tool_limits: dict[str, int] | None = None,
    session_turns_total: int | None = None,
    session_model_requests: int | None = None,
    session_total_tokens: int | None = None,
    session_cost_usd: str | None = None,
    session_deadline_at: str | None = None,
    session_tool_limits: dict[str, int] | None = None,
    session_max_delegations: int | None = None,
    write: Callable[[str], None] | None = None,
) -> RunOutcome:
    """跑一次 Agent Loop：流式渲染到 write，返回 `RunOutcome`。

    与 web 共享 assembly.build_runtime 全栈装配（coding 工具 + Ledger/
    Checkpoint + capability 工具）——CLI 不再是削弱装配，耐久性语义一致。
    失败的 run 不抛异常（runtime 契约：失败事实由 run/failed 终结事件 +
    结构化日志承载）——返回空 final_text，main() 据此转 SystemExit(1)。
    成功的 run final_text 恒非空：空响应在 runtime 被拒为失败（R6-2），
    不存在"成功但空回答"的歧义态。

    `#312`：预算暂停既不是成功也不是失败（逻辑 run 未终结）——`paused=True` 由
    **durable** 的 `run/paused` 事件推出，且额外补一行"接下来怎么做"（含 session_id：
    恢复必须指名这个会话，而 CLI 的一次性命令此前从不打印它）。

    `run_turns_total` = `budget.run.max_agent_turns_total` 的**绝对** ceiling
    （`None` = 不设 run 作用域 ceiling，`02 §5.1`：除显式配置外无 ceiling）。
    CLI 是"创建 + 第一条消息"的一个人驱动入口，与 Web 的创建端点同一档能力——
    没有它，CLI 连一次真实暂停都产生不了，也就无从演示"暂停 → 提高 ceiling →
    同一个 run 完成"。

    `#313`：另外三个 run 维度（`run_model_requests` / `run_total_tokens` /
    `run_cost_usd`）同一条纪律、同一份 422 规则（`run_limits_from_request`）。
    cost 是十进制**字符串**（`11 §6.1`：二进制浮点相等不是契约），由领域层解析。

    `#314`：`run_tool_limits` 是 per-tool 绝对配额（工具名 → 正整数），域名/形状与
    Web 的 `budget.run.tool_call_limits` 同源；"名字已注册"由装配层在首个 Provider
    请求之前判（同一条 422 通道）。

    `#315`：`run_deadline_at` 是本 run 的**绝对截止时刻**（RFC 3339 UTC 文本，与 Web
    的 `budget.run.deadline_at` 同源、同一份形状判定）。给了已过去的时刻是合法的——
    等于立刻到点（即时 `run/paused reason=deadline`）。
    """
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    # LogContext 提供 trace_id/task_id 关联列——没有它 runtime 的结构化日志
    # 整条链都缺关联键（一次 CLI 运行 = 一个可对账的 trace）。
    with log_context(LogContext.create(service="agent-harness", env="local")):
        workspace_root = Path(settings.workspace_dir)
        # store 先建（#298 T7b）：`assemble_wiring` 要把它交给 V2 记忆形成——执行 job 时
        # 按 (session_id, run_id) 从**这一份**日志切本轮事件，所以必须注入而不是让装配层
        # 按目录约定另建一份（两份分叉的症状是"job 永远切片为空"，没有任何东西报路径不一致）。
        store = JsonlSessionStore(root=workspace_root / "sessions")
        _, wiring = await assemble_wiring(settings, sessions=store)
        # WS-2 / ADR-0025：项目索引必须拿到会话 header 来源，故先建 store 再建 stores
        # （首次 bootstrap 就在 initialize_stores 里发生，AC14–16）。
        stores = recovery_stores(workspace_root / "harness.db", workspace_headers=store)
        await initialize_stores(stores)
        workspace_registry = WorkspaceRegistry(root=workspace_root, backend="local")
        # 崩溃扫描**不**在 CLI 里跑：在途 run 只存在于持有它的进程内存中，
        # 短命命令无法区分「别的进程在跑」与「崩溃遗留」，误标会撞 seq
        # （见 recovery/scan.py 单进程假设）。扫描归属长驻会话宿主（web lifespan）。
        session_id = str(uuid4())
        workspace = workspace_root / "workspaces" / session_id
        # local fuse（#308）：CLI 不再自带一个低位数字——生效值 = Deployment 默认 500，
        # 会话级覆盖走 `budget.local.max_agent_turns`（CLI 暂无该开关，与 Web 同一条解析）。
        # 档位声明同样参与收窄（CLI 走默认 main 档位，与 `build_runtime` 的档位选择同源）。
        # 生效值与其**来源**都要传给装配层：`run/paused` 的 limits 快照里 local 一档
        # 带 source（`11 §6.1` 的可执行性口径），少了它 CLI 的暂停快照与 Web 的不是同一份事实。
        fuse = resolve_local_fuse(
            deployment=settings.local_max_agent_turns,
            profile=declared_turn_ceiling(None),
        )
        # `#318`：session 作用域账（--session-* 旗标 → durable 账行的首用声明；
        # 恒接线，账从本会话第一个 run 起就有持久读数，与 Web 创建路径同一条规则）。
        # `#564` 审查 P2-2：同一份声明同时喂 pre-CAS 注册名校验——CLI create 的
        # 坏名同样"任何工作开始前拒绝"（04 §9.1），不能静默进 durable 账行。
        session_limits = session_limits_from_request(
            max_agent_turns_total=session_turns_total,
            max_model_requests=session_model_requests,
            max_total_tokens=session_total_tokens,
            max_cost_usd=session_cost_usd,
            deadline_at=session_deadline_at,
            tool_call_limits=session_tool_limits,
            max_delegations=session_max_delegations,
            accounting=HARNESS_MODEL_ACCOUNTING,
        )
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=workspace_registry,
            session_id=session_id, workspace=workspace,
            max_agent_turns=fuse.max_agent_turns,
            local_fuse_source=fuse.source,
            # 档位显式化：**与下面证据端口的摘要输入同源**。运行时实际生效的档位就是
            # 这一档（此前靠 `build_runtime` 的默认参数），若两处各写一次，暂停快照
            # 会按"另一套策略输入"算，而恢复侧重算时对不上——那是一次假的
            # `policy_change`（`#317` / ADR-0048 D8）。
            permission_mode=PermissionPolicy.WORKSPACE_WRITE,
            # `#312`：run 作用域的绝对 ceiling（`--run-turns-total`）。None = 本 run
            # 不设 run 档 ceiling——**不是** 0（0 会把第一条 model 决策就挡下）。
            run_budget=LaunchRunBudget(
                limits=run_limits_from_request(
                    max_agent_turns_total=run_turns_total,
                    max_model_requests=run_model_requests,
                    max_total_tokens=run_total_tokens,
                    max_cost_usd=run_cost_usd,
                    deadline_at=run_deadline_at,
                    tool_call_limits=run_tool_limits,
                    accounting=HARNESS_MODEL_ACCOUNTING,
                ),
            ),
            session_budget=SessionBudgetHandle(
                stores.delegation_tree_ledger,
                budget_key=session_id,
                root_session_id=session_id,
                limits=session_limits,
            ),
            # 同一份声明（上面 hoist）：pre-CAS 校验与 durable 账行的首用声明
            # 是**一个**输入，不是两份各写一遍的配置。
            session_declared_limits=session_limits,
            auto_approve=True,
            session_store=store,
            # `#317`：`run` 是"创建 + 第一条消息"入口，第一次开跑就可能卡循环 ⇒ 它建的
            # 会话也必须带证据端口，否则 CLI 上跑出的 stuck 暂停在载荷里没有环境 /
            # 策略快照，恢复时那两条依据一律 409；剩下的一条 `relevant_steer` 在 CLI 上
            # 没有入口（没有 steer 子命令），所以少了这个端口 = **三条都不可恢复**。
            # 走**唯一**构造点（`resume_evidence.evidence_port`），与 Web 创建 / 续聊、
            # CLI 恢复同一份算法。
            stuck_evidence=evidence_port(
                workspace=workspace, permission_mode=PermissionPolicy.WORKSPACE_WRITE,
            ),
        )
        session = Session.start(store, session_id=session_id, cwd=workspace)
        # 与 web event_generator 同一契约：SESSION-scope 记忆 / 会话级工具
        # （ingest_document 的 sandbox 解析）需要可信 session id。
        session_token = memory_session_var.set(session.session_id)
        emit = write if write is not None else sys.stdout.write
        renderer = StreamRenderer(emit)
        final_text = ""
        pause_data: dict | None = None
        try:
            async for event in runtime.run_stream(session, message):
                renderer.handle(event)
                if event.type == RUN_COMPLETED:
                    final_text = event.data.get("final_text", "")
                elif event.type == RUN_PAUSED:
                    pause_data = event.data
        finally:
            memory_session_var.reset(session_token)
        if pause_data is not None:
            # 恢复指令要给出**本会话 id**——暂停摘要是事件的纯函数（不含会话上下文），
            # 而 CLI 的一次性命令此前从不打印 session_id。提示读的是 durable 事件的
            # data（不是进程内状态），与 replay / Web 显示同一份事实。
            emit(resume_hint(session.session_id, data=pause_data))
        return RunOutcome(final_text=final_text, paused=pause_data is not None)


def main() -> None:
    # W-11（#355）：`serve` 是唯一的冷启动闭环入口——自己竞争 InstanceLock、失败时
    # 二次检查附着既有服务，因此必须绕开下面的外层锁（否则 serve 永远死在
    # "第二个写者"的报错上）。与 --help 同级的早分发。
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        try:
            _main_serve(sys.argv[2:])
        finally:
            flush_process_sink()
        return
    # ARCH-7（#150）：CLI 与 Web 并发使用同一 session root 被**有意拒绝**——
    # 无保护的跨进程多写者会产出重复 seq / 交错写，且 run 归属共识只在进程内
    # 有效。这里不吞异常：响亮失败 + 明确错误信息（锁路径 / 占用者 / 逃生门）。
    # `--help` / `-h` 不触碰该根（argparse 直接打印帮助退出），不该被锁挡住；
    # 但它仍是"退出路径"，flush 契约（ADR-0018 D3）照旧要守。
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        try:
            _main_dispatch()
        finally:
            flush_process_sink()
        return
    settings = Settings()
    # 先配日志再取锁：逃生门降级时那条 WARNING 才落得进 agent.jsonl（AC7）。
    # setup_logging 幂等（子命令内重复调用只清一次 handlers）。
    setup_logging(settings.log_level, settings.workspace_dir)
    try:
        lock = InstanceLock(settings.workspace_dir).acquire()
    except InstanceLockError as error:
        # W-11（#355）：锁报错先做附着感知——活服务在运行时把它的地址指给用户，
        # 而不是留下「锁被占用」的旧话术诱发启动者另开第二个实例。
        from agent_harness.host_service import describe_lock_error_with_attach

        print(describe_lock_error_with_attach(settings.workspace_dir, error), file=sys.stderr)
        raise SystemExit(2) from error
    try:
        _main_dispatch()
    finally:
        lock.release()
        # 旁路收尾（ADR-0018 D3）：任何退出路径（正常/异常/SystemExit）都尽力
        # 发送剩余 Langfuse span；未配置/未装配时零开销 no-op。
        flush_process_sink()


def _main_serve(argv: list[str]) -> None:
    """W-11（#355）：单机服务冷启动闭环（附着 → 否则竞争启动 → 二次检查）。

    本机个人服务固定 loopback + 随机受管端口（`--host` 被限定为 127.0.0.1 /
    localhost，个人桌面服务不暴露 LAN）；远程/LAN 部署保持既有显式配置入口
    （`create_prod_app` + 显式 jwt_secret），不经本命令。
    """
    from agent_harness.host_service import HostServiceError, serve_once

    parser = argparse.ArgumentParser(
        prog="agent-harness serve",
        description="启动本机唯一 Agent Harness 服务（已运行则附着既有服务后退出）",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        choices=["127.0.0.1", "localhost"],
        help="绑定地址；只允许 loopback（W-11：个人服务不暴露 LAN，远程部署走显式配置入口）",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    try:
        outcome = serve_once(settings, host=args.host)
    except (HostServiceError, InstanceLockError) as error:
        # HostServiceError = 活服务但凭据不可用（exit 3）；InstanceLockError =
        # 二次检查窗口内无服务可附着、锁在非服务写者手里（exit 2，原报错原样出）。
        print(error, file=sys.stderr)
        raise SystemExit(3 if isinstance(error, HostServiceError) else 2) from error
    except KeyboardInterrupt:
        print("服务已停止")
        return
    if not outcome.served and outcome.endpoint is not None:
        print(
            f"已有本机服务在运行：http://127.0.0.1:{outcome.endpoint.port}"
            "（已附着，本进程退出）"
        )


def _main_dispatch() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "ingest":
        _main_ingest(argv[1:])
        return
    if argv and argv[0] == "fork":
        _main_fork(argv[1:])
        return
    if argv and argv[0] == "sessions":
        _main_sessions(argv[1:])
        return
    if argv and argv[0] == "budgets":
        _main_budgets(argv[1:])
        return
    if argv and argv[0] == "compact":
        _main_compact(argv[1:])
        return
    if argv and argv[0] == "approvals":
        _main_approvals(argv[1:])
        return
    if argv and argv[0] == "replay":
        _main_replay(argv[1:])
        return
    if argv and argv[0] == "resume":
        _main_resume(argv[1:])
        return
    parser = argparse.ArgumentParser(description="Agent Harness CLI")
    parser.add_argument("message", help="发送给 Agent 的任务")
    parser.add_argument(
        "--run-turns-total", type=int, default=None,
        help="本次 run 的**绝对** turn ceiling（budget.run.max_agent_turns_total；"
             "缺省=不设 run 档 ceiling）。低值会让 run 在预算处 `run/paused`，"
             "用 `agent-harness resume` 抬高后接上同一个 run。",
    )
    parser.add_argument(
        "--run-model-requests", type=int, default=None,
        help="本次 run 的**绝对** Provider 请求 ceiling"
             "（budget.run.max_model_requests；primary / fallback / closeout 各计一次）",
    )
    parser.add_argument(
        "--run-total-tokens", type=int, default=None,
        help="本次 run 的**绝对** token ceiling（budget.run.max_total_tokens；"
             "只统计 Provider 自报的 usage——不自报的链上显式配它会被 422 拒绝）",
    )
    parser.add_argument(
        "--run-cost-usd", default=None,
        help="本次 run 的**绝对** 成本 ceiling（budget.run.max_cost_usd，十进制；"
             "只统计 Provider 自报的归属成本——不臆造费率表，报告不了的链上恒 422）",
    )
    parser.add_argument(
        "--run-deadline", default=None, metavar="RFC3339",
        help="本次 run 的**绝对**截止时刻（budget.run.deadline_at，RFC 3339 UTC，"
             "如 2026-09-26T04:30:00Z）。到点后不再接纳新的 Provider 请求 / 工具调用 / "
             "子 Agent，run 按 reason=deadline 暂停；恢复要**换一个新的未来时刻**"
             "（`agent-harness resume --run-deadline …`）。给了已过去的时刻等于立刻到点。",
    )
    parser.add_argument(
        "--run-tool-limit", action="append", default=None, metavar="NAME=N",
        help="本次 run 对某个已注册工具的**绝对**调用次数上限"
             "（budget.run.tool_call_limits；可重复，如 --run-tool-limit bash=3）。"
             "用尽后该工具的新调用在接纳前被拒并让 run 暂停，用 `agent-harness resume` "
             "抬高后接上同一个 run。",
    )
    parser.add_argument(
        "--session-turns-total", type=int, default=None,
        help="session 作用域的**绝对** turn ceiling"
             "（budget.session.max_agent_turns_total，`#318`：跨 run 持久的 durable 账；"
             "缺省=不设）。与 run 档叠加判定：任一作用域到线都让 run 暂停",
    )
    parser.add_argument(
        "--session-model-requests", type=int, default=None,
        help="session 作用域的**绝对** Provider 请求 ceiling"
             "（budget.session.max_model_requests；跨 run 累计）",
    )
    parser.add_argument(
        "--session-total-tokens", type=int, default=None,
        help="session 作用域的**绝对** token ceiling"
             "（budget.session.max_total_tokens；跨 run 累计）",
    )
    parser.add_argument(
        "--session-cost-usd", default=None,
        help="session 作用域的**绝对**成本 ceiling"
             "（budget.session.max_cost_usd，十进制；跨 run 累计）",
    )
    parser.add_argument(
        "--session-deadline", default=None, metavar="RFC3339",
        help="session 作用域的**绝对**截止时刻"
             "（budget.session.deadline_at，RFC 3339 UTC；跨 run 生效）",
    )
    parser.add_argument(
        "--session-tool-limit", action="append", default=None, metavar="NAME=N",
        help="session 作用域对某个已注册工具的**绝对**调用次数上限"
             "（budget.session.tool_call_limits；可重复，如 --session-tool-limit bash=50）",
    )
    parser.add_argument(
        "--session-max-delegations", type=int, default=None,
        help="session 作用域的**整棵委派树**跨 run 累计上限"
             "（budget.session.max_delegations；缺省沿用域默认 8，`10 §5.1`）",
    )
    args = parser.parse_args(argv)
    try:
        tool_limits = parse_run_tool_limits(args.run_tool_limit)
        session_tool_limits = parse_run_tool_limits(
            args.session_tool_limit, flag="--session-tool-limit",
        )
    except ValueError as error:
        parser.error(str(error))
    outcome = asyncio.run(
        run(
            args.message,
            run_turns_total=args.run_turns_total,
            run_model_requests=args.run_model_requests,
            run_total_tokens=args.run_total_tokens,
            run_cost_usd=args.run_cost_usd,
            run_deadline_at=args.run_deadline,
            run_tool_limits=tool_limits,
            session_turns_total=args.session_turns_total,
            session_model_requests=args.session_model_requests,
            session_total_tokens=args.session_total_tokens,
            session_cost_usd=args.session_cost_usd,
            session_deadline_at=args.session_deadline,
            session_tool_limits=session_tool_limits,
            session_max_delegations=args.session_max_delegations,
        )
    )
    if outcome.paused:
        # `#312`：预算暂停**不是**失败——退出码 0，且暂停块 + 恢复指令已经打印。
        # 把它也压成 exit 1 会让脚本把"被预算挡住、可恢复"读成"跑挂了"。
        return
    if not outcome.final_text:
        raise SystemExit(1)


def _main_ingest(argv: list[str]) -> None:
    """CLI 建库入口（ADR-0013 决策 6）：与 ingest_document 工具同一服务函数。

    人驱动的宿主路径直读（不走 sandbox 边界——CLI 是人不是模型）；向量库
    配置与 capability 共用同一组 env（MILVUS_* / KNOWLEDGE_COLLECTION /
    EMBEDDING_*），未配置时响亮失败。
    """
    parser = argparse.ArgumentParser(prog="agent-harness ingest")
    parser.add_argument("path", help="要摄入的 UTF-8 文本文件路径（宿主本地）")
    parser.add_argument("--name", default=None, help="语料来源名（缺省取文件名）")
    args = parser.parse_args(argv)

    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    from agent_harness.knowledge.milvus_store import MilvusKnowledgeVectorStore
    from agent_harness.knowledge.registry import SqliteKnowledgeSourceRegistry
    from agent_harness.knowledge.service import KnowledgeService
    from agent_harness.memory.embeddings import create_embeddings

    async def _entry() -> None:
        store = MilvusKnowledgeVectorStore(
            settings,
            create_embeddings(settings) if settings.embedding_model else None,
        )
        await store.initialize()
        registry = SqliteKnowledgeSourceRegistry(
            Path(settings.workspace_dir) / "harness.db"
        )
        await registry.initialize()
        service = KnowledgeService(store=store, registry=registry)
        content = Path(args.path).read_text(encoding="utf-8")
        result = await service.ingest(
            text=content, source_name=args.name or Path(args.path).name,
            identity=IdentityContext("local", "local", ["user", "session"]),
        )
        print(f"语料 '{result.source_name}' {result.status}："
              f"{result.chunk_count} 个 chunk 已入索引。")

    asyncio.run(_entry())


def _main_fork(argv: list[str]) -> None:
    """CLI fork 入口（Phase 14 T5, ADR-0017 决策 6/10）：人驱动的分叉动作。

    fork 是用户动作而非模型工具——不注册进任何 ToolRegistry。全链 =
    boundary 校验 + seed + provenance + meta（fork 核心）→ copy-on-fork
    → tail summary（默认开：主模型一次调用；--no-summary 关闭）。
    """
    args = _parse_fork_args(argv)
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    try:
        child_id = asyncio.run(
            fork_command(
                args.session_id, from_message=args.from_message,
                no_summary=args.no_summary,
            )
        )
    except ForkBoundaryError as error:
        print(f"fork 失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(f"已分叉：child session = {child_id}")
    print(f"（原会话 {args.session_id} 未改动；在新分支重发第 "
          f"{args.from_message} 条用户消息即可继续）")


def _parse_fork_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agent-harness fork")
    parser.add_argument("session_id", help="要分叉的父会话 id")
    parser.add_argument(
        "--from-message", type=int, required=True, metavar="N",
        help="从父会话的第 N 条用户消息处分叉（只数可作分叉锚点的用户消息；"
             "该消息不进 seed，由你在新分支重发）",
    )
    parser.add_argument(
        "--no-summary", action="store_true",
        help="跳过 tail summary（默认对被放弃路线生成一次 LLM 摘要挂进新会话）",
    )
    return parser.parse_args(argv)


def _resolve_fork_ordinal(
    store: JsonlSessionStore, session_id: str, from_message: int,
) -> int:
    """把 `--from-message` 的**序数**解析成 fork 锚点的**事件 seq**（#424）。

    help 承诺"第 N 条用户消息"，而 ``fork_session`` 的契约是事件 seq——历史
    行为把 N 当 seq 直传，纯消息会话里二者恰好重合（started 占 seq 0），
    真实会话（消息间夹着 run/model/tool 事件）立刻分叉。解析范围是
    :func:`find_fork_boundaries` 的合法锚点：run 完整处的用户消息——
    "第 N 条用户消息"若落在 run 未收口处，fork 进 run 不完整的前缀本来
    就不合法，只能按可分叉的锚点数序数。越界报错同时给序数范围与 seq
    清单（用户按序数提问，按 seq 对账）。
    """
    # `#445`：read_events 对不存在的会话返回 []（不抛 SessionNotFound），
    # 解析器又先于 fork_session 运行——不存在/空日志必须在此分开报，
    # 否则被误报成下面的序数越界。
    events = store.read_events(session_id)
    if not events:
        raise ForkBoundaryError(f"Session '{session_id}' 不存在或事件日志为空")
    boundaries = find_fork_boundaries(events)
    if not 1 <= from_message <= len(boundaries):
        raise ForkBoundaryError(
            f"--from-message {from_message} 超出范围：父会话 '{session_id}' "
            f"只有 {len(boundaries)} 个可作分叉锚点的用户消息"
            f"（事件 seq: {boundaries}）"
        )
    return boundaries[from_message - 1]


async def fork_command(
    session_id: str,
    *,
    from_message: int,
    no_summary: bool,
    workspace_dir: str | None = None,
    write: Callable[[str], None] | None = None,
) -> str:
    """fork 命令的可测核心：返回 child session id。

    与 run() 同一装配约定（workspace_dir 缺省取 Settings）；--no-summary
    关闭 tail summary，否则用主模型链跑一次摘要（T4 seam）。
    """
    settings = Settings()
    if workspace_dir is not None:
        settings.workspace_dir = workspace_dir
    setup_logging(settings.log_level, settings.workspace_dir)
    workspace_root = Path(settings.workspace_dir)
    store = JsonlSessionStore(root=workspace_root / "sessions")
    meta_store = SqliteSessionMetaStore(workspace_root / "harness.db")
    await meta_store.initialize()
    # `#318`：session 预算谱系的账行读数源（同一 harness.db；幂等初始化）。
    from agent_harness.storage.delegation_tree import SqliteDelegationTreeLedger

    delegation_ledger = SqliteDelegationTreeLedger(workspace_root / "harness.db")
    await delegation_ledger.initialize()
    workspace_registry = WorkspaceRegistry(root=workspace_root, backend="local")
    summarizer = None
    if not no_summary:
        from agent_harness.model.provider import create_chat_model

        summarizer = TailSummarizer(
            create_chat_model(ModelConfig.from_settings(settings))
        )
    child = await fork_session(
        store, meta_store, session_id,
        boundary_user_message_seq=_resolve_fork_ordinal(
            store, session_id, from_message,
        ),
        workspace_registry=workspace_registry,
        summarizer=summarizer, with_tail_summary=not no_summary,
        # `#318`：fork 谱系读父账行（只读；同一 harness.db 的 durable 账）。
        # 表先于读（initialize 幂等，与 meta_store 同一条纪律）。
        budget_ledger=delegation_ledger,
    )
    # child 继承父当前模型（T7 #137：fork seed 不含父 session/started）。
    from agent_harness.session.service import inherit_parent_model

    inherit_parent_model(child, store.read_events(session_id))
    if write is not None:
        write(f"child session: {child.session_id}\n")
    return child.session_id


# ── replay（Phase 14 T8, ADR-0017 决策 4：逻辑回放，零副作用契约）────────────


def render_replay_event(event: SessionEvent) -> str | None:
    """SessionEvent → 终端行（纯函数）。生命周期噪音返回 None 不渲染。

    tool result 一律渲染**冻结终态**（spec 03 §6：逻辑回放不重执行、不产生
    外部副作用）。失败事实（run/failed、model/failed、熔断、fallback）如实
    呈现，绝不美化。
    """
    data = event.data
    if event.type == USER_MESSAGE:
        return f"\n[用户] {data.get('content', '')}"
    if event.type == MODEL_COMPLETED:
        content = data.get("content", "")
        return f"[assistant] {content}" if content else None
    if event.type == TOOL_CALL:
        args = data.get("args", {})
        return f"[工具] {data.get('tool_name', '')}({_collapse_args(args)})"
    if event.type == TOOL_RESULT:
        content = str(data.get("content", ""))
        lines = content.splitlines() or [""]
        preview = "\n".join(f"  │ {line}" for line in lines[:_PREVIEW_LINES])
        more = "" if len(lines) <= _PREVIEW_LINES else f"\n  │ ... +{len(lines) - _PREVIEW_LINES} more lines"
        return f"  → 结果（冻结）:\n{preview}{more}"
    if event.type == RUN_FAILED:
        return f"[run 失败] {data.get('reason', 'unspecified')}"
    if event.type == RUN_PAUSED:
        # `#312`：暂停是**非终态**收口（逻辑 run 未终结）——replay 必须如实重建它，
        # 否则刷新/回放之后的 CLI 看到的会话就像"跑完了"。
        return (
            render_pause_block(data).lstrip("\n")
            + resume_hint(event.session_id, data=data).rstrip("\n")
        )
    if event.type == RUN_RESUMED:
        return render_resume_block(data).lstrip("\n").rstrip("\n")
    if event.type == MODEL_FAILED:
        return f"[模型失败] {data.get('message', '')}"
    if event.type == TOOL_FAILURE_GUARD:
        return (f"[熔断] level={data.get('level', '')}"
                f" consecutive_failures={data.get('consecutive_failures', '')}")
    if event.type == GUARD_STUCK:
        # `#317`：stuck 护栏的两种动作都如实呈现（`replan` 是"已纠正过一次"的 durable
        # 依据，`paused` 是暂停前的最后一步）——只渲染 run/paused 会让回放看起来
        # "没发生过纠正"，与 `run/paused.stuck.replan_count` 对不上账。
        return (f"[stuck] level={data.get('level', '')} pattern={data.get('pattern', '')}"
                f" count={data.get('count', '')} threshold={data.get('threshold', '')}")
    if event.type == MODEL_FALLBACK:
        return (f"[fallback] {data.get('from_model', '')}→"
                f"{data.get('to_model', '')} ({data.get('reason', '')})")
    if event.type == AGENT_DELEGATION_STARTED:
        return (f"[委派→{data.get('target', '')}] "
                f"child={data.get('child_session_id', '')}")
    if event.type == AGENT_DELEGATION_FINISHED:
        summary = str(data.get("summary", ""))[:200]
        return (f"[委派完成→{data.get('target', '')}] "
                f"{data.get('status', '')}: {summary}")
    if event.type == ARTIFACT_CREATED:
        return f"[artifact] {str(data)[:120]}"
    if event.type == ARTIFACT_EXTERNALIZED:
        return f"[外置产物] artifact_id={data.get('artifact_id', '')} size={data.get('size', 0)}"
    if event.type == SESSION_FORKED:
        return (f"[fork] 来自 {data.get('parent_session_id', '')}"
                f" @{data.get('fork_point_seq')}")
    if event.type == CONTEXT_COMPACTED:
        return "[context 压缩]（早期历史已摘要，原文在 JSONL）"
    if event.type == OPERATION_RECONCILE_REQUIRED:
        return f"[需裁决] {str(data)[:120]}"
    # session/started, session/resumed, run/started, run/completed,
    # memory/degraded：生命周期噪音，不渲染
    return None


async def replay_command(
    session_id: str,
    *,
    workspace_dir: str | None = None,
    write: Callable[[str], None] | None = None,
) -> str:
    """replay 命令的可测核心：返回渲染文本。

    零副作用契约（测试钉死）：只经 store.read_events 只读加载——不走
    Session.resume（那会追加 session/resumed）、不构造 runtime（无模型
    访问）、不触碰 workspace。
    """
    settings = Settings()
    if workspace_dir is not None:
        settings.workspace_dir = workspace_dir
    setup_logging(settings.log_level, settings.workspace_dir)
    store = JsonlSessionStore(root=Path(settings.workspace_dir) / "sessions")
    events = store.read_events(session_id)
    if not events:
        raise ValueError(f"Session '{session_id}' 不存在或事件日志为空")
    lines = [line for line in (render_replay_event(e) for e in events) if line]
    output = "\n".join(lines) if lines else "（无可渲染内容）"
    if write is not None:
        write(output + "\n")
    return output


def _main_replay(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="agent-harness replay")
    parser.add_argument("session_id", help="要回放的历史会话 id")
    args = parser.parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    try:
        output = asyncio.run(replay_command(args.session_id))
    except ValueError as error:
        print(f"replay 失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(output)


# ── resume（#312：抬高绝对 ceiling，接上被暂停的同一个逻辑 run）────────────


async def _cli_session_service(settings: Settings):
    """CLI 侧的 `SessionService`：**复用同一个组合根**（`#312`）。

    `SessionService` 的唯一构造点是 `web/app.py::session_service(state)`（AC3 由
    `tests/session/test_service_collaborators.py` 机械守着，CLI 也不例外）——所以这里
    建一个 `AppState` 再走那个适配函数，而不是在 CLI 里重写一遍接线。同一份接线才有
    同一份语义：`on_run_terminal` 的排队接力（ADR-0030 D4）在 `AppState` 的注释里
    原话就是「**唯一**实现点……Web/CLI 不各写一份」。

    CLI 形态带来的唯一差别是"审批队列没有消费者"：CLI 没有 `/approve` 入口，
    交互式审批档下的请求会按 `settings.approval_timeout_seconds` **fail-closed**
    超时拒绝（既不自动批准，也不无限等待）。那是 CLI 没有那个能力，不是放宽语义。

    恢复的判定（CAS、`run/resumed` 落盘、以同一 run_id 启动）全在 `SessionService`
    里 ⇒ CLI 与 Web 是同一条实现，不会在并发与版本判定上分叉。

    失败时抛领域异常（`SessionNotFound` / `BudgetRejection` / `BudgetConflict` 等），
    由 `_main_resume` 翻成退出码——这里不吞异常。
    """
    from agent_harness.web.app import AppState, session_service

    return session_service(AppState(settings))


def _event_of(events: list[SessionEvent], event_type: str) -> SessionEvent | None:
    """最后一条指定类型的事件（CLI 只读事件流的取数 helper）。"""
    for event in reversed(events):
        if event.type == event_type:
            return event
    return None


async def resume_command(
    session_id: str,
    *,
    run_turns_total: int | None = None,
    run_model_requests: int | None = None,
    run_total_tokens: int | None = None,
    run_cost_usd: str | None = None,
    run_deadline_at: str | None = None,
    run_tool_limits: dict[str, int] | None = None,
    expected_version: int,
    run_id: str | None = None,
    basis: str = RESUME_BASIS_BUDGET_INCREASE,
    session_turns_total: int | None = None,
    session_model_requests: int | None = None,
    session_total_tokens: int | None = None,
    session_cost_usd: str | None = None,
    session_deadline_at: str | None = None,
    session_tool_limits: dict[str, int] | None = None,
    session_max_delegations: int | None = None,
    session_expected_version: int | None = None,
    workspace_dir: str | None = None,
    write: Callable[[str], None] | None = None,
) -> RunOutcome:
    """`resume` 命令的可测核心：抬高**绝对** ceiling 后接上被暂停的同一个逻辑 run。

    PRD §11：CLI 显示暂停原因 / trigger dimension / consumed / limits / continuation，
    恢复时接受新的绝对 ceiling 与 expected version，且状态**取自事件**而不是进程内
    记忆——所以先打印暂停摘要（`run/paused` 的 durable data），再走
    `SessionService.resume_and_launch`（同一条 CAS 路径），最后把恢复后的 run 事件流
    渲染到结束。

    `#313`：四个 run 维度各自可抬（与 Web 的 `budget.run.*` 同一套名字与同一份判定）。
    四个都缺省 = 恢复后的 run 没有 run 档 ceiling——它**合法**（`resume_headroom_ok`
    对未配置的维度不作要求），但那是"去掉 ceiling"，不是"抬高 ceiling"；
    `_main_resume` 因此在命令行层要求至少给一个（CLI 的可用性判断，不是领域判定）
    ——**例外是 stuck 的三类依据**（`#317`）：抬高 ceiling 不是 stuck 的依据，
    所以那三个 basis 不带 ceiling 也放行，判定仍旧只有领域层一处。

    被拒时（形状 422 / 冲突 409）异常向上抛：`_main_resume` 打印后 exit 1，
    且**零副作用**（判定在任何落盘之前——见 `agent/run_budget.validate_resume`）。
    ceiling **绝不**在 CLI 侧做加法（绝对量 vs 增量是两种语义，PRD §3 明文）。

    `#315`：`run_deadline_at` 是**新的绝对截止时刻**（`--run-deadline`）。deadline
    暂停的恢复依据就是它：没点名时沿用暂停快照里那个已过去的时刻，
    `resume_headroom_ok` 会以"恢复后立刻再停"拒绝（409）——这是规格要的诚实行为，
    不是 CLI 的可用性问题（所以本层不预先拦，判定只有一处）。
    """
    settings = Settings()
    if workspace_dir is not None:
        settings.workspace_dir = workspace_dir
    setup_logging(settings.log_level, settings.workspace_dir)
    emit = write if write is not None else sys.stdout.write
    service = await _cli_session_service(settings)
    events = await service.get_events(session_id)
    paused = latest_paused_run(events)
    if paused is None:
        raise BudgetConflict(
            f"session '{session_id}' 的最新逻辑 run 不在暂停态：没有可恢复的暂停"
            "（普通续聊走 `agent-harness <message>` 或 POST /messages）"
        )
    pause_event = next(
        (event for event in events if event.seq == paused.pause_seq), None
    )
    emit(render_pause_block(pause_event.data if pause_event is not None else {}))
    result = await service.resume_and_launch(
        session_id=session_id,
        task=None,
        resume_run_id=run_id or paused.run_id,
        resume_basis=basis,
        run_max_agent_turns_total=run_turns_total,
        run_max_model_requests=run_model_requests,
        run_max_total_tokens=run_total_tokens,
        run_max_cost_usd=run_cost_usd,
        run_deadline_at=run_deadline_at,
        run_tool_call_limits=run_tool_limits,
        expected_version=expected_version,
        # `#318`：session 维触发的暂停（或想顺手抬高 session 账）在这里点名——
        # durable 行的 CAS 更新（session_expected_version 必带，判定在 service/账本）。
        session_max_agent_turns_total=session_turns_total,
        session_max_model_requests=session_model_requests,
        session_max_total_tokens=session_total_tokens,
        session_max_cost_usd=session_cost_usd,
        session_deadline_at=session_deadline_at,
        session_tool_call_limits=session_tool_limits,
        session_max_delegations=session_max_delegations,
        session_expected_version=session_expected_version,
    )
    # 恢复后的版本事实**取自事件**（`run/resumed` 在 launch 之前落盘，不在 live
    # 订阅窗口内——读回 durable 事件才是权威读数）。
    resumed = _event_of(await service.get_events(session_id), RUN_RESUMED)
    if resumed is not None:
        emit(render_resume_block(resumed.data))
    if result.run is None or result.subscriber is None:  # pragma: no cover — 装配契约
        raise RuntimeError("resume 未返回可订阅的 run 句柄（launch 路径异常）")
    renderer = StreamRenderer(emit)
    run, subscriber = result.run, result.subscriber
    final_text = ""
    pause_data: dict | None = None
    try:
        while True:
            event = await subscriber.queue.get()
            if event is service.run_manager.DONE:
                break
            renderer.handle(event)
            if event.type == RUN_COMPLETED:
                final_text = event.data.get("final_text", "")
            elif event.type == RUN_PAUSED:
                pause_data = event.data
    finally:
        run.unsubscribe(subscriber)
    if pause_data is not None:
        emit(resume_hint(session_id, data=pause_data))
    return RunOutcome(final_text=final_text, paused=pause_data is not None)


def _main_resume(argv: list[str]) -> None:
    """CLI resume 入口（`#312`）：`resume <session_id> --run-turns-total N --expected-version V`。"""
    parser = argparse.ArgumentParser(prog="agent-harness resume")
    parser.add_argument("session_id", help="被暂停的会话 id")
    parser.add_argument(
        "--run-turns-total", type=int, default=None,
        help="抬高后的**绝对** run turn ceiling（budget.run.max_agent_turns_total，"
             "不是增量；必须高于已消耗 + 预留 closeout 轮）",
    )
    parser.add_argument(
        "--run-model-requests", type=int, default=None,
        help="抬高后的**绝对** Provider 请求 ceiling（budget.run.max_model_requests）",
    )
    parser.add_argument(
        "--run-total-tokens", type=int, default=None,
        help="抬高后的**绝对** token ceiling（budget.run.max_total_tokens）",
    )
    parser.add_argument(
        "--run-cost-usd", default=None,
        help="抬高后的**绝对** 成本 ceiling（budget.run.max_cost_usd，十进制）",
    )
    parser.add_argument(
        "--run-tool-limit", action="append", default=None, metavar="NAME=N",
        help="抬高后的**绝对** per-tool 调用上限（budget.run.tool_call_limits；"
             "可重复，如 --run-tool-limit bash=10）。未点名的工具沿用暂停时的配额。",
    )
    parser.add_argument(
        "--run-deadline", default=None, metavar="RFC3339",
        help="恢复后的**新**绝对截止时刻（budget.run.deadline_at，RFC 3339 UTC）。"
             "deadline 暂停的恢复依据就是它：沿用一个已过去的时刻会被拒（409）。",
    )
    parser.add_argument(
        "--expected-version", type=int, required=True,
        help="客户端看到的预算版本（CAS；版本过期 ⇒ 409，不启动任何工作）",
    )
    parser.add_argument(
        "--session-turns-total", type=int, default=None,
        help="session 作用域的**绝对** turn ceiling（budget.session.max_agent_turns_total；"
             "CAS 抬高 durable 账行，`#318`）。session 维触发的暂停恢复靠它",
    )
    parser.add_argument(
        "--session-model-requests", type=int, default=None,
        help="session 作用域的**绝对** Provider 请求 ceiling（跨 run 累计；CAS 抬高）",
    )
    parser.add_argument(
        "--session-total-tokens", type=int, default=None,
        help="session 作用域的**绝对** token ceiling（跨 run 累计；CAS 抬高）",
    )
    parser.add_argument(
        "--session-cost-usd", default=None,
        help="session 作用域的**绝对**成本 ceiling（十进制；跨 run 累计；CAS 抬高）",
    )
    parser.add_argument(
        "--session-tool-limit", action="append", default=None, metavar="NAME=N",
        help="session 作用域对某个工具的**绝对**调用上限"
             "（budget.session.tool_call_limits；可重复；CAS 抬高）",
    )
    parser.add_argument(
        "--session-deadline", default=None, metavar="RFC3339",
        help="session 作用域的**新**绝对截止时刻（RFC 3339 UTC；CAS 抬高）",
    )
    parser.add_argument(
        "--session-max-delegations", type=int, default=None,
        help="session 作用域的整棵委派树跨 run 累计上限（CAS 抬高）",
    )
    parser.add_argument(
        "--session-expected-version", type=int, default=None,
        help="客户端看到的 session 账行版本（CAS；点名任一 --session-* 维时必填，"
             "版本过期 ⇒ 409）",
    )
    parser.add_argument(
        "--run-id", default=None,
        help="被暂停的 run_id（缺省取会话最新暂停 run；显式给出用于对齐客户端已知事实）",
    )
    parser.add_argument(
        "--basis", default=RESUME_BASIS_BUDGET_INCREASE,
        help=f"resume_basis。预算 / deadline 暂停用 {RESUME_BASIS_BUDGET_INCREASE}"
             f"（配一个抬高的绝对 ceiling）；stuck 暂停用 "
             f"{' / '.join(STUCK_RESUME_REQUIREMENTS)}，且依据必须是**被观测到的**事实"
             "（新 steer / 工作区变过 / 策略变过），不是用户声明",
    )
    args = parser.parse_args(argv)
    session_dims_named = (
        args.session_turns_total is not None
        or args.session_model_requests is not None
        or args.session_total_tokens is not None
        or args.session_cost_usd is not None
        or args.session_deadline is not None
        or bool(args.session_tool_limit)
        or args.session_max_delegations is not None
    )
    # session CAS 版本是否必带由 service 判（行**已存在**才必带；首次钉死没有可竞争
    # 的版本）——CLI 无法便宜地知道行存不存在，不在这里复制一半规则制造假拒绝。
    if (
        args.run_turns_total is None and args.run_model_requests is None
        and args.run_total_tokens is None and args.run_cost_usd is None
        and args.run_deadline is None and not args.run_tool_limit
        # `#318`：session 维的 CAS 抬高同样是合法的恢复依据（session 触发的
        # 暂停恢复就靠它；run 维一个都不点也成立——headroom 仍由领域层判）。
        and not session_dims_named
        # stuck 暂停的恢复**不**以 ceiling 为依据（`02 §5.3`：抬高预算不是它的依据），
        # 所以三类 stuck 依据不要求给 ceiling——领域层那边同样不要求。
        and args.basis not in STUCK_RESUME_REQUIREMENTS
    ):
        # 一个都不给 = 去掉全部 ceiling，而不是"抬高"：那是另一件事（普通续聊也能做到），
        # 不在 `resume` 的语义里。CLI 层拒绝，不给用户一个看不出差别的成功。
        parser.error(
            "至少给一个绝对 ceiling（--run-turns-total / --run-model-requests / "
            "--run-total-tokens / --run-cost-usd / --run-deadline / --run-tool-limit / "
            "任一 --session-* 维）"
        )
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    try:
        tool_limits = parse_run_tool_limits(args.run_tool_limit)
        session_tool_limits = parse_run_tool_limits(
            args.session_tool_limit, flag="--session-tool-limit",
        )
    except ValueError as error:
        parser.error(str(error))
    try:
        outcome = asyncio.run(resume_command(
            args.session_id,
            run_turns_total=args.run_turns_total,
            run_model_requests=args.run_model_requests,
            run_total_tokens=args.run_total_tokens,
            run_cost_usd=args.run_cost_usd,
            run_deadline_at=args.run_deadline,
            run_tool_limits=tool_limits,
            expected_version=args.expected_version,
            run_id=args.run_id,
            basis=args.basis,
            session_turns_total=args.session_turns_total,
            session_model_requests=args.session_model_requests,
            session_total_tokens=args.session_total_tokens,
            session_cost_usd=args.session_cost_usd,
            session_deadline_at=args.session_deadline,
            session_tool_limits=session_tool_limits,
            session_max_delegations=args.session_max_delegations,
            session_expected_version=args.session_expected_version,
        ))
    except (BudgetConflict, BudgetRejection, InvalidSessionId, SessionNotFound) as error:
        # 被拒请求零副作用——这里只说事实，不重试、不猜（PRD §9：422 形状 / 409 冲突）。
        print(f"resume 被拒绝：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    if outcome.paused:
        return  # 又被预算挡住仍是可恢复的事实，不是失败（同 `run` 的口径）
    if not outcome.final_text:
        raise SystemExit(1)


def _main_sessions(argv: list[str]) -> None:
    """CLI sessions 入口（Phase 14 T6）：会话列表 / lineage 树视图。"""
    parser = argparse.ArgumentParser(prog="agent-harness sessions")
    parser.add_argument(
        "--tree", action="store_true",
        help="按 lineage 树渲染（fork + delegation 两类边）",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    output = asyncio.run(sessions_command(tree=args.tree))
    print(output)


async def sessions_command(
    *, tree: bool = False, workspace_dir: str | None = None
) -> str:
    """sessions 命令的可测核心：返回渲染文本（flat 列表或 lineage 树）。"""
    settings = Settings()
    if workspace_dir is not None:
        settings.workspace_dir = workspace_dir
    setup_logging(settings.log_level, settings.workspace_dir)
    workspace_root = Path(settings.workspace_dir)
    store = JsonlSessionStore(root=workspace_root / "sessions")
    meta_store = SqliteSessionMetaStore(workspace_root / "harness.db")
    await meta_store.initialize()
    if not tree:
        metas = await meta_store.list_all()
        if not metas:
            return "（暂无会话）"
        lines = []
        for meta in metas:
            origin = f" [{meta.origin}]" if meta.origin else ""
            lines.append(f"{meta.session_id}{origin}")
        return "\n".join(lines)
    metas = await build_lineage_index(store, meta_store)
    roots = build_lineage_tree(metas)
    if not roots:
        return "（暂无会话）"
    return render_lineage_tree(roots)


def _main_budgets(argv: list[str]) -> None:
    """CLI budgets 入口（#616）：陈旧账行名（`session_budgets.tool_call_limits`
    里不在根 registry 的名字）的公开清除通道。

    整会话重整（未点名 `--tool`）是唯一需要二次确认的形态：无 `--yes` 时本函数只打印
    将清名单并 `SystemExit(2)` 拒绝执行（零改动）；`--tool` 单名收窄与 `--yes` 整会话
    重整都真执行。CLI 是同一个 `SessionService` 方法的瘦客户端（ADR-0045 D8）。
    """
    parser = argparse.ArgumentParser(prog="agent-harness budgets")
    subcommands = parser.add_subparsers(dest="command", required=True)
    clear = subcommands.add_parser(
        "clear-stale-tools",
        help="清掉会话账行里不在根 registry 的陈旧工具名（#616）",
    )
    clear.add_argument("--session", required=True, help="要清理的会话 id")
    clear.add_argument(
        "--tool", default=None, metavar="NAME",
        help="只清这一个工具名（收窄）；服务端仍按根 registry 判 stale",
    )
    clear.add_argument(
        "--yes", action="store_true",
        help="整会话重整的显式确认；缺省时只打印将清名单并拒绝执行",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    confirm_required = args.tool is None and not args.yes
    try:
        output = asyncio.run(
            budgets_clear_stale_tools_command(
                session=args.session, tool=args.tool, yes=args.yes,
            )
        )
    except (InvalidSessionId, SessionNotFound) as error:
        print(f"清理失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(output)
    if confirm_required:
        raise SystemExit(2)


async def budgets_clear_stale_tools_command(
    *, session: str, tool: str | None, yes: bool, workspace_dir: str | None = None,
) -> str:
    """`budgets clear-stale-tools` 的可测核心：返回渲染文本（#616）。

    CLI 是 API 的瘦客户端（ADR-0045 D8）：判定（谁陈旧）与落盘全在
    `SessionService.purge_stale_session_tool_limits`；本层只做参数解析 / 确认面 /
    文本渲染，不重算规则。

    确认面：整会话重整（`tool` 为空）默认只 `dry_run` 取将清名单、**零写入**，由
    `_main_budgets` 打印后 `SystemExit(2)` 拒绝；`--yes` 或点名 `--tool`（单名收窄）
    才真执行。
    """
    settings = Settings()
    if workspace_dir is not None:
        settings.workspace_dir = workspace_dir
    setup_logging(settings.log_level, settings.workspace_dir)
    service = await _cli_session_service(settings)
    dry_run = tool is None and not yes
    result = await service.purge_stale_session_tool_limits(
        session, tool=tool, entry_point=PURGE_ENTRY_CLI, dry_run=dry_run,
    )
    if not result.purged:
        scope = "将清名单" if dry_run else "清理结果"
        return f"会话 {session} 无陈旧工具名，{scope}为空（未改动）。"
    if dry_run:
        lines = [f"会话 {session} 将清除以下陈旧工具名（未确认，未改动）："]
    else:
        lines = [
            (
                f"会话 {session} 已清除 {result.rows} 个陈旧工具名"
                f"（version={result.version}）："
            )
        ]
    lines.extend(
        f"  {name} = {ceiling}" for name, ceiling in sorted(result.purged.items())
    )
    if dry_run:
        lines.append("加 --yes 执行清理。")
    return "\n".join(lines)


def _main_compact(argv: list[str]) -> None:
    """CLI compact 入口（#635）：手动触发一次上下文压缩。

    **确认面 = 直接执行，不设 `--yes`**：命令本身即用户的显式动作，压缩 append-only
    （旧事件 shadow 保留、可 replay），非破坏性；`--dry-run` 覆盖"先看后做"需求。
    CLI 是同一个 `SessionService.compact_session_context` 方法的瘦客户端
    （ADR-0045 D8）：判定与落盘在服务层，本层只做参数解析 / 渲染 / 退出码。

    退出码：无会话 / id 非法 / 非法 `--model` / 在途 run（或压缩进行中）→ 1
    （stderr 明确文案）；用法错 → 2（argparse）。
    """
    parser = argparse.ArgumentParser(prog="agent-harness compact")
    parser.add_argument("--session", required=True, help="要压缩的会话 id")
    parser.add_argument(
        "--model", default=None, metavar="NAME",
        help="摘要模型名（缺省 = settings.summary_model，再回退主模型）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只预览当前水位与可压缩窗口，零 LLM 调用、零写入",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    try:
        output = asyncio.run(
            compact_command(
                session=args.session, model=args.model, dry_run=args.dry_run,
            )
        )
    except (InvalidSessionId, SessionNotFound) as error:
        print(f"压缩失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    except ActiveRunConflict as error:
        # 两种拒绝理由共用异常类型（在途 run / 压缩已在进行中）；服务层用不同
        # detail 区分，这里翻成对应的中文回执（不吞掉"为什么被拒"）。
        reason = (
            "压缩已在进行中"
            if "compaction in progress" in str(error)
            else "在途 run 运行中"
        )
        print(f"压缩被拒绝：{reason}（零改动）", file=sys.stderr)
        raise SystemExit(1) from None
    except ConfigError as error:
        # 非法 --model：解析在零副作用前完成，未写入任何事件。
        print(f"压缩失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    print(output)


async def compact_command(
    *, session: str, model: str | None, dry_run: bool,
    workspace_dir: str | None = None,
) -> str:
    """`compact` 的可测核心：返回渲染文本（#616 式）。

    CLI 是 API 的瘦客户端（ADR-0045 D8）：能否压缩 / 在途 run 判定 / 模型解析 /
    bracket 落盘全在 `SessionService.compact_session_context`；本层只把 DTO
    `SessionContextCompaction` 渲染成人能读的两三行，不重算任何压缩规则。
    """
    settings = Settings()
    if workspace_dir is not None:
        settings.workspace_dir = workspace_dir
    setup_logging(settings.log_level, settings.workspace_dir)
    service = await _cli_session_service(settings)
    result = await service.compact_session_context(
        session, model=model, entry_point=COMPACT_ENTRY_CLI, dry_run=dry_run,
    )
    return _render_compaction(session, result)


def _format_grouped(count: int) -> str:
    """token 数 → 带千位分隔的原始数字（本票渲染用；`_format_tokens` 的 K/M 压缩
    是事件流尾注口径，这里是压缩前后对比，保留可逐位对账的原始量级）。"""
    return f"{count:_}"


def _render_compaction(session: str, result: SessionContextCompaction) -> str:
    """DTO → CLI 渲染文本（成功 / 低水位 / dry-run 三种形态）。"""
    if result.dry_run:
        window = (
            "有可压缩的早期轮"
            if result.compacted_turn_count
            else "无可压缩的早期轮（水位过低）"
        )
        return "\n".join(
            [
                f"会话 {session} 将压缩（dry-run，未改动）：",
                f"  tokens: {_format_grouped(result.tokens_before)}（当前水位）",
                f"  source: {window}",
            ]
        )
    if not result.compacted_turn_count or result.bracket_id is None:
        # 后端 floor：无可压缩早期轮（或校验闸门未过）时零写入返回 0。
        return f"会话 {session} 水位过低，无需压缩（未改动）。"
    before, after = result.tokens_before, result.tokens_after
    saved = 0 if before <= 0 else round((before - after) / before * 100)
    return "\n".join(
        [
            f"会话 {session} 压缩完成（bracket={result.bracket_id}）：",
            (
                f"  tokens: {_format_grouped(before)} → {_format_grouped(after)}"
                f"（-{saved}%）"
            ),
            # schema 是压缩管线的固定形态（builder.py 写 CONTEXT_COMPACTED 时的
            # 常量），CLI 只转述摘要来自哪一种结构，不自行生成摘要内容。
            (
                f"  source: seq {result.source_seq_start}..{result.source_seq_end}"
                " → 摘要（8 节，schema=eight_section）"
            ),
        ]
    )


def _main_approvals(argv: list[str]) -> None:
    """CLI approvals 入口（#684 Phase 2）：项目级持久审批规则的公开管理面。

    - `approvals policy list`：列出本项目 `.agent-harness/approve-policy.json` 的规则；
    - `approvals policy remove <id> [--yes]`：默认二次确认——无 `--yes` 且规则存在时，
      只打印将删规则并 `SystemExit(2)` 拒绝执行（**零改动**）；`--yes` 真删；id 不存在
      幂等成功（不报错）。

    规则内容由 Runtime 在审批流里精确派生、只读展示，CLI **不提供创建入口**：唯一安装
    路径是用户在审批卡上主动选「以后都允许」（F21：显式授权，禁止静默创建）。项目根 =
    进程当前工作目录，与 `ToolExecutor` 的 `project_root` 回落口径一致——管理面与真正
    读取规则的执行域指向同一份文件。
    """
    parser = argparse.ArgumentParser(prog="agent-harness approvals")
    subcommands = parser.add_subparsers(dest="command", required=True)
    policy = subcommands.add_parser("policy", help="项目级持久审批规则（#684）")
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    policy_sub.add_parser("list", help="列出本项目持久审批规则")
    remove = policy_sub.add_parser("remove", help="按 id 删除一条持久审批规则")
    remove.add_argument("rule_id", metavar="ID", help="要删除的规则 id")
    remove.add_argument(
        "--yes", action="store_true",
        help="显式确认删除；缺省且规则存在时只打印将删规则并拒绝执行（零改动）",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    if args.policy_command == "list":
        print(approvals_policy_list_command())
        return
    output, confirm_required = approvals_policy_remove_command(
        args.rule_id, yes=args.yes,
    )
    print(output)
    if confirm_required:
        raise SystemExit(2)


def _approve_policy_store(project_root: Path | str | None = None) -> ApprovePolicyStore:
    """本项目持久审批规则存储；`project_root` 缺省 = 进程当前工作目录。

    与 `ToolExecutor.__init__` 的回落口径逐字一致（`Path.cwd()`），保证 CLI / Web 管理面
    与执行域读写同一份 `.agent-harness/approve-policy.json`。
    """
    return ApprovePolicyStore(Path(project_root) if project_root is not None else Path.cwd())


def approvals_policy_list_command(*, project_root: Path | str | None = None) -> str:
    """`approvals policy list` 的可测核心：返回渲染文本（#684 Phase 2）。

    无规则时给友好提示（不是空输出）——「没有配置」与「读取失败」在人类眼里都应先是
    一句可读的话。坏文件由 `ApprovePolicyStore.load` fail-closed 成空列表，与无规则同形
    （回调到默认逐调用审批，绝不臆造放行）。
    """
    rules = _approve_policy_store(project_root).load()
    if not rules:
        return "（暂无持久审批规则）"
    lines = [f"本项目持久审批规则（{len(rules)} 条）："]
    lines.extend(
        f"  id={rule.id}  tool={rule.tool}  key={rule.key}  "
        f"granularity={rule.granularity.value}  created_at={rule.created_at}"
        for rule in rules
    )
    return "\n".join(lines)


def approvals_policy_remove_command(
    rule_id: str, *, yes: bool, project_root: Path | str | None = None,
) -> tuple[str, bool]:
    """`approvals policy remove <id> [--yes]` 的可测核心：返回 `(文本, 是否拒绝执行)`。

    `confirm_required=True` 时调用方（`_main_approvals`）在打印后 `SystemExit(2)`，
    且本函数**零改动**——默认二次确认是 F21 在 CLI 上的落点。

    幂等（F21/F22 同源）：id 不存在就是成功，不报错、不改文件；不带 `--yes` 时那条
    「不存在」本身也不是破坏性动作，故直接成功（无需确认一个不会发生的删除）。

    带 `--yes` 时**直接以 `remove_rule` 的返回值判定结果**，不再「先 load 判存在、
    再 remove_rule 二次 load」——两次读之间规则可能被另一进程删除/改动，那个窗口
    会让「确认时还在」的规则到删除时已消失（本次批准仍报成功）。单次读-改-写由
    `ApprovePolicyStore` 内部文件锁串行化（#684 P2-2）。
    """
    store = _approve_policy_store(project_root)
    if yes:
        if store.remove_rule(rule_id):
            return (f"已删除持久审批规则 {rule_id}。", False)
        return (f"持久审批规则 {rule_id} 不存在（幂等，未改动）。", False)
    rule = next((item for item in store.load() if item.id == rule_id), None)
    if rule is None:
        return (f"持久审批规则 {rule_id} 不存在（幂等，未改动）。", False)
    detail = (
        "将删除持久审批规则（未确认，未改动）：\n"
        f"  id={rule.id}  tool={rule.tool}  key={rule.key}  "
        f"granularity={rule.granularity.value}  created_at={rule.created_at}\n"
        "加 --yes 执行删除。"
    )
    return (detail, True)


if __name__ == "__main__":
    main()
