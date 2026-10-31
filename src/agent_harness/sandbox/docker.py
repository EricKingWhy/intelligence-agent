"""Docker-backed Sandbox with an optional, lazily loaded SDK dependency."""

from __future__ import annotations

import errno
import fnmatch
import importlib
import io
import logging
import os
import posixpath
import queue
import shlex
import shutil
import stat
import tarfile
import tempfile
import threading
import weakref
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AbstractContextManager, suppress
from pathlib import Path, PurePosixPath
from time import perf_counter, sleep
from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

from agent_harness.sandbox.base import (
    ExecResult,
    Sandbox,
    ShellEnvironment,
    ShellFamily,
)
from agent_harness.sandbox.decoding import StreamDecoder
from agent_harness.sandbox.local import DEFAULT_EXEC_TIMEOUT, _CappedCapture

logger = logging.getLogger("agent_harness.sandbox.docker")


class _ContainerExecState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cleanup_failed = False
        self.cleanup_pending = False
        self.late_cleanup: tuple[object, object] | None = None
        self.late_cleanup_event = threading.Event()
        # 只保护 `late_cleanup` / `cleanup_pending` 这一对的**交接**（读-清原子化）。
        # 与 `lock` 分开：`lock` 要跨越 drain 里 0.5s+5s 的外部调用，不能用来做交接。
        self.late_cleanup_lock = threading.Lock()


def _glob_match_posix(rel_path: str, pattern: str) -> bool:
    """对容器内相对路径做 glob 匹配，支持 ** 递归（与 LocalSubprocessSandbox._glob_match 同语义）。"""
    if pattern in ("", "*"):
        return True
    if "**" in pattern:
        normalized = pattern.replace("**/", "").replace("**", "*")
        return fnmatch.fnmatch(rel_path, normalized) or fnmatch.fnmatch(
            PurePosixPath(rel_path).name, normalized
        )
    return fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(
        PurePosixPath(rel_path).name, pattern
    )


def _export_data_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    filtered = tarfile.data_filter(member, destination)
    # data_filter grants owner write access for extraction. Preserve a POSIX
    # source's read-only bit without restoring unsafe mode bits or link paths.
    if (filtered is not None and (filtered.isfile() or filtered.islnk()) and os.name != "nt"
            and filtered.mode is not None and not member.mode & stat.S_IWUSR):
        return filtered.replace(mode=filtered.mode & ~stat.S_IWUSR)
    return filtered


def _validate_windows_archive_members(members: list[tarfile.TarInfo]) -> None:
    reserved_names = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
        f"{prefix}{number}"
        for prefix in ("com", "lpt")
        for number in (*range(1, 10), "¹", "²", "³")
    }
    invalid_characters = set('<>:"|?*')
    for member in members:
        name = (
            member.name[:-1]
            if member.isdir() and member.name.endswith("/")
            else member.name
        )
        parts = name.split("/")
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or any(part in {"", ".", ".."} for part in parts)
            or any(
                part.endswith((".", " "))
                or any(
                    ord(character) < 0x20 or character in invalid_characters
                    for character in part
                )
                or part.split(".", 1)[0].casefold() in reserved_names
                for part in parts
            )
        ):
            raise ValueError("Docker workspace archive contains a Windows-unsafe path")


def _retry_readonly_cleanup(
    function: Callable[..., object],
    path: str,
    error_info: tuple[type[BaseException], BaseException, object],
) -> None:
    error = error_info[1]
    if os.name != "nt" or not isinstance(error, PermissionError):
        raise error
    os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    function(path)


def _remove_export_path(path: Path) -> None:
    try:
        if path.is_symlink() or not path.is_dir():
            try:
                path.unlink()
            except PermissionError:
                if os.name != "nt":
                    raise
                os.chmod(path, path.stat().st_mode | stat.S_IWRITE)
                path.unlink()
            return
        shutil.rmtree(path, onerror=_retry_readonly_cleanup)
    except FileNotFoundError:
        return


def _open_within_pinned_parent(parent_fd: int, name: str, flags: int) -> int:
    """相对已 pin 住的父目录 fd 打开组件，O_NOFOLLOW 拒绝换链（#1301）。

    ELOOP / ENOTDIR（组件被换成 symlink / 类型改变）按 Windows 分支同一契约
    fail-closed；其余错误原样上抛（含 FileNotFoundError：条目在归档中途消失
    与基线按路径遍历的行为一致）。
    """
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        # ELOOP：组件被换成 symlink（O_NOFOLLOW 拒绝）；ENOTDIR：目录组件被
        # 换成非目录；ENXIO：文件组件被换成 unix socket（O_RDONLY 打开报
        # ENXIO）。三者都按 Windows 分支同一契约 fail-closed。其余错误原样
        # 上抛（含 FileNotFoundError：条目在归档中途消失与基线按路径遍历的
        # 行为一致）。
        if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENXIO):
            raise PermissionError(
                "copy_in source changed during archive"
            ) from error
        raise


def _apply_owner(member: tarfile.TarInfo, owner: tuple[int, int] | None) -> None:
    """给 tar 成员写显式属主（#1486：daemon 以 root 解包）。

    `owner is None` 表示调用方要求保留成员自身观测到的属主（copy_in 的宿主
    文件语义）；非 None 时一律用它，保证容器内落盘文件归容器用户。
    """
    if owner is not None:
        member.uid, member.gid = owner


def _add_pinned_leaf_member(
    archive: tarfile.TarFile,
    parent_fd: int,
    name: str,
    member_name: str,
    owner: tuple[int, int] | None = None,
) -> None:
    """symlink / fifo / 设备成员：不经路径打开，dir_fd 两次取稳定快照（#1301）。

    symlink 本体进 tar（dereference=False 语义不变，不跟随）；linkname 与
    lstat 各读两次逐项比对，归档中途被换链/重建即 fail-closed。快照稳定后
    再发生的换链不影响本次归档——成员描述的是 pin 住时刻观察到的对象，
    与常规文件"fd pin 住后读旧 inode"同一语义。
    """

    def _snapshot() -> tuple[os.stat_result, str]:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        linkname = (
            os.readlink(name, dir_fd=parent_fd)
            if stat.S_ISLNK(entry_stat.st_mode)
            else ""
        )
        return entry_stat, linkname

    first_stat, first_link = _snapshot()
    second_stat, second_link = _snapshot()
    if (first_stat.st_ino, first_stat.st_mtime_ns, first_link) != (
        second_stat.st_ino,
        second_stat.st_mtime_ns,
        second_link,
    ):
        raise PermissionError("copy_in source changed during archive")

    member = tarfile.TarInfo(member_name)
    member.mode = first_stat.st_mode
    if owner is not None:
        member.uid, member.gid = owner
    else:
        member.uid = first_stat.st_uid
        member.gid = first_stat.st_gid
    member.mtime = first_stat.st_mtime
    member.linkname = first_link
    if stat.S_ISLNK(first_stat.st_mode):
        member.type = tarfile.SYMTYPE
    elif stat.S_ISFIFO(first_stat.st_mode):
        member.type = tarfile.FIFOTYPE
    elif stat.S_ISCHR(first_stat.st_mode) or stat.S_ISBLK(first_stat.st_mode):
        member.type = (
            tarfile.CHRTYPE if stat.S_ISCHR(first_stat.st_mode) else tarfile.BLKTYPE
        )
        if hasattr(os, "major") and hasattr(os, "minor"):
            member.devmajor = os.major(first_stat.st_rdev)
            member.devminor = os.minor(first_stat.st_rdev)
    else:
        # socket 等 tarfile.gettarinfo 同样映射不了的类型，与 Windows 分支同
        # 契约 fail-closed。
        raise PermissionError("copy_in accepts only regular files and directories")
    archive.addfile(member)


def _walk_fd_tree(
    archive: tarfile.TarFile,
    parent_fd: int,
    parent_arc: str,
    dir_flags: int,
    file_flags: int,
    owner: tuple[int, int] | None = None,
) -> None:
    """从已 pin 住的目录 fd 相对遍历并归档整棵子树（#1301）。

    每个组件都用 `os.open(O_NOFOLLOW, dir_fd=已 pin 的祖先 fd)` 相对打开
    （与 `skills/inspection.py` 的安全读同型；Go 1.24 `os.Root`/`OpenInRoot`
    的逐组件 openat(O_NOFOLLOW|O_DIRECTORY) 同思路），文件内容一律从已打开
    的 fd 读取，绝不按路径名二次打开。凡是能让读取**逃出 pin 住的树**的
    换链（组件被换成 symlink ⇒ ELOOP、目录组件换成非目录 ⇒ ENOTDIR、文件
    组件换成 socket ⇒ ENXIO）一律 fail-closed；同一 pin 树内的对象替换
    （rename 换名、内容重写）不被放大为逃逸——读取自 fd，描述的是 pin 住
    时刻观察到的对象。symlink/fifo/设备成员的稳定快照见
    `_add_pinned_leaf_member`；归档中途条目消失按基线行为原样上抛
    FileNotFoundError。
    """
    with os.scandir(parent_fd) as scanner:
        entries = sorted(scanner, key=lambda entry: entry.name)
    for entry in entries:
        member_name = f"{parent_arc}/{entry.name}"
        if entry.is_symlink():
            _add_pinned_leaf_member(
                archive, parent_fd, entry.name, member_name, owner,
            )
            continue
        entry_stat = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(entry_stat.st_mode):
            dir_fd = _open_within_pinned_parent(parent_fd, entry.name, dir_flags)
            try:
                member = archive.gettarinfo(
                    arcname=member_name,
                    fileobj=SimpleNamespace(name="", fileno=lambda fd=dir_fd: fd),
                )
                if member is None or not member.isdir():
                    raise PermissionError("copy_in source changed during archive")
                _apply_owner(member, owner)
                archive.addfile(member)
                _walk_fd_tree(
                    archive, dir_fd, member_name, dir_flags, file_flags, owner,
                )
            finally:
                os.close(dir_fd)
        elif stat.S_ISREG(entry_stat.st_mode):
            # O_NONBLOCK：常规文件打开无效果；若组件在 scandir 之后被换成
            # fifo，可避免打开时阻塞，由下面的 fstat 复核 fail-closed。
            fd = _open_within_pinned_parent(parent_fd, entry.name, file_flags)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise PermissionError("copy_in source changed during archive")
                stream = os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
            try:
                member = archive.gettarinfo(
                    arcname=member_name,
                    fileobj=SimpleNamespace(name="", fileno=stream.fileno),
                )
                if member is None or not (member.isreg() or member.islnk()):
                    raise PermissionError("copy_in source changed during archive")
                _apply_owner(member, owner)
                archive.addfile(member, stream)
            finally:
                stream.close()
        else:
            _add_pinned_leaf_member(archive, parent_fd, entry.name, member_name)


def _archive_posix_tree_fd_relative(
    archive: tarfile.TarFile,
    source: Path,
    root_name: str,
    owner: tuple[int, int] | None = None,
) -> None:
    """copy_in 非 Windows 分支：fd 相对归档源树，不按路径二次遍历（#1301）。

    修补前 `archive.add(source, recursive=True)` 按**路径**二次遍历整棵树
    （lstat/open 之间的窗口可被并发换链利用：先按路径 lstat 到常规文件、
    再按路径 open 时已被换成指向源根外的 symlink ⇒ 源根外内容被打进 tar，
    绕过 `_reject_escaping_symlinks` 的 fail-closed 边界）。现改为：pin 住
    根 fd 后逐组件 dir_fd 相对打开，所有读取都从 fd 出发；Windows 分支的
    `_pin_host_path_tree_for_archive` 在 POSIX 的等价实现。

    行为契约保持：`dereference=False` 语义不变（symlink 本体进 tar，不跟随；
    完全位于源树内的 hard-link pair 经 gettarinfo 的 inode 去重仍归档为
    LNKTYPE 成员）；FileNotFoundError（根缺失 / 条目消失）与 put_archive
    失败的 RuntimeError 抛错点不变；tar 成员名（`root_name` 树形）不变。
    两处**有意收紧**（与 Windows 分支同契约，非行为保持）：根被换成 symlink
    由"归档出 SYMTYPE 成员"改为 PermissionError 响亮失败；根/成员被换成
    unix socket 由基线的静默跳过（`TarFile.add` 对 gettarinfo→None 不报错）
    改为 PermissionError 响亮失败。
    """
    dir_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_BINARY", 0)
    )
    if (
        os.open not in os.supports_dir_fd
        or os.scandir not in os.supports_fd
        or os.stat not in os.supports_dir_fd
        or os.readlink not in os.supports_dir_fd
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
    ):
        raise OSError("fd-relative copy_in archiving is unavailable on this platform.")

    try:
        # 根不带 O_DIRECTORY：copy_in 的根允许是常规文件（契约"regular files
        # and directories"），目录/文件由 fstat 分派；O_NOFOLLOW 拒绝换链成
        # symlink 的根，O_NONBLOCK 防根被换成 fifo 时阻塞在打开上。
        root_fd = os.open(source, file_flags)
    except OSError as error:
        # ELOOP：根被换成 symlink；ENXIO：根被换成 unix socket——与组件级
        # 映射同构的 fail-closed（基线分别会归档出 SYMTYPE 成员 / 静默跳过）。
        if error.errno in (errno.ELOOP, errno.ENXIO):
            raise PermissionError(
                "copy_in source changed during archive"
            ) from error
        raise
    try:
        root_stat = os.fstat(root_fd)
        if stat.S_ISDIR(root_stat.st_mode):
            member = archive.gettarinfo(
                arcname=root_name,
                fileobj=SimpleNamespace(name="", fileno=lambda fd=root_fd: fd),
            )
            if member is None or not member.isdir():
                raise PermissionError("copy_in source changed during archive")
            _apply_owner(member, owner)
            archive.addfile(member)
            _walk_fd_tree(
                archive, root_fd, root_name, dir_flags, file_flags, owner,
            )
        elif stat.S_ISREG(root_stat.st_mode):
            stream = os.fdopen(root_fd, "rb")
            root_fd = -1  # 所有权转移给 stream
            try:
                member = archive.gettarinfo(
                    arcname=root_name,
                    fileobj=SimpleNamespace(name="", fileno=stream.fileno),
                )
                if member is None or not (member.isreg() or member.islnk()):
                    raise PermissionError("copy_in source changed during archive")
                _apply_owner(member, owner)
                archive.addfile(member, stream)
            finally:
                stream.close()
        else:
            raise PermissionError(
                "copy_in accepts only regular files and directories"
            )
    finally:
        if root_fd >= 0:
            os.close(root_fd)


#: #1486 基础降权档：容器用户 / 丢弃的能力 / 安全选项。
#: - 非 root：默认 `python:3-slim`（及无 USER 的自定义镜像）按 uid 0 跑，
#:   新容器一律显式落到该 uid:gid；自定义镜像已声明非 root USER 时尊重镜像
#:   声明（见 `_resolve_container_user`），绝不静默跳过降权。
#: - `cap_drop=ALL`：Docker 默认能力集（CHOWN / DAC_OVERRIDE / SETUID …）全部丢弃。
#: - `no-new-privileges`：禁止 setuid/setgid 提权。
#: 保留构建网络（不设 `network_mode`）与可写工作卷（业务要求）。
DEFAULT_CONTAINER_USER = "1000:1000"
HARDENED_CAP_DROP = ["ALL"]
HARDENED_SECURITY_OPT = ["no-new-privileges:true"]

#: 具名卷属主迁移容器：以 uid 0 起、只保留 chown/chmod 所需的能力，且
#: `--network none`（迁移不需要网络）。工作区容器自身已 `cap_drop=ALL`，
#: 不能借它 chown（#1486 探针实测：非 root + cap_drop=ALL 下 chown 报 EPERM）。
#: `FSETID` 必须给：`chown` 会清掉 setgid 位，而把 setgid 位加回去要
#: `CAP_FSETID`（调用者 gid 不属于文件新组时缺它就静默丢位）——缺了它，
#: 卷里只要有一个 setgid 条目，迁移的复核就永远失败、工作区永久不可用。
_OWNERSHIP_HELPER_CAPS = ["CHOWN", "FOWNER", "DAC_OVERRIDE", "FSETID"]

#: 迁移备份卷：`<workspace volume>-migration-backup`，挂在 helper 的 /backup。
#: 备份 tar 留在**独立卷**里而不是 helper 的 /tmp/用户卷：helper 被硬杀时
#: 备份仍在，重试可先恢复再迁移（可验证的恢复路径，AC2）。
_BACKUP_MOUNT = "/backup"
_BACKUP_SUFFIX = "-migration-backup"
#: 迁移 helper 容器的认领 label：`containers.run` 在 start 失败时不返回容器对象，
#: 只能事后按 label 找回来清理（见 `_remove_helper_containers`）。
_HELPER_LABEL = "agent-harness.migration-helper"

#: 迁移 helper 的等待上限。迁移成本 = 两次全树 find + 一次整卷 tar + chown，
#: 随卷大小线性增长；15 分钟对常见工作区（GB 级）留有充分余量，同时保证
#: daemon 卡死时不会让会话启动永久挂住（复核 P3-2）。
_MIGRATION_WAIT_SECONDS = 900

#: 超时后给 helper 的优雅停止窗口（SIGTERM → SIGKILL）。脚本没装 trap，这一步
#: 换不到「脚本自己回滚」，只是把「正在 chown 时被立刻 SIGKILL」变成「给它几秒
#: 收尾」；真正的「超时不动数据」需要脚本在 chown 前写标记、恢复侧优先回滚，
#: 超出本次修复范围（已登记为已知项）。
_MIGRATION_STOP_GRACE_SECONDS = 10

#: 属主迁移脚本（bash；默认 `python:3-slim` 自带）。步骤：
#: 0) 备份卷里已有 `pre.tar` → 上次迁移没走完，先核对 `pre.idx` 记录的字节数
#:    （缺 idx 的旧备份退化成清单比对）、再解到临时树、最后换入（幂等重试）；
#:    任何一步不过都不动活数据、保留备份并响亮失败（含人工恢复指引）；
#: 1) 卷内已全部是目标属主 → 直接成功（重启/重开无副作用）；
#: 2) 整树 tar 快照先写 `pre.tar.tmp` 再原子改名（numeric owner：模式/属主/
#:    符号链接/空目录/hard link）落独立备份卷，并落一份「模式/类型/路径」索引；
#: 3) `chown -R` 到目标 uid:gid；
#: 4) 按快照恢复 chown 清掉的 setuid/setgid 位（迁移只改属主，不改权限）；
#: 5) 复核：属主全部为目标，且「模式/类型/路径」逐条 NUL 记录比较不变。
#: 任何一步失败 → 回滚到快照再响亮失败；回滚本身不可验证 → 再响亮一级
#: （`MIGRATE_FATAL`）。路径可含空格/制表符/换行，故索引一律按 NUL 记录比较。
_OWNER_MIGRATION_SCRIPT = r"""set -u
cd /workspace || { printf 'MIGRATE_ERROR cannot enter /workspace\n'; exit 90; }
BACKUP=$3
U=$1
G=$2
D=/tmp/.harness-migration
# 目标必须是数字：镜像声明 `USER name:group` 时若上游漏了名字解析，`find -uid`
# 会报 non-numeric 且输出为空——那绝不能被当成「已全部合规」。
case "${U}${G}" in
  *[!0-9]*|'') printf 'MIGRATE_ERROR non-numeric target %s:%s\n' "$U" "$G"; exit 99 ;;
esac
rm -rf "$D"
mkdir -p "$D" "$BACKUP" || { printf 'MIGRATE_ERROR cannot create snapshot dir\n'; exit 91; }
# 只有 pre.tar.tmp 没有 pre.tar = 上次死在做快照，还没动过活数据；清掉残件
# 免得陈旧 pre.idx 影响下一次。
rm -f "$BACKUP/pre.tar.tmp" "$BACKUP/pre.idx.tmp"

# 从备份恢复。备份可能是上次被硬杀留下的：先核对落盘时记录的字节数、再解到
# **临时树**，全部成功后才动 /workspace。任何一步不过都直接返回非零，活数据保持
# 原样、备份留在备份卷里——恢复动作本身绝不再是破坏源。
# 为什么不能只看 `tar -xpf` 的退出码：归档正好在 512 字节成员边界被截断时，
# tar 把它当成「到这儿就结束了的合法归档」，退出码 0 且只解出前几个成员（实测：
# 5 个成员、40960 字节的归档截到 9728 字节 → `-xpf` rc=0，只解出 ./、./d/、f4）。
# 那样下面会先清空 /workspace 再搬进残缺备份 = 静默丢文件。字节数是最小充分的
# 不变量：任何截断都改变归档大小，而 `pre.idx` 在改名**之前**写的是同一份内容。
# `pre.idx` 是本版本才有的：升级前留下的完整备份只有 pre.tar，此时退化成
# 「tar 全量可读 + 解到临时树 + 与活树『类型/路径』清单逐条一致」，仍然只动
# 临时树；成员边界截断被清单比对拦下（那种截断 tar 自己的退出码是 0）。
restore_from_backup() {
  R="$D/restore"
  rm -rf "$R"
  mkdir -p "$R" || return 1
  if [ -f "$BACKUP/pre.idx" ]; then
    [ "$(stat -c %s "$BACKUP/pre.tar")" = "$(cat "$BACKUP/pre.idx")" ] || return 1
  else
    tar -tf "$BACKUP/pre.tar" > /dev/null || return 1
  fi
  tar -xpf "$BACKUP/pre.tar" --numeric-owner -C "$R" || return 1
  if [ ! -f "$BACKUP/pre.idx" ]; then
    ( cd "$R" && find . -mindepth 1 -printf '%y\t%p\0' | LC_ALL=C sort -z ) > "$D/restore.rest" || return 1
    find . -mindepth 1 -printf '%y\t%p\0' | LC_ALL=C sort -z > "$D/live.rest" || return 1
    cmp -s "$D/restore.rest" "$D/live.rest" || return 1
  fi
  find . -mindepth 1 -maxdepth 1 -exec rm -rf {} + || return 1
  ( cd "$R" && find . -mindepth 1 -maxdepth 1 -exec mv -f -t /workspace {} + ) || return 1
  rm -rf "$R"
}

if [ -f "$BACKUP/pre.tar" ]; then
  printf 'MIGRATE_RECOVER previous incomplete migration\n'
  restore_from_backup || {
    printf 'MIGRATE_ERROR backup restore failed; backup kept at %s/pre.tar (retry is safe, the backup is not modified)\n' "$BACKUP"
    printf 'MIGRATE_HINT inspect it read-only: docker run --rm -v %s:/backup <image> tar -tvf /backup/pre.tar\n' "$BACKUP"
    exit 89
  }
  rm -f "$BACKUP/pre.tar" "$BACKUP/pre.idx"
fi

# 快路径只在 find 成功时成立：find 报错（非数字 uid、卷不可读）必须响亮失败。
mismatch=$(find . -xdev \( ! -uid "$U" -o ! -gid "$G" \) -print -quit) \
  || { printf 'MIGRATE_ERROR cannot inspect workspace\n'; exit 99; }
if [ -z "$mismatch" ]; then
  printf 'MIGRATE_OK records=0 target=%s:%s (already owned)\n' "$U" "$G"
  exit 0
fi

# 快照先写 .tmp 再原子改名：被杀时留下的是 .tmp，「pre.tar 存在 ⇒ 完整」。
# 同时把归档字节数记进 pre.idx（也先写 .tmp 再改名，且先于 pre.tar 落位）：
# 恢复侧靠它辨认「成员边界截断」这类 tar 自己不报错的残缺归档。
tar -cpf "$BACKUP/pre.tar.tmp" --numeric-owner . \
  || { printf 'MIGRATE_ERROR snapshot failed\n'; exit 92; }
wc -c < "$BACKUP/pre.tar.tmp" | tr -d ' ' > "$BACKUP/pre.idx.tmp" \
  || { printf 'MIGRATE_ERROR snapshot index failed\n'; exit 92; }
mv -f "$BACKUP/pre.idx.tmp" "$BACKUP/pre.idx" \
  || { printf 'MIGRATE_ERROR snapshot index rename failed\n'; exit 92; }
mv -f "$BACKUP/pre.tar.tmp" "$BACKUP/pre.tar" \
  || { printf 'MIGRATE_ERROR snapshot rename failed\n'; exit 92; }
find . -xdev -printf '%U:%G\t%m\t%y\t%p\0' > "$D/pre.bin" || { printf 'MIGRATE_ERROR manifest failed\n'; exit 94; }
find . -xdev -printf '%m\t%y\t%p\0' | LC_ALL=C sort -z > "$D/pre.rest" || { printf 'MIGRATE_ERROR manifest failed\n'; exit 94; }
records=$(tr -cd '\000' < "$D/pre.bin" | wc -c)
[ "$records" -ge 1 ] || { printf 'MIGRATE_ERROR manifest empty\n'; exit 95; }
printf 'MIGRATE_SNAPSHOT records=%s bytes=%s\n' "$records" "$(wc -c < "$BACKUP/pre.tar")"

rest_index() { find . -xdev -printf '%m\t%y\t%p\0' | LC_ALL=C sort -z > "$1"; }

rollback() {
  printf 'MIGRATE_ROLLBACK begin\n'
  tar -xpf "$BACKUP/pre.tar" --numeric-owner -C /workspace || { printf 'MIGRATE_ROLLBACK_FAILED\n'; return 1; }
  rest_index "$D/rb.rest" || { printf 'MIGRATE_ROLLBACK_FAILED\n'; return 1; }
  cmp -s "$D/pre.rest" "$D/rb.rest" || { printf 'MIGRATE_ROLLBACK_FAILED\n'; return 1; }
  rm -f "$BACKUP/pre.tar" "$BACKUP/pre.idx"
  printf 'MIGRATE_ROLLBACK_OK\n'; return 0
}
abort_with_rollback() { printf '%s\n' "$1"; rollback || { printf 'MIGRATE_FATAL rollback failed\n'; exit 96; }; printf 'MIGRATE_ABORTED\n'; exit "$2"; }

chown -R "$U:$G" /workspace || abort_with_rollback 'MIGRATE_ERROR chown failed' 97

while IFS=$'\t' read -r -d '' _ mode kind path; do
  [ -n "$path" ] || continue
  [ "$kind" = "l" ] && continue
  chmod "$mode" "$path" || abort_with_rollback "MIGRATE_ERROR chmod restore failed: $path" 98
done < "$D/pre.bin"

find . -xdev -printf '%U:%G\t%m\t%y\t%p\0' > "$D/post.bin" || abort_with_rollback 'MIGRATE_ERROR post manifest failed' 100
if ! awk -v want="$U:$G" 'BEGIN{RS="\0"} { i=index($0, "\t"); if (i == 0 || substr($0,1,i-1) != want) { print "MIGRATE_MISMATCH " $0; bad=1 } } END { exit bad }' "$D/post.bin"; then
  abort_with_rollback 'MIGRATE_ERROR ownership verification failed' 101
fi
rest_index "$D/post.rest" || abort_with_rollback 'MIGRATE_ERROR post manifest failed' 100
cmp -s "$D/pre.rest" "$D/post.rest" || abort_with_rollback 'MIGRATE_ERROR content or mode changed' 102
rm -rf "$D"
rm -f "$BACKUP/pre.tar" "$BACKUP/pre.idx"
printf 'MIGRATE_OK records=%s target=%s:%s\n' "$records" "$U" "$G"
"""


class SandboxHardeningError(RuntimeError):
    """容器未满足基础降权档，且无法在不破坏数据的前提下自动处置。"""


class DockerSandbox(Sandbox):
    """Run Coding Tools inside an isolated Docker container."""

    _container_exec_states: ClassVar[weakref.WeakValueDictionary[str, _ContainerExecState]] = weakref.WeakValueDictionary()
    _container_exec_states_lock = threading.Lock()
    _poisoned_container_exec_states: ClassVar[dict[str, _ContainerExecState]] = {}

    def __init__(
        self,
        *,
        image: str = "python:3-slim",
        container_name: str | None = None,
        volume_name: str | None = None,
    ) -> None:
        try:
            docker = importlib.import_module("docker")
        except ModuleNotFoundError as error:
            raise RuntimeError("DockerSandbox 需要 pip install docker") from error
        self._client = docker.from_env(use_context=False)
        suffix = uuid4().hex
        self._image = image
        self._container_name = container_name or f"agent-harness-{suffix}"
        self._volume_name = volume_name or f"agent-harness-{suffix}"
        self._container = None
        #: 当前容器用户的数字 uid/gid（put_archive 显式属主用）；随容器建立时缓存，
        #: 不在每次文件写入上多打一次 daemon。
        self._owner_uid_gid: tuple[int, int] | None = None
        self._workspace_restore_guard: Callable[
            [], AbstractContextManager[None]
        ] | None = None
        self._exec_state = self._state_for_container(self._container_name)
        self._exec_lock = self._exec_state.lock

    @classmethod
    def _state_for_container(cls, container_name: str) -> _ContainerExecState:
        with cls._container_exec_states_lock:
            state = cls._poisoned_container_exec_states.get(container_name)
            if state is not None:
                return state
            state = cls._container_exec_states.get(container_name)
            if state is None:
                state = _ContainerExecState()
                cls._container_exec_states[container_name] = state
            return state

    @property
    def workspace_root(self) -> PurePosixPath:
        return PurePosixPath("/workspace")

    @property
    def shell_environment(self) -> ShellEnvironment:
        """容器内固定用 `/bin/sh -lc`（见 exec）——OBS-012：是 sh，不是 bash。"""
        return ShellEnvironment(name="/bin/sh", family=ShellFamily.POSIX_SH)

    def ensure_started(self) -> None:
        restore_guard = getattr(self, "_workspace_restore_guard", None)
        if restore_guard is None:
            self._ensure_started()
            return
        with restore_guard():
            self._ensure_started()

    def _ensure_started(self) -> None:
        if self._container is not None:
            self._container.reload()
            if self._container.status == "running":
                return
            self._container.start()
            return

        # 跨进程恢复：按确定性 container_name 查找已存在的容器。
        # 进程重启后 self._container 为 None，但容器可能还在（停止状态）。
        # 如果找到，先**检验降权档**：合规才复用；历史未硬化容器保留其卷、
        # 显式重建（#1486 AC1/AC3——绝不静默复用未硬化容器）。
        docker = importlib.import_module("docker")
        try:
            existing = self._client.containers.get(self._container_name)
        except docker.errors.NotFound:
            existing = None
        if existing is not None:
            existing.reload()
            if self._is_hardened(existing.attrs):
                if existing.status != "running":
                    existing.start()
                self._container = existing
                self._owner_uid_gid = self._parse_owner(existing.attrs)
                return
            logger.warning(
                "Replacing unhardened historical container %s "
                "(user=%r cap_drop=%r security_opt=%r)",
                self._container_name,
                existing.attrs.get("Config", {}).get("User"),
                existing.attrs.get("HostConfig", {}).get("CapDrop"),
                existing.attrs.get("HostConfig", {}).get("SecurityOpt"),
            )
            existing.remove(force=True)

        self._create_hardened_container()

    # —— #1486 基础降权档 ——

    @staticmethod
    def _is_hardened(attrs: dict) -> bool:
        """inspect attrs 是否满足降权档：非 root + cap_drop=ALL + no-new-privileges。

        这是**复用路径唯一的安全门**，所以按结构化白名单判，不看"像不像"：

        - `Config.User` 必须解析出数字 uid 且 `uid != 0`。`0:1000`（root uid、
          非 root gid）不算非 root；名字解析不出数字（`nobody`）**拒绝复用**，
          不猜。
        - `CapDrop` 精确等于 `["ALL"]`，且 `CapAdd` 为空——`cap_drop=ALL` 上再
          `cap_add=SYS_ADMIN` 等于没丢。
        - `SecurityOpt` 必须含精确项 `no-new-privileges:true`；子串匹配会把
          `no-new-privileges:false` 也放过去（内核里是 NoNewPrivs=0）。
        - `Privileged` 必须显式为 False——privileged 容器会关掉 seccomp，
          上面几条字段看着合规、实际完全没隔离。
        - `NetworkMode != "host"`：票面保留的是构建网络（bridge），不是宿主
          网络栈。
        `init` 不参与判定：它不是本票降权档的一部分（见 create 处注释）。
        """
        user = str((attrs.get("Config") or {}).get("User") or "")
        uid_text, _, _ = user.partition(":")
        if not uid_text.isdecimal() or int(uid_text) == 0:
            return False
        host = attrs.get("HostConfig") or {}
        cap_drop = sorted(str(item).upper() for item in host.get("CapDrop") or [])
        if cap_drop != ["ALL"]:
            return False
        if [str(item) for item in host.get("CapAdd") or []]:
            return False
        if host.get("Privileged") is not False:
            return False
        security_opt = [str(opt) for opt in host.get("SecurityOpt") or []]
        if "no-new-privileges:true" not in security_opt:
            return False
        return str(host.get("NetworkMode") or "") != "host"

    def _resolve_container_user(self) -> str:
        """返回新容器要用的 `user`：尊重镜像已声明的非 root USER，否则降权档。

        无 USER / USER=root 的自定义镜像一律落到 `DEFAULT_CONTAINER_USER`，
        绝不因"镜像自己说是 root"就跳过降权。
        """
        try:
            image_attrs = self._client.images.get(self._image).attrs or {}
        except Exception as error:  # noqa: BLE001 — 读不到镜像属性按无声明处理
            logger.warning("无法读取镜像 %s 的 USER 声明：%s", self._image, error)
            return DEFAULT_CONTAINER_USER
        declared = str((image_attrs.get("Config") or {}).get("User") or "").strip()
        if not declared or declared.split(":", 1)[0] in ("root", "0"):
            return DEFAULT_CONTAINER_USER
        if declared.isdecimal():
            # 只声明 uid 时 Docker 的组会落到 0（root 组）——显式补同名 gid，
            # 不让降权档出现 root 组。
            return f"{declared}:{declared}"
        # 带 `:` 的声明不能直接信：`USER nobody:nogroup` / `USER app:app` 是
        # 名字而不是数字，原样下传会让迁移脚本的 `find -uid` 报 non-numeric
        # 从而「空输出 = 已合规」误报成功，属主永远迁不过去。逐段判定：
        # 两段都已是十进制才原样使用，任一段是名字就进镜像解析。
        uid_part, _, gid_part = declared.partition(":")
        if uid_part.isdecimal() and (gid_part or uid_part).isdecimal():
            return declared
        # 名字（裸名字或 name:group）一律用镜像自身解析出数字 uid/gid，
        # 保证挂载属主可预测；解析不出就响亮失败，不猜。
        resolved = self._resolve_image_uid_gid(declared)
        if resolved is None:
            raise SandboxHardeningError(
                f"无法在镜像 {self._image} 内解析用户 {declared!r} 的 uid/gid，"
                "无法保证挂载属主适配（拒绝以不确定属主启动）。"
            )
        return resolved

    def _resolve_image_uid_gid(self, declared: str) -> str | None:
        """在镜像内解析 `user` / `user:group` / `uid:group` 为数字 uid:gid。"""
        user_part, _, group_part = declared.partition(":")
        user_part = user_part or "0"
        group_part = group_part or user_part
        try:
            output = self._client.containers.run(
                self._image,
                [
                    "sh", "-c",
                    f"id -u {shlex.quote(user_part)}; id -g {shlex.quote(group_part)}",
                ],
                remove=True,
                entrypoint="",
                user="0:0",
                cap_drop=HARDENED_CAP_DROP,
                security_opt=HARDENED_SECURITY_OPT,
                network_mode="none",
            )
        except Exception as error:  # noqa: BLE001 — 解析失败退回镜像声明
            logger.warning("镜像用户 %r 解析失败：%s", declared, error)
            return None
        parts = output.decode("utf-8", "replace").split()
        if len(parts) == 2 and all(part.isdecimal() for part in parts):
            return f"{parts[0]}:{parts[1]}"
        return None

    def _create_hardened_container(self) -> None:
        user = self._resolve_container_user()
        # `containers.create` 不带 docker-py 的自动 pull（那是 `containers.run` 的
        # 行为）：镜像不在本地就直接 ImageNotFound。直接构造 DockerSandbox(image=…)
        # 的调用方与测试走的就是这条路，所以这里显式补一次。
        # 读取失败一律只记 debug：真正的错误由紧随其后的 create 报出来，别在这里
        # 换成另一条更难懂的错误（注入的 fake client 也可能没有 images）。
        docker = importlib.import_module("docker")
        images = getattr(self._client, "images", None)
        not_found = getattr(getattr(docker, "errors", None), "ImageNotFound", ())
        if images is not None:
            try:
                images.get(self._image)
            except not_found:
                images.pull(self._image)
            except Exception as error:  # noqa: BLE001 — 交给 create 报真错
                logger.debug("读取镜像 %s 失败：%s", self._image, error)
        # 顺序是硬约束：`containers.create` 会**先用镜像里的 /workspace 填充空卷**
        # （属主 root），所以必须先建容器、再做属主迁移、最后 start。反过来
        # （先迁移后 run）会让 run 的卷填充把刚 chown 好的目录重新写回 root。
        container = self._client.containers.create(
            self._image,
            ["sleep", "infinity"],
            name=self._container_name,
            working_dir=str(self.workspace_root),
            user=user,
            cap_drop=HARDENED_CAP_DROP,
            security_opt=HARDENED_SECURITY_OPT,
            # init=True 不在票面的降权档里，也不参与 `_is_hardened` 判定：它只是
            # 给容器插一个 PID 1 收尸进程，避免 `sleep infinity` 下的僵尸堆积。
            # 对隔离边界没有影响，故保留并在此写明用途（复核 P4-1）。
            init=True,
            # 非 root 的 HOME 默认 "/" 不可写（pip 用户安装会 EACCES）；
            # 放 /tmp 让工具链有可写家目录，不额外限制资源或网络（AC3）。
            environment={"HOME": "/tmp"},
            volumes={
                self._volume_name: {
                    "bind": str(self.workspace_root),
                    "mode": "rw",
                }
            },
        )
        try:
            self._migrate_volume_ownership(user)
            try:
                container.start()
            except Exception as error:
                # 镜像声明了容器里不存在的用户时 start 才报错（create 不解析
                # passwd）。统一成 SandboxHardeningError，别让调用方收到裸
                # APIError 500 还得自己判断半成品怎么收。
                raise SandboxHardeningError(
                    f"无法以用户 {user} 启动容器 {self._container_name}：{error}"
                ) from error
        except BaseException:
            # 迁移/启动失败不留半成品容器（下次按名字找回时会被当成"历史容器"再判一次，
            # 但那会把失败原因埋在二次重建里）。显式移除，让错误就是错误。
            with suppress(Exception):
                container.remove(force=True)
            raise
        self._container = container
        # put_archive 由 daemon 以 root 解包；缓存数字属主，写文件时显式带上
        # （否则非 root 容器用户改不了 harness 自己写进去的文件）。
        self._owner_uid_gid = self._parse_owner({"Config": {"User": user}})

    def _target_uid_gid(self, user: str) -> tuple[str, str]:
        uid, _, gid = user.partition(":")
        return uid, (gid or uid)

    def _parse_owner(self, attrs: dict) -> tuple[int, int] | None:
        declared = str((attrs.get("Config") or {}).get("User") or "")
        uid, gid = self._target_uid_gid(declared)
        if uid.isdecimal() and gid.isdecimal():
            return int(uid), int(gid)
        return None

    def _container_owner(self) -> tuple[int, int]:
        """当前容器用户的数字 uid/gid（put_archive 显式属主用）。

        容器用户一定已经被归一成数字（`_resolve_container_user`），这里拿不到
        就是内部状态坏了——响亮失败，绝不退回镜像默认行为：那会把 harness 自己
        写的文件落成 root 属主，非 root 容器用户随后改不了。
        """
        owner = getattr(self, "_owner_uid_gid", None)
        if owner is None:
            raise SandboxHardeningError(
                f"容器 {self._container_name} 的用户属主未解析为数字 uid/gid，"
                "拒绝写入属主不确定的文件。"
            )
        return owner

    def _remove_helper_containers(self) -> None:
        """清掉本卷残留的迁移 helper（失败分支用；按 label 认领，不靠名字猜）。"""
        with suppress(Exception):
            for helper in self._client.containers.list(
                all=True, filters={"label": f"{_HELPER_LABEL}={self._volume_name}"}
            ):
                with suppress(Exception):
                    helper.remove(force=True)

    def _migrate_volume_ownership(self, user: str) -> None:
        """把具名卷里的历史文件属主迁到容器用户；失败响亮、可重试、不毁数据。

        - 对**每一个**卷都做，新建的空卷也做：`containers.create` 会先用镜像内容
          填充空卷（属主 root），非 root 容器仍需要这一次迁移才可写；空卷迁移是
          零风险的记录级操作。
        - 迁移在一次性 helper 容器里跑（uid 0 + 最小能力集 + `--network none`），
          不进主容器的降权边界。
        - 备份 tar 落在独立 `<volume>-migration-backup` 卷，迁移成功即删；
          失败时保留备份并在 helper 里回滚到备份（AC2 的恢复路径）。
        """
        uid, gid = self._target_uid_gid(user)
        backup_volume = f"{self._volume_name}{_BACKUP_SUFFIX}"
        docker = importlib.import_module("docker")
        try:
            self._client.volumes.get(backup_volume)
        except docker.errors.NotFound:
            self._client.volumes.create(backup_volume)
        # `containers.run(detach=True)` 是 create + start 两步：start 失败时它抛错
        # 且**不返回**容器对象，`finally: remove` 拿不到东西 → 孤儿容器仍挂着工作
        # 卷，随后 `delete()` 的卷删除报 409。用 label 认领，失败分支按 label 清掉。
        helper_labels = {_HELPER_LABEL: self._volume_name}
        try:
            container = self._client.containers.run(
                self._image,
                [
                    "bash", "-c", _OWNER_MIGRATION_SCRIPT, "--",
                    uid, gid, _BACKUP_MOUNT,
                ],
                # 默认镜像没有 bash 时（极小自定义镜像）响亮失败，不静默跳过硬化。
                entrypoint="",
                user="0:0",
                cap_drop=HARDENED_CAP_DROP,
                cap_add=_OWNERSHIP_HELPER_CAPS,
                security_opt=HARDENED_SECURITY_OPT,
                network_mode="none",
                working_dir=str(self.workspace_root),
                labels=helper_labels,
                volumes={
                    self._volume_name: {
                        "bind": str(self.workspace_root),
                        "mode": "rw",
                    },
                    backup_volume: {"bind": _BACKUP_MOUNT, "mode": "rw"},
                },
                detach=True,
            )
        except Exception as error:
            self._remove_helper_containers()
            raise SandboxHardeningError(
                f"无法启动卷属主迁移容器（{self._volume_name}）：{error}"
            ) from error
        try:
            # 无上界等待会让会话启动在 daemon 卡住 / 超大卷时永久挂住（复核
            # P3-2）。超时按迁移失败处理：先请 helper 优雅停下（SIGTERM 让脚本
            # 有机会走自己的回滚 / 至少不在 chown 半途被 SIGKILL），再把它的
            # MIGRATE_* 日志带进异常——只看「>900s」运维判断不出停在哪一步。
            status = container.wait(timeout=_MIGRATION_WAIT_SECONDS)
            logs = container.logs(stdout=True, stderr=True).decode("utf-8", "replace")
        except Exception as error:
            with suppress(Exception):
                container.stop(timeout=_MIGRATION_STOP_GRACE_SECONDS)
            detail = ""
            with suppress(Exception):
                logs = container.logs(stdout=True, stderr=True).decode(
                    "utf-8", "replace"
                )
                detail = " | ".join(
                    line for line in logs.splitlines() if line.startswith("MIGRATE")
                )
            raise SandboxHardeningError(
                f"卷 {self._volume_name} 属主迁移等待失败"
                f"（>{_MIGRATION_WAIT_SECONDS}s 或 daemon 异常）：{error}"
                + (f" | helper 日志：{detail}" if detail else "")
            ) from error
        finally:
            with suppress(Exception):
                container.remove(force=True)
        exit_code = status.get("StatusCode")
        if exit_code != 0 or "MIGRATE_OK" not in logs:
            detail = " | ".join(
                line for line in logs.splitlines() if line.startswith("MIGRATE")
            )
            raise SandboxHardeningError(
                f"卷 {self._volume_name} 属主迁移失败（exit={exit_code}）："
                f"{detail or logs.strip()[-500:]}"
            )
        # 迁移成功：备份卷整个删掉（里面只剩备份 tar，没有别的用途）。
        try:
            self._client.volumes.get(backup_volume).remove(force=True)
        except Exception:
            logger.debug("迁移备份卷清理失败：%s", backup_volume, exc_info=True)

    def code_worker(self):
        """Create an unstarted no-egress worker without the workspace volume."""
        from agent_harness.sandbox.code_worker import DockerCodeWorker

        return DockerCodeWorker(self._client, self._image, self._container_name)

    def workspace_volume_exists(self) -> bool:
        """具名私有卷是否仍存在（#1132）。

        恢复路径据此 fail-closed：`containers.run(volumes=...)` 在具名卷缺失时
        会**静默重建一个空卷**，于是恢复会"成功"在一个空 workspace 上，而原文件
        已经不可达。调用方必须在**同一临界区**内完成"检查 → ensure_started"，
        否则检查与启动之间仍可被删除插入（消融实测：无锁时该窗口复现原症状）。
        """
        docker = importlib.import_module("docker")
        try:
            self._client.volumes.get(self._volume_name)
        except docker.errors.NotFound:
            return False
        return True

    def exec(self, command: str, *, timeout: float | None = None,
             deadline: float | None = None,
             cancel_event=None, on_output=None) -> ExecResult:
        """在容器内执行命令，超时/取消终止对应 exec 的进程组。

        deadline（ADR-0039）为 ToolExecutor 给的绝对边界：给了就用它，收不到
        deadline 才退回 timeout 相对预算；容器启动（ensure_started）吃掉预算
        后不得再 exec_create——过期后产生新副作用就是 bug。
        """
        started = perf_counter()
        effective_timeout = timeout if timeout is not None else DEFAULT_EXEC_TIMEOUT
        effective_deadline = (
            deadline if deadline is not None else started + effective_timeout
        )
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return self._interrupted_result(started, effective_timeout, cancelled=True)
            remaining = effective_deadline - perf_counter()
            if remaining <= 0:
                return self._interrupted_result(started, effective_timeout, cancelled=False)
            if self._exec_lock.acquire(timeout=min(0.05, remaining)):
                break
        try:
            self._drain_pending_cleanup()
            self._assert_exec_cleanup_healthy()
            remaining = effective_deadline - perf_counter()
            if remaining <= 0:
                return self._interrupted_result(started, effective_timeout, cancelled=False)
            self.ensure_started()
            if cancel_event is not None and cancel_event.is_set():
                return self._interrupted_result(
                    started, effective_timeout, cancelled=True,
                )
            if perf_counter() >= effective_deadline:
                return self._interrupted_result(
                    started, effective_timeout, cancelled=False,
                )
            return self._exec_locked(
                command,
                effective_timeout=effective_timeout,
                started=started,
                deadline=effective_deadline,
                cancel_event=cancel_event,
                on_output=on_output,
            )
        finally:
            self._exec_lock.release()

    @staticmethod
    def _interrupted_result(
        started: float,
        effective_timeout: float,
        *,
        cancelled: bool,
        stdout: str = "",
        stderr: str = "",
    ) -> ExecResult:
        detail = "\n命令被取消，进程树已终止" if cancelled else (
            f"\n命令超时（上限 {effective_timeout} 秒）"
        )
        return ExecResult(
            exit_code=-1,
            stdout=stdout,
            stderr=f"{stderr}{detail}",
            duration_ms=round((perf_counter() - started) * 1000, 1),
            cancelled=cancelled,
            timed_out=not cancelled,
        )

    def _drain_pending_cleanup(self) -> None:
        state = getattr(self, "_exec_state", None)
        if state is None:
            return
        # 「取走待清理项」必须与 `_queue_late_cleanup` 的写入在 `late_cleanup_lock` 下互斥，
        # **并且要在那个可能耗时 0.5s+5s 的外部清理之前完成**：否则期间并入的新项会被
        # 旧写法的 `cleanup_pending = False` 一起清掉，那个 exec 就再没有驱动去回收它。
        with state.late_cleanup_lock:
            if not state.cleanup_pending:
                return
            pending = state.late_cleanup
            if pending is None:
                raise RuntimeError("DockerSandbox is awaiting late exec cleanup")
            state.late_cleanup = None
            state.cleanup_pending = False
        self._cleanup_late_exec_create(*pending)
        self._assert_exec_cleanup_healthy()

    def _assert_exec_cleanup_healthy(self) -> None:
        state = getattr(self, "_exec_state", None)
        if getattr(self, "_exec_cleanup_failed", False) or (
            state is not None and state.cleanup_failed
        ):
            raise RuntimeError(
                "DockerSandbox cannot execute after a previous cleanup was unconfirmed"
            )

    def _mark_exec_cleanup_failed(self) -> None:
        self._exec_cleanup_failed = True
        state = getattr(self, "_exec_state", None)
        if state is not None:
            state.cleanup_failed = True
            container_name = getattr(self, "_container_name", None)
            if container_name is not None:
                cls = type(self)
                with cls._container_exec_states_lock:
                    cls._poisoned_container_exec_states[container_name] = state

    def _exec_locked(
        self,
        command: str,
        *,
        effective_timeout: float,
        started: float,
        deadline: float,
        cancel_event,
        on_output,
    ) -> ExecResult:
        api = self._client.api
        if not hasattr(api, "exec_create"):
            raise RuntimeError("DockerSandbox 需要支持 low-level exec API 的 Docker SDK")

        wrapped_command = (
            "printf '\\036AH_PID:%s\\037\\n' \"$$\" >&2; "
            "trap 'exit 143' TERM INT HUP; "
            f"/bin/sh -lc {shlex.quote(command)}; status=$?; exit $status"
        )
        if cancel_event is not None and cancel_event.is_set():
            return self._interrupted_result(started, effective_timeout, cancelled=True)
        complete, created = self._bounded_call(
            lambda: api.exec_create(
                self._container.id,
                ["setsid", "-w", "/bin/sh", "-lc", wrapped_command],
                workdir=str(self.workspace_root),
            ),
            max(0.0, deadline - perf_counter()),
            cancel_event=cancel_event,
            on_late_result=lambda late: self._queue_late_cleanup(api, late),
        )
        if not complete:
            return self._interrupted_result(
                started,
                effective_timeout,
                cancelled=bool(cancel_event is not None and cancel_event.is_set()),
            )
        if not isinstance(created, dict) or not created.get("Id"):
            raise RuntimeError("Docker exec_create 返回了无效的 exec ID")
        if cancel_event is not None and cancel_event.is_set():
            self._queue_late_cleanup(api, created)
            return self._interrupted_result(started, effective_timeout, cancelled=True)
        if perf_counter() >= deadline:
            self._queue_late_cleanup(api, created)
            return self._interrupted_result(started, effective_timeout, cancelled=False)
        exec_id = created["Id"]
        output_queue: queue.Queue[tuple[bytes | None, bytes | None] | None] = queue.Queue(
            maxsize=128,
        )
        stop_stream = threading.Event()
        stream_lock = threading.Lock()
        holder: dict[str, object] = {}
        control_pid: int | None = None
        control_pending = bytearray()
        control_prefix = b"\x1eAH_PID:"
        control_suffix = b"\x1f\n"

        def _put(chunk: tuple[bytes | None, bytes | None] | None) -> None:
            while not stop_stream.is_set():
                try:
                    output_queue.put(chunk, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def _run() -> None:
            try:
                stream = api.exec_start(exec_id, stream=True, demux=True)
                with stream_lock:
                    stop_requested = stop_stream.is_set()
                    if not stop_requested:
                        holder["stream"] = stream
                if stop_requested:
                    response_closed, stream_closed, pending_stream_cleanup = (
                        self._close_exec_resources(stream)
                    )
                    with stream_lock:
                        holder["stream_cleanup"] = (
                            stream,
                            response_closed,
                            stream_closed,
                            pending_stream_cleanup,
                        )
                        holder["stream"] = stream
                    if pending_stream_cleanup is None and (
                        not response_closed or not stream_closed
                    ):
                        self._mark_exec_cleanup_failed()
                        logger.error("Late Docker exec stream could not be closed")
                    return
                for chunk in stream:
                    _put(chunk)
            except Exception as error:  # noqa: BLE001
                holder["error"] = error
            finally:
                _put(None)

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        stdout_capture = _CappedCapture(2_000_000)
        stderr_capture = _CappedCapture(2_000_000)
        stdout_decoder = StreamDecoder("utf-8")
        stderr_decoder = StreamDecoder("utf-8")

        def consume(chunk: tuple[bytes | None, bytes | None]) -> None:
            nonlocal control_pid
            stdout_payload, stderr_payload = chunk
            if stderr_payload and control_pid is None:
                control_pending.extend(stderr_payload)
                start = control_pending.find(control_prefix)
                if start < 0:
                    keep = 0
                    for size in range(1, len(control_prefix)):
                        if control_pending.endswith(control_prefix[:size]):
                            keep = size
                    if keep:
                        stderr_payload = bytes(control_pending[:-keep])
                        del control_pending[:-keep]
                    else:
                        stderr_payload = bytes(control_pending)
                        control_pending.clear()
                else:
                    end = control_pending.find(
                        control_suffix, start + len(control_prefix),
                    )
                    if end < 0:
                        stderr_payload = bytes(control_pending[:start])
                        del control_pending[:start]
                    else:
                        candidate = bytes(control_pending[start + len(control_prefix):end])
                        try:
                            decoded_pid = candidate.decode("ascii")
                        except UnicodeDecodeError:
                            decoded_pid = ""
                        if decoded_pid.isdecimal():
                            control_pid = int(decoded_pid)
                            stderr_payload = bytes(
                                control_pending[:start]
                                + control_pending[end + len(control_suffix):]
                            )
                            control_pending.clear()
                        else:
                            stderr_payload = bytes(control_pending)
                            control_pending.clear()
                chunk = (stdout_payload, stderr_payload)
            for channel, payload, decoder, capture in (
                ("stdout", chunk[0], stdout_decoder, stdout_capture),
                ("stderr", chunk[1], stderr_decoder, stderr_capture),
            ):
                if not payload:
                    continue
                text = decoder.feed(payload)
                capture.append(text)
                if on_output is not None and text:
                    try:
                        on_output(channel, text)
                    except Exception:
                        logger.debug("Docker output callback failed", exc_info=True)

        cancelled = False
        timed_out = False
        finished = False
        while not finished:
            remaining = max(0.0, deadline - perf_counter())
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if remaining <= 0:
                timed_out = True
                break
            try:
                chunk = output_queue.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if chunk is None:
                finished = True
            else:
                consume(chunk)

        if cancelled or timed_out:
            startup_deadline = perf_counter() + 0.25
            while control_pid is None and perf_counter() < startup_deadline:
                try:
                    chunk = output_queue.get(timeout=0.02)
                except queue.Empty:
                    continue
                if chunk is not None:
                    consume(chunk)
            while True:
                try:
                    chunk = output_queue.get_nowait()
                except queue.Empty:
                    break
                if chunk is not None:
                    consume(chunk)
            with stream_lock:
                stop_stream.set()
            self._terminate_exec(api, exec_id, control_pid)
            stop_error: Exception | None = None
            try:
                self._stop_container()
            except Exception as error:  # noqa: BLE001
                stop_error = error
            try:
                self._close_exec_reader(holder, worker, stream_lock)
            except RuntimeError as cleanup_error:
                if stop_error is not None:
                    raise cleanup_error from stop_error
                raise
            if stop_error is not None:
                raise stop_error
            while True:
                try:
                    chunk = output_queue.get_nowait()
                except queue.Empty:
                    break
                if chunk is not None:
                    consume(chunk)
        else:
            if "error" in holder:
                self._terminate_exec(api, exec_id, control_pid)
                stop_error: Exception | None = None
                try:
                    self._stop_container()
                except Exception as error:  # noqa: BLE001
                    stop_error = error
                try:
                    self._close_exec_reader(holder, worker, stream_lock)
                except RuntimeError as cleanup_error:
                    if stop_error is not None:
                        raise cleanup_error from stop_error
                    raise cleanup_error from holder["error"]
                if stop_error is not None:
                    raise stop_error from holder["error"]
                raise holder["error"]
            self._close_exec_reader(holder, worker, stream_lock)

        if control_pending:
            text = stderr_decoder.feed(bytes(control_pending))
            stderr_capture.append(text)
            if on_output is not None and text:
                try:
                    on_output("stderr", text)
                except Exception:
                    logger.debug("Docker output callback failed", exc_info=True)
            control_pending.clear()

        for decoder, capture, channel in (
            (stdout_decoder, stdout_capture, "stdout"),
            (stderr_decoder, stderr_capture, "stderr"),
        ):
            text = decoder.flush()
            capture.append(text)
            if on_output is not None and text:
                try:
                    on_output(channel, text)
                except Exception:
                    logger.debug("Docker output callback failed", exc_info=True)

        if cancelled or timed_out:
            stdout, stderr = stdout_capture.value(), stderr_capture.value()
            if stdout_capture.truncated:
                stdout += "\n[stdout 超过捕获上限 2000000 字符，已截断]"
            if stderr_capture.truncated:
                stderr += "\n[stderr 超过捕获上限 2000000 字符，已截断]"
            stderr += "\n命令被取消，进程树已终止" if cancelled else (
                f"\n命令超时（上限 {effective_timeout} 秒）"
            )
            return ExecResult(
                exit_code=-1, stdout=stdout, stderr=stderr,
                duration_ms=round((perf_counter() - started) * 1000, 1),
                cancelled=cancelled,
                timed_out=timed_out,
            )

        inspected = api.exec_inspect(exec_id)
        stdout, stderr = stdout_capture.value(), stderr_capture.value()
        if stdout_capture.truncated:
            stdout += "\n[stdout 超过捕获上限 2000000 字符，已截断]"
        if stderr_capture.truncated:
            stderr += "\n[stderr 超过捕获上限 2000000 字符，已截断]"
        return ExecResult(
            exit_code=inspected.get("ExitCode", 0), stdout=stdout, stderr=stderr,
            duration_ms=round((perf_counter() - started) * 1000, 1),
        )

    def _queue_late_cleanup(self, api, created: object) -> None:
        state = getattr(self, "_exec_state", None)
        if state is None:
            self._cleanup_late_exec_create(api, created)
            return
        with state.late_cleanup_lock:
            state.late_cleanup = (api, created)
            state.cleanup_pending = True
        state.late_cleanup_event.set()
        # 立刻在后台驱动一次清理，**不等下一次 `exec()`**：会话结束 / sandbox 被遗弃时
        # 不会再有 `exec()`，只排队就等于让容器里的迟到进程永远跑下去（#258 P2）。
        threading.Thread(target=self._drive_late_cleanup, daemon=True).start()

    def _drive_late_cleanup(self) -> None:
        """从一个游离线程里把排队的迟到清理跑掉。

        清理失败只记日志、**绝不外逃**：这个线程是 detached 的，抛出去只会变成
        `threading` 的未捕获异常输出，既没人接也掩盖了真正的失败点。
        """
        lock = getattr(self, "_exec_lock", None)
        try:
            if lock is None:
                self._drain_pending_cleanup()
            else:
                # 必须与 `exec()` 共用 `_exec_lock`：它才是「同一时刻只有一个 exec 在跑」的
                # 那把锁；调用方若正持锁（create 之后才被 cancel/deadline 命中的那条路径），
                # 本线程会等它 release 后再跑。
                with lock:
                    self._drain_pending_cleanup()
        except Exception:
            logger.exception("DockerSandbox 后台驱动迟到的 exec 清理失败")

    def _cleanup_late_exec_create(self, api, created: object) -> None:
        """Reap an exec created after its caller already timed out/cancelled."""
        if not isinstance(created, dict) or not created.get("Id"):
            return
        if self._container is None:
            # 容器已被 `stop()` / `delete()` 拆掉：没有可回收的宿主进程，也**不能**因此把
            # `_container_name` 记进进程级 `_poisoned_container_exec_states`——那是永久性的，
            # 会让此后所有同名容器都 exec 不了。
            return
        try:
            # A late exec has no PID marker yet; fail closed by stopping the
            # owning container rather than leaving an untracked process behind.
            self._stop_container()
        except Exception:
            self._mark_exec_cleanup_failed()
            logger.exception("迟到的 Docker exec 创建结果无法回收")

    @staticmethod
    def _best_effort_bounded_call(call, timeout: float) -> tuple[bool, object | None]:
        try:
            return DockerSandbox._bounded_call(call, timeout)
        except Exception as error:  # noqa: BLE001
            logger.debug("Docker cleanup call failed: %s", type(error).__name__)
            return False, None

    @staticmethod
    def _close_exec_resources(
        stream: object | None,
        close_state: Future[tuple[bool, bool]] | None = None,
    ) -> tuple[bool, bool, Future[tuple[bool, bool]] | None]:
        if close_state is not None:
            if not close_state.done():
                return False, False, close_state
            response_closed, stream_closed = close_state.result()
            return response_closed, stream_closed, None
        if stream is None:
            return True, True, None
        # docker-py 7.x keeps the HTTPResponse in CancellableStream._response;
        # stream.close() only shuts down its socket. Run both closes in order on
        # one bounded worker so a timeout cannot make them race.
        response = getattr(stream, "_response", None)
        response_close = getattr(response, "close", None) if response is not None else None
        if response is not None and response_close is None:
            return False, False, None
        stream_close = getattr(stream, "close", None)
        status = {
            "response_closed": response is None,
            "stream_closed": stream_close is None,
        }
        close_result: Future[tuple[bool, bool]] = Future()

        def close_resources() -> None:
            try:
                try:
                    if response_close is not None:
                        response_close()
                    status["response_closed"] = True
                except Exception as error:  # noqa: BLE001
                    logger.debug(
                        "Docker exec HTTP response close failed: %s",
                        type(error).__name__,
                    )
                try:
                    if stream_close is not None:
                        stream_close()
                    status["stream_closed"] = True
                except Exception as error:  # noqa: BLE001
                    logger.debug(
                        "Docker exec output stream close failed: %s",
                        type(error).__name__,
                    )
            finally:
                close_result.set_result(
                    (status["response_closed"], status["stream_closed"]),
                )

        DockerSandbox._best_effort_bounded_call(close_resources, 0.4)
        if close_result.done():
            response_closed, stream_closed = close_result.result()
            return response_closed, stream_closed, None
        return False, False, close_result

    def _close_exec_reader(
        self,
        holder: dict[str, object],
        worker: threading.Thread,
        stream_lock,
    ) -> None:
        closed_stream = None
        response_closed = True
        stream_closed = True
        pending_stream_cleanup: tuple[object, Future[tuple[bool, bool]]] | None = None
        for _ in range(3):
            if pending_stream_cleanup is not None:
                stream, close_state = pending_stream_cleanup
                try:
                    close_state.result(timeout=1.0)
                except FutureTimeoutError:
                    pass
                except Exception:  # noqa: BLE001
                    response_closed = False
                    stream_closed = False
                    closed_stream = stream
                    pending_stream_cleanup = None
                else:
                    response_closed, stream_closed, next_pending = (
                        self._close_exec_resources(stream, close_state)
                    )
                    pending_stream_cleanup = (
                        (stream, next_pending) if next_pending is not None else None
                    )
                    if pending_stream_cleanup is None:
                        closed_stream = stream
            else:
                worker.join(1.0)
                with stream_lock:
                    stream = holder.get("stream")
                    late_cleanup = holder.get("stream_cleanup")
                if stream is not None and stream is not closed_stream:
                    if (
                        isinstance(late_cleanup, tuple)
                        and len(late_cleanup) == 4
                        and late_cleanup[0] is stream
                        and isinstance(late_cleanup[1], bool)
                        and isinstance(late_cleanup[2], bool)
                        and (
                            late_cleanup[3] is None
                            or isinstance(late_cleanup[3], Future)
                        )
                    ):
                        response_closed = late_cleanup[1]
                        stream_closed = late_cleanup[2]
                        close_state = late_cleanup[3]
                        if close_state is not None:
                            pending_stream_cleanup = (stream, close_state)
                        else:
                            closed_stream = stream
                    else:
                        response_closed, stream_closed, close_state = (
                            self._close_exec_resources(stream)
                        )
                        if close_state is not None:
                            pending_stream_cleanup = (stream, close_state)
                        else:
                            closed_stream = stream
            if not worker.is_alive() and pending_stream_cleanup is None:
                break

        failures = []
        if not response_closed:
            failures.append("HTTP response did not close")
        if not stream_closed:
            failures.append("output stream did not close")
        if worker.is_alive():
            failures.append("output stream worker did not stop")
        if failures:
            self._mark_exec_cleanup_failed()
            raise RuntimeError(
                "Docker exec cleanup could not be confirmed: " + "; ".join(failures)
            )

    def _stop_container(self) -> None:
        kill_error: Exception | None = None
        try:
            complete, _ = self._bounded_call(self._container.kill, 0.5)
            if not complete:
                raise TimeoutError("Docker container kill timed out")
        except Exception as error:  # noqa: BLE001
            kill_error = error

        try:
            complete, _ = self._bounded_call(self._container.wait, 5.0)
            if not complete:
                raise TimeoutError("Docker container stop wait timed out")
        except Exception as error:
            self._mark_exec_cleanup_failed()
            logger.debug(
                "Docker container stop could not be confirmed (kill error: %s)",
                type(kill_error).__name__ if kill_error is not None else "none",
            )
            raise RuntimeError(
                "Docker exec cleanup could not be confirmed: container did not stop"
            ) from error

    @staticmethod
    def _bounded_call(
        call,
        timeout: float,
        *,
        cancel_event=None,
        on_late_result=None,
    ) -> tuple[bool, object | None]:
        holder: dict[str, object] = {}
        expired = threading.Event()

        def run() -> None:
            try:
                result = call()
                holder["result"] = result
                if expired.is_set() and on_late_result is not None:
                    on_late_result(result)
            except Exception as error:  # noqa: BLE001
                holder["error"] = error

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = perf_counter() + timeout
        while worker.is_alive():
            if cancel_event is not None and cancel_event.is_set():
                expired.set()
                return False, None
            remaining = deadline - perf_counter()
            if remaining <= 0:
                expired.set()
                return False, None
            worker.join(min(0.05, remaining))
        if "error" in holder:
            raise holder["error"]
        return True, holder.get("result")

    def _terminate_exec(self, api, exec_id: str, pid: int | None) -> None:
        if pid is None or pid <= 1:
            logger.warning("Docker exec %s did not publish a kill PID", exec_id)
            return
        workdir = str(self.workspace_root)

        def kill(signal_name: str):
            command = (
                "signal=$2; "
                "kill_tree() { "
                "for child in $(cat /proc/$1/task/$1/children 2>/dev/null); do "
                "kill_tree $child; "
                "done; "
                "kill -$signal -- -$1 2>/dev/null; "
                "}; "
                "kill_tree $1"
            )
            return self._container.exec_run(
                ["/bin/sh", "-lc", command, "--", str(pid), signal_name],
                workdir=workdir,
            )

        self._best_effort_bounded_call(lambda: kill("TERM"), 0.2)
        sleep(0.2)
        self._best_effort_bounded_call(lambda: kill("KILL"), 0.2)

    def list_files(self, pattern: str) -> list[str]:
        """枚举容器 workspace 内匹配 glob 模式的文件，返回相对 /workspace 路径（排序）。

        用 exec("find . -type f -printf '%P\n'") 拿文件列表后 Python 侧 fnmatch 过滤。
        """
        effective = pattern if pattern else "*"
        result = self.exec("find . -type f -printf '%P\n'")
        if result.exit_code != 0:
            return []
        candidates = [line for line in result.stdout.splitlines() if line.strip()]
        matched = [
            rel
            for rel in candidates
            if _glob_match_posix(rel, effective)
        ]
        matched.sort()
        return matched

    def path_kind(self, path: str) -> str | None:
        """容器内路径的类型探测（#1167）。

        基类 `Sandbox.path_kind` 要求 `resolve_within_workspace` 返回宿主 `Path`，
        Docker 后端返回容器内 `PurePosixPath` ⇒ 基类实现恒返回 `None`
        （项目指令加载/重载在 Docker 会话上恒为 `missing`、
        WriteTool 的目录甄别失效）。

        实现用容器内 `/bin/sh` 的 `test` 内建做廉价探测——**不**用 `get_archive`：
        后者对目录会返回整棵子树的递归归档并全量驻留内存（P2），而这里只需要
        类型。`test -L` 优先于 `-d`/`-f`（不跟随末级符号链接，与宿主侧
        "符号链接指令文件不跟随"一致）；`-e` 兜底特殊文件（FIFO/socket/device，
        报不可读 kind 而非"缺失"，与宿主侧对齐）。

        #1510：`resolve_within_workspace` 现按容器视角解析 symlink 实际目标
        判界，本探测因此改走**词法**围栏 `_lexical_within_workspace`——
        `test -L` 要看到链接本身，不能拿解析后的目标路径来问类型。
        **中间目录**链接另过容器围栏（Standards 轴 S1）：父目录 realpath
        解析后判界、末级 basename 保持词法——否则 `path_kind("dir-link/x")`
        会跟随中间链接报出 workspace 外目标的类型/存在性（宿主后端对越界
        路径恒 None，两侧对齐）。父目录解析出界 → 本探测也拒绝（None）。
        """
        try:
            target = self._lexical_within_workspace(path)
            root = self.workspace_root
            if target != root:
                parent = self._resolve_container_symlinks(target.parent, original=path)
                probe = parent / target.name
            else:
                probe = target
        except OSError:
            return None
        self.ensure_started()
        quoted = shlex.quote(str(probe))
        result = self._container.exec_run(
            [
                "/bin/sh",
                "-c",
                (
                    f"if test -L {quoted}; then echo symlink;"
                    f" elif test -d {quoted}; then echo directory;"
                    f" elif test -f {quoted}; then echo file;"
                    f" elif test -e {quoted}; then echo special;"
                    " else echo missing; fi"
                ),
            ],
        )
        if result.exit_code != 0:
            return None
        lines = result.output.decode("utf-8", errors="ignore").strip().splitlines()
        return {
            "symlink": "symlink",
            "directory": "directory",
            "file": "file",
            "special": "special",
        }.get(lines[-1] if lines else "")

    def read_text(self, path: str) -> str:
        # #1510：target 是容器视角解析后的真实路径——内部链接读穿到目标，
        # 出界目标在围栏处已拒绝；get_archive 不再经用户路径二次解析。
        target = self.resolve_within_workspace(path)
        self.ensure_started()
        docker = importlib.import_module("docker")
        try:
            stream, _ = self._container.get_archive(str(target))
        except docker.errors.NotFound as exc:
            # Docker 的 get_archive 对不存在的文件抛 NotFound（OSError 子类，
            # 不是 FileNotFoundError）。统一映射成 FileNotFoundError，
            # 让调用方（如 WriteTool 读 before）能按预期捕获。
            raise FileNotFoundError(path) from exc
        except docker.errors.APIError as exc:
            # #1510（Correctness 轴 F2）：daemon 侧拒绝（实测链接环 =
            # `evalSymlinksInScope: too many links` 500）。APIError 经
            # requests 继承 OSError，但**精确类型**不在 web 的
            # `_WORKSPACE_ERROR_STATUS` 表里（KeyError → 未分类 500）；
            # 映射成裸 OSError 落既有 422"路径不合法"档，fail-closed 不变。
            raise OSError(f"容器读取 '{path}' 失败：{exc}") from exc
        with tarfile.open(fileobj=io.BytesIO(b"".join(stream)), mode="r:") as archive:
            member = next((item for item in archive.getmembers() if item.isfile()), None)
            if member is None:
                raise FileNotFoundError(path)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise FileNotFoundError(path)
            return extracted.read().decode("utf-8")

    def write_text(self, path: str, content: str) -> None:
        # #1510：target 是容器视角解析后的真实路径；mkdir / put_archive / cat
        # 全部落在真实路径上，链接（含中间目录链接）不再被写路径跟随出界。
        target = self.resolve_within_workspace(path)
        self.ensure_started()
        mkdir_result = self._container.exec_run(["mkdir", "-p", str(target.parent)])
        if mkdir_result.exit_code != 0:
            raise RuntimeError(f"无法创建 Workspace 目录 '{target.parent}'")

        payload = content.encode("utf-8")
        # 容器内就地覆盖：先 put_archive 落一个临时文件，再 `cat 临时文件 > 目标`
        # 截断写已有 inode。直接 put_archive 到目标会 unlink 旧 inode——对**完全
        # 位于 workspace 内**的 hard-link pair 会拆散链接，与宿主目录/POSIX 语义
        # 不一致（#1048 AC4/AC8：内部 pair 保持可用）。
        tmp_name = f".harness-write-{uuid4().hex}.tmp"
        owner = self._container_owner()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            member = tarfile.TarInfo(name=tmp_name)
            member.size = len(payload)
            member.mode = 0o644
            # #1486：daemon 以 root 解包；不显式写属主会把文件落成 root，
            # 非 root 容器用户随后就改不了自己刚写的文件。
            _apply_owner(member, owner)
            archive.addfile(member, io.BytesIO(payload))
        if not self._container.put_archive(str(target.parent), buffer.getvalue()):
            raise RuntimeError(f"无法写入 Workspace 文件 '{target}'")
        command = (
            f"cat {shlex.quote(tmp_name)} > {shlex.quote(target.name)} "
            f"&& rm -f {shlex.quote(tmp_name)}"
        )
        result = self._container.exec_run(
            ["/bin/sh", "-c", command], workdir=str(target.parent)
        )
        if result.exit_code != 0:
            self._container.exec_run(
                ["/bin/sh", "-c", f"rm -f {shlex.quote(tmp_name)}"],
                workdir=str(target.parent),
            )
            raise RuntimeError(f"无法写入 Workspace 文件 '{target}'")

    def copy_in(self, host_path: Path, workspace_path: str) -> None:
        # #1510：target 是容器视角解析后的真实路径，put_archive 落在真实
        # 父目录上，不跟随用户路径里的（中间目录）链接出界。
        target = self.resolve_within_workspace(workspace_path)
        source = Path(host_path)
        if not source.exists():
            raise FileNotFoundError(source)

        self.ensure_started()
        mkdir_result = self._container.exec_run(["mkdir", "-p", str(target.parent)])
        if mkdir_result.exit_code != 0:
            raise RuntimeError(f"无法创建 Workspace 目录 '{target.parent}'")

        owner = self._container_owner()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", dereference=False) as archive:
            if os.name == "nt":
                import msvcrt
                from types import SimpleNamespace

                from agent_harness.sandbox.windows_psec import (
                    _pin_host_path_tree_for_archive,
                )

                with _pin_host_path_tree_for_archive(source) as (entries, transferred):
                    for relative_path, handle, is_directory in entries:
                        member_name = (
                            f"{target.name}/{relative_path}"
                            if relative_path
                            else target.name
                        )
                        fd = msvcrt.open_osfhandle(
                            handle, os.O_RDONLY | os.O_BINARY,
                        )
                        transferred.add(handle)
                        try:
                            if is_directory:
                                fileobj = SimpleNamespace(
                                    name="", fileno=lambda fd=fd: fd,
                                )
                                member = archive.gettarinfo(
                                    arcname=member_name, fileobj=fileobj,
                                )
                                if member is None or not member.isdir():
                                    raise PermissionError(
                                        "copy_in source changed during archive"
                                    )
                                _apply_owner(member, owner)
                                archive.addfile(member)
                            else:
                                with os.fdopen(fd, "rb") as stream:
                                    fd = None
                                    fileobj = SimpleNamespace(
                                        name="", fileno=stream.fileno,
                                    )
                                    member = archive.gettarinfo(
                                        arcname=member_name, fileobj=fileobj,
                                    )
                                    if member is None or not (
                                        member.isreg() or member.islnk()
                                    ):
                                        raise PermissionError(
                                            "copy_in source changed during archive"
                                        )
                                    _apply_owner(member, owner)
                                    archive.addfile(member, stream)
                        finally:
                            if fd is not None:
                                os.close(fd)
            else:
                # #1301：fd 相对归档，不按路径二次遍历（换链 TOCTOU 修补）。
                _archive_posix_tree_fd_relative(
                    archive, source, target.name, owner,
                )
        if not self._container.put_archive(str(target.parent), buffer.getvalue()):
            raise RuntimeError(f"无法导入 Host 路径 '{source}'")

    def export_to_host(self, dest: Path) -> None:
        """把容器工作区**副本**导出到全新宿主目录（#1048 AC5/AC6/AC7）。

        用 `get_archive` 取 `/workspace` 的 tar 快照并安全解包（`filter="data"`：
        拒绝绝对路径 / `..` 穿越 / 越界链接）。Docker `get_archive` 归档的顶层是
        被取路径的 basename（`workspace/`），而导出语义是「工作区**内容**副本」，
        故剥掉该顶层目录、把内容直接落在 `dest` 根——否则导出目标会平白多出一层
        `workspace/`，docker fork 的子 volume 也会被多套一层目录（子会话按
        `/workspace/<file>` 读不到父文件）。导出的是**副本**：容器内对象与宿主目标
        各自独立，不共享 inode/canary；绝不把宿主 `/workspace` 当直连路径。允许把
        新成员写入既有目标目录，但遇到同名目标项时拒绝覆盖。
        """
        out = Path(dest)
        destination_exists = out.exists()
        if destination_exists and not out.is_dir():
            raise NotADirectoryError(out)
        created_destination = False
        created_targets: list[Path] = []
        try:
            self.ensure_started()
            stream, _ = self._container.get_archive(str(self.workspace_root))
            payload = b"".join(stream)
            with tempfile.TemporaryDirectory() as raw_dir:
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
                    members = archive.getmembers()
                    if os.name == "nt":
                        _validate_windows_archive_members(members)
                    archive.extractall(path=raw_dir, members=members, filter=_export_data_filter)
                raw = Path(raw_dir)
                extracted = list(raw.iterdir())
                source = (
                    extracted[0]
                    if len(extracted) == 1 and extracted[0].is_dir()
                    else raw
                )
                if not destination_exists:
                    out.mkdir(parents=True)
                    created_destination = True
                for item in source.iterdir():
                    target = out / item.name
                    if target.exists() or target.is_symlink():
                        raise FileExistsError(
                            f"导出目标项已存在，拒绝覆盖：{target}"
                        )
                    created_targets.append(target)
                    if item.is_symlink():
                        shutil.copy2(item, target, follow_symlinks=False)
                    elif item.is_dir():
                        shutil.copytree(item, target, symlinks=True)
                    else:
                        shutil.copy2(item, target)
                    _remove_export_path(item)
        except BaseException:
            if created_destination:
                _remove_export_path(out)
            else:
                for target in reversed(created_targets):
                    _remove_export_path(target)
            raise

    def resolve_within_workspace(self, path: str) -> PurePosixPath:
        """Resolve a model-supplied path with container-native POSIX semantics.

        两段判界（#1510）：先词法规范化挡 `..` 穿越，再让**容器自己**解析
        symlink（`realpath -m`）并按解析后的实际目标复核边界。围栏必须与
        实际操作同语义：read 的 `get_archive` 跟随中间目录链接（末级链接
        返回链接条目本身），write 的 `cat >` 与 copy_in 的
        `put_archive(父目录)` 跟随全部链接——只查词法字符串时，
        `/workspace/link -> /etc/x` 会让"workspace 路径"静默穿透到容器
        文件系统（宿主侧基类用 `Path.resolve()` 跟随链接，本实现对齐）。

        `-m`（缺失尾部按词法补全）：目标不存在仍是合法 write 语义；悬空
        链接按其目标判界（与宿主 strict=False 行为一致）。`path_kind` 的
        "不跟随末级链接"探测走 `_lexical_within_workspace`（父目录仍过
        容器围栏），不受影响。

        代价与宿主的差异（如实登记）：① 本方法现在每次调用产生**一次容器
        exec**（并 ensure_started）——web pathspec / 指令路径这类"只校验"
        用法同样付出该往返，这是"判界与操作同容器语义"的必要代价；
        ② 词法阶段先折叠 `..` 再解析链接，宿主 `Path.resolve` 是先跟链接
        再消费 `..`——"链接+`..`"组合上两者结果可不同，但两段围栏
        （词法入界 + 解析入界）都闭合才放行，不构成出界。

        TOCTOU 残余（如实登记）：realpath 与后续操作之间存在替换链接的
        竞态窗口；并发攻击者本可用 bash exec 直接读写容器内任意路径
        （exec 无路径围栏），本围栏执行的是文件工具的 workspace 契约，
        不是容器逃逸边界（票面明确不声称）。缓解：所有后续操作都落在
        **解析后的真实路径**上，不再经用户路径二次解析。
        """
        resolved = self._lexical_within_workspace(path)
        return self._resolve_container_symlinks(resolved, original=path)

    def _lexical_within_workspace(self, path: str) -> PurePosixPath:
        """词法规范化 + 边界检查（不解析 symlink；`path_kind` 探测用）。"""
        root = self.workspace_root
        raw = PurePosixPath(path.replace("\\", "/"))
        candidate = raw if raw.is_absolute() else root / raw
        resolved = PurePosixPath(posixpath.normpath(str(candidate)))
        if resolved != root and root not in resolved.parents:
            raise PermissionError(
                f"路径 '{path}' 解析为 '{resolved}'，越出 workspace '{root}' 边界，拒绝访问"
            )
        return resolved

    def _resolve_container_symlinks(
        self, target: PurePosixPath, *, original: str
    ) -> PurePosixPath:
        """在容器视角解析 symlink 实际目标并复核 workspace 边界（#1510）。

        `realpath -m` 跑在容器里，与 get_archive / put_archive / `cat >`
        看到同一文件系统视图（权限检查与实际操作同语义）。解析失败
        fail-closed（裸 `OSError`——非法形态等属 POSIX errno 族，与宿主
        `Path.resolve` 抛 OSError 同分类，web 侧落既有 422"路径不合法"
        档，不产生未分类 500）；解析出界 fail-closed（PermissionError），
        不把"解析不了"降级成放行。

        已知 `-m` 局限（实测）：对**不可解析**组件（链接环）静默返回词法
        路径（exit 0）——环不会被本围栏拦截，由 daemon 侧 fail-closed
        兜住（get_archive `evalSymlinksInScope` 500 → `read_text` 映射
        OSError；`cat >` shell 失败 → RuntimeError）。

        输出只剥**一个**尾换行（F1）：文件名本身可以以空白/换行结尾，
        `.strip()`/`rstrip("\n")` 会把真实文件名字符吃掉、让操作落到
        同目录另一个文件上（前缀 `/workspace` 为 ASCII，缩短尾部无出界
        方向，仍如实修正；realpath 恰好只追加一个 `\n`，剥一个即净）。
        非 UTF-8 文件名字节经 `errors="ignore"` 丢弃的局限保留登记：
        解析结果可能指向该名字的"可显示形态"，两段围栏仍闭合。
        """
        self.ensure_started()
        result = self._container.exec_run(["realpath", "-m", "--", str(target)])
        if result.exit_code != 0:
            raise OSError(
                f"容器内解析路径 '{target}' 失败（realpath 退出 {result.exit_code}）"
            )
        decoded = result.output.decode("utf-8", errors="ignore").removesuffix("\n")
        if not decoded:
            raise OSError(f"容器内解析路径 '{target}' 失败（realpath 输出为空）")
        resolved = PurePosixPath(decoded)
        root = self.workspace_root
        if resolved != root and root not in resolved.parents:
            raise PermissionError(
                f"路径 '{original}' 经容器内 symlink 解析为 '{resolved}'，"
                f"越出 workspace '{root}' 边界，拒绝访问"
            )
        return resolved

    def stop(self) -> None:
        """停容器（保留 Volume 以便 resume）。幂等。"""
        if self._container is None:
            return
        docker = importlib.import_module("docker")
        try:
            self._container.reload()
            if self._container.status == "running":
                self._container.stop()
        except docker.errors.NotFound:
            pass
        finally:
            self._container = None

    def delete(self) -> None:
        """彻底清理：移除容器 + 删除 Volume。幂等。"""
        docker = importlib.import_module("docker")
        if self._container is not None:
            try:
                self._container.remove(force=True)
            except docker.errors.NotFound:
                pass
            finally:
                self._container = None
        else:
            # 跨进程：容器可能还在，按确定性名字查回再删。
            try:
                existing = self._client.containers.get(self._container_name)
            except docker.errors.NotFound:
                existing = None
            if existing is not None:
                existing.remove(force=True)
        try:
            self._client.volumes.get(self._volume_name).remove(force=True)
        except docker.errors.NotFound:
            pass
        # #1486：迁移备份卷随工作卷一起删（否则重开同名会话会拿旧备份）。
        with suppress(Exception):
            self._client.volumes.get(
                f"{self._volume_name}{_BACKUP_SUFFIX}"
            ).remove(force=True)
        state = getattr(self, "_exec_state", None)
        if state is not None:
            cls = type(self)
            with cls._container_exec_states_lock:
                if cls._poisoned_container_exec_states.get(self._container_name) is state:
                    cls._poisoned_container_exec_states.pop(self._container_name, None)
