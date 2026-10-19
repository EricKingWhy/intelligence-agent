"""markdown 权威的记忆存储：``MEMORY.md`` / ``USER.md``（#1353 M2 + M3）。

移植自 NousResearch/hermes-agent ``tools/memory_tool_store.py``（commit
b324b1f7ed52369ab4bd3b1a03378729120b0f01，2026-10-02，MIT，
Copyright (c) 2025 Nous Research），保留逐行结构以便与上游对读。

# 与上游的四处有意偏差（issue #1353 §0 已批准）

1. **漂移语义反转**：上游发现文件无法 round-trip 时**拒写**（`_drift_error`）。
   本仓改为「先备份 ``.bak.<ts>``，再以文件为准继续写」——单用户场景下，
   手改/外部追加是合法的用户意图，拒写会让用户的编辑永远卡住。备份保证不丢。
2. 无 `hermes_constants.mkdir_under_hermes_home`：目录由本模块自建（0700）。
3. 无 monkeypatch 的模块级 `get_memory_dir`：目录在构造时注入，测试直接传 tmp_path。
4. `_mutate` 的阻塞 IO（`fcntl.flock`）一律经 ``asyncio.to_thread`` 调用，
   绝不在事件循环里直调（issue §7 R9）。

# 权威性

markdown 是**唯一权威**；SQLite（`memory_v2_records`）降为派生/历史层，
由 `memory_v2_outbox` 异步投影。ADR-0046 D1 的「SQLite 唯一权威」被本票显式推翻。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from agent_harness.memory.v2.threat_patterns import (
    first_threat_message as _first_threat_message,
)
from agent_harness.memory.v2.threat_patterns import (
    scan_for_threats,
)

logger = logging.getLogger(__name__)

#: 渲染块标题；与上游 `MEMORY_BLOCK_HEADERS` 逐字一致。
MEMORY_BLOCK_HEADERS = {
    "memory": "MEMORY (your personal notes)",
    "user": "USER PROFILE (who the user is)",
}

#: 条目分隔符。上游同值；`§` 单独出现不算分隔。
ENTRY_DELIMITER = "\n§\n"

#: 容量预算（M3）：字符数，与模型无关。
DEFAULT_MEMORY_CHAR_LIMIT = 2200
DEFAULT_USER_CHAR_LIMIT = 1375

#: 每轮允许的 consolidation 失败次数；超过后返回 TERMINAL，模型停止重试（#42405）。
MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

#: `_snapshot` 的会话上限。快照**不随写入失效**（AC8：session 内逐字节不变），所以
#: 唯一能回收的就是旧会话；超上限时丢最早插入的那一个（dict 保序）。
SNAPSHOT_CACHE_LIMIT = 128

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def _scan_memory_content(content: str) -> str | None:
    """命中注入/外泄模式时的错误文案。strict scope：记忆进系统提示，污染会跨会话留存。"""
    return _first_threat_message(content, scope="strict")


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _read_failed_error(path: Path) -> dict[str, Any]:
    """文件存在但读不出来：按空视图保存会抹掉它，所以拒写。"""
    return _error(
        f"Refusing to write {path.name}: the file exists on disk but could not be read right now "
        f"(temporarily locked by another program, a permission change, invalid/corrupt text encoding, "
        f"or a filesystem error). Treating an unreadable file as empty and saving would wipe existing "
        f"memory, so the write is refused. Nothing was changed — retry in a moment."
    )


def find_unique_match(entries: list[str], old_text: str) -> tuple[int | None, bool]:
    """``(index, ambiguous)``：整条精确匹配优先于子串匹配。

    上游 `_find_unique_match` 逐行移植：`old_text == entry` 全等命中时忽略子串，
    这样短条目不会被更长的兄弟条目「吞掉」（remove('test') vs '...tests pass...'）。
    完全重复的多条命中是安全的（取第一条）；命中不同内容 → ``(None, True)``。
    """
    exact = [i for i, e in enumerate(entries) if e == old_text]
    matches = exact if exact else [i for i, e in enumerate(entries) if old_text in e]
    if len({entries[i] for i in matches}) > 1:
        return None, True
    return (matches[0] if matches else None), False


def target_for(kind: str, tier: str) -> str:
    """分拣规则的**唯一定义**：`kind=user` 或 `tier=profile` → ``user``，其余 → ``memory``。

    capability、migration、journey、notification 共用这一句。定义放在这个叶子模块
    （无内部导入），再在上面各层做薄适配，避免任何一个方向上的循环导入。
    """
    return "user" if tier == "profile" or kind == "user" else "memory"


def normalize_entry(text: str) -> str:
    """条目的唯一规范化：两端去空白。写入侧（add/replace/batch）与定位侧共用这一句。

    条目在文件里永远是 strip 过的；调用方手里的 `matched_entry`（SQLite 记录正文、
    staged pin）可能带尾随空白。两边不归一，pin 就会指向一条"磁盘上不存在"的原文。
    """
    return text.strip()


def entry_digest(text: str) -> str:
    """条目正文的稳定摘要。墓碑（content-free）与 markdown 清理入口之间的唯一对账键。

    算法必须与 `store.SqliteMemoryV2Store._digest` 一致（墓碑里的 `content_hashes`
    写的就是它）——所以定义放在这个叶子模块，store 反向引用，避免两处各写一遍。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pinned_index(entries: list[str], matched_entry: str) -> int | None:
    """staged 写入审阅时的那条完整条目当前的下标；条目已消失 → None（stale）。"""
    entry = normalize_entry(matched_entry)
    return entries.index(entry) if entry in entries else None


def stale_entry_message(entry: str) -> str:
    return (
        f"Entry changed since it was staged, so this write was not applied: '{entry}' is no longer "
        f"in memory as reviewed. Recreate the change against the current entry or reject it; the "
        f"pending record has been preserved."
    )


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录 tempfile + ``os.replace``，读者永远看不到半截文件；落盘后收紧到 0600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".mem_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        with suppress(OSError):
            os.chmod(tmp_name, _FILE_MODE)
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise
    with suppress(OSError):
        os.chmod(path, _FILE_MODE)


class MarkdownMemoryStore:
    """有界、文件支撑的 curated memory；一次会话一个实例。

    ``_snapshot`` 在会话开始渲染一次并冻结（前缀缓存稳定，M9）；
    ``memory_entries`` / ``user_entries`` 是活状态，写即落盘。
    """

    def __init__(
        self,
        memory_dir: Path,
        *,
        memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
        user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
        memory_enabled: bool = True,
        user_profile_enabled: bool = True,
    ) -> None:
        self.directory = Path(memory_dir)
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self.memory_enabled = memory_enabled
        self.user_profile_enabled = user_profile_enabled
        #: session_id → {target: rendered}；会话开始渲染一次，会话结束才失效（M9）。
        self._snapshot: dict[str, dict[str, str]] = {}
        self._consolidation_failures = 0
        # ponytail: 一把进程内互斥覆盖整个 `_mutate`；文件锁管跨进程，这把锁管
        # to_thread 的线程间活状态竞争。单用户单实例，不值得上细粒度锁。
        self._guard = threading.RLock()

    # ------------------------------------------------------------------ 路径与目录

    def path_for(self, target: str) -> Path:
        return self.directory / ("USER.md" if target == "user" else "MEMORY.md")

    def _ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(self.directory, _DIR_MODE)

    def target_enabled(self, target: str) -> bool:
        return self.user_profile_enabled if target == "user" else self.memory_enabled

    # ------------------------------------------------------------------ 熔断

    def reset_consolidation_failures(self) -> None:
        """轮次边界调用（新 run 开始）。"""
        self._consolidation_failures = 0

    def _consolidation_failure(self, response: dict[str, Any]) -> dict[str, Any]:
        """记一次 consolidation 失败：未超上限返回原响应（含重试指引），超上限返回 TERMINAL。"""
        self._consolidation_failures += 1
        if self._consolidation_failures <= MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {
            "success": False,
            "done": True,
            "error": (
                f"Memory consolidation failed {self._consolidation_failures} times this turn. Stop "
                "retrying memory calls — leave memory unchanged for now and continue with your reply "
                "to the user. The fact can be saved in a later turn."
            ),
        }

    # ------------------------------------------------------------------ 读取

    @staticmethod
    def _parse_entries(raw: str) -> list[str]:
        """去空白、去空条目；按**完整分隔符**切分，裸 ``§`` 会保留在条目内。"""
        return [e for e in (x.strip() for x in raw.split(ENTRY_DELIMITER)) if e]

    @staticmethod
    def read_raw_checked(path: Path) -> tuple[str, bool]:
        """``(raw, read_ok)``；仅当文件**存在但读不出来**时 ``read_ok=False``。

        解码保持 strict（``errors="replace"`` 会把有损视图交给后续保存，等于覆盖真实字节）；
        ``utf-8-sig`` 去掉记事本 BOM，否则 BOM 会永久粘在第一条上（上游 #10878 / PR #10888）。
        """
        if not path.exists():
            return "", True
        try:
            return path.read_text(encoding="utf-8-sig"), True
        except (OSError, UnicodeDecodeError):
            return "", False

    def load_from_disk(self) -> None:
        """读入两个文件并把**当前会话**的快照冻住。

        threat 命中条目只在**快照**里替换成 ``[BLOCKED: …]``；活列表保留原文，
        让用户看得见、删得掉（静默丢弃会把攻击藏起来）。
        """
        self._ensure_directory()
        for target in ("memory", "user"):
            path = self.path_for(target)
            entries = list(dict.fromkeys(self._parse_entries(self.read_raw_checked(path)[0])))
            self._set_entries(target, entries)
            if (count := self._char_count(target)) > (limit := self._char_limit(target)):
                # 外部写入可能超限；上限只在 add/replace 时触发，超限块会静默留在提示里。
                # 只警告，绝不截断用户的记忆（上游 #10877）。
                logger.warning(
                    "%s exceeds its char limit on load: %d/%d chars. Entries stay loaded; "
                    "further additions are blocked until it is back under the limit.",
                    path.name, count, limit,
                )

    def _sanitize(self, entry: str, filename: str) -> str:
        if not entry or entry.startswith("[BLOCKED:"):
            return entry
        findings = scan_for_threats(entry, scope="strict")
        if not findings:
            return entry
        logger.warning("Memory entry from %s blocked at load time: %s", filename, ", ".join(findings))
        return (
            f"[BLOCKED: {filename} entry contained threat pattern(s): {', '.join(findings)}. "
            f"Removed from system prompt; use memory(action=remove) to delete the original.]"
        )

    def render_snapshot(self, session_id: str) -> dict[str, str]:
        """会话级冻结快照（M9）：首次调用渲染，之后返回同一份。

        首次渲染**读盘**而不是读活列表：新会话可能由另一个进程（或用户手改）写下的内容
        驱动，读盘才与"markdown 是权威"一致；读不出来时退回活列表，绝不因读失败清空提示。

        快照刻意**不**在写入后失效（AC8：同一 session 内注入必须逐字节不变，前缀缓存
        才稳定），所以缓存按插入序做 FIFO 回收（命中不重排），见 `SNAPSHOT_CACHE_LIMIT`。
        """
        cached = self._snapshot.get(session_id)
        if cached is not None:
            return cached
        rendered: dict[str, str] = {}
        for target in ("memory", "user"):
            path = self.path_for(target)
            raw, read_ok = self.read_raw_checked(path)
            entries = (
                list(dict.fromkeys(self._parse_entries(raw))) if read_ok
                else self._entries_for(target)
            )
            block = self._render_block(target, [self._sanitize(e, path.name) for e in entries])
            rendered[target] = block
        while len(self._snapshot) >= SNAPSHOT_CACHE_LIMIT:
            self._snapshot.pop(next(iter(self._snapshot)))
        self._snapshot[session_id] = rendered
        return rendered

    def _entries_for(self, target: str) -> list[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: list[str]) -> None:
        setattr(self, "user_entries" if target == "user" else "memory_entries", entries)

    def _char_count(self, target: str) -> int:
        return len(ENTRY_DELIMITER.join(self._entries_for(target)))

    def _char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def _usage(self, target: str) -> str:
        return f"{self._char_count(target):,}/{self._char_limit(target):,}"

    def _usage_pct(self, target: str, current: int) -> str:
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        return f"{pct}% — {current:,}/{limit:,} chars"

    def _render_block(self, target: str, entries: list[str]) -> str:
        """系统提示块：标题 + 用量 + 条目（空列表 → 空串）。"""
        if not entries:
            return ""
        content, sep = ENTRY_DELIMITER.join(entries), "═" * 46
        title = MEMORY_BLOCK_HEADERS["user" if target == "user" else "memory"]
        return f"{sep}\n{title} [{self._usage_pct(target, len(content))}]\n{sep}\n{content}"

    # ------------------------------------------------------------------ 锁

    @contextmanager
    def _file_lock(self, path: Path) -> Iterator[None]:
        """对独立 ``.lock`` 文件加排他锁，记忆文件本身仍可被原子替换。"""
        try:
            import fcntl
        except ImportError:  # pragma: no cover — Windows
            fcntl = None  # type: ignore[assignment]
            import msvcrt
        else:
            msvcrt = None  # type: ignore[assignment]

        self._ensure_directory()
        lock_path = path.with_suffix(path.suffix + ".lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        raw_fd = os.open(lock_path, flags, _FILE_MODE)
        try:
            # 创建模式会被 umask 过滤，也不会修好旧进程留下的松权限；在加锁前按 fd 收紧，
            # 避免路径替换窗口。
            if hasattr(os, "fchmod"):
                os.fchmod(raw_fd, _FILE_MODE)
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

    # ------------------------------------------------------------------ 写核心

    def _write_file(self, path: Path, entries: list[str]) -> None:
        try:
            _atomic_write_text(path, ENTRY_DELIMITER.join(entries))
        except OSError as error:
            raise RuntimeError(f"Failed to write memory file {path}: {error}") from error

    def _detect_external_drift(self, target: str, raw: str) -> str | None:
        """漂移时把原文快照到 ``.bak.<ts>`` 并返回其路径，否则 None。

        信号：round-trip 不一致，或单条超过整文件上限（工具写不出这种条目，
        说明是外部追加的自由文本）。
        """
        parsed = self._parse_entries(raw)
        if not raw.strip() or (
            raw.strip() == ENTRY_DELIMITER.join(parsed)
            and max(map(len, parsed), default=0) <= self._char_limit(target)
        ):
            return None
        path = self.path_for(target)
        # 纳秒戳：秒级两道写入落在同一秒会互相覆盖，备份等于没备。
        bak_path = path.with_suffix(path.suffix + f".bak.{time.time_ns()}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except OSError:
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)

    def _mutate(
        self, target: str, mutator: Callable[[list[str], int], Any], *, skip_drift: bool = False,
    ) -> dict[str, Any]:
        """加锁 → 重读磁盘 → ``mutator(entries, limit)`` → 落盘 → 成功响应。

        重读保证不会用旧快照覆盖并发写（上游 #119668）。读失败一律拒写（即便 add 也会
        重写整个文件）。**漂移不再拒写**：先备份 ``.bak.<ts>``，再以磁盘内容为准继续
        （issue #1353 §0 偏差 1），备份路径经 ``drift_backup`` 回传。

        ``mutate`` 返回 ``(new_entries, message)`` 或 ``(new_entries, message, extra)``；
        返回任何 dict 时原样透传且**不落盘**（错误 dict，或只读闭包的 dry-run 结果）。
        """
        path = self.path_for(target)
        with self._guard, self._file_lock(path):
            raw, read_ok = self.read_raw_checked(path)
            if not read_ok:
                return _read_failed_error(path)
            bak = None if skip_drift else self._detect_external_drift(target, raw)
            # 漂移时仍以磁盘为准：解析结果就是权威内容，写回只是规范化。
            self._set_entries(target, list(dict.fromkeys(self._parse_entries(raw))))
            result = mutator(self._entries_for(target), self._char_limit(target))
            if isinstance(result, dict):
                return result
            self._set_entries(target, result[0])
            self._ensure_directory()
            self._write_file(path, result[0])
            extra_fields = result[2] if len(result) > 2 else {}
            response = self._success_response(target, result[1], **extra_fields)
            if bak:
                response["drift_backup"] = bak
            return response

    def _failure_with_entries(self, target: str, message: str) -> dict[str, Any]:
        """携带活条目列表的 consolidation 失败，让模型就地合并。"""
        return self._consolidation_failure(
            _error(message, current_entries=self._entries_for(target), usage=self._usage(target))
        )

    def _batch_failure(self, target: str, message: str) -> dict[str, Any]:
        """batch 中止失败，**不**回显 ``current_entries``：store 没变、调用方已有清单，
        回显只会让每次重试把上下文撑得更大（上游 #97316）。"""
        return self._consolidation_failure(
            _error(
                message + " No operations were applied (batch is all-or-nothing).",
                usage=self._usage(target),
            )
        )

    def _success_response(self, target: str, message: str | None = None, **extra: Any) -> dict[str, Any]:
        """TERMINAL 且**不带**条目列表：回显条目会诱导模型「再找点要修的」并重发同样的操作。"""
        self._consolidation_failures = 0
        return {
            "success": True,
            "done": True,
            "target": target,
            "usage": self._usage_pct(target, self._char_count(target)),
            "entry_count": len(self._entries_for(target)),
            **({"message": message} if message else {}),
            **extra,
            "note": "Write saved. This update is complete — do not repeat it.",
        }

    # ------------------------------------------------------------------ 操作

    def _add_sync(self, target: str, content: str) -> dict[str, Any]:
        content = normalize_entry(content)
        if not content:
            return _error("Content cannot be empty.")
        if scan_error := _scan_memory_content(content):
            return _error(scan_error)

        def _add(entries: list[str], limit: int) -> Any:
            if content in entries:
                return self._success_response(
                    target, "Entry already exists (no duplicate added).", deduplicated=True,
                )
            if len(ENTRY_DELIMITER.join(entries + [content])) > limit:
                return self._failure_with_entries(
                    target,
                    f"Memory at {self._char_count(target):,}/{limit:,} chars. Adding this entry "
                    f"({len(content)} chars) would exceed the limit. Consolidate now: use 'replace' to "
                    f"merge overlapping entries into shorter ones or 'remove' stale or less important "
                    f"entries (see current_entries below), then retry this add — all in this turn.",
                )
            return entries + [content], "Entry added.", {"deduplicated": False}

        # add 会重写整个文件，所以要和其他写路径一样走漂移备份：手改后追加不是漂移
        # （`_detect_external_drift` 只在 round-trip 失败或单条超限时返回备份路径），
        # 但**无法 round-trip** 的手改会在 add 时被静默规范化——没有备份就等于吞掉。
        return self._mutate(target, _add)

    def _replace_sync(
        self, target: str, old_text: str, new_content: str, matched_entry: str | None = None,
    ) -> dict[str, Any]:
        new_content = new_content.strip()
        if not old_text.strip():
            return _error("old_text cannot be empty.")
        if not new_content:
            return _error("new_content cannot be empty. Use 'remove' to delete entries.")
        if scan_error := _scan_memory_content(new_content):
            return _error(scan_error)
        return self._edit(target, old_text.strip(), new_content, matched_entry)

    def _remove_sync(self, target: str, old_text: str, matched_entry: str | None = None) -> dict[str, Any]:
        if not old_text.strip():
            return _error("old_text cannot be empty.")
        return self._edit(target, old_text.strip(), None, matched_entry)

    def _locate(
        self, entries: list[str], old_text: str, verb: str, matched_entry: str | None = None,
    ) -> int | dict[str, Any]:
        """``old_text`` 选中的条目下标，或编辑应返回的错误 dict。

        staged 写入携带审阅时的**完整条目**（``matched_entry``）：只有那一条合格，
        重放不会命中一条「仍包含 old_text 的更新条目」。
        """
        if matched_entry is not None:
            idx = pinned_index(entries, matched_entry)
            return idx if idx is not None else _error(stale_entry_message(matched_entry))
        idx, ambiguous = find_unique_match(entries, old_text)
        if ambiguous:
            return _error(
                f"Multiple entries matched '{old_text}'. Be more specific.",
                matches=[e[:80] + ("..." if len(e) > 80 else "") for e in entries if old_text in e],
            )
        if idx is None:
            return self._consolidation_failure(
                _error(
                    f"No entry matched '{old_text}'. Check current_entries below and retry with the "
                    f"exact text of the entry you want to {verb}.",
                    current_entries=entries,
                )
            )
        return idx

    def _resolve_entry_sync(self, target: str, old_text: str, verb: str) -> dict[str, Any]:
        """``{"success": True, "matched_entry": <完整条目>}``，在锁内读当前态。"""

        def _resolve(entries: list[str]) -> dict[str, Any]:
            idx = self._locate(entries, old_text.strip(), verb)
            return idx if isinstance(idx, dict) else {"success": True, "matched_entry": entries[idx]}

        return self._mutate(target, lambda entries, _limit: _resolve(entries), skip_drift=True)

    def _edit(
        self, target: str, old_text: str, new_content: str | None, matched_entry: str | None = None,
    ) -> dict[str, Any]:
        def _apply(entries: list[str], limit: int) -> Any:
            idx = self._locate(entries, old_text, "replace" if new_content else "remove", matched_entry)
            if isinstance(idx, dict):
                return idx
            replaced = entries[:idx] + ([] if new_content is None else [new_content]) + entries[idx + 1:]
            if new_content is None:
                return replaced, "Entry removed.", {"removed_entry": entries[idx]}
            new_total = len(ENTRY_DELIMITER.join(replaced))
            if new_total > limit:
                return self._failure_with_entries(
                    target,
                    f"Replacement would put memory at {new_total:,}/{limit:,} chars. Shorten the new "
                    f"content, or 'remove' other stale or less important entries to make room (see "
                    f"current_entries below), then retry — all in this turn.",
                )
            return replaced, "Entry replaced.", {"replaced_entry": entries[idx]}

        return self._mutate(target, _apply)

    @staticmethod
    def _apply_batch_op(
        working: list[str], act: str, content: str, old_text: str, pos: str,
        matched_entry: str | None = None,
    ) -> tuple[str | None, str | None]:
        """把一个 batch op 应用到 *working*；返回 ``(错误消息, 被替换/删除的原条目)``。"""
        if act == "add":
            if not content:
                return f"{pos}: content is required.", None
            if content not in working:  # 幂等：重复条目跳过，不让整批失败
                working.append(content)
            return None, None
        if act not in ("replace", "remove"):
            return f"{pos}: unknown action. Use add, replace, or remove.", None
        if not old_text:
            return f"{pos}: old_text is required.", None
        if act == "replace" and not content:
            return f"{pos}: content is required (use action='remove' to delete).", None
        if matched_entry is not None:
            idx = pinned_index(working, matched_entry)
            if idx is None:
                return f"{pos}: {stale_entry_message(matched_entry)}", None
        else:
            idx, ambiguous = find_unique_match(working, old_text)
            if ambiguous:
                return f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific.", None
            if idx is None:
                return f"{pos}: no entry matched '{old_text}'.", None
        previous_content = working[idx]
        working[idx:idx + 1] = [content] if act == "replace" else []
        return None, previous_content

    def _batch_sync(self, target: str, operations: list[dict[str, Any]], *, commit: bool) -> dict[str, Any]:
        if not operations:
            return _error("operations list is empty.")
        ops = [op or {} for op in operations]
        # 先扫所有 add/replace 内容，再碰磁盘——一个投毒 op 拒绝整批。
        for i, op in enumerate(ops):
            scan_error = (
                op.get("action") in {"add", "replace"}
                and op.get("content")
                and _scan_memory_content(op["content"])
            )
            if scan_error:
                return _error(f"Operation {i + 1}: {scan_error}")

        def _apply(entries: list[str], limit: int) -> Any:
            working = list(entries)  # 整批校验通过才提交
            matched: list[str | None] = []
            for i, op in enumerate(ops):
                act = op.get("action")
                msg, previous_content = self._apply_batch_op(
                    working,
                    act,
                    (op.get("content") or op.get("new_text") or "").strip(),
                    (op.get("old_text") or "").strip(),
                    f"Operation {i + 1} ({act or 'unknown'})",
                    op.get("matched_entry"),
                )
                if msg:
                    return self._batch_failure(target, msg)
                matched.append(previous_content)
            if entries and not working:
                # #103419：把最后一条删掉的 consolidation batch 会提交一个空文件当成功。
                # 拒绝；单条 remove() 才是有意清空的路径。
                label = self.path_for(target).name
                return self._batch_failure(
                    target,
                    f"Refusing to empty {label}: this batch would remove every entry from a "
                    f"previously non-empty store. Keep at least one entry — merge overlapping "
                    f"entries into a shorter one instead of removing the last one. To delete the "
                    f"final entry deliberately, use single remove() calls.",
                )
            new_total = len(ENTRY_DELIMITER.join(working))  # 只在最终态校验预算
            if new_total > limit:
                return self._batch_failure(
                    target,
                    f"After applying all {len(operations)} operations, memory would be at "
                    f"{new_total:,}/{limit:,} chars -- over the limit. Remove or shorten more "
                    f"entries in the same batch, then retry.",
                )
            if not commit:
                return {"success": True, "matched_entries": matched}
            replaced: dict[int, str] = {}
            removed: dict[int, str] = {}
            for i, (op, previous_content) in enumerate(zip(ops, matched), 1):
                if previous_content is not None:
                    (replaced if op.get("action") == "replace" else removed)[i] = previous_content
            extra: dict[str, Any] = {}
            if replaced:
                extra["replaced_entries"] = replaced
            if removed:
                extra["removed_entries"] = removed
            return working, f"Applied {len(operations)} operation(s).", extra

        return self._mutate(target, _apply, skip_drift=not commit)

    def _resolve_batch_entries_sync(
        self, target: str, operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._batch_sync(target, operations, commit=False)

    # ------------------------------------------------------------------ async 外壳

    async def add(self, target: str, content: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._add_sync, target, content)

    async def replace(
        self, target: str, old_text: str, new_content: str, matched_entry: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._replace_sync, target, old_text, new_content, matched_entry)

    async def remove(self, target: str, old_text: str, matched_entry: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._remove_sync, target, old_text, matched_entry)

    async def resolve_entry(self, target: str, old_text: str, verb: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._resolve_entry_sync, target, old_text, verb)

    async def apply_batch(self, target: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
        return await asyncio.to_thread(self._batch_sync, target, operations, commit=True)

    async def resolve_batch_entries(
        self, target: str, operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._resolve_batch_entries_sync, target, operations)

    async def remove_by_digest(self, digests: Sequence[str]) -> list[str]:
        """按正文哈希清理条目（墓碑-only 删除的补偿入口，见 `store.MemoryDeletionReceiptV2`）。

        两个文件都扫：墓碑是 content-free 的，除哈希外没有任何分拣线索（kind/tier 随
        记录行一起没了），所以不能替调用方猜 USER.md 还是 MEMORY.md。返回被清理的
        文件名（不含条目内容），供调用方留痕；没命中的文件不重写。
        """
        wanted = set(digests)
        if not wanted:
            return []
        cleaned: list[str] = []
        for target in ("memory", "user"):
            def _drop(entries: list[str], _limit: int) -> Any:
                kept = [entry for entry in entries if entry_digest(entry) not in wanted]
                removed = len(entries) - len(kept)
                if not removed:
                    return {"success": True, "removed_by_digest": 0}
                return kept, f"Removed {removed} entry(ies) matched by digest.", {
                    "removed_by_digest": removed,
                }

            response = await asyncio.to_thread(self._mutate, target, _drop)
            if response.get("removed_by_digest"):
                cleaned.append(self.path_for(target).name)
        return cleaned

    # ------------------------------------------------------------------ 查询

    def entries(self, target: str) -> list[str]:
        """活条目（原文，含被 threat 命中的条目）。"""
        return list(self._entries_for(target))

    def usage(self, target: str) -> str:
        return self._usage(target)

    @property
    def is_empty(self) -> bool:
        return not self.memory_entries and not self.user_entries


__all__ = [
    "DEFAULT_MEMORY_CHAR_LIMIT",
    "DEFAULT_USER_CHAR_LIMIT",
    "ENTRY_DELIMITER",
    "MAX_CONSOLIDATION_FAILURES_PER_TURN",
    "MEMORY_BLOCK_HEADERS",
    "MarkdownMemoryStore",
    "entry_digest",
    "find_unique_match",
    "normalize_entry",
    "pinned_index",
    "stale_entry_message",
    "target_for",
]
