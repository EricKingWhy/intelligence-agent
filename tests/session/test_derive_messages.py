"""Ticket B — derive_messages 纯函数 + dangling 处理契约测试。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_harness.session import (
    DANGLING_TOOL_CONTENT,
    MODEL_COMPLETED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
    derive_messages,
    detect_dangling,
)


def _user(seq: int, sid: str, content: str) -> SessionEvent:
    return SessionEvent(seq=seq, type=USER_MESSAGE, session_id=sid, data={"content": content})


def _model(seq: int, sid: str, content: str, tool_calls=None) -> SessionEvent:
    data: dict = {"content": content}
    if tool_calls:
        data["tool_calls"] = tool_calls
    return SessionEvent(seq=seq, type=MODEL_COMPLETED, session_id=sid, data=data)


def _tool_call(seq: int, sid: str, tc_id: str, name: str, args: dict) -> SessionEvent:
    return SessionEvent(
        seq=seq,
        type=TOOL_CALL,
        session_id=sid,
        data={"tool_call_id": tc_id, "tool_name": name, "args": args},
    )


def _tool_result(seq: int, sid: str, tc_id: str, content: str) -> SessionEvent:
    return SessionEvent(
        seq=seq,
        type=TOOL_RESULT,
        session_id=sid,
        data={"tool_call_id": tc_id, "content": content},
    )


# ── 纯对话投影 ──


class TestPureConversation:
    def test_single_user_message(self):
        events = [_user(0, "s1", "你好")]
        messages = derive_messages(events)
        assert len(messages) == 1
        assert isinstance(messages[0], HumanMessage)
        assert messages[0].content == "你好"

    def test_user_then_model_reply(self):
        events = [
            _user(0, "s1", "你好"),
            _model(1, "s1", "你好！有什么可以帮你的？"),
        ]
        messages = derive_messages(events)
        assert len(messages) == 2
        assert isinstance(messages[0], HumanMessage)
        assert isinstance(messages[1], AIMessage)
        assert messages[1].content == "你好！有什么可以帮你的？"
        assert messages[1].tool_calls == []

    def test_multi_turn_conversation(self):
        events = [
            _user(0, "s1", "第一句"),
            _model(1, "s1", "回复1"),
            _user(2, "s1", "第二句"),
            _model(3, "s1", "回复2"),
        ]
        messages = derive_messages(events)
        assert len(messages) == 4
        assert messages[0].content == "第一句"
        assert messages[3].content == "回复2"


# ── 工具配对 ──


class TestToolPairing:
    def test_single_tool_call_result_paired(self):
        events = [
            _user(0, "s1", "算 1+1"),
            _model(
                1,
                "s1",
                "让我算一下",
                tool_calls=[{"id": "tc1", "name": "add", "args": {"a": 1, "b": 1}}],
            ),
            _tool_call(2, "s1", "tc1", "add", {"a": 1, "b": 1}),
            _tool_result(3, "s1", "tc1", "2"),
            _model(4, "s1", "1+1=2"),
        ]
        messages = derive_messages(events)
        assert len(messages) == 4
        assert isinstance(messages[1], AIMessage)
        assert len(messages[1].tool_calls) == 1
        assert messages[1].tool_calls[0]["id"] == "tc1"
        assert isinstance(messages[2], ToolMessage)
        assert messages[2].tool_call_id == "tc1"
        assert messages[2].content == "2"
        assert messages[3].content == "1+1=2"

    def test_multiple_tool_calls_single_turn(self):
        """一条 AIMessage 带多个 tool_calls，投影成 1 AIMessage + N ToolMessages。"""
        events = [
            _user(0, "s1", "同时读两个文件"),
            _model(
                1,
                "s1",
                "读取中",
                tool_calls=[
                    {"id": "tc1", "name": "read", "args": {"path": "a.txt"}},
                    {"id": "tc2", "name": "read", "args": {"path": "b.txt"}},
                ],
            ),
            _tool_call(2, "s1", "tc1", "read", {"path": "a.txt"}),
            _tool_call(3, "s1", "tc2", "read", {"path": "b.txt"}),
            _tool_result(4, "s1", "tc1", "内容A"),
            _tool_result(5, "s1", "tc2", "内容B"),
            _model(6, "s1", "两个文件都读完了"),
        ]
        messages = derive_messages(events)
        # HumanMessage + AIMessage(2 tool_calls) + ToolMessage + ToolMessage + AIMessage
        assert len(messages) == 5
        assert isinstance(messages[1], AIMessage)
        assert len(messages[1].tool_calls) == 2
        assert isinstance(messages[2], ToolMessage)
        assert messages[2].tool_call_id == "tc1"
        assert isinstance(messages[3], ToolMessage)
        assert messages[3].tool_call_id == "tc2"

    def test_tool_result_content_preserved(self):
        events = [
            _model(0, "s1", "", tool_calls=[{"id": "tc1", "name": "bash", "args": {}}]),
            _tool_result(1, "s1", "tc1", '{"exit_code": 0, "stdout": "hello"}'),
        ]
        messages = derive_messages(events)
        tool_msg = messages[1]
        assert tool_msg.content == '{"exit_code": 0, "stdout": "hello"}'


# ── Dangling 处理 ──


class TestDanglingToolCall:
    def test_dangling_injects_synthetic_tool_message(self):
        """有 tool_calls 但无 tool/result → 注入合成 ToolMessage。"""
        events = [
            _user(0, "s1", "算一下"),
            _model(
                1,
                "s1",
                "算",
                tool_calls=[{"id": "tc1", "name": "add", "args": {}}],
            ),
            # 进程在这里崩溃——没有 tool/result
        ]
        messages = derive_messages(events)
        # HumanMessage + AIMessage + 合成 ToolMessage
        assert len(messages) == 3
        assert isinstance(messages[2], ToolMessage)
        assert messages[2].tool_call_id == "tc1"
        assert messages[2].content == DANGLING_TOOL_CONTENT

    def test_dangling_only_for_unresolved_calls(self):
        """部分 tool_call 有 result 部分没有 → 只为未解决的注入合成。"""
        events = [
            _model(
                0,
                "s1",
                "",
                tool_calls=[
                    {"id": "tc1", "name": "read", "args": {}},
                    {"id": "tc2", "name": "read", "args": {}},
                ],
            ),
            _tool_result(1, "s1", "tc1", "文件内容"),
            # tc2 无 result
        ]
        messages = derive_messages(events)
        # AIMessage + ToolMessage(real tc1) + ToolMessage(synthetic tc2)
        assert len(messages) == 3
        assert messages[1].tool_call_id == "tc1"
        assert messages[1].content == "文件内容"
        assert messages[2].tool_call_id == "tc2"
        assert messages[2].content == DANGLING_TOOL_CONTENT

    def test_no_dangling_when_all_resolved(self):
        events = [
            _model(0, "s1", "", tool_calls=[{"id": "tc1", "name": "read", "args": {}}]),
            _tool_result(1, "s1", "tc1", "done"),
        ]
        messages = derive_messages(events)
        assert len(messages) == 2  # AIMessage + ToolMessage, no synthetic

    def test_detect_dangling_returns_unresolved_ids(self):
        events = [
            _model(
                0,
                "s1",
                "",
                tool_calls=[
                    {"id": "tc1", "name": "read", "args": {}},
                    {"id": "tc2", "name": "read", "args": {}},
                ],
            ),
            _tool_call(1, "s1", "tc1", "read", {}),
            _tool_call(2, "s1", "tc2", "read", {}),
            _tool_result(3, "s1", "tc1", "done"),
        ]
        dangling = detect_dangling(events)
        assert dangling == ["tc2"]

    def test_detect_dangling_empty_when_all_resolved(self):
        events = [
            _tool_call(0, "s1", "tc1", "read", {}),
            _tool_result(1, "s1", "tc1", "done"),
        ]
        assert detect_dangling(events) == []


# ── 混合场景 ──


class TestMixedScenarios:
    def test_full_conversation_with_tools(self):
        events = [
            _user(0, "s1", "帮我写个文件"),
            _model(
                1,
                "s1",
                "好的",
                tool_calls=[{"id": "tc1", "name": "write", "args": {"path": "a.txt"}}],
            ),
            _tool_call(2, "s1", "tc1", "write", {"path": "a.txt"}),
            _tool_result(3, "s1", "tc1", "写入成功"),
            _model(4, "s1", "文件已写入"),
            _user(5, "s1", "读出来看看"),
            _model(
                6,
                "s1",
                "好的",
                tool_calls=[{"id": "tc2", "name": "read", "args": {"path": "a.txt"}}],
            ),
            _tool_call(7, "s1", "tc2", "read", {"path": "a.txt"}),
            _tool_result(8, "s1", "tc2", "文件内容"),
            _model(9, "s1", "这是文件内容"),
        ]
        messages = derive_messages(events)
        # 2 Human + 4 AIMessage + 2 ToolMessage = 8
        assert len(messages) == 8
        assert isinstance(messages[0], HumanMessage)
        assert isinstance(messages[1], AIMessage)
        assert isinstance(messages[2], ToolMessage)
        assert isinstance(messages[7], AIMessage)

    def test_derive_does_not_mutate_input(self):
        events = [
            _user(0, "s1", "test"),
            _model(1, "s1", "reply"),
        ]
        original_lens = len(events)
        derive_messages(events)
        assert len(events) == original_lens
