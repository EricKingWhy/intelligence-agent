"""Workspace 路径的**唯一**规范化入口（WS-1 / issue #151 AC5）。

会话侧锚（`session/started` 的 `cwd`）与 `WorkspaceRegistry` 映射文件里的
`workspace_root` 是同一个物理目录的**两个写入点**。若两处各自规范化，同名目录
在尾部斜杠、`.`/`..`、符号链接等写法差异下会产出**不同字符串**，于是"同一路径"
被当成两个——成员资格校验的结果就会随写入点漂移。故规范化只此一处，两处都调它。
"""

from __future__ import annotations

import os
from pathlib import Path


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
