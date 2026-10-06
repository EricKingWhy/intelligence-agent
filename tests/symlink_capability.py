"""symlink 创建能力探测与共享 skip marker（#726）。

Windows 宿主默认无 SeCreateSymbolicLinkPrivilege（开发者模式/管理员才有），
`os.symlink` 抛 `OSError [WinError 1314]`。依赖 symlink 语义的测试用
`needs_symlink` 做能力级守卫（收集期判定，reason 进 `-rs` 汇总）。

设计出处：探测 = 真实建链一次并缓存（CPython `test.support.os_helper.can_symlink`
同构），异常面 `(OSError, NotImplementedError)`；`TemporaryDirectory` 零残留
（Django `utils._os.symlinks_supported` 同款）；成功后核验链接真建成（git lazy
prereq `test -h` 同款防御，本仓 B-23 有「静默成功却没建出」的 no-op 环境前科）。
marker 逐条贴用例，不做整模块 `pytestmark`——避免连累同文件内不依赖 symlink
的用例（pytest 官方对共享 marker 变量的建议用法）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

SKIP_REASON = (
    "宿主无符号链接创建特权（Windows 需开发者模式/管理员，os.symlink 抛 "
    "OSError [WinError 1314]），无法验证 symlink→同键/链接环语义"
)

_can_symlink: bool | None = None


def can_symlink() -> bool:
    """真实 `os.symlink` 探测一次并缓存；结果不含平台字符串判断。"""
    global _can_symlink
    if _can_symlink is None:
        with tempfile.TemporaryDirectory(prefix="symlink-capability-") as tmp:
            src, link = Path(tmp) / "t", Path(tmp) / "l"
            try:
                os.symlink(src, link)
                _can_symlink = link.is_symlink()
            except (OSError, NotImplementedError):
                _can_symlink = False
    return _can_symlink


needs_symlink = pytest.mark.skipif(not can_symlink(), reason=SKIP_REASON)
