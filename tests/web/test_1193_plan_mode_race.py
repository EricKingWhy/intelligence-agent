"""#1193：plan 模式两处语义缺陷的回归钉面。

缺陷一（先 launch、后切 mode 的竞态）：`POST /api/sessions`（autonomy=plan）
此前在 `create_and_launch` 返回后才补写 `workflow/mode-changed`（plan），而 run
task 已随 launch 启动 ⇒ 竞态窗口内写工具以 NORMAL 档放行。修复后 plan 档是
run 的**初始条件**：`workflow/mode-changed`（plan）的 seq 严格先于任何
`run/started`，且窗口内写工具被 plan 门禁拦截（PERMISSION_DENIED）。

缺陷二（致命异常无明确 ERROR 事件）：run 启动期致命异常（plan 从未生成）时，
mode **保持 plan**（不自动回滚，fail-closed），异常臂向前端推送 `error` 事件
（code=run-failed-before-plan，含手动退出 plan 模式的指引）；用户显式退出
plan 后写工具恢复可用。计划已批准（mode=normal）的执行期崩溃**不多写**任何
mode 变更事件、不推该 error 事件。

注入机制全部是测试侧 monkeypatch（可复现、不依赖手工断点）：
- `_delay_after_launch`：包装 `SessionService.create_and_launch`，在返回前注入
  延迟——把修复前「launch 之后、mode 落盘之前」的竞态窗口放大成确定性窗口；
- `_slow_begin_run`：包装 `Session.begin_run`，在 run 启动前注入延迟；
- `_raise_at_begin_run`：让 `Session.begin_run` 抛致命异常（模拟 SeqConflict
  等启动期崩溃：没有 run/started、没有 run/failed 的悬空失败）。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from agent_harness.config import Settings
from agent_harness.session.service import SessionService
from agent_harness.session.session import Session
from agent_harness.web.app import create_app
from tests.scripted_model import ScriptedModel

PLAN_GATE_MESSAGE = "会话处于 Plan 模式"
PLAN_MODE_EVENT = "workflow/mode-changed"


class _ModelProbe:
    """按剧本依次给每个会话装配 ScriptedModel（同 #556 测试的既有探针形状）。"""

    def __init__(self, scripts: list[list[AIMessage]]) -> None:
        self._scripts = list(scripts)
        self.models: list[ScriptedModel] = []
        self._patcher: Any = None

    def __enter__(self):
        def factory(config: Any, **kwargs: Any) -> ScriptedModel:
            model = ScriptedModel(responses=self._scripts.pop(0))
            self.models.append(model)
            return model

        self._patcher = patch(
            "agent_harness.assembly.create_chat_model", side_effect=factory
        )
        self._patcher.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        self._patcher.stop()
        return False


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None, workspace_dir=str(tmp_path), model_api_key="sk-test"
    )
    return TestClient(create_app(settings, enable_cors=False))


def _create_payload(task: str = "先出计划") -> dict[str, Any]:
    # 显式 local 后端：本套件不触达 Docker / 平台默认解析（与 plan 语义无关）。
    return {
        "task": task,
        "autonomy": "plan",
        "sandbox_backend": "local",
        "budget": {"local": {"max_agent_turns": 6}},
    }


def _frames(resp: Any) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data:"):].strip())
        for line in resp.text.splitlines()
        if line.startswith("data:")
    ]


def _session_id(resp: Any) -> str:
    frames = _frames(resp)
    sid = next((f["session_id"] for f in frames if f.get("session_id")), None)
    assert sid, f"SSE 流里没有 session_id：{frames[:3]}"
    return str(sid)


def _events(client: TestClient, session_id: str) -> list[dict[str, Any]]:
    resp = client.get(f"/api/sessions/{session_id}/events")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _plan_mode_seqs(events: list[dict[str, Any]]) -> list[int]:
    return [
        int(e["seq"])
        for e in events
        if e["type"] == PLAN_MODE_EVENT and e["data"].get("mode") == "plan"
    ]


def _mode_values(events: list[dict[str, Any]]) -> list[str]:
    return [
        str(e["data"].get("mode"))
        for e in events
        if e["type"] == PLAN_MODE_EVENT
    ]


def _write_tool_call(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "id": call_id,
                "name": "write",
                "args": {"path": "note.txt", "content": "hello"},
            }
        ],
    )


def _tool_result(client: TestClient, session_id: str, call_id: str) -> dict[str, Any]:
    events = _events(client, session_id)
    event = next(
        e
        for e in events
        if e["type"] == "tool/result" and e["data"]["tool_call_id"] == call_id
    )
    return json.loads(event["data"]["content"])


def _error_frames(resp: Any) -> list[dict[str, Any]]:
    return [
        f
        for f in _frames(resp)
        if f["type"] == "error"
        and f["data"].get("code") == "run-failed-before-plan"
    ]


def _boom_begin_run(self, **kwargs):
    raise RuntimeError("injected begin_run failure")


class TestPlanModeIsInitialCondition:
    """缺陷一：plan 档必须先于 run 启动落盘（顺序钉面 + 竞态窗口拦截钉面）。"""

    def test_plan_mode_event_precedes_run_started(self, tmp_path: Path):
        client = _client(tmp_path)
        with _ModelProbe([[AIMessage(content="ok")]]):
            resp = client.post("/api/sessions", json=_create_payload())
        assert resp.status_code == 200, resp.text
        sid = _session_id(resp)

        events = _events(client, sid)
        types = [e["type"] for e in events]
        assert "run/started" in types, f"run 未启动：{types}"

        plan_seqs = _plan_mode_seqs(events)
        assert plan_seqs, f"事件流里没有 plan 档切换：{types}"
        run_started_seq = next(e["seq"] for e in events if e["type"] == "run/started")
        assert max(plan_seqs) < run_started_seq, (
            "plan 档必须在 run 启动前落盘（plan 是 run 的初始条件，"
            f"不是事后补丁）：mode seq={plan_seqs} run/started seq={run_started_seq}"
        )
        # 只写一条：旧的「创建后再补切」路径不得残留（否则会多一条迟到的事件）。
        assert _mode_values(events) == ["plan"]

    def test_write_tool_denied_even_with_delayed_create_return(
        self, tmp_path: Path
    ):
        """放大修复前的竞态窗口（launch 之后、mode 落盘之前）：写工具仍被拦截。"""
        client = _client(tmp_path)
        original = SessionService.create_and_launch

        async def delayed(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            # 修复前 mode 的写入发生在 create_and_launch 返回**之后**——这段
            # 延迟把那个窗口拉长成确定性窗口；修复后 plan 档已在 launch 前落盘，
            # 延迟不再有语义。
            await asyncio.sleep(0.3)
            return result

        with (
            patch.object(SessionService, "create_and_launch", delayed),
            _ModelProbe(
                [[_write_tool_call("call-write-window"), AIMessage(content="done")]]
            ),
        ):
            resp = client.post("/api/sessions", json=_create_payload())
        assert resp.status_code == 200, resp.text
        sid = _session_id(resp)

        result = _tool_result(client, sid, "call-write-window")
        assert result["ok"] is False, f"竞态窗口内写工具被放行：{result}"
        assert result["error_code"] == "PERMISSION_DENIED"
        assert PLAN_GATE_MESSAGE in result["message"]

        events = _events(client, sid)
        plan_seqs = _plan_mode_seqs(events)
        run_started_seq = next(e["seq"] for e in events if e["type"] == "run/started")
        assert max(plan_seqs) < run_started_seq

    def test_write_tool_denied_with_slow_begin_run(self, tmp_path: Path):
        """`begin_run` 前注入延迟（run 启动被拖慢）：写工具仍被拦截。"""
        client = _client(tmp_path)
        original_begin_run = Session.begin_run

        def slow_begin_run(self, **kwargs):
            # 同步阻塞把 run 启动推迟到下一个调度点之后——修复前这正好让
            # 「先 launch 后切 mode」的窗口张开；修复后窗口不存在。
            time.sleep(0.05)
            return original_begin_run(self, **kwargs)

        with (
            patch.object(Session, "begin_run", slow_begin_run),
            _ModelProbe(
                [[_write_tool_call("call-write-slow"), AIMessage(content="done")]]
            ),
        ):
            resp = client.post("/api/sessions", json=_create_payload())
        assert resp.status_code == 200, resp.text
        sid = _session_id(resp)

        result = _tool_result(client, sid, "call-write-slow")
        assert result["ok"] is False, f"run 启动窗口内写工具被放行：{result}"
        assert result["error_code"] == "PERMISSION_DENIED"

        events = _events(client, sid)
        plan_seqs = _plan_mode_seqs(events)
        run_started_seq = next(e["seq"] for e in events if e["type"] == "run/started")
        assert max(plan_seqs) < run_started_seq


class TestRunFailedBeforePlanSurface:
    """缺陷二：致命异常时 mode 保持 plan + 明确 ERROR 事件 + 手动退出可恢复。"""

    def test_fatal_start_failure_keeps_plan_and_emits_error_event(
        self, tmp_path: Path
    ):
        client = _client(tmp_path)
        with (
            patch.object(Session, "begin_run", _boom_begin_run),
            _ModelProbe([[AIMessage(content="unused")]]),
        ):
            resp = client.post("/api/sessions", json=_create_payload())
        assert resp.status_code == 200, resp.text
        sid = _session_id(resp)

        errors = _error_frames(resp)
        assert len(errors) == 1, f"应有恰好一条 run-failed-before-plan：{_frames(resp)}"
        data = errors[0]["data"]
        assert data["workflow_mode"] == "plan"
        assert "手动退出" in data["hint"]
        assert data["message"], "message 必须带可读原因"
        # durable（#1193 独立审查 F2）：致命启动失败发生在 run 起点，前端接流总晚
        # 一步（认领轮询 ≥150ms / 交付层攒包更晚）——瞬时帧发出时没有订阅者，必丢，
        # 前端照样退回「连接中断」重试。落盘后迟到接流的 WS 快照与刷新后的
        # GET /events 都能读到，前端横幅成为事件流投影。
        assert isinstance(errors[0]["seq"], int), f"必须落盘（带 seq）：{errors[0]}"
        assert errors[0]["durability"] == "durable"

        events = _events(client, sid)
        types = [e["type"] for e in events]
        assert "run/started" not in types, f"异常发生在 run 启动前：{types}"
        assert "task/plan_updated" not in types
        # 不自动回滚：mode 保持 plan，没有 normal 事件。
        assert _mode_values(events) == ["plan"]
        # 同一事实在 durable 读面上可见——这正是 F2 的判据（无订阅者在场时帧必丢）。
        durable_errors = [e for e in events if e["type"] == "error"]
        assert len(durable_errors) == 1, f"durable log 必须含该事件：{types}"
        assert durable_errors[0]["data"]["code"] == "run-failed-before-plan"
        assert durable_errors[0]["seq"] == errors[0]["seq"]

    def test_durable_append_failure_degrades_to_transient_frame(
        self, tmp_path: Path
    ):
        """落盘失败兜底：不得顶替原始异常，且在场的订阅者仍收到瞬时帧。

        durable 化（审查 F2）把信号从流挪进日志，代价是异常臂里多了一次 IO——
        这条用例钉住那次 IO 失败时的降级形状（stages 契约：第一处异常在全部
        收尾跑完后**原样**再抛；不得被 OSError 顶替，也不得静默丢弃信号）。"""
        client = _client(tmp_path)
        original_append = Session.append

        def failing_append(self, event_type, data, **kwargs):
            if event_type == "error":
                raise OSError("injected durable write failure")
            return original_append(self, event_type, data, **kwargs)

        with (
            patch.object(Session, "begin_run", _boom_begin_run),
            patch.object(Session, "append", failing_append),
            _ModelProbe([[AIMessage(content="unused")]]),
        ):
            resp = client.post("/api/sessions", json=_create_payload())
        assert resp.status_code == 200, resp.text

        errors = _error_frames(resp)
        assert len(errors) == 1, f"落盘失败仍须推出信号：{_frames(resp)}"
        assert errors[0]["seq"] is None, "退化为瞬时帧（没有 seq 可分配）"
        # #1280 AC2：降级帧带显式 degraded 标记自描述——durable 型却无 seq 的帧
        # 只允许这一种形状（golden 不变式「seq=None ⟺ STREAM_ONLY_TYPES ∪
        # 带 degraded 标记的存储降级帧」的生产发射点就在这里）。
        assert errors[0]["data"].get("degraded") is True, errors[0]

        sid = _session_id(resp)
        events = _events(client, sid)
        types = [e["type"] for e in events]
        assert "error" not in types, f"落盘失败就不该出现在 durable log：{types}"
        # 不回滚 mode 的语义与落盘成败无关。
        assert _mode_values(events) == ["plan"]

    def test_manual_exit_plan_restores_write_tools(self, tmp_path: Path):
        """用户显式退出 plan 后，写操作工具恢复可用（端到端恢复路径）。"""
        client = _client(tmp_path)
        with (
            patch.object(Session, "begin_run", _boom_begin_run),
            _ModelProbe([[AIMessage(content="unused")]]),
        ):
            resp = client.post("/api/sessions", json=_create_payload())
        assert resp.status_code == 200, resp.text
        sid = _session_id(resp)
        assert _error_frames(resp), "前置条件：崩溃 run 必须推出 run-failed-before-plan"

        # 手动退出 plan（显式操作，非自动回滚）。
        exit_resp = client.post(
            f"/api/sessions/{sid}/workflow-mode", json={"mode": "normal"}
        )
        assert exit_resp.status_code == 200, exit_resp.text

        # 恢复后续聊：写工具在 normal 档下正常执行。
        with _ModelProbe(
            [[_write_tool_call("call-write-after-exit"), AIMessage(content="done")]]
        ):
            resumed = client.post(
                f"/api/sessions/{sid}/messages",
                json={"content": "继续执行", "budget": {"local": {"max_agent_turns": 6}}},
            )
        assert resumed.status_code == 200, resumed.text

        result = _tool_result(client, sid, "call-write-after-exit")
        assert result["ok"] is True, f"退出 plan 后写工具仍被拦截：{result}"


class TestCrashAfterPlanApprovedRegression:
    """AC 回归：计划已批准（mode=normal）的执行期崩溃不多写 mode 事件、不推 error。"""

    def test_approved_plan_crash_adds_no_mode_events_and_no_error_event(
        self, tmp_path: Path
    ):
        client = _client(tmp_path)
        with _ModelProbe([[AIMessage(content="探索完成")]]):
            resp = client.post("/api/sessions", json=_create_payload())
        assert resp.status_code == 200, resp.text
        sid = _session_id(resp)

        # 模拟「计划已生成 + 已批准」：追加 plan_updated（服务层同源写入口）+
        # 批准动作把档位切回 normal（POST /workflow-mode，前端批准路径的等价动作）。
        from agent_harness.session.event import TASK_PLAN_UPDATED

        Session.append_event(
            client.app.state.agent.store,
            sid,
            TASK_PLAN_UPDATED,
            {
                "items": [
                    {
                        "id": "t1",
                        "content": "执行计划",
                        "activeForm": "执行计划中",
                        "status": "pending",
                        "source": "agent",
                    }
                ]
            },
        )
        approve = client.post(
            f"/api/sessions/{sid}/workflow-mode", json={"mode": "normal"}
        )
        assert approve.status_code == 200, approve.text

        # 执行期崩溃：模型调用失败（剧本空 = 首次调用即抛错），run 已启动 ⇒
        # run/failed 走既有面；本票的 error 事件与 mode 变更都**不得**出现。
        with _ModelProbe([[]]):
            crashed = client.post(
                f"/api/sessions/{sid}/messages",
                json={"content": "开始执行", "budget": {"local": {"max_agent_turns": 6}}},
            )
        assert crashed.status_code == 200, crashed.text

        events = _events(client, sid)
        assert _mode_values(events) == ["plan", "normal"], (
            "批准后的崩溃不得多写 mode 变更事件："
            f"{[e for e in events if e['type'] == PLAN_MODE_EVENT]}"
        )
        assert _error_frames(crashed) == [], "mode=normal 的崩溃不得推 run-failed-before-plan"
        # 崩溃本身仍是既有失败面（run/failed），不被本票吞掉。
        assert any(e["type"] == "run/failed" for e in events)
