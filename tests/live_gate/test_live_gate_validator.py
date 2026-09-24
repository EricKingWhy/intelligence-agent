"""证据的独立复核（`#307` R3 / AC「证据包含要求字段并能由独立 replay/validator 验证」）。

每条用例都是"**改一处事实、看复核是否抓得住**"：漏判一条复核项，就等于允许一份被别人
改过的证据通过（`#213` 的失效形状就是读数与树不一致而无人拦）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from agent_harness.session import JsonlSessionStore, Session
from evaluation.live_gate import repo, validator
from evaluation.live_gate.schema import GATE_ATTEMPTS, SCHEMA_VERSION, Verdict
from evaluation.live_gate.validator import FAIL, PASS, validate_evidence
from tests.live_gate._evidence_factory import (
    RUN_ID,
    attempt_record,
    make_evidence,
    write_session_trace,
)

TOOL_CALLS = ("write",)


def _trace(tmp_path: Path, index: int) -> tuple[str, str, int, list[str]]:
    """在**证据文件旁边**放一条轨迹，返回 `(相对引用, sha256, 行数, 工具序列)`。"""
    relative = f"attempt-{index}.jsonl"
    source = write_session_trace(
        tmp_path / f"session-{index}", session_id="s", tool_calls=TOOL_CALLS,
    )
    shutil.copyfile(source, tmp_path / relative)
    text = (tmp_path / relative).read_text(encoding="utf-8")
    return relative, hashlib.sha256(text.encode("utf-8")).hexdigest(), text.count("\n"), list(TOOL_CALLS)


def _attempts_with_traces(tmp_path: Path) -> list:
    records = []
    for index in range(1, GATE_ATTEMPTS + 1):
        ref, digest, lines, tools = _trace(tmp_path, index)
        records.append(attempt_record(
            index, events_ref=ref, events_sha256=digest, event_count=lines, tool_calls=tuple(tools),
        ))
    return records


def _validate(tmp_path: Path, evidence, **overrides):
    path = tmp_path / "evidence.json"
    path.write_text(evidence.to_json(), encoding="utf-8", newline="\n")
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        payload[key] = value
    return validate_evidence(payload, evidence_path=path)


def _failures(report) -> list[str]:
    return [f"{check.name}: {check.detail}" for check in report.failures]


def test_untampered_evidence_passes_every_check(tmp_path: Path) -> None:
    report = _validate(tmp_path, make_evidence(attempts=_attempts_with_traces(tmp_path)))
    assert report.ok is True, _failures(report)
    names = {check.name for check in report.checks}
    assert {"schema", "sha_in_repo", "worktree_clean", "verdict_recomputed", "attempt[1].events",
            "attempt[1].tool_calls", "secret_scan"} <= names
    assert [check.status for check in report.checks if check.name == "worktree_clean"] == [PASS]


def test_tampered_trace_is_caught_by_sha256(tmp_path: Path) -> None:
    evidence = make_evidence(attempts=_attempts_with_traces(tmp_path))
    target = tmp_path / evidence.attempts[1].events_ref
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    report = _validate(tmp_path, evidence)
    assert report.ok is False
    assert "attempt[2].events" in " ".join(_failures(report))


def test_tampered_line_count_is_caught(tmp_path: Path) -> None:
    evidence = make_evidence(attempts=_attempts_with_traces(tmp_path))
    broken = evidence.model_copy(deep=True)
    broken.attempts[0].event_count = broken.attempts[0].event_count + 5
    report = _validate(tmp_path, broken)
    assert "attempt[1].event_count" in " ".join(_failures(report))


def test_declared_pass_with_a_failed_attempt_is_caught(tmp_path: Path) -> None:
    """声明 PASS 但有一次 FAIL ⇒ 重算不一致（"少跑一次仍称 3/3"的机器判据）。"""
    records = _attempts_with_traces(tmp_path)
    records[0].status = Verdict.FAIL
    records[0].error = "脚本化失败"
    report = _validate(tmp_path, make_evidence(attempts=records, verdict=Verdict.PASS))
    assert "verdict_recomputed" in " ".join(_failures(report))


def test_missing_attempt_is_caught_even_with_declared_pass(tmp_path: Path) -> None:
    records = _attempts_with_traces(tmp_path)[:2]
    report = _validate(tmp_path, make_evidence(attempts=records, verdict=Verdict.PASS))
    assert "verdict_recomputed" in " ".join(_failures(report))


def test_attempts_planned_must_stay_three(tmp_path: Path) -> None:
    """把计划次数改小（1/1 也算过）必须被复核抓住。"""
    evidence = make_evidence(
        attempts=_attempts_with_traces(tmp_path)[:1], verdict=Verdict.PASS, attempts_planned=1,
    )
    assert "verdict_recomputed" in " ".join(_failures(_validate(tmp_path, evidence)))


def test_injected_failure_can_never_validate_as_pass(tmp_path: Path) -> None:
    evidence = make_evidence(attempts=_attempts_with_traces(tmp_path), verdict=Verdict.PASS,
                             injected_failure="attempt:2")
    assert "verdict_recomputed" in " ".join(_failures(_validate(tmp_path, evidence)))


def test_blocked_with_attempts_or_without_preconditions_is_caught(tmp_path: Path) -> None:
    with_attempts = make_evidence(
        attempts=_attempts_with_traces(tmp_path), verdict=Verdict.BLOCKED, missing=("缺凭证",),
    )
    assert "BLOCKED 却带着已执行的 attempt" in " ".join(_failures(_validate(tmp_path, with_attempts)))

    without_preconditions = make_evidence(attempts=[], verdict=Verdict.BLOCKED)
    assert "没写未满足的前置" in " ".join(_failures(_validate(tmp_path, without_preconditions)))

    proper = make_evidence(attempts=[], verdict=Verdict.BLOCKED, missing=("缺凭证",))
    assert _validate(tmp_path, proper).ok is True


def test_skipped_requires_a_reason(tmp_path: Path) -> None:
    assert "SKIPPED 但没写理由" in " ".join(_failures(_validate(tmp_path, make_evidence(verdict=Verdict.SKIPPED))))
    ok = make_evidence(attempts=[], verdict=Verdict.SKIPPED, reason="维护窗口")
    assert _validate(tmp_path, ok).ok is True


def test_unresolvable_events_ref_is_caught(tmp_path: Path) -> None:
    records = [attempt_record(index, events_ref="nowhere/attempt-1.jsonl") for index in range(1, 4)]
    report = _validate(tmp_path, make_evidence(attempts=records))
    assert "轨迹引用无法解析" in " ".join(_failures(report))


def test_dangling_tool_call_is_caught(tmp_path: Path) -> None:
    """轨迹内部不一致（call 没有 result）也必须被复核抓住。"""
    root = tmp_path / "dangling"
    session = Session.start(JsonlSessionStore(root=root), session_id="s", cwd=root)
    session.append("run/started", {"agent_id": "default"}, run_id=RUN_ID)
    session.append(
        "tool/call", {"tool_call_id": "c1", "tool_name": "write", "args": {}}, run_id=RUN_ID,
    )
    source = root / "s" / "events.jsonl"
    text = source.read_text(encoding="utf-8")
    ref = "attempt-1.jsonl"
    shutil.copyfile(source, tmp_path / ref)
    records = [
        attempt_record(
            1, status=Verdict.FAIL, error="悬空", events_ref=ref,
            events_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            event_count=text.count("\n"), tool_calls=("write",),
        )
    ]
    report = _validate(tmp_path, make_evidence(attempts=records, verdict=Verdict.FAIL))
    assert "dangling_tool_calls" in " ".join(_failures(report))


def test_secret_in_evidence_is_caught_by_the_shape_layer(tmp_path: Path) -> None:
    """复核**不需要**凭证：形状层就足以抓住"证据里回显了 key"。"""
    leaked = "sk-" + "b" * 24
    evidence = make_evidence(attempts=_attempts_with_traces(tmp_path), reason=f"失败：{leaked}")
    report = _validate(tmp_path, evidence)
    assert "secret_scan" in " ".join(_failures(report))
    assert leaked not in json.dumps(
        [check.detail for check in report.checks], ensure_ascii=False,
    ), "复核结论本身也不得回显凭证"


def test_unknown_schema_version_is_a_failure(tmp_path: Path) -> None:
    report = _validate(tmp_path, make_evidence(), schema_version=SCHEMA_VERSION + 1)
    assert [check.name for check in report.failures] == ["schema"]


def test_evidence_sha_must_exist_in_this_repo(tmp_path: Path) -> None:
    evidence = make_evidence(attempts=_attempts_with_traces(tmp_path), sha="f" * 40, tree="0" * 40)
    report = _validate(tmp_path, evidence)
    assert "sha_in_repo" in " ".join(_failures(report)) or any(
        check.name == "sha_in_repo" and check.status != PASS for check in report.checks
    )


def test_recorded_worktree_deviation_fails_the_check(tmp_path: Path) -> None:
    """工作树输入证明如实为假时，复核必须红（读数不得指到一棵被污染的树）。"""
    from evaluation.live_gate.schema import WorktreeProof

    dirty = WorktreeProof(head_sha=repo.head_sha(), tree=repo.tree_of(), tracked_matches_head=False)
    evidence = make_evidence(attempts=_attempts_with_traces(tmp_path), worktree=dirty)
    assert "worktree_clean" in " ".join(_failures(_validate(tmp_path, evidence)))


def test_validator_does_not_import_the_runner() -> None:
    """"独立复核"的机械底线：复核不依赖产生证据的那份实现（判据重复是**有意**的）。"""
    assert "runner" not in dir(validator)
    assert FAIL != PASS
