"""markdown 权威面的**文件引擎**：``MEMORY.md`` / ``USER.md``（#1353 M2 + M3；#1494 拆分）。

移植自 NousResearch/hermes-agent ``tools/memory_tool_store.py``（commit
b324b1f7ed52369ab4bd3b1a03378729120b0f01，2026-10-02，MIT，
Copyright (c) 2025 Nous Research），保留逐行结构以便与上游对读。

# #1494（ADR-0058 D2）后的职责边界

本模块**只剩机制**：路径与目录、锁、读盘与解析、漂移备份、原子替换、容量/条目计数、
会话快照渲染，以及模型面响应的措辞（consolidation 预算与 usage 文案）。
**写操作的语义**（add / replace / remove / batch / remove_by_digest 的匹配、扫描、上限、
pin 规则与批量原子性）全部归 `commit.MemoryCommit`——它是当前内容的唯一可变事实入口。
本模块只暴露一个变更入口 `mutate_now()`，且只有 `commit.py` 调用它（静态图见
`tests/memory/v2/test_1494_single_writer.py`）。

保留在本模块的写侧动作只有两个，都属于「机制的必然部分」而不是可选写路径：
`mutate_now` 临界区本身，以及它内部的漂移备份与 `os.replace`。

# 与上游的四处有意偏差（issue #1353 §0 已批准）

1. **漂移语义反转**：上游发现文件无法 round-trip 时**拒写**（`_drift_error`）。
   本仓改为「先备份 ``.bak.<ts>``，再以文件为准继续写」——单用户场景下，
   手改/外部追加是合法的用户意图，拒写会让用户的编辑永远卡住。备份保证不丢。
2. 无 `hermes_constants.mkdir_under_hermes_home`：目录由本模块自建（0700）。
3. 无 monkeypatch 的模块级 `get_memory_dir`：目录在构造时注入，测试直接传 tmp_path。
4. `mutate_now` 的阻塞 IO（`fcntl.flock`）一律经 ``asyncio.to_thread`` 调用，
   绝不在事件循环里直调（issue §7 R9）。

# 权威性

markdown 是**唯一权威**；SQLite（`memory_v2_records`）降为派生/历史层，
由 `memory_v2_outbox` 异步投影。ADR-0046 D1 的「SQLite 唯一权威」被本票显式推翻。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_harness.memory.v2.file_state import atomic_write_text, file_lock
from agent_harness.memory.v2.threat_patterns import scan_for_threats
from agent_harness.session.context import current_session_var, run_context_var
from agent_harness.session.event import MEMORY_HUMAN_UPDATED

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


def error_response(message: str, **extra: Any) -> dict[str, Any]:
    """模型面失败响应的唯一形状（`success=False` + 文案 + 附加字段）。"""
    return {"success": False, "error": message, **extra}


def _read_failed_error(path: Path) -> dict[str, Any]:
    """文件存在但读不出来：按空视图保存会抹掉它，所以拒写。"""
    return error_response(
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


def entries_digest(entries: Sequence[str]) -> str:
    """一个目标文件**解析后条目序列**的摘要（#1494 回执的 before/after 口径）。

    对条目序列取摘要而不是原始字节：BOM、行尾、条目间空行都由解析吸收，
    回执判的是「内容是不是这一份」。与 `entry_digest` 同一哈希族。
    """
    return hashlib.sha256(ENTRY_DELIMITER.join(entries).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MutationOutcome:
    """`mutate_now` 的结果：要么是拒绝/只读响应，要么是「已写盘」的完整描述。

    ``response`` 非 None ⇒ mutator 返回了 dict（错误或 dry-run）：**没有写盘**。
    ``entries_before`` / ``entries_after`` 是解析后的条目序列（提交 seam 由此算回执摘要）；
    ``entries_after`` 为 None 表示未写盘。``message`` / ``extra`` 是 mutator 的成功文案与
    附加字段（`replaced_entry` / `deduplicated` …），由调用方组装模型面响应。
    """

    response: dict[str, Any] | None
    entries_before: list[str] | None
    entries_after: list[str] | None
    limit: int
    message: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    drift_backup: str | None = None

    @property
    def wrote(self) -> bool:
        return self.entries_after is not None


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


class MarkdownMemoryStore:
    """有界、文件支撑的 curated memory；**进程级单例**，跨 run / session 复用。

    ``_snapshot`` 按 session 冻结：首次渲染后，同一 session 内逐字节不变（前缀缓存稳定，
    M9）；缓存按插入序 FIFO 淘汰最早的 session（上限 ``SNAPSHOT_CACHE_LIMIT``），被淘汰
    的 session 下次渲染**重新读盘**——长寿进程里注入内容可能因此在会话中途变更。
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
        notification_mode: str = "on",
    ) -> None:
        self.directory = Path(memory_dir)
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        self.memory_enabled = memory_enabled
        self.user_profile_enabled = user_profile_enabled
        #: #1440 P3-3：漂移通知也遵守 `memory_notification_mode`（部署级档位）。
        #: 默认 `on` 保持其它构造点（测试 / migration）行为不变。
        self.notification_mode = str(notification_mode or "on").lower()
        #: session_id → {target: rendered}；首次渲染后冻结，不随写入失效（M9/AC8）。
        #: 没有会话结束钩子：回收只靠 `SNAPSHOT_CACHE_LIMIT` 的插入序 FIFO 淘汰，
        #: 被淘汰的 session 下次渲染重新读盘。
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
        """轮次边界调用：每个新 run 开始时由记忆宿主归零（#1439 F-03）。

        调用链：`AgentRuntime._drive` 的 run-start → `MemoryJobRunner.notify_run_started`
        → 本方法。目标是让「连续失败数」回到 `MAX_CONSOLIDATION_FAILURES_PER_TURN`
        所描述的 per-run 口径；不重置时它会跨 run 累积，长进程里后续 run 的自纠机会
        被提前掐断。

        **这是尽力而为的 per-run 预算，不是硬保证**：计数器是本进程单例上的裸 int，
        没有 session/run 维度。并发 run 的 run-start 会互相把对方的计数清零，正在累计
        的那一侧会被中途续满额度。单用户部署里并发 run 罕见，影响小。
        """
        self._consolidation_failures = 0

    def consolidation_failure(self, response: dict[str, Any]) -> dict[str, Any]:
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

    def disk_entries(self, target: str) -> list[str] | None:
        """**磁盘**上的活条目（去重）；文件存在但读不出来时返回 ``None``。

        返回 ``None`` 而不是空列表，是因为调用方要区分"文件确实空了"与"这次没读到"：
        对账拿后者当空集合会把派生层整片删掉。进程内 ``entries()`` 是会话快照，
        另一个文件在整场会话里都不会刷新，不能替代这个读法。
        """
        raw, read_ok = self.read_raw_checked(self.path_for(target))
        if not read_ok:
            return None
        return list(dict.fromkeys(self._parse_entries(raw)))

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

    # ------------------------------------------------------------------ 写核心

    def _write_file(self, path: Path, entries: list[str]) -> None:
        try:
            atomic_write_text(path, ENTRY_DELIMITER.join(entries))
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

    def _notify_drift(self, target: str, backup: str, entries: Sequence[str]) -> None:
        """#1353 §0 裁决 4 / #1440 F-07：漂移备份发生 → 用户侧通知。

        载荷只带计数 / 指纹 / 备份指针，**不带正文**（不变量 #22：事件流不是第二份
        记忆真相）。指纹算法与 `journey.memory_fingerprint` 逐字相同，但不能 import
        它——journey 反向依赖本模块（环）。`entries` 是**被备份的那份磁盘内容**的解析
        结果，不是本次写入后的条目。没有当前 Session（直调存储、无 run 上下文）就不发：
        观察不能反过来要求调用方提供会话。通知失败只告警，绝不影响写入。
        `notification_mode == "off"` 时不发（#1440 P3-3：部署级开关对两个通知源一致）。
        """
        if self.notification_mode == "off":
            return
        session = current_session_var.get()
        if session is None:
            return
        try:
            session.append(
                MEMORY_HUMAN_UPDATED,
                {
                    "count": len(entries),
                    "fingerprints": [
                        hashlib.sha256(entry.strip().encode("utf-8")).hexdigest()[:12]
                        for entry in entries
                    ],
                    "backup": backup,
                    "target": target,
                    "source": "markdown_drift",
                },
                run_id=run_context_var.get(),
            )
        except Exception as error:  # noqa: BLE001 — 通知是观察，不能破坏写入
            logger.warning("漂移备份通知发送失败（%s）：%s", type(error).__name__, target)

    def mutate_now(
        self, target: str, mutator: Callable[[list[str], int], Any], *, skip_drift: bool = False,
    ) -> MutationOutcome:
        """加锁 → 重读磁盘 → ``mutator(entries, limit)`` → 落盘。

        这是本模块**唯一**的变更入口（ADR-0058 D2）；调用者只有 `commit.MemoryCommit`。
        阻塞 IO：一律经 ``asyncio.to_thread`` 调用，不在事件循环里直调。

        重读保证不会用旧快照覆盖并发写（上游 #119668）。读失败一律拒写（即便 add 也会
        重写整个文件）。**漂移不再拒写**：先备份 ``.bak.<ts>``，再以磁盘内容为准继续
        （issue #1353 §0 偏差 1），备份路径经 ``drift_backup`` 回传。

        ``mutator`` 返回 ``(new_entries, message)`` 或 ``(new_entries, message, extra)``；
        返回任何 dict 时原样透传且**不落盘**（错误 dict，或只读闭包的 dry-run 结果）。
        漂移备份已发生时，错误 dict 也合并 ``drift_backup``（#1440 F-07：失败路径不能
        丢掉"备份在哪"这个事实，与成功路径同形）。
        """
        path = self.path_for(target)
        bak: str | None = None
        drift_entries: list[str] = []
        with self._guard, file_lock(path):
            raw, read_ok = self.read_raw_checked(path)
            if not read_ok:
                return MutationOutcome(
                    response=_read_failed_error(path), entries_before=None,
                    entries_after=None, limit=self._char_limit(target),
                )
            bak = None if skip_drift else self._detect_external_drift(target, raw)
            # 漂移时仍以磁盘为准：解析结果就是权威内容，写回只是规范化。
            entries = list(dict.fromkeys(self._parse_entries(raw)))
            self._set_entries(target, entries)
            if bak:
                drift_entries = list(entries)
            result = mutator(list(entries), self._char_limit(target))
            if isinstance(result, dict):
                response = {**result, "drift_backup": bak} if bak else result
                outcome = MutationOutcome(
                    response=response, entries_before=entries, entries_after=None,
                    limit=self._char_limit(target), drift_backup=bak,
                )
            else:
                new_entries = result[0]
                self._set_entries(target, new_entries)
                self._ensure_directory()
                self._write_file(path, new_entries)
                outcome = MutationOutcome(
                    response=None, entries_before=entries, entries_after=list(new_entries),
                    limit=self._char_limit(target),
                    message=result[1],
                    extra=dict(result[2]) if len(result) > 2 else {},
                    drift_backup=bak,
                )
        if bak:
            # 锁外发通知：事件写盘（fsync）不该占着记忆文件的写锁。
            self._notify_drift(target, bak, drift_entries)
        return outcome

    def _usage_of(self, target: str, entries: Sequence[str]) -> str:
        return f"{len(ENTRY_DELIMITER.join(entries)):,}/{self._char_limit(target):,}"

    def failure_with_entries(
        self, target: str, entries: Sequence[str], message: str,
    ) -> dict[str, Any]:
        """携带活条目列表的 consolidation 失败，让模型就地合并。"""
        return self.consolidation_failure(
            error_response(
                message, current_entries=list(entries), usage=self._usage_of(target, entries),
            )
        )

    def batch_failure(self, target: str, entries: Sequence[str], message: str) -> dict[str, Any]:
        """batch 中止失败，**不**回显 ``current_entries``：store 没变、调用方已有清单，
        回显只会让每次重试把上下文撑得更大（上游 #97316）。"""
        return self.consolidation_failure(
            error_response(
                message + " No operations were applied (batch is all-or-nothing).",
                usage=self._usage_of(target, entries),
            )
        )

    def success_response(
        self, target: str, entries: Sequence[str], message: str | None = None, **extra: Any,
    ) -> dict[str, Any]:
        """TERMINAL 且**不带**条目列表：回显条目会诱导模型「再找点要修的」并重发同样的操作。"""
        self._consolidation_failures = 0
        return {
            "success": True,
            "done": True,
            "target": target,
            "usage": self._usage_pct(target, len(ENTRY_DELIMITER.join(entries))),
            "entry_count": len(entries),
            **({"message": message} if message else {}),
            **extra,
            "note": "Write saved. This update is complete — do not repeat it.",
        }

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
    "MutationOutcome",
    "entries_digest",
    "entry_digest",
    "error_response",
    "find_unique_match",
    "normalize_entry",
    "pinned_index",
    "stale_entry_message",
    "target_for",
]
