"""内置场景：低预算暂停 → 抬高**绝对** ceiling → **同一 run_id** 跑完（`#312` T4 的核心验收）。

## 它证明什么（三条结构事实，不是"看起来跑通了"）

1. **暂停是预算撞线的结构结果**：首次执行的 run 作用域 ceiling 取 `LOW_CEILING = 2`，
   而准入判定是 `consumed + 1 >= ceiling`、位置在任何 model / tool / child 工作**之前**
   （`02 §5.1`）⇒ 只要首轮真的产出了东西，第 2 次准入必然命中
   `run.max_agent_turns_total`。判据是机械的（不含"模型愿不愿意"）：若模型第一轮就
   给出最终回答（= 一步都没干活），它不会暂停——而那种情况立刻被
   `chain_completed_after_resume` 判红（链一步没推进），不会变成假通过。
2. **恢复沿用同一 run，且由生产代码落盘**：续跑走 `SessionService.resume_and_launch`
   （Web / CLI 的**同一条** CAS 路径）⇒ `run/resumed` 不是本场景手写的。断言钉住
   `from_pause_seq` / `previous_budget_version` / `consumed` 快照 / `resume_basis` /
   抬高后的绝对 ceiling，以及全程只有一条 `run/started`、一条 `user/message`
   （同 run 续跑不落新任务文本）。两次调用的形状也顺带被真实走了一遍：首轮带 `task`
   （新任务入口——空会话上就是同一条 `resume_and_launch`），续跑不带 `task` 但带
   `run_id` + `expected_version`（同 run 形态）；两者的可分辨性是 `validate_resume`
   的形状闸门，不是本场景自定的规矩。
3. **暂停前干过活、恢复后接着干**：链任务的信息屏障（见下）保证"还剩几格"只可能由
   后续执行完成。断言里"暂停之后仍有 tool/call"与"链走满 + 产物等于最终串"两条合起来，
   排除"暂停时其实已经做完、恢复只是走个过场"的假通过。

## 为什么任务必须逐步（信息屏障）

与 `long_task.py` 同一论证（那边证的是"必须越过旧 10 轮上限"，这边证的是"必须跨过暂停点"）：
`chain.py` 只有参数等于 `chain.txt` 当前串时才推进，推进结果是**新随机串**
（`secrets.token_hex(4)`，不可预计算），且一次调用最多推进一格。于是"下一步要传什么"
只由上一条命令的 stdout 产生。4 格 ⇒ 至少 4 次独立调用；首次执行只有 1 个产出轮
（`LOW_CEILING=2` 的必然结果）⇒ 至少 3 格只能发生在恢复之后。

链脚本的键名刻意是 `next=` 而不是 `token=`：`live_gate/secrets.py` 的赋值形态扫描会把
`token=<长串>` 读成凭证回显（`long_task.py` 的模块 docstring 记录了那次实测），
而安全边界不为取证方便让步 —— 这里沿用同一条命名。

## 为什么断言里没有"模型措辞"

判据全部机械可检：事件类型 / 计数 / 字段形状 / 工作区文件内容（`chain-steps.txt` 与
`done.txt` 的比对不必复算随机值）。continuation 只钉**契约形状**（四个键齐、`next_safe_action`
非空），不评内容好坏 —— 本场景不引入 LLM judge（`runner.scope.does_not_cover` 已登记）。

## 生产性来自哪里（这条决定证据算不算数）

- **暂停 / 恢复面**：`AppState(settings)` + `session_service(state)` —— 与 Web 应用、CLI
  `resume` 命令**同一个组合根**（`SessionService` 的唯一合法构造点，由
  `tests/session/test_service_collaborators.py` 机械守着）。
- **模型 / 工具 / 沙箱**：与 smoke / long_task 同源 —— `build_runtime` 装配的生产工具集、
  生产 Sandbox、真实 provider 客户端。
- **会话身份**：每次尝试有一个**固定的** `ctx.session_id`（runner 按它归档轨迹：
  `session_root/<session_id>/events.jsonl`），而 `create_and_launch` 自己生成 uuid ⇒
  本场景用同一套生产原语自建会话（`WorkspaceRegistry.create(session_id, workspace_root=…)`
  + `Session.start(cwd=…)`；前者正是 `build_runtime` 内部绑定 worktree 的同一调用形状）。
  这不是"绕过服务"：暂停 / 恢复的判定、`run/paused`、`run/resumed`、CAS 全部仍发生在
  `SessionService` 里。

## 一次性边界

运行时产物（会话轨迹 / SQLite / workspace 登记）全部落在一次尝试的临时根目录内
（`settings.workspace_dir` 重定向到 `ctx.session_root.parent`，见 `_scenario_settings`），
跑完由 `workspace.teardown()` 连同根目录一起销毁并核实。可选能力（CAPABILITIES）**沿用
部署配置**：生产装配路径按 `wire_capabilities` 的既有纪律装配它们（外部依赖故障按
OPTIONAL 降级，`08 §7`）。

## 账本面（`#313` T5）

暂停快照 / `run/resumed` 快照 / API 投影（`SessionService.budget_projection`）里的四维
读数，与**轨迹重算**结果逐维对账（判据与理由见 `accounting.py`）。暂停是"账在恢复基数上
必须完全一致"的现场：`model_requests` 要数进 closeout 那次请求、不得并进 `agent_turns`；
token / cost 只认 Provider 自报，缺一格就是未知而不是 0。

## 受控 primary 失败（`--inject-failure primary-failure`）

AC-9 要的是"受控 primary 失败 → **真实配置的** fallback 接管"的证据。本场景在收到该注入
标记时只改**一个**字段：把 primary 的 `base_url` 指到 `.invalid` 保留域（RFC 2606，必然解析
失败 ⇒ 真实发出的一次连接失败请求，瞬时故障分类 ⇒ policy 放行切换）。fallback 一侧
**逐字沿用部署配置**，两级链由生产 `build_runtime` 装配——不是本场景手搭的模型链。

这条运行**不得**被当成 3/3 的一部分：`injected_failure` 非空 ⇒ runner 与 validator 两边都把
判定锁到 `FAIL`（`schema.decide_verdict` / `validator._recompute_verdict`），它产出的是
**证据**（两侧 model id + 请求计数 + 未自报 usage 的未知语义），不是通过。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from evaluation.assertions import dangling_tool_call_ids
from evaluation.live_gate.registry import AttemptOutcome, ScenarioContext
from evaluation.live_gate.scenarios.accounting import (
    consumed_counter_assertions,
    request_accounting,
)
from evaluation.live_gate.schema import AssertionResult

SCENARIO_ID = "budget-pause-resume-same-run"
#: v2（`#313` T5）：断言集加入四维账本对账 + 受控 primary 失败的证据面（下面的注入标记）。
SCENARIO_VERSION = 2

#: 受控 primary 失败的注入标记（`python scripts/live_gate.py run --inject-failure primary-failure`）。
#: 它**不**走 `attempt:N` 那条"合成一次失败尝试"的路（那是给 validator 自证用的），而是由本
#: 场景消费：见模块 docstring「受控 primary 失败」。判定仍被锁到 `FAIL`（证据，不是通过）。
PRIMARY_FAILURE_INJECTION = "primary-failure"

#: 受控失败用的 primary 端点：`.invalid` 是 RFC 2606 保留域 ⇒ DNS 必然解析失败
#: （不依赖本机某个端口恰好没人监听，也不赌 Provider 的错误语义）。
BROKEN_PRIMARY_BASE_URL = "http://live-gate-primary.invalid/v1"

#: 首次执行的 run 作用域**绝对** ceiling。2 = 「1 个产出轮 + 为 closeout 预留的那一轮」：
#: 准入判定是 `consumed + 1 >= ceiling`，所以在首轮之后必然命中（暂停是**结构**结果，
#: 与模型怎么选无关）。取 1 会让首轮都不发生，证据里就没有"暂停前真的干过活"这一面
#: ——而那正是"恢复不是走过场"的前提。
LOW_CEILING = 2

#: 恢复时给出的绝对 ceiling（**绝对值，不是增量** —— PRD §3 明文，场景不做加法）。
#: 恢复后可接纳的轮数 = `ceiling - consumed - 1` = 12 − 1 − 1 = 10 ≥ 9（
#: `consumed = LOW_CEILING - 1 = 1`）：4 格链从任何断点都跑得完，同时**不是**无限预算
#: ——"恢复后再次撞线"这种真回归仍然可见（撞线会让 `run/paused` 变成两条，断言如实判红）。
RESUME_CEILING = 12

#: 链步数 = 模型决策数下界（见模块 docstring 的信息屏障）。
TRANSITIONS = 4

#: 单次执行的收口上限（秒）。两次执行合计远低于 `runner.ATTEMPT_TIMEOUT`（900s），
#: 到点判 FAIL（不是"跳过"）。
PHASE_TIMEOUT = 300.0

CHAIN_SCRIPT = "chain.py"
TOKEN_FILE = "chain.txt"
STEPS_FILE = "chain-steps.txt"
DONE_FILE = "done.txt"
SEED_TOKEN = "seed0"

#: local fuse 兜底终态的标记（`agent/runtime.py` 的 `STATUS_MAX_STEPS_EXCEEDED`）：
#: 本场景的低 ceiling 走的是 `run/paused`，轨迹里出现这个标记就是走错了路。
FUSE_TRIP_MARKER = "max_steps_exceeded"

#: 真实模型调用的单次超时（秒）。与 smoke / long_task 同值：这是一次完整 agent run。
REQUEST_TIMEOUT = 180.0

_CHAIN_SCRIPT_SOURCE = '''"""Live Gate 预算暂停/恢复链（#312）：一次调用推进一格，推进串随机生成。

契约：`python chain.py <当前串>`，与 chain.txt 一致才推进——新串由 secrets 生成并写回
chain.txt，步数 +1；不一致则非零退出且**不推进**。

键名用 `next` 而不是 `token`：`live_gate/secrets.py` 的赋值形态扫描会把
`token=<≥16 字符>` 读成凭证回显（见 long_task.py 的实测记录），安全边界不为此让步。
"""

import pathlib
import secrets
import sys

link_file = pathlib.Path("chain.txt")
steps_file = pathlib.Path("chain-steps.txt")

if not link_file.exists():
    sys.exit("chain.txt 缺失：工作区未被正确 seed")
current = link_file.read_text().strip()
if len(sys.argv) < 2 or sys.argv[1].strip() != current:
    sys.exit("串不匹配：必须把当前串作为参数传入（先读上一条输出，不要猜）")

new_link = secrets.token_hex(4)
link_file.write_text(new_link)
steps = int((steps_file.read_text().strip() or "0")) + 1
steps_file.write_text(str(steps))
print(f"step={steps} next={new_link}")
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
    "  - 不要探索其它文件，不要写别的文件；最后用一句话汇总，不要长篇输出。"
)


def _safe_read(sandbox: Any, name: str) -> str:
    """读工作区文件；读不到返回空串（**读失败也是判据的一种取值**，不是异常）。"""
    try:
        return sandbox.read_text(name)
    except Exception:  # noqa: BLE001 - 产物缺失 → 后续断言自然不过
        return ""


def _event_text(event: Any) -> str:
    """事件的文字面（用于扫 `max_steps_exceeded` 这类标记）。取不到就退化成类型名。"""
    try:
        return json.dumps({"type": event.type, "data": event.data}, default=str)
    except Exception:  # noqa: BLE001 - 事件序列化失败不该让整次尝试崩掉
        return str(getattr(event, "type", ""))


def _agent_turns(raw: Any) -> int | None:
    """`consumed` 快照 → `agent_turns`（缺键/畸形 = None，不猜 0）。"""
    value = raw.get("agent_turns") if isinstance(raw, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _run_scope_ceiling(raw_limits: Any) -> int | None:
    """`limits` 快照 → run 作用域的 ceiling（缺键 = 无 ceiling，**不是** 0）。"""
    scope = raw_limits.get("run") if isinstance(raw_limits, dict) else None
    value = scope.get("max_agent_turns_total") if isinstance(scope, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _scenario_settings(ctx: ScenarioContext) -> Any:
    """把一次性根目录当成本次运行的运行时目录（会话轨迹 / SQLite / workspace 登记）。

    两条理由，都是取证纪律：

    1. **轨迹必须落在 `ctx.session_root` 下** —— runner 只从
       `session_root/<session_id>/events.jsonl` 归档轨迹（`runner._copy_events`），
       而这棵树的形状由 `settings.workspace_dir` 决定（`AppState.sessions_root`）；
    2. `Settings` 的相对默认值（`.agent/workspace` / `.agent/artifacts`）按**进程 CWD**
       解析 —— 那就是开发仓库。Live Gate 的 `repo_unchanged` 要求跑前跑后仓库指纹一致，
       往仓库里写运行时产物是另一类副作用（即使被 .gitignore 盖住也不该发生）。

    **其余字段逐字沿用部署配置**（含 CAPABILITIES / 模型链 / 审批超时）：本场景要跑的
    就是生产装配路径，只在"运行时产物落哪"这一项上重定向。

    受控 primary 失败（`--inject-failure primary-failure`，见模块 docstring）时**多改一个
    字段**：primary 的 `base_url` 指向 `.invalid`。`model_provider` / `model_name` /
    `model_api_key` 与**整条 fallback 配置**逐字不动——所以证据里的两侧 id 与部署配置一致，
    而 primary 那次失败是"真实发出、真实连不上"，不是替身假装的。
    """
    root = ctx.session_root.parent
    overrides: dict[str, Any] = {
        "workspace_dir": str(root),
        "artifact_dir": str(root / "artifacts"),
    }
    if ctx.injected_failure == PRIMARY_FAILURE_INJECTION:
        overrides["model_base_url"] = BROKEN_PRIMARY_BASE_URL
    return ctx.settings.model_copy(update=overrides)


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


class BudgetPauseResumeSameRunScenario:
    """真实模型：低预算暂停一次 → 抬高绝对 ceiling → 同一 run_id 跑完。"""

    id = SCENARIO_ID
    version = SCENARIO_VERSION
    description = (
        "低预算（ceiling=2）暂停一次 → 生产 CAS 面抬高绝对 ceiling 恢复 → 同一 run_id"
        "（无新 run/started、无新 user/message）跑完链任务；断言钉暂停快照 / 恢复字段 /"
        "续跑工作量 / 终态"
    )

    async def prepare(self, ctx: ScenarioContext) -> list[str]:
        """seed 链脚本与初值 + 前置自查。返回未满足的前置（非空 ⇒ BLOCKED，不发 run）。"""
        probe = ctx.sandbox.exec("python --version")
        if probe.exit_code != 0:
            return [
                (
                    f"工作区里没有可用的 python（rc={probe.exit_code}）——本场景的链脚本要在"
                    "沙箱内执行，缺它不构成 FAIL 而是环境不具备"
                ),
            ]
        ctx.sandbox.write_text(CHAIN_SCRIPT, _CHAIN_SCRIPT_SOURCE)
        ctx.sandbox.write_text(TOKEN_FILE, SEED_TOKEN)
        ctx.sandbox.write_text(STEPS_FILE, "0")
        return []

    async def run(self, ctx: ScenarioContext) -> AttemptOutcome:
        """跑一次完整流程。**异常一律转 `ok=False`**（runner 兜底捕获是第二道）。

        顺序即证据：低预算执行 → 读 durable `run/paused` → 生产 CAS 面恢复 → 续跑执行 →
        读 durable 终态。中间任何一步不成立都如实 `ok=False`，不"重试到通过"。
        """
        from agent_harness.agent.run_budget import (
            RESUME_BASIS_BUDGET_INCREASE,
            latest_paused_run,
        )
        from agent_harness.session import JsonlSessionStore, Session
        from agent_harness.session.event import RUN_COMPLETED
        from agent_harness.web.app import AppState, session_service

        settings = _scenario_settings(ctx)
        state = AppState(settings)
        service = session_service(state)
        try:
            store = JsonlSessionStore(root=ctx.session_root)
            # 会话身份 = Live Gate 给的 id（轨迹归档按它找）；worktree 绑定用生产
            # `build_runtime` 的同一调用形状（见模块 docstring"生产性来自哪里"）。
            state.workspace_registry.create(
                ctx.session_id, workspace_root=ctx.sandbox.workspace_root,
            )
            Session.start(
                store, session_id=ctx.session_id, cwd=ctx.sandbox.workspace_root,
            )

            first = await service.resume_and_launch(
                session_id=ctx.session_id, task=TASK,
                run_max_agent_turns_total=LOW_CEILING,
            )
            stream_pause = await _drain(first, service=service, phase="低预算执行")
            events = await service.get_events(ctx.session_id)
            paused = latest_paused_run(events)
            if paused is None:
                raise RuntimeError(
                    "低预算执行没有留下可恢复的暂停（run/paused）——场景前提不成立，"
                    "此时不做恢复（不做「假恢复」）"
                )

            resumed = await service.resume_and_launch(
                session_id=ctx.session_id, task=None,
                resume_run_id=paused.run_id,
                resume_basis=RESUME_BASIS_BUDGET_INCREASE,
                run_max_agent_turns_total=RESUME_CEILING,
                expected_version=paused.version,
            )
            stream_resume = await _drain(resumed, service=service, phase="同 run 续跑执行")
        except Exception as error:  # noqa: BLE001 - 失败要如实记录（runner 统一脱敏）
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id,
                error=f"{type(error).__name__}: {error}",
            )
        finally:
            # 关闭可选能力（记忆形成泵 / MCP / 外部连接）与在途 run —— 一次尝试一个
            # 生命周期，不把连接与后台任务带进下一次尝试。
            await state.shutdown()

        # 终态读盘必须在 `shutdown()` **之后**（`#312` 实测的坑）：可选能力的收尾会
        # **追加 durable 事实** —— 本环境里 `memory/degraded` 就落在 `run/completed`
        # 之后（`MemoryWriteback.close()` 先 drain 在途写回、再关连接，降级事件由那条
        # 写回任务落盘）。早读会让判定所依据的事件比 runner 复制的那份轨迹少一行，
        # 于是 `event_count` 与轨迹行数对不上 —— `validator.py` 的「记录值 ↔ 轨迹行数」
        # 比对会如实判 FAIL（第一次真实运行时 attempt 2 就是这样被抓住的）。
        # `shutdown()` 幂等，finally 里那次照旧：异常 / 取消路径也要关。
        try:
            events = await service.get_events(ctx.session_id)
            # 独立复读（新的 store 实例、重新读盘）：durable 事实与内存态一致性的证据面
            replayed = store.read_events(ctx.session_id)
            # API 投影（`#313` T5，`11 §6.1` 的客户端面）：四维读数与水位的**服务端**读数，
            # 与轨迹重算对账。它**只读**（`budget_projection` 不写事件），所以放在读盘之后。
            projection = await service.budget_projection(ctx.session_id)
        except Exception as error:  # noqa: BLE001 - 同上的如实记录
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id,
                error=f"{type(error).__name__}: {error}",
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
            projection=projection,
            streams={
                "pause": [str(event.type) for event in stream_pause],
                "resume": [str(event.type) for event in stream_resume],
            },
        )
        completed = [event for event in events if event.type == RUN_COMPLETED]
        final_text = str(completed[0].data.get("final_text", "")) if completed else ""
        return AttemptOutcome(
            ok=all(item.ok for item in assertions),
            session_id=ctx.session_id,
            run_id=run_id,
            # 逻辑 run 的终态读 durable 事实（暂停不是终态；完成才是）
            run_status="completed" if completed else "unknown",
            steps=sum(1 for event in events if event.type == "model/completed"),
            tool_calls=tool_calls,
            assertions=assertions,
            event_count=len(events),
            output_tail=final_text[-2000:],
        )

    def _assertions(
        self, *, ctx: ScenarioContext, events: list[Any], replayed: list[Any],
        tool_calls: list[str], streams: dict[str, list[str]],
        projection: dict[str, Any] | None = None,
    ) -> list[AssertionResult]:
        """结构性判据（机械可检，不是 LLM judge）。"""
        from agent_harness.agent.run_budget import (
            REASON_BUDGET_EXHAUSTED,
            TRIGGER_RUN_TURNS,
            derive_run_budget,
        )
        from agent_harness.session.event import (
            RUN_COMPLETED,
            RUN_FAILED,
            RUN_INTERRUPTED,
            RUN_PAUSED,
            RUN_RESUMED,
            RUN_STARTED,
            USER_MESSAGE,
        )

        terminal_types = (RUN_COMPLETED, RUN_FAILED, RUN_INTERRUPTED)
        paused_events = [event for event in events if event.type == RUN_PAUSED]
        resumed_events = [event for event in events if event.type == RUN_RESUMED]
        completed_events = [event for event in events if event.type == RUN_COMPLETED]
        started_events = [event for event in events if event.type == RUN_STARTED]
        user_messages = [event for event in events if event.type == USER_MESSAGE]
        run_id = str(started_events[0].run_id or "") if started_events else ""
        run_ids = {str(event.run_id) for event in events if event.run_id}

        paused = paused_events[0] if paused_events else None
        pdata = dict(paused.data) if paused is not None else {}
        p_limits = pdata.get("limits")
        closeout = str(pdata.get("closeout_source") or "")
        consumed_at_pause = _agent_turns(pdata.get("consumed"))
        # 账（`02 §5.1` 的七个 counter 互不混同）：`agent_turns` 只数被接纳的产出轮，
        # ceiling=N 放行 N-1 轮（判定含预留）；closeout 那一次是 `model_requests`，
        # **不**进这个快照。它的位置由预留表达（`02 §5.2`）。
        expected_consumed = LOW_CEILING - 1
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
        local_scope = p_limits.get("local") if isinstance(p_limits, dict) else None
        local_ok = (
            isinstance(local_scope, dict)
            and isinstance(local_scope.get("max_agent_turns"), int)
            and not isinstance(local_scope.get("max_agent_turns"), bool)
            and bool(str(local_scope.get("source") or ""))
        )
        no_terminal_before_pause = all(
            event.seq > paused.seq
            for event in events
            if event.type in terminal_types
        ) if paused is not None else False

        resumed = resumed_events[0] if resumed_events else None
        rdata = dict(resumed.data) if resumed is not None else {}
        resumed_consumed = _agent_turns(rdata.get("consumed"))

        state = derive_run_budget(events, run_id) if run_id else None
        replay_state = derive_run_budget(replayed, run_id) if run_id else None
        tool_calls_after_pause = sum(
            1 for event in events
            if event.type == "tool/call" and paused is not None and event.seq > paused.seq
        )

        steps_recorded = _safe_read(ctx.sandbox, STEPS_FILE).strip()
        final_token = _safe_read(ctx.sandbox, TOKEN_FILE).strip()
        done_content = _safe_read(ctx.sandbox, DONE_FILE)
        fuse_tripped = any(FUSE_TRIP_MARKER in _event_text(event) for event in events)
        dangling = dangling_tool_call_ids(events)
        # 账本面（`#313` T5）：暂停快照是**恢复的基数**、API 投影是**客户端读到的**那一份，
        # 两者都要与轨迹重算逐维相同（判据与理由见 `accounting.py`）。
        pause_cutoff = paused.seq if paused is not None else None
        pause_facts = request_accounting(
            [event for event in events if pause_cutoff is None or event.seq <= pause_cutoff]
        )
        run_facts = request_accounting(events)
        projection_consumed = (
            projection.get("consumed") if isinstance(projection, dict) else None
        )
        induced = ctx.injected_failure == PRIMARY_FAILURE_INJECTION
        return [
            AssertionResult(
                name="pause_exactly_once", ok=len(paused_events) == 1,
                detail=(
                    f"run/paused={len(paused_events)}（恰好一条 = 低预算处暂停一次，"
                    "且续跑**没有**再次撞线）"
                ),
            ),
            AssertionResult(
                name="pause_reason_and_dimension",
                ok=(
                    str(pdata.get("reason")) == REASON_BUDGET_EXHAUSTED
                    and str(pdata.get("trigger_dimension")) == TRIGGER_RUN_TURNS
                ),
                detail=(
                    f"reason={pdata.get('reason')!r} "
                    f"trigger_dimension={pdata.get('trigger_dimension')!r}"
                ),
            ),
            AssertionResult(
                name="pause_budget_snapshot",
                ok=(
                    pdata.get("budget_version") == 1
                    and _run_scope_ceiling(p_limits) == LOW_CEILING
                    and local_ok
                    and "trace_id" in pdata
                    and list(pdata.get("resume_requirements") or []) == []
                ),
                detail=(
                    f"version={pdata.get('budget_version')} "
                    f"run.limits={_run_scope_ceiling(p_limits)}（期望 {LOW_CEILING}）"
                    f" local={'有 ceiling+source' if local_ok else '缺'} "
                    f"trace_id键={'有' if 'trace_id' in pdata else '无'} "
                    f"resume_requirements={pdata.get('resume_requirements')!r}"
                ),
            ),
            AssertionResult(
                name="pause_consumed_accounting",
                ok=consumed_at_pause == expected_consumed and closeout in ("model", "deterministic"),
                detail=(
                    f"consumed={consumed_at_pause} closeout_source={closeout!r}"
                    f"（期望 {expected_consumed} = {LOW_CEILING - 1} 个产出轮；"
                    "closeout 是 model_requests，不进 agent_turns）"
                ),
            ),
            *consumed_counter_assertions(
                facts=pause_facts, consumed=pdata.get("consumed"), label="pause",
            ),
            AssertionResult(
                name="pause_continuation_contract", ok=continuation_ok,
                detail=f"continuation 四键形状（next_safe_action 非空）：{'齐' if continuation_ok else '不齐'}",
            ),
            AssertionResult(
                name="pause_is_not_terminal", ok=no_terminal_before_pause and len(started_events) == 1,
                detail=(
                    f"暂停之前无终态事件；run/started={len(started_events)}"
                    "（暂停是本次执行收口，逻辑 run 仍开着）"
                ),
            ),
            AssertionResult(
                name="resume_same_run_snapshot",
                ok=(
                    len(resumed_events) == 1
                    and resumed is not None
                    and resumed.run_id == run_id
                    and rdata.get("from_pause_seq") == (paused.seq if paused is not None else None)
                    and rdata.get("previous_budget_version") == 1
                    and rdata.get("budget_version") == 2
                    and str(rdata.get("resume_basis")) == "budget_increase"
                    and _run_scope_ceiling(rdata.get("limits")) == RESUME_CEILING
                ),
                detail=(
                    f"run/resumed={len(resumed_events)} "
                    f"from_pause_seq={rdata.get('from_pause_seq')}"
                    f"（暂停 seq={paused.seq if paused is not None else None}）"
                    f" version={rdata.get('previous_budget_version')}→{rdata.get('budget_version')}"
                    f" basis={rdata.get('resume_basis')!r}"
                    f" run.limits={_run_scope_ceiling(rdata.get('limits'))}（期望 {RESUME_CEILING}）"
                ),
            ),
            AssertionResult(
                name="resume_does_not_reset_consumed",
                ok=resumed_consumed == consumed_at_pause and resumed_consumed is not None,
                detail=(
                    f"run/resumed.consumed={resumed_consumed} "
                    f"== 暂停快照 {consumed_at_pause}（恢复不重置、也不预支）"
                ),
            ),
            AssertionResult(
                name="run_identity_single",
                ok=(
                    bool(run_id)
                    and run_ids == {run_id}
                    and len(started_events) == 1
                    and len(user_messages) == 1
                    and len(completed_events) == 1
                    and completed_events[0].run_id == run_id
                ),
                detail=(
                    f"run_id={run_id or '缺失'} 事件里的 run_id 集合={sorted(run_ids)}"
                    f" run/started={len(started_events)} user/message={len(user_messages)}"
                    f" run/completed={len(completed_events)}"
                ),
            ),
            AssertionResult(
                name="resumed_execution_did_work", ok=tool_calls_after_pause >= 1,
                detail=(
                    f"暂停之后仍有 {tool_calls_after_pause} 次 tool/call"
                    "（信息屏障保证剩余链步只能发生在恢复之后）"
                ),
            ),
            AssertionResult(
                name="chain_completed_after_resume",
                ok=steps_recorded == str(TRANSITIONS) and bool(done_content) and done_content.strip() == final_token,
                detail=(
                    f"{STEPS_FILE}={steps_recorded or '缺失'}（期望 {TRANSITIONS}）；"
                    f"{DONE_FILE} 与 {TOKEN_FILE} 比对（缺失或不等即不通过）"
                ),
            ),
            AssertionResult(
                name="final_budget_state",
                ok=(
                    state is not None and replay_state is not None
                    and state.version == 2
                    and state.limits.max_agent_turns_total == RESUME_CEILING
                    and state.consumed_turns >= (consumed_at_pause or 0) + 1
                    and state.paused is None
                    and state.terminal
                ),
                detail=(
                    f"派生账本 version={getattr(state, 'version', None)}"
                    f" ceiling={state.limits.max_agent_turns_total if state else None}"
                    f" consumed={getattr(state, 'consumed_turns', None)}"
                    f"（暂停快照 {consumed_at_pause} + 恢复后的产出轮）"
                    f" paused={getattr(state, 'paused', None) is not None}"
                    f" terminal={getattr(state, 'terminal', None)}"
                ),
            ),
            *consumed_counter_assertions(
                facts=run_facts, consumed=projection_consumed, label="api_projection",
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
                name="stream_mirrors_pause_and_completion",
                ok=(
                    streams.get("pause", []).count(RUN_PAUSED) == 1
                    and streams.get("resume", []).count(RUN_COMPLETED) == 1
                    and RUN_RESUMED not in streams.get("pause", [])
                ),
                detail=(
                    f"低预算执行的 live 流里 run/paused="
                    f"{streams.get('pause', []).count(RUN_PAUSED)}；"
                    f"续跑执行的 live 流里 run/completed="
                    f"{streams.get('resume', []).count(RUN_COMPLETED)}"
                    "（`run/resumed` 在 launch 之前落盘、不在 live 窗口内，"
                    "与既有 `session/resumed` 同形）"
                ),
            ),
            AssertionResult(
                name="no_fuse_trip", ok=not fuse_tripped,
                detail=f"轨迹里{'存在' if fuse_tripped else '没有'} {FUSE_TRIP_MARKER}",
            ),
            AssertionResult(
                name="no_dangling_tool_calls", ok=not dangling,
                detail=f"悬空 call={dangling}",
            ),
            AssertionResult(
                name="session_identity_present",
                ok=bool(ctx.session_id) and bool(run_id) and bool(tool_calls),
                detail=(
                    f"session_id={'有' if ctx.session_id else '无'} "
                    f"run_id={'有' if run_id else '无'} tool_calls={len(tool_calls)}"
                ),
            ),
            *(
                _fallback_evidence_assertions(
                    ctx=ctx, events=events, pause_facts=pause_facts,
                )
                if induced else []
            ),
        ]


def _fallback_evidence_assertions(
    *, ctx: ScenarioContext, events: list[Any], pause_facts: Any,
) -> list[AssertionResult]:
    """受控 primary 失败的证据面（只在 `--inject-failure primary-failure` 下出现）。

    三条各自独立：**失败请求真的发生过**（primary 角色 + 失败格）、**真实配置的 fallback
    接了手**（`model/fallback` 的两侧名字 == 部署配置，且有 fallback 角色的请求）、
    **缺 usage 的格让该维度转未知而不是 0**（`02 §5.1` / `11 §6.1` 的语义在真实数据上成立）。
    判定被锁到 `FAIL`（注入）——这三条是"这次运行的证据读得懂"，不是"这次运行通过"。
    """
    from agent_harness.session import MODEL_FALLBACK

    from_model = str(getattr(ctx.settings, "model_name", "") or "")
    to_model = str(getattr(ctx.settings, "fallback_model_name", "") or "")
    transitions = [event for event in events if event.type == MODEL_FALLBACK]
    reached = [
        event for event in transitions
        if str(event.data.get("from_model") or "") == from_model
        and str(event.data.get("to_model") or "") == to_model
    ]
    return [
        AssertionResult(
            name="fallback_primary_failure_recorded",
            ok=pause_facts.failed >= 1 and pause_facts.by_role.get("primary", 0) >= 1,
            detail=(
                f"受控 primary={from_model or '?'}；{pause_facts.summary()}"
                "（失败的那次请求占 model_requests 一席，不增 agent_turns）"
            ),
        ),
        AssertionResult(
            name="fallback_used_configured_provider",
            ok=bool(reached) and pause_facts.by_role.get("fallback", 0) >= 1,
            detail=(
                f"model/fallback 事件 {len(transitions)} 条，其中 "
                f"{from_model or '?'} → {to_model or '?'} 的 {len(reached)} 条；"
                f"fallback 角色的请求 {pause_facts.by_role.get('fallback', 0)} 次"
                "（fallback 一侧逐字沿用部署配置）"
            ),
        ),
        AssertionResult(
            name="unavailable_usage_is_unknown_not_zero",
            ok=pause_facts.tokens is None and pause_facts.cost is None,
            detail=(
                "暂停快照 total_tokens="
                f"{'未知' if pause_facts.tokens is None else pause_facts.tokens}"
                " cost_usd="
                f"{'未知' if pause_facts.cost is None else format(pause_facts.cost, 'f')}"
                "（该 run 有请求未自报 usage ⇒ 未知，不得记为 0）"
            ),
        ),
    ]


SCENARIO = BudgetPauseResumeSameRunScenario()
