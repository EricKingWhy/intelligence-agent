"""Fork：从父 Session 事件前缀派生新独立会话（Phase 14, ADR-0017）。

file-per-lineage（决策 1）：fork = 新 session 文件；每个 session 保持
append-only 线性 JSONL，树是 SessionMetaStore 索引层的元数据关系。

- boundary（决策 2）：机制上 seed 前缀必须止于 run 终态之后（child 文件
  绝不以悬空 run 开头）；UX 选择器 = 「从第 N 条用户消息分叉」——锚点
  消息本身不进 seed（child 侧由用户重新发送，pi /fork 同款语义）。
- seed（决策 3）：事件前缀逐字复制进 child（Session.adopt_history 重编
  seq、保留原 event_id），child 自包含可读，不依赖父文件存活。
- provenance（决策 8）：session/forked 只落 child 文件，父文件一字不改
  （父会话以 store.read_events 只读加载——绝不能 resume，那会写父）。
- 索引（决策 7）：fork 同时 upsert SessionMeta（origin=fork）。
- 分阶段可见性（#555）：fork 是多步物理过程（child 文件 → workspace 拷贝 →
  seed 移植 → provenance → 索引），单次 rename 保护不了跨步崩溃。child 在
  workspace 拷贝前落 durable `fork/in-progress` 意图标记（SQLite 提交日志
  同型），拷贝走暂存目录 + 同卷 rename 发布；失败/取消在调用栈内做幂等
  补偿，进程 kill 留下的现场由启动扫描（recovery/scan.py）按标记回收。
  补偿与扫描只回收 harness 自建工件（暂存目录、默认形态子工作区），
  child 的 JSONL 与映射文件保留——它们是「fork 未完成」的可读事实。
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from langchain_core.messages import HumanMessage

from agent_harness.agent.run_budget import session_budget_key
from agent_harness.prompt import DEFAULT_REGISTRY
from agent_harness.session.approval import (
    SESSION_AUTO_APPROVE_KEY,
    SESSION_PERMISSION_MODE_KEY,
    effective_auto_approve,
    effective_permission_mode,
)
from agent_harness.session.cwd import session_cwd
from agent_harness.session.derive import derive_protected_facts
from agent_harness.session.event import (
    AGENT_DELEGATION_FINISHED,
    FORK_IN_PROGRESS,
    MODEL_COMPLETED,
    PERMISSION_CHANGED,
    PERMISSION_GRANTED,
    PERMISSION_REVOKED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    RUN_TERMINAL_TYPES,
    SESSION_FORKED,
    SESSION_STARTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    WORKFLOW_MODE_CHANGED,
    SessionEvent,
)
from agent_harness.session.session import Session
from agent_harness.storage.session_meta import SessionMeta

if TYPE_CHECKING:
    from agent_harness.sandbox.registry import WorkspaceRegistry
    from agent_harness.session.store import JsonlSessionStore
    from agent_harness.storage.delegation_tree import (
        InMemoryDelegationTreeLedger,
        SqliteDelegationTreeLedger,
    )
    from agent_harness.storage.session_meta import SessionMetaStore

    #: 只读账行读数所需的接口（`get_session_budget`）；duck-typed，运行期不 import
    #: storage（fork 在 session 层，两个实现都满足这一条）。
    SessionBudgetLedger = SqliteDelegationTreeLedger | InMemoryDelegationTreeLedger

logger = logging.getLogger("agent_harness.session.fork")

#: tail 摘要输入的字符上限（尾部截断）——保护摘要调用不被超长会话打爆。
_MAX_TAIL_CHARS = 8000


class TailSummarizerProtocol(Protocol):
    """tail 摘要 seam：任何提供 async summarize(text)->str 的对象可用。"""

    async def summarize(self, text: str) -> str: ...


def render_tail_transcript(tail_events: list[SessionEvent]) -> str:
    """把 fork 点之后的事件渲染成有界可读文本（摘要器的输入）。"""
    lines: list[str] = []
    for event in tail_events:
        if event.type == USER_MESSAGE:
            lines.append(f"[user] {event.data.get('content', '')}")
        elif event.type == MODEL_COMPLETED:
            lines.append(f"[assistant] {event.data.get('content', '')}")
        elif event.type == TOOL_CALL:
            lines.append(f"[tool] {event.data.get('tool_name', '')}")
        elif event.type == TOOL_RESULT:
            content = str(event.data.get("content", ""))[:200]
            lines.append(f"[tool-result] {content}")
        elif event.type == RUN_FAILED:
            lines.append(
                f"[run] failed（{event.data.get('reason', 'unspecified')}）"
            )
        elif event.type == AGENT_DELEGATION_FINISHED:
            lines.append(
                f"[delegation] {event.data.get('target', '')}"
                f" → {event.data.get('status', '')}"
            )
    return "\n".join(lines)[-_MAX_TAIL_CHARS:]


class TailSummarizer:
    """tail 摘要器（ADR-0017 决策 9）：任何 ainvoke(messages)->AIMessage 的模型可用。

    pi branch_summary / oh-my-pi rewind-report 的 file-per-lineage 对应物：
    把「被放弃的线得出了什么」压缩成一段可携带的上下文。恰好一次调用，
    无重试放大；失败由调用方降级（fork 照常）。
    """

    def __init__(self, model, *, max_tail_chars: int = _MAX_TAIL_CHARS) -> None:
        self._model = model
        self._max_tail_chars = max_tail_chars

    async def summarize(self, tail_text: str) -> str:
        tail_text = tail_text[-self._max_tail_chars :]
        prompt = DEFAULT_REGISTRY.assemble(
            "aux:fork_tail", {"tail_text": tail_text}
        ).meta_user_text
        response = await self._model.ainvoke([HumanMessage(content=prompt)])
        return str(response.content)


class ForkBoundaryError(ValueError):
    """非法 fork 边界：锚点不存在 / 不是用户消息 / 前缀含未终态 run。"""


def _run_open_delta(event: SessionEvent) -> int:
    """事件对「未收口 run 计数」的增量（`#312`）。

    暂停计一次收口（`03 §3.4`：暂停态非终态，但该时点没有在途工具调用、也没有
    悬空 run），`run/resumed` 把同一个逻辑 run 重新计入未收口——fork 要的是
    「前缀里没有悬空的执行」，不是「没有暂停过」。
    """
    if event.type == RUN_STARTED or event.type == RUN_RESUMED:
        return 1
    if event.type in RUN_TERMINAL_TYPES or event.type == RUN_PAUSED:
        return -1
    return 0


def find_fork_boundaries(events: list[SessionEvent]) -> list[int]:
    """列出合法 fork 锚点（用户消息 seq，锚点语义：seed = [0, seq)）。

    规则：锚点处的 seed 前缀必须 run 完整——逐事件跟踪 run/started 与
    run 收口事件（`RUN_TERMINAL_TYPES`：completed / failed / interrupted，
    以及 `#312` 起的非终态 `run/paused`）的开合计数，计数为 0 时遇到的用户
    消息才是合法切点。暂停计一次收口：暂停中的执行没有在途工具调用、
    也没有悬空 run（`03 §3.4`），child 以它作前缀是完整的；该 run 若被
    `run/resumed` 接回则重新计入未收口（`_run_open`）。
    """
    boundaries: list[int] = []
    open_runs = 0
    for event in events:
        open_runs += _run_open_delta(event)
        if event.type == USER_MESSAGE and open_runs == 0:
            boundaries.append(event.seq)
    return boundaries


async def fork_session(
    store: JsonlSessionStore,
    meta_store: SessionMetaStore,
    parent_session_id: str,
    *,
    boundary_user_message_seq: int,
    child_session_id: str | None = None,
    agent_id: str = "default",
    workspace_registry: WorkspaceRegistry | None = None,
    summarizer: TailSummarizerProtocol | None = None,
    with_tail_summary: bool = True,
    budget_ledger: SessionBudgetLedger | None = None,
) -> Session:
    """从父会话的第 boundary_user_message_seq 条用户消息处 fork 出 child。

    锚点消息不进 seed；seed = 锚点之前的全部事件（父的 session/started 除
    外——child 写自己的身份事件）。全部校验先于任何落盘；start 之后的失败/
    取消不静默留孤儿：`fork/in-progress` 标记使现场可读，栈内补偿回收暂存
    与子工作区，进程 kill 的残余由启动扫描按标记回收（模块 docstring #555）。

    session 预算谱系（`#318`）：`budget_ledger` 给定时，读**父账行**的当前快照
    落进 `session/forked.data.budget_session`（谱系可溯），其余一字不动——
    fork = **新 SessionBudget 身份**（`03 §7`：child 是新 session_id ⇒ 新账行、
    新计数器，账从零开始；父行零写入：不迁移消耗、不 bump version、不写
    任何事件）。父行不存在（父会话还没跑过任何 step）⇒ 落 `snapshot: null`，
    "父没有账"与"父的账是空的"都如实可读。child 行由它自己的首个 run 惰性
    建出（与创建路径同一条规则）。
    """
    # 父会话只读加载（§7 父不可改：绝不能 Session.resume，那会追加 resumed）
    parent_events = store.read_events(parent_session_id)
    if not parent_events:
        raise ForkBoundaryError(
            f"Session '{parent_session_id}' 不存在或事件日志为空"
        )

    anchor = next(
        (
            e
            for e in parent_events
            if e.seq == boundary_user_message_seq
        ),
        None,
    )
    if anchor is None or anchor.type != USER_MESSAGE:
        available = find_fork_boundaries(parent_events)
        raise ForkBoundaryError(
            f"fork 边界 seq={boundary_user_message_seq} 不是父会话中的用户消息"
            f"（可用边界: {available}）"
        )

    # F18-A #282：`permission/changed`、`session/started` 与 #555 的 `fork/in-progress`
    # 同属**会话级状态**，不是对话历史——从 seed 里剔除（child 用 `started_data`
    # 重新声明自己的档）。若不剔除，child 的「最后一次 changed 胜」会取到 seed 里
    # **更早**的那条（锚点之前），覆盖掉下面从父派生的**当下** effective 档，造成
    # 「父 fork 后又改过档、child 却继承旧档」。意图标记同理：它描述的是**这一条
    # fork 线**的构建过程，孙代 seed 不该携带（孙的 fork 流程写自己的标记）。
    # #526：`permission/approval-granted|revoked`（会话级审批授权）与
    # `workflow/mode-changed`（工作流档）同属会话级状态——fork **不继承授权**
    # （#358 W-14 / F26：高风险权限不因 Fork 静默扩大；设计文档 §5 推荐默认值），
    # 工作流档亦不继承（child 从 NORMAL 起，用户可一键重进 plan）。
    boundary_events = [event for event in parent_events if event.seq < anchor.seq]
    seed = [
        event
        for event in boundary_events
        if event.type
        not in {
            SESSION_STARTED,
            PERMISSION_CHANGED,
            FORK_IN_PROGRESS,
            PERMISSION_GRANTED,
            PERMISSION_REVOKED,
            WORKFLOW_MODE_CHANGED,
        }
    ]
    _validate_run_complete(seed, parent_session_id)

    # 校验全部通过后才落盘：先建 child，再做 workspace 物理复制，再移植
    # seed 与 provenance/索引（copy 失败属基础设施故障，原样上抛）。
    # WS-1 #151 AC4：child 的 cwd **显式继承自 parent**（不靠"反正目录是复制来的"
    # 隐式成立）。父无 cwd（历史遗留）→ child 也不写该字段，父子的未分组状态一致。
    # F15 #234 + F18-A #282：会话级权限决策与 cwd 同级，显式继承——父是只读而 child
    # "未声明"的话，续聊会落到 workspace-write + 全自动批准（复制了父的 workspace 文件，
    # 却对写操作免审批）。继承的是父**当下生效**档（ADR-0041 §2 D7），父未声明 → 不写键。
    inherited: dict[str, object] = {}
    parent_mode = effective_permission_mode(boundary_events)
    if parent_mode is not None:
        inherited[SESSION_PERMISSION_MODE_KEY] = parent_mode.value
    parent_auto = effective_auto_approve(boundary_events)
    if parent_auto is not None:
        inherited[SESSION_AUTO_APPROVE_KEY] = parent_auto

    child = Session.start(
        store, agent_id=agent_id, session_id=child_session_id,
        workspace_registry=workspace_registry,
        started_data=inherited or None,
        cwd=session_cwd(parent_events),
    )
    # 校验全部通过后才落盘：先建 child，再做 workspace 物理复制，再移植
    # seed 与 provenance/索引（copy 失败属基础设施故障，原样上抛）。
    removed_state_event_ids = {
        event.event_id
        for event in boundary_events
        if event.type
        in {
            SESSION_STARTED,
            PERMISSION_CHANGED,
            FORK_IN_PROGRESS,
            PERMISSION_GRANTED,
            PERMISSION_REVOKED,
            WORKFLOW_MODE_CHANGED,
        }
    }
    event_id_remap: dict[str, str | None] = {
        event_id: None for event_id in removed_state_event_ids
    }
    fact_id_remap: dict[str, str | None] = {}
    parent_facts = derive_protected_facts(boundary_events)
    removed_authorizations = [
        fact
        for fact in parent_facts
        if fact.type == "authorization"
        and fact.source_event_id in removed_state_event_ids
    ]
    child_started = child.events[0]
    child_authorizations = [
        fact
        for fact in derive_protected_facts(child.events)
        if fact.type == "authorization" and fact.source_event_id == child_started.event_id
    ]
    if removed_authorizations:
        # Fork carries the permission value effective at its boundary in the child's
        # SESSION_STARTED event. Only the latest removed authorization maps to it;
        # older revoked/superseded links must not revoke the new boundary state.
        latest_parent_authorization = max(
            removed_authorizations, key=lambda fact: fact.source_seq
        )
        replacement = next(
            (
                fact
                for fact in child_authorizations
                if fact.value == latest_parent_authorization.value
            ),
            None,
        )
        for removed in removed_authorizations:
            fact_id_remap[removed.fact_id] = (
                replacement.fact_id
                if removed is latest_parent_authorization and replacement is not None
                else None
            )
        if replacement is not None:
            event_id_remap[latest_parent_authorization.source_event_id] = (
                child_started.event_id
            )
    # WS-1 #151 AC4：child 的 cwd **显式继承自 parent**（不靠"反正目录是复制来的"
    # 隐式成立）。父无 cwd（历史遗留）→ child 也不写该字段，父子的未分组状态一致。
    # F15 #234 + F18-A #282：会话级权限决策与 cwd 同级，显式继承——父是只读而 child
    # "未声明"的话，续聊会落到 workspace-write + 全自动批准（复制了父的 workspace 文件，
    # 却对写操作免审批）。继承的是父**当下生效**档（ADR-0041 §2 D7），父未声明 → 不写键。
    try:
        # #555：fork 意图标记——workspace 拷贝**之前**落 durable 事实（SQLite 提交
        # 日志同型）。W1 窗口（kill 于 started 之后、标记之前）留下的 child 与
        # 「合法的空会话」不可区分（空 workspace + 映射，无需补偿）；拷贝不可能
        # 在标记缺席时启动，启动扫描因此永远有判定依据。
        child.append(
            FORK_IN_PROGRESS,
            {
                "parent_session_id": parent_session_id,
                "boundary_user_message_seq": anchor.seq,
            },
            agent_id=agent_id,
        )
        if workspace_registry is not None:
            _copy_workspace(workspace_registry, parent_session_id, child)
        child.adopt_history(
            seed,
            event_id_remap=event_id_remap,
            fact_id_remap=fact_id_remap,
        )
        fork_point_seq = seed[-1].seq if seed else None

        # tail summary（决策 9）：锚点之后被放弃的路线压缩成一段上下文。
        # 恰好一次调用、无重试；失败降级不挂接，fork 照常（不变量 #21）。
        tail_summary: str | None = None
        tail_events = [e for e in parent_events if e.seq > anchor.seq]
        if summarizer is not None and with_tail_summary and tail_events:
            try:
                tail_summary = await summarizer.summarize(
                    render_tail_transcript(tail_events)
                )
            except Exception:
                logger.warning(
                    "tail summary 生成失败——降级不挂接（fork 照常完成）",
                    exc_info=True,
                )
                tail_summary = None

        forked_data: dict = {
            "parent_session_id": parent_session_id,
            "boundary_user_message_seq": anchor.seq,
            "fork_point_seq": fork_point_seq,
        }
        if tail_summary:
            forked_data["tail_summary"] = tail_summary
        # session 预算谱系（`#318`）：只读父账行 + 落快照引用；父零写入。
        if budget_ledger is not None:
            parent_key = session_budget_key(
                parent_events, session_id=parent_session_id
            )
            parent_snapshot = await budget_ledger.get_session_budget(parent_key)
            forked_data["budget_session"] = {
                "parent_budget_key": parent_key,
                "snapshot": (
                    parent_snapshot.as_projection()
                    if parent_snapshot is not None
                    else None
                ),
            }
        child.append(SESSION_FORKED, forked_data, agent_id=agent_id)
    except BaseException:
        # 阶段性补偿（#555）：session/forked 落盘前失败/取消（含 CancelledError，
        # 取消是审计列明的阶段之一）→ 本 child 永不可用，回收 harness 自建的
        # 暂存目录与默认形态子工作区；forked 已落盘的失败（如 meta upsert 前
        # 崩溃）child 已完整——保留 workspace。补偿以磁盘为准（append 中途
        # 抛错时内存标志不可信），且绝不掩盖原始错误。
        if workspace_registry is not None:
            try:
                completed = any(
                    event.type == SESSION_FORKED
                    for event in store.read_events(child.session_id)
                )
            except Exception:
                logger.warning(
                    "fork 失败补偿：child=%s 状态不可读，保守保留现场（留给启动扫描）",
                    child.session_id, exc_info=True,
                )
            else:
                if not completed:
                    _reclaim_failed_fork(workspace_registry, child.session_id)
        raise

    await meta_store.upsert(
        SessionMeta(
            session_id=child.session_id,
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            agent_id=agent_id,
            parent_session_id=parent_session_id,
            origin="fork",
            fork_point_seq=fork_point_seq,
        )
    )
    return child


def _copy_workspace(
    registry, parent_session_id: str, child: Session
) -> None:
    """copy-on-fork（ADR-0017 决策 5）：父 workspace 整目录复制为 child 的。

    物理策略独立于事件 fork（spec §7）。父无 workspace / 目录不存在 =
    child 空 workspace（降级）。Artifact 是全局 store 的内容寻址 ref——
    随事件 seed 原样可用，绝不复制（规格「Artifact Ref 按权限复用」）。

    #555 分阶段可见性：默认形态本地工作区（`<root>/workspaces/<child_id>`，
    注册表自建的 harness 自有路径）走 **暂存 + 同卷 rename 发布**——先把父
    目录整拷进 `<root>/.fork-tmp/<child_id>-<rand>`（拷贝中途崩溃只污染暂存，
    child 根目录保持空壳），成功后删空壳、单次 `os.replace`（同一 registry
    根 ⇒ 同卷）到位。**跨卷降级（#527）时发布为非原子直拷**：`os.replace`
    抛 EXDEV 则 `copytree(staging, child_root)`，此时观察者可能看到半份
    child_root；失败由内层清理 + `_reclaim_failed_fork` 兜底。
    其余形态（docker 容器内路径、命名 workspace 指向的
    用户目录）保持原位 copytree——rename 无法跨边界，其残余语义不变（由
    映射与事件层对账兜底）。拷贝失败 = 基础设施故障，暂存现场回收后原样
    上抛（fork 不带残缺快照继续）。
    """
    if not registry.exists(parent_session_id):
        return
    parent_root = registry.get(parent_session_id).workspace_root
    child_sandbox = child.sandbox
    if child_sandbox is None:
        return
    if not parent_root.is_dir():
        return
    child_root = registry.default_workspace_root(child.session_id)
    if Path(child_sandbox.workspace_root) != child_root:
        shutil.copytree(
            parent_root, child_sandbox.workspace_root, dirs_exist_ok=True
        )
        return
    staging_root = registry.fork_staging_root()
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / f"{child.session_id}-{uuid.uuid4().hex[:8]}"
    try:
        shutil.copytree(parent_root, staging, dirs_exist_ok=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # 发布：child 根目录预期是空壳（`ensure_started` 对 local 后端是 no-op，
    # 拷贝前无人写入），删壳后同卷 rename——观察者要么看到空目录、要么看到
    # 完整副本，不存在半份拷贝。跨卷时 os.replace 抛 EXDEV，降级为直接整拷
    #（纯整拷，不引入 CoW；#527 裁决 A）。
    shutil.rmtree(child_root, ignore_errors=True)
    try:
        os.replace(staging, child_root)
    except OSError as e:
        if e.errno != errno.EXDEV:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            shutil.copytree(staging, child_root)
        except BaseException:
            shutil.rmtree(child_root, ignore_errors=True)
            shutil.rmtree(staging, ignore_errors=True)
            raise
        shutil.rmtree(staging, ignore_errors=True)


def _reclaim_failed_fork(registry, child_session_id: str) -> None:
    """fork 失败（未落 session/forked）后的幂等补偿：只回收 harness 自建工件。

    - `<root>/.fork-tmp/<child_id>-*`（本 child 的暂存目录——带 uuid 后缀，
      glob 限定本 child，不影响同进程其他在途 fork）；
    - `<root>/workspaces/<child_id>/`（默认形态子工作区——构造规则白名单，
      与 `WorkspaceRegistry.discard_session_artifacts` 同源，用户目录永不在此前缀下）。

    child 的 JSONL（含 `fork/in-progress` 标记）与映射文件**保留**：它们是
    「fork 未完成」的 durable 可见性事实，启动扫描与续聊对账按它给诚实提示，
    不静默抹掉。每步失败只记日志——补偿绝不掩盖原始错误。
    """
    try:
        staging_root = registry.fork_staging_root()
        if staging_root.is_dir():
            for entry in staging_root.glob(f"{child_session_id}-*"):
                shutil.rmtree(entry, ignore_errors=True)
    except Exception:
        logger.warning(
            "fork 补偿：child=%s 暂存目录回收失败", child_session_id, exc_info=True
        )
    try:
        default_workspace = registry.default_workspace_root(child_session_id)
        if default_workspace.is_dir():
            shutil.rmtree(default_workspace, ignore_errors=True)
    except Exception:
        logger.warning(
            "fork 补偿：child=%s 子工作区回收失败", child_session_id, exc_info=True
        )


def _validate_run_complete(
    seed: list[SessionEvent], parent_session_id: str
) -> None:
    open_runs = 0
    for event in seed:
        open_runs += _run_open_delta(event)
        if open_runs < 0:
            raise ForkBoundaryError(
                f"父会话 '{parent_session_id}' 前缀 run 事件序非法（负计数）"
            )
    if open_runs > 0:
        raise ForkBoundaryError(
            f"父会话 '{parent_session_id}' 前缀以未终态 run 结尾"
            "（child 不允许以悬空 run 开头）"
        )
