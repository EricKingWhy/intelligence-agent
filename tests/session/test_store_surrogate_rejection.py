"""#650 回归：持久化写入边界对孤立 Unicode surrogate 的受控拒绝。

票面 T6f（issue #650）：``JsonlSessionStore.append_event`` 的
``json.dumps(..., ensure_ascii=False)`` 产物经 UTF-8 文本 write 才失败
（裸 ``UnicodeEncodeError``），且失败发生在 ``mkdir`` / 撕裂尾中立化**之后**
——违反「UTF-8 可编码性验证必须发生在目录/文件变更及 seq 状态提交之前」。

修复契约（票面）：校验前置到任何目录/文件变更之前；受控拒绝用明确的
``ValueError``（``session/errors.py`` 不在本票文件边界内，不新增 typed 类）。
拒绝后 ``events.jsonl`` 字节与拒绝前完全相同；失败不占号——合法 seq=N+1
追加仍成功（与 ``Session._persist_event``「store 成功后才推进计数器」的既有
契约衔接）。错误消息不回显用户内容（AC4）。
"""

from __future__ import annotations

import json

import pytest

from agent_harness.session import USER_MESSAGE, JsonlSessionStore, SessionEvent


def _user_event(session_id: str, seq: int, content: str) -> SessionEvent:
    return SessionEvent(
        type=USER_MESSAGE,
        session_id=session_id,
        seq=seq,
        data={"content": content},
    )


def _events_bytes(root, session_id: str) -> bytes | None:
    path = root / session_id / "events.jsonl"
    if not path.exists():
        return None
    return path.read_bytes()


class TestSurrogateWriteRejection:
    @staticmethod
    def _assert_controlled_rejection(excinfo) -> None:
        """受控拒绝 ≠ 裸 codec 异常。

        ``UnicodeEncodeError`` 是 ``ValueError`` 的子类：只断言
        ``pytest.raises(ValueError)`` 会把「写时才炸」的原始症状放过绿
        （红环实测），必须显式排除，红测才命中真实缺陷形状。
        """
        assert not isinstance(excinfo.value, UnicodeEncodeError)

    def test_illegal_first_append_rejected_before_directory_creation(self, tmp_path):
        store = JsonlSessionStore(root=tmp_path)
        with pytest.raises(ValueError) as excinfo:
            store.append_event(
                "s-sur", _user_event("s-sur", 0, "ok" + chr(0xD800))
            )
        self._assert_controlled_rejection(excinfo)
        # 校验先于目录/文件变更：会话目录与 events.jsonl 都不得被创建
        assert not (tmp_path / "s-sur").exists()

    def test_rejection_keeps_file_bytes_identical_and_seq_unoccupied(self, tmp_path):
        store = JsonlSessionStore(root=tmp_path)
        sid = "s-seq"
        store.append_event(sid, _user_event(sid, 0, "合法中文 🌏 ok"))
        before = _events_bytes(tmp_path, sid)
        assert before is not None

        with pytest.raises(ValueError) as excinfo:
            store.append_event(sid, _user_event(sid, 1, chr(0xDC00)))
        self._assert_controlled_rejection(excinfo)

        # AC2：拒绝后文件字节与拒绝前完全相同（不修改旧 JSONL）
        assert _events_bytes(tmp_path, sid) == before
        # 失败未占号：合法 seq=1 仍成功
        store.append_event(sid, _user_event(sid, 1, "legal after reject"))
        lines = (
            (tmp_path / sid / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert len(lines) == 2
        assert json.loads(lines[1])["seq"] == 1
        assert json.loads(lines[1])["data"]["content"] == "legal after reject"

    def test_error_message_does_not_echo_user_content(self, tmp_path):
        store = JsonlSessionStore(root=tmp_path)
        marker = "SECRET-" + chr(0xD800) + "-PAYLOAD"
        with pytest.raises(ValueError) as excinfo:
            store.append_event("s-echo", _user_event("s-echo", 0, marker))
        self._assert_controlled_rejection(excinfo)
        assert "SECRET-" not in str(excinfo.value)

    def test_rejection_preserves_codec_error_as_cause(self, tmp_path):
        """审查 P3 建议：``__cause__`` 保留 codec 异常，根因可追溯（诊断面）。"""
        store = JsonlSessionStore(root=tmp_path)
        with pytest.raises(ValueError) as excinfo:
            store.append_event("s-cause", _user_event("s-cause", 0, chr(0xD800)))
        self._assert_controlled_rejection(excinfo)
        assert isinstance(excinfo.value.__cause__, UnicodeEncodeError)

    def test_surrogate_in_dict_key_rejected_too(self, tmp_path):
        """审查 P3 建议：键位 surrogate 同样被前置校验拦截（encode 对键值一视同仁）。"""
        store = JsonlSessionStore(root=tmp_path)
        event = SessionEvent(
            type=USER_MESSAGE,
            session_id="s-key",
            seq=0,
            data={chr(0xD800) + "-key": "value"},
        )
        with pytest.raises(ValueError) as excinfo:
            store.append_event("s-key", event)
        self._assert_controlled_rejection(excinfo)
        assert not (tmp_path / "s-key").exists()


class TestLegalUnicodeRoundTrip:
    """AC3 / AC4：合法内容（emoji、中文、换行控制转义）准确往返、不丢字符。"""

    def test_legal_content_round_trips_through_jsonl(self, tmp_path):
        store = JsonlSessionStore(root=tmp_path)
        sid = "s-round"
        content = "中文 😀 \n\t\"引号\" \\backslash"
        store.append_event(sid, _user_event(sid, 0, content))
        events = store.read_events(sid)
        assert events[0].data["content"] == content
