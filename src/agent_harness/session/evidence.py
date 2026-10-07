"""W-08（#352）：验收项 ↔ 真实证据的服务端投影（session 层）。

写侧：``apply_evidence_recorded`` 把票面 14 字段 DTO 落成一条 append-only typed
事件 ``evidence/recorded``；形状校验住在 handler（坏形状 → 拒绝、零事件，
plan.py / task.py 同判据）；重跑追加新证据、不覆盖旧记录（1 criterion ↔ N 证据）。

读侧：``derive_evidence_state`` 是纯投影（按 ``criterion_id`` 聚合；幂等、可 replay），
与 ``derive_task_state`` 同域（#22），不新增状态机。

陈旧层：``compute_evidence_manifest``（记录时：显式文件清单 + 逐文件 sha256；
``progress.md`` 永不进入被覆盖文件集、其 hash 独立单列——审计红线 1）与
``evaluate_evidence_freshness``（读取时求值：当前 manifest / HEAD / artifact
可读回 + 归属校验；fail-closed，产出明确过期原因；绝不沿用旧"通过"）。

``VerificationEntry.evidence``（``str|None`` 人类摘要/指针）维持不变；#524
completion_evidence（Runtime 完成门）与本票严格区分。

脱敏纪律：``command_or_action`` / ``exit_code_or_observation`` 原样持久化
（append-only 真相）；敏感值不得进正文是**记录侧**的责任——调用方先脱敏再记录，
本层只保证脱敏后的值逐字往返。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from agent_harness.session.event import EVIDENCE_RECORDED
from agent_harness.session.task import TASK_FIELD_MAX_LENGTH, derive_task_state

logger = logging.getLogger(__name__)

#: 证据来源分类（票面枚举）：``test`` / ``ui`` / ``diff`` = 机器验证事实；
#: ``external`` = 人工/外部结论（reviewer 结论、用户手工验收——票面"用户手工验收
#: 与机器验证事实分列"即按 kind 区分；用户裁决另由接受轴 task/accepted 承载）。
EVIDENCE_KINDS = ("test", "ui", "diff", "external")

#: 证据结果三态（票面枚举；无 Chrome/MCP 的浏览器项落 blocked，绝不落 pass）。
EVIDENCE_RESULTS = ("pass", "fail", "blocked")

#: 票面 14 字段（写侧闭合 DTO：未知顶层键按 shape 拒绝）。
_EVIDENCE_FIELD_NAMES = frozenset(
    {
        "evidence_id",
        "task_session_id",
        "run_id",
        "criterion_id",
        "kind",
        "source_event_seq",
        "tool_call_id",
        "captured_at",
        "result",
        "command_or_action",
        "exit_code_or_observation",
        "artifact_ref",
        "base_head",
        "workspace_manifest",
    }
)

_SHA256_RE_LEN = 64
_BASE_HEAD_HEX_LEN = 40
_PROGRESS_MD_NAME = "progress.md"


@dataclass(frozen=True)
class EvidenceFile:
    """被覆盖清单中的一个文件（相对 POSIX 路径 + 原始字节 sha256）。"""

    path: str
    sha256: str

    def to_payload(self) -> dict:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class EvidenceManifest:
    """证据覆盖的工作区 manifest：显式文件清单 + 逐文件 sha256。

    ``progress.md`` 永不进入 ``files``（否则更新进度即自我过期——审计红线 1），
    其 hash 独立单列为 ``progress_md_sha256``（缺席时为 None，如实）。
    ``manifest_hash`` = 排序后 ``path\\tsha256`` 行拼接的 sha256（顺序无关）。
    """

    files: tuple[EvidenceFile, ...]
    manifest_hash: str
    progress_md_sha256: str | None

    def to_payload(self) -> dict:
        return {
            "files": [item.to_payload() for item in self.files],
            "manifest_hash": self.manifest_hash,
            "progress_md_sha256": self.progress_md_sha256,
        }


@dataclass(frozen=True)
class EvidenceRecord:
    """一条结构化证据（票面 14 字段；不可变）。"""

    evidence_id: str
    task_session_id: str
    run_id: str
    criterion_id: str
    kind: str
    source_event_seq: int | None
    tool_call_id: str | None
    captured_at: str
    result: str
    command_or_action: str | None
    exit_code_or_observation: int | str | None
    artifact_ref: str | None
    base_head: str | None
    workspace_manifest: EvidenceManifest

    def to_payload(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "task_session_id": self.task_session_id,
            "run_id": self.run_id,
            "criterion_id": self.criterion_id,
            "kind": self.kind,
            "source_event_seq": self.source_event_seq,
            "tool_call_id": self.tool_call_id,
            "captured_at": self.captured_at,
            "result": self.result,
            "command_or_action": self.command_or_action,
            "exit_code_or_observation": self.exit_code_or_observation,
            "artifact_ref": self.artifact_ref,
            "base_head": self.base_head,
            "workspace_manifest": self.workspace_manifest.to_payload(),
        }


@dataclass(frozen=True)
class EvidenceFreshness:
    """读取时求值的陈旧判定（fail-closed：任一存疑即 stale）。

    ``status`` 只取 ``fresh`` / ``stale``；``reasons`` 是明确过期原因
    （变动文件逐个列出 / base_head 变化 / artifact 不可读回或归属未过）。
    """

    status: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceState:
    """证据投影快照（不可变；按 criterion_id 聚合，重跑追加不覆盖）。"""

    by_criterion: dict[str, tuple[EvidenceRecord, ...]] = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "by_criterion": {
                criterion_id: [record.to_payload() for record in records]
                for criterion_id, records in self.by_criterion.items()
            }
        }


@dataclass(frozen=True)
class EvidenceOutcome:
    """``apply_evidence_recorded`` 的统一结果（task.py ``TaskOutcome`` 同构）。

    ``error_kind``：``shape`` = 载荷非法（REST 层映射 422）；
    ``conflict`` = evidence_id 重复（REST 层映射 409）。
    ``state`` 是本次调用时点的证据投影快照（成功 = 落盘后，失败 = 被拒时点）。
    """

    ok: bool
    reason: str | None
    error_kind: str | None
    state: EvidenceState


def _failure(state: EvidenceState, error_kind: str, reason: str) -> EvidenceOutcome:
    return EvidenceOutcome(ok=False, reason=reason, error_kind=error_kind, state=state)


def _is_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(c in "0123456789abcdef" for c in value)
    )


def _non_empty_str(value: object, *, max_length: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_length


def _opt_str(value: object, *, max_length: int) -> bool:
    """可选自由文本字段的统一判据：None 或限长非空字符串。"""
    return value is None or _non_empty_str(value, max_length=max_length)


def _check_manifest_shape(raw: object) -> str | None:
    """workspace_manifest 嵌套 DTO 形状校验；合法返回 None，否则返回原因。"""
    if not isinstance(raw, dict):
        return f"workspace_manifest 必须是对象，得到 {type(raw).__name__}"
    files = raw.get("files")
    if not isinstance(files, list):
        return f"workspace_manifest.files 必须是数组，得到 {type(files).__name__}"
    for entry in files:
        if not isinstance(entry, dict):
            return f"manifest 文件项必须是对象，得到 {type(entry).__name__}"
        path = entry.get("path")
        if not _non_empty_str(path, max_length=TASK_FIELD_MAX_LENGTH):
            return f"manifest 文件 path 必须是非空字符串，得到 {path!r}"
        posix = PurePosixPath(path)
        if posix.is_absolute() or ".." in posix.parts:
            return f"manifest 文件 path 非法（绝对路径或含 ..）：{path!r}"
        if posix.name == _PROGRESS_MD_NAME:
            return (
                f"manifest 覆盖集不得包含 {_PROGRESS_MD_NAME}"
                "（否则更新进度即自我过期；其 hash 独立单列）"
            )
        if not _is_hex(entry.get("sha256"), _SHA256_RE_LEN):
            return f"manifest 文件 sha256 必须是 64 位 hex：{path!r}"
    if not _is_hex(raw.get("manifest_hash"), _SHA256_RE_LEN):
        return "workspace_manifest.manifest_hash 必须是 64 位 hex"
    progress_sha = raw.get("progress_md_sha256")
    if progress_sha is not None and not _is_hex(progress_sha, _SHA256_RE_LEN):
        return "workspace_manifest.progress_md_sha256 必须是 64 位 hex 或 None"
    return None


def _parse_manifest(raw: object) -> tuple[EvidenceManifest | None, str | None]:
    """投影侧 manifest 解析 → ``(manifest, None)``；非法 → ``(None, 具体原因)``。"""
    error = _check_manifest_shape(raw)
    if error is not None:
        return None, error
    assert isinstance(raw, dict)
    return (
        EvidenceManifest(
            files=tuple(
                EvidenceFile(path=entry["path"], sha256=entry["sha256"])
                for entry in raw["files"]
            ),
            manifest_hash=raw["manifest_hash"],
            progress_md_sha256=raw.get("progress_md_sha256"),
        ),
        None,
    )


def _parse_record(data: dict | None) -> tuple[EvidenceRecord | None, str | None]:
    """evidence/recorded 载荷解析 → ``(record, None)``；非法 → ``(None, 具体原因)``。

    具体原因供写侧"保存失败时显示缺项"（票面）；投影侧只取 record（非法即跳过
    该事件）。刻意与写侧同严格：投影侧不做缺省补全（task.py ``_parse_criteria``
    同判据），否则两次 derive 可能得到不同结果，重放确定性就没了。
    """
    if not isinstance(data, dict):
        return None, f"evidence 载荷必须是对象，得到 {type(data).__name__}"
    unknown = set(data) - _EVIDENCE_FIELD_NAMES
    if unknown:
        return None, f"未知字段：{sorted(unknown)}（14 字段 DTO 是闭合的）"
    if not _non_empty_str(data.get("evidence_id"), max_length=TASK_FIELD_MAX_LENGTH):
        return None, "evidence_id 必须是非空字符串"
    for key in ("task_session_id", "run_id", "criterion_id", "captured_at"):
        if not _non_empty_str(data.get(key), max_length=TASK_FIELD_MAX_LENGTH):
            return None, f"{key} 必须是非空字符串"
    if data.get("kind") not in EVIDENCE_KINDS:
        return None, f"kind 非法：{data.get('kind')!r}（合法值 {list(EVIDENCE_KINDS)}）"
    if data.get("result") not in EVIDENCE_RESULTS:
        return (
            None,
            f"result 非法：{data.get('result')!r}（合法值 {list(EVIDENCE_RESULTS)}）",
        )
    seq = data.get("source_event_seq")
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool) or seq < 0):
        return None, f"source_event_seq 必须是非负整数或 None，得到 {seq!r}"
    if not _opt_str(data.get("tool_call_id"), max_length=TASK_FIELD_MAX_LENGTH):
        return None, "tool_call_id 必须是非空字符串或 None"
    if not _opt_str(data.get("command_or_action"), max_length=TASK_FIELD_MAX_LENGTH):
        return None, "command_or_action 必须是非空字符串或 None"
    exit_info = data.get("exit_code_or_observation")
    if exit_info is not None:
        if isinstance(exit_info, bool):
            return None, "exit_code_or_observation 不能是布尔值"
        if not isinstance(exit_info, int) and not _non_empty_str(
            exit_info, max_length=TASK_FIELD_MAX_LENGTH
        ):
            return None, "exit_code_or_observation 必须是整数/非空字符串或 None"
    if not _opt_str(data.get("artifact_ref"), max_length=TASK_FIELD_MAX_LENGTH):
        return None, "artifact_ref 必须是非空字符串或 None"
    base_head = data.get("base_head")
    if base_head is not None and not _is_hex(base_head, _BASE_HEAD_HEX_LEN):
        return None, "base_head 必须是 40 位 hex（git rev-parse HEAD）或 None"
    manifest, manifest_error = _parse_manifest(data.get("workspace_manifest"))
    if manifest is None:
        return None, manifest_error
    return EvidenceRecord(
        evidence_id=data["evidence_id"],
        task_session_id=data["task_session_id"],
        run_id=data["run_id"],
        criterion_id=data["criterion_id"],
        kind=data["kind"],
        source_event_seq=seq,
        tool_call_id=data.get("tool_call_id"),
        captured_at=data["captured_at"],
        result=data["result"],
        command_or_action=data.get("command_or_action"),
        exit_code_or_observation=exit_info,
        artifact_ref=data.get("artifact_ref"),
        base_head=base_head,
        workspace_manifest=manifest,
    ), None


def _check_shape(evidence: dict, session) -> str | None:
    """写侧形状校验：合法返回 None，否则返回拒绝原因（零事件）。

    判据：未知顶层键拒绝（闭合 DTO，防调用方笔误静默持久化——判据实现只在
    ``_parse_record`` 一处，错误文案不写两份）；
    task_session_id 必须等于本会话 id（防跨会话证据污染）；
    criterion_id 必须在当前任务清单里（apply_verification 同判据）；
    manifest 复用同一套嵌套校验（含 progress.md 禁入覆盖集）。
    """
    _, error = _parse_record(evidence)
    if error is not None:
        return error
    if evidence["task_session_id"] != session.session_id:
        return (
            f"task_session_id {evidence['task_session_id']!r} 与本会话 "
            f"{session.session_id!r} 不一致（不记录跨会话证据）"
        )
    task_state = derive_task_state(session.events)
    if not task_state.defined:
        return "尚无任务定义，无可关联的验收项"
    if all(item.item_id != evidence["criterion_id"] for item in task_state.criteria):
        return f"验收项 {evidence['criterion_id']!r} 不在当前清单里"
    return None


def apply_evidence_recorded(session, evidence: object) -> EvidenceOutcome:
    """handler：14 字段证据 DTO → ``evidence/recorded``（append-only）。

    坏形状 → ``shape`` 拒绝、零事件；``evidence_id`` 重复 → ``conflict``
    拒绝、零新事件。重跑追加新证据，不覆盖旧记录。
    """
    current = derive_evidence_state(session.events)
    if not isinstance(evidence, dict):
        return _failure(
            current, "shape", f"evidence 必须是对象，得到 {type(evidence).__name__}"
        )
    error = _check_shape(evidence, session)
    if error is not None:
        return _failure(current, "shape", error)
    if any(
        record.evidence_id == evidence["evidence_id"]
        for records in current.by_criterion.values()
        for record in records
    ):
        return _failure(
            current, "conflict",
            f"evidence_id {evidence['evidence_id']!r} 已记录（不双记；重跑请用新 id）",
        )
    payload = {key: evidence[key] for key in _EVIDENCE_FIELD_NAMES}
    session.append(EVIDENCE_RECORDED, payload, run_id=evidence["run_id"])
    return EvidenceOutcome(
        ok=True, reason=None, error_kind=None,
        state=derive_evidence_state(session.events),
    )


def derive_evidence_state(events: list) -> EvidenceState:
    """纯函数投影：events → EvidenceState（幂等；重放 N 次结果一致）。

    按 ``criterion_id`` 聚合，保持记录顺序（重跑追加不覆盖）；畸形载荷的事件
    只跳过该事件（warning），不破坏整流投影。
    """
    by_criterion: dict[str, list[EvidenceRecord]] = {}
    for event in events:
        if event.type != EVIDENCE_RECORDED:
            continue
        record, _ = _parse_record(event.data if isinstance(event.data, dict) else None)
        if record is None:
            logger.warning(
                "evidence/recorded (seq=%s) payload 非法，投影跳过该事件",
                getattr(event, "seq", None),
            )
            continue
        by_criterion.setdefault(record.criterion_id, []).append(record)
    return EvidenceState(
        by_criterion={
            criterion_id: tuple(records)
            for criterion_id, records in by_criterion.items()
        }
    )


def compute_evidence_manifest(
    root: str | Path, covered_paths: Sequence[str]
) -> EvidenceManifest:
    """为显式文件清单计算 manifest（记录时调用；纯函数）。

    - 每个路径按相对 POSIX 解析于 ``root`` 下；绝对路径 / ``..`` 段 →
      ``ValueError``（§4.3 路径穿越纪律）；缺失文件 → ``FileNotFoundError``
      （调用方 bug，如实抛）。
    - ``progress.md``（任意层级）不得进入覆盖集 → ``ValueError``（审计红线 1）；
      root 下的 ``progress.md`` hash 独立单列（不存在 → None）。
    - sha256 取文件原始字节（字节同一性才是"版本匹配"的判据，不做换行归一化）。
    - ``files`` 保持调用方给定的顺序；``manifest_hash`` 与顺序无关。
    """
    base = Path(root)
    files: list[EvidenceFile] = []
    for raw in covered_paths:
        posix = PurePosixPath(raw)
        if posix.is_absolute() or ".." in posix.parts:
            raise ValueError(f"覆盖路径非法（绝对路径或含 ..）：{raw!r}")
        if posix.name == _PROGRESS_MD_NAME:
            raise ValueError(
                f"覆盖集不得包含 {_PROGRESS_MD_NAME}（否则更新进度即自我过期）"
            )
        full = base.joinpath(*posix.parts)
        digest = hashlib.sha256(full.read_bytes()).hexdigest()
        files.append(EvidenceFile(path=posix.as_posix(), sha256=digest))
    canonical = "\n".join(
        f"{item.path}\t{item.sha256}" for item in sorted(files, key=lambda i: i.path)
    )
    manifest_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    progress_file = base / _PROGRESS_MD_NAME
    progress_sha = (
        hashlib.sha256(progress_file.read_bytes()).hexdigest()
        if progress_file.is_file()
        else None
    )
    return EvidenceManifest(
        files=tuple(files),
        manifest_hash=manifest_hash,
        progress_md_sha256=progress_sha,
    )


def evaluate_evidence_freshness(
    record: EvidenceRecord,
    *,
    current_manifest: EvidenceManifest | None,
    current_head: str | None,
    artifact_readable: bool | None = None,
    artifact_attribution_ok: bool | None = None,
    artifact_cleaned: bool = False,
) -> EvidenceFreshness:
    """读取时求值证据是否仍然新鲜（fail-closed：任一存疑即 stale）。

    - ``current_manifest is None``（工作区不可读）→ stale；
    - 记录中有、当前缺失或 sha 不同的文件 → stale（逐个列明 path）；
    - ``record.base_head`` 非 None 且 ``current_head`` 不符 → stale；
    - ``record.artifact_ref`` 非空：``artifact_cleaned`` 为真 → stale（原件已被
      显式清理，原因区别于"不可读回/损坏"）；否则不可读回 → stale；可读回但归属
      校验未过 → stale；
    - 全过 → fresh、``reasons == ()``。陈旧只产出状态，**不改写** ``record.result``。

    ``artifact_cleaned`` 只是"原件已清理"的枚举区分（#368 / W-24）：证据投影不重写，
    仍落 stale，只是把清理与损坏/不可读分开报。
    """
    reasons: list[str] = []
    if current_manifest is None:
        reasons.append("无法读取当前工作区 manifest（fail-closed 判 stale）")
    elif current_manifest.manifest_hash != record.workspace_manifest.manifest_hash:
        # manifest_hash 先行比对：一致则文件维度直接通过；不一致才逐文件
        # diff 产出明确原因（列出变动文件）。
        current = {item.path: item.sha256 for item in current_manifest.files}
        for item in record.workspace_manifest.files:
            current_sha = current.get(item.path)
            if current_sha is None:
                reasons.append(f"覆盖文件已缺失：{item.path}")
            elif current_sha != item.sha256:
                reasons.append(f"覆盖文件已变动：{item.path}")
    if record.base_head is not None and current_head != record.base_head:
        reasons.append(
            f"base_head 已变动：记录 {record.base_head!r}，当前 {current_head!r}"
        )
    if record.artifact_ref:
        if artifact_cleaned:
            reasons.append(f"artifact 原件已清理：{record.artifact_ref}")
        elif artifact_readable is not True:
            reasons.append(f"artifact 不可读回：{record.artifact_ref}")
        elif record.tool_call_id and artifact_attribution_ok is not True:
            reasons.append(f"artifact 归属校验未通过：{record.artifact_ref}")
    return EvidenceFreshness(
        status="fresh" if not reasons else "stale", reasons=tuple(reasons)
    )
