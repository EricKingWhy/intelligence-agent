"""#735（P0-3）：工具调用进行态 → 完成态收敛状态机 + orphan 防御 + TTY 门控同行 spinner。

渲染器仍是「事件 → 文本」的纯函数：本票只加进程内 `bool`/timer 状态用于收敛配对，
不缓存业务事实（spec 11 §1/§6）。非 TTY 下输出与静态票面逐字节一致、零 `\\r`/ANSI 动画。
"""

import json
import sys
import time

from agent_harness.agent import AgentEvent
from agent_harness.cli import StreamRenderer, _format_duration
from agent_harness.cli_theme import Theme
from agent_harness.session import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_RESUMED,
    TOOL_CALL,
    TOOL_RESULT,
)

_SPINNER_FRAME_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _event(event_type: str, data: dict) -> AgentEvent:
    return AgentEvent(type=event_type, data=data, run_id="r1", step_id=1)


def _tool_call(tool: str = "bash", args: dict | None = None) -> AgentEvent:
    return _event(TOOL_CALL, {
        "tool_call_id": "c1",
        "tool_name": tool,
        "args": {"command": "ls"} if args is None else args,
    })


def _tool_result(ok: bool = True, duration_ms: float | None = None,
                 message: str = "") -> AgentEvent:
    content: dict = {"ok": ok, "message": message}
    if duration_ms is not None:
        content["metadata"] = {"duration_ms": duration_ms}
    return _event(TOOL_RESULT, {"tool_call_id": "c1", "content": json.dumps(content)})


def _wait_for_spinner(out: list[str], timeout: float = 3.0) -> str:
    """轮询直到 spinner 写出第一帧（比固定 sleep 抗机器负载抖动）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        joined = "".join(out)
        if "\r\033[K" in joined:
            return joined
        time.sleep(0.05)
    return "".join(out)


class TestPendingStateMachine:
    def test_pending_flag_lifecycle(self):
        renderer = StreamRenderer([].append)
        renderer.handle(_tool_call())
        assert renderer._pending_tool is True
        renderer.handle(_tool_result(ok=True, duration_ms=1000))
        assert renderer._pending_tool is False

    def test_orphan_on_run_completed(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_tool_call())
        renderer.handle(_event(RUN_COMPLETED, {
            "final_text": "x",
            "usage_total": {"prompt_tokens": 1, "completion_tokens": 2}}))
        joined = "".join(out)
        assert "(previous tool result not observed)" in joined
        # orphan 行出现在用量行之前（用量行文案归 P0-6，本断言只约束相对顺序）
        assert joined.index("(previous tool result not observed)") < joined.index("tokens:")
        assert renderer._pending_tool is False

    def test_orphan_on_run_failed(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_tool_call())
        renderer.handle(_event(RUN_FAILED, {"reason": "boom"}))
        joined = "".join(out)
        assert "(previous tool result not observed)" in joined
        assert joined.index("(previous tool result not observed)") < joined.index("[run failed]")
        assert renderer._pending_tool is False

    def test_orphan_on_run_paused(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_tool_call())
        renderer.handle(_event(RUN_PAUSED, {"reason": "budget", "trigger_dimension": "run"}))
        joined = "".join(out)
        assert "(previous tool result not observed)" in joined
        assert joined.index("(previous tool result not observed)") < joined.index("[run paused]")
        assert renderer._pending_tool is False

    def test_no_orphan_on_resume(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_tool_call())
        renderer.handle(_event(RUN_RESUMED, {}))
        assert "(previous tool result not observed)" not in "".join(out)
        assert renderer._pending_tool is True  # RUN_RESUMED 不清 pending

    def test_double_tool_call_emits_orphan(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_tool_call())
        renderer.handle(_tool_call(tool="read", args={"path": "x"}))
        joined = "".join(out)
        # orphan 行在第二行 running 行之前
        assert joined.index("(previous tool result not observed)") < joined.index("read")

    def test_result_without_call_no_orphan(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append)
        renderer.handle(_tool_result(ok=True, duration_ms=1200))  # 无前置 TOOL_CALL
        joined = "".join(out)
        assert "(previous tool result not observed)" not in joined
        assert joined == "  └ ● done · Took 1.2s\n"


def test_format_duration():
    assert _format_duration(100) == "0.1s"
    assert _format_duration(0) == "0.0s"
    assert _format_duration(61000) == "1m 1s"
    assert _format_duration(3661000) == "1h 1m 1s"
    # 59999/1000 = 59.999 → :.1f 得 "60.0s"（与 Pi toFixed(1) 一致）
    assert _format_duration(59999) == "60.0s"


def test_convergence_truecolor():
    out: list[str] = []
    renderer = StreamRenderer(out.append, theme=Theme(color="truecolor"))
    renderer.handle(_tool_result(ok=True, duration_ms=1200))
    first = out[0]
    # ok 角色真彩 = _RGB["ok"] = (135, 215, 135)；`●` 走 ok 角色。
    # （票面 §验收标准 test_convergence_truecolor 写的是 `\x1b[38;5;114m`，那是 256 色档
    # 的编码；truecolor 档按 _RGB 产出 `38;2;…`，两者语义同为 ok 绿——见交付报告的偏差登记。）
    assert "\x1b[38;2;135;215;135m●\x1b[0m" in first
    assert "Took 1.2s" in first


class _FakeStream:
    """伪造 isatty/encoding 的流，供 TERM 门控单测（不改真实 stdout）。"""

    def __init__(self, tty: bool) -> None:
        self._tty = tty
        self.encoding = "utf-8"

    def isatty(self) -> bool:
        return self._tty


class TestSpinnerGate:
    def test_no_animation_when_not_tty(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append, tty_animated=False)
        renderer.handle(_tool_call())
        time.sleep(1.2)  # 超过阈值也不该有动画
        renderer.handle(_tool_result(ok=True, duration_ms=1200))
        joined = "".join(out)
        assert "\r" not in joined
        assert joined == '\n● bash command=ls\n  └ ● done · Took 1.2s\n'

    def test_spinner_starts_after_threshold(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append, tty_animated=True)
        renderer.handle(_tool_call())
        joined = _wait_for_spinner(out)
        assert "\r\033[K" in joined
        assert any(ch in joined for ch in _SPINNER_FRAME_CHARS)
        renderer.handle(_tool_result(ok=True, duration_ms=1200))  # 终止，释放 timer

    def test_spinner_cleared_on_result(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append, tty_animated=True)
        renderer.handle(_tool_call())
        _wait_for_spinner(out)
        renderer.handle(_tool_result(ok=True, duration_ms=1200))
        joined = "".join(out)
        # 收敛行以 \r\033[K 擦除收尾，随后是收敛行，无残留帧
        tail = joined[joined.rindex("\r\033[K"):]
        assert tail == "\r\033[K  └ ● done · Took 1.2s\n"
        after = joined[joined.rindex("  └ ● done"):]
        assert not any(ch in after for ch in _SPINNER_FRAME_CHARS)

    def test_orphan_clears_spinner(self):
        out: list[str] = []
        renderer = StreamRenderer(out.append, tty_animated=True)
        renderer.handle(_tool_call())
        _wait_for_spinner(out)
        renderer.handle(_event(RUN_COMPLETED, {"final_text": "x"}))
        joined = "".join(out)
        tail = joined[joined.rindex("\r\033[K"):]
        assert tail.startswith("\r\033[K  └ (previous tool result not observed)\n")
        assert renderer._pending_tool is False

    def test_term_dumb_disables_animation(self, monkeypatch):
        monkeypatch.setenv("TERM", "dumb")
        monkeypatch.setattr(sys, "stdout", _FakeStream(tty=True))
        monkeypatch.setattr(sys, "stderr", _FakeStream(tty=True))
        assert StreamRenderer([].append)._tty_animated is False

    def test_double_tty_with_normal_term_enables_animation(self, monkeypatch):
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.setattr(sys, "stdout", _FakeStream(tty=True))
        monkeypatch.setattr(sys, "stderr", _FakeStream(tty=True))
        assert StreamRenderer([].append)._tty_animated is True

    def test_non_tty_stream_disables_animation(self, monkeypatch):
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.setattr(sys, "stdout", _FakeStream(tty=True))
        monkeypatch.setattr(sys, "stderr", _FakeStream(tty=False))
        assert StreamRenderer([].append)._tty_animated is False
