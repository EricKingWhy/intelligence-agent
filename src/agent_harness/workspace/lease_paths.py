"""W-10（#354）目录租约键的**唯一**规范化入口。

租约互斥的正确性完全取决于"两个写法是否指向同一目录"这个判定。只比较原始
字符串是不够的（票面核查增强明确要求）：盘符大小写（`D:\\a` vs `d:\\a`）、
尾分隔符（`D:\\a\\` vs `D:\\a`）、`.`/`..` 段、符号链接/junction（指向同一
目录的两条路径）、UNC 前缀写法（`\\\\s\\sh` vs `//s/sh`）都会让"同一路径"
被当成两个，从而绕过互斥。

两条分支，按**路径形态**而不是宿主平台选择（Linux 上测试 Windows 语义时
同样要能归一 Windows 形态的路径）：

- **Windows 形态**（`PureWindowsPath(p).drive` 非空，含 UNC）：盘符 + 全路径
  **casefold**（NTFS 大小写不敏感）；`.`/`..`/尾分隔符/分隔符方向按词法归一。
  在真实 Windows 宿主上调用方先经 `os.path.realpath`（junction 由 OS 解析），
  本层再做词法归一与折叠；POSIX 宿主（测试）只有词法层，这是它的等价逻辑。
- **POSIX 形态**：`os.path.realpath` + 目标目录存在性校验——符号链接在此
  解析，链接环/不可解析、目标不存在/不是目录 → `LeasePathError`
  （fail-closed：不能安全解析的路径不能证明独立性，拒绝）。

失败一律 fail-closed 抛 `LeasePathError`，绝不猜：相对路径、drive-relative
（`D:relative`）、含 NUL、不存在/不可解析，都拒绝并要求用户重新选目录
（PRD §4.1：租约取得在任何模型写任务开始之前）。
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import PureWindowsPath


class LeasePathError(Exception):
    """路径不能安全规范化为租约键（fail-closed）。"""


def _is_windows_form(path: str) -> bool:
    """这条路径是不是 Windows 形态（盘符或 UNC 根）？

    按形态判定而不是按 `os.name`：POSIX 宿主上的 `D:\\a\\b` 仍是 Windows
    形态，要走 Windows 归一分支（等价逻辑测试的前提）。
    """
    return PureWindowsPath(path).drive != ""


def _lexical_windows_key(path: str) -> str:
    """Windows 形态的词法归一：`.`/`..`/尾分隔符/分隔符方向 + 全路径 casefold。

    `PureWindowsPath` 自身会丢弃 `.` 段与尾分隔符、统一分隔符方向，但**保留**
    `..`（符号链接语义下不能盲目词法消解）。租约键需要的是"同一目录同一键"：
    `..` 在这里按词法消解——真实 Windows 宿主上 junction 语义的 `..` 由
    `os.path.realpath` 先行解析（模块 docstring），落到本层的词法消解只处理
    "链接无关"的段；POSIX 宿主（测试）没有 junction，词法消解即等价逻辑。
    """
    pure = PureWindowsPath(path)
    stack: list[str] = []
    # parts[0] 是 drive / UNC 根；其后可能还有一个根分隔符段（'\\'），两者都
    # 不是目录分量，跳过。
    for part in pure.parts[1:]:
        if part in ("", ".", "\\"):
            continue
        if part == "..":
            if stack:
                stack.pop()
            continue
        stack.append(part)
    display = "\\\\".join((pure.drive.upper(), *stack))
    return display.casefold()


def normalize_dir_key(path: str) -> str:
    """→ 同一物理目录恒同键的租约比较键；不能安全解析 → `LeasePathError`。

    Windows 形态返回 casefold 后的 `\\` 连接绝对形态；POSIX 形态返回
    `os.path.realpath(strict=True)` 的解析结果（符号链接/junction 由 OS
    解析，目录必须存在）。输入必须已是绝对形态。
    """
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise LeasePathError(f"路径非法（空/空白/NUL）：{path!r}")
    if _is_windows_form(path):
        pure = PureWindowsPath(path)
        if not pure.is_absolute():
            # `D:relative`（drive-relative）按进程当前盘解析——同 `paths.py`
            # is_absolute_path 的口径：歧义锚定直接拒绝，不猜。
            raise LeasePathError(f"Windows 路径不是绝对形态（缺根）：{path!r}")
        if os.name == "nt":
            # 真实 Windows 宿主：junction/symlink 先由 OS 解析，再词法归一。
            resolved = os.path.realpath(path)
        else:
            resolved = path
        return _lexical_windows_key(resolved)
    if not os.path.isabs(path):
        raise LeasePathError(
            f"路径不是本平台绝对形态（相对路径会锚定进程 cwd，拒绝）：{path!r}"
        )
    try:
        resolved = os.path.realpath(path)  # 非 strict：缺失段词法消解，链接尽量解析
    except OSError as e:
        # 链接环 / 不可解析：不能安全证明指向哪个目录 → fail-closed。
        raise LeasePathError(f"路径无法安全解析（{e}）：{path!r}") from e
    try:
        st = os.stat(resolved)
    except OSError as e:
        # 目标不存在/不可达：租约没有可指向的目录 → fail-closed（ENOENT 等）。
        raise LeasePathError(f"路径无法安全解析（{e}）：{path!r}") from e
    if not stat.S_ISDIR(st.st_mode):
        raise LeasePathError(f"路径不是目录（errno={errno.EINVAL}）：{path!r}")
    return resolved


def _split_key(key: str) -> tuple[str, ...]:
    """规范化键 → 分量元组（父子相交判定用；键内分隔符已归一）。"""
    return tuple(seg for seg in key.replace("\\", "/").split("/") if seg)


def paths_conflict(key_a: str, key_b: str) -> bool:
    """两个**已规范化**的租约键是否不能并发写（相等或祖先/后代相交）。

    票面："若不能安全证明两个路径独立，拒绝并要求用户选目录"。父目录与
    子目录的写入面相交（父级操作可触及子级文件），一律判冲突。
    """
    parts_a, parts_b = _split_key(key_a), _split_key(key_b)
    shorter, longer = sorted((parts_a, parts_b), key=len)
    return longer[: len(shorter)] == shorter
