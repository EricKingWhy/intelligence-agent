"""Ticket C — Session 聚合根生命周期契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.session import (
    DANGLING_TOOL_CONTENT,
    MODEL_COMPLETED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    SESSION_RESUMED,
    SESSION_STARTED,
    TOOL_CALL,
    TOOL_RESULT,
    USER_MESSAGE,
    JsonlSessionStore,
    Session,
)
from agent_harness.session.errors import SessionNotFound


@pytest.fixture
def store(tmp_path: Path) -> JsonlSessionStore:
    return JsonlSessionStore(root=tmp_path)


# ── start ──


class TestSessionStart:
    def test_start_creates_session_with_started_event(self, store: JsonlSessionStore):
        session = Session.start(store)
        assert session.session_id  # UUID 自动生成
        events = session.events
        assert len(events) == 1
        assert events[0].type == SESSION_STARTED
        assert events[0].session_id == session.session_id
        assert events[0].seq == 0

    def test_start_writes_to_disk(self, store: JsonlSessionStore):
        session = Session.start(store, agent_id="test-agent")
        # 从磁盘重新加载
        loaded = store.read_events(session.session_id)
        assert len(loaded) == 1
        assert loaded[0].type == SESSION_STARTED
        assert loaded[0].agent_id == "test-agent"

    def test_start_assigns_incremental_seq(self, store: JsonlSessionStore):
        session = Session.start(store)
        session.append(USER_MESSAGE, {"content": "hello"})
        session.append(MODEL_COMPLETED, {"content": "hi back"})
        seqs = [e.seq for e in session.events]
        assert seqs == [0, 1, 2]


# ── append ──


class TestSessionAppend:
    def test_append_returns_event_with_correct_seq(self, store: JsonlSessionStore):
        session = Session.start(store)
        event = session.append(USER_MESSAGE, {"content": "test"})
        assert event.seq == 1  # start 占了 seq 0
        assert event.type == USER_MESSAGE
        assert event.data == {"content": "test"}

    def test_append_syncs_memory_and_disk(self, store: JsonlSessionStore):
        session = Session.start(store)
        session.append(USER_MESSAGE, {"content": "msg1"})
        # 内存
        assert len(session.events) == 2
        # 磁盘
        disk_events = store.read_events(session.session_id)
        assert len(disk_events) == 2
        assert disk_events[1].type == USER_MESSAGE

    def test_append_with_run_id_and_step_id(self, store: JsonlSessionStore):
        session = Session.start(store)
        session.append(
            MODEL_COMPLETED,
            {"content": "response"},
            run_id="r1",
            step_id=3,
        )
        last = session.events[-1]
        assert last.run_id == "r1"
        assert last.step_id == 3


# ── derive_messages ──


class TestSessionDeriveMessages:
    def test_derive_after_append_sequence(self, store: JsonlSessionStore):
        session = Session.start(store)
        session.append(USER_MESSAGE, {"content": "你好"})
        session.append(MODEL_COMPLETED, {"content": "你好！"})

        messages = session.derive_messages()
        # session/started 不投影成 message，只有 user + model
        assert len(messages) == 2
        assert messages[0].content == "你好"
        assert messages[1].content == "你好！"


# ── resume ──


class TestSessionResume:
    def test_resume_loads_events_and_appends_resumed(self, store: JsonlSessionStore):
        # 先创建并写入若干事件
        s1 = Session.start(store)
        s1.append(USER_MESSAGE, {"content": "first"})
        s1.append(MODEL_COMPLETED, {"content": "reply"})
        sid = s1.session_id

        # 重新加载
        s2 = Session.resume(store, sid)
        assert s2.session_id == sid
        # start + user + model + resumed = 4
        assert len(s2.events) == 4
        assert s2.events[-1].type == SESSION_RESUMED

    def test_resume_derive_matches_original(self, store: JsonlSessionStore):
        s1 = Session.start(store)
        s1.append(USER_MESSAGE, {"content": "hello"})
        s1.append(MODEL_COMPLETED, {"content": "world"})
        original_messages = s1.derive_messages()

        s2 = Session.resume(store, s1.session_id)
        resumed_messages = s2.derive_messages()
        # resumed 后只多了 session/resumed 事件，不影响消息投影
        assert len(resumed_messages) == len(original_messages)
        assert [m.content for m in resumed_messages] == [
            m.content for m in original_messages
        ]

    def test_resume_nonexistent_session_raises(self, store: JsonlSessionStore):
        # 类型化领域异常（BUG-011）：不再是裸 ValueError——web 层据此翻译成 404，
        # 不再需要 `except ValueError → SessionNotFound` 这种一刀切。
        with pytest.raises(SessionNotFound, match="不存在"):
            Session.resume(store, "fake-session-id")

    def test_resume_repair_dangling_tool_call(self, store: JsonlSessionStore):
        """有 tool/call 无 tool/result → resume 时追加合成 tool/result。"""
        s1 = Session.start(store)
        s1.append(USER_MESSAGE, {"content": "算一下"})
        s1.append(
            MODEL_COMPLETED,
            {
                "content": "算",
                "tool_calls": [{"id": "tc1", "name": "add", "args": {}}],
            },
        )
        s1.append(TOOL_CALL, {"tool_call_id": "tc1", "tool_name": "add", "args": {}})
        # 没有 tool/result——模拟崩溃

        s2 = Session.resume(store, s1.session_id)
        # resume 后应有合成 tool/result
        events_after = s2.events
        tool_results = [e for e in events_after if e.type == TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].data["tool_call_id"] == "tc1"
        assert tool_results[0].data["content"] == DANGLING_TOOL_CONTENT
        assert tool_results[0].data.get("dangling") is True

        # derive_messages 现在不该再有 dangling（已被修复）
        messages = s2.derive_messages()
        tool_msgs = [m for m in messages if hasattr(m, "tool_call_id")]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].content == DANGLING_TOOL_CONTENT


# ── Run 生命周期 ──


class TestRunLifecycle:
    def test_begin_run_returns_run_id_and_writes_event(self, store: JsonlSessionStore):
        session = Session.start(store)
        run_id, _ = session.begin_run()
        assert run_id  # UUID
        last = session.events[-1]
        assert last.type == RUN_STARTED
        assert last.run_id == run_id

    def test_begin_run_increments_turn_index(self, store: JsonlSessionStore):
        """T9 #139：每个 run 的 turn_index 递增（1-based）。"""
        session = Session.start(store)
        session.begin_run()
        assert session.events[-1].data["turn_index"] == 1
        session.end_run(session.events[-1].run_id, status="completed", final_text="ok")
        session.begin_run()
        assert session.events[-1].data["turn_index"] == 2
        session.end_run(session.events[-1].run_id, status="completed", final_text="ok2")
        session.begin_run()
        assert session.events[-1].data["turn_index"] == 3

    def test_begin_run_records_requested_model_when_given(self, store: JsonlSessionStore):
        """#226：调用方给了请求侧模型标识 ⇒ 落进 run/started（持久事实）。"""
        session = Session.start(store)
        session.begin_run(model="deepseek-chat")
        assert session.events[-1].data["model"] == "deepseek-chat"

    def test_begin_run_omits_model_key_when_not_given(self, store: JsonlSessionStore):
        """#226 添加性：不传 = **不落该键**（本方法不替调用方编默认值）。

        「键在场但为空」与「键缺席」是两种不同的历史事实，后端只在确知时才断言，
        前端按缺字段降级。既有直接调 begin_run 的调用方与旧事件载荷逐字不变。
        """
        session = Session.start(store)
        session.begin_run()
        assert "model" not in session.events[-1].data
        # 同一事实经 JSONL 落盘/重读后仍然成立（不是内存对象的偶然形状）。
        reloaded = Session.resume(store, session.session_id)
        assert "model" not in reloaded.events[-1].data

    def test_end_run_completed(self, store: JsonlSessionStore):
        session = Session.start(store)
        run_id, _ = session.begin_run()
        session.append(USER_MESSAGE, {"content": "test"})
        session.end_run(run_id, status="completed", final_text="done")

        last = session.events[-1]
        assert last.type == RUN_COMPLETED
        assert last.run_id == run_id
        assert last.data.get("final_text") == "done"

    def test_end_run_failed(self, store: JsonlSessionStore):
        session = Session.start(store)
        run_id, _ = session.begin_run()
        session.end_run(run_id, status="failed")

        last = session.events[-1]
        assert last.type == RUN_FAILED
        assert last.run_id == run_id

    # ── 终态幂等（#1103） ──

    def test_end_run_twice_lands_exactly_one_terminal(self, store: JsonlSessionStore):
        """#1103：同一 run 的第二次 `end_run` 不落第二条终态。

        终态是「这个 run 结束了吗」的唯一 durable 事实。旧实现两次 `end_run` 会落
        `run/completed` **和** `run/failed` 两条（durable 真相层被污染：消费方按
        `RUN_TERMINAL_TYPES` 投影会看到自相矛盾的历史）。生产侧原先只有
        `AgentRuntime._terminal_written` 这层护栏，Session 层自己没有守卫。

        判据与 `run/resumed` 的 `expected_version` 同族（CAS）：该 run 已有终态 ⇒
        拒写并返回**既存的那一枚**（幂等返回，不是抛错——同一 run 重入到底不该炸）。
        """
        session = Session.start(store)
        run_id, _ = session.begin_run()
        first = session.end_run(run_id, status="completed", final_text="done")
        second = session.end_run(run_id, status="failed", reason="boom")

        assert second.event_id == first.event_id, "第二次必须返回既存终态，而不是新事件"
        mine = [
            e
            for e in store.read_events(session.session_id)
            if e.run_id == run_id and e.type in (RUN_COMPLETED, RUN_FAILED)
        ]
        assert [e.type for e in mine] == [RUN_COMPLETED], (
            f"该 run 的 durable 终态必须恰好一条，实际 {[e.type for e in mine]}"
        )
        # 内存投影与盘面一致（不得只拦住盘、却往 _events 里再塞一枚）。
        assert session.events[-1].event_id == first.event_id

    def test_end_run_second_call_does_not_advance_seq(self, store: JsonlSessionStore):
        """幂等返回不得消耗 seq：被拒的那次不能留下空洞（否则下次 append 的形状变了）。"""
        session = Session.start(store)
        run_id, _ = session.begin_run()
        session.end_run(run_id, status="completed", final_text="done")
        seqs_after_first = [e.seq for e in store.read_events(session.session_id)]
        session.end_run(run_id, status="failed", reason="boom")
        assert [e.seq for e in store.read_events(session.session_id)] == seqs_after_first

    def test_end_run_idempotent_under_interleaved_writes(self, store: JsonlSessionStore):
        """#1113 第 8 项：幂等判据必须按 run 定界，且不受中途交错写入影响。

        上面两条用例只发**顺序**的第二次 `end_run`（CAS 检查与写盘之间没有任何
        交错）。本用例把两种交错叠起来：同聚合上**另一个 run 的终态**，以及夹在
        两次 `end_run(run_a)` 之间的**无关 append**。判据若退化成「末条事件是不是
        终态」或「有没有任何终态」，都会在这里露出来——前者会把 run B 的终态误当
        run A 的既存，后者会让 run A 落第二条。

        变异实证（改动后的树 + 破坏实现，隔离副本；本机复跑读数）：删掉 CAS ⇒ **3 条
        一起红**（本用例与上面两条顺序用例，`3 failed, 26 passed`）；CAS 忽略
        `run_id`、或 CAS 只看末条事件 ⇒ 本用例红而上面两条顺序用例**仍绿**
        （`1 failed, 28 passed`）⇒ 本用例的独立鉴别力由后两态给出，**不由**删 CAS
        那一态（那一态三条用例共用同一个失败面，互不构成鉴别力证据）。

        未覆盖（如实登记）：同实例多线程**真并发**、或另一 Session 实例（各自
        `_events` 都没有终态）仍可各落一条——本机实测（隔离副本，每组 30 轮 ×4 组）：
        两实例并发**每组都 30/30** 落 2 条（tmpfs 与 btrfs 皆然）；同实例两线程并发
        则**非确定**只在 **tmpfs** 上成立（4 组读数 16/17/20/20 落 2 条），btrfs 上
        8 组 ×30 = 240 轮**恒 30/30** ⇒ 断言恒 1 条在任一介质上都不成立，「flaky」
        这个定性也只对 tmpfs 成立。它要把判据移进 store 写锁才能根治，属另一票的范围。
        """
        session = Session.start(store)
        run_a, _ = session.begin_run()
        run_b, _ = session.begin_run()
        first_a = session.end_run(run_a, status="completed", final_text="a")
        # 交错 1：无关写者（模型事件）落在 run A 的终态之后。
        session.append(MODEL_COMPLETED, {"interleaved": True}, run_id=run_a)
        # 交错 2：另一个 run 的终态落在两次 `end_run(run_a)` 之间。
        session.end_run(run_b, status="failed", reason="b")
        again_a = session.end_run(run_a, status="failed", reason="again")

        assert again_a.event_id == first_a.event_id, "run A 第二次必须返回既存终态"
        assert again_a.run_id == run_a, "返回的既存终态必须属于被调用的那个 run"
        events = store.read_events(session.session_id)
        mine = [e.type for e in events
                if e.run_id == run_a and e.type in (RUN_COMPLETED, RUN_FAILED)]
        other = [e.type for e in events
                 if e.run_id == run_b and e.type in (RUN_COMPLETED, RUN_FAILED)]
        assert mine == [RUN_COMPLETED], f"run A 的 durable 终态必须恰好一条，实际 {mine}"
        assert other == [RUN_FAILED], f"run B 的 durable 终态必须恰好一条，实际 {other}"

    # ── trace_id / trace_url 契约（trace_url 契约：并列字段，对称终态） ──

    def test_end_run_completed_persists_trace_id_and_trace_url(self, store: JsonlSessionStore):
        session = Session.start(store)
        run_id, _ = session.begin_run()
        session.end_run(
            run_id, status="completed", final_text="done",
            trace_id="tr-001", trace_url="https://lf.example/traces/tr-001",
        )

        last = session.events[-1]
        assert last.data.get("trace_id") == "tr-001"
        assert last.data.get("trace_url") == "https://lf.example/traces/tr-001"

    def test_end_run_failed_symmetrically_persists_trace_id_and_trace_url(
        self, store: JsonlSessionStore,
    ):
        """失败 run 也下发 trace_id / trace_url（契约对称决策：失败 trace 在
        Langfuse 也有排查价值）。补既有 bug：failed 分支此前丢弃了 trace_id。"""
        session = Session.start(store)
        run_id, _ = session.begin_run()
        session.end_run(
            run_id, status="failed", reason="boom",
            trace_id="tr-failed", trace_url="https://lf.example/traces/tr-failed",
        )

        last = session.events[-1]
        assert last.data.get("trace_id") == "tr-failed"
        assert last.data.get("trace_url") == "https://lf.example/traces/tr-failed"
        assert last.data.get("reason") == "boom"

    def test_end_run_failed_persists_http_status_only_when_given(
        self, store: JsonlSessionStore,
    ):
        """#996：未分类 provider 失败的 HTTP 状态码是可选整数键——给值才落，
        不给不落（不伪造）；completed 终态永不带它。"""
        session = Session.start(store)
        run_id, _ = session.begin_run()
        session.end_run(run_id, status="failed", reason="BadRequestError", http_status=400)
        assert session.events[-1].data.get("http_status") == 400

        run_id, _ = session.begin_run()
        session.end_run(run_id, status="failed", reason="APIConnectionError")
        assert "http_status" not in session.events[-1].data

        run_id, _ = session.begin_run()
        session.end_run(run_id, status="completed", final_text="ok", http_status=500)
        assert "http_status" not in session.events[-1].data

    def test_end_run_trace_fields_null_when_not_provided(self, store: JsonlSessionStore):
        """trace_id / trace_url 键恒存在（前端契约据此降级显示）；未追踪 = null，
        不伪造占位字符串（保持 None 语义；与既有 completed 的 cost_usd 同款）。"""
        session = Session.start(store)
        run_id, _ = session.begin_run()
        session.end_run(run_id, status="completed", final_text="done")

        last = session.events[-1]
        assert "trace_id" in last.data
        assert last.data["trace_id"] is None
        assert "trace_url" in last.data
        assert last.data["trace_url"] is None


# ── next_seq ──


class TestNextSeq:
    def test_next_seq_empty_events(self, store: JsonlSessionStore):
        session = Session("s1", store, events=[])
        assert session.next_seq == 0

    def test_next_seq_after_appends(self, store: JsonlSessionStore):
        session = Session.start(store)  # seq 0
        session.append(USER_MESSAGE, {"content": "a"})  # seq 1
        session.append(MODEL_COMPLETED, {"content": "b"})  # seq 2
        assert session.next_seq == 3


# ── mark / since：Session 拥有"自 X 之后追加的事件"句柄（A1 深化）──


class TestMarkAndSince:
    def test_mark_and_since_roundtrip(self, store: JsonlSessionStore):
        session = Session.start(store)
        marker = session.mark()
        session.append(USER_MESSAGE, {"content": "a"})
        session.append(MODEL_COMPLETED, {"content": "b"})
        fresh = session.since(marker)
        assert [e.type for e in fresh] == [USER_MESSAGE, MODEL_COMPLETED]

    def test_since_before_any_append_is_empty(self, store: JsonlSessionStore):
        session = Session.start(store)
        assert session.since(session.mark()) == []

    def test_since_returns_copy_not_internal_list(self, store: JsonlSessionStore):
        session = Session.start(store)
        marker = session.mark()
        session.append(USER_MESSAGE, {"content": "a"})
        fresh = session.since(marker)
        fresh.clear()
        assert len(session.since(marker)) == 1
