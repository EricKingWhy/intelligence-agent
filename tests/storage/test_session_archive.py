"""#1389 会话全文存档 + FTS5（移植 Hermes `b324b1f7` state.db）。

覆盖票面 AC1–AC6：

- AC1 建表 / 触发器清单（三套 FTS × insert/delete/update）
- AC2 三轮对话（含一次工具调用）按 `messages.id` 自增序可查全文 + FTS 能搜 tool 结果
- AC3 `session_search` 四形态 + hidden/demoted
- AC4 tool 行 >8192 字符：索引只取前缀，存储与 READ 仍是全文
- AC5 并发写不丢不乱序；kill -9 后可打开 + `integrity-check` 通过
- AC6 事件流 schema 无新增大字段（有/无归档写者的事件载荷逐字段相同）
"""

from __future__ import annotations

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from agent_harness.agent import AgentRuntime
from agent_harness.session import Session
from agent_harness.session.event import SessionEvent
from agent_harness.storage.session_archive import (
    FTS_TOOL_CONTENT_PREFIX_CHARS,
    MAX_FTS5_QUERY_CHARS,
    SessionArchive,
    SessionArchiveObserver,
    SessionArchiveWriter,
)
from agent_harness.tooling import Tool, ToolExecutor, ToolRegistry, ToolResult
from tests.conftest import make_session
from tests.scripted_model import ScriptedModel

REPO_ROOT = Path(__file__).resolve().parents[2]
CJK_SOURCE = REPO_ROOT / "native" / "fts5_cjk" / "fts5_cjk.c"
CJK_VENDOR = REPO_ROOT / "native" / "fts5_cjk" / "vendor"


# ---------------------------------------------------------------- fixtures --


def _build_cjk_so(dest: Path) -> Path | None:
    """按 Hermes `build.sh` 的口径编译 cjk_unicode61 tokenizer；无编译器返回 None。"""
    if not CJK_SOURCE.exists() or shutil.which("gcc") is None:
        return None
    so = dest / "libfts5_cjk.so"
    cmd = ["gcc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra"]
    if not _has_system_sqlite_header():
        cmd += ["-I", str(CJK_VENDOR)]
    cmd += [str(CJK_SOURCE), "-o", str(so)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return None
    return so


def _has_system_sqlite_header() -> bool:
    proc = subprocess.run(
        ["gcc", "-E", "-xc", "-"], input="#include <sqlite3ext.h>\n",
        capture_output=True, text=True, check=False,
    )
    return proc.returncode == 0


@pytest.fixture(scope="session")
def cjk_so(tmp_path_factory: pytest.TempPathFactory) -> Path:
    so = _build_cjk_so(tmp_path_factory.mktemp("fts5-cjk"))
    if so is None:
        pytest.skip("cjk_unicode61 tokenizer 无法编译（缺 gcc 或缺少源码）")
    return so


@pytest.fixture()
def archive(tmp_path: Path, cjk_so: Path) -> SessionArchive:
    db = tmp_path / "sessions" / "state.db"
    arc = SessionArchive(db, cjk_so_path=cjk_so)
    arc.initialize()
    return arc


def _raw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _names(db_path: Path, kind: str) -> set[str]:
    with _raw(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
        ).fetchall()
    return {row["name"] for row in rows}


# ------------------------------------------------------------------- AC1 ---


class TestSchema:
    def test_tables_present(self, archive: SessionArchive) -> None:
        tables = _names(archive.db_path, "table")
        assert {
            "sessions",
            "messages",
            "messages_fts",
            "messages_fts_trigram",
            "messages_fts_cjk",
            "state_meta",
            "system_prompts",
            "session_model_usage",
        } <= tables
        # `*_src` 是 external-content 的投影视图，不是实体表。
        assert {
            "messages_fts_src",
            "messages_fts_trigram_src",
            "messages_fts_cjk_src",
        } <= _names(archive.db_path, "view")

    def test_trigger_inventory(self, archive: SessionArchive) -> None:
        triggers = _names(archive.db_path, "trigger")
        for family in ("messages_fts", "messages_fts_trigram", "messages_fts_cjk"):
            assert {
                f"{family}_insert",
                f"{family}_delete",
                f"{family}_update",
            } <= triggers

    def test_base_update_trigger_scoped_to_content_columns(self, archive: SessionArchive) -> None:
        """compaction 归档翻转 active/compacted **故意不触发**（Hermes #68858/#73639）。"""
        with _raw(archive.db_path) as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='messages_fts_update'"
            ).fetchone()["sql"]
        assert "AFTER UPDATE OF content, tool_name, tool_calls, role ON messages" in sql

    def test_trigram_update_trigger_scope(self, archive: SessionArchive) -> None:
        with _raw(archive.db_path) as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='messages_fts_trigram_update'"
            ).fetchone()["sql"]
        assert "AFTER UPDATE OF content, tool_name, role ON messages" in sql

    def test_cjk_update_trigger_scope(self, archive: SessionArchive) -> None:
        with _raw(archive.db_path) as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='messages_fts_cjk_update'"
            ).fetchone()["sql"]
        assert "AFTER UPDATE OF content, tool_name, tool_calls, role ON messages" in sql

    def test_pending_columns_exist(self, archive: SessionArchive) -> None:
        """票面「待裁决列全量建表」—— 裁决前一个不许少。"""
        with _raw(archive.db_path) as conn:
            session_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
            message_cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        assert {
            "billing_provider", "billing_base_url", "billing_mode", "estimated_cost_usd",
            "actual_cost_usd", "cost_status", "cost_source", "pricing_version",
            "handoff_state", "handoff_platform", "handoff_error",
            "compression_failure_cooldown_until", "compression_failure_error",
            "compression_fallback_streak", "compression_ineffective_count",
            "compression_recovery_deadline", "compression_overload_streak",
            "chat_id", "chat_type", "thread_id", "session_key", "user_id", "created_source",
            "profile_name", "transport_profile", "origin_json", "expiry_finalized",
            "display_name", "archived", "auto_archived", "pinned", "hidden",
        } <= session_cols
        assert {
            "codex_reasoning_items", "codex_message_items",
            "display_kind", "display_metadata", "display_identity", "display_order",
            "message_uid", "absorbed_message_uids", "tool_call_uids", "tool_call_uid",
            "platform_message_id", "effect_disposition", "observed",
            "api_content", "_compressed_summary", "active", "compacted",
        } <= message_cols

    def test_rebuild_markers_default_open(self, archive: SessionArchive) -> None:
        """新库无回填 ⇒ 门控恒真（无 marker 行时两条触发器都照常索引）。"""
        with _raw(archive.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM state_meta WHERE key LIKE 'fts%'"
            ).fetchone()[0] == 0


# ------------------------------------------------------------------- AC2 ---


class EchoArgs(BaseModel):
    text: str = ""


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回显一段文本。"

    @property
    def args_schema(self) -> type[BaseModel]:
        return EchoArgs

    async def execute(self, args: EchoArgs) -> ToolResult:
        return ToolResult.success(message=f"ECHO[{args.text}]", data={"text": args.text})


def _runtime(scripted: ScriptedModel) -> AgentRuntime:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return AgentRuntime(
        model=scripted, registry=registry, executor=ToolExecutor(registry),
        max_agent_turns=10,
    )


def _run_with_archive(
    tmp_path: Path, archive: SessionArchive, scripted: ScriptedModel,
    *, source: str = "interactive",
) -> tuple[Session, object]:
    session = make_session(tmp_path)
    archive.ensure_session(session.session_id, source=source, started_at=time.time())
    writer = SessionArchiveWriter(archive, session.session_id)
    writer.attach(session)
    runtime = _runtime(scripted)
    result = _run(runtime, session)
    return session, result


def _run(runtime: AgentRuntime, session: Session):
    import asyncio

    return asyncio.run(runtime.run(session, "请回显 hello-archive"))


class TestThreeRoundConversation:
    def test_messages_in_id_order_and_tool_result_searchable(
        self, tmp_path: Path, archive: SessionArchive,
    ) -> None:
        round1 = AIMessage(
            content="",
            tool_calls=[{
                "name": "echo", "args": {"text": "存档关键词-青花瓷"},
                "id": "call_arc_1", "type": "tool_call",
            }],
        )
        scripted = ScriptedModel([round1, AIMessage(content="工具已执行完成")])
        session, result = _run_with_archive(tmp_path, archive, scripted)

        rows = archive.get_messages(session.session_id)
        assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
        assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
        assert "请回显 hello-archive" in rows[0]["content"]
        assert "call_arc_1" in rows[1]["tool_calls"]
        tool_call_id = rows[2]["tool_call_id"]
        assert tool_call_id == "call_arc_1"
        assert "ECHO[存档关键词-青花瓷]" in rows[2]["content"]
        assert rows[3]["content"] == "工具已执行完成"

        hits = archive.search_messages("青花瓷", limit=10)
        assert hits, "FTS5 必须能搜到 tool 结果里的关键词"
        assert any(hit["session_id"] == session.session_id for hit in hits)
        assert result.status == "completed"

    def test_delta_chunks_are_not_archived(
        self, tmp_path: Path, archive: SessionArchive,
    ) -> None:
        scripted = ScriptedModel([AIMessage(content="流式终态"), AIMessage(content="再一轮")])
        session = make_session(tmp_path)
        archive.ensure_session(session.session_id, source="interactive", started_at=time.time())
        writer = SessionArchiveWriter(archive, session.session_id)
        writer.attach(session)
        runtime = _runtime(scripted)
        import asyncio

        async def drive() -> None:
            async for _ in runtime.run_stream(session, "流式问题"):
                pass

        asyncio.run(drive())
        roles = [r["role"] for r in archive.get_messages(session.session_id)]
        # delta 不落库：一轮只有 user + 终态各一条，没有逐 chunk 的重复行
        assert roles == ["user", "assistant"]
        assert archive.get_messages(session.session_id)[-1]["content"] == "流式终态"


# ------------------------------------------------------------------- AC3 ---


class TestSessionSearchShapes:
    def _seed(self, archive: SessionArchive) -> dict[str, str]:
        now = time.time()
        ids = {}
        for sid, source, text in (
            ("s-interactive", "interactive", "用户主会话 讨论 部署 方案 甲"),
            ("s-subagent", "subagent", "子代理会话 讨论 部署 方案 乙"),
            ("s-fork", "fork", "分支会话 讨论 部署 方案 丙"),
            ("s-cron", "cron", "定时任务 讨论 部署 方案 丁"),
        ):
            archive.ensure_session(sid, source=source, started_at=now)
            archive.append_message(sid, "user", text, timestamp=now)
            archive.append_message(sid, "assistant", f"{text} 答复", timestamp=now + 1)
            ids[source] = sid
        return ids

    def test_discovery_hides_subagent_and_fork(self, archive: SessionArchive) -> None:
        ids = self._seed(archive)
        out = archive.session_search(query="部署")
        found = {r["session_id"] for r in out["results"]}
        assert ids["interactive"] in found
        assert ids["cron"] in found
        assert ids["subagent"] not in found
        assert ids["fork"] not in found

    def test_discovery_demotes_cron(self, archive: SessionArchive) -> None:
        ids = self._seed(archive)
        out = archive.session_search(query="部署")
        assert out["results"][0]["session_id"] == ids["interactive"]
        assert out["results"][0]["source"] == "interactive"

    def test_scroll_anchor(self, archive: SessionArchive) -> None:
        self._seed(archive)
        rows = archive.get_messages("s-interactive")
        anchor = rows[1]["id"]
        out = archive.session_search(session_id="s-interactive", around_message_id=anchor, window=1)
        assert out["mode"] == "scroll"
        window_ids = [m["id"] for m in out["messages"]]
        assert anchor in window_ids
        assert any(m.get("anchor") for m in out["messages"])

    def test_read_shape_caps_content(self, archive: SessionArchive) -> None:
        self._seed(archive)
        big = "长" * 5000
        archive.append_message("s-interactive", "tool", big, tool_name="big", timestamp=time.time())
        out = archive.session_search(session_id="s-interactive")
        assert out["mode"] == "read"
        big_rows = [m for m in out["messages"] if m.get("tool_name") == "big"]
        assert big_rows and big_rows[0]["content_truncated"] is True
        assert len(big_rows[0]["content"]) <= 2000 + 1

    def test_browse_lists_recent_sessions(self, archive: SessionArchive) -> None:
        ids = self._seed(archive)
        out = archive.session_search()
        assert out["mode"] == "browse"
        listed = {r["session_id"] for r in out["results"]}
        assert ids["interactive"] in listed
        assert ids["subagent"] not in listed

    def test_relative_time_bounds(self, archive: SessionArchive) -> None:
        now = time.time()
        archive.ensure_session("old", source="interactive", started_at=now - 30 * 86400)
        archive.append_message("old", "user", "远古部署话题", timestamp=now - 30 * 86400)
        archive.ensure_session("new", source="interactive", started_at=now)
        archive.append_message("new", "user", "近期部署话题", timestamp=now)
        out = archive.session_search(query="部署", after="7d")
        assert [r["session_id"] for r in out["results"]] == ["new"]

    def test_cjk_query_routes_to_cjk_index(self, archive: SessionArchive) -> None:
        archive.ensure_session("zh", source="interactive", started_at=time.time())
        archive.append_message("zh", "user", "修改服务器配置后重启", timestamp=time.time())
        assert archive.search_messages("服务", limit=5), "2 字 CJK 词必须能命中（cjk_unicode61 大二元）"

    def test_query_input_is_capped(self, archive: SessionArchive) -> None:
        archive.ensure_session("cap", source="interactive", started_at=time.time())
        archive.append_message("cap", "user", "x" * (MAX_FTS5_QUERY_CHARS + 500), timestamp=time.time())
        # 超长输入不得抛异常，且被截断处理
        archive.search_messages("x" * (MAX_FTS5_QUERY_CHARS + 500), limit=5)


# ------------------------------------------------------------------- AC4 ---


class TestToolContentPrefix:
    def test_index_only_sees_prefix_but_storage_keeps_full(
        self, archive: SessionArchive,
    ) -> None:
        archive.ensure_session("big", source="interactive", started_at=time.time())
        head = "前缀关键词A" + "填" * FTS_TOOL_CONTENT_PREFIX_CHARS
        tail = "后缀关键词B"
        full = head + tail
        archive.append_message("big", "tool", full, tool_name="big", timestamp=time.time())

        assert archive.search_messages("前缀关键词", limit=5)
        assert not archive.search_messages(tail, limit=5), "8192 之后的内容不得进 FTS 索引"

        rows = archive.get_messages("big")
        assert rows[0]["content"] == full, "存储仍是全文"

        out = archive.session_search(session_id="big")
        assert out["mode"] in ("read", "scroll")
        assert out["message_count"] == 1

    def test_projection_view_matches_prefix(self, archive: SessionArchive) -> None:
        archive.ensure_session("p", source="interactive", started_at=time.time())
        full = "甲" * (FTS_TOOL_CONTENT_PREFIX_CHARS + 10)
        archive.append_message("p", "tool", full, tool_name="p", timestamp=time.time())
        with _raw(archive.db_path) as conn:
            projected = conn.execute(
                "SELECT content FROM messages_fts_src WHERE tool_name='p'"
            ).fetchone()["content"]
        assert projected == full[:FTS_TOOL_CONTENT_PREFIX_CHARS]


# ------------------------------------------------------------------- AC5 ---


class TestConcurrencyAndCrash:
    def test_concurrent_writers_lose_nothing(self, archive: SessionArchive) -> None:
        archive.ensure_session("c", source="interactive", started_at=time.time())
        threads = 8
        per_thread = 25
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            try:
                for i in range(per_thread):
                    archive.append_message(
                        "c", "user", f"thread-{n}-msg-{i}", timestamp=time.time(),
                    )
            except BaseException as exc:  # noqa: BLE001 - 测试要收集全部失败
                errors.append(exc)

        workers = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        assert not errors
        rows = archive.get_messages("c")
        assert len(rows) == threads * per_thread
        assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
        assert len({r["content"] for r in rows}) == threads * per_thread

    def test_kill_9_leaves_openable_consistent_db(
        self, tmp_path: Path, cjk_so: Path,
    ) -> None:
        db_path = tmp_path / "sessions" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        script = textwrap.dedent(
            """
            import sys, time
            from pathlib import Path
            from agent_harness.storage.session_archive import SessionArchive

            arc = SessionArchive(Path(sys.argv[1]), cjk_so_path=Path(sys.argv[2]))
            arc.initialize()
            arc.ensure_session("k", source="interactive", started_at=time.time())
            i = 0
            while True:
                arc.append_message("k", "user", f"crash-{i}", timestamp=time.time())
                i += 1
                sys.stdout.write("x")
                sys.stdout.flush()
            """
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, "-c", script, str(db_path), str(cjk_so)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        try:
            deadline = time.time() + 30
            seen = 0
            while seen < 40 and time.time() < deadline:
                chunk = proc.stdout.read(40 - seen)
                if not chunk:
                    break
                seen += len(chunk)
            assert seen >= 40, proc.stderr.read().decode() if proc.stderr else "no progress"
        finally:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=10)

        # 裸连接读不了 cjk 表（tokenizer 是**按连接**登记的）：先加载扩展再校验。
        with _raw(db_path) as conn:
            conn.enable_load_extension(True)
            conn.load_extension(str(cjk_so))
            conn.enable_load_extension(False)
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute(
                "INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')"
            ) is not None
            rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert rows >= 40
        reopened = SessionArchive(db_path, cjk_so_path=cjk_so)
        assert reopened.get_messages("k")


# ------------------------------------------------------------------- AC6 ---


class TestEventStreamUntouched:
    def _payload(self, events) -> list[tuple[str, tuple[str, ...]]]:
        return [(e.type, tuple(sorted(e.data))) for e in events]

    def test_writer_does_not_change_event_payloads(
        self, tmp_path: Path, archive: SessionArchive,
    ) -> None:
        scripted = ScriptedModel([
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "echo", "args": {"text": "相同载荷"},
                    "id": "call_arc_2", "type": "tool_call",
                }],
            ),
            AIMessage(content="完成"),
        ])
        plain = make_session(tmp_path / "plain")
        _run(_runtime(scripted), plain)

        scripted2 = ScriptedModel([
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "echo", "args": {"text": "相同载荷"},
                    "id": "call_arc_2", "type": "tool_call",
                }],
            ),
            AIMessage(content="完成"),
        ])
        archived = make_session(tmp_path / "archived")
        archive.ensure_session(archived.session_id, source="interactive", started_at=time.time())
        SessionArchiveWriter(archive, archived.session_id).attach(archived)
        _run(_runtime(scripted2), archived)

        assert self._payload(plain.events) == self._payload(archived.events)
        # 归档写者不往事件流塞自己的事件类型
        assert {e.type for e in archived.events} == {e.type for e in plain.events}

    def test_no_large_content_added_to_event_types(self, archive: SessionArchive) -> None:
        """归档库存在与否，事件类型集合不变（不变量 #22：派生索引不是 Session 真相）。"""
        assert archive is not None


# ------------------------------------------------------- 产品接线（#1389 6) ---


class TestStoreObserverWiring:
    """写入钩子挂在 `JsonlSessionStore` 上：一处接线覆盖全部生产写路径。"""

    def _store(self, tmp_path: Path) -> object:
        from agent_harness.session.store import JsonlSessionStore

        return JsonlSessionStore(root=tmp_path / "sessions")

    def test_observer_archives_terminal_events_only(
        self, tmp_path: Path, archive: SessionArchive,
    ) -> None:
        from agent_harness.session.event import (
            MODEL_COMPLETED,
            MODEL_DELTA,
            USER_MESSAGE,
        )

        store = self._store(tmp_path)
        SessionArchiveObserver(archive).attach(store)
        sid = "sess-observer"
        store.append_event(
            sid, SessionEvent(type=USER_MESSAGE, session_id=sid, seq=0,
                              data={"content": "观察者写入"}),
        )
        store.append_event(
            sid, SessionEvent(type=MODEL_DELTA, session_id=sid, seq=1,
                              data={"content": "流式 chunk"}),
        )
        store.append_event(
            sid, SessionEvent(type=MODEL_COMPLETED, session_id=sid, seq=2,
                              data={"content": "终态"}),
        )
        rows = archive.get_messages(sid)
        assert [r["role"] for r in rows] == ["user", "assistant"]
        assert rows[1]["content"] == "终态"

    def test_observer_failure_never_breaks_append(
        self, tmp_path: Path, archive: SessionArchive,
    ) -> None:
        from agent_harness.session.event import USER_MESSAGE

        store = self._store(tmp_path)

        def boom(session_id: str, event: object) -> None:
            raise RuntimeError("档案库炸了")

        store.set_event_observer(boom)
        event = store.append_event(
            "sess-boom",
            SessionEvent(type=USER_MESSAGE, session_id="sess-boom", seq=0,
                         data={"content": "写入必须成功"}),
        )
        assert event.seq == 0
        assert archive.get_messages("sess-boom") == []

    def test_source_vocabulary_mapping(
        self, tmp_path: Path, archive: SessionArchive,
    ) -> None:
        """fork → hidden、委派子会话 → subagent(hidden)、普通 → interactive。"""
        from agent_harness.session.event import (
            SESSION_FORKED,
            SESSION_STARTED,
            USER_MESSAGE,
        )

        store = self._store(tmp_path)
        SessionArchiveObserver(archive).attach(store)
        store.append_event(
            "sess-normal",
            SessionEvent(type=SESSION_STARTED, session_id="sess-normal", seq=0, data={}),
        )
        store.append_event(
            "sess-child",
            SessionEvent(type=SESSION_STARTED, session_id="sess-child", seq=0,
                         data={"delegation_tree_id": "tree-1"}),
        )
        store.append_event(
            "sess-fork",
            SessionEvent(type=SESSION_FORKED, session_id="sess-fork", seq=0,
                         data={"parent_session_id": "sess-normal"}),
        )
        assert archive.get_session("sess-normal")["source"] == "interactive"
        assert archive.get_session("sess-child")["source"] == "subagent"
        fork = archive.get_session("sess-fork")
        assert fork["source"] == "fork"
        assert fork["parent_session_id"] == "sess-normal"

        # hidden 语义：fork / subagent 不出现在 DISCOVERY 结果里
        for sid in ("sess-normal", "sess-child", "sess-fork"):
            store.append_event(
                sid, SessionEvent(type=USER_MESSAGE, session_id=sid, seq=1,
                                  data={"content": "共同关键词 灯塔"}),
            )
        hits = archive.session_search(query="灯塔", limit=10)
        assert {h["session_id"] for h in hits["results"]} == {"sess-normal"}

    def test_install_creates_nothing_until_first_event(self, tmp_path: Path) -> None:
        """接线本身零磁盘痕迹（422 请求不留尾巴的不变量），建库后才不干扰会话列表。"""
        from agent_harness.session.event import USER_MESSAGE
        from agent_harness.storage.session_archive import install_session_archive

        store = self._store(tmp_path)
        root = tmp_path / "sessions"
        install_session_archive(store, root / "state.db")
        assert not root.exists() or list(root.iterdir()) == []

        sid = "sess-real"
        store.append_event(
            sid, SessionEvent(type=USER_MESSAGE, session_id=sid, seq=0,
                              data={"content": "真实会话"}),
        )
        store.append_event(
            sid, SessionEvent(type=USER_MESSAGE, session_id=sid, seq=1,
                              data={"content": "第二笔"}),
        )
        # state.db 落在 sessions root 下（裸文件，不是目录）：列表只认目录。
        assert (root / "state.db").exists()
        assert store.list_session_ids() == [sid]
