"""证据的独立复核（`#307` R3 / AC「证据包含要求字段并能由独立 replay/validator 验证」）。

## "独立"到什么程度

本模块**不重新跑那个场景**（那要凭证与真实 run），而是把证据里的**声明**重新算一遍，
凡是能由载荷自身或 git 事实判定的，一律重算：

| 复核项 | 判据 | 抓的是哪种假 |
| --- | --- | --- |
| schema 版本 / 必需字段 | `schema.load_evidence`（pydantic） | 手改漏字段、版本漂移 |
| `sha` ↔ 本仓库 | `git rev-parse <sha>^{tree}` 必须等于 `tree` | **证据指向另一棵树**（#213 同族） |
| 总判定 | 由 `attempts` 的逐条状态**重算** 3/3（含注入/替身上限） | 声明 `PASS` 但只有 2 次成功 |
| 尝试数 | `len(attempts)` 必须等于 `runner.attempts_planned`（缺的不得当成功） | 少跑一次仍称 3/3 |
| 事件轨迹 | 逐条重读 JSONL：重算行数、`tool/call` 工具序列、run 终态在场、`sha256` 与记录值比对 | 轨迹被替换 / 事后编辑 |
| 空轨迹引用 | **PASS 尝试没有轨迹引用 ⇒ FAIL**；未跑起来的尝试（注入 / 前置不成立 / 命中凭证）⇒ `UNAVAILABLE` | "声称跑过但不可复核" / 把失败样本的缺席当成 FAIL |
| 悬空工具调用 | `evaluation.assertions.dangling_tool_call_ids` 重算 | 轨迹内部不一致 |
| 凭证 | 整份 JSON + 轨迹文件重扫一遍 | 凭证被写进证据 |
| 工作树 | 证据里记录的 `tracked_matches_head` 为真（且 `risky` 为空） | 读数指到一棵被污染的树 |

`validate_evidence()` 返回逐项结论，任一 `FAIL` ⇒ 退出码非 0（`UNAVAILABLE` **不算失败**：
它是"本环境复核不了这一项"，如实标出而不是放行）。**它判不了**"场景语义是否真的
实现正确"（那要读代码 + 真跑）；它判的是"这份证据**自洽且可核对**"。

## 证据路径解析

`attempts[i].events_ref` 优先按**仓库相对路径**解析（runner 落盘时的写法）；相对路径在
仓库里找不到时，退回到"证据文件所在目录下的同名文件"（测试用临时目录里的证据走这条）。
两条都不中 ⇒ 该 attempt 判**不通过**（轨迹引用不可解析 = 无法复核）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evaluation.assertions import dangling_tool_call_ids
from evaluation.live_gate import repo
from evaluation.live_gate.schema import (
    GATE_ATTEMPTS,
    LiveEvidence,
    Verdict,
    load_evidence,
)
from evaluation.live_gate.secrets import SecretFinding, mask_text

PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"


@dataclass
class CheckResult:
    """一条复核项。`status` ∈ {PASS, FAIL, UNAVAILABLE}。"""

    name: str
    status: str
    detail: str = ""


@dataclass
class ValidationReport:
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, status=status, detail=detail))

    @property
    def ok(self) -> bool:
        return all(check.status != FAIL for check in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if check.status == FAIL]


def _resolve_events_ref(ref: str, *, evidence_path: Path | None) -> Path | None:
    if not ref:
        return None
    candidate = Path(ref)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    in_repo = repo.REPO_ROOT / candidate
    if in_repo.exists():
        return in_repo
    if evidence_path is not None:
        beside = evidence_path.parent / candidate
        if beside.exists():
            return beside
    return None


def _recompute_verdict(evidence: LiveEvidence) -> Verdict:
    """与 `runner` **独立**的一遍重算（同一判据、另一处实现 —— 刻意不 import runner）。

    差异点即攻击面：若 runner 哪天放宽了判据，这里会红。
    """
    if evidence.injected_failure or evidence.seams:
        return Verdict.FAIL
    if len(evidence.attempts) != evidence.runner.attempts_planned:
        return Verdict.FAIL
    if evidence.runner.attempts_planned != GATE_ATTEMPTS:
        return Verdict.FAIL
    if any(attempt.status is not Verdict.PASS for attempt in evidence.attempts):
        return Verdict.FAIL
    return Verdict.PASS


def _run_terminal_check(events: list[Any], attempt: Any) -> tuple[str, str]:
    """该 attempt 的 run 在轨迹里有没有终态。**只出 PASS / UNAVAILABLE**。

    不给 FAIL 是刻意的：真实运行里"进程被杀 / 连接中断"的尝试可能真的没有终态事件，而
    那正是要保留的失败样本；把它判 FAIL 等于"失败的形状本身不可通过复核"。缺终态就如实
    记 UNAVAILABLE —— 复核者知道该往哪看，报告不会因此变绿也不会变红。
    """
    from agent_harness.session.event import RUN_TERMINAL_TYPES

    terminal = [
        event for event in events
        if event.type in RUN_TERMINAL_TYPES and (not attempt.run_id or event.run_id == attempt.run_id)
    ]
    if terminal:
        return PASS, f"run 终态在场（{terminal[-1].type}）"
    return UNAVAILABLE, "轨迹里没有该 run 的终态事件（尝试可能中途中断）"


def validate_evidence(
    payload: dict[str, Any],
    *,
    evidence_path: Path | None = None,
    secret_values: tuple[str, ...] = (),
) -> ValidationReport:
    """复核一份证据。返回逐项结论（`report.ok` = 无 FAIL）。"""
    report = ValidationReport()
    try:
        evidence = load_evidence(payload)
    except Exception as error:  # noqa: BLE001 - 解析失败就是复核失败
        report.add("schema", FAIL, f"{type(error).__name__}: {error}")
        return report
    report.add("schema", PASS, f"schema_version={evidence.schema_version}")

    # ① 证据指向的树与本仓库对得上吗（对不上就是"读了一棵没被测过的树"）
    if evidence.sha and evidence.tree:
        resolved = repo.git("rev-parse", f"{evidence.sha}^{{tree}}").stdout.strip()
        if not resolved:
            report.add("sha_in_repo", UNAVAILABLE, f"本仓库没有 {evidence.sha[:12]}（干净克隆里复核时正常）")
        elif resolved == evidence.tree:
            report.add("sha_in_repo", PASS, f"{evidence.sha[:12]} → tree {evidence.tree[:12]}")
        else:
            report.add("sha_in_repo", FAIL, f"{evidence.sha[:12]} 的 tree 是 {resolved[:12]}，证据写的是 {evidence.tree[:12]}")
    else:
        report.add("sha_in_repo", FAIL, "证据缺 sha / tree")

    # ② 工作树输入证明
    if evidence.worktree.tracked_matches_head and not evidence.worktree.risky:
        report.add("worktree_clean", PASS, "三条判据均未触发")
    else:
        report.add(
            "worktree_clean", FAIL,
            f"tracked_matches_head={evidence.worktree.tracked_matches_head} risky={len(evidence.worktree.risky)}",
        )

    # ③ 总判定重算
    recomputed = _recompute_verdict(evidence)
    declared = evidence.verdict
    if declared in (Verdict.PASS, Verdict.FAIL):
        if declared is recomputed:
            report.add("verdict_recomputed", PASS, f"{declared.value}（由 attempts 重算一致）")
        else:
            report.add(
                "verdict_recomputed", FAIL,
                f"声明 {declared.value}，按 attempts 重算应为 {recomputed.value}",
            )
    elif declared is Verdict.BLOCKED:
        if evidence.attempts:
            report.add("verdict_recomputed", FAIL, "BLOCKED 却带着已执行的 attempt")
        elif not evidence.missing_preconditions:
            report.add("verdict_recomputed", FAIL, "BLOCKED 但没写未满足的前置")
        else:
            report.add("verdict_recomputed", PASS, "BLOCKED + 前置清单在场")
    else:  # SKIPPED
        if not evidence.reason:
            report.add("verdict_recomputed", FAIL, "SKIPPED 但没写理由")
        else:
            report.add("verdict_recomputed", PASS, "SKIPPED + 理由在场")

    # ④ 逐次尝试的轨迹复核
    for attempt in evidence.attempts:
        where = f"attempt[{attempt.index}]"
        if not attempt.events_ref:
            # 空引用只在"这次没跑起来"时成立（注入 / 前置不成立 / 命中凭证而停）：如实记
            # UNAVAILABLE。**PASS 尝试不允许没有轨迹** —— 那正是"声称跑过但不可复核"的形状。
            if attempt.status is Verdict.PASS:
                report.add(f"{where}.events", FAIL, "PASS 尝试没有轨迹引用（不可复核）")
            else:
                report.add(
                    f"{where}.events", UNAVAILABLE,
                    f"未产生轨迹的尝试（{attempt.error or '无错误信息'}）",
                )
            continue
        path = _resolve_events_ref(attempt.events_ref, evidence_path=evidence_path)
        if path is None:
            report.add(f"{where}.events", FAIL, f"轨迹引用无法解析：{attempt.events_ref}")
            continue
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if attempt.events_sha256 and digest != attempt.events_sha256:
            report.add(f"{where}.events", FAIL, "轨迹 sha256 与证据记录不一致（文件被改过？）")
        else:
            report.add(f"{where}.events", PASS, f"sha256 一致（{digest[:12]}）")
        if attempt.event_count and attempt.event_count != text.count("\n"):
            report.add(
                f"{where}.event_count", FAIL,
                f"证据写 {attempt.event_count} 行，轨迹实为 {text.count(chr(10))} 行",
            )
        events = _load_events(path)
        if events is None:
            report.add(f"{where}.events_parse", UNAVAILABLE, "轨迹不是本仓的 SessionEvent JSONL 格式")
            continue
        dangling = dangling_tool_call_ids(events)
        if dangling:
            report.add(f"{where}.dangling_tool_calls", FAIL, f"悬空 call={dangling}")
        else:
            report.add(f"{where}.dangling_tool_calls", PASS, "无悬空 call")
        tools = [str(event.data.get("tool_name")) for event in events if event.type == "tool/call"]
        if attempt.tool_calls and tools != attempt.tool_calls:
            report.add(f"{where}.tool_calls", FAIL, f"证据写 {attempt.tool_calls}，轨迹实为 {tools}")
        else:
            report.add(f"{where}.tool_calls", PASS, f"{len(tools)} 次调用一致")
        report.add(f"{where}.run_terminal", *_run_terminal_check(events, attempt))

    # ⑤ 凭证复扫（证据本体 + 轨迹文件；`seek` 走整份文本）
    findings: list[SecretFinding] = []
    _, hits = mask_text(json.dumps(payload, ensure_ascii=False), values=secret_values, where="evidence")
    findings.extend(hits)
    for attempt in evidence.attempts:
        path = _resolve_events_ref(attempt.events_ref, evidence_path=evidence_path)
        if path is None:
            continue
        _, hits = mask_text(path.read_text(encoding="utf-8"), values=secret_values, where=attempt.events_ref)
        findings.extend(hits)
    if findings:
        report.add(
            "secret_scan", FAIL,
            "；".join(f"{finding.rule}@{finding.where}" for finding in findings),
        )
    else:
        note = "" if secret_values else "（本环境未配置凭证 ⇒ 只跑了形状层，精确值层 unavailable）"
        report.add("secret_scan", PASS, f"未发现凭证{note}")
    return report


def _load_events(path: Path) -> list[Any] | None:
    """把轨迹读成 `SessionEvent` 列表（逐行 JSON；解析不了就返回 None，不猜）。

    ⚠ `SessionEvent` 是 **dataclass**（`session/event.py`），解析入口是 `from_dict` ——
    没有 `model_validate`。这里曾误用 pydantic 的入口，结果是**每一条轨迹都被判成
    "不是本仓格式"（UNAVAILABLE）**：悬空 call / 工具序列两项复核整条失效而报告仍然
    "通过"。真实证据上的复查抓到了它，故此处按生产写侧的对称入口解析。
    """
    from agent_harness.session.event import SessionEvent

    events: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(SessionEvent.from_dict(json.loads(line)))
        except Exception:  # noqa: BLE001 - 格式不符是 UNAVAILABLE，不是 FAIL
            return None
    return events
