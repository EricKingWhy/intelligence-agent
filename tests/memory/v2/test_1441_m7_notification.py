"""#1441 §3（F-13b）：M7 人面向通知——三档、去重、只认成功、待审不报成落地、载荷只带计数/指纹。

纯函数（`summarize_background_review_actions` / `review_messages_for`）直接钉语义；
事件出口用 `MemoryJobRunner._notify_human` 直调 + 记录型 sink。最后一条用例补上真实
`_run_one` 路径（注入只返回已提交结果的假 executor），钉住 `runner.py` 里那唯一一行
`self._notify_human(job, result)`——删掉它，直调用例全绿而这条会红。
"""

from __future__ import annotations

import json

import pytest

from agent_harness.memory.v2.executor import MemoryJobResult
from agent_harness.memory.v2.jobs import (
    MemoryFormationJob,
    MemoryJobOutcome,
    MemoryJobStage,
    MemoryV2JobStore,
)
from agent_harness.memory.v2.journey import memory_fingerprint
from agent_harness.memory.v2.notification import (
    review_messages_for,
    summarize_background_review_actions,
)
from agent_harness.memory.v2.roles import MemoryModelRoles
from agent_harness.memory.v2.runner import MemoryJobRunner
from agent_harness.model.config import ModelConfig
from agent_harness.session import MEMORY_HUMAN_UPDATED
from agent_harness.session.store import JsonlSessionStore
from tests.memory.v2._records import make_record


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, str | None]] = []

    def emit(self, event_type: str, data: dict, *, run_id: str | None = None) -> None:
        self.events.append((event_type, data, run_id))


def _tool_result(call_id: str, *, message: str, target: str = "memory", **extra) -> list[dict]:
    """一对 assistant tool_call + tool 结果（上游 review 消息形状）。"""
    arguments = {"action": "add", "target": target, "content": extra.pop("content", "x")}
    payload = {"success": True, "target": target, "message": message, **extra}
    return [
        {"role": "assistant", "tool_calls": [{
            "id": call_id,
            "function": {"name": "memory", "arguments": json.dumps(arguments)},
        }]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload)},
    ]


# ---------------------------------------------------------------- 三档


def test_off_mode_reports_nothing() -> None:
    messages = _tool_result("c1", message="Entry added.")
    assert summarize_background_review_actions(messages, [], "off") == []


def test_on_mode_is_one_generic_sentence_without_content() -> None:
    messages = _tool_result("c1", message="Entry added.", content="绝密偏好内容")
    lines = summarize_background_review_actions(messages, [], "on")

    assert lines == ["Memory updated"]
    assert all("绝密偏好内容" not in line for line in lines), "on 档不得泄露内容"


def test_verbose_mode_previews_content_and_old_text() -> None:
    add = _tool_result("c1", message="Entry added.", content="用 pnpm 安装依赖")
    remove_payload = {
        "role": "tool", "tool_call_id": "c2",
        "content": json.dumps({"success": True, "target": "memory", "message": "Entry removed."}),
    }
    call = {
        "role": "assistant", "tool_calls": [{
            "id": "c2",
            "function": {"name": "memory", "arguments": json.dumps(
                {"action": "remove", "target": "memory", "old_text": "旧偏好"})},
        }],
    }
    lines = summarize_background_review_actions([*add, call, remove_payload], [], "verbose")

    assert lines[0] == "Memory ➕ 用 pnpm 安装依赖"
    assert lines[1] == "Memory ➖ 旧偏好"


def test_verbose_preview_is_truncated_to_upstream_limits() -> None:
    messages = _tool_result("c1", message="Entry added.", content="长" * 200)
    line = summarize_background_review_actions(messages, [], "verbose")[0]

    # add/replace 取 120 字符 + 省略号（上游 `_MEMORY_OP_FORMATS`）。
    assert line == "Memory ➕ " + "长" * 120 + "…"


# ---------------------------------------------------------------- 去重 / 只认成功 / staged


def test_prior_snapshot_results_are_not_reported() -> None:
    """继承来的 tool 结果不是这一轮的新工作（按 tool_call_id 去重）。"""
    messages = _tool_result("c1", message="Entry added.")
    prior = [messages[1]]
    assert summarize_background_review_actions(messages, prior, "on") == []


def test_prior_snapshot_without_ids_dedupes_by_content() -> None:
    """没有 `tool_call_id` 的旧形状 tool 消息按内容去重（上游同一判据）。"""
    messages = _tool_result("c1", message="Entry added.")
    current = dict(messages[1])
    current.pop("tool_call_id")
    prior = [dict(current)]
    assert summarize_background_review_actions([current], prior, "on") == []


def test_failed_results_are_not_reported() -> None:
    messages = _tool_result("c1", message="Entry added.")
    messages[1]["content"] = json.dumps({"success": False, "message": "Entry added."})
    assert summarize_background_review_actions(messages, [], "on") == []


def test_staged_write_is_never_reported_as_landed() -> None:
    """Owlet 的 stage 是待审写入：没有 `proposal_staged` 就一行都不报。"""
    messages = _tool_result("c1", message="待审：新增一条", staged=True)
    assert summarize_background_review_actions(messages, [], "on") == []
    assert summarize_background_review_actions(messages, [], "verbose") == []


def test_non_notify_tools_are_not_counted_as_memory_work() -> None:
    messages = _tool_result("c1", message="Entry added.")
    messages[0]["tool_calls"][0]["function"]["name"] = "bash"
    assert summarize_background_review_actions(messages, [], "on") == []


# ---------------------------------------------------------------- 事件载荷（不变量 #22）


def _job(*, job_id: str = "job-1", run_id: str | None = "run-1") -> MemoryFormationJob:
    return MemoryFormationJob(
        job_id=job_id, idempotency_key="k", tenant_id="local", user_id="local",
        project_id=None, session_id="session-1", run_id=run_id,
        stage=MemoryJobStage.COMPLETED, state={}, outcome=MemoryJobOutcome.COMMITTED,
        reason=None, lease_owner=None, lease_expires_at=None,
        created_at="2026-10-19T00:00:00+00:00", updated_at="2026-10-19T00:00:00+00:00",
    )


async def _runner(tmp_path, sink: RecordingSink, mode: str) -> MemoryJobRunner:
    jobs = MemoryV2JobStore(tmp_path / "memory-v2.db")
    await jobs.initialize()
    return MemoryJobRunner(
        jobs=jobs, sessions=JsonlSessionStore(tmp_path / "sessions"), executor=object(),
        roles=MemoryModelRoles(primary=ModelConfig(
            provider="p", model_name="m", api_key="k", base_url="http://localhost:1",
            temperature=0.0), fallback=None),
        sink=sink, notification_mode=mode,
    )


def _result(*records) -> MemoryJobResult:
    return MemoryJobResult(
        job_id="job-1", stage=MemoryJobStage.COMPLETED,
        outcome=MemoryJobOutcome.COMMITTED, reason=None, written=tuple(records),
    )


@pytest.mark.asyncio
async def test_event_payload_carries_only_counts_fingerprints_and_pointers(tmp_path) -> None:
    sink = RecordingSink()
    runner = await _runner(tmp_path, sink, "verbose")
    record = make_record(memory_id="mem-1", content="用户偏好简洁直接的回答")

    runner._notify_human(_job(), _result(record))

    assert [event[0] for event in sink.events] == [MEMORY_HUMAN_UPDATED]
    _, data, run_id = sink.events[0]
    assert set(data) == {"count", "memory_ids", "fingerprints", "job_id"}
    assert data["count"] == 1
    assert data["memory_ids"] == ["mem-1"]
    assert data["fingerprints"] == [memory_fingerprint(record.content)]
    assert run_id == "run-1"
    assert "用户偏好简洁直接的回答" not in json.dumps(data, ensure_ascii=False)


@pytest.mark.asyncio
async def test_off_mode_emits_no_event(tmp_path) -> None:
    sink = RecordingSink()
    runner = await _runner(tmp_path, sink, "off")
    runner._notify_human(_job(), _result(make_record(memory_id="mem-1")))

    assert sink.events == []


@pytest.mark.asyncio
async def test_zero_write_and_non_committed_results_emit_nothing(tmp_path) -> None:
    sink = RecordingSink()
    runner = await _runner(tmp_path, sink, "on")

    runner._notify_human(_job(), None)
    runner._notify_human(_job(), _result())
    runner._notify_human(_job(), MemoryJobResult(
        job_id="job-1", stage=MemoryJobStage.COMPLETED,
        outcome=MemoryJobOutcome.NO_WRITE, reason=None, written=(make_record(),)))

    assert sink.events == [], "安静成功 / 非提交终态都不得装成『更新了』"


@pytest.mark.asyncio
async def test_review_messages_render_committed_records_as_successful_entries() -> None:
    messages = review_messages_for((make_record(memory_id="mem-1", content="甲"),))
    assert [m["role"] for m in messages] == ["assistant", "tool"]
    payload = json.loads(messages[1]["content"])
    assert payload == {"success": True, "target": "memory", "message": "Entry added."}


# ---------------------------------------------------------------- 真实接线路径


class _FakeExecutor:
    """只跑到 `_notify_human`：返回一个已提交结果，其余参数原样忽略。"""

    def __init__(self, result: MemoryJobResult) -> None:
        self._result = result
        self.calls = 0

    async def run(self, job, **_kwargs) -> MemoryJobResult:
        self.calls += 1
        return self._result


@pytest.mark.asyncio
async def test_run_one_wires_the_notification_to_the_event_sink(tmp_path) -> None:
    """`runner.py` 的 `self._notify_human(job, result)` 是唯一接线行：删掉它本用例必须红。

    直调 `_notify_human` 的用例证明不了这件事——它们绕过了 `_run_one`。
    """
    sink = RecordingSink()
    jobs = MemoryV2JobStore(tmp_path / "memory-v2.db")
    await jobs.initialize()
    record = make_record(memory_id="mem-1", content="用户偏好简洁直接的回答")
    executor = _FakeExecutor(_result(record))
    runner = MemoryJobRunner(
        jobs=jobs, sessions=JsonlSessionStore(tmp_path / "sessions"), executor=executor,
        roles=MemoryModelRoles(primary=ModelConfig(
            provider="p", model_name="m", api_key="k", base_url="http://localhost:1",
            temperature=0.0), fallback=None),
        sink=sink, notification_mode="on",
    )

    result = await runner._run_one(_job())

    assert executor.calls == 1
    assert result is executor._result
    assert [event[0] for event in sink.events] == [MEMORY_HUMAN_UPDATED]
    _, data, run_id = sink.events[0]
    assert data["memory_ids"] == ["mem-1"]
    assert run_id == "run-1"
