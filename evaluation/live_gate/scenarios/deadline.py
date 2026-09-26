"""内置场景：绝对 deadline 到点 ⇒ 安全暂停 ⇒ 换新时刻同 run 续跑（`#315` T7 的核心验收）。

## 它证明什么（四条结构事实）

1. **到点是真实挂钟的结果，不是脚本安排的**：run 的 `budget.run.deadline_at` 是一个
   **绝对时刻**（`now + DEADLINE_SECONDS`），判定发生在每个稳定边界（准入之前），
   依据是运行时读到的挂钟（`now >= deadline_at`）。任务链**结构上跑不完**
   （见下"信息屏障"）⇒ 只要模型真的在干活，到点必然在一次边界上被读到。暂停快照
   回带的 `limits.run.deadline_at` 与启动时给的那个时刻逐字相同（不是重算出来的）。
2. **到点之后不再接纳任何新工作**：deadline 暂停的 closeout 是**确定性**的
   （`closeout_source=deterministic`）——到点后容量判定恒为"没有"，一次 Provider
   请求都不发（`04 §9.1`）。账本对账（`accounting.consumed_counter_assertions`）钉住
   "暂停快照里的 `model_requests` == 轨迹到暂停点为止的真实请求数"：没有"偷偷多打一次"
   的余地。第二条证据在**执行域**：同一个真实 workspace 里，deadline 已过时再发一次
   MUTATING 调用 ⇒ `DEADLINE_EXCEEDED`、**账上不留行**、**副作用没有发生**。
3. **暂停可续、且必须换一个新的未来时刻**：沿用那个已到点的时刻恢复 ⇒ 领域层拒绝
   （409 面 `BudgetConflict`），事件流一字不改；换一个未来的时刻 ⇒ 生产 CAS 面
   落 `run/resumed`（同一 `run_id`、版本 +1、快照里是新时刻）并**真的继续干活**
   （信息屏障保证剩余链步只可能发生在恢复之后）。
4. **未证副作用不会被当成"没事"**：同一台机器上，一个真实的 MUTATING 超时（生产
   `BashTool` + 生产 Ledger）留下 `UNKNOWN` + "副作用未证"标记 ⇒ 恢复入口拒绝
   （`RecoveryConflict`）、投影报 `reconcile.tool_call_ids`（`11 §6.1` 的真键名）、
   **没有重跑**（副作用计数不变）。这一条不需要模型配合，因此**每次尝试都必然出现**。

## 第二条腿的收尾形状由模型当下的选择决定（两种都安全）

到点暂停**不是**任务结束，但"恢复后模型继续干到再次到点"与"它写一句话自己收尾"都是
**合法且安全**的结局，本场景两种都收（`run_ended_in_a_safe_state`）；`run/failed` /
`run/interrupted` 是第三种，判红。这条口径不是为了让门禁好过——实测（2026-09-26 的两次
真实运行、6 次尝试）：到点拒绝的文案当时只写"不要重复提交本调用"，模型的原话是
"The error says deadline not solved by retry. I should report status briefly."，
于是恢复后的那一轮**零工具调用**、run 直接 `run/completed`；补上"恢复后继续"那句之后
形状不变——说明**停顿之后模型没有可见标记**（残余与后续归属见 ADR-0046 §5）。
两件事因此分开钉：

- 产品侧：拒绝文案必须说明"以新的未来时刻恢复后从暂停前进度继续"（`tooling/executor.py`，
  由 `tests/tooling/test_deadline_admission.py` 守）——**到点不是任务结束**；
- 场景侧：`resumed_leg_admitted_new_work`（恢复后 ≥1 条**接纳**：Provider 请求或 ToolCall）
  仍**严格**判红"恢复只是走过场"。判据是**接纳**而不是"模型用没用工具"：后者是模型的选择
  （实测 6/6 都是"只答一句话"），前者才是产品契约里"新时刻把 run 重新打开"这件事；两种
  读数都如实留在证据里。

## 两种安全结局（票面 AC 的原文是"safe outcome **or** NEED_RECONCILE"）

真实模型的一次 run 有两个合法走向，本场景**都接受**，但各自必须**完整成立**
（`deadline_outcome_is_safe_or_needs_reconcile` 是那个判据，不是"随便哪个都行"）：

- **Arm A（safe）**：到点时没有"副作用未证"的行（正常路径：在途调用全部收尾成终态）
  ⇒ 账本干净、无悬空、换新时刻后可同 run 续跑；
- **Arm B（NEED_RECONCILE）**：到点时**有**这样的行（真实来源：模型一次 bash 调用挂到
  超时 —— 生产 `BashTool` 声明 MUTATING 且 `bash_timeout_seconds` 是部署值）
  ⇒ 暂停里必须点名（`operation/reconcile-required` + `continuation.blockers` +
  `next_safe_action` 指向 reconcile），且**新时刻的恢复同样被拒**（`03 §5`：
  对账优先于恢复），不是"抬个时刻就放行"。

两条路都不允许出现"把未证副作用当没事继续跑"这一种结局 —— 那才是本票要堵的形状。

## 为什么任务必须逐步（信息屏障）

与 `pause_resume.py` 同一论证：`chain.py` 只有参数等于 `chain.txt` 当前串时才推进，
推进结果是**新随机串**（`secrets.token_hex(4)`，不可预计算），且一次调用最多推进一格；
每次推进前 `time.sleep(STEP_SLEEP_SECONDS)`（真实耗时，不是装饰）。`TRANSITIONS` 取一个
两次 deadline 窗口**都不可能跑完**的数：任何一条腿的墙钟除以单步耗时都是上界。于是
"到点时还剩很多步"是**结构**结论，与模型的快慢无关。

链脚本的键名刻意是 `next=` 而不是 `token=`：`live_gate/secrets.py` 的赋值形态扫描会把
`token=<长串>` 读成凭证回显（`long_task.py` 的模块 docstring 记录了那次实测），
安全边界不为取证方便让步。

## 为什么断言里没有"模型措辞"

判据全部机械可检：事件类型 / 计数 / 字段形状 / Ledger 状态 / 工作区文件内容。
continuation 只钉**契约形状**（四键齐、`next_safe_action` 非空），不评内容好坏 ——
本场景不引入 LLM judge（`runner.scope.does_not_cover` 已登记）。

## 生产性来自哪里（这条决定证据算不算数）

- **run / 暂停 / 恢复面**：`AppState(settings)` + `session_service(state)` —— 与 Web 应用、
  CLI `resume` 命令**同一个组合根**（`SessionService` 的唯一合法构造点由
  `tests/session/test_service_collaborators.py` 机械守着）；
- **模型 / 工具 / 沙箱**：与 smoke / long_task / pause_resume 同源 —— `build_runtime`
  装配的生产工具集（`BashTool` 的真正实例）、生产 Sandbox、真实 provider 客户端；
- **执行域那一半**（第 2、4 条）：`ToolExecutor` + `BashTool(sandbox, timeout_seconds=…)`
  + 生产 `OperationLedger`。工具是**同一个类、同一个接线点**（`assembly.py` 里
  `BashTool(sandbox, timeout_seconds=settings.bash_timeout_seconds)`），只把超时值调小
  （部署默认 60s ⇒ 本场景 `UNCERTAIN_TOOL_TIMEOUT_SECONDS`）：让一次真实的 MUTATING
  超时落在可接受的墙钟内。审批回调照生产默认（`assembly.py`：`None` + 未声明
  `auto_approve=False` ⇒ 自动批准），policy 是会话默认的 `workspace-write`。
  为什么不借模型的手造这个超时：**"让真实模型恰好发一条会超时的命令"不是结构保证**，
  而票面要的正是"生产 mutating 工具在一次性 Sandbox 里真的走过 deadline 边界"。
- **会话身份**：每次尝试有一个**固定的** `ctx.session_id`（runner 按它归档轨迹），
  而 `create_and_launch` 自己生成 uuid ⇒ 本场景用同一套生产原语自建会话
  （`WorkspaceRegistry.create(session_id, workspace_root=…)` + `Session.start(cwd=…)`，
  与 `build_runtime` 内部绑定 worktree 的调用形状相同）。这不是"绕过服务"：暂停 / 恢复 /
  CAS 全部仍发生在 `SessionService` 里。

## 一次性边界

运行时产物（会话轨迹 / SQLite / workspace 登记）全部落在一次尝试的临时根目录内
（`settings.workspace_dir` 重定向到 `ctx.session_root.parent`，见 `_scenario_settings`），
跑完由 `workspace.teardown()` 连同根目录一起销毁并核实。可选能力（CAPABILITIES）**沿用
部署配置**：生产装配路径按 `wire_capabilities` 的既有纪律装配它们（外部依赖故障按
OPTIONAL 降级，`08 §7`）。

## 这条运行**不得**被当成"模型表现"的证据

断言里没有一条依赖模型说了什么、做了几步：模型只负责"把链任务做下去"（信息屏障保证
它做不完），到点、暂停、拒绝、对账四条都由 runtime / executor / ledger 的机械事实判定。
"""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evaluation.assertions import dangling_tool_call_ids
from evaluation.live_gate.registry import AttemptOutcome, ScenarioContext
from evaluation.live_gate.scenarios.accounting import (
    consumed_counter_assertions,
    request_accounting,
)
from evaluation.live_gate.schema import AssertionResult

SCENARIO_ID = "run-deadline-boundary"
SCENARIO_VERSION = 1

#: 首次执行的绝对 deadline（相对启动时刻的秒数）。取值理由（两侧都要留够）：
#: 下界 —— 真实 provider 的第一次响应 + 第一次工具调用必须发生在它**之前**（否则
#: 到点时一次工具都没跑，第 1 条结构事实就没有载体）；上界 —— 链任务在这个窗口里
#: **不可能**跑完（信息屏障算出的步数上界远小于 `TRANSITIONS`）。
DEADLINE_SECONDS = 30.0

#: 恢复时给的新时刻（相对恢复时刻）。它必须**严格在未来**（`resume_headroom_ok`），
#: 且**仍然跑不完**链（同一条信息屏障论证）⇒ 第二条腿的收口形状只由模型当下的选择决定：
#: 再撞一次点（`run/paused`）或它自己收尾（`run/completed`）。两种都安全，断言都收；
#: 唯一必须严格成立的是"恢复后真的干了新活"（≥1 次 `tool/call`）。
#: 20s 曾让模型的**第一次**恢复后回答就收尾（实测：恢复后零工具调用）；30s 留出一整轮
#: 决策 + 若干次真实工具调用的余量。窗口之和（30+30=60s）仍小于链的纯工具时间（80s）。
RESUME_DEADLINE_SECONDS = 30.0

#: 链步数 = 模型决策数下界（见模块 docstring 的信息屏障）。
#: 40 × 2s = 80s 的纯工具时间 > 两次窗口之和（50s）⇒ 结构上跑不完。
TRANSITIONS = 40

#: 单步的固定耗时（秒）。它同时是"循环作弊"的防护：把 40 步塞进一次 bash 调用
#: 需要 80s > 生产 bash 超时（60s）⇒ 循环只会换来一次超时（Arm B），不会让链提前跑完。
STEP_SLEEP_SECONDS = 2.0

#: 单次执行的收口上限（秒）。runner 的 `ATTEMPT_TIMEOUT` 是 900s，本场景两条腿各 ≈
#: 一个窗口 + 尾段，远低于它；到点判 FAIL（不是"跳过"）。
PHASE_TIMEOUT = 300.0

CHAIN_SCRIPT = "chain.py"
TOKEN_FILE = "chain.txt"
STEPS_FILE = "chain-steps.txt"
DONE_FILE = "done.txt"
SEED_TOKEN = "seed0"

#: 执行域那一半（第 2、4 条结构事实）用的脚本：**先落副作用、再挂着** ⇒ 超时收尾时
#: 世界状态真的未知（不是"其实什么都没发生"）。sentinel 是副作用计数，也是"没有重跑"
#: 的观测点（追加写：重跑会多一行）。
UNCERTAIN_SCRIPT = "uncertain.py"
UNCERTAIN_SENTINEL = "uncertain.txt"
UNCERTAIN_SENTINEL_LINE = "touched"
#: 生产 bash 超时的部署默认是 60s（`Settings.bash_timeout_seconds`）。本场景把它调小到
#: 这个值：一次真实的 MUTATING 超时要在可接受的墙钟内发生。类与接线点逐字不变。
UNCERTAIN_TOOL_TIMEOUT_SECONDS = 5.0
#: `uncertain.py` 挂着的时长（> 工具超时）。它短于一次尝试的剩余墙钟 ⇒ 即使沙箱没能
#: 立刻杀掉进程树，进程也会在自己的时间里退出，不给 teardown 留一个占着目录的活进程。
UNCERTAIN_SLEEP_SECONDS = 8.0
#: 接纳窗口（秒）：admission 在这个未来时刻**之前**发生（能开单），工具超时（5s）在它
#: **之后**发生 ⇒ 第二次调用落在"已到点"那一侧。两个方向的余量都是秒级。
UNCERTAIN_ADMIT_SECONDS = 3.0

#: local fuse 兜底终态的标记（`agent/runtime.py` 的 `STATUS_MAX_STEPS_EXCEEDED`）：
#: 本场景走的是 `run/paused`，轨迹里出现这个标记就是走错了路。
FUSE_TRIP_MARKER = "max_steps_exceeded"

_CHAIN_SCRIPT_SOURCE = f'''"""Live Gate deadline 链（#315）：一次调用推进一格，每格都真的耗时，推进串随机。

契约：`python chain.py <当前串>`，与 chain.txt 一致才推进——新串由 secrets 生成并写回
chain.txt，步数 +1；不一致则非零退出且**不推进**。

键名用 `next` 而不是 `token`：`live_gate/secrets.py` 的赋值形态扫描会把
`token=<≥16 字符>` 读成凭证回显（见 long_task.py 的实测记录），安全边界不为此让步。
"""

import pathlib
import secrets
import sys
import time

link_file = pathlib.Path("chain.txt")
steps_file = pathlib.Path("chain-steps.txt")

if not link_file.exists():
    sys.exit("chain.txt 缺失：工作区未被正确 seed")
current = link_file.read_text().strip()
if len(sys.argv) < 2 or sys.argv[1].strip() != current:
    sys.exit("串不匹配：必须把当前串作为参数传入（先读上一条输出，不要猜）")

time.sleep({STEP_SLEEP_SECONDS})
new_link = secrets.token_hex(4)
link_file.write_text(new_link)
steps = int((steps_file.read_text().strip() or "0")) + 1
steps_file.write_text(str(steps))
print(f"step={{steps}} next={{new_link}}")
'''

_UNCERTAIN_SCRIPT_SOURCE = f'''"""Live Gate deadline 边界上的"未证副作用"（#315）：副作用先落地，然后挂到超时。

它被生产 `BashTool` 调用（MUTATING）⇒ 工具超时收尾时，账上留 UNKNOWN + "副作用未证"：
`uncertain.txt` 已经写出来了，谁也证明不了它"没发生"（`07 §7`）。
"""

import pathlib
import time

pathlib.Path("{UNCERTAIN_SENTINEL}").write_text("{UNCERTAIN_SENTINEL_LINE}", encoding="utf-8")
time.sleep({UNCERTAIN_SLEEP_SECONDS})
print("finished")
'''

TASK = (
    "这是一条必须**逐步**完成的链式任务：工作区里已经放好 chain.py、chain.txt 与 chain-steps.txt。\n"
    "每一步只做一件事：\n"
    "  1) 运行 `python chain.py <当前串>`"
    "（第一次的当前串就是 chain.txt 里的初值，可以先用 bash 读一次 chain.txt）；\n"
    "  2) 从输出里拿到新串（形如 `step=N next=xxxxxxxx` 两段），它同时被写进 chain.txt。\n"
    f"重复上面的动作，直到 chain-steps.txt 里的步数达到 {TRANSITIONS}。\n"
    "必须遵守的规则：\n"
    "  - **一次 bash 调用只运行一次 chain.py**：不要用 shell 循环，也不要用 `;` / `&&` "
    "把多次调用拼在一条命令里——串是随机值，只能从上一条命令的输出里读出来；\n"
    "  - 每一步都要真的执行命令并读输出，不要跳步；\n"
    "  - 不确定进度时**重新读** chain.txt 与 chain-steps.txt：它们就是权威状态；\n"
    f"  - 全部 {TRANSITIONS} 步完成后，用 write 工具创建 {DONE_FILE}，内容恰好是最终的串"
    "（不要换行、不要引号、不要多余字符）；\n"
    "  - 如果本次运行因为到点被打断（工具调用收到 DEADLINE_EXCEEDED，或 run 暂停），"
    "那是**暂时**的：本 run 以新的未来时刻恢复后，你必须从 chain.txt / "
    f"{STEPS_FILE} 指示的当前位置继续，直到 {TRANSITIONS} 步真的完成；\n"
    "  - 不要探索其它文件，不要写别的文件；最后用一句话汇总，不要长篇输出。"
)


# ── 小工具 ────────────────────────────────────────────────────────────────


def _safe_read(sandbox: Any, name: str) -> str:
    """读工作区文件；读不到返回空串（**读失败也是判据的一种取值**，不是异常）。"""
    try:
        return sandbox.read_text(name)
    except Exception:  # noqa: BLE001 - 产物缺失 → 后续断言自然不过
        return ""


def _step_count(raw: str) -> int | None:
    """`chain-steps.txt` → 步数（读不出 / 畸形 = None，不猜 0）。"""
    text = raw.strip()
    if not text.isdigit():
        return None
    return int(text)


def _sentinel_count(sandbox: Any) -> int:
    """副作用计数：每次成功的 `python uncertain.py` 追加一行（重跑会多一行）。"""
    return _safe_read(sandbox, UNCERTAIN_SENTINEL).count(UNCERTAIN_SENTINEL_LINE)


def _event_text(event: Any) -> str:
    """事件的文字面（用于扫 `max_steps_exceeded` 这类标记）。取不到就退化成类型名。"""
    try:
        return json.dumps({"type": event.type, "data": event.data}, default=str)
    except Exception:  # noqa: BLE001 - 事件序列化失败不该让整次尝试崩掉
        return str(getattr(event, "type", ""))


def _failure_text(error: BaseException) -> str:
    """异常 → **可定位**的一行：类型 + 消息 + 最后几个本仓库栈帧 + 出错那行的源码。

    Live Gate 的证据常常是远程运行留下的唯一产物，`类型: 消息` 不够用——本票实测：
    真实运行三连 FAIL 只报 `TypeError: 'NoneType' object is not iterable`，看不出是哪一行，
    只能再烧一次真实运行去猜（一次 ≈ 4 分钟 + 真实调用）。第三方帧对定位没有帮助，
    还被排除掉：它们只会把环境路径写进证据。
    """
    frames = [
        frame for frame in traceback.extract_tb(error.__traceback__)
        if "site-packages" not in frame.filename
    ]
    if not frames:
        return f"{type(error).__name__}: {error}"
    chain = " <- ".join(f"{Path(frame.filename).name}:{frame.lineno}" for frame in frames[-3:])
    return f"{type(error).__name__}: {error} @ {chain} 〔{(frames[-1].line or '').strip()}〕"


def _deadline_instant(value: Any) -> datetime | None:
    """快照 / 请求里的 `deadline_at` → `datetime`（读不出来 = None，不猜）。

    断言比的是**同一个瞬时**，不是同一个字符串：wire 上它是 RFC 3339 UTC 文本
    （`11 §6.1`），而 `isoformat()` 在微秒为 0 时不写小数部分 ⇒ 逐字节比对会造出
    一条与语义无关的红。本函数把两侧都归一成时刻，文本只在 detail 里出现。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError:
        return None


def _deadline_text(value: Any) -> str:
    """`deadline_at` 的可读文本（证据 detail 用；形状与 `run_budget._deadline_text` 同族）。"""
    moment = _deadline_instant(value)
    return "" if moment is None else moment.isoformat().replace("+00:00", "Z")


def _unreconciled(operations: list[Any]) -> list[Any]:
    """账本里仍欠对账的行（判据只在 `storage.needs_reconcile` 一处）。"""
    from agent_harness.storage import needs_reconcile

    return [operation for operation in operations if needs_reconcile(operation)]


def _projection_reconcile_pending(projection: Any) -> list[str]:
    """API 投影里**仍欠对账**的 tool_call_id（读的是 `11 §6.1` 的真键名）。

    键名有一个踩过的坑，值得写在这里：投影里**没有** `reconcile_pending` 这个键——
    `run_budget.project_budget` 只在非空时落一个 `reconcile` 子对象
    （`{"state": "needs_reconcile", "tool_call_ids": [...]}`）。按名字猜着读会拿到 `None`，
    而 `list(None)` 会以 `TypeError: 'NoneType' object is not iterable` 收场：本票实测
    真实运行三连 FAIL 就出在这里，且因为异常被兜成一行读数，真实运行白烧一次才定位到。
    """
    if not isinstance(projection, dict):
        return []
    reconcile = projection.get("reconcile")
    if not isinstance(reconcile, dict):
        return []
    return sorted(str(item) for item in (reconcile.get("tool_call_ids") or []))


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


async def _drain(result: Any, *, service: Any, phase: str) -> list[Any]:
    """消费一次执行的 live 流直到哨兵；返回流里的 AgentEvent 列表。

    形状照 CLI：订阅者队列 `get()` 到 `run_manager.DONE` 即本次执行收口
    （暂停也走这条：`run/paused` 是**非终态**收口，但 run task 同样结束）。
    到点未收口 ⇒ 抛 `TimeoutError`（runner 外层仍会兜住），绝不伪装成"跑完了"。
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


class _Refusal:
    """一次"恢复必须被拒"的实测结论（不抛给调用方，转成一等事实）。"""

    def __init__(self, *, raised: BaseException | None, events_unchanged: bool) -> None:
        self.raised = raised
        self.events_unchanged = events_unchanged
        self.reason = "" if raised is None else f"{type(raised).__name__}: {raised}"

    @property
    def ok(self) -> bool:
        return self.raised is not None and self.events_unchanged


class RunDeadlineBoundaryScenario:
    """真实模型 + 真实道具：绝对 deadline 到点 ⇒ 安全暂停 ⇒ 换新时刻同 run 续跑。"""

    id = SCENARIO_ID
    version = SCENARIO_VERSION
    description = (
        "绝对 deadline 到点：closeout 确定性、零新 Provider 请求；沿用已到点时刻恢复被拒"
        "（无副作用）；换未来时刻恢复同一 run_id 继续干活；生产执行域里 MUTATING 超时 ⇒"
        "账上未证 + 恢复被拒 + 不重跑"
    )

    async def prepare(self, ctx: ScenarioContext) -> list[str]:
        """seed 链脚本 / 未证副作用脚本 + 前置自查。返回未满足的前置（非空 ⇒ BLOCKED）。"""
        probe = ctx.sandbox.exec("python --version")
        if probe.exit_code != 0:
            return [
                (
                    f"工作区里没有可用的 python（rc={probe.exit_code}）——本场景的链脚本与"
                    "未证副作用脚本都要在沙箱内执行，缺它不构成 FAIL 而是环境不具备"
                ),
            ]
        ctx.sandbox.write_text(CHAIN_SCRIPT, _CHAIN_SCRIPT_SOURCE)
        ctx.sandbox.write_text(TOKEN_FILE, SEED_TOKEN)
        ctx.sandbox.write_text(STEPS_FILE, "0")
        ctx.sandbox.write_text(UNCERTAIN_SCRIPT, _UNCERTAIN_SCRIPT_SOURCE)
        return []

    async def run(self, ctx: ScenarioContext) -> AttemptOutcome:
        """跑一次完整流程。**异常一律转 `ok=False`**（runner 兜底捕获是第二道）。"""
        from agent_harness.agent.run_budget import RESUME_BASIS_BUDGET_INCREASE
        from agent_harness.session import JsonlSessionStore, Session
        from agent_harness.web.app import AppState, session_service

        settings = _scenario_settings(ctx)
        state = AppState(settings)
        service = session_service(state)
        deadline_1 = datetime.now(UTC) + timedelta(seconds=DEADLINE_SECONDS)
        legs: dict[str, Any] = {
            "first_deadline": deadline_1,
            "first_stream": [],
            "resume_stream": [],
            "resume_refused": None,
        }
        uncertain: dict[str, Any] = {}
        try:
            # 显式初始化恢复三 Store（DDL + transport 表）。`SessionService` 自己也会惰性
            # ensure（它拿到的是同一个回调），但本场景**直接**读 `state.operation_ledger`
            # ——不把"某条服务路径恰好先 ensure 过"当成前提，否则一次真实的运行会以
            # `no such table: operations` 收场（本文件自己的 bug，不是被测对象的问题）。
            await state.ensure_stores()
            store = JsonlSessionStore(root=ctx.session_root)
            # 会话身份 = Live Gate 给的 id（轨迹归档按它找）；worktree 绑定用生产
            # `build_runtime` 的同一调用形状（见模块 docstring"生产性来自哪里"）。
            state.workspace_registry.create(
                ctx.session_id, workspace_root=ctx.sandbox.workspace_root,
            )
            Session.start(store, session_id=ctx.session_id, cwd=ctx.sandbox.workspace_root)

            first = await service.resume_and_launch(
                session_id=ctx.session_id, task=TASK, run_deadline_at=deadline_1,
            )
            legs["first_stream"] = await _drain(first, service=service, phase="deadline 首次执行")
            events = await service.get_events(ctx.session_id)
            pauses = [event for event in events if event.type == "run/paused"]
            if not pauses:
                # 场景前提不成立（模型没干活就收口了）：如实 ok=False，**不做假恢复**。
                raise RuntimeError(
                    "deadline 执行没有留下暂停（run/paused）——场景前提不成立，"
                    f"轨迹={[event.type for event in events]}"
                )
            paused = pauses[0]
            operations = await state.operation_ledger.list_for_session(ctx.session_id)
            debt_after_pause = _unreconciled(operations)

            # ① 已到点的时刻不得用于恢复（两种结局下都必须被拒：Arm B 是"对账优先"，
            # Arm A 是"恢复后立刻再次到点"）。
            legs["resume_refused"] = await self._expect_refusal(
                service, store, session_id=ctx.session_id, paused=paused,
                deadline=deadline_1, resume_basis=RESUME_BASIS_BUDGET_INCREASE,
            )
            if debt_after_pause:
                # Arm B：账上欠对账 ⇒ 换新时刻的恢复同样必须被拒（这就是"阻塞恢复"）。
                legs["resume_with_new_instant_refused"] = await self._expect_refusal(
                    service, store, session_id=ctx.session_id, paused=paused,
                    deadline=datetime.now(UTC) + timedelta(seconds=RESUME_DEADLINE_SECONDS),
                    resume_basis=RESUME_BASIS_BUDGET_INCREASE,
                )
            else:
                # Arm A：换一个未来的时刻 ⇒ 生产 CAS 面接回同一个 run 并真的继续干。
                deadline_2 = datetime.now(UTC) + timedelta(seconds=RESUME_DEADLINE_SECONDS)
                resume = await service.resume_and_launch(
                    session_id=ctx.session_id, task=None,
                    resume_run_id=str(paused.run_id or ""),
                    resume_basis=RESUME_BASIS_BUDGET_INCREASE,
                    run_deadline_at=deadline_2,
                    expected_version=int(paused.data.get("budget_version") or 0),
                )
                legs["resume_stream"] = await _drain(
                    resume, service=service, phase="新时刻同 run 续跑执行",
                )
                legs["resume_deadline"] = deadline_2

            uncertain = await self._uncertain_mutation(
                ctx=ctx, state=state, service=service, store=store,
            )
        except Exception as error:  # noqa: BLE001 - 失败要如实记录（runner 统一脱敏）
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id,
                error=_failure_text(error),
            )
        finally:
            # 关闭可选能力（记忆形成泵 / MCP / 外部连接）与在途 run —— 一次尝试一个
            # 生命周期，不把连接与后台任务带进下一次尝试。`shutdown()` 幂等。
            await state.shutdown()

        # 终态读盘必须在 `shutdown()` **之后**：可选能力的收尾会**追加 durable 事实**
        # （`memory/degraded` 就落在 `run/completed` 之后），早读会让判定所依据的事件
        # 比 runner 复制的那份轨迹少一行（`pause_resume.py` 记录了那次实测）。
        try:
            events = await service.get_events(ctx.session_id)
            replayed = store.read_events(ctx.session_id)
            projection = await service.budget_projection(ctx.session_id)
            operations = await state.operation_ledger.list_for_session(ctx.session_id)
        except Exception as error:  # noqa: BLE001 - 同上的如实记录
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id,
                error=_failure_text(error),
            )

        tool_calls = [
            str(event.data.get("tool_name"))
            for event in events
            if event.type == "tool/call"
        ]
        run_id = next(
            (str(event.run_id or "") for event in events if event.type == "run/started"), "",
        )
        assertions = self._assertions(
            ctx=ctx, events=events, replayed=replayed, tool_calls=tool_calls,
            operations=operations, projection=projection, legs=legs, uncertain=uncertain,
            pause=next((event for event in events if event.type == "run/paused"), None),
        )
        pauses = [event for event in events if event.type == "run/paused"]
        completed = [event for event in events if event.type == "run/completed"]
        return AttemptOutcome(
            ok=all(item.ok for item in assertions),
            session_id=ctx.session_id,
            run_id=run_id,
            # 终态读 durable 事实：第一条腿必到点暂停（非终态）；第二条腿两种形状都如实报
            # ——又干到到点 ⇒ paused，模型自己收尾 ⇒ completed。不把两种形状并成一个词。
            run_status="completed" if completed else ("paused" if pauses else "unknown"),
            steps=sum(1 for event in events if event.type == "model/completed"),
            tool_calls=tool_calls,
            assertions=assertions,
            event_count=len(events),
            output_tail=self._continuation_tail(pauses[0] if pauses else None),
        )

    # ── 执行域那一半（第 2、4 条结构事实）────────────────────────────────

    async def _uncertain_mutation(
        self, *, ctx: ScenarioContext, state: Any, service: Any, store: Any,
    ) -> dict[str, Any]:
        """生产执行域在真实 workspace 里走一遍"未证副作用"与"到点不再接纳"。

        两条调用同一个脚本、只差时刻：

        - 调用 A：在 deadline **之前**被接纳（`run_deadline` 是个未来时刻）⇒ 命令先落
          副作用、再挂到工具超时 ⇒ 执行器按 `07 §7` 落 `UNKNOWN` + "副作用未证"；
        - 调用 B：工具超时（3s）已经把墙钟推过那个时刻 ⇒ 接纳闸门拒收
          （`DEADLINE_EXCEEDED`、账上**不留行**、脚本没跑 ⇒ 副作用计数不变）。

        之后用生产恢复入口（`SessionService.recover`）证明"欠对账 ⇒ 拒绝恢复"，并核对
        API 投影把欠账如实报给客户端（`reconcile_pending`）。
        """
        from agent_harness.session import Session
        from agent_harness.storage import needs_reconcile
        from agent_harness.tooling import (
            ApprovalResponse,
            PermissionPolicy,
            ToolCall,
            ToolExecutor,
            ToolRegistry,
        )
        from agent_harness.tools import BashTool

        session_id = f"{ctx.session_id}-uncertain"
        session = Session.start(
            store, session_id=session_id, cwd=ctx.sandbox.workspace_root,
        )
        registry = ToolRegistry()
        # 与 `assembly.py` 的接线点同一个类、同一个参数名（超时值见模块 docstring）。
        registry.register(
            BashTool(ctx.sandbox, timeout_seconds=UNCERTAIN_TOOL_TIMEOUT_SECONDS),
        )

        async def _auto_approve(_request: Any) -> ApprovalResponse:
            # 生产默认（`assembly.py`：未声明 auto_approve 且无交互回调 ⇒ 自动批准）。
            return ApprovalResponse(approved=True, reason="auto-approve")

        executor = ToolExecutor(
            registry,
            policy=PermissionPolicy.WORKSPACE_WRITE,
            approval_callback=_auto_approve,
            operation_ledger=state.operation_ledger,
        )
        deadline = datetime.now(UTC) + timedelta(seconds=UNCERTAIN_ADMIT_SECONDS)
        context = self._operation_context(session_id)
        first = await executor.execute(
            ToolCall(id="call-uncertain-1", name="bash", args={"command": f"python {UNCERTAIN_SCRIPT}"}),
            operation_context=context, session=session, run_deadline=deadline,
        )
        second = await executor.execute(
            ToolCall(id="call-uncertain-2", name="bash", args={"command": f"python {UNCERTAIN_SCRIPT}"}),
            operation_context=context, session=session, run_deadline=deadline,
        )
        rows = {
            operation.tool_call_id: operation
            for operation in await state.operation_ledger.list_for_session(session_id)
        }
        sentinel_after_calls = _sentinel_count(ctx.sandbox)

        # 生产恢复入口：欠对账 ⇒ 拒绝（没有 ReconcileCallback 时不允许"猜一个结果"）。
        refusal: BaseException | None = None
        try:
            await service.recover(session_id)
        except BaseException as error:  # noqa: BLE001 - 这是**要记录的事实**，不是异常面
            refusal = error
        rows_after_recovery = {
            operation.tool_call_id: operation
            for operation in await state.operation_ledger.list_for_session(session_id)
        }
        projection = await service.budget_projection(session_id)
        return {
            "session_id": session_id,
            "deadline": deadline,
            "first": first,
            "second": second,
            "rows": rows,
            "rows_after_recovery": rows_after_recovery,
            "sentinel_after_calls": sentinel_after_calls,
            "sentinel_after_recovery": _sentinel_count(ctx.sandbox),
            "recovery_refused": refusal,
            "reconcile_pending": _projection_reconcile_pending(projection),
            "needs_reconcile_first": bool(
                rows.get("call-uncertain-1") is not None
                and needs_reconcile(rows["call-uncertain-1"])
            ),
        }

    @staticmethod
    def _operation_context(session_id: str) -> Any:
        from agent_harness.storage import OperationContext

        return OperationContext(session_id=session_id, run_id=None, agent_id="default")

    async def _expect_refusal(
        self, service: Any, store: Any, *, session_id: str, paused: Any,
        deadline: datetime, resume_basis: str,
    ) -> _Refusal:
        """调一次恢复并**期望被拒**；同时核实事件流一字未改（零副作用）。"""
        before = [event.to_dict() for event in store.read_events(session_id)]
        raised: BaseException | None = None
        try:
            await service.resume_and_launch(
                session_id=session_id, task=None,
                resume_run_id=str(paused.run_id or ""),
                resume_basis=resume_basis,
                run_deadline_at=deadline,
                expected_version=int(paused.data.get("budget_version") or 0),
            )
        except BaseException as error:  # noqa: BLE001 - 拒绝是**期望结果**，不是异常面
            raised = error
        after = [event.to_dict() for event in store.read_events(session_id)]
        return _Refusal(raised=raised, events_unchanged=before == after)

    # ── 断言（机械可检，不是 LLM judge）────────────────────────────────

    def _assertions(
        self, *, ctx: ScenarioContext, events: list[Any], replayed: list[Any],
        tool_calls: list[str], operations: list[Any], projection: dict[str, Any],
        legs: dict[str, Any], uncertain: dict[str, Any], pause: Any,
    ) -> list[AssertionResult]:
        from agent_harness.agent.run_budget import (
            REASON_DEADLINE,
            TRIGGER_RUN_DEADLINE,
            derive_run_budget,
        )
        from agent_harness.session.event import (
            OPERATION_RECONCILE_REQUIRED,
            RUN_COMPLETED,
            RUN_FAILED,
            RUN_INTERRUPTED,
            RUN_PAUSED,
            RUN_RESUMED,
            RUN_STARTED,
            TOOL_CALL,
            USER_MESSAGE,
        )
        from agent_harness.storage import OperationState
        from agent_harness.tooling import ErrorCode

        terminal_types = (RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED)
        pauses = [event for event in events if event.type == RUN_PAUSED]
        resumed_events = [event for event in events if event.type == RUN_RESUMED]
        started_events = [event for event in events if event.type == RUN_STARTED]
        user_messages = [event for event in events if event.type == USER_MESSAGE]
        run_id = str(started_events[0].run_id or "") if started_events else ""
        pdata = dict(pause.data) if pause is not None else {}
        p_limits = pdata.get("limits")
        run_scope = p_limits.get("run") if isinstance(p_limits, dict) else None
        local_scope = p_limits.get("local") if isinstance(p_limits, dict) else None
        continuation = pdata.get("continuation")
        continuation_ok = (
            isinstance(continuation, dict)
            and all(
                isinstance(continuation.get(key), list)
                for key in ("completed", "remaining", "blockers")
            )
            and isinstance(continuation.get("next_safe_action"), str)
            and bool(continuation.get("next_safe_action", "").strip())
        )
        pause_ok = (
            pause is not None
            and str(pdata.get("reason")) == REASON_DEADLINE
            and str(pdata.get("trigger_dimension")) == TRIGGER_RUN_DEADLINE
            and str(pdata.get("closeout_source")) == "deterministic"
            and list(pdata.get("resume_requirements") or []) == []
            and isinstance(run_scope, dict)
            and _deadline_instant(run_scope.get("deadline_at"))
            == _deadline_instant(legs["first_deadline"])
            and isinstance(local_scope, dict)
            and bool(str(local_scope.get("source") or ""))
            and pdata.get("budget_version") == 1
        )
        pause_cutoff = pause.seq if pause is not None else None
        pause_facts = request_accounting(
            [event for event in events if pause_cutoff is None or event.seq <= pause_cutoff]
        )
        consumed_at_pause = pdata.get("consumed")
        turns_at_pause = (
            consumed_at_pause.get("agent_turns") if isinstance(consumed_at_pause, dict) else None
        )
        tool_calls_before_pause = sum(
            1 for event in events
            if event.type == TOOL_CALL and (pause_cutoff is None or event.seq <= pause_cutoff)
        )
        no_terminal_before_pause = all(
            event.seq > pause.seq for event in events if event.type in terminal_types
        ) if pause is not None else False

        # ── 两种安全结局的分类（票面 AC 的原文就是这二择一）──
        debt = _unreconciled(operations)
        reconcile_events = [
            event for event in events if event.type == OPERATION_RECONCILE_REQUIRED
        ]
        blocked_by = list((continuation or {}).get("blockers") or [])
        next_action = str((continuation or {}).get("next_safe_action") or "")
        arm = "needs_reconcile" if debt else "safe"
        unreconciled_ok = (
            (
                # Arm A：账本干净（每一条都是终态）+ 无悬空调用
                not debt
                and not dangling_tool_call_ids(events)
                and all(
                    operation.state in (
                        OperationState.SUCCEEDED,
                        OperationState.FAILED,
                        OperationState.CANCELLED,
                    )
                    for operation in operations
                )
            )
            if arm == "safe"
            else (
                # Arm B：暂停必须点名这些欠账（事件 + continuation），不是"悄悄带着债停"
                bool(reconcile_events)
                and all(str(event.run_id or "") == run_id for event in reconcile_events)
                # blockers 是**文案**（`runtime._raise_deadline_reconcile` 按
                # `工具 '<name>'（tool_call_id=<id>）…` 组装），不是 id 的集合 ⇒ 判据是
                # **子串**，不是列表成员（`in` 对字符串列表做的是相等比较，写错这一处
                # 会让本判据恒红而不报错）。
                and any(
                    any(
                        operation.tool_call_id in str(blocker)
                        or operation.tool_name in str(blocker)
                        for blocker in blocked_by
                    )
                    for operation in debt
                )
                and "reconcile" in next_action.lower()
            )
        )
        refusal = legs["resume_refused"]
        expired_refusal_ok = (
            refusal.ok
            and (
                arm == "needs_reconcile"
                or "严格在未来" in str(refusal.reason)
            )
        )
        # 第一条腿的收口形状是**产品决定**的，与模型无关：到点 ⇒ 恰好在稳定边界暂停一次，
        # 且暂停之前没有任何终态事件（`03 §3.4`：暂停是非终态）。
        first_leg_ok = no_terminal_before_pause and len(started_events) == 1
        completed_events = [event for event in events if event.type == RUN_COMPLETED]
        failed_events = [event for event in events if event.type in (RUN_FAILED, RUN_INTERRUPTED)]
        if arm == "safe":
            resumed = resumed_events[0] if resumed_events else None
            rdata = dict(resumed.data) if resumed is not None else {}
            r_limits = rdata.get("limits")
            r_run_scope = r_limits.get("run") if isinstance(r_limits, dict) else None
            tool_calls_after_resume = sum(
                1 for event in events
                if event.type == TOOL_CALL and resumed is not None and event.seq > resumed.seq
            )
            after_resume = request_accounting(
                [event for event in events if resumed is not None and event.seq > resumed.seq]
            )
            admitted_after_resume = after_resume.requests + tool_calls_after_resume
            # 恢复契约（`#312` T4 的版本合同，逐条机械可检，与模型无关）。
            resume_contract_ok = (
                len(resumed_events) == 1
                and resumed is not None
                and resumed.run_id == run_id
                and rdata.get("from_pause_seq") == (pause.seq if pause is not None else None)
                and rdata.get("previous_budget_version") == 1
                and rdata.get("budget_version") == 2
                and isinstance(r_run_scope, dict)
                and _deadline_instant(r_run_scope.get("deadline_at"))
                == _deadline_instant(legs.get("resume_deadline"))
                and len(started_events) == 1
                and len(user_messages) == 1
            )
            # 恢复**不是**走过场：新窗口里必须真的接纳过新工作。判据是**接纳**（Provider 请求
            # 或 ToolCall），不是"模型用了工具"——后者是模型当下的选择，不是产品契约：
            # 实测（2026-09-26 两次真实运行、6 次尝试）模型在恢复后的那一轮选择直接收尾
            # （`tool/call=0`、Provider 请求 ≥1、`run/completed`），到点暂停在模型看来
            # 没有可见标记（残余登记见 ADR-0046 §5）。而"新时刻真的把 run 重新打开"这件事
            # 是可证的：环顶的接纳闸门在到点时会暂停整个 run，所以恢复后的那一次请求
            # 本身就是"新窗口在未来且被接纳"的证据。
            new_work_ok = admitted_after_resume >= 1
            # 第二条腿的收尾形状**由模型当下的选择决定**，产品侧两种都合法、都安全：
            #   ① 又干到到点 ⇒ 第二次 `run/paused`（版本 2，仍非终态）；
            #   ② 它自己认为可以收尾 ⇒ `run/completed`（写一句话就停，不发新工具调用）。
            # 第三种（`run/failed` / `run/interrupted`）**不安全**，判红。
            # 第二种形状的前提是"恢复真的发生过"：没有 `run/resumed` 的两次暂停是
            # **恢复缺失**（由 `resume_contract_holds` 判红），不是"恢复后又干到到点"。
            ended_paused = (
                len(pauses) == 2
                and resumed is not None
                and pauses[1].seq > resumed.seq
                and not completed_events
                and not failed_events
            )
            ended_completed = (
                len(pauses) == 1
                and len(completed_events) == 1
                and resumed is not None
                and completed_events[0].seq > resumed.seq
                and not failed_events
            )
            ending_ok = ended_paused or ended_completed
            ending_label = (
                "到点再次暂停（版本 2，非终态）" if ended_paused
                else "恢复后由模型收尾（run/completed）" if ended_completed
                else f"不安全的收尾（暂停 {len(pauses)} 次、完成 {len(completed_events)} 次、"
                     f"失败/中断 {len(failed_events)} 次）"
            )
            # `derive_run_budget` 里终态与 paused **互斥**（终态压过暂停，`03 §5`）：
            # 自己收尾的那条腿上派生出的 `paused` 必须是 None，不是"还挂着等恢复"。
            expected_terminal = ended_completed
            expected_paused = not ended_completed
            arm_ok = True  # Arm A 的"安全性"证据全在 `unreconciled_ok`（账本干净 + 无悬空）
            arm_detail = (
                f"Arm A（safe）：账本无欠账/无悬空；换新时刻后 run/resumed={len(resumed_events)}"
                f"（同一 run_id，{rdata.get('previous_budget_version')}→{rdata.get('budget_version')}，"
                f"新 deadline={_deadline_text((r_run_scope or {}).get('deadline_at')) or '缺失'}）"
                f"，恢复后 tool/call={tool_calls_after_resume}，收尾={ending_label}"
            )
        else:
            second = legs.get("resume_with_new_instant_refused")
            arm_ok = second is not None and second.ok
            expected_terminal = False
            expected_paused = True
            resume_contract_ok = True  # Arm B 没有恢复（对账优先于恢复）
            new_work_ok = True
            ending_ok = True
            ending_label = "未恢复（对账优先于恢复）"
            arm_detail = (
                f"Arm B（NEED_RECONCILE）：欠账={[op.tool_call_id for op in debt]}，"
                f"reconcile-required={len(reconcile_events)}，blockers={blocked_by}，"
                f"next_safe_action={next_action[:80]!r}；换新时刻的恢复也被拒="
                f"{second.reason if second is not None else '未测'}"
            )

        state = derive_run_budget(events, run_id) if run_id else None
        replay_state = derive_run_budget(replayed, run_id) if run_id else None
        fuse_tripped = any(FUSE_TRIP_MARKER in _event_text(event) for event in events)
        steps_recorded = _step_count(_safe_read(ctx.sandbox, STEPS_FILE))
        p_unreconciled = _projection_reconcile_pending(projection)
        ledger_debt = sorted(operation.tool_call_id for operation in debt)

        # ── 执行域那一半 ──
        rows = uncertain.get("rows") or {}
        first_row = rows.get("call-uncertain-1")
        first_result = getattr(uncertain.get("first"), "result", None)
        second_result = getattr(uncertain.get("second"), "result", None)
        first_unproven = (
            first_result is not None
            and first_result.ok is False
            and first_result.error_code is ErrorCode.TIMEOUT
            and first_row is not None
            and first_row.state is OperationState.UNKNOWN
            and uncertain.get("needs_reconcile_first") is True
            and uncertain.get("sentinel_after_calls") == 1
        )
        second_blocked = (
            second_result is not None
            and second_result.ok is False
            and second_result.error_code is ErrorCode.DEADLINE_EXCEEDED
            and second_result.retryable is False
            and "call-uncertain-2" not in rows
            and uncertain.get("sentinel_after_calls") == 1
        )
        recovery_refused = uncertain.get("recovery_refused")
        rows_after = uncertain.get("rows_after_recovery") or {}
        # 这一条的投影读的是**执行域那个会话**的（`<session_id>-uncertain`）：未证副作用
        # 发生在那里，而 `projection` 是主会话的（它自己那本账是干净的 ⇒ 由
        # `projection_matches_ledger_debt` 判）。两本账不能混着比：主会话的
        # `reconcile_pending` 永远是空的，拿它当判据会让本条恒红。
        uncertainty_pending = list(uncertain.get("reconcile_pending") or [])
        recovery_ok = (
            recovery_refused is not None
            and type(recovery_refused).__name__ == "RecoveryConflict"
            and rows_after.get("call-uncertain-1") is not None
            and rows_after["call-uncertain-1"].state is OperationState.UNKNOWN
            and uncertain.get("sentinel_after_recovery") == 1
            and uncertainty_pending == ["call-uncertain-1"]
        )

        return [
            AssertionResult(
                name="deadline_pause_snapshot",
                ok=pause_ok,
                detail=(
                    f"run/paused={len(pauses)} reason={pdata.get('reason')!r} "
                    f"trigger={pdata.get('trigger_dimension')!r} "
                    f"closeout={pdata.get('closeout_source')!r} "
                    f"resume_requirements={pdata.get('resume_requirements')!r} "
                    f"run.deadline_at={_deadline_text((run_scope or {}).get('deadline_at')) or '缺失'}"
                    f"（期望 {_deadline_text(legs['first_deadline'])}）"
                    f" version={pdata.get('budget_version')}"
                ),
            ),
            AssertionResult(
                name="deadline_pause_precedes_any_terminal",
                ok=first_leg_ok,
                detail=(
                    f"暂停={len(pauses)}；暂停之前无终态事件={no_terminal_before_pause}；"
                    f"run/started={len(started_events)}（到点只是收口，逻辑 run 未终结）；"
                    f"收尾形状={ending_label}"
                ),
            ),
            AssertionResult(
                name="deadline_pause_continuation_contract",
                ok=continuation_ok,
                detail=(
                    "continuation 四键形状（next_safe_action 非空）："
                    f"{'齐' if continuation_ok else '不齐'}"
                ),
            ),
            AssertionResult(
                name="real_work_admitted_before_the_deadline",
                ok=(
                    isinstance(turns_at_pause, int)
                    and not isinstance(turns_at_pause, bool)
                    and turns_at_pause >= 1
                    and pause_facts.by_role.get("primary", 0) >= 1
                    and tool_calls_before_pause >= 1
                ),
                detail=(
                    f"到点前被接纳的产出轮={turns_at_pause}；primary 请求="
                    f"{pause_facts.by_role.get('primary', 0)}（真实 provider）；"
                    f"tool/call={tool_calls_before_pause}"
                    f"（真实 BashTool 在一次性 Sandbox 里跑过）；"
                    f"{STEPS_FILE}={steps_recorded if steps_recorded is not None else '缺失'}"
                    f"/{TRANSITIONS}（结构上跑不完）"
                ),
            ),
            *consumed_counter_assertions(
                facts=pause_facts, consumed=consumed_at_pause, label="deadline_pause",
            ),
            AssertionResult(
                name="expired_deadline_resume_refused",
                ok=expired_refusal_ok,
                detail=(
                    f"沿用已到点时刻恢复：被拒={refusal.raised is not None}，"
                    f"事件流一字未改={refusal.events_unchanged}，原因={refusal.reason or '未拒绝'}"
                ),
            ),
            AssertionResult(
                name="deadline_outcome_is_safe_or_needs_reconcile",
                ok=unreconciled_ok and arm_ok,
                detail=arm_detail,
            ),
            AssertionResult(
                name="uncertain_mutation_recorded_as_unproven",
                ok=first_unproven,
                detail=(
                    "生产 BashTool（MUTATING）+ 生产 Ledger：调用 A 在 deadline 前被接纳、"
                    f"超时收尾 ⇒ error_code={getattr(first_result, 'error_code', None)} "
                    f"state={getattr(first_row, 'state', None)} "
                    f"needs_reconcile={uncertain.get('needs_reconcile_first')} "
                    f"副作用计数={uncertain.get('sentinel_after_calls')}"
                    "（副作用已落地 ⇒ 真的证不出结论，不是「其实没发生」）"
                ),
            ),
            AssertionResult(
                name="no_new_admission_after_the_deadline",
                ok=second_blocked,
                detail=(
                    "到点后再发同一条 MUTATING 调用："
                    f"error_code={getattr(second_result, 'error_code', None)} "
                    f"retryable={getattr(second_result, 'retryable', None)} "
                    f"账上留行={'call-uncertain-2' in rows} "
                    f"副作用计数={uncertain.get('sentinel_after_calls')}"
                    "（拒收 = 不留行 + 没执行 —— 三个证据同向）"
                ),
            ),
            AssertionResult(
                name="unreconciled_debt_blocks_recovery",
                ok=recovery_ok,
                detail=(
                    f"生产恢复入口被拒={recovery_refused is not None}"
                    f"（{type(recovery_refused).__name__ if recovery_refused else '未拒绝'}）；"
                    f"账行状态={getattr(rows_after.get('call-uncertain-1'), 'state', None)}"
                    "（拒绝不顺手改账）；"
                    f"恢复后副作用计数={uncertain.get('sentinel_after_recovery')}"
                    "（没有盲重跑）；"
                    f"执行域会话的投影 reconcile_pending={uncertainty_pending}"
                    f"（主会话那份={p_unreconciled}）"
                ),
            ),
            AssertionResult(
                name="projection_matches_ledger_debt",
                ok=p_unreconciled == ledger_debt,
                detail=(
                    f"投影 reconcile_pending={p_unreconciled} == 账本欠账 {ledger_debt}"
                    "（客户端读到的就是账本事实）"
                ),
            ),
            AssertionResult(
                name="run_identity_and_task_shape",
                ok=(
                    bool(run_id)
                    and {str(event.run_id) for event in events if event.run_id} == {run_id}
                    and len(user_messages) == 1
                    and bool(tool_calls)
                ),
                detail=(
                    f"run_id={run_id or '缺失'} user/message={len(user_messages)}"
                    f"（同 run 续跑不落新任务文本）tool_calls={len(tool_calls)}"
                    f" 事件里的 run_id 集合={sorted({str(e.run_id) for e in events if e.run_id})}"
                ),
            ),
            AssertionResult(
                name="resume_contract_holds",
                ok=resume_contract_ok,
                detail=(
                    "换新时刻后的恢复契约（`#312` T4 的版本合同）："
                    f"run/resumed={len(resumed_events)} 同一 run_id、版本 1→2、"
                    f"from_pause_seq={rdata.get('from_pause_seq') if arm == 'safe' else '（未恢复）'}、"
                    f"无新 run/started、无新 user/message={len(user_messages) == 1}"
                ),
            ),
            AssertionResult(
                name="resumed_leg_admitted_new_work",
                ok=new_work_ok,
                detail=(
                    "恢复后新窗口里被接纳的工作："
                    f"Provider 请求={after_resume.requests if arm == 'safe' else '（未恢复）'}、"
                    f"tool/call={tool_calls_after_resume if arm == 'safe' else '（未恢复）'}"
                    "（判据只要求 ≥1 条接纳：新时刻真的把 run 重新打开。"
                    "实测残余：模型可能选择在恢复后直接收尾 ⇒ tool/call=0，"
                    "这条读数照实留在证据里，残余登记见 ADR-0046）"
                ),
            ),
            AssertionResult(
                name="run_ended_in_a_safe_state",
                ok=ending_ok,
                detail=f"收尾形状={ending_label}（暂停/完成都安全，失败与中断判红）",
            ),
            AssertionResult(
                name="final_budget_state",
                ok=(
                    state is not None and replay_state is not None
                    and state.version == (1 if arm == "needs_reconcile" else 2)
                    and (state.paused is not None) == expected_paused
                    and state.terminal is expected_terminal
                ),
                detail=(
                    f"派生账本 version={getattr(state, 'version', None)}"
                    f"（{arm} 结局下期望 {1 if arm == 'needs_reconcile' else 2}）"
                    f" paused={getattr(state, 'paused', None) is not None}"
                    f"（期望 {expected_paused}：终态压过暂停，两者互斥）"
                    f" terminal={getattr(state, 'terminal', None)}（期望 {expected_terminal}）"
                ),
            ),
            AssertionResult(
                name="durable_replay_matches_live",
                ok=(
                    state is not None and replay_state is not None
                    and state == replay_state
                    and [(event.seq, event.type) for event in events]
                    == [(event.seq, event.type) for event in replayed]
                ),
                detail=(
                    f"重新读盘 {len(replayed)} 条事件与内存态逐条同序；"
                    "派生账本（version / limits / consumed / paused / terminal）一致"
                ),
            ),
            AssertionResult(
                name="stream_mirrors_pause",
                ok=(
                    [str(event.type) for event in legs["first_stream"]].count(RUN_PAUSED) == 1
                    and len(pauses) >= 1
                ),
                detail=(
                    "首次执行的 live 流里 run/paused="
                    f"{[str(e.type) for e in legs['first_stream']].count(RUN_PAUSED)}；"
                    f"续跑执行的 live 流={[str(e.type) for e in legs['resume_stream']][-3:]}"
                ),
            ),
            AssertionResult(
                name="no_fuse_trip", ok=not fuse_tripped,
                detail=f"轨迹里{'存在' if fuse_tripped else '没有'} {FUSE_TRIP_MARKER}",
            ),
            AssertionResult(
                name="no_dangling_tool_calls_in_run",
                ok=not dangling_tool_call_ids(events),
                detail=f"悬空 call={dangling_tool_call_ids(events)}",
            ),
            AssertionResult(
                name="session_identity_present",
                ok=bool(ctx.session_id) and bool(run_id) and bool(tool_calls),
                detail=(
                    f"session_id={'有' if ctx.session_id else '无'} "
                    f"run_id={'有' if run_id else '无'} tool_calls={len(tool_calls)}"
                ),
            ),
        ]

    @staticmethod
    def _continuation_tail(pause: Any) -> str:
        """暂停的 continuation（`next_safe_action`）作为证据尾巴：人读时最想先看到的那句。"""
        if pause is None:
            return ""
        continuation = pause.data.get("continuation")
        if not isinstance(continuation, dict):
            return ""
        return str(continuation.get("next_safe_action") or "")[:2000]


SCENARIO = RunDeadlineBoundaryScenario()
