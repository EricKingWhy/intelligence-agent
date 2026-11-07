"""Workspace 路径的**唯一**规范化入口（WS-1 / issue #151 AC5）。

会话侧锚（`session/started` 的 `cwd`）与 `WorkspaceRegistry` 映射文件里的
`workspace_root` 是同一个物理目录的**两个写入点**。若两处各自规范化，同名目录
在尾部斜杠、`.`/`..`、符号链接等写法差异下会产出**不同字符串**，于是"同一路径"
被当成两个——成员资格校验的结果就会随写入点漂移。故规范化只此一处，两处都调它。
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable
from pathlib import Path, PureWindowsPath


def canonical_workspace_path(path: str | Path) -> str:
    """→ 规范化绝对路径（`fs.realpath` 语义）。

    解析尾部斜杠、`.`/`..`、以及**符号链接/junction**（`D:\\a\\b\\`、
    `D:\\a\\..\\a\\b`、指向 `b` 的链接三者产出同一字符串）。

    `realpath` 只解析**已存在**的链接；路径里不存在的段只能做词法归一。链接能不能
    被解析取决于它本身在不在，**不取决于本函数之前是否 mkdir**——`WorkspaceRegistry
    .create` 先建目录是它原本就要做的事（确保 workspace 目录存在），规范化紧随其后，
    两者没有因果依赖。
    """
    return os.path.realpath(os.fspath(path))


def is_absolute_path(value: str) -> bool:
    """用户提供的路径是不是**本平台**的绝对路径（形态判定，不 touch 文件系统）。

    为什么不能统一用 `PureWindowsPath(...).is_absolute()`（本仓曾有三处这样写）：
    `PureWindowsPath("/home/user/proj")` 的 `drive` 为空 → `is_absolute()` 为 **False**，
    于是 POSIX 上任何合法绝对路径都被判成相对路径、被 422 拒掉（`C:\\x` 在 POSIX 上
    的真实错误应该是"不存在"，不是"不是绝对路径"）。反之，用 `os.path.isabs` 判
    Windows 路径也不对：`os.path.isabs("\\\\foo")` 为 True（有根无盘符，**盘符相关**），
    接受它会把一次歧义路径变成"当前盘根 + foo"的静默锚定。

    所以按平台分支——各平台只接受自己那套绝对形态（Windows 仍要求盘符 + 根，与
    `_validate_workspace_name` / `_require_absolute_path` 的既有口径逐字一致）。

    **必须在任何 `realpath` / `exists` 之前调用**：`os.path.realpath("relative/x")` 会
    按进程当前工作目录解析，把一次笔误变成"落在服务器碰巧启动的目录"。
    """
    if os.name == "nt":
        return PureWindowsPath(value).is_absolute()
    return os.path.isabs(value)


def glob_match(
    rel_path: str,
    pattern: str,
    *,
    basename: Callable[[str], str],
) -> bool:
    """对 workspace 相对路径做 glob 匹配，支持 `**` 递归（三 backend 共用的**唯一**纯策略）。

    `basename` 是本函数**唯一**的平台/后端参数：三后端的 `rel_path` 都由各自的路径解析
    域生产，只有"从相对路径里取最后一段"这一步的口径不同，所以由调用方注入：

    - `LocalSubprocessSandbox` → 宿主 `Path(...).name`；
    - `DockerSandbox` → `PurePosixPath(...).name`（容器内语义，不吃宿主 `\\`）；
    - `WindowsPsecSandbox` → `ntpath.basename`（nt 语义，吃 `\\`）。

    **为什么不能统一**（具体反例，不是"无法合并"一句带过）：
    `rel_path=r"pkg\\inner.py"` 时 `ntpath.basename` 得 `inner.py`、`PurePosixPath` 得
    `pkg\\inner.py`（宿主 `Path` 跟随宿主：POSIX 上同 `PurePosixPath`，Windows 上同
    `ntpath`）；`basename("a/b/")` 在 `ntpath` 是 `""`、在 pathlib 是 `"b"`。
    把三者中任一语义强加给另外两个，都会改掉后者的既有 `list_files` 结果。

    **大小写不在这里分支**：沿用 `fnmatch.fnmatch`（它经 `os.path.normcase`），所以折叠
    规则跟随**宿主进程**——这正是三处现行行为。Docker backend 的匹配也跑在宿主 Python
    里（`find` 只负责列文件名），所以容器内 POSIX 路径在 Windows 宿主上同样被折叠。

    `**` 的收敛方式沿用现状：`"**/"` → `""`，`"**"` → `"*"`；完整相对路径与 basename
    各匹配一次，任一命中即算命中。
    """
    if pattern in ("", "*"):
        return True
    effective = pattern.replace("**/", "").replace("**", "*") if "**" in pattern else pattern
    return fnmatch.fnmatch(rel_path, effective) or fnmatch.fnmatch(
        basename(rel_path), effective
    )
