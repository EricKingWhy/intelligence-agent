"""会话硬删的**只读回执**删除语义（#939 M-13，从 `storage/local_artifact.py` 拆出）。

为什么独立成模块
----------------
这四个函数 + 一个公开入口此前住在 `storage/local_artifact.py`，但它们与那个宿主的
`ArtifactStore` 抽象**零交集**：不 import 任何 `storage.artifact` 的内容符号、不碰字节、
不知道"附件"是什么。它们只做三件事——清只读位、`shutil.rmtree`、按 sha 从路径反推。
审计实测（MM 审计 A-F12）：这一簇吸收了 **5 笔**修复，而同文件对照组
`delete_local_artifacts`（按文本 artifact id 精确删，是本模块的领域操作）**一笔未改**
——热点不是"文件大"，是"职责与宿主语义不匹配"。

复杂度来源（唯一的那个平台事实）
--------------------------------
`LocalArtifactStore._publish_blob` 发布的对象是只读的（`_OBJECT_MODE`），而**会话回执是
它的 hardlink** ⇒ Windows 上回执与全局对象共享同一只读属性，`os.unlink` 抛
`PermissionError`；改动前 `rmtree(..., ignore_errors=True)` 会把它静默吞掉、回执因此残留。
POSIX 上 unlink 不受只读位约束（只看目录写权限），本模块的回调不会被触发。

布局常量的归属
--------------
布局常量（`GLOBAL_ATTACHMENT_DIRNAME` / `BYTE_OBJECTS_DIRNAME` /
`SESSION_ATTACHMENTS_DIRNAME`）的**定义权在 `storage/attachment_layout.py`**（#933 M-16
已升格为跨 Provider 契约，三个 Provider 共用一份）。本模块只**读**它——不再从
`local_artifact` 借常量，因此与宿主模块之间**没有 import 边**（依赖单向：
`readonly_fs → attachment_layout` / `artifact` / `config`）。

本模块不负责
------------
- 不做自动 TTL / 体积清理（ADR-0004）；不删远端对象（`discard_local_artifacts` 只管本地）。
- **不回收全局字节对象**：它们落在全局内容寻址根、跨会话去重，会话硬删只带走回执
  （#830 D1，PRD D2 "v1 不做自动清理"；GC 是独立 follow-up）。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from agent_harness.config import Settings
from agent_harness.storage.artifact import SESSION_KEY_PATTERN
from agent_harness.storage.attachment_layout import (
    BYTE_OBJECTS_DIRNAME,
    GLOBAL_ATTACHMENT_DIRNAME,
    SESSION_ATTACHMENTS_DIRNAME,
)

#: 内容寻址对象的摘要形状（与 `artifact.BYTE_ARTIFACT_ID_PATTERN` 的摘要段同形）。
#: 恢复只读位时用它逐段校验回执路径（#923）——路径里嵌的 sha 既是推导依据也是形状约束。
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")

#: 回执相对会话目录的固定中段（与 `LocalArtifactStore._session_blob_object_path` 同形）：
#: `<session_dir>/attachments/objects/<sha[:2]>/<sha>`。由共享布局常量拼出，不写字面量
#: （#933 M-16：布局常量只此一份，见 `storage/attachment_layout.py`）。
_RECEIPT_RELATIVE_PARTS = (SESSION_ATTACHMENTS_DIRNAME, BYTE_OBJECTS_DIRNAME)

#: 发布与恢复共用的对象权限。恢复必须与 `LocalArtifactStore._publish_blob` 发布时逐位一致，
#: 否则"恢复"会写在一个与发布态不同的值上。
_OBJECT_MODE = 0o400


def _read_only_retry_handler(cleared_receipts: set[Path]) -> Callable[..., None]:
    """构造 `shutil.rmtree` 的错误回调：清只读位后重试一次删除（#916），并登记改过位的路径。

    注册方式按解释器版本分支，见 `_rmtree_clearing_read_only`：两个分支（`onexc` 收异常
    实例 / `onerror` 收 `sys.exc_info()` 三元组）**共用本工厂产出的同一个回调**——不读第三个
    参数的是内层 `_handler`（形参 `_exc_info`）；工厂本身没有第三个形参，所以版本差异不进
    这里，cleared_receipts 的记录逻辑也就不存在"只修了一条分支"。

    `LocalArtifactStore._publish_blob` 发布的对象是只读的（`os.chmod(target, _OBJECT_MODE)`），
    而会话回执是它的 hardlink ⇒ Windows 上回执与全局对象共享同一只读属性，`os.unlink` 抛
    `PermissionError`；改动前 `rmtree(..., ignore_errors=True)` 会把它静默吞掉，回执因此
    残留。POSIX 上 unlink 不受只读位约束（只看目录写权限），本回调不会被触发。

    只对 `os.unlink` / `os.rmdir` 重试：rmtree 对 `os.open` 等内部操作传的 func 需要更多
    参数，盲目 `func(path)` 会抛 `TypeError`；其余情况直接返回（不抛）。重试仍失败时静默
    放弃——保持改动前 best-effort 的"会话删除不因残留文件而崩溃"语义。

    清位用"原 mode **或上**写位"而不是 `stat.S_IWRITE`：后者把 mode 归一成 0o200（只写），
    在 POSIX 上会连**读**权限一起抹掉（hardlink 共享 inode ⇒ 全局对象也变不可读）。只加写位
    对 Windows 同样有效（`os.chmod` 只认写位来清 FILE_ATTRIBUTE_READONLY）。

    **副作用（Windows 固有，非本回调引入；本段是这一因果的权威叙述）**：hardlink 共享文件
    属性，清回执的只读位会同时清掉其全局对象的只读位——这是删除只读 hardlink 在 Windows 上
    无法回避的代价。**#923 起由调用方收尾**：回调把"确实清过位的只读**文件**回执"路径记进
    `cleared_receipts`，`discard_local_artifacts` 在删除结束后据此恢复对应全局对象的只读位。

    `cleared_receipts` 是**单次 discard 调用的局部状态**：不做全量扫描，也不跨调用累积。

    保留为工厂而非内联进 `_rmtree_clearing_read_only` 的唯一原因是**测试需要注入 spy**
    （`test_discard_callback_receives_version_appropriate_exc` monkeypatch 本符号）；生产调用
    点唯一。
    """

    def _handler(func: Callable[..., object], path: str, _exc_info: object) -> None:
        if func not in (os.unlink, os.rmdir):
            # 改位与重试**同域**：只对可单参重试的两个 func 清只读位。对 `os.open` 等其余
            # func 触发时不碰权限（它们需要更多参数、无法重试，改位会是纯粹的越界副作用）。
            return
        with suppress(OSError):
            os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
            if func is os.unlink:
                # 只登记 `os.unlink`：能共享对象只读属性的只有**文件** hardlink。`os.rmdir`
                # 分支上面那行 chmod 照样执行（否则清位后重试删不掉目录），但它清的是目录的
                # 写位，目录不是对象、不共享全局对象的属性 ⇒ 不登记（形状校验另有一道）。
                cleared_receipts.add(Path(path))
        with suppress(OSError):
            func(path)

    return _handler


def _global_object_for_receipt(receipt: Path, session_dir: Path, root: Path) -> Path | None:
    """回执路径 → 它指向的全局对象路径；形状不符或越界时返回 `None`（纵深防御）。

    回执路径里嵌着 sha256，所以可机械推导：`<session_dir>/attachments/objects/<sha[:2]>/<sha>`
    → `<root>/.attachments/objects/<sha[:2]>/<sha>`。逐段校验形状并 `relative_to`（拒绝 `..`），
    推导结果还必须仍在 `<root>/.attachments/objects/` 之下——这段校验是信任边界，不随"路径
    是 harness 自己拼的"而省略（同 `discard_local_artifacts` 重校验 session_id 的理由）。
    """
    try:
        relative = receipt.relative_to(session_dir)
    except ValueError:
        return None
    parts = relative.parts
    # 中段与 `_RECEIPT_RELATIVE_PARTS` 同源（#933 M-16）：把中段长度当偏移量推导，
    # 而不是写死 `parts[2], parts[3]`——改布局常量时这里跟着走，不会静默错位。
    offset = len(_RECEIPT_RELATIVE_PARTS)
    if len(parts) != offset + 2 or parts[:offset] != _RECEIPT_RELATIVE_PARTS:
        return None
    shard, sha256 = parts[offset], parts[offset + 1]
    if shard != sha256[:2] or not _SHA256_PATTERN.fullmatch(sha256):
        return None
    objects_dir = (root / GLOBAL_ATTACHMENT_DIRNAME / BYTE_OBJECTS_DIRNAME).resolve()
    derived = (objects_dir / shard / sha256).resolve()
    if not derived.is_relative_to(objects_dir):
        return None
    return derived


def _rmtree_clearing_read_only(path: Path, cleared_receipts: set[Path]) -> None:
    """删除目录树，只读文件清位后重试一次（#916）。

    `onerror` 在 Python 3.12 被 `onexc` 取代（CPython `Lib/shutil.py` 起把 `onerror`
    委托给 `onexc`），这里按版本选参数名，形状同 pip
    `src/pip/_internal/utils/misc.py:160-164`（`if sys.version_info >= (3, 12)`）。
    两分支共用 `_read_only_retry_handler` 产出的回调（内层 `_handler` 不读第三个参数）
    ⇒ 不存在逻辑分叉。

    版本判定读在**调用时**的 `sys.version_info`：import 时求值会写死在模块里，
    测试注入替身（以及将来冻进旧解释器的构建）都改不动。

    `cleared_receipts` 由调用方持有（见 `_read_only_retry_handler`）：回调在本轮删除中实际清过
    只读位的回执路径会登记进去，供删除结束后恢复全局对象的只读位（#923）。
    """
    handler = _read_only_retry_handler(cleared_receipts)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=handler)
    else:
        shutil.rmtree(path, onerror=handler)


def _restore_object_read_only(receipt: Path, session_dir: Path, root: Path) -> None:
    """把 `receipt` 指向的全局字节对象恢复成发布时的只读态（#923）。best-effort。

    只对**确实被清过只读位**的回执调用（调用方据 `cleared_receipts` 驱动），失败一律
    `suppress(OSError, RuntimeError)`——对象可能已被并发删除、权限不够、父目录不可写，以及
    `_global_object_for_receipt` 里 `.resolve()` 撞上符号链接环（抛 `RuntimeError`，**不是**
    `OSError`），都不许让会话硬删崩溃（保持 `discard_local_artifacts` 原有的 best-effort 语义）。
    清位副作用的因果见 `_read_only_retry_handler` docstring。

    **升级场景例外**（`LocalArtifactStore._link_session_receipt` docstring）：旧布局下回执可能
    与全局对象**不是同一 inode**（`os.link` 撞 `FileExistsError` 时保留了旧 inode）。此时清回执
    的只读位根本没波及对象，但按 sha 推导出的全局对象路径照常恢复——对已是只读的对象重复
    `chmod` 无害，故不为旧 inode 加特殊分支（Scope Lock）。
    """
    with suppress(OSError, RuntimeError):
        object_path = _global_object_for_receipt(receipt, session_dir, root)
        if object_path is None:
            return
        os.chmod(object_path, _OBJECT_MODE)


def discard_local_artifacts(settings: Settings, session_id: str) -> None:
    """会话硬删时丢弃该会话的**本地** artifact 目录。幂等。

    窄方法，沿用 `WorkspaceRegistry.discard_session_artifacts` 的既有模式
    （ADR-0029 D2）：**不读映射**、只删"用 setting + session_id 自己拼出来的路径"。
    这里的路径完全由 `settings.artifact_dir` 与 session_id 拼成，只有 harness 会往里写，
    所以删除判定是"写死的构造规则"，碰不到用户目录——这正是 ADR-0029 D2 要求的安全形状。

    **三条刻意不做的**（记录在案，不在本票范围）：
    - 配置了 S3/MinIO 时**远端对象不删**（那些 Provider 没有 delete）；
    - 不做任何自动 TTL / 体积清理（ADR-0004 明确不做自动 TTL；ADR-0029 Non-Goals 同款）。
      文本 artifact 仍满足旧口径"生命周期 = 会话生命周期：只要会话还在，它的引用就必然可解析"；
    - **#830 D1 起，附件字节对象的回收已从"会话删除"语义中剥离**：字节对象落在全局内容
      寻址根（`<root>/.attachments/objects/…`，跨会话去重），本函数只删 `<root>/<session_id>`
      （连带回执），**全局字节对象留在原地、不随会话删除回收**。这是相对 D1 之前的行为回归
      （旧布局下字节对象在会话目录内、随目录一起删），当前**刻意接受**（设计文档 §8.2 / PRD D2
      "v1 不做自动清理"）；后续 GC（引用计数 / 宽限期；DSH 有 `gc`、oh-my-pi 有 `omp gc`）
      是独立的 follow-up 票，不由本函数承担。别把这里的"幂等删除"读成"字节也被回收了"。

    Windows 上删除只读回执会连带清掉其全局对象的只读位（hardlink 共享属性 ⇒ 副作用原文见
    `_read_only_retry_handler` docstring），本函数在删除结束后把本轮实际清过位的回执恢复成
    发布时的只读态（#923，见 `_restore_object_read_only`）；恢复是 best-effort，失败不影响
    会话删除本身。
    """
    root = settings.artifact_dir.strip()
    if not root:
        return
    if not SESSION_KEY_PATTERN.fullmatch(session_id):
        # 硬删入口已校验过 id 形态（422），这里再挡一次：本函数只允许删自己拼得出的路径。
        return
    root_path = Path(root).resolve()
    session_dir = root_path / session_id
    if session_dir.is_dir():
        # 回执是只读对象的 hardlink：Windows 上直接 rmtree 删不掉（旧的 ignore_errors
        # 会静默吞掉、留下回执）。改用清只读位后重试的错误回调，
        # 见 `_rmtree_clearing_read_only` / `_read_only_retry_handler`。
        cleared_receipts: set[Path] = set()
        _rmtree_clearing_read_only(session_dir, cleared_receipts)
        # 收尾（#923）：本轮清过只读位的回执背后是**共享同一属性**的全局字节对象，删完把它们
        # 恢复成发布时的只读态（清位副作用的因果见 `_read_only_retry_handler` docstring）。
        # POSIX 上 unlink 不看只读位 ⇒ cleared_receipts 为空 ⇒ no-op。
        for receipt in cleared_receipts:
            _restore_object_read_only(receipt, session_dir, root_path)


__all__ = ["discard_local_artifacts"]
