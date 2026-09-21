"""BashTool：在 workspace 内执行 shell 命令的 Coding Tool。

MUTATING 副作用（保守默认）：即使命令本身是只读的（如 ls），也无法静态保证，
所以整批串行执行。

核心不变量（ADR-0002）：命令**业务失败**（非零 exit_code，如 pytest 失败）不是 Tool
Runtime 异常——exit_code/stdout/stderr 放进 data 供模型读取，模型据此决定下一步，
而不是被 Executor 自动重试。

唯一例外是 **执行预算到期**（`ExecResult.timed_out`，含不经 Executor 直调时后端自己的
默认预算）：此时返回 TIMEOUT 失败，沙箱已捕获的部分输出与 exit_code 进 metadata。
机制与取舍见 ADR-0039。
"""

from __future__ import annotations

import asyncio
import threading

from pydantic import BaseModel, Field

from agent_harness.sandbox import Sandbox, ShellFamily
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.contract import ToolPermission
from agent_harness.tooling.deadline import tool_execution_deadline_var
from agent_harness.tooling.result import ErrorCode

DEFAULT_BASH_TIMEOUT_SECONDS = 60.0

# 超时 payload 进 metadata 的显示预算（每通道）。**必须自带上限**：
# `ArtifactOverflowHandler` 只扫 `data` 的 output/content/stdout/stderr/before/after
# 与 `message`，**不扫 metadata**——不受它管的字段一旦放大幅文本，就等于绕过 Context
# 预算（不变量 #15）：捕获上限默认 2 MB，一次高噪声命令超时即可原样灌进模型上下文。
# 截断只减少"模型这一眼看到多少"；完整输出仍经 tool/output_delta 落进会话（ADR-0016）。
_TIMEOUT_PAYLOAD_MAX_CHARS = 2000


def _clip_for_model(text: str, limit: int = _TIMEOUT_PAYLOAD_MAX_CHARS) -> str:
    """保留首尾、中间留明确标记的截断（不静默丢内容）。

    返回长度最多 `limit + 标记长度`（标记要报出截了多少，无法算进 limit）。
    非正 `limit` 直接返回空串：`text[-0:]` 是整串、负 tail 会切出乱序片段，
    两种都不是"截断"，留着只会把上限变成假的。
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    head = limit * 3 // 5
    tail = limit - head
    return (
        f"{text[:head]}\n…[超时 payload 已截断：共 {len(text)} 字符，"
        f"此处只保留首 {head} + 末 {tail}]\n{text[-tail:]}"
    )


class _BashArgs(BaseModel):
    command: str = Field(..., description="要在 workspace 内执行的 shell 命令")


class BashTool(Tool):
    """bash 工具：在 workspace 内执行 shell 命令，返回 exit_code/stdout/stderr。

    名字是**历史名称**（OBS-012）：实际解释器由 Sandbox 后端决定，**不是 bash**——
    POSIX/容器为 `/bin/sh`，Windows 本机为 `cmd.exe`。模型可见描述据此声明真相。
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        """模型可见描述——必须声明**实际** shell（OBS-012）。

        工具名 `bash` 是历史名称：没有任何后端真的调 bash（POSIX/容器是 `/bin/sh`，
        Windows 本机是 `cmd.exe`）。若描述暗示 bash，模型会写出该解释器不认的语法
        （本机实证：cmd.exe 对 bash 语法报「此时不应有 i。」）。解释器名取自 Sandbox
        的事实声明，**不用 `os.name` 猜**（宿主 Windows 时容器内仍是 sh）。
        """
        env = self._sandbox.shell_environment
        if env.family is ShellFamily.BASH:
            # 后端真的用 bash 时不否认（当前无此后端，但 API 允许——避免出现
            # 「实际解释器是 bash（不是 bash）」的自相矛盾）。
            lead = f"在 workspace 内执行 shell 命令。实际解释器是 {env.name}。"
        else:
            lead = (
                "在 workspace 内执行 shell 命令。注意：工具名 bash 是历史名称，"
                f"实际解释器是 {env.name}（不是 bash），请按该解释器的语法书写命令。"
            )
        parts = [lead]
        if env.family is ShellFamily.CMD:
            parts.append(
                "Windows cmd.exe 注意事项：单引号不是引用符、$VAR 不展开、"
                "cat/ls/grep 等 Unix 命令通常不可用（用 type/dir/findstr 代替）、"
                "Unix 风格重定向（如 2>/dev/null）无效。"
            )
        parts.append(
            "参数：command 为要执行的 shell 命令字符串。"
            "返回 exit_code、stdout、stderr——命令返回非零 exit_code 不代表工具调用失败，"
            "应读取 stdout/stderr 判断命令执行结果。"
        )
        return "".join(parts)

    @property
    def args_schema(self) -> type[BaseModel]:
        return _BashArgs

    @property
    def timeout_seconds(self) -> float:
        """统一 Bash contract：ToolExecutor 的有效预算为 60 秒。"""
        return DEFAULT_BASH_TIMEOUT_SECONDS

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def permission(self) -> ToolPermission:
        return ToolPermission.DANGER

    async def execute(self, args: _BashArgs) -> ToolResult:
        """调 sandbox.exec；命令业务失败仍返回 ok=True（ADR-0002），预算到期除外（ADR-0039）。

        sandbox.exec 是同步阻塞调用（子进程最长跑满超时），卸载到工作线程
        执行——否则 event loop 会被单条命令冻结整个异步服务器（D10）。
        执行期 stdout/stderr 经 sink 逐段流式（ADR-0016 §4.2）：sink 由
        ToolExecutor 在执行期放入 contextvar，asyncio.to_thread 复制 context
        使工作线程内可读；无 sink（纯执行场景）= None，sandbox 不回调。
        """
        from agent_harness.tooling.output_stream import tool_output_sink_var

        # 协作取消（C1）：asyncio 超时/断连只能取消 await，杀不掉已在
        # 工作线程里跑的子进程——通过 cancel_event 通知 sandbox 击杀进程树，
        # "超时/取消返回"之后命令不再继续改 workspace（R7-1 的执行层闭环）。
        cancel_event = threading.Event()
        sink = tool_output_sink_var.get()
        deadline = tool_execution_deadline_var.get()
        # Executor 建立的绝对 deadline 原样转发给 Sandbox：沙箱**消费**它而不是
        # 重新起算相对预算（ADR-0039 D2）。为 None 时沙箱退回自己的默认预算——
        # 这**不能**反推"没经过 Executor"（不转发 deadline 的工具也存在，见 L2）。
        worker = asyncio.create_task(asyncio.to_thread(
            self._sandbox.exec, args.command, timeout=self.timeout_seconds,
            deadline=deadline,
            cancel_event=cancel_event,
            on_output=(sink.push if sink is not None else None),
        ))
        try:
            # shield：Executor 的 deadline 掐断本协程时，worker 仍可被下面的
            # 清理 join 等到——不能让它随取消一起消失。
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel_event.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # 重复取消（外层再次取消）不放弃 join：清理未完成就返回，
                    # 等于放过一个仍在改 workspace 的进程树。
                    cancel_event.set()
            # 清理失败压过超时/取消结论——进程可能还活着时不能报"干净结束"。
            worker.result()
            raise
        except PermissionError as e:
            return ToolResult.failure(
                message=str(e),
                error_code=ErrorCode.PERMISSION_DENIED,
            )
        except Exception as e:  # noqa: BLE001
            # Sandbox 本身崩了（如容器挂了、子进程底层故障）——这才是真正的工具失败。
            return ToolResult.failure(
                message=f"bash 执行环境异常: {type(e).__name__}: {e}",
                error_code=ErrorCode.TOOL_EXECUTION_ERROR,
            )
        if result.timed_out:
            # 预算到期 ⇒ 统一成 TIMEOUT 失败，且把沙箱已捕获的 exit_code/stdout/stderr
            # 带出去（约束与取舍见 ADR-0039 D5/L1）：到期不是命令自己的结论，
            # 不能报"命令已执行"。retryable 按 side_effect 派生，与 Executor 同一规则
            # ——重试决策只有一个责任域（AGENTS.md 不变量 #8）。
            mutating = self.side_effect is ToolSideEffect.MUTATING
            return ToolResult.failure(
                message=(
                    f"命令超时（预算 {self.timeout_seconds} 秒到期）：若命令已启动，"
                    "已尽力终止进程树（可能仍有残留）；预算在启动前就用完时，命令"
                    "没有启动过。超时前捕获的部分输出与 exit_code 见 metadata"
                    f"（可能不完整；每通道裁剪到约 {_TIMEOUT_PAYLOAD_MAX_CHARS} 字符，"
                    "含截断标记在内）。"
                    + ("该工具是 MUTATING，副作用状态未知，不要直接重跑。"
                       if mutating else
                       "该工具声明为 READ_ONLY，可安全重试。")
                ),
                error_code=ErrorCode.TIMEOUT,
                retryable=not mutating,
                metadata={
                    "exit_code": result.exit_code,
                    "stdout": _clip_for_model(result.stdout),
                    "stderr": _clip_for_model(result.stderr),
                    # 不复用 Executor 回填的 duration_ms 键（它会被覆盖成 attempt 墙钟）。
                    "sandbox_duration_ms": result.duration_ms,
                },
            )
        # 关键映射（ADR-0002）：命令业务失败（exit_code!=0）→ ok=True，
        # exit_code/stdout/stderr 在 data 里供模型读取。
        return ToolResult.success(
            message=f"命令已执行，exit_code={result.exit_code}。",
            data={
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_ms": result.duration_ms,
                **({"cancelled": True} if result.cancelled else {}),
            },
        )
