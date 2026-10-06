"""W-05（#349）：项目可见进度文件——事件投影、写前脱敏、原子写。

**定位**：progress.md 是 SessionEvent 流的**确定性投影**（与 UI/messages 同源
读法，spec 03 §4/§6），不是第二套任务真相——它不新增事件类型、不写 Memory V2、
不做 UI 编辑/Fork/自动提交；系统绝不 ``git add``（文件在 ``git status`` 里以
未跟踪形态天然可见）。

**文件契约**（票面）：``<项目根>/agent-progress/<session-id>/progress.md`` 每
Session 独立；固定头部字段 schema_version / session_id / parent_session_id +
fork_point_seq（可空）/ source_event_seq / generated_at，固定节：原目标、约束
与授权、验收项与状态、已验证里程碑、决策、失败尝试与坑点、未决 Operation、
证据 refs、阻塞与下一步。保留精确 ID/金额/用户禁令原文字面与来源 seq；**绝不**
写凭证值、Cookie、``.env`` 值、私密原始 Tool 输出（写前脱敏；无法安全呈现的
段整段阻止并标缺项）；大原文只留 Artifact ref。

**原子写**：同目录临时文件（``mkstemp``）+ flush + fsync + ``os.replace`` 原子
替换——写入中 kill/断电/盘满/锁定都不留半文件，旧版或新版完整可读；上一版本
保留在 ``progress.prev.md``，``progress.meta.json`` 记录 source seq/hash（含
上一版），重复更新幂等（同 source_event_seq + 同内容哈希 ⇒ 跳过）。目录 fsync
仅在平台支持处执行（Windows 目录句柄不支持 fsync，安静跳过——数据完整性由
``os.replace`` 的原子性保证）。

**Windows 只读目录说明**：Windows 的目录只读属性不能真正阻止创建文件（与
POSIX 语义不同），故"只读目录"故障面以文件级只读（read-only target）与目标
被占用（open handle）两类真实可复现形态钉住，均显式失败且旧文件原样。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_harness.session.derive import ProtectedFact, derive_protected_facts
from agent_harness.session.event import (
    ARTIFACT_CREATED,
    ARTIFACT_EXTERNALIZED,
    CONTEXT_COMPACTION_FAILED,
    GUARD_STUCK,
    OPERATION_RECONCILE_REQUIRED,
    OPERATION_RECONCILED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RESUMED,
    RUN_STARTED,
    RUN_TERMINAL_TYPES,
    SESSION_FORKED,
    TASK_ACCEPTANCE_RELEASED,
    TASK_ACCEPTED,
)
from agent_harness.session.plan import derive_plan
from agent_harness.session.task import derive_task_state

logger = logging.getLogger("agent_harness.session.progress")

PROGRESS_SCHEMA_VERSION = "1"
# 存储布局契约（票面 <项目根>/agent-progress/<session-id>/）：公开常量，
# web 工作区浏览面（workspace_files）据同一名字过滤这份 harness 内部状态。
PROGRESS_DIRNAME = "agent-progress"
_MD_NAME = "progress.md"
_META_NAME = "progress.meta.json"
_PREV_NAME = "progress.prev.md"
_TMP_PREFIX = _MD_NAME + "."
_TMP_SUFFIX = ".tmp"
_END_MARKER = "<!-- agent-progress:end -->"
#: 外部编辑守卫的"现有正文不可读"哨兵（与"文件缺失"的 None 区分）。
_UNREADABLE_BODY = object()
# 写锁文件（#660，协议形态 Port 自 instance_lock.py L101-124）。命名必须避开
# `_TMP_PREFIX`（"progress.md.*"）清理 glob——否则下一次写入会误删外部持有的锁文件，
# 架空整个协议（tests/session/test_progress_file.py 有回归钉）。
_LOCK_NAME = ".progress.md.lock"
# Windows 区间锁偏移必须远离载荷区（msvcrt 区间锁是 mandatory 的，锁 byte 0 会让
# 锁文件内容再也读不出来）；POSIX 用 flock 整文件 advisory，无此问题。
_WIN_LOCK_OFFSET = 1 << 20

_MISSING = "（缺项：无任务定义）"
_BLOCKED = "（该段包含无法安全呈现的内容，已整段省略——缺项）"
_REDACTED = "<已脱敏>"
_NONE_LINE = "（无）"


# ── 路径 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProgressPaths:
    """一个 Session 的进度文件四件套（同目录，原子替换安全）。"""

    directory: Path
    markdown: Path
    meta: Path
    previous: Path


def progress_paths(root: Path | str, session_id: str) -> ProgressPaths:
    directory = Path(root) / PROGRESS_DIRNAME / session_id
    return ProgressPaths(
        directory=directory,
        markdown=directory / _MD_NAME,
        meta=directory / _META_NAME,
        previous=directory / _PREV_NAME,
    )


# ── 投影 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProgressDocument:
    """进度文件的结构化投影（纯数据，渲染前不落任何字符串拼接）。"""

    schema_version: str
    session_id: str
    parent_session_id: str | None
    fork_point_seq: int | None
    source_event_seq: int
    goal: str | None
    read_write_intent: str | None
    constraints: tuple[dict[str, Any], ...]
    acceptance: dict[str, Any]
    milestones: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    failures: tuple[dict[str, Any], ...]
    pending_operations: tuple[dict[str, Any], ...]
    evidence: tuple[dict[str, Any], ...]
    blockers: tuple[dict[str, Any], ...]


def _fact_value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _scan_run_lifecycle(
    events,
) -> tuple[tuple[dict, ...], tuple[dict, ...], tuple[dict, ...]]:
    """单遍扫描 run 生命周期 →（里程碑 completed、失败 failed、阻塞 paused/open）。

    paused 是非终态（spec 03 §3.4）：run 暂停后可同 run 恢复（run/resumed 回到
    open），终态（completed/failed/interrupted）移出阻塞表。
    """
    milestones: list[dict] = []
    failures: list[dict] = []
    state: dict[str, str] = {}  # run_id -> "open" | "paused"
    paused_reason: dict[str, Any] = {}
    for event in events:
        run_id = event.run_id
        data = event.data if isinstance(event.data, dict) else {}
        if event.type == RUN_STARTED and run_id is not None:
            state[run_id] = "open"
        elif event.type == RUN_PAUSED and run_id is not None:
            state[run_id] = "paused"
            paused_reason[run_id] = data.get("reason")
        elif event.type == RUN_RESUMED and run_id is not None:
            state[run_id] = "open"
            paused_reason.pop(run_id, None)
        elif event.type == RUN_COMPLETED and run_id is not None:
            state.pop(run_id, None)
            paused_reason.pop(run_id, None)
            milestones.append({"kind": "run_completed", "run_id": run_id})
        elif event.type in RUN_TERMINAL_TYPES and run_id is not None:
            state.pop(run_id, None)
            paused_reason.pop(run_id, None)
            if event.type == RUN_FAILED:
                failures.append({
                    "kind": "run_failed",
                    "run_id": run_id,
                    "reason": data.get("reason"),
                })
    blockers = [
        {"kind": "run_paused", "run_id": run_id, "reason": paused_reason.get(run_id)}
        for run_id in sorted(r for r, s in state.items() if s == "paused")
    ] + [
        {"kind": "run_open", "run_id": run_id}
        for run_id in sorted(r for r, s in state.items() if s == "open")
    ]
    return tuple(milestones), tuple(failures), tuple(blockers)


def derive_progress_document(events, *, session_id: str) -> ProgressDocument:
    """事件流 → 进度文档（纯函数；与 task/plan/protected-facts 投影同源）。"""
    task_state = derive_task_state(events)
    plan = derive_plan(events)

    constraints: list[dict] = []
    if task_state.read_write_intent:
        constraints.append({
            "kind": "read_write_intent",
            "value": task_state.read_write_intent,
            "source_seq": 0,
        })
    facts: list[ProtectedFact] = derive_protected_facts(events)
    for fact in facts:
        if fact.status != "active":
            continue
        constraints.append({
            "kind": "protected_fact",
            "fact_type": fact.type,
            "value": _fact_value_text(fact.value),
            "source_seq": fact.source_seq,
            "fact_id": fact.fact_id,
        })

    criteria: list[dict] = []
    for item in task_state.criteria:
        entry = task_state.verification.get(item.item_id)
        criteria.append({
            "item_id": item.item_id,
            "text": item.text,
            "origin": item.origin,
            "confirmed": item.confirmed,
            "verification_value": entry.value if entry else None,
            "evidence": entry.evidence if entry else None,
        })
    acceptance_section: dict[str, Any] = {
        "criteria": criteria,
        "acceptance": (
            {"decision": task_state.acceptance.decision,
             "reason": task_state.acceptance.reason}
            if task_state.acceptance else None
        ),
        "version": task_state.version,
    }

    run_milestones, run_failures, blockers = _scan_run_lifecycle(events)

    milestones: list[dict] = [
        {"kind": "verification_passed", "item_id": item.item_id, "evidence": entry.evidence}
        for item, entry in (
            (item, task_state.verification.get(item.item_id))
            for item in task_state.criteria
        )
        if entry is not None and entry.value == "passed"
    ] + [
        {"kind": "plan_step_completed", "item_id": item.id, "content": item.content}
        for item in plan.items if item.status == "completed"
    ] + list(run_milestones)

    decisions: list[dict] = []
    failures: list[dict] = list(run_failures)
    pending: dict[str, dict] = {}
    evidence: list[dict] = []
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        if event.type == TASK_ACCEPTED:
            decisions.append({
                "kind": "accepted",
                "decision": data.get("decision"),
                "reason": data.get("reason"),
                "source_seq": event.seq,
            })
        elif event.type == TASK_ACCEPTANCE_RELEASED:
            decisions.append({
                "kind": "released",
                "reason": data.get("reason"),
                "source_seq": event.seq,
            })
        elif event.type == CONTEXT_COMPACTION_FAILED:
            failures.append({
                "kind": "compaction_failed",
                "error_class": data.get("error_class"),
                "source_seq": event.seq,
            })
        elif event.type == GUARD_STUCK:
            failures.append({
                "kind": "guard_stuck",
                "level": data.get("level"),
                "source_seq": event.seq,
            })
        elif event.type == OPERATION_RECONCILE_REQUIRED:
            tool_call_id = data.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id not in pending:
                pending[tool_call_id] = {
                    "tool_call_id": tool_call_id,
                    "tool_name": data.get("tool_name"),
                    "source_seq": event.seq,
                }
        elif event.type == OPERATION_RECONCILED:
            pending.pop(data.get("tool_call_id"), None)
        elif event.type in (ARTIFACT_CREATED, ARTIFACT_EXTERNALIZED):
            # 生产 payload 形状（tooling/overflow.py / multiagent/tools.py）：
            # artifact_id/session_id/source_tool/tool_call_id/size/mime_type——
            # artifact_id 即 read_artifact 的 ref，name/ref 键生产者不写。
            evidence.append({
                "artifact_id": data.get("artifact_id"),
                "size": data.get("size"),
                "source_tool": data.get("source_tool"),
                "source_seq": event.seq,
            })

    parent_session_id: str | None = None
    fork_point_seq: int | None = None
    for event in events:
        if event.type == SESSION_FORKED:
            data = event.data if isinstance(event.data, dict) else {}
            parent = data.get("parent_session_id")
            point = data.get("fork_point_seq")
            parent_session_id = parent if isinstance(parent, str) else None
            fork_point_seq = point if isinstance(point, int) and not isinstance(point, bool) else None

    return ProgressDocument(
        schema_version=PROGRESS_SCHEMA_VERSION,
        session_id=session_id,
        parent_session_id=parent_session_id,
        fork_point_seq=fork_point_seq,
        source_event_seq=events[-1].seq if events else 0,
        goal=task_state.task_text or None,
        read_write_intent=task_state.read_write_intent,
        constraints=tuple(constraints),
        acceptance=acceptance_section,
        milestones=tuple(milestones),
        decisions=tuple(decisions),
        failures=tuple(failures),
        pending_operations=tuple(pending.values()),
        evidence=tuple(evidence),
        blockers=blockers,
    )


# ── 写前脱敏（票面：凭证值 / Cookie / .env 值 / 私钥绝不入文） ───────────────


_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)
_ENV_ASSIGN_RE = re.compile(
    r"(?im)^(\s*[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|CREDENTIAL)"
    r"[A-Za-z0-9_]*\s*=\s*)\S.*$"
)
_COOKIE_RE = re.compile(r"(?i)(cookie\s*:\s*)([^；;\n]*)")
_AUTH_RE = re.compile(r"(?im)^(authorization\s*:\s*)(\S.*)$")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_PREFIXES = ("sk-", "ghp_", "gho_", "github_pat_", "xoxb-", "xoxp-", "AKIA")
_TOKEN_RE = re.compile(
    "|".join(re.escape(prefix) + r"[A-Za-z0-9._~/+=-]{6,}" for prefix in _TOKEN_PREFIXES)
)


def _mask_token(match: re.Match) -> str:
    token = match.group(0)
    for prefix in _TOKEN_PREFIXES:
        if token.startswith(prefix):
            return prefix + _REDACTED
    return _REDACTED


def sanitize_text(text: str) -> str | None:
    """写字段的唯一入口：命中私钥块 ⇒ 返回 ``None``（整段阻止，标缺项）；
    其余命中模式就地脱敏（键名/前缀保留，值替换）。"""
    if not isinstance(text, str):
        return _fact_value_text(text)
    if _PEM_RE.search(text):
        return None
    redacted = _ENV_ASSIGN_RE.sub(r"\1" + _REDACTED, text)
    redacted = _COOKIE_RE.sub(r"\1" + _REDACTED, redacted)
    redacted = _AUTH_RE.sub(r"\1" + _REDACTED, redacted)
    redacted = _BEARER_RE.sub("Bearer " + _REDACTED, redacted)
    redacted = _TOKEN_RE.sub(_mask_token, redacted)
    return redacted


def _render_line(prefix: str, value: Any) -> str:
    safe = sanitize_text(value) if isinstance(value, str) else _fact_value_text(value)
    if safe is None:
        return f"{prefix}{_BLOCKED}"
    return f"{prefix}{safe}"


# ── 渲染 ───────────────────────────────────────────────────────────────────


def _bullets(items) -> list[str]:
    return [f"- {item}" for item in items] if items else [f"- {_NONE_LINE}"]


def _v(value: Any) -> str:
    """任何进正文的动态字符串的唯一出口（脱敏或整段缺项）。"""
    if value is None:
        return "-"
    safe = sanitize_text(value) if isinstance(value, str) else _fact_value_text(value)
    return _BLOCKED if safe is None else safe


def render_progress_markdown(doc: ProgressDocument, *, generated_at: str) -> str:
    """投影 → markdown（确定性：同文档 + 同 generated_at ⇒ 字节相同）。"""
    lines: list[str] = ["# 会话进度（progress.md）", ""]
    lines.append(f"- schema_version: {doc.schema_version}")
    lines.append(f"- session_id: {doc.session_id}")
    lines.append(f"- parent_session_id: {_v(doc.parent_session_id)}")
    lines.append(f"- fork_point_seq: {doc.fork_point_seq if doc.fork_point_seq is not None else '-'}")
    lines.append(f"- source_event_seq: {doc.source_event_seq}")
    lines.append(f"- generated_at: {generated_at}")
    lines.append("")

    lines.append("## 原目标")
    lines.append(f"- {_v(doc.goal)}" if doc.goal else f"- {_MISSING}")
    lines.append("")

    lines.append("## 约束与授权")
    if doc.read_write_intent:
        lines.append(_render_line("- 读写意图：", doc.read_write_intent))
    for constraint in doc.constraints:
        if constraint["kind"] == "protected_fact":
            lines.append(_render_line(
                f"- [保护事实/{constraint.get('fact_type', '?')}] "
                f"（来源 seq {constraint['source_seq']}）",
                constraint["value"],
            ))
    if not doc.constraints:
        lines.append(f"- {_NONE_LINE}")
    lines.append("")

    lines.append("## 验收项与状态")
    acceptance = doc.acceptance
    for item in acceptance["criteria"]:
        verification = item["verification_value"] or "未验证"
        evidence = f"（证据：{_v(item['evidence'])}）" if item["evidence"] else ""
        lines.append(
            f"- [{_v(item['item_id'])}] {_v(item['text'])}"
            f"（origin={_v(item['origin'])}，"
            f"confirmed={item['confirmed']}）— 验证：{verification}{evidence}"
        )
    if not acceptance["criteria"]:
        lines.append(f"- {_NONE_LINE}")
    if acceptance["acceptance"]:
        acc = acceptance["acceptance"]
        reason = f"，原因：{_v(acc['reason'])}" if acc["reason"] else ""
        lines.append(
            f"- 接受状态：{_v(acc['decision'])}{reason}（version {acceptance['version']}）"
        )
    else:
        lines.append(f"- 接受状态：未接受（version {acceptance['version']}）")
    lines.append("")

    lines.append("## 已验证里程碑")
    for milestone in doc.milestones:
        if milestone["kind"] == "verification_passed":
            evidence = f"（证据：{_v(milestone['evidence'])}）" if milestone["evidence"] else ""
            lines.append(f"- 验证通过 [{_v(milestone['item_id'])}]{evidence}")
        elif milestone["kind"] == "plan_step_completed":
            lines.append(
                f"- 清单完成 [{_v(milestone['item_id'])}] {_v(milestone['content'])}"
            )
        elif milestone["kind"] == "run_completed":
            lines.append(f"- Run 完成 run_id={_v(milestone['run_id'])}")
    if not doc.milestones:
        lines.append(f"- {_NONE_LINE}")
    lines.append("")

    lines.append("## 决策")
    for decision in doc.decisions:
        if decision["kind"] == "accepted":
            reason = f"，原因：{_v(decision['reason'])}" if decision["reason"] else ""
            lines.append(
                f"- 接受裁决：{_v(decision['decision'])}{reason}"
                f"（来源 seq {decision['source_seq']}）"
            )
        else:
            reason = f"：{_v(decision['reason'])}" if decision["reason"] else ""
            lines.append(f"- 释放接受{reason}（来源 seq {decision['source_seq']}）")
    if not doc.decisions:
        lines.append(f"- {_NONE_LINE}")
    lines.append("")

    lines.append("## 失败尝试与坑点")
    for failure in doc.failures:
        if failure["kind"] == "run_failed":
            lines.append(
                f"- Run 失败 run_id={_v(failure['run_id'])}，原因：{_v(failure['reason'])}"
            )
        elif failure["kind"] == "compaction_failed":
            lines.append(f"- 压缩失败 error_class={_v(failure['error_class'])}"
                         f"（来源 seq {failure['source_seq']}）")
        elif failure["kind"] == "guard_stuck":
            lines.append(f"- 循环护栏 level={_v(failure['level'])}"
                         f"（来源 seq {failure['source_seq']}）")
    if not doc.failures:
        lines.append(f"- {_NONE_LINE}")
    lines.append("")

    lines.append("## 未决 Operation")
    for operation in doc.pending_operations:
        lines.append(
            f"- tool_call_id={_v(operation['tool_call_id'])} "
            f"tool={_v(operation['tool_name'])}"
            f"（来源 seq {operation['source_seq']}，NEED_RECONCILE 待对账）"
        )
    if not doc.pending_operations:
        lines.append(f"- {_NONE_LINE}")
    lines.append("")

    lines.append("## 证据 refs")
    for artifact in doc.evidence:
        parts = [f"来源 seq {artifact['source_seq']}"]
        if artifact["artifact_id"]:
            parts.append(f"artifact_id={_v(artifact['artifact_id'])}")
        if artifact["size"] is not None:
            parts.append(f"size={artifact['size']}")
        if artifact["source_tool"]:
            parts.append(f"tool={_v(artifact['source_tool'])}")
        lines.append("- " + "，".join(parts))
    if not doc.evidence:
        lines.append(f"- {_NONE_LINE}")
    lines.append("")

    lines.append("## 阻塞与下一步")
    for blocker in doc.blockers:
        if blocker["kind"] == "run_paused":
            lines.append(
                f"- Run 暂停 run_id={_v(blocker['run_id'])}，原因：{_v(blocker['reason'])}"
            )
        else:
            lines.append(f"- Run 在途 run_id={_v(blocker['run_id'])}")
    if not doc.blockers:
        lines.append(f"- {_NONE_LINE}")
    lines.append("")
    lines.append(_END_MARKER)
    lines.append("")
    return "\n".join(lines)


# ── 原子写 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProgressWriteOutcome:
    """写结果（失败 = 明确 error_kind/reason，绝不静默吞；不回显文件内容）。"""

    ok: bool
    skipped: bool
    path: Path
    source_event_seq: int
    error_kind: str | None = None
    reason: str | None = None


def _normalize_generated_at(body: str) -> str:
    """generated_at 行归一成占位（digest 与 W-06 对账共用的唯一归一化）。"""
    return re.sub(r"(?m)^- generated_at: .*$", "- generated_at: -", body)


def progress_content_digest(body: str) -> str:
    """进度文件内容哈希（幂等跳过与 W-06 reader 对账的**公共契约**）。

    ``generated_at`` 行归一成占位后取 sha256——同源事件 ⇒ 同哈希，与写入时刻
    无关。任何消费方判断"文件是否与事件流一致"MUST 复用本函数，不得自算
    第二种归一化（两套哈希 = 第二真相）。
    """
    return hashlib.sha256(
        _normalize_generated_at(body).encode("utf-8")
    ).hexdigest()


def _read_meta(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_meta_atomic(meta_path: Path, payload: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=_META_NAME + ".", suffix=_TMP_SUFFIX,
                                    dir=meta_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, meta_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _cleanup_stale_tmp(directory: Path) -> None:
    # 同前缀孤儿临时文件全清（历次版本的后缀形态可能不同；本目录为 writer 专用，
    # glob `progress.md.*` 不可能命中 progress.md / progress.meta.json / progress.prev.md）
    for stale in directory.glob(_TMP_PREFIX + "*"):
        try:
            stale.unlink()
        except OSError:
            pass


def _fsync_directory(directory: Path) -> None:
    """平台可行处 fsync 目录（Windows 目录句柄不支持，安静跳过）。"""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _take_write_lock(fd: int) -> None:
    """非阻塞取 OS 级排他写锁；已被占用时抛 OSError。

    协议形态 Port 自 ``instance_lock.py::_take_os_lock``（#660）：POSIX
    ``fcntl.flock(LOCK_EX | LOCK_NB)`` / Windows ``msvcrt.locking(LK_NBLCK)``
    （1 字节区间锁，偏移 ``_WIN_LOCK_OFFSET`` 远离载荷区）。advisory 边界同
    instance_lock.py：只约束遵守本协议的进程，进程崩溃/退出由 OS 自动释放，
    无陈旧锁路径。同进程内独立 ``open()`` 的 fd 属不同 file description，
    flock 互斥同样成立（测试据此在同进程内模拟外部锁定）。
    """
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_write_lock(fd: int) -> None:
    """显式解锁（close 也会释放，双保险）。"""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _acquire_write_lock(directory: Path) -> int | None:
    """写前取 progress 目录写锁；被占用返回 None（fail-fast，不重试不等待）。"""
    fd = os.open(directory / _LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _take_write_lock(fd)
    except OSError:
        os.close(fd)
        return None
    return fd


def write_progress_file(
    root: Path | str,
    session_id: str,
    events,
    *,
    now: str | None = None,
    overwrite_external_edit: bool = False,
) -> ProgressWriteOutcome:
    """派生 → 渲染 → 脱敏 → 原子落盘；幂等；失败明确报错不谎报最新。

    写前对同目录锁文件（``.progress.md.lock``）取非阻塞 OS 排他写锁（#660，
    advisory，被占用 ⇒ ``fail("locked")``，不重试不等待）；目标只读先行检查
    明确失败。锁进程退出由 OS 兜底释放。
    只派生与写文件，不追加任何事件、不调 git；调用方负责在既有触发点
    （创建/交付/cancel/run 终态等）以 best-effort 方式调用。

    **外部编辑守卫（W-06，#350）**：现有文件内容既不等于当前事件投影、也不是
    服务端最后一次写入（meta hash）⇒ 判外部编辑，``fail("external_edit")``
    拒绝覆写——用户在"丢弃手改"或"确认为新用户指令"（``resolve`` 路径，
    ``overwrite_external_edit=True``）之前，真相与文件都不动，绝不把文件文本
    自动升级为授权/事实。文件缺失或只读到旧版本照常重建/更新。
    """
    paths = progress_paths(root, session_id)
    doc = derive_progress_document(events, session_id=session_id)
    stamp = now or datetime.now(UTC).isoformat(timespec="seconds")
    body = render_progress_markdown(doc, generated_at=stamp)
    digest = progress_content_digest(body)

    def fail(kind: str, reason: str) -> ProgressWriteOutcome:
        logger.warning("progress 写入失败（session=%s, kind=%s）：%s",
                       session_id, kind, reason)
        return ProgressWriteOutcome(ok=False, skipped=False, path=paths.markdown,
                                    source_event_seq=doc.source_event_seq,
                                    error_kind=kind, reason=reason)

    try:
        paths.directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return fail("env", f"进度目录不可创建：{exc.strerror or exc}")
    _cleanup_stale_tmp(paths.directory)

    old_meta = _read_meta(paths.meta)
    # 磁盘实况先读一次（W-06）：幂等跳过与外部编辑守卫共用同一份读数。
    # 读不了（权限/编码损坏）用哨兵标记，走 fail-closed 分支。
    disk_digest: str | None | object = None
    if paths.markdown.exists():
        try:
            disk_digest = _disk_body_digest(paths.markdown)
        except (OSError, UnicodeDecodeError):
            disk_digest = _UNREADABLE_BODY
    if (
        old_meta is not None
        and isinstance(disk_digest, str)
        and disk_digest == digest  # 磁盘正文就是当前投影（不是被人改过的旧版）
        and old_meta.get("source_event_seq") == doc.source_event_seq
        and old_meta.get("content_sha256") == digest
    ):
        return ProgressWriteOutcome(ok=True, skipped=True, path=paths.markdown,
                                    source_event_seq=doc.source_event_seq)

    # 只读前置检查（#660）：POSIX 的 rename(2) 不查目标文件权限位，覆写只读目标
    # 必须由 writer 主动检查（root 绕过权限位时 os.access 恒真 ⇒ 自动放行不误伤）。
    # Windows 只读位同样在此拦截；下方 PermissionError 路径保留为第二道防线。
    if paths.markdown.exists() and not os.access(paths.markdown, os.W_OK):
        return fail("locked", "目标文件只读，不可覆写")

    # 锁文件自身的 os.open 也可能抛 OSError（只读目录、目录中途被删），必须收敛为
    # 明确失败——此前同场景由 mkstemp 在 try 内接住，不能让本次改动退化成抛异常逃逸。
    try:
        lock_fd = _acquire_write_lock(paths.directory)
    except OSError as exc:
        return fail("env", f"写锁文件不可创建：{exc.strerror or exc}")
    if lock_fd is None:
        return fail("locked", "目标进度文件被另一写入方锁定（advisory 写锁）")

    try:
        # 外部编辑守卫（W-06）：锁内复判（disk_digest 在锁前已读一次，这里用
        # 同一读数；锁内复读会缩小竞态窗口但多一次 I/O——幂等跳过已在锁外
        # 依据同一读数做出，此处保持同一份事实）。判据与 verify_progress_file
        # 共用 _classify_disk_body：现有内容 ≠ 当前投影 且 ≠ 服务端最后一次
        # 写入（meta hash）⇒ 外部编辑；读不了同样 fail-closed，不盲覆写。
        # （置于 mkstemp 之前：守卫拒绝时不留临时文件。）
        if disk_digest is _UNREADABLE_BODY and not overwrite_external_edit:
            return fail(
                "external_edit",
                "现有进度文件不可读（权限或编码损坏），等待用户确认后再重建",
            )
        if (
            isinstance(disk_digest, str)
            and not overwrite_external_edit
            and _classify_disk_body(disk_digest, old_meta, digest) == "external"
        ):
            return fail(
                "external_edit",
                "进度文件在服务端最后一次写入之外被修改；用户确认丢弃"
                "或作为新用户指令前不覆写",
            )
        fd, tmp_name = tempfile.mkstemp(prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX,
                                        dir=paths.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            if paths.markdown.exists():
                with open(paths.markdown, "rb") as src, open(paths.previous, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
            os.replace(tmp_name, paths.markdown)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except PermissionError as exc:
        return fail("locked", f"目标被占用或只读：{exc.strerror or exc}")
    except OSError as exc:
        return fail("env", f"写入失败：{exc.strerror or exc}")
    finally:
        try:
            _release_write_lock(lock_fd)
            os.close(lock_fd)
        except OSError:
            pass
    _fsync_directory(paths.directory)

    new_meta = {
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "session_id": session_id,
        "source_event_seq": doc.source_event_seq,
        "content_sha256": digest,
        "generated_at": stamp,
        "previous": None
        if old_meta is None
        else {
            "source_event_seq": old_meta.get("source_event_seq"),
            "content_sha256": old_meta.get("content_sha256"),
        },
    }
    try:
        _write_meta_atomic(paths.meta, new_meta)
    except OSError as exc:
        return fail("env", f"正文已替换但 meta 落盘失败（下次写自动修复）：{exc.strerror or exc}")
    return ProgressWriteOutcome(ok=True, skipped=False, path=paths.markdown,
                                source_event_seq=doc.source_event_seq)


# ── W-06（#350）：磁盘重读对账（reader / compare）────────────────────────────
#
# 进度文件是投影不是真相（模块 docstring）；本节把"文件现在说了什么"与
# "SessionEvent 投影说该是什么"对账，给四个重读点（任务启动 / 服务重启 /
# 压缩后 / 交付前）与外部编辑冲突流一个确定状态。对账哈希**复用**
# ``progress_content_digest``（W-05 公共契约，禁第二套归一化）。


PROGRESS_VERIFY_OK = "ok"
PROGRESS_VERIFY_MISSING = "missing"
PROGRESS_VERIFY_STALE = "stale"
PROGRESS_VERIFY_FOREIGN_SESSION = "foreign_session"
PROGRESS_VERIFY_INVALID_SCHEMA = "invalid_schema"
PROGRESS_VERIFY_EXTERNALLY_EDITED = "externally_edited"
PROGRESS_VERIFY_UNREADABLE = "unreadable"

_HEADER_FIELD_RE = re.compile(r"^- ([a-z_]+): (.*)$")


def _disk_body_digest(path: Path) -> str | None:
    """读现有正文并算对账哈希；文件不存在返回 None；读不了原样抛
    （OSError / UnicodeDecodeError），由调用方决定 fail-closed 形态。"""
    try:
        body = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return progress_content_digest(body)


def _classify_disk_body(
    disk_digest: str | None, meta: dict | None, expected_digest: str,
) -> str:
    """现有内容三分类（writer 守卫与 verify 共用的唯一判据）。

    - ``current``：与当前事件投影逐字节一致（归一化后）；缺文件同 current
      （调用方在调用前已处理"缺文件"分支）；
    - ``server_stale``：等于服务端最后一次写入（meta hash）——只是事件前进了，
      正常更新路径；
    - ``external``：两者都不是——服务端写入之外被修改（或服务端从未写过它）。
    """
    if disk_digest is None or disk_digest == expected_digest:
        return "current"
    if meta is not None and disk_digest == meta.get("content_sha256"):
        return "server_stale"
    return "external"


def _parse_progress_body(body: str) -> tuple[dict[str, str], dict[str, str]]:
    """把渲染格式的正文拆成（头部字段, 节名 → 节文本）。容错：解析不了的
    形状只影响 diff 展示，不参与状态判定（状态判定只看哈希与头部字段）。"""
    header: dict[str, str] = {}
    sections: dict[str, str] = {}
    current: str | None = None
    bucket: list[str] = []
    for line in body.splitlines():
        match = _HEADER_FIELD_RE.match(line)
        if match is not None and current is None:
            header[match.group(1)] = match.group(2)
            continue
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(bucket).strip()
            current = line[3:].strip()
            bucket = []
            continue
        if current is not None:
            bucket.append(line)
    if current is not None:
        sections[current] = "\n".join(bucket).strip()
    return header, sections


def _diff_bodies(
    file_body: str, expected_body: str,
) -> tuple[ProgressFieldDiff, ...]:
    """字段级差异（文件 vs 投影）。actual 一律过 ``sanitize_text``——外部编辑
    可能塞进凭证内容，冲突展示面绝不能成为第二个泄漏通道。"""
    file_header, file_sections = _parse_progress_body(file_body)
    expected_header, expected_sections = _parse_progress_body(expected_body)
    diffs: list[ProgressFieldDiff] = []
    for key in sorted(set(file_header) | set(expected_header)):
        if key == "generated_at":
            continue  # 写入时刻不是投影事实，归一化后不参与对账
        expected = expected_header.get(key)
        actual = file_header.get(key)
        if expected != actual:
            diffs.append(ProgressFieldDiff(
                field=f"header.{key}", expected=expected, actual=actual,
            ))
    for name in sorted(set(file_sections) | set(expected_sections)):
        expected = expected_sections.get(name)
        actual = file_sections.get(name)
        if expected != actual:
            diffs.append(ProgressFieldDiff(
                field=f"section:{name}",
                expected=expected, actual=actual,
            ))
    sanitized = []
    for diff in diffs:
        expected_safe = sanitize_text(diff.expected)
        actual_safe = sanitize_text(diff.actual)
        sanitized.append(ProgressFieldDiff(
            field=diff.field,
            expected=expected_safe,
            actual=actual_safe,
        ))
    return tuple(sanitized)


@dataclass(frozen=True)
class ProgressFieldDiff:
    """一个字段/节的文件侧与投影侧差异（两侧文本均已脱敏）。"""

    field: str
    expected: str | None  # SessionEvent 投影（真相侧；行内已带（来源 seq N））
    actual: str | None    # 磁盘文件侧；无法安全呈现时为 None（整段省略）


def read_progress_file_version(
    root: Path | str, session_id: str
) -> dict[str, Any]:
    """读 progress.md 头部版本（``schema_version`` + ``source_event_seq``）。

    #357 W-13（契约 5 / R6）：恢复列表行的"进度文件版本"四要素之一——
    **纯读、零副作用**，与 ``verify_progress_file`` 同源解析（``_parse_progress_body``）
    但**不做**投影对账（恢复列表只回答"文件自报的版本"，不判新旧）。

    返回两种互斥形状：
    - 可读且头合法：``{"schema_version": str, "source_event_seq": int}``；
    - 缺失/不可读/头非法：``{"status": "missing" | "unreadable" |
      "invalid_schema", "reason": str}``——**如实标注，不伪造"最新"**。
    """
    paths = progress_paths(root, session_id)
    try:
        body = paths.markdown.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"status": "missing", "reason": "进度文件不存在"}
    except (OSError, UnicodeDecodeError) as exc:
        return {"status": "unreadable", "reason": str(exc)}
    header, _sections = _parse_progress_body(body)
    if header.get("schema_version") != PROGRESS_SCHEMA_VERSION:
        return {
            "status": "invalid_schema",
            "reason": f"schema_version 不匹配：{header.get('schema_version')!r}",
        }
    raw_seq = header.get("source_event_seq")
    try:
        file_seq = int(raw_seq) if raw_seq is not None else None
    except ValueError:
        file_seq = None
    if file_seq is None:
        return {
            "status": "invalid_schema",
            "reason": f"source_event_seq 缺失或非法：{raw_seq!r}",
        }
    return {"schema_version": PROGRESS_SCHEMA_VERSION, "source_event_seq": file_seq}


@dataclass(frozen=True)
class ProgressVerification:
    """verify_progress_file 的结果（确定状态机，见各 PROGRESS_VERIFY_* 常量）。"""

    status: str
    session_id: str
    expected_source_event_seq: int
    file_source_event_seq: int | None
    diffs: tuple[ProgressFieldDiff, ...]
    #: 证据 refs 里读不回的 artifact_id（仅 artifact_exists 提供时检查）
    unreadable_artifacts: tuple[str, ...]
    #: "performed"（做过可读回校验）| "skipped"（无 artifact 判据，未检查）
    artifact_check: str
    reason: str | None
    evidence_artifact_ids: tuple[str, ...] = ()

    @property
    def verifiable(self) -> bool:
        """票面口径：status=ok 且全部证据 refs 可读回，才算"进度文件可核对"。"""
        return self.status == PROGRESS_VERIFY_OK and not self.unreadable_artifacts


def verify_progress_file(
    root: Path | str,
    session_id: str,
    events,
    *,
    artifact_exists: Callable[[str], bool] | None = None,
) -> ProgressVerification:
    """从磁盘重读 progress.md，与 SessionEvent 投影对账（纯读，零副作用）。

    状态判定顺序（互斥，先命中先返回）：
    文件缺失 → ``missing``；读不了 → ``unreadable``；schema_version 头不匹配
    → ``invalid_schema``；session_id 头不匹配 → ``foreign_session``；内容
    ≠ 投影且 ≠ 服务端最后一次写入 → ``externally_edited``（附字段级差异）；
    文件落后于当前投影 → ``stale``；一致 → ``ok``。
    ``artifact_exists`` 提供时对证据 refs 做可读回校验（票面：仅允许引用可读回
    的 Artifact；读不到 ⇒ verifiable=False，展示"进度文件不可核对"）。
    """
    paths = progress_paths(root, session_id)
    doc = derive_progress_document(events, session_id=session_id)
    expected_body = render_progress_markdown(doc, generated_at="-")
    evidence_ids = tuple(
        artifact["artifact_id"] for artifact in doc.evidence
        if artifact.get("artifact_id")
    )

    def result(status: str, *, file_seq: int | None = None,
               diffs: tuple[ProgressFieldDiff, ...] = (),
               reason: str | None = None) -> ProgressVerification:
        return ProgressVerification(
            status=status, session_id=session_id,
            expected_source_event_seq=doc.source_event_seq,
            file_source_event_seq=file_seq, diffs=diffs,
            unreadable_artifacts=(), artifact_check="skipped",
            reason=reason, evidence_artifact_ids=evidence_ids,
        )

    try:
        body = paths.markdown.read_text(encoding="utf-8")
    except FileNotFoundError:
        return result(PROGRESS_VERIFY_MISSING, reason="进度文件不存在")
    except (OSError, UnicodeDecodeError) as exc:
        return result(PROGRESS_VERIFY_UNREADABLE, reason=str(exc))

    header, _sections = _parse_progress_body(body)
    if header.get("schema_version") != PROGRESS_SCHEMA_VERSION:
        return result(
            PROGRESS_VERIFY_INVALID_SCHEMA,
            reason=f"schema_version 不匹配：{header.get('schema_version')!r}",
        )
    if header.get("session_id") != session_id:
        return result(
            PROGRESS_VERIFY_FOREIGN_SESSION,
            reason=f"文件头部 session_id={header.get('session_id')!r}，"
                   f"期望 {session_id!r}",
        )
    raw_seq = header.get("source_event_seq")
    file_seq: int | None
    try:
        file_seq = int(raw_seq) if raw_seq is not None else None
    except ValueError:
        file_seq = None
    if file_seq is None:
        return result(
            PROGRESS_VERIFY_INVALID_SCHEMA,
            reason=f"source_event_seq 不可解析：{raw_seq!r}",
        )

    disk_digest = progress_content_digest(body)
    classification = _classify_disk_body(
        disk_digest, _read_meta(paths.meta),
        progress_content_digest(expected_body),
    )
    if classification == "external":
        return result(
            PROGRESS_VERIFY_EXTERNALLY_EDITED, file_seq=file_seq,
            diffs=_diff_bodies(body, expected_body),
            reason="内容既不等于当前投影也不是服务端最后一次写入（外部编辑）",
        )
    if file_seq < doc.source_event_seq:
        return result(
            PROGRESS_VERIFY_STALE, file_seq=file_seq,
            reason=f"文件 source_event_seq={file_seq} 落后于当前投影 "
                   f"{doc.source_event_seq}",
        )
    if file_seq > doc.source_event_seq:
        # 文件自报比事件流还新：伪造的 seq，按外部编辑处理
        return result(
            PROGRESS_VERIFY_EXTERNALLY_EDITED, file_seq=file_seq,
            diffs=_diff_bodies(body, expected_body),
            reason="文件 source_event_seq 超前于事件流（伪造 seq）",
        )
    if classification == "server_stale":
        # seq 相同但内容是旧版本：meta 与正文不同源，按外部编辑处理
        return result(
            PROGRESS_VERIFY_EXTERNALLY_EDITED, file_seq=file_seq,
            diffs=_diff_bodies(body, expected_body),
            reason="内容与 meta 同源但与当前投影不一致",
        )

    verification = result(PROGRESS_VERIFY_OK, file_seq=file_seq)
    if artifact_exists is not None and evidence_ids:
        unreadable = tuple(
            artifact_id for artifact_id in evidence_ids
            if not artifact_exists(artifact_id)
        )
        verification = ProgressVerification(
            status=verification.status,
            session_id=verification.session_id,
            expected_source_event_seq=verification.expected_source_event_seq,
            file_source_event_seq=verification.file_source_event_seq,
            diffs=verification.diffs,
            unreadable_artifacts=unreadable,
            artifact_check="performed",
            reason=(
                "部分证据 refs 不可读回：进度文件不可核对"
                if unreadable else None
            ),
            evidence_artifact_ids=evidence_ids,
        )
    return verification


def render_progress_brief_lines(doc: ProgressDocument) -> list[str]:
    """W-06 注入块内容行：原目标 + 接受状态 + 阻塞与下一步 + 待对账。

    全部动态文本走 ``_v`` 唯一脱敏出口（与正文渲染同一条纪律）。"""
    lines = [f"原目标：{_v(doc.goal)}" if doc.goal else f"原目标：{_MISSING}"]
    acceptance = doc.acceptance
    if acceptance["acceptance"]:
        lines.append(
            f"接受状态：{_v(acceptance['acceptance']['decision'])}"
            f"（version {acceptance['version']}）"
        )
    else:
        lines.append(f"接受状态：未接受（version {acceptance['version']}）")
    for blocker in doc.blockers:
        if blocker["kind"] == "run_paused":
            lines.append(
                f"阻塞：Run 暂停 run_id={_v(blocker['run_id'])}，"
                f"原因：{_v(blocker['reason'])}"
            )
        else:
            lines.append(f"在途：Run run_id={_v(blocker['run_id'])}")
    for operation in doc.pending_operations:
        lines.append(
            f"待对账：tool={_v(operation['tool_name'])}"
            f"（来源 seq {operation['source_seq']}）"
        )
    return lines


def render_external_edit_instruction(verification: ProgressVerification) -> str:
    """把外部编辑差异渲染成"用户确认为新用户指令"的消息文本（confirm 路径）。

    差异文本已在 ``_diff_bodies`` 脱敏；这里只做事实性搬运，绝不替用户措辞、
    绝不把文件文本升格成授权。"""
    lines = ["（用户确认）进度文件被手动修改，以下手改内容作为新用户指令："]
    for diff in verification.diffs:
        actual = diff.actual if diff.actual is not None else _BLOCKED
        lines.append(f"- {diff.field} 改为：{actual}")
    if len(lines) == 1:
        lines.append(f"- （无字段级差异；{_MISSING}）")
    return "\n".join(lines)
