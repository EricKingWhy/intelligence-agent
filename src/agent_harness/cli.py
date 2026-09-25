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
from agent_harness.agent.run_budget import (
    RESUME_BASIS_BUDGET_INCREASE,
    LaunchRunBudget,
    latest_paused_run,
    run_limits_from_request,
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
from agent_harness.model.config import ModelConfig
from agent_harness.observability import flush_process_sink
from agent_harness.sandbox import WorkspaceRegistry
from agent_harness.session import (
    AGENT_DELEGATION_FINISHED,
    AGENT_DELEGATION_STARTED,
    ARTIFACT_CREATED,
    ARTIFACT_EXTERNALIZED,
    CONTEXT_COMPACTED,
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
from agent_harness.session.errors import InvalidSessionId, SessionNotFound
from agent_harness.session.fork import (
    ForkBoundaryError,
    TailSummarizer,
    fork_session,
)
from agent_harness.session.lineage import (
    build_lineage_index,
    build_lineage_tree,
    render_lineage_tree,
)
from agent_harness.storage.sqlite import SqliteSessionMetaStore

_ARGS_LINE_LIMIT = 120
_PREVIEW_LINES = 3


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


#: 触发维度 → 抬高它的 CLI 开关（`#313`：四维各自可抬；提示必须指向**真存在**的开关）。
_RESUME_FLAGS: dict[str, str] = {
    "run.max_agent_turns_total": "--run-turns-total",
    "run.max_model_requests": "--run-model-requests",
    "run.max_total_tokens": "--run-total-tokens",
    "run.max_cost_usd": "--run-cost-usd",
}


def resume_hint(session_id: str, *, data: dict) -> str:
    """暂停之后"接下来怎么做"的一行指令（`#312` 建 / `#313` 按维度点名开关）。

    ceiling 用占位符 `N`：抬高多少是用户/运维的决定，CLI **不替它猜**一个数字
    （猜出来的"建议值"会被当成策略，且绝对 ceiling 与增量是两种语义）。

    开关按 `trigger_dimension` 取（不是写死 turns）：暂停可能落在 requests / token /
    cost 维度上，提示里给一个抬不动它的开关是**假指令**（PRD §11：CLI 显示的就是
    durable 事实本身）。未知维度（本票之外的暂停原因）回落到 turns 开关——那是
    `#308` 起一直存在的维度，也是唯一一个任何 run 都读得懂的。
    """
    dimension = str(data.get("trigger_dimension", ""))
    flag = _RESUME_FLAGS.get(dimension, "--run-turns-total")
    return (
        f"  resume: agent-harness resume {session_id}"
        f" {flag} N --expected-version {data.get('budget_version', '')}"
        "  (N 是**绝对** ceiling，必须高于 consumed + 预留 closeout 轮；不是增量)\n"
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
        runtime = await build_runtime(
            settings=settings, wiring=wiring, stores=stores,
            workspace_registry=workspace_registry,
            session_id=session_id, workspace=workspace,
            max_agent_turns=fuse.max_agent_turns,
            local_fuse_source=fuse.source,
            # `#312`：run 作用域的绝对 ceiling（`--run-turns-total`）。None = 本 run
            # 不设 run 档 ceiling——**不是** 0（0 会把第一条 model 决策就挡下）。
            run_budget=LaunchRunBudget(
                limits=run_limits_from_request(
                    max_agent_turns_total=run_turns_total,
                    max_model_requests=run_model_requests,
                    max_total_tokens=run_total_tokens,
                    max_cost_usd=run_cost_usd,
                    accounting=HARNESS_MODEL_ACCOUNTING,
                ),
            ),
            auto_approve=True,
            session_store=store,
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
        print(error, file=sys.stderr)
        raise SystemExit(2) from error
    try:
        _main_dispatch()
    finally:
        lock.release()
        # 旁路收尾（ADR-0018 D3）：任何退出路径（正常/异常/SystemExit）都尽力
        # 发送剩余 Langfuse span；未配置/未装配时零开销 no-op。
        flush_process_sink()


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
    args = parser.parse_args(argv)
    outcome = asyncio.run(
        run(
            args.message,
            run_turns_total=args.run_turns_total,
            run_model_requests=args.run_model_requests,
            run_total_tokens=args.run_total_tokens,
            run_cost_usd=args.run_cost_usd,
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
        help="从父会话的第 N 条用户消息处分叉（该消息不进 seed，由你在新分支重发）",
    )
    parser.add_argument(
        "--no-summary", action="store_true",
        help="跳过 tail summary（默认对被放弃路线生成一次 LLM 摘要挂进新会话）",
    )
    return parser.parse_args(argv)


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
    workspace_registry = WorkspaceRegistry(root=workspace_root, backend="local")
    summarizer = None
    if not no_summary:
        from agent_harness.model.provider import create_chat_model

        summarizer = TailSummarizer(
            create_chat_model(ModelConfig.from_settings(settings))
        )
    child = await fork_session(
        store, meta_store, session_id,
        boundary_user_message_seq=from_message,
        workspace_registry=workspace_registry,
        summarizer=summarizer, with_tail_summary=not no_summary,
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
    expected_version: int,
    run_id: str | None = None,
    basis: str = RESUME_BASIS_BUDGET_INCREASE,
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
    `_main_resume` 因此在命令行层要求至少给一个（CLI 的可用性判断，不是领域判定）。

    被拒时（形状 422 / 冲突 409）异常向上抛：`_main_resume` 打印后 exit 1，
    且**零副作用**（判定在任何落盘之前——见 `agent/run_budget.validate_resume`）。
    ceiling **绝不**在 CLI 侧做加法（绝对量 vs 增量是两种语义，PRD §3 明文）。
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
        expected_version=expected_version,
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
        "--expected-version", type=int, required=True,
        help="客户端看到的预算版本（CAS；版本过期 ⇒ 409，不启动任何工作）",
    )
    parser.add_argument(
        "--run-id", default=None,
        help="被暂停的 run_id（缺省取会话最新暂停 run；显式给出用于对齐客户端已知事实）",
    )
    parser.add_argument(
        "--basis", default=RESUME_BASIS_BUDGET_INCREASE,
        help=f"resume_basis（本票只实现 {RESUME_BASIS_BUDGET_INCREASE}）",
    )
    args = parser.parse_args(argv)
    if (
        args.run_turns_total is None and args.run_model_requests is None
        and args.run_total_tokens is None and args.run_cost_usd is None
    ):
        # 一个都不给 = 去掉全部 run ceiling，而不是"抬高"：那是另一件事（普通续聊也能做到），
        # 不在 `resume` 的语义里。CLI 层拒绝，不给用户一个看不出差别的成功。
        parser.error(
            "至少给一个绝对 ceiling（--run-turns-total / --run-model-requests / "
            "--run-total-tokens / --run-cost-usd）"
        )
    settings = Settings()
    setup_logging(settings.log_level, settings.workspace_dir)
    try:
        outcome = asyncio.run(resume_command(
            args.session_id,
            run_turns_total=args.run_turns_total,
            run_model_requests=args.run_model_requests,
            run_total_tokens=args.run_total_tokens,
            run_cost_usd=args.run_cost_usd,
            expected_version=args.expected_version,
            run_id=args.run_id,
            basis=args.basis,
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


if __name__ == "__main__":
    main()
