"""Sandbox 抽象契约：Coding Tool 的隔离执行环境。

Sandbox 是 Runtime 安全边界而非 Prompt 约束（ADR-0001）：模型即使尝试访问
workspace 外的路径，Sandbox 也会拒绝。它定义了 read / write / bash 等 Coding Tool
运行的唯一接口，具体后端（本机子进程 / Docker 容器）实现这一契约。

ExecResult 是 Sandbox 层的原生返回，不感知 ToolResult——Tool 层负责映射。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ExecResult:
    """Sandbox 执行一条 shell 命令后的原生结果。

    Sandbox 不返回 ToolResult（那是 Tool 层的语义），只返回执行事实：
    exit_code 是 shell 命令的真实退出码（非零 = 命令业务失败，但 Sandbox 调用本身成功）。
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float = field(default=0.0)
    # 协作取消（R7-1/C1）：进程因取消信号被整树击杀时置 True——
    # 上层（Tool/Ledger）据此区分"自然结束"与"被取消（副作用未知）"。
    cancelled: bool = field(default=False)


class Sandbox(ABC):
    """Coding Tool 的隔离执行环境契约。

    6 个方法一次定全（ADR-0001），即使某后端暂时用不到某个方法也在基类里声明，
    避免未来加后端时改契约（破坏性变更）。

    workspace 路径边界由 Sandbox 统一强制（ADR-0001 / Q11(a)）：
    所有接受路径的方法把传入路径 resolve 后校验是否在 workspace 内，越界抛 PermissionError。
    """

    @abstractmethod
    def ensure_started(self) -> None:
        """惰性启动执行环境。幂等：多次调用不报错。

        LocalSubprocess 是 no-op（进程总在）；Docker 起容器。
        """

    @abstractmethod
    def exec(self, command: str, *, timeout: float | None = None,
             cancel_event: threading.Event | None = None) -> ExecResult:
        """执行 shell 命令，返回 ExecResult。

        timeout 为秒；None 表示用后端默认值。超时行为由后端决定。
        cancel_event 是协作取消钩子（C1）：置位后后端必须尽快击杀进程树并返回
        cancelled=True 的结果——asyncio 超时/断连只能取消 await，杀不掉已经
        跑起来的子进程，没有这个钩子，"超时返回"之后命令还会继续改 workspace。
        """

    @abstractmethod
    def list_files(self, pattern: str) -> list[str]:
        """枚举 workspace 内匹配 glob 模式的文件，返回 workspace 相对路径列表。

        - 仅返回文件，不返回目录。
        - 相对路径用 POSIX 风格（正斜杠），按路径排序。
        - pattern 为空字符串或 "*" 时返回 workspace 内所有文件。
        - 越界访问（pattern 解析出 workspace 外）抛 PermissionError。

        这是 grep / glob Coding Tool 跨后端枚举文件的唯一可移植入口；
        LocalSubprocessSandbox 用 os.walk，DockerSandbox 用 exec("find")。
        """

    @abstractmethod
    def read_text(self, path: str) -> str:
        """读 workspace 内文件，返回文本。路径越界抛 PermissionError。"""

    @abstractmethod
    def write_text(self, path: str, content: str) -> None:
        """覆盖写 workspace 内文件。路径越界抛 PermissionError。"""

    @abstractmethod
    def copy_in(self, host_path: Path, workspace_path: str) -> None:
        """把宿主文件拷入 workspace 内指定位置。

        LocalSubprocess 用文件复制到 workspace 目录；Docker 用 docker cp。
        workspace_path 越界抛 PermissionError。
        """

    @abstractmethod
    def stop(self) -> None:
        """停止 Sandbox（保留持久状态以便 resume）。幂等：多次调用不报错。

        语义：停容器/进程，但不删 workspace 数据/Volume——下次 ensure_started 可恢复。
        对无持久化需要的后端（LocalSubprocess）可以是 no-op。
        """

    @abstractmethod
    def delete(self) -> None:
        """彻底清理 Sandbox 资源（容器 + Volume + workspace 目录）。幂等。

        语义：完全销毁，不可 resume。WorkspaceRegistry.delete() 调它。
        对 LocalSubprocess，删除 workspace_root 目录；对 Docker，移除容器和 Volume。
        """

    # —— 路径安全工具（具体方法，子类复用） ——

    def _resolve_within_workspace(self, path: str) -> Path:
        """把传入路径 resolve 成 workspace 内绝对路径，越界抛 PermissionError。

        这是 ADR-0001 路径边界的唯一强制点：所有子类接受路径的方法都先调它。
        """
        workspace = self.workspace_root
        resolved = (workspace / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
        if not resolved.is_relative_to(workspace):
            raise PermissionError(
                f"路径 '{path}' 解析为 '{resolved}'，越出 workspace '{workspace}' 边界，拒绝访问"
            )
        return resolved

    @property
    @abstractmethod
    def workspace_root(self) -> Path:
        """workspace 根目录的绝对路径（路径边界的基准）。"""
