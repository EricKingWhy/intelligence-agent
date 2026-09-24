"""内置场景：真实模型 + 生产工具（`#307` AC「专用命令用真实已配置模型和至少一个生产文件/命令/Git 工具完成 smoke task」）。

## 生产性从哪来（这条决定证据算不算数）

- **工具**：`agent_harness.assembly.BUILTIN_LOCAL_TOOLS` —— 装配层注册进 runtime 的**同一份**
  元组，不是测试自建的裁剪版（B-38 登记过 `demo/live_agent.py` 自建 registry 的账）；
- **执行器**：`ToolExecutor`，policy = `WORKSPACE_WRITE` + auto-approve 回调 —— 与
  `assembly.build_runtime` 的默认形状**逐项相同**（`assembly.py` 的 `elif approval_callback is None`
  分支就是这么造的）；
- **沙箱**：`LocalSubprocessSandbox`（生产 `WorkspaceRegistry` 默认后端，且 env 白名单生效）；
- **模型**：`ModelConfig.from_settings` → `create_chat_model`（真实 provider 客户端，`#307` FIXED）；
- **事件**：`JsonlSessionStore` + `Session`（append-only typed SessionEvent），轨迹可被独立
  复核（见 `validator.py`）。

## 任务为什么这么短

3/3 是硬判据 ⇒ 任务必须**结构性可达**（不赌模型的措辞或长链推理）。任务只要三件事：
写一个内容固定的文件（`write`）、跑一条只读 git 命令（`bash`）、看仓库状态（`git_status`）。
断言只钉**结果**与**工具面**（文件内容 + 至少一个生产工具 + 无悬空 call），不评语义质量。
"""

from __future__ import annotations

from typing import Any

from evaluation.assertions import dangling_tool_call_ids
from evaluation.live_gate.registry import AttemptOutcome, ScenarioContext
from evaluation.live_gate.schema import AssertionResult

SCENARIO_ID = "smoke-production-tools"
SCENARIO_VERSION = 1

#: 场景产物：路径与内容都是**常量** —— 断言才能是"相等"，而不是"看起来像"。
ARTIFACT = "notes.txt"
ARTIFACT_CONTENT = "live-gate-ok"

#: 真实模型调用的单次超时（秒）。比连接探测的 15s 宽得多：这是一次完整的 agent run。
REQUEST_TIMEOUT = 180.0

#: 命令 / Git 类生产工具（断言"至少用到一个"时用的集合）。
COMMAND_OR_GIT_TOOLS = ("bash", "git_status", "git_diff")

TASK = (
    "在当前工作区里完成三件事：\n"
    f"1) 用 write 工具创建 {ARTIFACT}，内容恰好是 {ARTIFACT_CONTENT}"
    "（不要换行、不要引号、不要多余字符）；\n"
    "2) 用 bash 工具执行 `git log --oneline -1`，确认这是一个 git 仓库；\n"
    "3) 用 git_status 工具查看仓库状态。\n"
    "最后用一句话汇总你做了什么，不要输出文件内容以外的长文本。"
)


def _seed_commands() -> tuple[tuple[str, ...], ...]:
    """seed 的 git 仓库：让 `bash`/`git_status` 两个工具**有真实对象可操作**。

    邮箱用 `.invalid`（RFC 2606 保留域）：seed 提交的作者身份不该是任何真实地址。
    """
    return (
        ("git init -q",),
        ("git config user.email", "live-gate@example.invalid"),
        ("git config user.name", "live-gate"),
    )


class SmokeProductionToolsScenario:
    """最小真实 smoke：真实模型 + 生产工具 + 一次性 git 工作区。"""

    id = SCENARIO_ID
    version = SCENARIO_VERSION
    description = (
        "真实模型驱动生产工具（write/bash/git_status）在一次性 git 工作区完成固定产物；"
        "断言钉结果与工具面，不评语义"
    )

    async def prepare(self, ctx: ScenarioContext) -> list[str]:
        """seed 工作区 + 确认 git 可用。返回未满足的前置（非空 ⇒ 本次 Gate 判 BLOCKED）。"""
        probe = ctx.sandbox.exec("git --version")
        if probe.exit_code != 0:
            missing_git = (
                f"工作区里没有可用的 git（rc={probe.exit_code}）——"
                "本场景要真实驱动 git 工具，缺它不构成 FAIL 而是环境不具备"
            )
            return [missing_git]
        for command in _seed_commands():
            result = ctx.sandbox.exec(" ".join(command))
            if result.exit_code != 0:
                return [f"seed 失败：{command[0]} rc={result.exit_code}"]
        ctx.sandbox.write_text("README.md", "# live gate workspace\n")
        for command in ("git add -A", "git commit -q -m seed"):
            result = ctx.sandbox.exec(command)
            if result.exit_code != 0:
                return [f"seed 失败：{command} rc={result.exit_code}"]
        return []

    async def run(self, ctx: ScenarioContext) -> AttemptOutcome:
        """跑一次真实 run。**异常一律转 `ok=False`**（runner 兜底捕获是第二道，不是第一道）。"""
        from agent_harness.agent import AgentRuntime
        from agent_harness.assembly import BUILTIN_LOCAL_TOOLS
        from agent_harness.model.config import ModelConfig
        from agent_harness.model.provider import create_chat_model
        from agent_harness.session import JsonlSessionStore, Session
        from agent_harness.tooling import PermissionPolicy, ToolExecutor, ToolRegistry
        from agent_harness.tooling.approval import ApprovalResponse

        store = JsonlSessionStore(root=ctx.session_root)
        session: Session | None = None
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
            runtime = AgentRuntime(model, registry, executor, primary_model_name=config.model_name)
            result = await runtime.run(session, TASK)
        except Exception as error:  # noqa: BLE001 - 失败要如实记录（错误的原文由 runner 统一脱敏）
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
            run_id=run_id, events=events,
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
        run_id: str, events: list[Any],
    ) -> list[AssertionResult]:
        """结构性判据（`evaluation/assertions.py` 那类代码判断，不是 LLM judge）。"""
        try:
            content = ctx.sandbox.read_text(ARTIFACT)
            artifact_ok, artifact_detail = content == ARTIFACT_CONTENT, f"len={len(content)}"
        except Exception as error:  # noqa: BLE001 - 产物不存在也是判据的一种取值
            artifact_ok, artifact_detail = False, f"读取失败：{type(error).__name__}"
        dangling = dangling_tool_call_ids(events)
        return [
            AssertionResult(
                name="run_completed", ok=run_status == "completed",
                detail=f"run_status={run_status}",
            ),
            AssertionResult(
                name="artifact_content", ok=artifact_ok,
                detail=f"{ARTIFACT} 内容比对（{artifact_detail}）",
            ),
            AssertionResult(
                name="write_tool_used", ok="write" in tool_calls,
                detail=f"工具序列={tool_calls}",
            ),
            AssertionResult(
                name="command_or_git_tool_used",
                ok=any(name in COMMAND_OR_GIT_TOOLS for name in tool_calls),
                detail=f"工具序列={tool_calls}",
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


SCENARIO = SmokeProductionToolsScenario()
