"""内置场景：重复的真实工具失败 ⇒ 恰好一次纠正 ⇒ 同模式持续 ⇒ stuck 暂停 ⇒ 有据恢复（`#317` T9）。

## 它证明什么（六条结构事实）

1. **失败是确定性的、重复是真的**：工作区里**故意没有** `upstream_ready.txt`（上游依赖还没
   产出它），任务要求模型用 **read 工具**（不是 bash）对着**同一个路径**原样重试 6 次
   （3 + 3 两轮）。生产 `ReadTool` 对不存在的文件返回
   `ToolResult.failure(error_code=TOOL_EXECUTION_ERROR)` ⇒ 事件流里因此有一串**同动作同错误**
   的 `tool/call` + `tool/result` —— ① 的计数对象，不是断言自己编出来的。
   ⚠ 为什么不是 bash：**非零退出码不是工具失败**——生产 `BashTool` 对 `exit_code=3` 返回
   `ok=true`（命令确实执行了，退出码是交给模型看的**结果**）。用 bash 演"确定性失败"会
   让 ① 一次都数不到（实测：3/3 跑到 `run/completed`）。失败必须是 `ok=false` 的那一侧。
2. **恰好一次纠正**：第 3 次同错命中 ① 的首达阈值 ⇒ 一条 `tool/failure-guard(level=soft)`
   + 一条 `injected_by=tool_failure_guard` 的 `user/message`（① 复用既有护栏形状，
   ADR-0048 D2）；`guard/stuck(level=replan)` 在本 run 里必须**为空**。整场只有这一条纠正。
3. **暂停是非终态、且带着可比的快照**：同模式再达阈值 ⇒ `guard/stuck(level=paused)` +
   `run/paused(reason=stuck)`，`data.stuck` 八键齐（`STUCK_KEYS`，含逐维 `policy_inputs`——
   它必须能重算出同一格里的 `policy_version`，否则恢复侧还原不回来），其中
   `threshold=3`、`count == 轨迹里那串同动作失败的真实条数`、`replan_count=1`、
   `environment_revision` / `policy_version` 非空；`resume_requirements` = 三类依据。
   整场**没有** `run/completed` / `run/failed` / `run/interrupted`，也没有旧 HARD 形状
   （`tool/failure-guard(level=hard)` / `run/failed(identical_tool_failure_loop)`，
   ADR-0048 D5 把 HARD 的动作改成了暂停）。暂停点上没有悬空 `tool/call`。
4. **四种无依据的恢复全部 409，且零副作用**（事件流一字不改、不发 Provider / 工具请求）：
   `budget_increase`（stuck 缺的不是额度——即使点一个足够大的绝对 ceiling 也拒）、
   没有 steer 的 `relevant_steer`、工作区没变的 `environment_change`、策略没变的
   `policy_change`；外加一条**无关变更**：改动落在**会话工作区之外** ⇒ 环境依据照旧不成立
   （"我改过了"是声明，不是证据）。
5. **有依据的恢复放行并记账**：暂停之后**上游真的就绪了**（场景把 `upstream_ready.txt` 写进
   工作区 ⇒ 现算的环境 revision 与暂停快照不同）⇒ `run/resumed` 以**同一 `run_id`** 接回，
   `resume_evidence.basis=environment_change`、`environment_revision != recorded`，恢复后
   **≥1 次接纳**、收口在安全状态。
   ⚠ **不断言**"恢复那条腿真的读了那份文件"：它能不能知道上游就绪，取决于"恢复依据的正文
   有没有被投进模型上下文"，而生产**没有**这条投递路径（`send_message(mode="steer")` 要求
   有在途 run；运行时只排空内存队列）——实测两条读数与边界见 ADR-0048 §4 的残余 4。
6. **重放能重建同样的判定**（AC「Restart and replay reconstruct the exact fingerprint/replan
   state」）：暂停之后用**生产检测器** `StuckDetector.from_events` 把"暂停前那一段"重放成
   状态，再喂那段历史的**尾部**：得到的信号必须与落盘载荷**同模式、同计数、同阈值**，且
   **不再**产生 replan 信号（"恰好一次"的闩也由 durable 事件重建）；同一份事件再喂一次
   必须什么都不产出（游标语义）。

## 为什么任务必须写"原样重试"（这条前提是场景的一部分，不是对缺陷的掩盖）

`02 §5.3` 的护栏假设"模型可能不换路"；而生产纠正消息的原文是"不要再以相同方式重试"
（`prompt/builtin.py::_CORRECTIVE_TOOL_FAILURE_GUARD`）。两者同时在场时，模型服从哪一边
是**它的选择**。本场景把任务写成"上游依赖预热期间的等待型任务"——真实世界里存在的一类
任务：正确做法就是**原样重试**，而且用户指令显式说明"通用的换路提示不适用于本任务"。
**若模型改路**（撞上 ① 之后就停手、或开始探索别的工具），本场景判红而不是判绿：那说明
这次运行没有走到"同一模式持续"这一步，票面 AC 没有被证明。这不是"模型表现"的判据，
是"这次运行有没有构成证据载体"的判据。

## 生产性来自哪里（这条决定证据算不算数）

- **run / 暂停 / 恢复面**：`AppState(settings)` + `session_service(state)` —— 与 Web 应用、
  CLI `resume` 命令**同一个组合根**；
- **模型 / 工具 / 沙箱**：与 smoke / long_task / pause_resume 同源 —— `build_runtime`
  装配的生产工具集（真正的 `ReadTool` 实例）、生产 Sandbox、真实 provider 客户端；
- **检测器**：第 6 条用的是产品里**同一个** `StuckDetector`（不是场景自己写一份重放）；
- **凭证零泄漏**：证据只留事件流与断言读数；本场景的工录入参只有一个工作区相对路径。

## 一次性边界

运行时产物（会话轨迹 / SQLite / workspace 登记）全部落在一次尝试的临时根目录内
（`settings.workspace_dir` 重定向到 `ctx.session_root.parent`）；"无关变更"那条故意写在
**会话工作区之外、同一个一次性根目录之内**，跑完由 `workspace.teardown()` 一起销毁。

## 这条运行**不得**被当成"模型表现"的证据

判据全部机械可检：事件类型 / 计数 / 字段形状 / 异常类型 / 事件流是否一字未改。
本场景不引入 LLM judge，也不评任务的措辞好坏（`runner.scope.does_not_cover` 已登记）。
"""

from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import Mapping
from typing import Any

from evaluation.assertions import dangling_tool_call_ids
from evaluation.live_gate.registry import AttemptOutcome, ScenarioContext
from evaluation.live_gate.scenarios.accounting import (
    consumed_counter_assertions,
    request_accounting,
)
from evaluation.live_gate.schema import AssertionResult

SCENARIO_ID = "stuck-tool-failure-pause"
#: v2 = 失败面换成"read 读一个还不存在的上游产物"。v1（bash 非零退出码）实测**跑不出**
#: ①：生产 BashTool 对 `exit_code=3` 返回 `ok=true`（非零退出码是给模型看的**结果**，
#: 不是工具失败），3/3 都跑到 `run/completed`。那次的失败读数留在
#: `docs/live_gate/20260927T045736-533d31735a3d-stuck-tool-failure-pause/`。
#: v3 = 有据恢复那条腿改用 `environment_change`（"上游真的就绪了"这条世界事实），并去掉
#: "恢复后读到了那份文件"这条断言：v2 实测暴露生产**没有**把恢复依据的正文投进模型上下文
#: 的路径（两条读数见 `docs/live_gate/20260927T051359-048e2246a362-…`、边界见 ADR-0048 §4）。
#: v4 = `#317` T9 两轴审查的处置：首跑声明非默认档位（`policy_change` 拒绝腿因此有鉴别力）、
#: 暂停断言要求逐维策略输入能重算出记下的摘要、失败面判据进谓词（`FAILURE_MARKER`）、
#: 删掉一条恒真断言（`ok=True` 的"无关变更没被当成进展"——事实由拒绝腿承载）。
SCENARIO_VERSION = 4

#: 上游依赖**还没**产出的那份产物：read 工具对它必然失败（`文件 '...' 不存在。`）。
#: 名字在任务文本里逐字点名——模型不需要猜，也不会去探索。
AWAITED_FILE = "upstream_ready.txt"
#: 上游就绪之后这份文件的内容。它落进工作区就是**环境变更**这次恢复依据的物理来源
#: （环境 revision = 全树清单摘要 ⇒ 多一个文件必然算出不同的值）。
AWAITED_CONTENT = "upstream: ready"

#: 失败面用的工具名（`02 §5.3` 的"同动作"= 同工具 + 同参数）。**不能**用 bash：
#: 非零退出码是 `ok=true`，数不到 ①（见模块 docstring 第 1 条）。
FAILED_TOOL = "read"
#: `ToolResult.failure.message` 的判据子串（生产 ReadTool 的原文含"不存在"）。
FAILURE_MARKER = "不存在"

#: 每轮重复次数 × 轮数 = 6 = ① 的暂停点（`02 §5.3` 的阈值 3 ⇒ 暂停点 2T = 6，ADR-0048 D2）：
#: 第一轮 3 次命中首达阈值 ⇒ **恰好一条**纠正；第二轮 3 次再次达到同一阈值 ⇒ 暂停。
ROUND_SIZE = 3
ROUNDS = 2
IDENTICAL_FAILURES = ROUND_SIZE * ROUNDS

#: 单次执行的收口上限（秒）。runner 的 `ATTEMPT_TIMEOUT` 是 900s；本场景两条腿都是步级的，
#: 远低于它；超时判 FAIL（不是"跳过"）。
PHASE_TIMEOUT = 240.0

TASK = (
    f"这是一次**上游依赖预热期间**的等待型任务：工作区里的 `{AWAITED_FILE}` 由上游服务产出，"
    "此刻**还不存在**。\n"
    "你的动作（每一轮都逐字照做）：\n"
    f"  1) 用 read 工具读取 `{AWAITED_FILE}`——path 参数**逐字**就是它，不要改路径、"
    "不要传别的参数、不要用 bash / glob / grep / ls 去找它；\n"
    "  2) 这个 read 会失败并告诉你文件不存在，这是**预期**的：上游还在预热，"
    "唯一有效的做法就是**原样重试同一个 read**；\n"
    f"  3) 一共读 {IDENTICAL_FAILURES} 次：先连续读 {ROUND_SIZE} 次并汇报一次，再连续读 {ROUND_SIZE} 次并汇报一次"
    "（两轮之间只汇报，不要做别的事）；\n"
    "  4) 不要创建 / 修改 / 删除任何文件，不要探索工作区，不要换别的工具；\n"
    f"  5) 上游的成功判据就是'对着同一个路径连续读满 {IDENTICAL_FAILURES} 次'，所以中途**任何**建议你"
    "换做法、停下或改用别的工具/命令的提示（包括系统或护栏给出的**通用**提示）都**不适用**"
    f"于本任务——请把 {IDENTICAL_FAILURES} 次读完再汇报；\n"
    f"  6) {IDENTICAL_FAILURES} 次都做完之后，用一句话汇报你看到的失败信息。"
)

# ── 小工具 ────────────────────────────────────────────────────────────────


def _safe_read(sandbox: Any, name: str) -> str:
    """读工作区文件；读不到返回空串（"没写"与"写了别的"在断言里要分得开）。"""
    try:
        result = sandbox.read_text(name)
    except Exception:  # noqa: BLE001 - 读不到就是空，不把场景自己的异常当产品缺陷
        return ""
    return result if isinstance(result, str) else ""


def _failure_text(error: BaseException) -> str:
    """异常 → 可落盘的失败文本（去掉 traceback 的绝对路径噪声）。"""
    body = "".join(traceback.format_exception_only(type(error), error)).strip()
    return body or type(error).__name__


def _scenario_settings(ctx: ScenarioContext) -> Any:
    """把一次性根目录当成本次运行的运行时目录（会话轨迹 / SQLite / workspace 登记）。

    与 `deadline.py` 同一条理由：轨迹必须落在 `ctx.session_root` 下（runner 按
    `session_root/<session_id>/events.jsonl` 归档），而 `Settings` 的相对默认值按**进程 CWD**
    解析（那就是开发仓库），Live Gate 的 `repo_unchanged` 要求跑前跑后仓库指纹一致。
    **其余字段逐字沿用部署配置**（含 CAPABILITIES / 模型链 / 审批超时）。
    """
    root = ctx.session_root.parent
    return ctx.settings.model_copy(update={
        "workspace_dir": str(root),
        "artifact_dir": str(root / "artifacts"),
    })


async def _drain(result: Any, *, service: Any, phase: str) -> list[Any]:
    """消费一次执行的 live 流直到哨兵；返回流里的 AgentEvent 列表。

    形状照 CLI：订阅者队列 `get()` 到 `run_manager.DONE` 即本次执行收口（**暂停也走这条**：
    `run/paused` 是非终态收口，但 run task 同样结束）。到点未收口 ⇒ 抛 `TimeoutError`
    （runner 外层仍会兜住），绝不伪装成"跑完了"。
    """
    run, subscriber = result.run, result.subscriber
    if run is None or subscriber is None:  # pragma: no cover - 装配契约
        raise RuntimeError(f"{phase}：launch 未返回 run/subscriber（装配契约异常）")
    streamed: list[Any] = []
    try:
        async with asyncio.timeout(PHASE_TIMEOUT):
            while True:
                event = await subscriber.queue.get()
                if event is service.run_manager.DONE:
                    break
                streamed.append(event)
    except TimeoutError as error:
        raise TimeoutError(f"{phase}：{PHASE_TIMEOUT:.0f}s 内未收口") from error
    finally:
        run.unsubscribe(subscriber)
    return streamed


def _identical_failure_streak(
    events: list[Any], *, cutoff_seq: int | None, tool_name: str = FAILED_TOOL,
) -> tuple[int, str]:
    """场景侧**独立重算** ① 的重复串：连续同 (tool_name, 参数) 的失败，最长的一段。

    与产品检测器**不是**同一份实现（`accounting.py` 的同一纪律：故意留两份，分歧判红）。
    返回 `(最长串长度, 那个动作的可读标签)`。判定规则比产品窄且更直白：
    `tool/call` 与配对的 `tool/result` 按 `tool_call_id` 配对，结果非成功即算一次失败
    （只算 `tool_name` 对得上的那些）；动作键 = `tool_name` + 参数原文的规范化 JSON
    （`command` 参数存在时取它，否则取整个 args 的排序 JSON——read 走的正是后一条）。
    中间夹任何别的动作就断开。
    """
    calls: dict[str, tuple[str, str]] = {}
    failed: set[str] = set()
    order: list[str] = []
    for event in events:
        if cutoff_seq is not None and event.seq is not None and event.seq > cutoff_seq:
            break
        if event.type == "tool/call":
            call_id = str(event.data.get("tool_call_id"))
            args = event.data.get("args")
            command = ""
            if isinstance(args, dict):
                raw = args.get("command")
                command = raw if isinstance(raw, str) else json.dumps(args, sort_keys=True)
            calls[call_id] = (str(event.data.get("tool_name")), command)
            order.append(call_id)
        elif event.type == "tool/result":
            call_id = str(event.data.get("tool_call_id"))
            content = event.data.get("content")
            ok = False
            if isinstance(content, str):
                try:
                    payload = json.loads(content)
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    ok = bool(payload.get("ok"))
            if not ok:
                failed.add(call_id)

    best = 0
    best_label = ""
    run_length = 0
    previous: tuple[str, str] | None = None
    for call_id in order:
        action = calls.get(call_id)
        if action is None or call_id not in failed or action[0] != tool_name:
            run_length, previous = 0, None
            continue
        run_length = run_length + 1 if action == previous else 1
        previous = action
        if run_length > best:
            best, best_label = run_length, f"{action[0]} {action[1]!r}"
    return best, best_label


def _inputs_match_the_digest(stuck: Mapping[str, Any]) -> bool:
    """暂停载荷里的逐维策略输入能**重算出**记下来的那个 `policy_version` 吗。

    只判"两格同源"：恢复侧还原策略靠的正是这组逐维值，只留摘要就还原不回来
    （`#317` T9 审查 P1）。值本身的合法域由产品侧负责，场景不重复校验。
    """
    if not isinstance(stuck, Mapping):
        return False
    inputs = stuck.get("policy_inputs")
    digest = stuck.get("policy_version")
    if not isinstance(inputs, Mapping) or not isinstance(digest, str):
        return False
    # 与模块其余部分同形：`agent_harness` 只在方法体内惰性引入。
    from agent_harness.agent.resume_evidence import digest_policy_inputs

    try:
        return digest_policy_inputs(inputs) == digest
    except (TypeError, ValueError):
        return False


class _Refusal:
    """一次"恢复必须被拒"的实测结论（不抛给调用方，转成一等事实）。

    与 `deadline.py` 的同名小工具形状相同、各自持有：它的字段跟着各自的恢复调用
    （谁点名了哪个 ceiling）与各自的判据子串走，为省 20 行在场景之间开共享面不值得。
    """

    def __init__(
        self, *, raised: BaseException | None, events_unchanged: bool, expected: str,
    ) -> None:
        self.raised = raised
        self.events_unchanged = events_unchanged
        self.expected = expected
        self.reason = "" if raised is None else f"{type(raised).__name__}: {raised}"

    @property
    def ok(self) -> bool:
        return (
            self.raised is not None
            and self.raised.__class__.__name__ == "BudgetConflict"
            and self.events_unchanged
            and self.expected in self.reason
        )


class StuckToolFailurePauseScenario:
    """真实模型 + 生产工具：同一条确定性失败的调用被原样重试 ⇒ 一次纠正 ⇒ stuck 暂停 ⇒ 有据恢复。"""

    id = SCENARIO_ID
    version = SCENARIO_VERSION
    description = (
        f"重复的真实工具失败（read 读一个上游还没产出的文件，{IDENTICAL_FAILURES} 次）："
        "首达阈值恰好一次纠正（tool/failure-guard soft），同模式再达阈值 ⇒ "
        "run/paused(reason=stuck)（非终态、带环境/策略快照）；五种无依据恢复"
        "（含工作区之外的无改变更）全部 409 且零副作用；上游就绪（工作区真的变了）⇒ "
        "同一 run_id 以 environment_change 有据恢复并继续接纳"
    )

    async def prepare(self, ctx: ScenarioContext) -> list[str]:
        """前置自查：**上游产物此刻必须还不存在**（否则第一次 read 就成功，测不到 ①）。

        返回未满足的前置（非空 ⇒ BLOCKED，不发模型请求）。
        """
        existing = _safe_read(ctx.sandbox, AWAITED_FILE)
        if existing:
            return [
                (
                    f"工作区里已经有 {AWAITED_FILE}（{len(existing)} 字符）——本场景要求它在"
                    "暂停之前不存在，否则第一次 read 就会成功、① 永远数不到"
                ),
            ]
        return []

    async def run(self, ctx: ScenarioContext) -> AttemptOutcome:
        """跑一次完整流程。**异常一律转 `ok=False`**（runner 兜底捕获是第二道）。"""
        from agent_harness.agent.run_budget import (
            REASON_STUCK,
            RESUME_BASIS_BUDGET_INCREASE,
            RESUME_BASIS_ENVIRONMENT_CHANGE,
            RESUME_BASIS_POLICY_CHANGE,
            RESUME_BASIS_RELEVANT_STEER,
            latest_paused_run,
        )
        from agent_harness.session import JsonlSessionStore, Session
        from agent_harness.web.app import AppState, session_service

        settings = _scenario_settings(ctx)
        state = AppState(settings)
        service = session_service(state)
        legs: dict[str, Any] = {
            "first_stream": [],
            "resume_stream": [],
            "refusals": {},
            "raised_turns": None,
        }
        try:
            # 显式初始化恢复三 Store（DDL + transport 表）：本场景**直接**读
            # `state.operation_ledger`，不把"某条服务路径恰好先 ensure 过"当成前提。
            await state.ensure_stores()
            store = JsonlSessionStore(root=ctx.session_root)
            state.workspace_registry.create(
                ctx.session_id, workspace_root=ctx.sandbox.workspace_root,
            )
            Session.start(store, session_id=ctx.session_id, cwd=ctx.sandbox.workspace_root)

            # 起跑时就声明一个**非默认档位**（`#317` T9 审查 P1）：暂停快照里的策略面因此
            # 非默认，下面那条 `policy_change` 拒绝腿才有鉴别力——恢复侧若只拿"本次请求
            # 声明了什么"去重算，摘要必然不同，"没变"会被判成"变了"而放行（fail-open）。
            # `coding` 在 `BUILTIN_PROFILES` 里有 `read` 且在声明面不收窄 turn ceiling。
            from agent_harness.session.amend import AmendOptions

            first = await service.resume_and_launch(
                session_id=ctx.session_id, task=TASK,
                amend=AmendOptions(agent_profile="coding"),
            )
            legs["first_stream"] = await _drain(first, service=service, phase="stuck 首次执行")
            events = await service.get_events(ctx.session_id)
            paused = latest_paused_run(events)
            if paused is None or paused.reason != REASON_STUCK:
                # 场景前提不成立（模型没走到"同模式持续"就收口了）：如实 ok=False。
                # **不**做假恢复，也不把这里兜成"跳过"。
                raise RuntimeError(
                    "首次执行没有留下 stuck 暂停（run/paused reason=stuck）——本次运行"
                    "不构成证据载体；事件类型="
                    f"{[event.type for event in events]}"
                )
            pause = next(
                event for event in events
                if event.type == "run/paused" and event.seq == paused.pause_seq
            )
            legs["pause"] = pause

            # ①‑④ 无依据的恢复：全部必须 409，且事件流一字不改。
            raised = int(paused.consumed.agent_turns or 0) + 50
            legs["raised_turns"] = raised
            legs["refusals"]["budget_increase"] = await self._expect_refusal(
                service, store, session_id=ctx.session_id, paused=paused,
                basis=RESUME_BASIS_BUDGET_INCREASE, expected="budget_increase",
                run_max_agent_turns_total=raised,
            )
            legs["refusals"]["relevant_steer_without_a_steer"] = await self._expect_refusal(
                service, store, session_id=ctx.session_id, paused=paused,
                basis=RESUME_BASIS_RELEVANT_STEER, expected="没有",
            )
            legs["refusals"]["environment_change_unchanged"] = await self._expect_refusal(
                service, store, session_id=ctx.session_id, paused=paused,
                basis=RESUME_BASIS_ENVIRONMENT_CHANGE, expected="相同",
            )
            legs["refusals"]["policy_change_unchanged"] = await self._expect_refusal(
                service, store, session_id=ctx.session_id, paused=paused,
                basis=RESUME_BASIS_POLICY_CHANGE, expected="相同",
            )
            # 无关变更：改动落在**会话工作区之外**（同一个一次性根目录里）⇒ 环境快照照旧。
            outside = ctx.session_root.parent / "outside-the-workspace.txt"
            outside.write_text("irrelevant change", encoding="utf-8")
            legs["refusals"]["environment_change_outside_the_workspace"] = (
                await self._expect_refusal(
                    service, store, session_id=ctx.session_id, paused=paused,
                    basis=RESUME_BASIS_ENVIRONMENT_CHANGE, expected="相同",
                )
            )

            # ⑤ 有依据的恢复：上游**真的**就绪了——产物落进工作区即"世界变了"，
            # 现算的环境 revision 因此与暂停快照不同 ⇒ environment_change 成立。
            # 直接同步调用（与其它场景的 `prepare` 同形）：LocalSubprocessSandbox 的
            # `write_text` 是一次短写，不引入线程池这一层。
            ctx.sandbox.write_text(AWAITED_FILE, AWAITED_CONTENT)
            legs["awaited_file_written"] = _safe_read(ctx.sandbox, AWAITED_FILE)
            resume = await service.resume_and_launch(
                session_id=ctx.session_id, task=None,
                resume_run_id=paused.run_id,
                resume_basis=RESUME_BASIS_ENVIRONMENT_CHANGE,
                expected_version=paused.version,
            )
            legs["resume_stream"] = await _drain(
                resume, service=service, phase="有据恢复后的执行",
            )
        except Exception as error:  # noqa: BLE001 - 失败要如实记录（runner 统一脱敏）
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id, error=_failure_text(error),
            )
        finally:
            # 关闭可选能力（记忆形成泵 / MCP / 外部连接）与在途 run —— 一次尝试一个生命周期。
            await state.shutdown()

        # 终态读盘必须在 `shutdown()` **之后**：可选能力的收尾会追加 durable 事实
        # （`memory/degraded` 就落在 `run/completed` 之后），早读会让判定所依据的事件
        # 比 runner 复制的那份轨迹少一行。
        try:
            events = await service.get_events(ctx.session_id)
            replayed = store.read_events(ctx.session_id)
            projection = await service.budget_projection(ctx.session_id)
            operations = await state.operation_ledger.list_for_session(ctx.session_id)
        except Exception as error:  # noqa: BLE001 - 同上的如实记录
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id, error=_failure_text(error),
            )

        tool_calls = [
            str(event.data.get("tool_name"))
            for event in events if event.type == "tool/call"
        ]
        run_id = next(
            (str(event.run_id or "") for event in events if event.type == "run/started"), "",
        )
        assertions = self._assertions(
            ctx=ctx, events=events, replayed=replayed, tool_calls=tool_calls,
            projection=projection, operations=operations, legs=legs,
        )
        pauses = [event for event in events if event.type == "run/paused"]
        completed = [event for event in events if event.type == "run/completed"]
        return AttemptOutcome(
            ok=all(item.ok for item in assertions),
            session_id=ctx.session_id,
            run_id=run_id,
            # 终态读 durable 事实：stuck 暂停是**非终态**；恢复那条腿两种结局都如实报
            # （模型读到内容后收尾 ⇒ completed；若它又把自己走成暂停 ⇒ paused）。
            run_status="completed" if completed else ("paused" if pauses else "unknown"),
            steps=sum(1 for event in events if event.type == "model/completed"),
            tool_calls=tool_calls,
            assertions=assertions,
            event_count=len(events),
            output_tail=self._continuation_tail(legs.get("pause")),
        )

    async def _expect_refusal(
        self, service: Any, store: Any, *, session_id: str, paused: Any, basis: str,
        expected: str, run_max_agent_turns_total: int | None = None,
    ) -> _Refusal:
        """调一次恢复并**期望被拒**；同时核实事件流一字未改（零副作用）。"""
        before = [event.to_dict() for event in store.read_events(session_id)]
        raised: BaseException | None = None
        try:
            await service.resume_and_launch(
                session_id=session_id, task=None,
                resume_run_id=paused.run_id,
                resume_basis=basis,
                expected_version=paused.version,
                run_max_agent_turns_total=run_max_agent_turns_total,
            )
        except BaseException as error:  # noqa: BLE001 - 拒绝是**期望结果**，不是异常面
            raised = error
        after = [event.to_dict() for event in store.read_events(session_id)]
        return _Refusal(
            raised=raised, events_unchanged=before == after, expected=expected,
        )

    # ── 断言（机械可检，不是 LLM judge）────────────────────────────────

    def _assertions(
        self, *, ctx: ScenarioContext, events: list[Any], replayed: list[Any],
        tool_calls: list[str], projection: dict[str, Any], operations: list[Any],
        legs: dict[str, Any],
    ) -> list[AssertionResult]:
        from agent_harness.agent.guards import (
            STUCK_LEVEL_PAUSED,
            STUCK_LEVEL_REPLAN,
        )
        from agent_harness.agent.run_budget import (
            REASON_STUCK,
            STUCK_KEYS,
            STUCK_RESUME_REQUIREMENTS,
        )
        from agent_harness.session import (
            GUARD_STUCK,
            MODEL_REQUEST,
            RUN_COMPLETED,
            RUN_FAILED,
            RUN_INTERRUPTED,
            RUN_PAUSED,
            RUN_RESUMED,
            RUN_STARTED,
            TOOL_CALL,
            TOOL_FAILURE_GUARD,
            TOOL_RESULT,
            USER_MESSAGE,
        )

        results: list[AssertionResult] = []
        pauses = [event for event in events if event.type == RUN_PAUSED]
        pause = pauses[0] if pauses else None
        pdata = dict(pause.data) if pause is not None else {}
        stuck = pdata.get("stuck") if isinstance(pdata.get("stuck"), dict) else {}
        cutoff = pause.seq if pause is not None else None
        terminal_types = (RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED)
        terminal = [event for event in events if event.type in terminal_types]
        resumes = [event for event in events if event.type == RUN_RESUMED]
        started = [event for event in events if event.type == RUN_STARTED]
        run_id = str(started[0].run_id or "") if started else ""

        # ── 事实 1：同一条动作真的被原样重复了，且每次的失败面一样 ──────────
        def _result_events(call_ids: set[str]) -> list[Any]:
            return [
                event for event in events
                if event.type == TOOL_RESULT
                and str(event.data.get("tool_call_id")) in call_ids
            ]

        streak, streak_label = _identical_failure_streak(events, cutoff_seq=cutoff)
        call_ids_at_pause = {
            str(event.data.get("tool_call_id"))
            for event in events
            if event.type == TOOL_CALL and (cutoff is None or event.seq <= cutoff)
            and str(event.data.get("tool_name")) == FAILED_TOOL
        }
        failure_payloads: set[str] = set()
        failed_results = 0
        for event in _result_events(call_ids_at_pause):
            content = event.data.get("content")
            payload = None
            if isinstance(content, str):
                try:
                    payload = json.loads(content)
                except ValueError:
                    payload = None
            if isinstance(payload, dict) and not payload.get("ok"):
                failed_results += 1
                failure_payloads.add(
                    json.dumps(
                        {"code": payload.get("error_code"), "message": payload.get("message")},
                        sort_keys=True, ensure_ascii=False,
                    )
                )
        results.append(AssertionResult(
            name="the_model_really_repeated_one_identical_action",
            ok=streak >= IDENTICAL_FAILURES,
            detail=(
                f"轨迹里最长的一串'同动作失败'={streak}（要求 ≥{IDENTICAL_FAILURES}）"
                f"，动作={streak_label}；{FAILED_TOOL} 调用总数={len(call_ids_at_pause)}"
                f"，其中失败结果={failed_results}"
            ),
        ))
        # 判据子串也进谓词（不只是写进 detail）：只数"组合数 = 1"的话，换成任何一个
        # **别的**单一失败（工具异常、权限拒绝）照样绿。
        only_payload = next(iter(failure_payloads)) if len(failure_payloads) == 1 else ""
        results.append(AssertionResult(
            name="every_repeat_failed_the_same_deterministic_way",
            ok=bool(only_payload) and FAILURE_MARKER in only_payload,
            detail=(
                f"失败结果里出现过的 (error_code,message) 组合数={len(failure_payloads)}"
                f"（要求恰好 1 个，且含 {FAILURE_MARKER!r}）；"
                f"组合={sorted(failure_payloads)[:2]}"
            ),
        ))

        # ── 事实 2：恰好一次纠正 ────────────────────────────────────────────
        soft_guards = [
            event for event in events
            if event.type == TOOL_FAILURE_GUARD and event.data.get("level") == "soft"
        ]
        hard_guards = [
            event for event in events
            if event.type == TOOL_FAILURE_GUARD and event.data.get("level") == "hard"
        ]
        stuck_replans = [
            event for event in events
            if event.type == GUARD_STUCK and event.data.get("level") == STUCK_LEVEL_REPLAN
        ]
        correctives = [
            event for event in events
            if event.type == USER_MESSAGE
            and event.data.get("injected_by") in ("tool_failure_guard", "stuck_guard")
        ]
        results.append(AssertionResult(
            name="exactly_one_corrective_replan",
            ok=(
                len(soft_guards) + len(stuck_replans) == 1
                and len(correctives) == 1
                and not hard_guards
            ),
            detail=(
                f"tool/failure-guard(soft)={len(soft_guards)}、"
                f"guard/stuck(replan)={len(stuck_replans)}、"
                f"注入纠正消息={len(correctives)}（injected_by="
                f"{[event.data.get('injected_by') for event in correctives]}）、"
                f"tool/failure-guard(hard)={len(hard_guards)}（旧 HARD 形状必须为 0）"
            ),
        ))

        # ── 事实 3：暂停是非终态，且载荷带着可比的快照 ──────────────────────
        pause_ok = (
            pause is not None
            and str(pdata.get("reason")) == REASON_STUCK
            and str(pdata.get("trigger_dimension")) == str(stuck.get("pattern"))
            and list(pdata.get("resume_requirements") or []) == list(STUCK_RESUME_REQUIREMENTS)
            and all(key in stuck for key in STUCK_KEYS)
            and stuck.get("threshold") == 3
            and stuck.get("count") == streak
            and stuck.get("replan_count") == 1
            and isinstance(stuck.get("environment_revision"), str)
            and str(stuck.get("environment_revision")).startswith("sha256:")
            and isinstance(stuck.get("policy_version"), str)
            and str(stuck.get("policy_version")).startswith("sha256:")
            # 逐维值能重算出记下来的那个摘要（`#317` T9 审查 P1 的机械口径）：恢复侧正是
            # 拿它把暂停时那一套策略还原回自己的 amend。两格不同源（只记摘要、还原不回来）
            # 就会退化成"靠本次请求声明了什么去猜"——那正是被审查判 P1 的 fail-open。
            and _inputs_match_the_digest(stuck)
            and isinstance(pdata.get("continuation"), dict)
            and bool(str(pdata["continuation"].get("next_safe_action") or "").strip())
        )
        results.append(AssertionResult(
            name="stuck_pause_is_recorded_with_the_evidence_snapshot",
            ok=pause_ok,
            detail=(
                f"reason={pdata.get('reason')!r} trigger_dimension={pdata.get('trigger_dimension')!r} "
                f"pattern={stuck.get('pattern')!r} threshold={stuck.get('threshold')!r} "
                f"count={stuck.get('count')!r}（轨迹重算串长 {streak}）"
                f"replan_count={stuck.get('replan_count')!r} "
                f"env={str(stuck.get('environment_revision'))[:16]!r} "
                f"policy={str(stuck.get('policy_version'))[:16]!r} "
                f"resume_requirements={pdata.get('resume_requirements')!r}"
            ),
        ))
        stuck_pauses = [
            event for event in events
            if event.type == GUARD_STUCK and event.data.get("level") == STUCK_LEVEL_PAUSED
        ]
        results.append(AssertionResult(
            name="pause_is_not_a_terminal_event",
            ok=(
                pause is not None and len(stuck_pauses) == 1
                and [event.data.get("reason") for event in terminal]
                != ["identical_tool_failure_loop"]
                and not [event for event in events if event.type == RUN_FAILED]
            ),
            detail=(
                f"run/paused={len(pauses)}、guard/stuck(paused)={len(stuck_pauses)}、"
                f"终态事件={[(event.type, event.data.get('reason')) for event in terminal]}"
            ),
        ))
        pause_dangling = dangling_tool_call_ids(
            [event for event in events if cutoff is None or event.seq <= cutoff]
        )
        results.append(AssertionResult(
            name="no_dangling_tool_call_at_the_pause",
            ok=not pause_dangling,
            detail=f"暂停点上的悬空 tool/call={pause_dangling}",
        ))

        # ── 事实 4：四种无依据恢复 + 一条无关变更全部 409、零副作用 ──────────
        refusals = legs.get("refusals", {})
        refused_ok = bool(refusals) and all(item.ok for item in refusals.values())
        results.append(AssertionResult(
            name="every_resume_without_evidence_is_a_409_with_zero_side_effects",
            ok=refused_ok,
            detail="；".join(
                f"{name}: {'拒绝' if item.ok else '未达判据'} — {item.reason[:160]}"
                for name, item in refusals.items()
            ) or "没有任何拒绝读数",
        ))

        # ── 事实 5：有依据的恢复放行、记账、真的继续干活 ────────────────────
        resume_event = resumes[0] if resumes else None
        evidence = (
            resume_event.data.get("resume_evidence")
            if resume_event is not None and isinstance(resume_event.data.get("resume_evidence"), dict)
            else {}
        )
        recorded_env = stuck.get("environment_revision")
        observed_env = evidence.get("environment_revision")
        # 依据成立的机械口径（`run_budget.stuck_resume_evidence` 的 environment_change 臂）：
        # 现算值 ≠ 快照值，且 `recorded` 逐字等于暂停快照那一格——这三项就是"与快照比过"
        # 的全部证据（那条臂**不**写 `pause_seq`，它只在 relevant_steer 臂里有）。
        resume_ok = (
            resume_event is not None and len(started) == 1
            and str(resume_event.run_id or "") == run_id
            and evidence.get("basis") == "environment_change"
            and isinstance(recorded_env, str) and bool(recorded_env)
            and isinstance(observed_env, str) and bool(observed_env)
            and observed_env != recorded_env
            and evidence.get("recorded") == recorded_env
        )
        results.append(AssertionResult(
            name="resume_records_the_accepted_basis",
            ok=resume_ok,
            detail=(
                f"run/resumed={len(resumes)} run/started={len(started)}、"
                f"resume_evidence={evidence}、暂停快照 environment_revision={recorded_env!r}"
                f"（暂停 seq={cutoff}）"
            ),
        ))
        resumed_seq = resume_event.seq if resume_event is not None else None
        admitted = [
            event for event in events
            if event.type in (MODEL_REQUEST, TOOL_CALL)
            and resumed_seq is not None and event.seq is not None and event.seq > resumed_seq
        ]
        results.append(AssertionResult(
            name="resumed_leg_admitted_new_work",
            ok=bool(admitted),
            detail=(
                "恢复后的接纳（model/request + tool/call）="
                f"{[event.type for event in admitted]}"
            ),
        ))
        # 这里**没有**"恢复那条腿读到了那份文件"的断言：它读不读得到取决于"恢复依据的正文
        # 有没有被投进模型上下文"，而生产没有这条投递路径（`send_message(mode="steer")` 要求
        # 有在途 run；运行时只排空内存队列）——把"模型该知道上游就绪"写成断言等于把一条
        # 不存在的产品承诺算进证据。边界、实测两条读数与残余见 ADR-0048 §4 残余 4。
        results.append(AssertionResult(
            name="resumed_leg_ended_safely",
            ok=(
                not [event for event in events if event.type == RUN_INTERRUPTED]
                and not [event for event in events if event.type == RUN_FAILED]
                and (RUN_COMPLETED in [event.type for event in events] or bool(pauses))
            ),
            detail=(
                f"终态={[event.type for event in terminal]}；"
                f"恢复那条腿的事件={[event.type for event in events if resumed_seq is not None and (event.seq or 0) > resumed_seq]}"
            ),
        ))

        # ── 事实 6：重放能重建同样的判定（AC：restart / replay） ─────────────
        replay = self._replay_verdict(
            events=events, pause=pause, run_id=run_id, pattern=str(stuck.get("pattern") or ""),
            count=stuck.get("count"), threshold=stuck.get("threshold"),
        )
        results.append(replay)

        # ── 附加：暂停那一刻的账与轨迹一致（没有"偷偷多打一次"） ─────────────
        if pause is not None:
            facts = request_accounting(
                [event for event in events if cutoff is None or event.seq <= cutoff]
            )
            results.extend(consumed_counter_assertions(
                facts=facts, consumed=pdata.get("consumed"), label="stuck 暂停快照",
            ))
        results.append(AssertionResult(
            name="no_local_fuse_terminal",
            ok="max_steps_exceeded" not in [
                str(event.data.get("reason")) for event in terminal
            ],
            detail=(
                f"terminal reason={[event.data.get('reason') for event in terminal]}；"
                f"projection.reason={projection.get('reason')!r}"
            ),
        ))
        if replayed and events:
            results.append(AssertionResult(
                name="durable_trajectory_is_replayable",
                ok=len(replayed) <= len(events),
                detail=f"store 读回 {len(replayed)} 条 / 服务读 {len(events)} 条",
            ))
        _ = operations
        return results

    def _replay_verdict(
        self, *, events: list[Any], pause: Any, run_id: str, pattern: str,
        count: Any, threshold: Any,
    ) -> AssertionResult:
        """把"暂停前那一段"重放成检测器状态，再喂尾部：判定必须与落盘载荷一致。

        尾部 = 暂停点之前最后一条 `tool/result`（就是那条把计数推到暂停点的事件）。
        `from_events(prefix[:-1])` 重建状态（包含 durable 的纠正事件 ⇒ "恰好一次"的闩），
        `advance(prefix)` 才产生动作。两条硬要求：
        ① 尾部产生**一个** paused 级信号，模式 / 计数 / 阈值与载荷逐项相同；
        ② 尾部**不再**产生 replan 级信号（闩由事件重建，不会被重放抹掉）；
        ③ 同一份事件再喂一次 ⇒ 什么都不产出（游标语义，重放幂等）。
        """
        from agent_harness.agent.guards import (
            STUCK_LEVEL_PAUSED,
            STUCK_LEVEL_REPLAN,
            StuckDetector,
        )

        if pause is None:
            return AssertionResult(
                name="replay_reconstructs_the_same_verdict", ok=False,
                detail="没有暂停事件，重放无从比对",
            )
        prefix: list[Any] = []
        for event in events:
            if event.seq is not None and pause.seq is not None and event.seq >= pause.seq:
                break
            prefix.append(event)
        while prefix and prefix[-1].type != "tool/result":
            prefix.pop()
        if len(prefix) < 2:
            return AssertionResult(
                name="replay_reconstructs_the_same_verdict", ok=False,
                detail=f"暂停前拿不到可重放的尾部（prefix={len(prefix)}）",
            )
        detector = StuckDetector.from_events(prefix[:-1], run_id or None)
        signals = detector.advance(prefix)
        again = detector.advance(prefix)
        paused_signals = [
            signal for signal in signals
            if getattr(signal, "level", None) == STUCK_LEVEL_PAUSED
        ]
        replans = [
            signal for signal in signals
            if getattr(signal, "level", None) == STUCK_LEVEL_REPLAN
        ]
        ok = (
            len(paused_signals) == 1
            and paused_signals[0].pattern == pattern
            and paused_signals[0].count == count
            and paused_signals[0].threshold == threshold
            and not replans
            and not again
        )
        return AssertionResult(
            name="replay_reconstructs_the_same_verdict",
            ok=ok,
            detail=(
                f"重放尾部得到 paused 信号={[(s.pattern, s.count, s.threshold) for s in paused_signals]}"
                f"、replan 信号={len(replans)}（必须为 0）、再次喂同一份事件={len(again)} 条（必须为 0）；"
                f"载荷 pattern={pattern!r} count={count!r} threshold={threshold!r}"
            ),
        )

    @staticmethod
    def _continuation_tail(pause: Any) -> str:
        if pause is None:
            return ""
        continuation = pause.data.get("continuation")
        if not isinstance(continuation, dict):
            return ""
        return json.dumps(continuation, ensure_ascii=False)[:400]


SCENARIO = StuckToolFailurePauseScenario()
