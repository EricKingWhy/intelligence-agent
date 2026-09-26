"""内置场景：真实模型 + 生产工具 + 生产账本 —— durable 工作结清之前**不落**终态（`#316` T8）。

## 它证明什么（同一会话里三次真实执行 + 一条真实未结清 owner）

一个 `session_id`、一本**生产账本**（`AppState` 的 `SqliteOperationLedger`），依次发生：

1. **正向腿**：真实模型用生产 `write` 工具落一个内容固定的产物，然后给出最终回答 ⇒
   `run/completed` 恰好一条，且它出现在**那次调用的用户工作全部落盘之后**：`tool/result`
   在它之前、账本里那次调用的行在它之前已是终态（`SUCCEEDED`）、产物在工作区里真的存在。
   票面 AC 的原文（a real Provider run with a real production tool completes only after
   the durable tool result is present）要的就是这条顺序事实，不是模型的措辞。
2. **真实执行域留下的未结清 owner**：一次生产 `BashTool`（MUTATING）调用挂到工具超时——
   命令**先把副作用落到工作区、再挂住** ⇒ 收尾时"世界状态未知"是事实（不是"其实什么都没
   发生"），账上留 `UNKNOWN` + "副作用未证"标记（`07 §7`）。这条不是模型发的（"让真实模型
   恰好发一条会超时的命令"不是结构保证，`deadline.py` 的同一条理由）。
3. **被挡腿**：同一个真实模型、同样形状的任务（写一个固定内容的文件 + 一句话回答）。
   模型照常干活、照常给出最终回答，但**完成闸门拒绝收口**：这个会话里没有属于它的
   `run/completed`，也没有 `run/failed` / `run/paused`；结果是 `quiescence_blocked` +
   稳定理由，理由点名账本那条未结清行贡献的两类 blocker（谓词 4 未定在途、谓词 5 未证/欠对账）。
   那条 owner 行**逐字段不变**（拒绝不替它做分类，ADR-0047 D4），而本次执行自己的那条调用
   照常结清成 `SUCCEEDED` —— 被拒的是**收口**，不是工作。
4. **结清后重入腿**：按状态机的合法链把 owner 结清（`UNKNOWN` → `NEED_RECONCILE`（分类）→
   `SUCCEEDED`（裁决，裁决内容**覆盖**未证标记）），同一个闸门重新聚合、这次通过 ⇒
   `run/completed` 一条。全程这个会话的终态事件总数 = 2（属于第 1、3 次执行），
   被挡的那次**一条也没有**（票面 AC：no duplicate or contradictory terminal events）。

每一步的闸门读数都在**两条独立路径**上各算一次：产品侧在 `AgentRuntime` 里（第 3、4 条腿
的收口行为本身就是读数），场景侧用同一个纯函数 `collect_quiescence_report` 按 durable 事实
重算（`_gate_readout`）。两条路径给出同一个结论才算对账成立（`#213` 的失效形状是"同一处实现
自己给自己打分"）。

## 为什么逐类独立阻断不在这里做

那需要五类未解工作各跑一次真实执行（五倍真实调用），而**独立性**是纯函数层的性质：闸门的
输入就是三份已落盘的输入，`collect_quiescence_report` 可以只喂一条 blocker 单独驱动
（`tests/agent/test_completion_quiescence.py` 逐类覆盖，票面 Verification 的"one outstanding-work
category at a time"）。本场景补的是另一件事——**这条路径在生产组合下真的在跑**：真实 provider
的决策、生产工具的真实副作用、生产账本的真实行、**部署默认**（不传 `completion_policy`，
`assembly.build_runtime` 同样不传）的完成边界。两者不互相替代。

## 生产性来自哪里

- **Provider**：`ModelConfig.from_settings` → `create_chat_model`（真实客户端，照 smoke / long_task）；
- **工具**：`assembly.BUILTIN_LOCAL_TOOLS` —— 装配层注册进 runtime 的**同一份**元组；执行走
  生产 `ToolExecutor`（`policy=WORKSPACE_WRITE` + auto-approve，与 `assembly.build_runtime`
  的默认分支逐项相同），实现身份落进证据（类路径必须在 `agent_harness.*` 下）；
- **账本**：`AppState(settings).operation_ledger` —— 与 `assembly` 交给 `ToolExecutor` 的是
  同一个类、同一个接线点；本场景的负向证据完全建立在这本账上（`tool/call` 与账本行**逐条**
  对得上，见 `ledger_rows_for_every_call`）；
- **完成边界**：`AgentRuntime(...)` **不传** `completion_policy`；每次执行现建一次装配
  （与生产"每次 launch 建一次 runtime"同形）；
- **会话**：`JsonlSessionStore` + `Session.start`（与 `build_runtime` 内部绑 worktree 的调用
  形状同族，照 long_task / deadline）。

## 这条运行**不得**被当成"模型表现"的证据

判据里没有一条依赖模型说了什么：三条腿的任务都只有"写一个固定内容的文件 + 一句话回答"，
blocker 全部来自账本行与事件流。模型若偏离任务（多写文件 / 不写文件 / 不收尾），产物内容、
调用计数与"有没有终态"这些断言会如实判红，不洗成 PASS。

## 一次性边界

运行时产物（会话轨迹 / `harness.db` / workspaces 登记）全部落在一次尝试的临时根目录内
（`settings.workspace_dir` 重定向到 `ctx.session_root.parent`，见 `_scenario_settings`），
跑完由 runner 的 `workspace.teardown()` 销毁并核实。本场景**不**装 capability 栈
（不 `get_wiring`）⇒ 收口之后没有后写者，读盘稳定（`durable_replay_matches_live` 证明它）。
"""

from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from typing import Any

from evaluation.assertions import dangling_tool_call_ids
from evaluation.live_gate.registry import AttemptOutcome, ScenarioContext
from evaluation.live_gate.schema import AssertionResult

SCENARIO_ID = "completion-quiescence-gate"
SCENARIO_VERSION = 1

#: 三条腿各自的产物：路径与内容都是**常量** ⇒ 断言才能是"相等"而不是"看起来像"。
POSITIVE_ARTIFACT = "durable.txt"
POSITIVE_CONTENT = "completed-after-durable-tool-result"
REFUSED_ARTIFACT = "refused-leg.txt"
REFUSED_CONTENT = "work-done-closeout-refused"
RESUMED_ARTIFACT = "resumed-leg.txt"
RESUMED_CONTENT = "settled-then-completed"

#: 真实执行域留下的那条未结清 owner（本场景的负向道具，见模块 docstring 第 2 条）。
OWNER_CALL_ID = "call-unproven-owner"
OWNER_TOOL_NAME = "bash"
UNCERTAIN_SCRIPT = "uncertain.py"
UNCERTAIN_SENTINEL = "uncertain.txt"
UNCERTAIN_SENTINEL_LINE = "touched"
#: 生产 bash 超时的部署默认是 60s（`Settings.bash_timeout_seconds`）；本场景把它调小到这个值，
#: 让一次真实的 MUTATING 超时落在可接受的墙钟内。**类与接线点逐字不变**（照 deadline.py）。
UNCERTAIN_TOOL_TIMEOUT_SECONDS = 5.0
#: `uncertain.py` 挂着的时长（> 工具超时）。它短于一次尝试的剩余墙钟 ⇒ 即使沙箱没能立刻杀掉
#: 进程树，进程也会在自己的时间里退出，不给 teardown 留一个占着目录的活进程。
UNCERTAIN_SLEEP_SECONDS = 8.0

#: 真实模型调用的单次超时（秒）：本场景三次执行都是"两次决策"的短任务，给足余量。
REQUEST_TIMEOUT = 180.0
#: 单条腿（一次 run）的墙钟上限（秒）。runner 的 `ATTEMPT_TIMEOUT` 是 900s，本场景三条腿
#: 各 ≈ 两次决策，远低于它；到点判 FAIL（不是"跳过"），并在错误里**指名是哪条腿**。
LEG_TIMEOUT = 300.0

_UNCERTAIN_SCRIPT_SOURCE = f'''"""Live Gate 完成闸门场景（#316）：副作用先落地，然后挂到工具超时。

它被生产 `BashTool` 调用（MUTATING）⇒ 工具超时收尾时，账上留 UNKNOWN + "副作用未证"：
`{UNCERTAIN_SENTINEL}` 已经写出来了，谁也证明不了它"没发生"（`07 §7`）。
"""

import pathlib
import time

pathlib.Path("{UNCERTAIN_SENTINEL}").write_text("{UNCERTAIN_SENTINEL_LINE}", encoding="utf-8")
time.sleep({UNCERTAIN_SLEEP_SECONDS})
print("finished")
'''


def _task(artifact: str, content: str) -> str:
    """三条腿共用的任务形状：只有产物名与内容不同（否则 blocker 的归因就不干净了）。

    "不要运行任何命令"是刻意的：工具面收窄到生产 `write` 一个（MUTATING），
    账本上因此只有"模型自己的那次调用"这一种新行，未结清 owner 的归因不会被搅混。
    """
    return (
        "这是一次很短的工作，只做两件事：\n"
        f"1) 用 write 工具创建 {artifact}，内容恰好是 {content}"
        "（不要换行、不要引号、不要多余字符）；\n"
        "2) 然后用一句话回答我：文件已经写好。\n"
        "不要探索其它文件，不要写别的文件，不要运行任何命令。"
    )


# ── 小工具 ────────────────────────────────────────────────────────────────


def _safe_read(sandbox: Any, name: str) -> str:
    """读工作区文件；读不到返回空串（**读失败也是判据的一种取值**，不是异常）。"""
    try:
        return sandbox.read_text(name)
    except Exception:  # noqa: BLE001 - 产物缺失 → 后续断言自然不过
        return ""


def _sentinel_count(sandbox: Any) -> int:
    """副作用计数：`uncertain.py` 每成功跑一次追加一行（重跑会多一行）。"""
    return _safe_read(sandbox, UNCERTAIN_SENTINEL).count(UNCERTAIN_SENTINEL_LINE)


def _failure_text(error: BaseException) -> str:
    """异常 → **可定位**的一行：类型 + 消息 + 最后几个本仓库栈帧 + 出错那行的源码。

    Live Gate 的证据常常是远程运行留下的唯一产物，`类型: 消息` 不够用——本仓库的实测：
    真实运行三连 FAIL 只报 `TypeError: 'NoneType' object is not iterable`，看不出是哪一行，
    只能再烧一次真实运行去猜（一次 ≈ 数分钟 + 真实调用）。第三方帧对定位没有帮助，
    还被排除掉：它们只会把环境路径写进证据。形状与 `deadline.py` 的同名取证工具一致。
    """
    frames = [
        frame for frame in traceback.extract_tb(error.__traceback__)
        if "site-packages" not in frame.filename
    ]
    if not frames:
        return f"{type(error).__name__}: {error}"
    chain = " <- ".join(f"{Path(frame.filename).name}:{frame.lineno}" for frame in frames[-3:])
    return f"{type(error).__name__}: {error} @ {chain} 〔{(frames[-1].line or '').strip()}〕"


def _scenario_settings(ctx: ScenarioContext) -> Any:
    """把一次性根目录当成本次运行的运行时目录（会话轨迹 / SQLite / workspace 登记）。

    两条理由，都是取证纪律：轨迹必须落在 `ctx.session_root` 下（runner 按
    `session_root/<session_id>/events.jsonl` 归档）；`Settings` 的相对默认值按**进程 CWD**
    解析（那就是开发仓库），而 Live Gate 的 `repo_unchanged` 要求跑前跑后仓库指纹一致。
    **其余字段逐字沿用部署配置**（含 CAPABILITIES / 模型链 / 审批超时）。
    """
    root = ctx.session_root.parent
    return ctx.settings.model_copy(update={
        "workspace_dir": str(root),
        "artifact_dir": str(root / "artifacts"),
    })


def _row_snapshot(row: Any) -> dict[str, Any] | None:
    """账本行的**全字段**快照（`model_dump(mode="json")`）——用于"逐字段不变"的断言。

    只比"状态没变"是不够的：拒绝顺手改一格 `reconcile_meta` 也是副作用（ADR-0047 D4）。
    """
    return None if row is None else row.model_dump(mode="json")


async def _gate_readout(store: Any, ledger: Any, session_id: str) -> dict[str, Any]:
    """**独立重算**一次完成闸门的输入（不经过 Runtime）：durable 事件 + 账本行。

    三个读数各有分工：`kinds` 是闸门会看到的全量 blocker；`kinds_from_events_only` 是
    "把账本摘掉之后还剩什么"——它为空才能证明 blocker 来自**账本行**（而不是事件流里
    某个别的东西），归因不靠人读字符串；`reason` 是 product 侧同一份纯函数给出的稳定串。
    """
    from agent_harness.agent.completion import collect_quiescence_report

    events = store.read_events(session_id)
    operations = await ledger.list_for_session(session_id)
    report = collect_quiescence_report(events=events, operations=operations, new_tool_calls=False)
    events_only = collect_quiescence_report(events=events, operations=(), new_tool_calls=False)
    return {
        "kinds": list(report.kinds),
        "reason": report.refusal_reason(),
        "quiescent": report.quiescent,
        "kinds_from_events_only": list(events_only.kinds),
    }


def _production_registry(ctx: ScenarioContext, settings: Any) -> tuple[Any, dict[str, str]]:
    """生产工具集 + **实现身份**表（`#314` T6 的同一判据：类路径必须在 `agent_harness.*` 下）。"""
    from agent_harness.assembly import BUILTIN_LOCAL_TOOLS
    from agent_harness.tooling import ToolRegistry

    registry = ToolRegistry()
    for tool_cls in BUILTIN_LOCAL_TOOLS:
        # 与 `assembly.build_runtime` 的接线点同一个参数名（超时值来自部署配置）。
        kwargs: dict[str, Any] = (
            {"timeout_seconds": settings.bash_timeout_seconds}
            if tool_cls.__name__ == "BashTool" else {}
        )
        registry.register(tool_cls(ctx.sandbox, **kwargs))
    impls = {
        tool.name: f"{type(tool).__module__}.{type(tool).__qualname__}"
        for tool in registry.list()
    }
    return registry, impls


async def _run_real_run(
    *, registry: Any, ledger: Any, settings: Any, session: Any, task: str,
) -> Any:
    """一次真实 launch 形状的执行：真实 provider + 生产工具 + 生产账本。

    `AgentRuntime(...)` **不传** `completion_policy`：部署默认就是这道闸门的默认通用策略
    （`assembly.build_runtime` 同样不传）——本场景证明的是**运行时默认路径**，
    不是"另外配了一个策略之后"的路径。每次执行现建一次装配（与生产每次 launch 建一次同形）。
    """
    from agent_harness.agent import AgentRuntime
    from agent_harness.model.config import ModelConfig
    from agent_harness.model.provider import create_chat_model
    from agent_harness.tooling import PermissionPolicy, ToolExecutor
    from agent_harness.tooling.approval import ApprovalResponse

    config = ModelConfig.from_settings(settings)
    model = create_chat_model(config, request_timeout=REQUEST_TIMEOUT)

    async def auto_approve(_request: Any) -> ApprovalResponse:
        # 生产默认（`assembly.py`：未声明 auto_approve 且无交互回调 ⇒ 自动批准）。
        return ApprovalResponse(approved=True, reason="live-gate-auto-approve")

    executor = ToolExecutor(
        registry, policy=PermissionPolicy.WORKSPACE_WRITE,
        approval_callback=auto_approve, operation_ledger=ledger,
    )
    runtime = AgentRuntime(model, registry, executor, primary_model_name=config.model_name)
    async with asyncio.timeout(LEG_TIMEOUT):
        return await runtime.run(session, task)


async def _produce_unquiescent_owner(ctx: ScenarioContext, *, ledger: Any, session: Any) -> dict[str, Any]:
    """用**生产执行域**产出一条真实的未结清 owner：一次 MUTATING 调用挂到工具超时。

    接线点与 `assembly` 逐项相同：生产 `BashTool`（同一个类、同一个 `timeout_seconds`
    参数名）+ 生产 `ToolExecutor` + 生产 `OperationLedger`。命令先落副作用再挂住，
    所以收尾时的 `UNKNOWN` 是**真的不知道**（不是"其实什么都没发生"）。

    为什么不借模型的手造它：见模块 docstring 第 2 条（"让真实模型恰好发一条会超时的命令"
    不是结构保证）。本调用**不落 SessionEvent** —— `ToolExecutor.execute` 本身不写事件，
    生产链路里事件由 Runtime 在它外侧写；所以这条 owner 在 durable 面上是**账本事实**，
    闸门的谓词 4/5 读的正是它（谓词 1 的悬空调用是**另一类**事实，由崩溃恢复链结清）。
    """
    from agent_harness.storage import OperationContext
    from agent_harness.tooling import (
        ApprovalResponse,
        PermissionPolicy,
        ToolCall,
        ToolExecutor,
        ToolRegistry,
    )
    from agent_harness.tools import BashTool

    registry = ToolRegistry()
    registry.register(BashTool(ctx.sandbox, timeout_seconds=UNCERTAIN_TOOL_TIMEOUT_SECONDS))

    async def _auto_approve(_request: Any) -> ApprovalResponse:
        return ApprovalResponse(approved=True, reason="live-gate-auto-approve")

    executor = ToolExecutor(
        registry, policy=PermissionPolicy.WORKSPACE_WRITE,
        approval_callback=_auto_approve, operation_ledger=ledger,
    )
    execution = await executor.execute(
        ToolCall(
            id=OWNER_CALL_ID, name=OWNER_TOOL_NAME,
            args={"command": f"python {UNCERTAIN_SCRIPT}"},
        ),
        operation_context=OperationContext(
            session_id=session.session_id, run_id=None, agent_id="default",
        ),
        session=session,
    )
    row = await ledger.get(session.session_id, OWNER_CALL_ID)
    return {
        "execution": execution,
        "row_before_block": _row_snapshot(row),
        "sentinel_after_call": _sentinel_count(ctx.sandbox),
    }


class CompletionQuiescenceGateScenario:
    """真实模型 + 生产工具 + 生产账本：完成只在 durable 工作结清之后发生，未结清则拒绝收口。"""

    id = SCENARIO_ID
    version = SCENARIO_VERSION
    description = (
        "同一会话三次真实执行（完成 → 因一条真实未结清 owner 被拒收口 → 结清后重入通过）；"
        "断言终态只在 durable 工具结果之后出现、拒绝零副作用、全程不重复矛盾终态"
    )

    async def prepare(self, ctx: ScenarioContext) -> list[str]:
        """seed 未证副作用脚本 + 前置自查。返回未满足的前置（非空 ⇒ BLOCKED，不发 run）。"""
        probe = ctx.sandbox.exec("python --version")
        if probe.exit_code != 0:
            return [
                (
                    f"工作区里没有可用的 python（rc={probe.exit_code}）——本场景要用一次真实的"
                    "MUTATING 超时产出一条未结清 owner（先落副作用、再挂着），"
                    "缺它不构成 FAIL 而是环境不具备"
                ),
            ]
        ctx.sandbox.write_text(UNCERTAIN_SCRIPT, _UNCERTAIN_SCRIPT_SOURCE)
        return []

    async def run(self, ctx: ScenarioContext) -> AttemptOutcome:
        """跑一次完整流程。**异常一律转 `ok=False`**（runner 兜底捕获是第二道）。"""
        from agent_harness.session import JsonlSessionStore, Session
        from agent_harness.web.app import AppState

        settings = _scenario_settings(ctx)
        state = AppState(settings)
        store = JsonlSessionStore(root=ctx.session_root)
        legs: dict[str, Any] = {}
        try:
            # 显式初始化恢复 Store（DDL + transport 表）。`SessionService` 自己也会惰性 ensure，
            # 但本场景**直接**读 `state.operation_ledger` —— 不把"某条服务路径恰好先 ensure 过"
            # 当成前提（照 deadline.py）。
            await state.ensure_stores()
            ledger = state.operation_ledger
            registry, tool_impls = _production_registry(ctx, settings)
            session = Session.start(
                store, session_id=ctx.session_id, cwd=ctx.sandbox.workspace_root,
            )

            # ── 第 1 条腿：静止 ⇒ 默认策略接受 ⇒ 收口 ──────────────────────────
            legs["positive"] = await _run_real_run(
                registry=registry, ledger=ledger, settings=settings, session=session,
                task=_task(POSITIVE_ARTIFACT, POSITIVE_CONTENT),
            )

            # ── 真实执行域留下一条未结清 owner（账本事实，见 `_produce_unquiescent_owner`）──
            legs["owner"] = await _produce_unquiescent_owner(ctx, ledger=ledger, session=session)

            # ── 第 2 条腿：同一会话、同一个真实模型 ⇒ 完成闸门拒绝收口 ──────────
            legs["refused"] = await _run_real_run(
                registry=registry, ledger=ledger, settings=settings, session=session,
                task=_task(REFUSED_ARTIFACT, REFUSED_CONTENT),
            )
            legs["owner_after_block"] = _row_snapshot(
                await ledger.get(ctx.session_id, OWNER_CALL_ID)
            )
            legs["readout_blocked"] = await _gate_readout(store, ledger, ctx.session_id)

            # ── 结清：分类（UNKNOWN → NEED_RECONCILE）→ 裁决（→ SUCCEEDED）──────
            # 每一档都留一次闸门读数：`03 §5` 的两步对账在**同一个闸门**上的投影不同。
            from agent_harness.storage import OperationState

            await ledger.update_state(
                ctx.session_id, OWNER_CALL_ID, OperationState.NEED_RECONCILE,
            )
            legs["readout_classified"] = await _gate_readout(store, ledger, ctx.session_id)
            # `reconcile_meta` 必须写**裁决内容**：`update_state` 对它是
            # `COALESCE(?, reconcile_meta)`，传 None 会保留原来那格未证标记（谓词 5 继续挡）。
            # 裁决落地即覆盖 = 疑问解除（ADR-0046 D4）。
            await ledger.update_state(
                ctx.session_id, OWNER_CALL_ID, OperationState.SUCCEEDED,
                result_json=json.dumps({"reconciled": "live-gate"}, ensure_ascii=False),
                reconcile_meta=json.dumps(
                    {"verdict": "side_effect_confirmed", "note": "live-gate 裁决"},
                    ensure_ascii=False,
                ),
            )
            legs["readout_settled"] = await _gate_readout(store, ledger, ctx.session_id)

            # ── 第 3 条腿：结清后重入同一闸门 ⇒ 通过 ──────────────────────────
            legs["resumed"] = await _run_real_run(
                registry=registry, ledger=ledger, settings=settings, session=session,
                task=_task(RESUMED_ARTIFACT, RESUMED_CONTENT),
            )
        except Exception as error:  # noqa: BLE001 - 失败要如实记录（runner 统一脱敏）
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id, error=_failure_text(error),
            )
        finally:
            # 关闭 AppState（本场景没装 capability 栈，`shutdown()` 幂等且不追加 durable 事实）。
            await state.shutdown()

        # 终态读盘必须在 `shutdown()` **之后**：可选能力的收尾会**追加 durable 事实**
        # （`memory/degraded` 就落在 `run/completed` 之后），早读会让判定所依据的事件
        # 比 runner 复制的那份轨迹少一行（`pause_resume.py` 记录了那次实测）。
        try:
            events = store.read_events(ctx.session_id)
            replayed = JsonlSessionStore(root=ctx.session_root).read_events(ctx.session_id)
            operations = await state.operation_ledger.list_for_session(ctx.session_id)
            artifacts = {
                name: _safe_read(ctx.sandbox, name)
                for name in (POSITIVE_ARTIFACT, REFUSED_ARTIFACT, RESUMED_ARTIFACT)
            }
            legs["sentinel_after_all"] = _sentinel_count(ctx.sandbox)
        except Exception as error:  # noqa: BLE001 - 同上的如实记录
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id, error=_failure_text(error),
            )

        tool_calls = [
            str(event.data.get("tool_name"))
            for event in events
            if event.type == "tool/call"
        ]
        run_ids = self._run_ids(events)
        assertions = self._assertions(
            ctx=ctx, legs=legs, events=events, replayed=replayed, operations=operations,
            artifacts=artifacts, tool_impls=tool_impls,
        )
        return AttemptOutcome(
            ok=all(item.ok for item in assertions),
            session_id=ctx.session_id,
            # run id 取**最后一次执行**：它是这个会话当前的活跃 run（前两次的收口形状
            # 由断言逐条钉住，不靠这一个字段表达）。
            run_id=run_ids[-1] if run_ids else "",
            # 终态读 durable 事实：最后一条腿的形状（`completed` / `quiescence_blocked`…）。
            run_status=str(getattr(legs.get("resumed"), "status", "") or ""),
            steps=sum(1 for event in events if event.type == "model/completed"),
            tool_calls=tool_calls,
            assertions=assertions,
            event_count=len(events),
            output_tail=str(getattr(legs.get("resumed"), "final_text", "") or "")[-2000:],
        )

    # ── 断言（机械可检，不是 LLM judge）────────────────────────────────

    @staticmethod
    def _run_ids(events: list[Any]) -> list[str]:
        """`run/started` 的出现次序 = 本会话的三次执行（`run_id` 由 finalizer 生成）。"""
        return [
            str(event.run_id or "")
            for event in events
            if event.type == "run/started"
        ]

    def _assertions(
        self, *, ctx: ScenarioContext, legs: dict[str, Any], events: list[Any],
        replayed: list[Any], operations: list[Any], artifacts: dict[str, str],
        tool_impls: dict[str, str],
    ) -> list[AssertionResult]:
        """结构性判据（`evaluation/assertions.py` 那类代码判断，不是 LLM judge）。"""
        from agent_harness.agent.types import (
            STATUS_COMPLETED,
            STATUS_QUIESCENCE_BLOCKED,
        )
        from agent_harness.session.event import (
            MODEL_COMPLETED,
            OPERATION_RECONCILE_REQUIRED,
            RUN_COMPLETED,
            RUN_FAILED,
            RUN_INTERRUPTED,
            RUN_PAUSED,
            RUN_STARTED,
            TOOL_CALL,
            TOOL_RESULT,
        )
        from agent_harness.storage import (
            Operation,
            OperationState,
            has_unproven_side_effect,
            needs_reconcile,
        )
        from agent_harness.tooling import ErrorCode

        terminal_types = (RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED)
        run_ids = self._run_ids(events)
        started_events = [event for event in events if event.type == RUN_STARTED]
        terminals = [event for event in events if event.type in terminal_types]
        terminal_runs = [str(event.run_id or "") for event in terminals]
        completed_runs = [
            str(event.run_id or "") for event in terminals if event.type == RUN_COMPLETED
        ]
        rows = {operation.tool_call_id: operation for operation in operations}
        owner = rows.get(OWNER_CALL_ID)
        owner_before = legs["owner"].get("row_before_block")
        # owner 的「产出形状」判据读的是**那次真实执行之后**的快照（还原成领域对象），
        # 不是会话最终那行：本场景后面按合法链把它裁决掉，最终行是 SUCCEEDED —— 拿最终行
        # 判「它当时是未结清的」会恒红（离线端到端用例抓到过这一处）。
        owner_produced = (
            Operation.model_validate(owner_before) if owner_before else None
        )
        owner_after_block = legs.get("owner_after_block")
        owner_execution = legs["owner"]["execution"]
        owner_result = getattr(owner_execution, "result", None)
        dangling = dangling_tool_call_ids(events)

        def _per_run(run_id: str) -> dict[str, Any]:
            """一次执行的顺序事实：最后一条 `tool/result` 与终态各自的 seq。"""
            results = [
                event.seq for event in events
                if event.type == TOOL_RESULT and str(event.run_id or "") == run_id
            ]
            ended = [
                event.seq for event in events
                if event.type in terminal_types and str(event.run_id or "") == run_id
            ]
            return {
                "last_result": max(results) if results else None,
                "terminal": min(ended) if ended else None,
                "results": len(results),
                "ended": len(ended),
            }

        positive = _per_run(run_ids[0]) if len(run_ids) >= 1 else {}
        refused_run_id = run_ids[1] if len(run_ids) >= 2 else ""
        refused = _per_run(refused_run_id) if refused_run_id else {}
        resumed = _per_run(run_ids[2]) if len(run_ids) >= 3 else {}
        refused_status = str(getattr(legs.get("refused"), "status", "") or "")
        refused_reason = str(getattr(legs.get("refused"), "reason", "") or "")
        resumed_status = str(getattr(legs.get("resumed"), "status", "") or "")
        readout_blocked = legs.get("readout_blocked") or {}
        readout_classified = legs.get("readout_classified") or {}
        readout_settled = legs.get("readout_settled") or {}

        call_ids = {
            str(event.data.get("tool_call_id") or "")
            for event in events
            if event.type == TOOL_CALL
        }
        observed_tools = sorted({
            str(event.data.get("tool_name") or "")
            for event in events
            if event.type == TOOL_CALL
        })
        impl_text = ", ".join(f"{name}→{tool_impls.get(name) or '未注册'}" for name in observed_tools)
        owner_impl = tool_impls.get(OWNER_TOOL_NAME, "")

        return [
            # ── 第 1 条腿：完成有一条 durable 前置事实链 ─────────────────────
            AssertionResult(
                name="positive_leg_completed_after_durable_tool_result",
                ok=(
                    str(getattr(legs.get("positive"), "status", "")) == STATUS_COMPLETED
                    and len(run_ids) >= 1
                    and positive.get("terminal") is not None
                    and positive.get("last_result") is not None
                    and positive["last_result"] < positive["terminal"]
                    and positive["ended"] == 1
                ),
                detail=(
                    f"第 1 次执行 status={getattr(legs.get('positive'), 'status', None)}；"
                    f"最后一条 tool/result 的 seq={positive.get('last_result')} "
                    f"< run/completed 的 seq={positive.get('terminal')}"
                    "（完成之前那次调用的用户工作已经落盘）；"
                    f"该 run 的终态事件数={positive.get('ended')}（恰好一条）"
                ),
            ),
            AssertionResult(
                name="positive_leg_artifact_on_disk",
                ok=artifacts.get(POSITIVE_ARTIFACT, "") == POSITIVE_CONTENT,
                detail=(
                    f"{POSITIVE_ARTIFACT} 内容比对（len={len(artifacts.get(POSITIVE_ARTIFACT, ''))}）"
                    "——完成不是模型的声称：工具的真实副作用在工作区里"
                ),
            ),
            AssertionResult(
                name="ledger_rows_for_every_call",
                ok=(
                    bool(call_ids)
                    and call_ids <= set(rows)
                    and all(
                        rows[tool_call_id].state
                        in (OperationState.SUCCEEDED, OperationState.FAILED,
                            OperationState.CANCELLED)
                        for tool_call_id in call_ids
                        if tool_call_id != OWNER_CALL_ID
                    )
                ),
                detail=(
                    f"事件的 tool/call={len(call_ids)} 条，账本里逐条有行="
                    f"{sorted(call_ids - set(rows)) or '无缺行'}；"
                    "每条都不是未结清状态（未结清的那条是下面单列的 owner）"
                    "—— 这证明执行器真的带着生产账本在跑，不是「账本缺席所以谓词空真」"
                ),
            ),
            # ── owner：真实执行域产出的一条未结清行 ─────────────────────────
            AssertionResult(
                name="unquiescent_owner_produced_by_production_execution",
                ok=(
                    owner_produced is not None
                    and owner_produced.state is OperationState.UNKNOWN
                    and has_unproven_side_effect(owner_produced)
                    and needs_reconcile(owner_produced)
                    and str(owner_produced.tool_name) == OWNER_TOOL_NAME
                    and owner_result is not None
                    and owner_result.ok is False
                    and owner_result.error_code is ErrorCode.TIMEOUT
                    and legs["owner"].get("sentinel_after_call") == 1
                    and owner_impl.startswith("agent_harness.")
                ),
                detail=(
                    f"owner 行产出时 state={getattr(owner_produced, 'state', None)} "
                    f"副作用未证="
                    f"{has_unproven_side_effect(owner_produced) if owner_produced else None} "
                    f"欠对账={needs_reconcile(owner_produced) if owner_produced else None}；"
                    f"生产 {owner_impl or OWNER_TOOL_NAME} 的返回 error_code="
                    f"{getattr(owner_result, 'error_code', None)}；"
                    f"副作用计数={legs['owner'].get('sentinel_after_call')}"
                    "（副作用已落地 ⇒ 真的证不出结论，不是「其实没发生」）"
                ),
            ),
            # ── 第 2 条腿：未静止 ⇒ 拒绝收口，且拒绝零副作用 ─────────────────
            AssertionResult(
                name="refused_leg_blocked_with_stable_reason",
                ok=(
                    refused_status == STATUS_QUIESCENCE_BLOCKED
                    and refused_reason.startswith("quiescence_blocked:")
                    and "unsettled_operation" in refused_reason
                    and "pending_reconcile" in refused_reason
                    and refused.get("ended") == 0
                    and not [event for event in events if event.type == RUN_PAUSED]
                    and not [
                        event for event in events
                        if event.type == OPERATION_RECONCILE_REQUIRED
                    ]
                ),
                detail=(
                    f"第 2 次执行 status={refused_status!r} reason={refused_reason!r}"
                    "（稳定理由点名账本那条行的两类 blocker）；"
                    f"该 run 的终态事件数={refused.get('ended')}（必须是 0）；"
                    f"run/paused 出现次数="
                    f"{len([e for e in events if e.type == RUN_PAUSED])}"
                    "（完成闸门不用暂停表达未静止：`03 §5` 的暂停只有预算/deadline/stuck 三类）；"
                    f"operation/reconcile-required 出现次数="
                    f"{len([e for e in events if e.type == OPERATION_RECONCILE_REQUIRED])}"
                    "（拒绝不顺手做对账分类）"
                ),
            ),
            AssertionResult(
                name="refusal_left_the_owner_untouched",
                ok=(
                    owner_before is not None
                    and owner_after_block == owner_before
                    and owner_produced is not None
                    and has_unproven_side_effect(owner_produced)
                ),
                detail=(
                    "owner 行在**被拒那一刻**的全字段快照与拒绝前逐字段不变="
                    f"{owner_after_block == owner_before}"
                    "（拒绝不替它做分类、不改 reconcile_meta）；"
                    "被拒的是收口不是工作：本场景另外断言同一次执行自己的 write 调用已结清"
                ),
            ),
            AssertionResult(
                name="refused_leg_work_still_settled",
                ok=(
                    any(
                        str(event.run_id or "") == refused_run_id
                        for event in events if event.type == TOOL_RESULT
                    )
                    and artifacts.get(REFUSED_ARTIFACT, "") == REFUSED_CONTENT
                    and any(
                        str(event.run_id or "") == refused_run_id
                        for event in events if event.type == MODEL_COMPLETED
                    )
                ),
                detail=(
                    f"被挡那次执行仍然落了自己的 tool/result（{REFUSED_ARTIFACT} 内容比对="
                    f"{artifacts.get(REFUSED_ARTIFACT, '') == REFUSED_CONTENT}）"
                    "—— 模型真的干完了活；只是这次 run 的收口被拒绝"
                ),
            ),
            # ── 独立重算：三条读数各自成立 ──────────────────────────────────
            AssertionResult(
                name="gate_readout_blocked_by_the_ledger_row",
                ok=(
                    readout_blocked.get("kinds")
                    == ["unsettled_operation", "pending_reconcile"]
                    and readout_blocked.get("kinds_from_events_only") == []
                    and readout_blocked.get("reason") == refused_reason
                ),
                detail=(
                    f"独立重算（不经过 Runtime）：blocker kinds={readout_blocked.get('kinds')}；"
                    f"摘掉账本后仅事件可见的 kinds="
                    f"{readout_blocked.get('kinds_from_events_only')}（必须是空 ⇒ 归因干净）；"
                    f"纯函数给的理由与产品给的理由一致="
                    f"{readout_blocked.get('reason') == refused_reason}"
                ),
            ),
            AssertionResult(
                name="gate_readout_follows_the_reconcile_chain",
                ok=(
                    readout_classified.get("kinds") == ["pending_reconcile"]
                    and readout_settled.get("quiescent") is True
                    and readout_settled.get("kinds") == []
                ),
                detail=(
                    f"分类（UNKNOWN→NEED_RECONCILE）后 kinds={readout_classified.get('kinds')}"
                    "（只剩谓词 5：行已进对账流程，不再是「未定在途」）；"
                    f"裁决（→SUCCEEDED，裁决内容覆盖未证标记）后 quiescent="
                    f"{readout_settled.get('quiescent')} kinds={readout_settled.get('kinds')}"
                ),
            ),
            AssertionResult(
                name="owner_row_settles_only_via_the_reconcile_chain",
                ok=(
                    owner is not None
                    and owner.state is OperationState.SUCCEEDED
                    and not has_unproven_side_effect(owner)
                    and not needs_reconcile(owner)
                    and "side_effect_confirmed" in (owner.reconcile_meta or "")
                ),
                detail=(
                    f"会话收尾时 owner 行 state={getattr(owner, 'state', None)} "
                    f"副作用未证={has_unproven_side_effect(owner) if owner else None} "
                    f"欠对账={needs_reconcile(owner) if owner else None}"
                    "（它由「产出即未结清」变成结清，只经由 classify→adjudicate 两步；"
                    "这是第 3 条腿得以通过的前提，也说明裁决内容必须覆盖未证标记 —— "
                    "`update_state` 对 reconcile_meta 是 COALESCE，传 None 会留下那格旧值）"
                ),
            ),
            # ── 第 3 条腿：结清后重入同一闸门 ───────────────────────────────
            AssertionResult(
                name="settled_work_reenters_the_gate_and_completes",
                ok=(
                    resumed_status == STATUS_COMPLETED
                    and resumed.get("terminal") is not None
                    and resumed.get("last_result") is not None
                    and resumed["last_result"] < resumed["terminal"]
                    and resumed["ended"] == 1
                    and artifacts.get(RESUMED_ARTIFACT, "") == RESUMED_CONTENT
                ),
                detail=(
                    f"第 3 次执行 status={resumed_status!r}；"
                    f"最后一条 tool/result 的 seq={resumed.get('last_result')} "
                    f"< run/completed 的 seq={resumed.get('terminal')}；"
                    f"该 run 的终态事件数={resumed.get('ended')}"
                ),
            ),
            AssertionResult(
                name="one_terminal_per_completed_run_no_duplicates",
                ok=(
                    len(run_ids) == 3
                    and len(started_events) == 3
                    and len(terminals) == 2
                    and completed_runs == [run_ids[0], run_ids[2]]
                ),
                detail=(
                    f"run/started={len(started_events)}（三次执行）"
                    f"终态事件={len(terminals)}（{terminal_runs}）"
                    "：被挡的那次（run_id="
                    f"{refused_run_id or '缺失'}）一条终态也没有 —— 不重复、不矛盾"
                ),
            ),
            # ── 生产实现身份 / 取证卫生 ─────────────────────────────────────
            AssertionResult(
                name="production_tools_used",
                ok=bool(observed_tools) and all(
                    tool_impls.get(name, "").startswith("agent_harness.") for name in observed_tools
                ),
                detail=f"实现={impl_text}（判据：类路径落在产品核心 `agent_harness.*`）",
            ),
            AssertionResult(
                name="no_dangling_tool_calls", ok=not dangling,
                detail=f"悬空 call={dangling}",
            ),
            AssertionResult(
                name="durable_replay_matches_live",
                ok=(
                    [(event.seq, event.type) for event in events]
                    == [(event.seq, event.type) for event in replayed]
                ),
                detail=(
                    f"独立复读（新 store 实例）{len(replayed)} 条事件与首读逐条同序；"
                    "同序 = 轨迹在判定时已静止，证据记的事件数就是 runner 将复制的那份"
                    "（不同序 ⇒ 读盘后仍被追加，属取证缺陷）"
                ),
            ),
            AssertionResult(
                name="owner_side_effect_happened_exactly_once",
                ok=legs.get("sentinel_after_all") == 1,
                detail=(
                    f"副作用计数={legs.get('sentinel_after_all')}"
                    "（整个会话只有那一次真实的 MUTATING 调用落过副作用：没有被重跑）"
                ),
            ),
            AssertionResult(
                name="session_identity_present",
                ok=bool(ctx.session_id) and len(run_ids) == 3 and bool(run_ids[-1]),
                detail=(
                    f"session_id={'有' if ctx.session_id else '无'} "
                    f"三次执行各自的 run_id={'齐' if all(run_ids) else run_ids}"
                ),
            ),
        ]


SCENARIO = CompletionQuiescenceGateScenario()
