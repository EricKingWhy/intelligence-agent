"""启动期单实例锁（ARCH-7 / #150）。

同一 `Settings.workspace_dir`（会话状态根：`sessions/`、`workspaces/`、
`harness.db`）同时只允许**一个进程**在跑。跨进程互斥靠 **OS 级 advisory lock**：

- POSIX：``fcntl.flock(LOCK_EX | LOCK_NB)``
- Windows：``msvcrt.locking(LK_NBLCK)``（1 字节区间锁）

**不用裸 lockfile + pid 的理由**：进程崩溃 / 被 kill 时由 OS 自动释放，不会留下
"残留锁导致永久起不来"的自锁死。裸 pidfile 必须自己处理陈旧判定，那是一条额外
的出错路径。锁文件里仍写 pid / 启动时间——**只用于诊断**，不参与占用判定
（因此锁文件内容可能是上一个持有者留下的陈旧值，错误信息里已提示核对 pid）。

**边界是 advisory**：它约束的是「遵守本协议的进程」——即本项目的 CLI 与 Web
入口；它不是文件系统级强制权限，绕过本项目直接写 `sessions/` 的第三方程序不受
约束。Windows 上区间锁虽是 mandatory，也只覆盖锁定的那 1 字节。

**有意行为**：CLI 与 Web 并发被拒绝（#150 AC5）。无保护的跨进程多写者正是本
模块要挡的危险形状——两个进程同时 append 会话 JSONL 会产出重复 seq / 交错写，
`RunManager` 的 run 归属共识也只在进程内有效。这不是 bug。
例外：不触碰该根的命令（`--help` / `-h`）不取锁。

逃生门 `ALLOW_SHARED_ROOT` **默认关闭**；显式设为真值时降级为警告放行，且日志
显著留痕（宁可吵，不要静默降级）。绕锁进程还会在 workspace root 持有独立 OS
租约；cutover 与 Memory V2 索引重建各自发布专用 fence 后扫描租约并拒绝与存量
绕锁 writer 并发。应用启动在注册租约后再次检查相应 fence，避免 fence 发布竞态。

进程内幂等**且线程安全**：同一路径重复 `acquire()` 返回同一把锁并计数，最后一个
`release()` 才真正放锁——避免"同一进程重复装配 = 自锁"。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "ALLOW_SHARED_ROOT_ENV",
    "CUTOVER_FENCE_FILENAME",
    "DEFAULT_LOCK_FILENAME",
    "MEMORY_INDEX_REBUILD_FENCE_FILENAME",
    "InstanceLock",
    "InstanceLockError",
]

logger = logging.getLogger(__name__)

#: 逃生门环境变量：设真值 → 占用时降级为警告放行并注册 OS 锁定的 writer 租约。
#: 默认安全（未设 = 取锁）。
ALLOW_SHARED_ROOT_ENV = "ALLOW_SHARED_ROOT"

#: 锁文件名（位于 workspace root 下）。
DEFAULT_LOCK_FILENAME = ".instance.lock"
CUTOVER_FENCE_FILENAME = ".memory-cutover-in-progress"
MEMORY_INDEX_REBUILD_FENCE_FILENAME = ".memory-v2-index-rebuild-in-progress"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Windows 字节区间锁的偏移。**必须远离载荷区**：Windows 的区间锁是 mandatory 的
#: （连同进程的另一个句柄读被锁区间都会被拒），若锁 byte 0，锁文件里写的 pid /
#: 诊断信息就再也读不出来——第二个进程的错误信息会退化成"未知占用者"。
#: 锁在远超载荷的偏移上（允许锁 EOF 之后），载荷区保持可读。POSIX 用 flock，
#: 整文件 advisory，无此问题。
_WIN_LOCK_OFFSET = 1 << 20
_SHARED_ROOT_LEASE_PREFIX = ".instance-shared-root-"
_SHARED_ROOT_LEASE_SUFFIX = ".lease"
_SHARED_ROOT_REGISTRY_FILENAME = ".instance-shared-root-registry.lock"

#: 进程内注册表：key = 锁文件的规范化真实路径。同进程重复装配返回同一把锁。
_lock_by_path: dict[str, InstanceLock] = {}
#: 保护上面这张表与 `_holders` 的 check-then-act：两个线程同时首装配时，不能
#: 都 miss 注册表然后各自去抢 OS 锁——那会让后到者对自己进程报"已被占用"。
_registry_lock = threading.Lock()


class InstanceLockError(RuntimeError):
    """同一 workspace root 已被另一个进程持有。"""


def _escape_hatch_enabled() -> bool:
    return os.environ.get(ALLOW_SHARED_ROOT_ENV, "").strip().lower() in _TRUTHY


def _key_for(path: Path) -> str:
    """注册表身份：realpath + normcase。

    只做 abspath 不够——Windows 上同一目录的 `C:\\WS` 与 `c:\\ws`、或 8.3 短名与
    长名，会算出两个 key → 同进程对同一文件开两个句柄去抢锁 → 对自己报占用。
    真实路径（解 symlink）才是文件身份。
    """
    return os.path.normcase(os.path.realpath(path))


def _take_os_lock(fd: int) -> None:
    """非阻塞取 OS 级排他锁；已被占用时抛 OSError。"""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(fd: int) -> None:
    """显式解锁（close 也会释放，这里是双保险）。"""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _shared_root_registry_lock(root: Path) -> Iterator[None]:
    """Serialize lease publication with scans that remove stale lease files."""
    fd = os.open(root / _SHARED_ROOT_REGISTRY_FILENAME, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        _take_os_lock(fd)
        locked = True
        yield
    finally:
        if locked:
            try:
                _release_os_lock(fd)
            except OSError:
                pass
        os.close(fd)


class InstanceLock:
    """`root` 目录的单实例锁。用法：``lock = InstanceLock(root).acquire()``。

    `allow_memory_index_rebuild=True` 仅供重建协调器使用：拿锁后必须先发布专用 fence，
    再调用 `assert_no_shared_root_writers()`。`release()` 幂等；持有者是同一进程时按引用
    计数，最后一次才真正放锁。
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        allow_cutover: bool = False,
        allow_memory_index_rebuild: bool = False,
    ) -> None:
        if allow_cutover and allow_memory_index_rebuild:
            raise ValueError(
                "cutover and memory index rebuild maintenance modes are mutually exclusive"
            )
        # 展示路径用 abspath（不 resolve）：只做规范化，不动 symlink / Windows
        # 短名——错误信息里出现的路径必须和调用方给的一致，否则用户认不出自己
        # 的目录。文件身份另用 realpath（见 `_key_for`）。
        self._root = Path(os.path.abspath(os.fspath(root)))
        self._path = self._root / DEFAULT_LOCK_FILENAME
        self._key = _key_for(self._path)
        self._allow_cutover = allow_cutover
        self._allow_memory_index_rebuild = allow_memory_index_rebuild
        self._fd: int | None = None
        self._shared_lease_path: Path | None = None
        self._shared_lease_fd: int | None = None
        self._holders = 0

    def _create_shared_root_lease(self) -> tuple[Path, int]:
        with _shared_root_registry_lock(self._root):
            path = self._root / f"{_SHARED_ROOT_LEASE_PREFIX}{uuid.uuid4().hex}{_SHARED_ROOT_LEASE_SUFFIX}"
            # 载荷是 UTF-8 文本，但显式带 `O_BINARY`：Windows 的 CRT 缺它时会把 `\n` 撑成
            # CRLF，读回（`_read_holder` 走 `read_text()`）虽能容忍，但落盘字节不再等于本进程
            # 写下的那些。与 `context/project_instructions.py` / `skills/inspection.py` 同一惯例。
            fd = os.open(
                path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            locked = False
            try:
                _take_os_lock(fd)
                locked = True
                payload = f"pid={os.getpid()}\nstarted_at={datetime.now(UTC).isoformat()}\n"
                os.write(fd, payload.encode("utf-8"))
                return path, fd
            except BaseException:
                if locked:
                    try:
                        _release_os_lock(fd)
                    except OSError:
                        pass
                os.close(fd)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

    def _release_shared_root_lease(self) -> None:
        fd, self._shared_lease_fd = self._shared_lease_fd, None
        path, self._shared_lease_path = self._shared_lease_path, None
        if fd is not None:
            try:
                _release_os_lock(fd)
            except OSError:
                logger.warning("释放 shared-root 租约失败（OS 会在进程退出时兜底）")
            finally:
                os.close(fd)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("清理 shared-root 租约文件失败：%s", path)

    def assert_no_shared_root_writers(self) -> None:
        """Refuse startup or maintenance while a bypass writer holds a lease."""
        try:
            with _shared_root_registry_lock(self._root):
                self._scan_shared_root_writers()
        except OSError as error:
            raise InstanceLockError(
                "The shared-root lease registry is busy or unavailable; refusing startup or maintenance."
            ) from error

    def _scan_shared_root_writers(self) -> None:
        pattern = f"{_SHARED_ROOT_LEASE_PREFIX}*{_SHARED_ROOT_LEASE_SUFFIX}"
        for path in sorted(self._root.glob(pattern)):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise InstanceLockError(
                    "A shared-root writer lease cannot be inspected; refusing startup or maintenance."
                ) from error
            if not path.is_file() or path.is_symlink() or info.st_nlink != 1:
                raise InstanceLockError(
                    "A shared-root writer lease is invalid; refusing startup or maintenance."
                )
            try:
                fd = os.open(path, os.O_RDWR)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise InstanceLockError(
                        "A shared-root writer lease cannot be inspected; refusing startup or maintenance."
                ) from error
            try:
                try:
                    _take_os_lock(fd)
                except OSError as error:
                    raise InstanceLockError(
                        "A shared-root workspace writer is active; stop it before retrying."
                    ) from error
            finally:
                os.close(fd)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise InstanceLockError(
                    "A stale shared-root writer lease cannot be removed; refusing startup or maintenance."
                ) from error

    def acquire(self) -> InstanceLock:
        """取锁；成功返回锁对象（同进程同路径幂等）。失败抛 `InstanceLockError`。

        目录不存在会先创建；打不开锁文件（权限 / 磁盘）时 `OSError` 原样抛出不
        包装——那是与"被占用"不同类的故障，混成一个错误会误导排查。
        """
        with _registry_lock:
            if (
                not self._allow_cutover
                and (self._root / CUTOVER_FENCE_FILENAME).exists()
            ):
                raise InstanceLockError(
                    "Memory clean-slate reset is incomplete; resume the cutover before startup."
                )
            if (
                not self._allow_memory_index_rebuild
                and os.path.lexists(self._root / MEMORY_INDEX_REBUILD_FENCE_FILENAME)
            ):
                raise InstanceLockError(
                    "Memory V2 index rebuild is incomplete; rerun the rebuild before startup."
                )
            existing = _lock_by_path.get(self._key)
            if existing is not None:
                if (
                    existing._allow_cutover != self._allow_cutover
                    or existing._allow_memory_index_rebuild
                    != self._allow_memory_index_rebuild
                ):
                    raise InstanceLockError(
                        "Workspace lock is already held for a different operation."
                    )
                existing._holders += 1
                return existing

            self._root.mkdir(parents=True, exist_ok=True)
            # 同上：`self._fd` 就是 `_write_holder` 的写目标，带 `O_BINARY` 免得 Windows 上
            # 把诊断文本的 `\n` 落成 CRLF。
            fd = os.open(
                self._path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600
            )
            try:
                _take_os_lock(fd)
            except OSError as error:
                os.close(fd)
                if self._allow_memory_index_rebuild or not _escape_hatch_enabled():
                    raise InstanceLockError(
                        self._locked_message(self._read_holder())
                    ) from error
                try:
                    self._shared_lease_path, self._shared_lease_fd = (
                        self._create_shared_root_lease()
                    )
                except OSError as lease_error:
                    raise InstanceLockError(
                        "The shared-root escape hatch could not register this writer; startup refused."
                    ) from lease_error
                if (
                    not self._allow_cutover
                    and (self._root / CUTOVER_FENCE_FILENAME).exists()
                ):
                    self._release_shared_root_lease()
                    raise InstanceLockError(
                        "Memory clean-slate reset is in progress; startup is refused."
                    )
                if (
                    not self._allow_memory_index_rebuild
                    and os.path.lexists(
                        self._root / MEMORY_INDEX_REBUILD_FENCE_FILENAME
                    )
                ):
                    self._release_shared_root_lease()
                    raise InstanceLockError(
                        "Memory V2 index rebuild is in progress; startup is refused."
                    )
                # 逃生门：显式开启才降级。消息里带字面 "WARNING" 前缀，便于
                # grep 与人工审计；OS 锁定的租约让 cutover 能发现仍运行的进程。
                logger.warning(
                    "WARNING: %s 已被其他进程占用，但 %s=%s 已显式设置 → "
                    "降级放行（跨进程并发写可能造成会话 JSONL 重复 seq / 交错写）。占用者：%s",
                    self._path,
                    ALLOW_SHARED_ROOT_ENV,
                    os.environ.get(ALLOW_SHARED_ROOT_ENV),
                    self._read_holder() or "未知",
                )
                self._fd = None  # 没拿到 OS 锁，release 时无需释放
                self._holders = 1
                _lock_by_path[self._key] = self
                return self

            if (
                not _escape_hatch_enabled()
                and not self._allow_memory_index_rebuild
            ):
                try:
                    self.assert_no_shared_root_writers()
                except BaseException:
                    try:
                        _release_os_lock(fd)
                    except OSError:
                        pass
                    finally:
                        os.close(fd)
                    raise

            self._fd = fd
            self._holders = 1
            self._write_holder()
            _lock_by_path[self._key] = self
            return self

    def release(self) -> None:
        """放锁（幂等）。引用计数归零才真正释放 OS 锁并注销。"""
        with _registry_lock:
            if self._holders == 0:
                return
            self._holders -= 1
            if self._holders > 0:
                return
            # 先解锁再注销：反过来的话，另一个线程可能在新 fd 上抢锁时旧的 OS
            # 锁还没放，于是它对自己进程报"已被占用"。
            fd, self._fd = self._fd, None
            if fd is not None:
                try:
                    _release_os_lock(fd)
                except OSError:  # 解锁失败不该掩盖调用方原本的退出路径
                    logger.warning("释放实例锁失败（OS 会在进程退出时兜底）：%s", self._path)
                finally:
                    os.close(fd)
            self._release_shared_root_lease()
            _lock_by_path.pop(self._key, None)

    def _write_holder(self) -> None:
        """把 pid / 启动时间写进锁文件——**仅供诊断**，不参与占用判定。

        截断发生在锁定的区间之外（锁在 `_WIN_LOCK_OFFSET`），不会影响持有的锁。
        """
        payload = (
            f"pid={os.getpid()}\n"
            f"started_at={datetime.now(UTC).isoformat()}\n"
            f"root={self._root}\n"
        )
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)  # type: ignore[arg-type]
            os.ftruncate(self._fd, 0)  # type: ignore[arg-type]
            os.write(self._fd, payload.encode("utf-8"))  # type: ignore[arg-type]
        except OSError:
            logger.debug("锁文件写入失败（仅诊断信息，不影响锁本身）", exc_info=True)

    def _read_holder(self) -> str | None:
        try:
            content = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return content.replace("\n", " / ") or None

    def _locked_message(self, holder: str | None) -> str:
        return (
            f"另一个进程已在写同一个 session root，启动被拒绝。\n"
            f"  锁文件：{self._path}\n"
            f"  占用者：{holder or '未知（锁文件内容不可读 / 尚未写入）'}\n"
            f"  确认方式：检查上面 pid 是否仍在运行；本锁由 OS 在进程退出 / 被杀时自动释放，\n"
            f"            所以不存在「陈旧锁」——但锁文件里的 pid 可能属于上一个持有者，\n"
            f"            以「该 pid 是否真的活着」为准。\n"
            f"  逃生门：确认安全后设 {ALLOW_SHARED_ROOT_ENV}=1 可降级放行（会显著记录警告）。"
        )
