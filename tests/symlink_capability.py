"""目录链接创建能力探测、junction 回退与共享 skip marker（#726/#731）。

为什么需要：租约测试要验证"符号链接/junction 指向同一目录 ⇒ 同键"与
"链接环 fail-closed"。Windows 上 `os.symlink` 需要开发者模式/管理员
（WinError 1314），而 `mklink /J`（目录联接）不需要特权——两者都是
reparse point，`os.path.realpath` 都解析到目标目录（lease_paths 模块
docstring），语义等价。无特权 Windows 开发机跳过等于没有覆盖（仓库立场：
tests/web/test_host_dirs_api.py「B-23」段、tests/session/test_session_cwd.py
同款先例），因此先试 symlink、失败退回 junction，两样都做不到才 skip。

探测惯例（真探测一次 + 缓存 + 后验）：CPython
`test.support.os_helper.can_symlink`（捕获 OSError/NotImplementedError/
AttributeError）、git test-lib SYMLINKS lazy prereq、pytest symlink_or_skip。
marker 逐条贴用例，不做整模块 `pytestmark`——避免连累同文件内不依赖链接
的用例（pytest 官方对共享 marker 变量的建议用法）。
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

SKIP_REASON = (
    "宿主既无符号链接创建特权（Windows 需开发者模式/管理员，os.symlink 抛 "
    "OSError [WinError 1314]）也无法创建目录联接（mklink /J），无法验证 "
    "链接→同键/链接环 fail-closed 语义"
)

_can_make_dir_link: bool | None = None


def _dir_link_created(link: Path) -> bool:
    """reparse point 建出即算（目标允许尚不存在——链接环构造需要悬空端）。

    不能用 `is_symlink()/is_dir()` 判 junction：junction 的 `is_symlink()`
    恒 False，悬空 junction 的 `is_dir()` 跟随解析到缺失目标也是 False；
    `os.lstat` 不跟随，symlink（S_ISLNK）与 junction/目录（S_ISDIR）都真。
    """
    try:
        st = os.lstat(link)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or stat.S_ISDIR(st.st_mode)


def make_dir_link(link: Path, target: Path) -> bool:
    """在 link 处建一个指向 target 的目录链接；本机做不到返回 False。

    优先 `os.symlink`；Windows 无特权退回**目录联接**（junction，`mklink /J`
    不需要特权）——两者都是 reparse point，realpath 都把 link 解析到 target，
    正是租约测试要的"链接指向同一目录"等价类。链接与目标都建在 tmp_path 内，
    清理不波及测试目录之外。`symlink_to` 可能**静默成功却没建出链接**（no-op
    环境，B-23）⇒ 必须核验 reparse point 建出再返回 True，核验不过走 junction
    回退，不退化 skip。mklink 的提示文本是本地代码页（非 UTF-8），读它只会在
    subprocess 的 reader 线程里炸 UnicodeDecodeError ⇒ 双向 DEVNULL，返回码
    已经够（tests/session/test_session_cwd.py 同款）。
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        if _dir_link_created(link):
            return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    return completed.returncode == 0 and _dir_link_created(link)


def can_make_dir_link() -> bool:
    """本机能否创建"解析到目标目录"的目录链接？真探测一次并缓存。

    建出还不够：租约测试依赖"realpath 把链接解析到目标"，故探测含解析等价
    后验（探测环境与断言环境同机制，能力变化只会发生在进程间）。
    """
    global _can_make_dir_link
    if _can_make_dir_link is None:
        with tempfile.TemporaryDirectory(prefix="dirlink-capability-") as tmp:
            target = Path(tmp) / "t"
            target.mkdir()
            link = Path(tmp) / "l"
            _can_make_dir_link = make_dir_link(link, target) and os.path.realpath(
                str(link)
            ) == os.path.realpath(str(target))
    return _can_make_dir_link


needs_dir_link = pytest.mark.skipif(not can_make_dir_link(), reason=SKIP_REASON)
