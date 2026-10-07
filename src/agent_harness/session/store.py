"""JsonlSessionStore：SessionEvent 的薄 IO 层（JSONL append-only）。

只负责三件事：
    1. read_events(session_id) — 读取一个 Session 的全部有效事件
    2. append_event(session_id, event) — 向一个 Session 追加一条事件
    3. 守卫 seq 单调性（BUG-011）——重复 / 回退 seq 拒写（``SeqConflict``）

seq **分配**仍是 Session 聚合根的职责（``_next_seq``）；本层负责把每会话的
「读已落盘最大 seq → 判定 → 写」串成临界区，并拒写违反单调性的 seq
（写者跨线程：事件循环的 ``Session.append`` 与恢复扫描的工作线程都会进来）。
保证范围是**同一进程内**——跨进程没有文件锁，见 ``__init__`` 的边界说明。

另有一条护栏（ADR-0036）：本进程硬删过的会话 id 不再接受追加——``delete_session``
与 ``append_event`` 共用一把会话写锁，迟到的旁路写者（run 收尾后的记忆写回任务）
无法把已删会话的日志重建出来。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent_harness.session.errors import SeqConflict, SessionNotFound
from agent_harness.session.event import (
    RUN_TERMINAL_TYPES,
    SESSION_STARTED,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.session.header import StartedHeader

logger = logging.getLogger("agent_harness.session.store")

#: read_session_summary 头部扫描的解析上限：first_user_message 几乎总在
#: 会话最初几条事件里；超过此数仍未找到则放弃（返回 None，前端有
#: events 扫描降级路径）。只约束"解析几条"，不约束行计数（O(1)/行）。
_SUMMARY_HEAD_PARSE_LIMIT = 200

#: 损坏行原因词表（#565）：`invalid_utf8`（完整行字节流无效）/ `bad_json`（完整
#: 换行结尾的坏 JSON 行，含坏尾行）/ `not_event_dict`（合法 JSON 非事件字典）/
#: `bad_seq`（seq 缺失、类型非法或为负）/ `bad_event_fields`（事件字段解析失败）。
#: 词表消费方是恢复入口的拒绝文案与测试锚点，只增不改义。
CORRUPT_LINE_REASONS: frozenset[str] = frozenset(
    {"invalid_utf8", "bad_json", "not_event_dict", "bad_seq", "bad_event_fields"}
)


@dataclass(frozen=True)
class CorruptLine:
    """一条损坏行的**脱敏定位记录**（#565 audit 增强块）。

    只带定位元数据，**不带行内容**——events.jsonl 是 durable 事实，诊断记录
    （日志 / 报告）不得把它未脱敏地复制进第二条通道；完整行原字节保留在文件
    里（append-only），按 `byte_offset` / `sha256` 可人工定位核对。无换行
    撕裂尾段由 append 前的 `_neutralize_torn_tail` 处置（截断/封印），是
    「本层不改写 durable 行」的唯一例外。
    """

    lineno: int
    byte_offset: int
    byte_length: int
    line_sha256: str
    reason: str


@dataclass(frozen=True)
class EventLogIntegrity:
    """一次全量扫描的完整性报告（#565）。

    - `corrupt_lines`：完整坏行（含坏尾行）——不是"未写完整的末尾片段"，不能
      静默丢弃；恢复入口据此拒绝。
    - `seq_gaps` / `seq_duplicates`：解析后事件序列的断层与重复。持久化写入
      全部经 `Session.append`（max+1，store 守卫拒重号），**持久化 seq 连续是
      写入侧不变量**——文件里的断层只可能来自坏行跳过或整行丢失（手工编辑 /
      磁盘异常），两者都意味着 permission/tool 事实可能缺失。断层检测锚定
      seq=0（head-seq 锚定）：首事件 seq≠0 即头部整行丢失，同样报 gap。
    """

    corrupt_lines: tuple[CorruptLine, ...] = ()
    seq_gaps: tuple[tuple[int, int], ...] = ()
    seq_duplicates: tuple[int, ...] = ()

    @property
    def healthy(self) -> bool:
        return not (self.corrupt_lines or self.seq_gaps or self.seq_duplicates)


@dataclass(frozen=True)
class WorkspaceRef:
    """会话摘要里的**项目引用**（WS-3 / #153 AC1–AC2）：id 做请求/重命名，title 做显示。

    放在 session 层而不是 `agent_harness.workspace`：依赖方向是 session ← workspace
    （`workspace/models.py` 本来就 import 本层），反向 import 会成环。本类只是**值对象**
    ——它就是"会话摘要里那一格"的形状，不含任何项目领域行为（那些在 `WorkspaceIndex`）。
    """

    id: str
    title: str


@dataclass(frozen=True)
class SessionSummaryStats:
    """read_session_summary 的产出：列表页所需的最小字段集。

    `session_id` 是这一行的身份，与统计字段同属列表页所需——它在这里自洽后，
    `service.list_sessions` 可原样返回本 dataclass：列表行的字段因此只有一个带类型的
    定义点，新增字段漏改会被构造点/类型检查暴露，而不是静默丢在契约之外。
    """

    session_id: str
    event_count: int
    first_event_time: str | None
    last_event_time: str | None
    first_user_message: str | None
    #: OBS-010：最近一次 run 的 Langfuse trace_id（末事件是 run 终结事件时才有）。
    #: 未配置可观测性时终结事件里本就是 null → 这里也是 None（不变量 #21：可观测性
    #: 缺席既不致命也不伪造）。
    trace_id: str | None = None
    #: ARCH-4b：同一终结事件的 `trace_url`（人类可点击的 Langfuse URL，契约 2d7f87a
    #: / ADR-0018 D7）。与 `trace_id` 同源、同一套守卫；未配置可观测性时为 None。
    trace_url: str | None = None
    #: WS-3 / #153：会话所属项目；**未分组**（历史遗留 / 未命名 workspace / 装配里
    #: 没有 workspace 索引）时为 None，绝不伪造（不变量 #21 同族）。
    #: store 层不认识项目，只提供这个带类型的落点；值由 `SessionService.list_sessions`
    #: 从 `WorkspaceIndex` 回填（AC1 要求三处契约同时有该字段，这是其中之一）。
    workspace: WorkspaceRef | None = None
    #: #171：会话是否已归档（列表可见性，不是删除）。与 `workspace` 同款分工——store
    #: 读的是 JSONL，归档标记在 DB（`session_meta.archived`），值由
    #: `SessionService.list_sessions` 回填。**`session_meta` 无该会话行 = 未归档**
    #: （行由 lineage/fork 懒补，不是 1:1 恒成立），所以这里的默认值与"无行"同义，
    #: 不会撒谎；真值只能由服务层的联接给出（AC3）。
    archived: bool = False
    #: #752：事件日志是否损坏（零可解析事件但有损坏行）。损坏是一种可观测的
    #: 状态，不是"不存在"——列表应包含并标记，而不是静默丢弃（与 `recover`
    #: 的 409 诊断对齐）。由 `SessionService.list_sessions` 经完整性报告回填。
    corrupted: bool = False


#: #516：目录扫描 stat 批次的共享线程池（懒建）。stat 在 syscall 期间释放 GIL，
#: 分块并行把 4000 文件的现读 mtime 从 ~200ms 压到几十 ms；worker 数再往上调
#: 实测不再改善（NTFS 元数据吞吐饱和），8 是占用与收益的平衡点。
#: 两个声明（审查 P3 补记）：① 池是**模块级单例**——所有 JsonlSessionStore 实例
#: 共享（批次任务无状态，只有排队争用，无正确性风险）；② 进程生命周期常驻
#: （8 条非 daemon 线程，CPython atexit 回收），不随 store 关闭销毁。
_STAT_POOL_LOCK = threading.Lock()
_STAT_POOL: ThreadPoolExecutor | None = None


def _stat_pool() -> ThreadPoolExecutor:
    global _STAT_POOL
    with _STAT_POOL_LOCK:
        if _STAT_POOL is None:
            _STAT_POOL = ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="session-list-stat"
            )
        return _STAT_POOL


class _ScanCohort:
    """一次 in-flight 扫描的共享记录：同批 waiter 等 `done` 后取 `result`/`error`。"""

    __slots__ = ("done", "error", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: list[str] | None = None
        self.error: BaseException | None = None


class _ScanGate:
    """#516 目录扫描的单飞闸（request coalescing，nginx `proxy_cache_lock` /
    Go singleflight 同型）：并发突发只放行 leader 真扫一次，其余共享 in-flight 结果。

    契约边界（**保序前提**，`test_gate_does_not_cache_across_requests` 钉住）：
    只合并**时间上重叠**的调用——扫描结束后闸门即清，下一请求照旧现扫，外部
    utime / 外部写者下一眼可见，「mtime 每次请求现读」的语义不变，闸门不引入
    任何跨请求缓存。waiter 拿结果**副本**，共享不产生别名；leader 扫描异常时
    同批 waiter 收到同一异常，闸门随批清空不残留（`_inflight is cohort` 的
    同一性检查保证新批不受旧批影响）。

    为什么是线程原语：扫描方法体是同步 I/O，调用方经 `to_thread.run_sync`
    下放到工作线程——同拍的 100 个调用在不同的线程里，asyncio 层的 future
    共享够不着它们。leader 的扫描**不持锁执行**（只持 `_lock` 的瞬间做登记/
    清场），waiter 阻塞在 `cohort.done` 上，不占任何锁。

    非重入边界（独立审查 F1）：`scan` 回调内不得再进入本闸门——重入者会在锁内
    看到 `_inflight` 非空、成为等自己 cohort 的 waiter 自锁。当前
    `_list_session_ids_uncached` 及其全部调用方均无重入路径。闸门作用于
    `list_session_ids` 的所有调用方（含 lineage / workspace header 收集两处
    事件循环线程上的同步直调，审查 F4）：与工作线程扫描重叠时至多等一次扫描
    时长，无死锁——leader 是纯磁盘 I/O + `_stat_pool`，不需要事件循环。

    异常共享取舍（独立审查 F2）：leader 扫描失败时同批 waiter re-raise **同一
    异常实例**（`concurrent.futures.Future` 同款语义）——类型/消息/errno 不受
    影响，代价是 `__traceback__` 被并发 re-raise 改写、帧跨线程交错；磁盘
    OSError 属罕见路径，不为其加异常拷贝。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inflight: _ScanCohort | None = None

    def run(self, scan: Callable[[], list[str]]) -> list[str]:
        with self._lock:
            cohort = self._inflight
            if cohort is None:
                cohort = _ScanCohort()
                self._inflight = cohort
                leader = True
            else:
                leader = False
        if not leader:
            cohort.done.wait()
            if cohort.error is not None:
                raise cohort.error
            assert cohort.result is not None
            return list(cohort.result)
        try:
            result = scan()
        except BaseException as error:
            cohort.error = error
            with self._lock:
                if self._inflight is cohort:
                    self._inflight = None
            cohort.done.set()
            raise
        cohort.result = result
        with self._lock:
            if self._inflight is cohort:
                self._inflight = None
        cohort.done.set()
        return result


class JsonlSessionStore:
    """JSONL append-only 事件存储。

    文件布局：``<root>/<session_id>/events.jsonl``
    每行一条 JSON 事件，整行写入后立即 flush（崩溃安全：半行 = 没发生）。
    """

    def __init__(self, root: str | Path = ".agent/sessions") -> None:
        self._root = Path(root)
        # ── seq 守卫的进程内状态（BUG-011）──
        # 每会话一把锁：把「读已落盘最大 seq → 写」串成临界区。写者跨线程
        # （事件循环的 Session.append + 恢复扫描下放的工作线程），故用
        # threading.Lock，不能用 asyncio.Lock。
        self._seq_locks: dict[str, threading.Lock] = {}
        # 每会话「已落盘最大 seq」缓存 + 观测到的文件戳（size, mtime_ns）。
        # 缓存只用于省掉每次 append 的 O(n) 全量读；戳变化 = 文件被本实例之外
        # 的写者动过（另一 store 实例 / 另一进程，CLI 与 server 共用同一 sessions
        # 目录）→ 重新以磁盘为准。缓存与锁表的结构由 _state_guard 保护。
        #
        # 边界（不得夸大为「跨进程安全」）：本层没有文件锁，跨进程**同时**追加仍可能
        # 各自通过检查（检查与写入之间的窗口跨进程不互斥）。戳只能察觉「已经落盘」的
        # 外部追加。同一进程内（真实缺陷 BUG-011 的现场：一个 server 的两个并发请求）
        # 由 _lock_for + 本检查共同保证严格单调。
        self._last_seq: dict[str, tuple[int, int, int]] = {}
        # 本进程硬删过的会话 id（ADR-0036）：`append_event` 据此拒写。不清除是有意的——
        # 生产 id 由 uuid4 生成（没有客户端可控入口），同一 id 再次出现只可能是迟到写者；
        # 进程重启即归零。它**不落盘、不可恢复**，因此不是 ADR-0029 D1 反对的"墓碑"
        # （那条反对的是"已删但还在"的半状态）。
        self._deleted_ids: set[str] = set()
        # 列表页摘要缓存（#516）：{session_id: ((size, mtime_ns), 摘要)}。4000 会话的
        # 列表页每刷一次都重扫全部文件太贵——戳未变直接复用上次结果。与 _last_seq
        # 同款纪律：戳变化 = 本实例之外的写者动过文件 → 以磁盘为准重扫；结构由
        # _state_guard 保护（读侧 .get 不加锁：dict 单键读取原子，脏读最坏多扫一次）。
        self._summary_cache: dict[
            str, tuple[tuple[int, int], SessionSummaryStats]
        ] = {}
        self._state_guard = threading.Lock()
        # #516 并发列表的单飞闸（用户裁决第五选项）：见 _ScanGate 的契约边界。
        self._scan_gate = _ScanGate()

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def _events_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "events.jsonl"

    def _lock_for(self, session_id: str) -> threading.Lock:
        """取得该会话的写锁（懒创建；表结构由 _state_guard 保护）。

        这把锁是**守卫原子性的一部分**，不是可有可无的优化：`append_event` 的
        「读已落盘最大 seq → 判定 → 写」若不在同一临界区内，两个线程可能都读到同一
        last_seq、都通过判定、都落盘 → 重复 seq。写者跨线程（事件循环的
        `Session.append` + 恢复扫描下放的工作线程），故是 threading.Lock。
        """
        with self._state_guard:
            lock = self._seq_locks.get(session_id)
            if lock is None:
                lock = self._seq_locks[session_id] = threading.Lock()
            return lock

    def _last_seq_on_disk(self, session_id: str) -> int:
        """已落盘最大 seq（无文件 = -1）。带文件戳校验的缓存，必须在会话写锁内调用。

        戳变化即回读磁盘，因此外部（另一实例 / 另一进程）**已经落盘**的追加一定会被
        看见；它不能防的是跨进程**同时**写入（无文件锁），见 ``__init__`` 的边界说明。
        """
        path = self._events_path(session_id)
        try:
            stat = path.stat()
        except OSError:
            return -1
        stamp = (stat.st_size, stat.st_mtime_ns)
        with self._state_guard:
            cached = self._last_seq.get(session_id)
        if cached is not None and cached[1:] == stamp:
            return cached[0]
        last = max((e.seq for e in self.read_events(session_id)), default=-1)
        with self._state_guard:
            self._last_seq[session_id] = (last, *stamp)
        return last

    def append_event(self, session_id: str, event: SessionEvent) -> None:
        """向 Session 的 JSONL 追加一条事件（整行 + flush + fsync）。

        fsync 是断电不丢的底线（用户拍板的耐久性决策）：flush 只把进程缓冲
        推到 OS page cache，断电即失；fsync 才真正落盘。代价是每次 append
        一次磁盘同步——事件流是恢复的唯一真相源，宁慢不丢。

        seq 守卫（BUG-011）：seq ≤ 已落盘最大 seq 是**拒写**而不是写入。
        否则并发取号会留下重复 seq，使该会话此后任何构造聚合的路径
        （``Session.load`` / ``resume``）永久失败。

        **硬删之后不得重建**（ADR-0036）：本进程删过的 id 一律拒写（``SessionNotFound``）。
        否则迟到的旁路写者会在这里把日志凭空重建，静默撤销用户那次不可逆的删除。
        抛异常而不是静默丢：拒写是事实，写者（如 ``MemoryWriteback``）自带降级兜底。
        """
        line = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        # #650：UTF-8 可编码性验证**前置于**一切目录/文件变更与 seq 状态提交。
        # 孤立 surrogate 能通过 json.dumps（ensure_ascii=False 原样保留进 str），
        # 却到文本 write 的编码步骤才炸——彼时 mkdir 已执行、撕裂尾中立化可能
        # 已改写文件，失败形状是裸 UnicodeEncodeError 而非受控拒绝。用显式
        # ValueError 拒绝（票面指定；session/errors.py 不在本票文件边界内），
        # ``__cause__`` 保留 codec 异常供诊断；消息只装定位，不回显用户内容。
        try:
            line.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(
                f"Session '{session_id}' 事件 seq={event.seq} 含无法以 UTF-8 "
                "编码的孤立 Unicode 代理项（U+D800–U+DFFF），拒绝落盘"
                "（校验先于任何目录/文件变更与 seq 状态提交）"
            ) from error
        path = self._events_path(session_id)
        with self._lock_for(session_id):
            # 已删集合与建目录都在临界区内（ADR-0036）：迟到的 append 只可能看到
            # 「还没删」或「已登记」两种状态之一，不会写进一个正在被删的目录里。
            if session_id in self._deleted_ids:
                raise SessionNotFound(
                    f"Session '{session_id}' 已被硬删，拒绝重建事件日志"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            self._neutralize_torn_tail(path)
            last = self._last_seq_on_disk(session_id)
            if event.seq <= last:
                raise SeqConflict(
                    f"Session '{session_id}' 事件 seq 冲突: seq={event.seq} 已被占用"
                    f"（已落盘最大 seq={last}）——并发写入或日志已损坏，拒绝落盘"
                )
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            stat = path.stat()
            with self._state_guard:
                self._last_seq[session_id] = (
                    event.seq, stat.st_size, stat.st_mtime_ns,
                )

    def _neutralize_torn_tail(self, path: Path) -> None:
        """append 前中立化「无换行撕裂尾段」（#565 双轴收敛 P2 加固）。

        崩溃可能把 append 撕成两截：line+\\n 只落了前缀。#565 读侧把这种
        尾段按「写入中断预期形状」宽容，但写侧若不处置就追加，新记录会拼进
        尾段字节——宽容的预期形状变成完整坏行，事件从读投影消失且该会话
        此后所有恢复入口被永久拒绝（etcd WAL 对同型问题的注释：目的即防止
        后续 append 产生帧错误）。

        成熟产品语义（§6.1 方案依据，四独立来源收敛——撕裂尾段是崩溃预期
        形状，中立化后才能续写，有效前缀保留）：

        - Redis AOF：载入丢弃最后一个不完整命令，记
          ``Truncating the AOF at offset N``（aof-load-truncated yes 默认保可用）；
        - etcd WAL：``ReadAll`` 写模式 seek 到最后有效记录偏移 ``ZeroToEnd``；
        - SQLite WAL：恢复停在最后有效校验帧（mxFrame），坏尾被无视后安全覆写；
        - LevelDB：损坏→跳块，writer 只写完整 record。

        处置分两支（「截到最后**有效**记录」而非「截到上一个记录」）：
        尾段可解析为完整事件（只缺终止符，如写到 ``\\r`` 后断）→ 补终止符
        **封印**保留；其余（JSON 半行 / 多字节断裂 / 合法 JSON 非事件字典 /
        bad_seq——后两者不可能来自本 writer 的崩溃，只可能来自外部篡改，
        封印会产生闸门永久拒绝的完整坏行，截断是三选一的正解）→ 截断到
        最后换行边界。封印支是**本项目判据下的扩展**：四来源对内容完整但
        缺终止符的记录均为丢弃（帧校验过不了），本仓 JSONL 无帧校验、内容
        完整性可由 json+from_dict 等价验证，且读侧本就把无换行完整事件计为
        有效事件——封印只是物理规范化，零语义翻转。撕裂原字节不保留在主
        日志（保留撕裂形状的正是本修复要消除的故障面，四来源同），脱敏
        指纹（offset/len/sha256，不含内容）进诊断日志供事后对账。

        必须在会话写锁内调用（并发 append 各自先中立化会互相打架）。跨进程
        边界见 ``__init__``：无文件锁下两个进程同时 append 仍可能各自通过
        检查；本方法的截断支在该边界内多了一个「按陈旧 boundary 截掉另一
        进程刚落盘完整行」的主动删除面（前置条件罕见：撕裂尾段存在 + 跨
        进程同时 append），这是对既有声明的忠实披露而非新增安全声明。

        稳态成本 O(1)——常数次 syscall，不随文件大小增长：空文件 = seek+tell
        零字节读；\\n 结尾 = 2 次 seek + 1 次 tell + 1 字节读。仅尾字节非 \\n
        时才反向 4096 分块找边界，也不整读文件（append 热路径，审查 P2-1）。
        """
        tail_offset = 0
        tail = b""
        try:
            with path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if size == 0:
                    return
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) == b"\n":
                    return
                # 尾字节非 \n：撕裂尾段在场，反向分块找最后一条完整行边界
                chunk_size = 4096
                pos = size
                while pos > 0:
                    step = min(chunk_size, pos)
                    pos -= step
                    fh.seek(pos)
                    chunk = fh.read(step)
                    idx = chunk.rfind(b"\n")
                    if idx != -1:
                        tail_offset = pos + idx + 1
                        break
                fh.seek(tail_offset)
                tail = fh.read()
        except FileNotFoundError:
            return  # 首次 append：文件尚不存在，无撕裂可言
        event, _corrupt, _partial = self._classify_event_line(
            tail, path_name=path.name, lineno=0, byte_offset=tail_offset,
            log_findings=False,  # 探测模式：全静默，随后可能截断/封印同一字节
        )
        if event is not None:
            with path.open("ab") as fh:
                fh.write(b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            logger.warning(
                "补齐撕裂尾段终止符 %s offset=%d len=%d（完整事件缺换行，封印保留）",
                path.name, tail_offset, len(tail),
            )
            return
        with path.open("r+b") as fh:
            fh.truncate(tail_offset)
            fh.flush()
            os.fsync(fh.fileno())
        logger.warning(
            "截断撕裂尾段 %s offset=%d len=%d sha256=%s"
            "（写入中断的未完成记录，截断后 append 从记录边界续写；"
            "原字节不保留，指纹供事后对账）",
            path.name, tail_offset, len(tail), hashlib.sha256(tail).hexdigest(),
        )

    @staticmethod
    def _iter_event_lines(
        path: Path, limit: int | None = None
    ) -> Iterator[tuple[int, int, bytes]]:
        """Yield ``(lineno, byte_offset, raw_bytes)`` lazily; ``limit`` caps physical lines.

        All Store readers share this one file-open/line-enumeration path. Parsing stays
        in the per-caller layer so summary can count the whole file while parsing only
        its bounded head and final non-empty line.

        **二进制**遍历（#565）：损坏记录要保留**原字节**的偏移与摘要；文本模式
        `errors="replace"` 会先把坏字节改写成 U+FFFD，事后算的 hash 就对不上磁盘。
        行切分语义与文本模式一致（按 ``\\n``；最后一个物理段可无换行结尾——那是
        写入中断的预期形状）。解析所需的 str 由调用方按需 decode。
        """
        if limit is not None and limit <= 0:
            return
        with path.open("rb") as handle:
            offset = 0
            for lineno, raw in enumerate(handle, start=1):
                yield lineno, offset, raw
                offset += len(raw)
                if limit is not None and lineno >= limit:
                    break

    @staticmethod
    def _parse_event_line(raw_line: str, path_name: str, lineno: int) -> SessionEvent | None:
        """单行解析（容错语义的单一 owner，read_events / summary 共用）。

        容错范围：JSON 语法损坏（半行）、合法 JSON 但非事件字典、非法 UTF-8、
        seq 缺失或类型非法——一行坏数据只损失该行，不得 brick 恢复。
        """
        stripped = raw_line.strip()
        if not stripped:
            return None
        try:
            parsed: Any = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning("跳过损坏行 %s:%d（半行或写入中断）", path_name, lineno)
            return None
        if not isinstance(parsed, dict):
            logger.warning("跳过损坏行 %s:%d（合法 JSON 但非事件字典）", path_name, lineno)
            return None
        seq = parsed.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            logger.warning("跳过损坏行 %s:%d（seq 缺失、类型非法或为负）", path_name, lineno)
            return None
        try:
            return SessionEvent.from_dict(parsed)
        except Exception:  # 单行损坏只损失该行（容错兜底）
            logger.warning(
                "跳过损坏行 %s:%d（事件字段解析失败）", path_name, lineno, exc_info=True,
            )
            return None

    @staticmethod
    def _classify_event_line(
        raw: bytes, *, path_name: str, lineno: int, byte_offset: int,
        log_findings: bool = True,
    ) -> tuple[SessionEvent | None, CorruptLine | None, bool]:
        """读路径单行分类（#565）：事件 / 损坏（带脱敏定位记录）/ 未写完整的末尾片段。

        `log_findings=False` 是写侧探测模式（`_neutralize_torn_tail` 用）：只取
        分类判定，**全部日志静默**（损坏 WARNING 与 partial DEBUG 都不发）——
        探测之后可能紧跟着截断/封印同一字节，若仍按读侧口径记「原字节保留在
        文件中未改动 / 按写入中断跳过」就会日志撒谎；且探测传的 lineno 是
        哨兵值，不得出现在任何日志里（真实定位由修复动作自己的 WARNING 提供）。

        `_parse_event_line` 的容错语义保留给摘要 / header 快路径；本方法服务
        read_events 全量扫描，新增 audit 增强块要求的两条判别：

        - **partial tail**（返回第三位 True）：最后一个物理段**没有换行结尾**
          且自身解析不出来（JSON 语法坏 / 字节断在多字节字符中间）——写入中断
          的预期形状，按恢复预期容错跳过（DEBUG，不算损坏）；
        - **损坏**（返回 CorruptLine）：其余一切解析失败——完整换行结尾的坏
          JSON 行（**含坏尾行**：换行说明写入已完成，内容坏是磁盘/编辑问题，
          不是"还没写完"）、合法 JSON 非事件字典、seq 非法、事件字段非法、
          完整行的无效 UTF-8。这类行会造成 seq 断层或丢 permission/tool 事实，
          记 WARNING 并进报告，恢复入口据此拒绝。
        """

        def _corrupt(reason: str) -> tuple[SessionEvent | None, CorruptLine, bool]:
            record = CorruptLine(
                lineno=lineno,
                byte_offset=byte_offset,
                byte_length=len(raw),
                line_sha256=hashlib.sha256(raw).hexdigest(),
                reason=reason,
            )
            if log_findings:
                logger.warning(
                    "损坏行 %s:%d offset=%d len=%d sha256=%s reason=%s"
                    "（原字节保留在文件中未改动，恢复将拒绝）",
                    path_name, lineno, byte_offset, len(raw), record.line_sha256, reason,
                )
            return None, record, False

        ends_with_newline = raw.endswith(b"\n")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            if not ends_with_newline:
                # 断在多字节字符中间且无换行：写入中断的预期形状
                if log_findings:
                    logger.debug(
                        "末段未写完整 %s:%d（无效 UTF-8 半行，按写入中断跳过）",
                        path_name, lineno,
                    )
                return None, None, True
            return _corrupt("invalid_utf8")
        stripped = text.strip()
        if not stripped:
            return None, None, False
        try:
            parsed: Any = json.loads(stripped)
        except json.JSONDecodeError:
            if not ends_with_newline:
                if log_findings:
                    logger.debug(
                        "末段未写完整 %s:%d（半行，按写入中断跳过）", path_name, lineno,
                    )
                return None, None, True
            return _corrupt("bad_json")
        if not isinstance(parsed, dict):
            return _corrupt("not_event_dict")
        seq = parsed.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            return _corrupt("bad_seq")
        try:
            return SessionEvent.from_dict(parsed), None, False
        except Exception:  # noqa: BLE001 — 事件字段损坏只损失该行（容错兜底，但记入报告）
            return _corrupt("bad_event_fields")

    def read_events_report(
        self, session_id: str
    ) -> tuple[list[SessionEvent], EventLogIntegrity]:
        """读取全部有效事件 + 完整性报告（#565）。

        `read_events` 的容错语义不变（一行坏数据只损失该行，不 brick 显示路径）；
        差别只在多返回一份**脱敏完整性报告**：完整坏行（定位记录）+ 解析后序列的
        seq 断层 / 重复。恢复入口据此拒绝——对有洞的投影做恢复裁决，等于把丢失的
        permission/tool 事实当不存在（不变量 #14 的反面就是这类沉默）。
        """
        path = self._events_path(session_id)
        if not path.exists():
            return [], EventLogIntegrity()

        events: list[SessionEvent] = []
        corrupt: list[CorruptLine] = []
        for lineno, offset, raw in self._iter_event_lines(path):
            event, record, _partial = self._classify_event_line(
                raw, path_name=path.name, lineno=lineno, byte_offset=offset,
            )
            if event is not None:
                events.append(event)
            elif record is not None:
                corrupt.append(record)

        seen: set[int] = set()
        duplicates: list[int] = []
        gaps: list[tuple[int, int]] = []
        prev: int | None = None
        for event in events:
            if event.seq in seen:
                duplicates.append(event.seq)
                continue
            if prev is None:
                # head-seq 锚定（P3 残余修复）：持久化 seq 从 0 起连续是写入侧
                # 不变量（Session.start=0 + append max+1；fork child 经
                # adopt_history 重编 seq 同样从 0 起）⇒ 首事件 seq≠0 即头部
                # 整行丢失，没有坏行也必须报 gap，不能沉默。
                if event.seq > 0:
                    gaps.append((0, event.seq - 1))
            elif event.seq > prev + 1:
                gaps.append((prev + 1, event.seq - 1))
            seen.add(event.seq)
            prev = event.seq
        return events, EventLogIntegrity(
            corrupt_lines=tuple(corrupt),
            seq_gaps=tuple(gaps),
            seq_duplicates=tuple(duplicates),
        )

    def read_events(self, session_id: str) -> list[SessionEvent]:
        """读取 Session 的全部有效事件，跳过无法解析的损坏行。

        容错范围见 `_classify_event_line`——一行坏数据只损失该行，不得 brick
        整个 session 的显示路径；恢复入口不走本方法（走 `read_events_report`
        的完整性闸门，#565）。跳过行为有信号：完整坏行记 WARNING（脱敏定位），
        未写完整的末尾片段按恢复预期 DEBUG。
        """
        return self.read_events_report(session_id)[0]

    def read_session_summary(self, session_id: str) -> SessionSummaryStats | None:
        """列表页快路径：单趟流式扫描 + 文件戳缓存（#516）。

        GET /api/sessions 曾对每个会话做全量 JSON 解析（30 会话 × 2000 事件
        ≈ 秒级串行阻塞），而列表页只需要：首条 user 消息（头部早退）、首末
        事件时间（首行 + 末行）、事件数（行计数）。本方法把解析量从 O(全部
        事件) 压到 O(头部上限 + 1)。

        缓存契约（#516）：文件戳 `(size, mtime_ns)` 未变 → 返回上次结果（同一
        对象，不再扫盘）；戳变化（外部写者追加，size 必变）→ 以磁盘为准重扫。
        与 `_last_seq` 同款纪律：缓存只服务本实例的重复读，跨实例/跨进程一致性
        以戳为准。写缓存前再取一次戳、与扫描前一致才入——与扫描并发的外部追加
        不会被误标为已缓存（否则「旧内容 + 新戳」会静默钉住旧结果）。
        """
        path = self._events_path(session_id)
        if not path.exists():
            return None
        cached = self._summary_cache.get(session_id)
        if cached is not None and self._summary_stamp(path) == cached[0]:
            return cached[1]
        stamp_before = self._summary_stamp(path)
        stats = self._scan_session_summary(session_id, path)
        if stamp_before == self._summary_stamp(path):
            with self._state_guard:
                self._summary_cache[session_id] = (stamp_before, stats)
        return stats

    def read_session_summaries(
        self, session_ids: list[str]
    ) -> list[SessionSummaryStats | None]:
        """批量摘要（#516）：同一批 id 在**一次** to_thread 卸载里读完。

        逐 id `run_sync(read_session_summary, sid)` 在突发并发下每个 id 都要排一次
        线程池队列（100 并发 × 50 行 = 5000 次 hop），延迟被排队放大；批量化后每
        请求只剩一次 hop。语义与逐条调用逐位一致（复用同一缓存与回退路径）。
        """
        return [self.read_session_summary(session_id) for session_id in session_ids]

    @staticmethod
    def _summary_stamp(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return (stat.st_size, stat.st_mtime_ns)

    def _scan_session_summary(
        self, session_id: str, path: Path
    ) -> SessionSummaryStats:
        """摘要单趟扫描本体（无缓存，read_session_summary 的实现细节）。

        精确性契约：扫描路径上发现任何损坏行 → 整体回退 read_events 全量
        解析（列表语义与全量严格一致，只是慢）。未扫描到的中段损坏行会让
        event_count 偏大——该情形只可能来自手工编辑/磁盘异常（正常崩溃损坏
        集中在末行，已覆盖），属显示级字段的已知取舍；resume 恢复仍走
        read_events 全量容错，不受影响。
        """

        event_count = 0
        first_time: str | None = None
        first_user_message: str | None = None
        head_done = False
        head_parsed = 0
        # 末条非空行的 (lineno, stripped)：lineno 留给损坏告警——探针日志必须
        # 可定位，不用哨兵值伪造位置。只保留最后一条：末行损坏一律整体回退
        # （见下方守卫），所以不存在「改用前一行」的分支。
        last_line: tuple[int, str] | None = None

        for lineno, _offset, raw in self._iter_event_lines(path):
            raw_line = raw.decode("utf-8", errors="replace")
            stripped = raw_line.strip()
            if not stripped:
                continue
            event_count += 1
            last_line = (lineno, stripped)

            if not head_done:
                event = self._parse_event_line(raw_line, path.name, lineno)
                if event is None:
                    # 头部存在损坏行：中段完整性不可信 → 全量回退
                    return self._summary_fallback(session_id)
                if first_time is None:
                    first_time = event.time
                if (event.type == USER_MESSAGE
                        and isinstance(event.data.get("content"), str)
                        and event.data["content"].strip()):
                    first_user_message = event.data["content"].strip()[:128]
                    head_done = True
                else:
                    head_parsed += 1
                    if head_parsed >= _SUMMARY_HEAD_PARSE_LIMIT:
                        head_done = True

        # 末行损坏（崩溃半写的常态位置）→ 全量回退，保证精确。
        # 只解析这一次：last_event_time 与 trace_id 同源于本事件，快路径与
        # _summary_fallback 的口径一致因此是**结构性**的，而非两份实现靠约定对齐。
        last_event = (
            self._parse_event_line(last_line[1], path.name, last_line[0])
            if last_line is not None else None
        )
        if last_line is not None and last_event is None:
            return self._summary_fallback(session_id)

        return SessionSummaryStats(
            session_id=session_id,
            event_count=event_count,
            first_event_time=first_time,
            last_event_time=last_event.time if last_event is not None else None,
            first_user_message=first_user_message,
            trace_id=self._terminal_trace_field(last_event, "trace_id"),
            trace_url=self._terminal_trace_field(last_event, "trace_url"),
        )

    @staticmethod
    def _terminal_trace_field(
        event: SessionEvent | None, key: Literal["trace_id", "trace_url"]
    ) -> str | None:
        """从 run 终结事件取 trace 关联字段（`trace_id` / `trace_url`）——取值规则唯一 owner。

        只认 run 终结事件：trace 关联是 **per-run** 事实（`run/completed|failed|
        interrupted` 的 data 里），会话可能有多次 run，末端那次才是列表页要展示的。
        非终结类型 / 缺键 / null / 空串 / 非字符串 → None（不把磁盘上被改坏的值塞进
        API 契约，也不伪造占位串，不变量 #21）。

        两个键（`trace_id` 机器可读 / `trace_url` 人类可点击，ADR-0018 D7 契约、
        互不替代）由本方法**同一套守卫**取——共用实现而非两份约定对齐，所以两者
        不会出现「一个有值一个漏填」的漂移。

        快路径与 `_summary_fallback` 各自把自己的末事件传进来，所以**这条规则**在两条
        路径上的一致性是结构性的。（`last_event_time` 仍是两条各自取值的路径，只由
        `test_fast_path_agrees_with_full_parse_on_clean_session` 断言相等。）

        已知边界（有意取舍）：末事件不是 run 终结事件即返回 None——包括
        「上一轮已 completed，但新轮的 user/message 或 run/started 成了末事件」。
        此时更早那个已完成的 trace 不回填。不向历史回溯是因为那需要逐行
        `json.loads` 直到 EOF，正好抵消 `read_session_summary` 的快路径（列表页
        从秒级全量解析压到几十 ms）。在途/新轮返回「未追踪」属诚实降级：那是
        尚无最终 trace 的 run。
        """
        if event is None or event.type not in RUN_TERMINAL_TYPES:
            return None
        value = event.data.get(key)
        return value if isinstance(value, str) and value else None

    def _summary_fallback(self, session_id: str) -> SessionSummaryStats | None:
        """扫描路径发现损坏 → 全量解析，产出与旧实现严格一致的摘要。"""
        events = self.read_events(session_id)
        if not events:
            return SessionSummaryStats(
                session_id=session_id,
                event_count=0, first_event_time=None,
                last_event_time=None, first_user_message=None,
            )
        first_user_message = next(
            (e.data.get("content") for e in events
             if e.type == USER_MESSAGE and isinstance(e.data.get("content"), str)
             and e.data["content"].strip()),
            None,
        )
        if first_user_message is not None:
            first_user_message = first_user_message.strip()[:128]
        return SessionSummaryStats(
            session_id=session_id,
            event_count=len(events),
            first_event_time=events[0].time,
            last_event_time=events[-1].time,
            first_user_message=first_user_message,
            # 与快路径同口径：只看最后一个事件（保证两条路径严格一致）。
            trace_id=self._terminal_trace_field(events[-1], "trace_id"),
            trace_url=self._terminal_trace_field(events[-1], "trace_url"),
        )

    def list_session_ids(self) -> list[str]:
        """列出 root 下所有有 events.jsonl 的 session_id，按最近修改倒序。

        Phase 9 / Web UI 用：GET /sessions 的基础。空 root 返回空列表。

        #516：`os.scandir` 的 dirent 自带 is_dir（不再额外 stat），`events.jsonl`
        的存在性与 mtime 用**单次** `os.stat` 合并判定（异常代替 exists 预检）；
        stat 批次在线程池里并行执行——`os.stat` 在 syscall 期间释放 GIL，4000
        文件从 ~200ms 串行压到几十 ms。顺序语义不变：mtime **每次请求现读**，
        外部 utime / 外部写者下一眼生效
        （`test_list_by_workspace_follows_ledger_order_not_activity` 钉死），线程
        池只是同一批 stat 的并行执行，不引入任何缓存。

        #516 并发 AC（用户裁决第五选项）：方法体经 `_ScanGate` 单飞——并发突发
        （100 并发同拍列表）只让 leader 真扫一次，其余共享 in-flight 结果，40 万
        stat 突发坍缩为 1 次扫描的成本；扫描结束闸门即清，**不跨请求缓存**，串行
        路径语义与无闸门时逐字一致。waiter 拿副本；调用方不得假设多次调用共享
        同一 list 对象（副本语义是有意的）。
        """
        return self._scan_gate.run(self._list_session_ids_uncached)

    def _list_session_ids_uncached(self) -> list[str]:
        """无闸门的一次现扫（`list_session_ids` 的实体；测试经此缝计数）。"""
        if not self._root.exists():
            return []
        root_str = str(self._root)
        with os.scandir(root_str) as entries:
            candidates = [
                (entry.name, entry.path) for entry in entries if entry.is_dir()
            ]

        def _stat_chunk(chunk: list[tuple[str, str]]) -> list[tuple[str, float]]:
            out: list[tuple[str, float]] = []
            for name, dir_path in chunk:
                try:
                    mtime = os.stat(os.path.join(dir_path, "events.jsonl")).st_mtime
                except OSError:
                    continue
                out.append((name, mtime))
            return out

        ids: list[tuple[str, float]] = []
        pool = _stat_pool()
        # 按块提交：块序 = scandir 序，拼接后与原串行循环的收集顺序逐位一致
        # （同为 scandir 序），mtime 平局时的稳定序不变。
        step = 256
        futures = [
            pool.submit(_stat_chunk, candidates[i : i + step])
            for i in range(0, len(candidates), step)
        ]
        for future in futures:
            ids.extend(future.result())
        # 按修改时间倒序（最近在前）
        ids.sort(key=lambda x: x[1], reverse=True)
        return [sid for sid, _ in ids]

    def delete_session(self, session_id: str) -> bool:
        """删除该会话的事件日志目录（**唯一真相源**）。返回它是否曾经存在。

        硬删（ADR-0029）里"删掉会话"的实质就是这一句：`list_session_ids` 是按目录扫出来的，
        目录没了会话就从所有列表里消失。

        **安全边界**：只删 `<root>/<session_id>` 这一层——两个成分都是本 store 自己用
        root 与 session_id 拼的，且调用方必须先过 `validate_session_id`（拒绝分隔符）。
        本方法**不读任何外部映射**，因此不可能像 `WorkspaceRegistry.delete` 那样
        `rmtree` 到用户的真实目录（ADR-0029 D2）。

        幂等：目录不存在 → `False`，不抛错（重跑即自愈，ADR-0029 D3）。

        **删除与追加互斥**（ADR-0036）：整个删除与"登记已删 id"都在会话写锁内完成。
        否则迟到的写者能在 `rmtree` 与登记之间挤进来重建目录，而"谁赢"取决于线程调度。
        """
        session_dir = self._session_dir(session_id)
        if not session_dir.exists():
            return False
        with self._lock_for(session_id):
            shutil.rmtree(session_dir, ignore_errors=True)
            # 进程内 seq 缓存与锁表要一起清——否则同 id 再次出现时会带着旧的 last_seq。
            # 先登记已删 id 再摘锁：摘锁后若又有写者取到**新**锁，它在临界区里读到的
            # 也已经是"已删"（ADR-0036）。
            with self._state_guard:
                self._deleted_ids.add(session_id)
                self._last_seq.pop(session_id, None)
                self._seq_locks.pop(session_id, None)
        return True

    def read_started_header(self, session_id: str) -> StartedHeader | None:
        """只读会话 header（WS-2 / #152 AC14）：第一条 `session/started` 即停。

        **不读事件正文**——workspace 首次引导只允许看 header（id / cwd / createdAt），
        所以这里流式读、命中即返回，正文多长都不碰。区别于 `read_session_summary`
        （那个要数到文件尾才知道事件数）。

        形状非法（没有 events.jsonl / 没有 session/started）→ None，不抛错：引导面对
        的是历史日志，一条坏数据不该拖垮整个启动（与 `read_events` 的容错同款）。
        """
        path = self._events_path(session_id)
        if not path.exists():
            return None
        for lineno, _offset, raw in self._iter_event_lines(path):
            raw_line = raw.decode("utf-8", errors="replace")
            if not raw_line.strip():
                continue
            event = self._parse_event_line(raw_line, path.name, lineno)
            if event is None:
                continue
            if event.type != SESSION_STARTED:
                # header 正常就是第一条；遇到别的说明日志形状异常，不再往下翻
                # （继续翻就等于读正文了）。
                return None
            data = event.data or {}
            cwd = data.get("cwd")
            return StartedHeader(
                session_id=session_id,
                cwd=cwd if isinstance(cwd, str) and cwd else None,
                created_at=event.time,
                agent_id=event.agent_id,
            )
        return None
