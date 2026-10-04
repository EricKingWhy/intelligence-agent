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
import os
import re
import shutil
import tempfile
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

logger = __import__("logging").getLogger("agent_harness.session.progress")

PROGRESS_SCHEMA_VERSION = "1"
_PROGRESS_DIRNAME = "agent-progress"
_MD_NAME = "progress.md"
_META_NAME = "progress.meta.json"
_PREV_NAME = "progress.prev.md"
_TMP_PREFIX = _MD_NAME + "."
_TMP_SUFFIX = ".tmp"
_END_MARKER = "<!-- agent-progress:end -->"

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
    directory = Path(root) / _PROGRESS_DIRNAME / session_id
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
        if event.type == RUN_STARTED and run_id is not None:
            state[run_id] = "open"
        elif event.type == RUN_PAUSED and run_id is not None:
            state[run_id] = "paused"
            paused_reason[run_id] = event.data.get("reason")
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
                    "reason": event.data.get("reason"),
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
            evidence.append({
                "artifact_id": data.get("artifact_id"),
                "name": data.get("name"),
                "ref": data.get("ref"),
                "source_seq": event.seq,
            })

    parent_session_id: str | None = None
    fork_point_seq: int | None = None
    for event in events:
        if event.type == SESSION_FORKED:
            parent = event.data.get("parent_session_id")
            point = event.data.get("fork_point_seq")
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
    lines.append(f"- parent_session_id: {doc.parent_session_id or '-'}")
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
            f"- [{item['item_id']}] {_v(item['text'])}（origin={item['origin']}，"
            f"confirmed={item['confirmed']}）— 验证：{verification}{evidence}"
        )
    if not acceptance["criteria"]:
        lines.append(f"- {_NONE_LINE}")
    if acceptance["acceptance"]:
        acc = acceptance["acceptance"]
        reason = f"，原因：{_v(acc['reason'])}" if acc["reason"] else ""
        lines.append(f"- 接受状态：{acc['decision']}{reason}（version {acceptance['version']}）")
    else:
        lines.append(f"- 接受状态：未接受（version {acceptance['version']}）")
    lines.append("")

    lines.append("## 已验证里程碑")
    for milestone in doc.milestones:
        if milestone["kind"] == "verification_passed":
            evidence = f"（证据：{_v(milestone['evidence'])}）" if milestone["evidence"] else ""
            lines.append(f"- 验证通过 [{milestone['item_id']}]{evidence}")
        elif milestone["kind"] == "plan_step_completed":
            lines.append(f"- 清单完成 [{milestone['item_id']}] {_v(milestone['content'])}")
        elif milestone["kind"] == "run_completed":
            lines.append(f"- Run 完成 run_id={milestone['run_id']}")
    if not doc.milestones:
        lines.append(f"- {_NONE_LINE}")
    lines.append("")

    lines.append("## 决策")
    for decision in doc.decisions:
        if decision["kind"] == "accepted":
            reason = f"，原因：{_v(decision['reason'])}" if decision["reason"] else ""
            lines.append(
                f"- 接受裁决：{decision['decision']}{reason}"
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
            lines.append(f"- Run 失败 run_id={failure['run_id']}，原因：{_v(failure['reason'])}")
        elif failure["kind"] == "compaction_failed":
            lines.append(f"- 压缩失败 error_class={failure['error_class']}"
                         f"（来源 seq {failure['source_seq']}）")
        elif failure["kind"] == "guard_stuck":
            lines.append(f"- 循环护栏 level={failure['level']}"
                         f"（来源 seq {failure['source_seq']}）")
    if not doc.failures:
        lines.append(f"- {_NONE_LINE}")
    lines.append("")

    lines.append("## 未决 Operation")
    for operation in doc.pending_operations:
        lines.append(
            f"- tool_call_id={operation['tool_call_id']} "
            f"tool={operation['tool_name']}"
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
        if artifact["name"]:
            parts.append(f"name={_v(artifact['name'])}")
        if artifact["ref"]:
            parts.append(f"ref={_v(artifact['ref'])}")
        lines.append("- " + "，".join(parts))
    if not doc.evidence:
        lines.append(f"- {_NONE_LINE}")
    lines.append("")

    lines.append("## 阻塞与下一步")
    for blocker in doc.blockers:
        if blocker["kind"] == "run_paused":
            lines.append(
                f"- Run 暂停 run_id={blocker['run_id']}，原因：{_v(blocker['reason'])}"
            )
        else:
            lines.append(f"- Run 在途 run_id={blocker['run_id']}")
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


def _normalized_digest(body: str) -> str:
    """内容哈希把 generated_at 归一成占位——同源事件重复写 ⇒ 幂等跳过。"""
    normalized = re.sub(r"(?m)^- generated_at: .*$", "- generated_at: -", body)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


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
    # 同前缀孤儿临时文件全清（历次版本的后缀形态可能不同；本目录为 writer 专用）
    for stale in directory.glob(_TMP_PREFIX + "*"):
        if stale.name in (_MD_NAME, _META_NAME, _PREV_NAME):
            continue
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


def write_progress_file(
    root: Path | str,
    session_id: str,
    events,
    *,
    now: str | None = None,
) -> ProgressWriteOutcome:
    """派生 → 渲染 → 脱敏 → 原子落盘；幂等；失败明确报错不谎报最新。

    只派生与写文件，不追加任何事件、不调 git；调用方负责在既有触发点
    （创建/交付/cancel/run 终态等）以 best-effort 方式调用。
    """
    paths = progress_paths(root, session_id)
    doc = derive_progress_document(events, session_id=session_id)
    stamp = now or datetime.now(UTC).isoformat(timespec="seconds")
    body = render_progress_markdown(doc, generated_at=stamp)
    digest = _normalized_digest(body)

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
    if (
        old_meta is not None
        and old_meta.get("source_event_seq") == doc.source_event_seq
        and old_meta.get("content_sha256") == digest
    ):
        return ProgressWriteOutcome(ok=True, skipped=True, path=paths.markdown,
                                    source_event_seq=doc.source_event_seq)

    try:
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
