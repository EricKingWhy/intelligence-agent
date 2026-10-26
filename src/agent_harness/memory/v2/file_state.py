"""#1494 / ADR-0058 D1+D11：操作状态的文件原语与状态根布局。

# 为什么需要这一层

ADR-0056 把五类操作状态（job/lease/幂等、approval/pin、settings、tombstone、receipt）判给
非 SQLite 的 owner，但它们共用同一套底层动作：**原子写一个 JSON 文件**、**跨进程串行化**
一次读-改-写、**读回一个 JSON**。这套动作在仓内已有一份被 Linux/Windows 双平台实测过的
实现（`markdown_store._file_lock`，自 #1353 起）——本模块把它抽出来复用一处，新增的只是
「写盘要 fsync」与「文件 id 必须文件名安全」两条。

# 不冒称

`atomic_write_text` 保证的是**单文件对并发读者原子**（`os.replace` 的既有语义）与尽力而为的
持久性（file fsync；POSIX 上补目录 fsync，Windows 由 NTFS 元数据日志承担，见
`_sync_directory`）。它**不**保证跨文件原子、也**不**保证断电后多文件整体一致——那两件事由
receipt 对账承担（ADR-0058 D3），不靠文件系统假象。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DIR_MODE = 0o700
FILE_MODE = 0o600

#: 状态根目录名（相对 `settings.workspace_dir`，与 `memories/`、`memory-v2.db` 并列）。
MEMORY_STATE_DIRECTORY_NAME = "memory-state"


class StateFileError(RuntimeError):
    """状态文件不可读/不可写。调用方必须当成「未收敛」处理，不得静默当空。"""


class CorruptStateFile(StateFileError):
    """文件在，但不是合法 JSON 对象：截断写入、外部编辑或磁盘故障。"""


def utc_stamp() -> str:
    """与 `_sqlite.stamp()` 同口径（UTC ISO），便于两代载体对读时间字段。"""
    return datetime.now(UTC).isoformat()


_WINDOWS_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
})


def safe_id(value: str, *, what: str = "id") -> str:
    """文件名级 id：只允许 ``[A-Za-z0-9._-]``；其余一律拒绝。

    五类状态都用一个 id 命名文件，id 又可能来自调用方（memory_id / job_id / 测试）。
    在这里挡住路径分隔符、``..``、Windows 保留设备名（``CON`` / ``NUL`` / ``COM1``…
    即使带扩展名也保留）与结尾点（Windows 会静默去掉），比在每个 store 里各写一遍
    校验少一处漏点。
    """
    if (
        not value
        or set(value) == {"."}  # "." / ".." / "..." —— 直接用作目录项会越出状态根
        or not all(character.isalnum() or character in "._-" for character in value)
        or value.endswith(".")
        or value.split(".", 1)[0].lower() in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError(f"unsafe {what} for a state file name: {value!r}")
    return value


def _sync_directory(path: Path) -> None:
    """把目录项也刷下去——文件 fsync 不覆盖「目录里新增了这个名字」。

    判据来源与 DSH 的附件发布同源（本地浅克隆 `D:\\reference\\deepseek-harness`
    @ `5badb150` 的 `packages/attachment/attachment-local/src/store.ts:127-140`：目录项没落盘
    时崩溃会让引用悬空）。Windows 打不开目录句柄（`os.open(dir)` 报 EACCES），
    该项持久性由 NTFS 元数据日志承担——显式跳过并在此说明，不假装做到了。
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


#: Windows 上 `os.replace` 的瞬时拒绝：`ERROR_ACCESS_DENIED` / `ERROR_SHARING_VIOLATION`。
#: 目标文件正被另一个线程用 `open()` 读着时，原子替换会撞上这两个错误——CRT 的读打开
#: 不带 `FILE_SHARE_DELETE`。POSIX `rename` 没有这个窗口，所以只在 Windows 错误码上重试。
_WINDOWS_REPLACE_CONFLICTS = frozenset({5, 32})
_REPLACE_ATTEMPTS = 20
_REPLACE_RETRY_BASE_SECONDS = 0.005


def _replace_with_retry(source: str, destination: Path) -> None:
    """`os.replace`，对 Windows 的共享冲突做有界退避重试。

    读路径（`read_json` / `_scan`）与写路径各自持锁不同：文件锁串行化的是写者，
    读者可以随时打开目标。Windows 的替换因此可能被一个短暂的读句柄拒绝；这是文件系统
    语义，不是数据冲突。重试上限约 1.4 秒，超过就把真实错误抛出去，不无限吞。
    """
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) not in _WINDOWS_REPLACE_CONFLICTS:
                raise
            if attempt == _REPLACE_ATTEMPTS:
                raise
            time.sleep(min(_REPLACE_RETRY_BASE_SECONDS * 2 ** (attempt - 1), 0.1))


def atomic_write_text(path: Path, text: str) -> None:
    """同目录 tempfile + ``os.replace``：读者永远看不到半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(path.parent, DIR_MODE)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".mstate_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(OSError):
            os.chmod(tmp_name, FILE_MODE)
        _replace_with_retry(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise
    with suppress(OSError):
        os.chmod(path, FILE_MODE)
    _sync_directory(path.parent)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """对独立 ``.lock`` 文件加排他锁；被保护的文件本身仍可被原子替换。

    advisory 语义：只有同样走本函数的写者会被串行化（本仓内**所有**状态写者都是）。
    进程崩溃时内核释放锁，不留死锁残留——这正是选 flock 而不是自造 ``O_EXCL`` 锁文件的原因
    （``O_EXCL`` 在持有者被 kill 后需要额外的过期判定，判错了就是永久阻塞）。
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows
        fcntl = None  # type: ignore[assignment]
        import msvcrt
    else:
        msvcrt = None  # type: ignore[assignment]

    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        os.chmod(path.parent, DIR_MODE)
    lock_path = path.with_suffix(path.suffix + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    raw_fd = os.open(lock_path, flags, FILE_MODE)
    try:
        # 创建模式会被 umask 过滤，也不会修好旧进程留下的松权限；在加锁前按 fd 收紧。
        if hasattr(os, "fchmod"):
            os.fchmod(raw_fd, FILE_MODE)
        fd = os.fdopen(raw_fd, "r+", encoding="utf-8")
    except Exception:
        os.close(raw_fd)
        raise
    with fd:
        def _flock(unlock: bool) -> None:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX)
            else:  # pragma: no cover — Windows
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_LOCK, 1)

        _flock(False)
        try:
            yield
        finally:
            with suppress(OSError):
                _flock(True)


@asynccontextmanager
async def async_file_lock(path: Path) -> Any:
    """`file_lock` 的异步外壳：加锁/解锁都在工作线程里做，事件循环不阻塞。

    锁跨 ``await`` 持有：本仓的临界区都只包本地小文件读写或一次 SQLite 事务，
    不跨模型调用（见 `jobs.commit_with_outcome`）。
    """
    manager = file_lock(path)
    await asyncio.to_thread(manager.__enter__)
    try:
        yield
    finally:
        await asyncio.to_thread(manager.__exit__, None, None, None)


def read_json(path: Path) -> dict[str, Any] | None:
    """``None`` = 文件不存在；坏文件抛 ``CorruptStateFile``（不当成空对象）。

    坏文件静默当空是「删除抑制/幂等键丢失」这类事故的入口：调用方会以为这条状态从没存在过。
    """
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as error:
        raise CorruptStateFile(f"{path.name} is not valid UTF-8") from error
    except OSError as error:
        raise StateFileError(f"cannot read {path.name}: {type(error).__name__}") from error
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise CorruptStateFile(f"{path.name} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise CorruptStateFile(f"{path.name} must contain a JSON object")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


class MemoryStateRoot:
    """`<workspace_dir>/memory-state/` 的路径布局（ADR-0058 D1）。

    只回答「东西放哪」；不读不写。各 owner 的 store 拿它构造自己的路径，因此布局只有一处定义。
    """

    def __init__(self, base: Path | str) -> None:
        self.base = Path(base)

    # ---------------------------------------------------------------- 文件

    @property
    def settings_path(self) -> Path:
        return self.base / "settings.json"

    @property
    def legacy_marker_path(self) -> Path:
        return self.base / "legacy-import.json"

    @property
    def settings_conflict_path(self) -> Path:
        """D9 旧 settings 多行冲突的人工裁决标记；存在即表示运行时必须 fail-closed。"""
        return self.base / "settings-conflict.json"

    # ---------------------------------------------------------------- 目录

    @property
    def tombstones_dir(self) -> Path:
        return self.base / "tombstones"

    @property
    def staged_dir(self) -> Path:
        return self.base / "staged"

    @property
    def blocked_dir(self) -> Path:
        return self.base / "blocked"

    @property
    def jobs_dir(self) -> Path:
        return self.base / "jobs"

    @property
    def receipts_dir(self) -> Path:
        return self.base / "receipts"

    @property
    def receipt_locks_dir(self) -> Path:
        return self.receipts_dir / "locks"

    def ensure(self) -> None:
        """建出全部目录（幂等）。锁文件目录另算：它只在有对应 op 时才需要。"""
        for directory in (
            self.base, self.tombstones_dir, self.staged_dir, self.blocked_dir,
            self.jobs_dir, self.receipts_dir, self.receipt_locks_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            with suppress(OSError):
                os.chmod(directory, DIR_MODE)

    def receipt_lock_path(self, operation_id: str) -> Path:
        return self.receipt_locks_dir / safe_id(operation_id, what="operation id")


__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "MEMORY_STATE_DIRECTORY_NAME",
    "CorruptStateFile",
    "MemoryStateRoot",
    "StateFileError",
    "async_file_lock",
    "atomic_write_text",
    "file_lock",
    "read_json",
    "safe_id",
    "utc_stamp",
    "write_json",
]
