"""内置场景：真实模型 + 生产工具完成**必须越过旧 10 轮上限**的长任务（`#308` AC）。

## 为什么这个任务**结构性**需要超过 10 次模型决策

车票要的不是"跑了个比较长的任务"，而是一次**不能**在 10 轮内完成的任务。所以任务的每一步
都建立在**信息屏障**上：工作区里的 `chain.py` 每次调用只有在参数等于 `chain.txt` 里的当前
token 时才推进一格，推进结果是**新随机 token**（`secrets.token_hex(4)`，不可能被预计算）。

于是"下一步要传什么参数"这个信息只存在于**上一条命令的 stdout** 里：

- 一次 bash 调用最多推进一格（要推进必须传入当前 token，而当前 token 只能从上次输出读）；
- 因此完成 `TRANSITIONS` 格 ⇒ **至少** `TRANSITIONS` 次模型决策（每轮一个决策）。

`TRANSITIONS = 12 > 10`：这正是旧 `max_steps=10` 会截断的地方。场景**不**手写任何 fuse
数字，而是走生产解析路径（`Settings.local_max_agent_turns` → `resolve_local_fuse`）——
"缺省入口解析出 500"这件事由实现决定，场景只消费它（车票 Must Do：缺省 local fuse=500）。

## 它证明什么 / 不证明什么（诚实边界）

- **证明**：以生产解析出的生效 fuse，一次真实 run 越过旧 10 轮上限并 `completed`；
  轨迹里没有 `max_steps_exceeded`；`model/completed` 计数与 `bash` / `write` 调用都真实发生；
  链状态（`chain-steps.txt`）与产物（`done.txt`）能对上"模型确实逐步观察并执行"。
- **不证明**：模型语义质量；也**不**证明"模型一定不会用 shell 循环一次走完"——prompt 明文
  禁止（`一次 bash 只运行一次 chain.py`），若真发生则 `steps > 10` 这条断言会红：如实判
  FAIL，不洗成 PASS（判据是机械的，不靠相信）。

## 为什么断言钉这两个可机检事实

`chain-steps.txt == 12` 只有在 12 次**正确的 token 回传**后才成立（script 自己数的）；
`done.txt` 内容 == 最终 token 只有在模型真的读到了最后一次输出后才成立。两条都不需要
复算随机值，也不需要读模型的话。
"""

from __future__ import annotations

import json
from typing import Any

from evaluation.assertions import dangling_tool_call_ids
from evaluation.live_gate.registry import AttemptOutcome, ScenarioContext
from evaluation.live_gate.schema import AssertionResult

SCENARIO_ID = "long-task-past-legacy-turn-limit"
SCENARIO_VERSION = 1

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

_CHAIN_SCRIPT_SOURCE = '''"""Live Gate 长任务链（#308）：一次调用推进一格，token 随机生成。

契约：`python chain.py <当前 token>`，token 与 chain.txt 一致才推进——新 token 由
secrets 生成并写回 chain.txt，步数 +1；不一致则非零退出且**不推进**。
"""

import pathlib
import secrets
import sys

token_file = pathlib.Path("chain.txt")
steps_file = pathlib.Path("chain-steps.txt")

if not token_file.exists():
    sys.exit("chain.txt 缺失：工作区未被正确 seed")
current = token_file.read_text().strip()
if len(sys.argv) < 2 or sys.argv[1].strip() != current:
    sys.exit("token 不匹配：必须把当前 token 作为参数传入（先读上一条输出，不要猜）")

new_token = secrets.token_hex(4)
token_file.write_text(new_token)
steps = int((steps_file.read_text().strip() or "0")) + 1
steps_file.write_text(str(steps))
print(f"step={steps} token={new_token}")
'''

TASK = (
    "这是一条必须**逐步**完成的链式任务：工作区里已经放好 chain.py 与初值文件 chain.txt。\n"
    "每一步只做一件事：\n"
    "  1) 运行 `python chain.py <当前 token>`"
    "（第一次的当前 token 就是 chain.txt 里的初值，可以先用 bash 读一次 chain.txt）；\n"
    "  2) 从它的输出里拿到新 token（形如 `step=N token=xxxxxxxx`），它同时被写进 chain.txt。\n"
    f"重复上面的动作，直到 chain-steps.txt 里的步数达到 {TRANSITIONS}。\n"
    "必须遵守的规则：\n"
    "  - **一次 bash 调用只运行一次 chain.py**：不要用 shell 循环，也不要用 `;` / `&&` "
    "把多次调用拼在一条命令里——token 是随机值，只能从上一条命令的输出里读出来；\n"
    "  - 每一步都要真的执行命令并读输出，不要跳步；\n"
    f"  - 全部 {TRANSITIONS} 步完成后，用 write 工具创建 {DONE_FILE}，内容恰好是最终的 token"
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

        events = store.read_events(ctx.session_id)
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
        )
        return AttemptOutcome(
            ok=all(item.ok for item in assertions),
            session_id=ctx.session_id,
            run_id=run_id,
            run_status=result.status,
            steps=result.steps,
            tool_calls=tool_calls,
            assertions=assertions,
            event_count=len(events),
            output_tail=result.final_text[-2000:],
        )

    def _assertions(
        self, *, ctx: ScenarioContext, run_status: str, tool_calls: list[str],
        run_id: str, events: list[Any], steps: int, fuse: Any,
    ) -> list[AssertionResult]:
        """结构性判据（机械可检，不是 LLM judge）。"""
        from agent_harness.agent.budget import SOURCE_DEPLOYMENT

        final_token = _safe_read(ctx.sandbox, TOKEN_FILE).strip()
        steps_recorded = _safe_read(ctx.sandbox, STEPS_FILE).strip()
        done_content = _safe_read(ctx.sandbox, DONE_FILE)
        model_requests = sum(1 for event in events if event.type == "model/completed")
        fuse_tripped = any(FUSE_TRIP_MARKER in _event_text(event) for event in events)
        dangling = dangling_tool_call_ids(events)
        bash_calls = tool_calls.count("bash")
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
                name="model_requests_observed", ok=model_requests > LEGACY_TURN_LIMIT,
                detail=f"model/completed={model_requests}",
            ),
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
                name="session_identity_present",
                ok=bool(ctx.session_id) and bool(run_id),
                detail=f"session_id={'有' if ctx.session_id else '无'} run_id={'有' if run_id else '无'}",
            ),
        ]


SCENARIO = LongTaskPastLegacyTurnLimitScenario()
