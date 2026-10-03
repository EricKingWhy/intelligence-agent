"""WriteTool：覆盖写入 workspace 内文件的 Coding Tool。

MUTATING 副作用：改变外部状态，批次调度时整批串行执行。
是覆盖写而非追加——调用方应理解为"把文件内容整体替换为 content"。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_harness.sandbox import Sandbox
from agent_harness.tooling import Tool, ToolResult, ToolSideEffect
from agent_harness.tooling.reconcile import ReconcileHint
from agent_harness.tooling.result import ErrorCode
from agent_harness.tools._diff_data import diff_data


class _WriteArgs(BaseModel):
    path: str = Field(..., description="workspace 内的文件相对路径，如 'src/main.py'")
    content: str = Field(..., description="要写入文件的完整文本内容（覆盖已有内容）")


class WriteTool(Tool):
    """write 工具：覆盖写入 workspace 内文件。父目录不存在时自动创建。"""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    @property
    def name(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "覆盖写入 workspace 内的文件。"
            "参数：path 为文件路径，content 为要写入的完整文本（会完全替换原有内容）。"
            "适合创建新文件或整体重写已有文件。"
        )

    @property
    def args_schema(self) -> type[BaseModel]:
        return _WriteArgs

    @property
    def side_effect(self) -> ToolSideEffect:
        return ToolSideEffect.MUTATING

    @property
    def reconcile_hint(self) -> ReconcileHint:
        return ReconcileHint(
            verifiable=True,
            suggested_action=(
                "重新读取目标文件，核对内容是否已与写入预期一致："
                "一致说明写入已生效（确认成功），不一致说明未执行。"
            ),
        )

    async def execute(self, args: _WriteArgs) -> ToolResult:
        """调 sandbox.write_text；路径越界映射成 PERMISSION_DENIED。"""
        # 读旧内容供前端 diff（文件不存在 → before 为空，表示这是新建）。
        # 读失败不阻塞写入——write 本就是覆盖语义，diff 是辅助视图不是契约。
        before = ""
        try:
            before = self._sandbox.read_text(args.path)
        except (FileNotFoundError, PermissionError):
            pass

        try:
            self._sandbox.write_text(args.path, args.content)
        except PermissionError as e:
            # #549-b / SB-07：Windows 的 os.replace 语义把"目标是目录"也报成
            # PermissionError [WinError 5]（POSIX 是 IsADirectoryError）。用磁盘
            # 事实区分两种成因：目录形态映射成模型可自纠的 INVALID_ARGUMENT
            # （可行动 message），真正的权限拒绝保持 PERMISSION_DENIED。
            if self._target_is_directory(args.path):
                return ToolResult.failure(
                    message=(
                        f"写入失败：'{args.path}' 是目录，不能作为文件写入"
                        "（请改用文件路径）"
                    ),
                    error_code=ErrorCode.INVALID_ARGUMENT,
                )
            return ToolResult.failure(
                message=str(e),
                error_code=ErrorCode.PERMISSION_DENIED,
            )
        except IsADirectoryError:
            # POSIX 形态的"目标是目录"（Windows 走上面的 PermissionError 分支）。
            return ToolResult.failure(
                message=(
                    f"写入失败：'{args.path}' 是目录，不能作为文件写入"
                    "（请改用文件路径）"
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        except NotADirectoryError:
            # 路径中某一段不是目录（POSIX：父路径是文件）。Windows 同形态报
            # FileExistsError [WinError 183]，落在下面的分支。
            return ToolResult.failure(
                message=(
                    f"写入失败：'{args.path}' 的父路径中有某一段不是目录"
                    "（请检查中间路径是否被同名文件占用）"
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        except FileExistsError:
            # Windows 形态的"父路径是文件"（[WinError 183] ERROR_ALREADY_EXISTS）。
            return ToolResult.failure(
                message=(
                    f"写入失败：'{args.path}' 的父路径中有某一段不是目录"
                    "（请检查中间路径是否被同名文件占用）"
                ),
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        return ToolResult.success(
            message=f"已写入 '{args.path}'（{len(args.content)} 字符）。",
            data={
                "path": args.path,
                "bytes_written": len(args.content.encode("utf-8")),
                **diff_data(before, args.content),
            },
        )

    def _target_is_directory(self, path: str) -> bool:
        """探测写目标在磁盘上是否是目录（#549-b 的 Windows 语义甄别）。

        只服务 PermissionError 分支的成因区分；探测失败（路径越界等，均为
        OSError 族）按 False 处理——保持旧的 PERMISSION_DENIED 行为，不新增
        误报。非 OSError 的解析故障是编程错误，让它照常暴露。
        """
        try:
            return self._sandbox.resolve_within_workspace(path).is_dir()
        except OSError:
            return False
