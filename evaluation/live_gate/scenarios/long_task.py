"""内置场景：真实模型 + 生产工具完成**必须越过旧 10 轮上限**的长任务（`#308` AC）。

## 为什么这个任务**结构性**需要超过 10 次模型决策

车票要的不是"跑了个比较长的任务"，而是一次**不能**在 10 轮内完成的任务。所以任务的每一步
都建立在**信息屏障**上：工作区里的 `chain.py` 每次调用只有在参数等于 `chain.txt` 里的当前串时
才推进一格，推进结果是**新随机串**（`secrets.token_hex(4)`，不可能被预计算）；一次调用最多
推进一格（每次调用都重新读 `chain.txt`，所以"一次调用推两格"结构上做不到）。

于是"下一步要传什么"这个信息**只由上一次调用产生**——它在 stdout 里给出，同时写回
`chain.txt`（两者同一时刻产生，读哪个都行）。12 格因此需要**至少 12 次独立调用**，而每次
调用都是模型的一次决策 ⇒ `steps ≥ 12`：

- `TRANSITIONS = 12 > 10`：这正是旧 `max_steps=10` 会截断的地方。场景**不**手写任何 fuse
  数字，而是走生产解析路径（`Settings.local_max_agent_turns` → `resolve_local_fuse`）——
  "缺省入口解析出 500"这件事由实现决定，场景只消费它（车票 Must Do：缺省 local fuse=500）。
- 下界而不是等式：模型若用 shell 循环一次跑完 12 格（prompt 明文禁止），`steps` 会 **≤ 10**
  ⇒ `legacy_turn_limit_exceeded` 断言**如实判红**，不洗成 PASS（见下"不证明什么"）。

## 它证明什么 / 不证明什么（诚实边界）

- **证明**：以生产解析出的生效 fuse，一次真实 run 越过旧 10 轮上限并 `completed`；
  轨迹里没有 `max_steps_exceeded`；`model/completed` 计数与 `bash` / `write` 调用都真实发生；
  链状态（`chain-steps.txt`）与产物（`done.txt`）能对上"模型确实逐步观察并执行"。
- **不证明**：模型语义质量；也**不**证明"模型一定不会用 shell 循环一次走完"——prompt 明文
  禁止（`一次 bash 只运行一次 chain.py`），若真发生则 `steps > 10` 这条断言会红：如实判
  FAIL，不洗成 PASS（判据是机械的，不靠相信）。

## 为什么断言钉这两个可机检事实

`chain-steps.txt == 12` 只有在 12 次**正确的串回传**后才成立（script 自己数的）；
`done.txt` 内容 == 最终串只有在模型真的读到了最后一次输出后才成立。两条都不需要
复算随机值，也不需要读模型的话。

## 为什么产物叫 `next` 而不是 `token`（实测踩过，别改回去）

首版把链输出写成 `step=N token=<hex>`，3/3 的第一次尝试**十条断言全绿**却被判 `FAIL`：
`secrets.py` 的赋值形态扫描（`_SECRET_ASSIGNMENT`，键名词表含 `token`）把它读成凭证回显。
误报的直接原因是**正文里 `token=` 后面跟着中文、中间没有空白**，于是 `[^\\s,;\"']{16,}`
把例子里那串连同后面的汉字一起吞进"值"里（≥16 字符 ⇒ 命中），证据因此不落盘。

**处置是改场景的命名，不是放松扫描器**：安全边界不为取证方便让步（`AGENTS` §9.5 红线），
而"日志里 `token=…` 是不是凭证"这件事扫描器**不该**去猜。所以链输出改用 `next=`，prompt 里
的例子也照此写——信息屏障与全部断言一条都没变。

## 账本面（`#313` T5）

12+ 次决策的长路是"计数器在高轮数下不漂移"的**真实样本**：`model_requests` /
`total_tokens` / `cost_usd` 与轨迹重算结果对账（判据与理由见 `accounting.py`），
于是"长跑之后账还对得上"有机械证据，而不靠人看数字。
"""

from __future__ import annotations

import json
from typing import Any

from agent_harness.session import MODEL_COMPLETED, RUN_COMPLETED
from evaluation.assertions import dangling_tool_call_ids
from evaluation.live_gate.registry import AttemptOutcome, ScenarioContext
from evaluation.live_gate.scenarios.accounting import (
    plain_path_request_assertion,
    request_accounting,
    terminal_counter_assertions,
)
from evaluation.live_gate.schema import AssertionResult

SCENARIO_ID = "long-task-past-legacy-turn-limit"
SCENARIO_VERSION = 2

#: 链步数 = 模型决策数下界（见模块 docstring 的信息屏障）。12 > 旧上限 10。
TRANSITIONS = 12

#: 旧产品行为的上限（`max_steps=10`）。它不是配置项：断言记录"越过了多少"用。
LEGACY_TURN_LIMIT = 10

CHAIN_SCRIPT = "chain.py"
TOKEN_FILE = "chain.txt"
STEPS_FILE = "chain-steps.txt"
DONE_FILE = "done.txt"
SEED_TOKEN = "seed0"

#: local fuse 兜底终态的标记（`agent/runtime.py` 的 STATUS_MAX_STEPS_EXCEEDED）。
FUSE_TRIP_MARKER = "max_steps_exceeded"

#: 真实模型调用的单次超时（秒）：本任务 12+ 轮，给足余量。
REQUEST_TIMEOUT = 180.0

_CHAIN_SCRIPT_SOURCE = '''"""Live Gate 长任务链（#308）：一次调用推进一格，推进串随机生成。

契约：`python chain.py <当前串>`，与 chain.txt 一致才推进——新串由 secrets 生成并写回
chain.txt，步数 +1；不一致则非零退出且**不推进**。

输出的键名刻意用 `next` 而不是 `token`：`live_gate/secrets.py` 的赋值形态扫描会把
`token=<≥16 字符>` 读成凭证回显（见模块 docstring 的实测记录），安全边界不为此让步。
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
    "这是一条必须**逐步**完成的链式任务：工作区里已经放好 chain.py 与初值文件 chain.txt。\n"
    "每一步只做一件事：\n"
    "  1) 运行 `python chain.py <当前串>`"
    "（第一次的当前串就是 chain.txt 里的初值，可以先用 bash 读一次 chain.txt）；\n"
    "  2) 从它的输出里拿到新串（形如 `step=N` 与 `next=xxxxxxxx` 两段），"
    "它同时被写进 chain.txt。\n"
    f"重复上面的动作，直到 chain-steps.txt 里的步数达到 {TRANSITIONS}。\n"
    "必须遵守的规则：\n"
    "  - **一次 bash 调用只运行一次 chain.py**：不要用 shell 循环，也不要用 `;` / `&&` "
    "把多次调用拼在一条命令里——串是随机值，只能从上一条命令的输出里读出来；\n"
    "  - 每一步都要真的执行命令并读输出，不要跳步；\n"
    f"  - 全部 {TRANSITIONS} 步完成后，用 write 工具创建 {DONE_FILE}，内容恰好是最终的串"
    "（不要换行、不要引号、不要多余字符）；\n"
    "  - 最后用一句话汇总，不要长篇输出。"
)


def _safe_read(sandbox: Any, name: str) -> str:
    """读工作区文件；读不到返回空串（**读失败本身也是判据的一种取值**，不是异常）。"""
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


class LongTaskPastLegacyTurnLimitScenario:
    """真实长任务：逐格推进 12 步（>旧上限 10），断言链状态与产物一致。"""

    id = SCENARIO_ID
    version = SCENARIO_VERSION
    description = (
        "真实模型经生产解析的 local fuse 驱动 bash/write 逐格推进 12 步（>旧 10 轮上限）；"
        "断言 run 完成、无 max_steps_exceeded、模型请求与工具调用真实发生"
    )

    async def prepare(self, ctx: ScenarioContext) -> list[str]:
        """seed 链脚本与初值 + 前置自查。返回未满足的前置（非空 ⇒ BLOCKED，不发 run）。"""
        from agent_harness.agent.budget import SOURCE_DEPLOYMENT, resolve_local_fuse

        probe = ctx.sandbox.exec("python --version")
        if probe.exit_code != 0:
            return [
                (
                    f"工作区里没有可用的 python（rc={probe.exit_code}）——本场景的链脚本要在"
                    "沙箱内执行，缺它不构成 FAIL 而是环境不具备"
                ),
            ]
        # 前置：部署解析出的缺省 fuse 必须**高于**旧上限——否则本场景要证明的命题
        # （"缺省入口允许越过旧的 10 轮"）在这个部署下不成立，应当 BLOCKED 而不是 FAIL。
        fuse = resolve_local_fuse(deployment=ctx.settings.local_max_agent_turns)
        if fuse.source != SOURCE_DEPLOYMENT or fuse.max_agent_turns <= LEGACY_TURN_LIMIT:
            return [
                (
                    f"缺省 local fuse 解析为 {fuse.max_agent_turns}（source={fuse.source}），"
                    f"未超过旧上限 {LEGACY_TURN_LIMIT}：本场景不适用于这次策略配置"
                ),
            ]
        ctx.sandbox.write_text(CHAIN_SCRIPT, _CHAIN_SCRIPT_SOURCE)
        ctx.sandbox.write_text(TOKEN_FILE, SEED_TOKEN)
        ctx.sandbox.write_text(STEPS_FILE, "0")
        return []

    async def run(self, ctx: ScenarioContext) -> AttemptOutcome:
        """跑一次真实 run。**异常一律转 `ok=False`**（runner 兜底捕获是第二道）。"""
        from agent_harness.agent import AgentRuntime
        from agent_harness.agent.budget import resolve_local_fuse
        from agent_harness.assembly import BUILTIN_LOCAL_TOOLS
        from agent_harness.model.config import ModelConfig
        from agent_harness.model.provider import create_chat_model
        from agent_harness.session import JsonlSessionStore, Session
        from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry
        from agent_harness.tooling.approval import ApprovalResponse

        # 生产解析路径（不手写 fuse 数字）：缺省请求 ⇒ Deployment ceiling（默认 500）。
        # 生效值必须 > 旧上限这件事已在 `prepare` 作为**前置**判定（不满足 ⇒ BLOCKED），
        # 这里只消费结果——不在 run 里再断言一次（那会把"策略不适用"变成一次 FAIL）。
        fuse = resolve_local_fuse(deployment=ctx.settings.local_max_agent_turns)

        store = JsonlSessionStore(root=ctx.session_root)
        try:
            config = ModelConfig.from_settings(ctx.settings)
            model = create_chat_model(config, request_timeout=REQUEST_TIMEOUT)

            registry = ToolRegistry()
            for tool_cls in BUILTIN_LOCAL_TOOLS:
                kwargs: dict[str, Any] = (
                    {"timeout_seconds": ctx.settings.bash_timeout_seconds}
                    if tool_cls.__name__ == "BashTool" else {}
                )
                registry.register(tool_cls(ctx.sandbox, **kwargs))

            async def auto_approve(_request: Any) -> ApprovalResponse:
                return ApprovalResponse(approved=True, reason="live-gate-auto-approve")

            executor = ToolExecutor(
                registry,
                policy=PermissionPolicy.WORKSPACE_WRITE,
                approval_callback=auto_approve,
            )
            session = Session.start(
                store, session_id=ctx.session_id, cwd=ctx.sandbox.workspace_root,
            )
            runtime = AgentRuntime(
                model, registry, executor,
                max_agent_turns=fuse.max_agent_turns,
                primary_model_name=config.model_name,
            )
            result = await runtime.run(session, TASK)
        except Exception as error:  # noqa: BLE001 - 失败要如实记录（runner 统一脱敏）
            return AttemptOutcome(
                ok=False, session_id=ctx.session_id,
                error=f"{type(error).__name__}: {error}",
            )

        # 终态读盘 + **独立复读**（新的 store 实例重新读盘）：两次读盘逐条同序 ==
        # 轨迹在判定时已经静止。这条不是礼节 —— `#312` 实测：可选能力的收尾会
        # **追加** durable 事实（`MemoryWriteback.close()` 在 `run/completed` 之后
        # 落一条 `memory/degraded`），早读会让证据里的事件数比 runner 随后复制的
        # 轨迹少一行，`validator.py` 的「记录值 ↔ 轨迹行数」比对如实判 FAIL ——
        # 那是一次**取证缺陷**，却长得像产品缺陷。
        # 本场景自建运行时（不经 capability 栈 ⇒ 当前没有后写者），复读用来**证明**
        # 这一点而不是假设它。**将来若在此接上 capability 栈**：它的收尾必须排在
        # 读盘之前（`pause_resume.py` 的 `state.shutdown()`，实现于 `089de04`）——
        # 不要把读盘放进 `try` 而把收尾留在 `finally`（那正是假 FAIL 的形状）。
        events = store.read_events(ctx.session_id)
        replayed = JsonlSessionStore(root=ctx.session_root).read_events(ctx.session_id)
        tool_calls = [
            str(event.data.get("tool_name"))
            for event in events
            if event.type == "tool/call"
        ]
        run_id = next(
            (event.run_id or "" for event in events if event.type == "run/started"), "",
        )
        assertions = self._assertions(
            ctx=ctx, run_status=result.status, tool_calls=tool_calls,
            run_id=run_id, events=events, steps=result.steps, fuse=fuse,
            replayed=replayed,
        )
        return AttemptOutcome(
            ok=all(item.ok for item in assertions),
            session_id=ctx.session_id,
            run_id=run_id,
            run_status=result.status,
            steps=result.steps,
            tool_calls=tool_calls,
            assertions=assertions,
            # 记**复读**的条数：它是场景能给出的最后一读，也是 runner 随即将复制的那份
            event_count=len(replayed),
            output_tail=result.final_text[-2000:],
        )

    def _assertions(
        self, *, ctx: ScenarioContext, run_status: str, tool_calls: list[str],
        run_id: str, events: list[Any], steps: int, fuse: Any, replayed: list[Any],
    ) -> list[AssertionResult]:
        """结构性判据（机械可检，不是 LLM judge）。"""
        from agent_harness.agent.budget import SOURCE_DEPLOYMENT

        final_token = _safe_read(ctx.sandbox, TOKEN_FILE).strip()
        steps_recorded = _safe_read(ctx.sandbox, STEPS_FILE).strip()
        done_content = _safe_read(ctx.sandbox, DONE_FILE)
        # `model/completed` 数的是**被接纳的决策**（= `agent_turns`），不是
        # `model_requests`（后者数每一次实际 Provider 请求，`02 §5.1` 两者分开）。
        # 这个局部名原先叫 `model_requests`，在 `#313` 引入真计数器后会造成读反。
        decisions = sum(1 for event in events if event.type == MODEL_COMPLETED)
        fuse_tripped = any(FUSE_TRIP_MARKER in _event_text(event) for event in events)
        dangling = dangling_tool_call_ids(events)
        bash_calls = tool_calls.count("bash")
        facts = request_accounting(events)
        terminal = next(
            (event.data for event in events if event.type == RUN_COMPLETED), None,
        )
        return [
            AssertionResult(
                name="run_completed", ok=run_status == "completed",
                detail=f"run_status={run_status}",
            ),
            AssertionResult(
                name="legacy_turn_limit_exceeded", ok=steps > LEGACY_TURN_LIMIT,
                detail=f"steps={steps}（旧上限 {LEGACY_TURN_LIMIT}）",
            ),
            AssertionResult(
                name="legacy_fuse_not_tripped", ok=not fuse_tripped,
                detail=f"轨迹里{'存在' if fuse_tripped else '没有'} {FUSE_TRIP_MARKER}",
            ),
            AssertionResult(
                name="local_fuse_from_deployment",
                ok=fuse.max_agent_turns > LEGACY_TURN_LIMIT and fuse.source == SOURCE_DEPLOYMENT,
                detail=f"max_agent_turns={fuse.max_agent_turns} source={fuse.source}",
            ),
            AssertionResult(
                name="chain_advanced_all_steps", ok=steps_recorded == str(TRANSITIONS),
                detail=f"{STEPS_FILE}={steps_recorded or '缺失'}，期望 {TRANSITIONS}",
            ),
            AssertionResult(
                name="done_token_matches_final",
                ok=bool(done_content) and done_content.strip() == final_token,
                detail=f"{DONE_FILE} 与 {TOKEN_FILE} 比对（缺失或不等即不通过）",
            ),
            AssertionResult(
                name="model_decisions_exceeded_legacy_limit",
                ok=decisions > LEGACY_TURN_LIMIT,
                detail=(
                    f"model/completed（= agent_turns）={decisions}"
                    f"（旧上限 {LEGACY_TURN_LIMIT}）"
                ),
            ),
            plain_path_request_assertion(facts=facts, label="budget"),
            *terminal_counter_assertions(facts=facts, terminal=terminal, label="budget"),
            AssertionResult(
                name="tool_work_observed",
                ok=bash_calls >= TRANSITIONS and "write" in tool_calls,
                detail=f"bash={bash_calls} write={tool_calls.count('write')}（共 {len(tool_calls)} 次）",
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
                name="session_identity_present",
                ok=bool(ctx.session_id) and bool(run_id),
                detail=f"session_id={'有' if ctx.session_id else '无'} run_id={'有' if run_id else '无'}",
            ),
        ]


SCENARIO = LongTaskPastLegacyTurnLimitScenario()
