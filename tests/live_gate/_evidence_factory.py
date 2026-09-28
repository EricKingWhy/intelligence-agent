"""机制测试的夹具：手工构造 `LiveEvidence` 与最小合法事件轨迹。

⚠ **本模块的产物不是 Live Gate 证据**，只是喂给 `validator` / `secrets` / CLI 的输入。
入库证据只能由 `scripts/live_gate.py run` 用真实模型 + 生产工具产出（`#305` 明文：
Fake 结果不得计入 Live Gate）。调用方一律把落点放在 `tmp_path`。

事件轨迹**用生产 `Session`**（`Session.start` + `append`）写，不手搓 JSON 行：这样
"轨迹格式合法"这件事由生产写侧保证，测试只在它上面做篡改（改 sha / 改行数 / 改判定）。
"""

from __future__ import annotations

from pathlib import Path

from agent_harness.session import JsonlSessionStore, Session
from evaluation.live_gate import repo
from evaluation.live_gate.schema import (
    GATE_ATTEMPTS,
    AssertionResult,
    AttemptRecord,
    CapabilityRecord,
    LiveEvidence,
    ProviderRecord,
    RunnerRecord,
    SandboxRecord,
    SecretScanRecord,
    Verdict,
    WorktreeProof,
    decide_verdict,
    now_utc,
)

#: 伪造证据里的 run id（固定值便于断言，不用随机）。
RUN_ID = "run-test-0001"


def write_session_trace(
    root: Path, *, session_id: str, tool_calls: tuple[str, ...] = ("write", "bash"),
) -> Path:
    """用生产 `Session` 写一条最小合法轨迹（`tool/call` 与 `tool/result` 严格配对）。

    返回 JSONL 路径（`<root>/<session_id>/events.jsonl`，与 runner 读的布局一致）。
    """
    store = JsonlSessionStore(root=root)
    session = Session.start(store, session_id=session_id, cwd=root)
    session.append("run/started", {"agent_id": "default"}, run_id=RUN_ID)
    for index, name in enumerate(tool_calls):
        call_id = f"call-{index}"
        session.append(
            "tool/call",
            {"tool_call_id": call_id, "tool_name": name, "args": {}},
            run_id=RUN_ID,
        )
        session.append(
            "tool/result",
            {"tool_call_id": call_id, "tool_name": name},
            run_id=RUN_ID,
        )
    session.append("run/completed", {}, run_id=RUN_ID)
    return root / session_id / "events.jsonl"


def attempt_record(
    index: int,
    *,
    status: Verdict = Verdict.PASS,
    events_ref: str = "",
    events_sha256: str = "",
    event_count: int = 0,
    tool_calls: tuple[str, ...] = (),
    error: str = "",
) -> AttemptRecord:
    return AttemptRecord(
        index=index,
        started_at=now_utc(),
        ended_at=now_utc(),
        status=status,
        duration_ms=1,
        session_id=f"session-{index}",
        run_id=RUN_ID if status is Verdict.PASS else "",
        run_status="completed" if status is Verdict.PASS else "",
        tool_calls=list(tool_calls),
        events_ref=events_ref,
        events_sha256=events_sha256,
        event_count=event_count,
        assertions=[AssertionResult(name="run_completed", ok=status is Verdict.PASS)],
        error=error,
    )


def make_evidence(
    *,
    attempts: list[AttemptRecord] | None = None,
    verdict: Verdict | None = None,
    reason: str = "",
    missing: tuple[str, ...] = (),
    injected_failure: str = "",
    seams: dict[str, str] | None = None,
    worktree: WorktreeProof | None = None,
    sha: str = "",
    tree: str = "",
    attempts_planned: int = GATE_ATTEMPTS,
    capability_verdict: str = "READY",
) -> LiveEvidence:
    """按需拼一份证据。`verdict` 不传 ⇒ 由 `decide_verdict` 算（不传才能造出"声明不一致"）。"""
    records = attempts if attempts is not None else [
        attempt_record(index) for index in range(1, attempts_planned + 1)
    ]
    statuses = [record.status for record in records]
    head = sha or repo.head_sha()
    return LiveEvidence(
        scenario_id="fake-scenario-for-mechanism-tests",
        scenario_version=1,
        verdict=(
            verdict if verdict is not None
            else decide_verdict(
                attempt_statuses=statuses,
                attempts_planned=attempts_planned,
                injected_failure=injected_failure,
                injected_seams=tuple(seams or {}),
            )
        ),
        reason=reason,
        missing_preconditions=list(missing),
        created_at=now_utc(),
        sha=head,
        tree=tree or repo.tree_of(head),
        worktree=worktree or WorktreeProof(
            head_sha=head, tree=tree or repo.tree_of(head), tracked_matches_head=True,
        ),
        provider=ProviderRecord(provider_id="fake", model_name="fake-model", base_url_host="localhost"),
        sandbox=SandboxRecord(
            backend="local", disposable=True, created=True, deleted=True,
            env_allowlisted=True, teardown="sandbox.delete", workspace_ids=["0123456789abcdef"],
        ),
        capability=CapabilityRecord(verdict=capability_verdict),
        attempts=records,
        seams=dict(seams or {}),
        injected_failure=injected_failure,
        secret_scan=SecretScanRecord(exact_value_scan="unavailable"),
        runner=RunnerRecord(tool_versions={"python": "0"}, argv=["pytest"], attempts_planned=attempts_planned),
        scope={"does_not_cover": ["机制测试"]},
    )
