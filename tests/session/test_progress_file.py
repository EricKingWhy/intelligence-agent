"""W-05（#349）：项目可见进度文件——来源、脱敏、原子写。

票面契约：``<项目根>/agent-progress/<session-id>/progress.md`` 每 Session 独立；
固定字段（schema_version / session_id / parent_session_id+fork_point_seq /
source_event_seq / generated_at / 原目标 / 约束与授权 / 验收项与状态 / 已验证
里程碑 / 决策 / 失败尝试与坑点 / 未决 Operation / 证据 refs / 阻塞与下一步）；
保留精确 ID/金额/用户禁令原文字面与来源 seq，**绝不**写凭证值、Cookie、``.env``
值、私密原始 Tool 输出；同目录临时文件 + fsync + 原子替换，kill / 锁定 / 权限
拒绝不留半文件；保留上一版本及 source seq/hash；重复更新幂等；文件可进 Git
diff 但系统不 ``git add``；写失败明确报错，不把旧文件说成最新。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.event import (
    ARTIFACT_EXTERNALIZED,
    CONTEXT_COMPACTION_FAILED,
    GUARD_STUCK,
    OPERATION_RECONCILE_REQUIRED,
    OPERATION_RECONCILED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_STARTED,
    SESSION_FORKED,
    TASK_ACCEPTANCE_RELEASED,
    TASK_ACCEPTED,
    TASK_PLAN_UPDATED,
    USER_MESSAGE,
)
from agent_harness.session.progress import (
    derive_progress_document,
    progress_paths,
    render_progress_markdown,
    write_progress_file,
)
from agent_harness.session.task import (
    apply_task_definition,
    apply_verification,
)

END_MARKER = "<!-- agent-progress:end -->"

FAKE_ENV = "API_KEY=sk-fake-0123456789abcdefSECRET"
FAKE_COOKIE = "Cookie: session=deadbeefcafebabe1234"
FAKE_BEARER = "Bearer faketest-token-1234567890"


def _session(tmp_path, **kwargs) -> Session:
    return Session.start(JsonlSessionStore(root=tmp_path), cwd=str(tmp_path), **kwargs)


def _write(root: Path, session: Session, **kwargs):
    return write_progress_file(root, session.session_id, session.events, **kwargs)


def _body(root: Path, session_id: str) -> str:
    return (progress_paths(root, session_id).markdown).read_text(encoding="utf-8")


def _meta(root: Path, session_id: str) -> dict:
    return json.loads(progress_paths(root, session_id).meta.read_text(encoding="utf-8"))


# ── 投影：固定字段与各节内容 ────────────────────────────────────────────────


class TestDeriveProgressDocument:
    def test_header_fields_and_source_seq(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="修复登录页")
        session.append(USER_MESSAGE, {"content": "先把构建弄绿"})
        doc = derive_progress_document(session.events, session_id=session.session_id)
        assert doc.session_id == session.session_id
        assert doc.source_event_seq == session.events[-1].seq
        assert doc.schema_version == "1"
        assert doc.parent_session_id is None and doc.fork_point_seq is None

    def test_parent_and_fork_from_session_forked(self, tmp_path) -> None:
        session = _session(tmp_path)
        session.append(SESSION_FORKED, {"parent_session_id": "parent-1", "fork_point_seq": 7})
        doc = derive_progress_document(session.events, session_id=session.session_id)
        assert doc.parent_session_id == "parent-1"
        assert doc.fork_point_seq == 7

    def test_goal_from_task_definition_and_undefined_marked_missing(self, tmp_path) -> None:
        session = _session(tmp_path)
        doc = derive_progress_document(session.events, session_id=session.session_id)
        assert doc.goal is None, "未定义任务时原目标是缺项，不是编造"
        apply_task_definition(session, task_text="把登录页修好")
        doc2 = derive_progress_document(session.events, session_id=session.session_id)
        assert doc2.goal == "把登录页修好"

    def test_constraints_read_write_intent_and_active_protected_facts(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(
            session, task_text="T", read_write_intent="只读 src/，可写 tests/"
        )
        session.append(USER_MESSAGE, {"content": "禁止删除 migrations 目录"})
        doc = derive_progress_document(session.events, session_id=session.session_id)
        assert doc.read_write_intent == "只读 src/，可写 tests/"
        facts = [c for c in doc.constraints if c["kind"] == "protected_fact"]
        assert facts and any("禁止删除 migrations 目录" in c["value"] for c in facts)
        assert all(c["source_seq"] > 0 for c in facts), "约束条目带来源 seq"

    def test_acceptance_section_reflects_w07_axes(self, tmp_path) -> None:
        session = _session(tmp_path)
        outcome = apply_task_definition(
            session, task_text="T", criteria=[{"text": "登录成功", "origin": "user"}]
        )
        assert outcome.ok
        item_id = outcome.state.criteria[0].item_id
        apply_verification(session, item_id, "passed", evidence="e2e 绿")
        session.append(
            TASK_ACCEPTED,
            {"decision": "accepted", "reason": None},
        )
        doc = derive_progress_document(session.events, session_id=session.session_id)
        assert doc.acceptance["criteria"][0]["item_id"] == item_id
        assert doc.acceptance["criteria"][0]["verification_value"] == "passed"
        assert doc.acceptance["acceptance"] == {
            "decision": "accepted",
            "reason": None,
        }
        assert doc.acceptance["version"] >= 1

    def test_milestones_from_passed_items_plan_and_completed_runs(self, tmp_path) -> None:
        session = _session(tmp_path)
        outcome = apply_task_definition(
            session, task_text="T", criteria=[{"text": "A 指标"}, {"text": "B 指标"}]
        )
        item_a, item_b = (c.item_id for c in outcome.state.criteria)
        apply_verification(session, item_a, "passed", evidence="证据 A")
        session.append(
            TASK_PLAN_UPDATED,
            {"items": [
                {"id": "step-1", "content": "搭骨架", "activeForm": "搭骨架",
                 "status": "completed", "source": "agent"},
                {"id": "step-2", "content": "补测试", "activeForm": "补测试",
                 "status": "pending", "source": "agent"},
            ]},
        )
        session.append(RUN_STARTED, {}, run_id="run-1")
        session.append(RUN_COMPLETED, {"summary": "第一轮完成"}, run_id="run-1")
        doc = derive_progress_document(session.events, session_id=session.session_id)
        passed = [m for m in doc.milestones if m["kind"] == "verification_passed"]
        assert [m["item_id"] for m in passed] == [item_a]
        assert item_b not in [m.get("item_id") for m in doc.milestones]
        assert any(m["kind"] == "plan_step_completed" and m["item_id"] == "step-1"
                   for m in doc.milestones)
        assert any(m["kind"] == "run_completed" and m["run_id"] == "run-1"
                   for m in doc.milestones)

    def test_decisions_from_acceptance_axis(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="T")
        session.append(
            TASK_ACCEPTED,
            {"decision": "accepted_with_gaps", "reason": "缺项 E 可后续补"},
        )
        session.append(TASK_ACCEPTANCE_RELEASED, {"reason": "重开交付"})
        doc = derive_progress_document(session.events, session_id=session.session_id)
        kinds = [d["kind"] for d in doc.decisions]
        assert kinds == ["accepted", "released"]
        assert doc.decisions[0]["decision"] == "accepted_with_gaps"
        assert doc.decisions[0]["reason"] == "缺项 E 可后续补"
        assert all(d["source_seq"] > 0 for d in doc.decisions)

    def test_failures_from_run_failed_compaction_and_stuck_guard(self, tmp_path) -> None:
        session = _session(tmp_path)
        session.append(RUN_STARTED, {}, run_id="run-9")
        session.append(RUN_FAILED, {"reason": "provider 5xx"}, run_id="run-9")
        session.append(
            CONTEXT_COMPACTION_FAILED, {"error_class": "provider_timeout"}
        )
        session.append(GUARD_STUCK, {"level": "replan"})
        doc = derive_progress_document(session.events, session_id=session.session_id)
        assert any(f["kind"] == "run_failed" and f["run_id"] == "run-9"
                   and f["reason"] == "provider 5xx" for f in doc.failures)
        assert any(f["kind"] == "compaction_failed" and f["error_class"] == "provider_timeout"
                   for f in doc.failures)
        assert any(f["kind"] == "guard_stuck" for f in doc.failures)

    def test_pending_operations_exclude_reconciled(self, tmp_path) -> None:
        session = _session(tmp_path)
        session.append(
            OPERATION_RECONCILE_REQUIRED,
            {"tool_call_id": "tc-1", "tool_name": "write_file",
             "args_identity": {}, "state": "need_reconcile"},
        )
        session.append(
            OPERATION_RECONCILE_REQUIRED,
            {"tool_call_id": "tc-2", "tool_name": "bash",
             "args_identity": {}, "state": "need_reconcile"},
        )
        session.append(OPERATION_RECONCILED, {"tool_call_id": "tc-1"})
        doc = derive_progress_document(session.events, session_id=session.session_id)
        ids = [op["tool_call_id"] for op in doc.pending_operations]
        assert ids == ["tc-2"], "已对账的 operation 不再是未决"
        assert all(op["tool_name"] for op in doc.pending_operations)

    def test_evidence_refs_from_artifact_events(self, tmp_path) -> None:
        session = _session(tmp_path)
        # 生产 payload 形状（tooling/overflow.py append 侧），不用想象键
        session.append(
            ARTIFACT_EXTERNALIZED,
            {"artifact_id": "art-1", "session_id": session.session_id,
             "source_tool": "write_file", "tool_call_id": "tc-7",
             "size": 4096, "mime_type": "text/plain"},
        )
        doc = derive_progress_document(session.events, session_id=session.session_id)
        assert doc.evidence[0]["artifact_id"] == "art-1"
        assert doc.evidence[0]["size"] == 4096
        assert doc.evidence[0]["source_tool"] == "write_file"
        assert doc.evidence[0]["source_seq"] > 0
        body = render_progress_markdown(
            doc, generated_at="2026-10-04T12:00:00+00:00"
        )
        assert "artifact_id=art-1" in body, "artifact_id 即 read_artifact 的 ref"

    def test_blockers_from_open_and_paused_runs(self, tmp_path) -> None:
        session = _session(tmp_path)
        session.append(RUN_STARTED, {}, run_id="run-a")
        session.append(RUN_PAUSED, {"reason": "budget_exhausted"}, run_id="run-a")
        session.append(RUN_STARTED, {}, run_id="run-b")
        doc = derive_progress_document(session.events, session_id=session.session_id)
        paused = [b for b in doc.blockers if b["kind"] == "run_paused"]
        open_runs = [b for b in doc.blockers if b["kind"] == "run_open"]
        assert [b["run_id"] for b in paused] == ["run-a"]
        assert paused[0]["reason"] == "budget_exhausted"
        assert [b["run_id"] for b in open_runs] == ["run-b"]


# ── 渲染与脱敏 ─────────────────────────────────────────────────────────────


class TestRenderAndSanitize:
    def _full_session(self, tmp_path) -> Session:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="修复登录页", criteria=[{"text": "跳转成功"}])
        return session

    def test_render_contains_fixed_fields_and_sections(self, tmp_path) -> None:
        session = self._full_session(tmp_path)
        body = render_progress_markdown(
            derive_progress_document(session.events, session_id=session.session_id),
            generated_at="2026-10-04T12:00:00+00:00",
        )
        for field in ("schema_version: 1", f"session_id: {session.session_id}",
                      "source_event_seq:", "generated_at: 2026-10-04T12:00:00+00:00"):
            assert field in body
        for section in ("原目标", "约束与授权", "验收项与状态", "已验证里程碑", "决策",
                        "失败尝试与坑点", "未决 Operation", "证据 refs", "阻塞与下一步"):
            assert section in body, f"缺固定节：{section}"
        assert body.rstrip().endswith(END_MARKER)

    def test_render_is_deterministic(self, tmp_path) -> None:
        session = self._full_session(tmp_path)
        doc = derive_progress_document(session.events, session_id=session.session_id)
        a = render_progress_markdown(doc, generated_at="2026-10-04T12:00:00+00:00")
        b = render_progress_markdown(doc, generated_at="2026-10-04T12:00:00+00:00")
        assert a == b

    def test_render_shows_parent_and_fork_lines(self, tmp_path) -> None:
        session = _session(tmp_path)
        session.append(
            SESSION_FORKED, {"parent_session_id": "parent-1", "fork_point_seq": 7}
        )
        body = render_progress_markdown(
            derive_progress_document(session.events, session_id=session.session_id),
            generated_at="2026-10-04T12:00:00+00:00",
        )
        assert "parent_session_id: parent-1" in body
        assert "fork_point_seq: 7" in body

    def test_env_and_cookie_and_bearer_redacted_from_body(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(
            session,
            task_text=f"读取配置并部署；配置内容 {FAKE_ENV}；{FAKE_COOKIE}；{FAKE_BEARER}",
        )
        doc = derive_progress_document(session.events, session_id=session.session_id)
        body = render_progress_markdown(doc, generated_at="2026-10-04T12:00:00+00:00")
        assert "sk-fake-0123456789abcdefSECRET" not in body
        assert "deadbeefcafebabe1234" not in body
        assert "faketest-token-1234567890" not in body
        assert "API_KEY" in body, "键名保留、值脱敏（精确 ID/禁令原文字面保留）"

    def test_private_key_block_blocks_segment_and_marks_gap(self, tmp_path) -> None:
        session = _session(tmp_path)
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----"
        apply_task_definition(session, task_text=f"导入密钥 {pem} 然后重启")
        doc = derive_progress_document(session.events, session_id=session.session_id)
        body = render_progress_markdown(doc, generated_at="2026-10-04T12:00:00+00:00")
        assert "MIIB" not in body
        assert "缺项" in body, "无法安全呈现的段被阻止并标出缺项"

    def test_secrets_never_reach_logs(self, tmp_path, caplog) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text=f"部署 {FAKE_ENV}（{FAKE_COOKIE}）")
        outcome = _write(tmp_path, session)
        assert outcome.ok
        assert "sk-fake-0123456789abcdefSECRET" not in caplog.text
        assert "deadbeefcafebabe1234" not in caplog.text

    def test_user_prohibitions_and_ids_kept_verbatim(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(
            session, task_text="订单 #48123 金额 ¥12345.67；禁止删除 artifacts 表"
        )
        doc = derive_progress_document(session.events, session_id=session.session_id)
        body = render_progress_markdown(doc, generated_at="2026-10-04T12:00:00+00:00")
        assert "#48123" in body and "¥12345.67" in body
        assert "禁止删除 artifacts 表" in body


# ── 原子写：创建 / 幂等 / 上一版本 / 失败面 ─────────────────────────────────


class TestAtomicWrite:
    def test_create_writes_file_meta_and_directory(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="T")
        outcome = _write(tmp_path, session)
        assert outcome.ok and not outcome.skipped
        body = _body(tmp_path, session.session_id)
        assert f"session_id: {session.session_id}" in body
        assert body.rstrip().endswith(END_MARKER)
        meta = _meta(tmp_path, session.session_id)
        assert meta["source_event_seq"] == outcome.source_event_seq
        assert len(meta["content_sha256"]) == 64
        assert meta["previous"] is None

    def test_repeat_update_is_idempotent(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="T")
        assert _write(tmp_path, session).ok
        target = progress_paths(tmp_path, session.session_id).markdown
        before = (target.read_bytes(), target.stat().st_mtime_ns)
        second = _write(tmp_path, session)
        assert second.ok and second.skipped, "同源事件重复更新 = 跳过（幂等）"
        after = (target.read_bytes(), target.stat().st_mtime_ns)
        assert before == after, "跳过时文件字节与 mtime 均不变"

    def test_update_replaces_and_keeps_previous_version(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="第一版目标")
        first = _write(tmp_path, session)
        old_meta = _meta(tmp_path, session.session_id)
        session.append(USER_MESSAGE, {"content": "推进到第二步"})
        second = _write(tmp_path, session)
        assert second.ok and not second.skipped
        assert second.source_event_seq > first.source_event_seq
        paths = progress_paths(tmp_path, session.session_id)
        prev_body = paths.previous.read_text(encoding="utf-8")
        assert "第一版目标" in prev_body, "上一版本可恢复"
        meta = _meta(tmp_path, session.session_id)
        assert meta["previous"]["source_event_seq"] == old_meta["source_event_seq"]
        assert meta["previous"]["content_sha256"] == old_meta["content_sha256"]

    def test_two_sessions_never_overwrite_each_other(self, tmp_path) -> None:
        s1 = _session(tmp_path)
        s2 = _session(tmp_path)
        apply_task_definition(s1, task_text="会话一目标")
        apply_task_definition(s2, task_text="会话二目标")
        assert _write(tmp_path, s1).ok
        assert _write(tmp_path, s2).ok
        assert "会话一目标" in _body(tmp_path, s1.session_id)
        assert "会话二目标" in _body(tmp_path, s2.session_id)
        assert s1.session_id != s2.session_id

    def test_locked_target_fails_explicitly_and_keeps_old_file(self, tmp_path) -> None:
        import agent_harness.session.progress as progress_module

        session = _session(tmp_path)
        apply_task_definition(session, task_text="第一版")
        assert _write(tmp_path, session).ok
        target = progress_paths(tmp_path, session.session_id).markdown
        old_bytes = target.read_bytes()
        session.append(USER_MESSAGE, {"content": "推进"})
        # 外部写入方经同一锁协议持锁（独立 fd ⇒ 不同 file description，
        # flock 互斥在同进程内也成立——POSIX flock(2) 语义）
        lock_path = target.parent / progress_module._LOCK_NAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            progress_module._take_write_lock(fd)
            outcome = _write(tmp_path, session)
            assert not outcome.ok, "外部锁定：明确失败，不静默吞"
            assert outcome.error_kind in ("locked", "env")
            assert outcome.reason
        finally:
            progress_module._release_write_lock(fd)
            os.close(fd)
        assert target.read_bytes() == old_bytes, "失败后旧文件原样（不谎报最新）"

    @pytest.mark.skipif(
        getattr(os, "geteuid", lambda: -1)() == 0,
        reason="root 绕过文件权限位，chmod 只读不生效（环境限制）",
    )
    def test_readonly_target_fails_explicitly(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="第一版")
        assert _write(tmp_path, session).ok
        target = progress_paths(tmp_path, session.session_id).markdown
        session.append(USER_MESSAGE, {"content": "推进"})
        os.chmod(target, stat.S_IREAD)
        try:
            outcome = _write(tmp_path, session)
            assert not outcome.ok and outcome.reason
        finally:
            os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
        assert "第一版" in target.read_text(encoding="utf-8")

    def test_occupied_progress_dir_fails_explicitly(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="T")
        (tmp_path / "agent-progress").write_text("not a dir", encoding="utf-8")
        outcome = _write(tmp_path, session)
        assert not outcome.ok and outcome.reason
        assert outcome.error_kind in ("env", "locked")

    def test_midwrite_failure_leaves_old_file_and_no_half_target(
        self, tmp_path, monkeypatch
    ) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="第一版")
        assert _write(tmp_path, session).ok
        target = progress_paths(tmp_path, session.session_id).markdown
        old_bytes = target.read_bytes()
        session.append(USER_MESSAGE, {"content": "推进"})

        import agent_harness.session.progress as progress_module

        real_replace = progress_module.os.replace

        def boom(src, dst):
            if str(dst).endswith("progress.md"):
                raise OSError(28, "No space left on device")
            return real_replace(src, dst)

        monkeypatch.setattr(progress_module.os, "replace", boom)
        outcome = _write(tmp_path, session)
        monkeypatch.setattr(progress_module.os, "replace", real_replace)
        assert not outcome.ok and outcome.error_kind == "env"
        assert target.read_bytes() == old_bytes, "替换失败 → 旧版完整"

    def test_stale_temp_files_cleaned_on_next_write(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="T")
        paths = progress_paths(tmp_path, session.session_id)
        paths.directory.mkdir(parents=True, exist_ok=True)
        orphan = paths.directory / "progress.md.orphantmp"
        orphan.write_text("half-written", encoding="utf-8")
        assert _write(tmp_path, session).ok
        assert not orphan.exists(), "下一次写清理同模式孤儿临时文件"

    def test_write_lock_file_survives_cleanup_glob(self, tmp_path) -> None:
        """#660：锁文件命名避开 progress.md.* 清理 glob——下次写入不得误删
        外部持有的锁文件，否则协议被架空（锁文件在 = 有写入方在协议内持锁）。"""
        import agent_harness.session.progress as progress_module

        session = _session(tmp_path)
        apply_task_definition(session, task_text="T")
        paths = progress_paths(tmp_path, session.session_id)
        paths.directory.mkdir(parents=True, exist_ok=True)
        lock_path = paths.directory / progress_module._LOCK_NAME
        lock_path.write_text("", encoding="utf-8")
        assert _write(tmp_path, session).ok
        assert lock_path.exists(), "清理 glob 不得命中协议锁文件"

    def test_write_lock_open_failure_fails_explicitly(self, tmp_path, monkeypatch) -> None:
        """#660 回归：锁文件 os.open 抛 OSError（如只读目录）必须收敛为明确失败，
        不得让异常逃逸出 write_progress_file（此前同场景由 mkstemp 在 try 内接住）。"""
        import agent_harness.session.progress as progress_module

        real_open = os.open

        def _boom(path, flags, *args, **kwargs):
            if os.fspath(path).endswith(progress_module._LOCK_NAME):
                raise PermissionError(13, "Permission denied")
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", _boom)
        session = _session(tmp_path)
        apply_task_definition(session, task_text="T")
        outcome = _write(tmp_path, session)
        assert not outcome.ok, "锁文件建不出：明确失败，不抛异常逃逸"
        assert outcome.error_kind == "env"
        assert outcome.reason

    def test_source_seq_and_hash_in_meta_match_body(self, tmp_path) -> None:
        session = _session(tmp_path)
        apply_task_definition(session, task_text="T")
        outcome = _write(tmp_path, session)
        meta = _meta(tmp_path, session.session_id)
        assert meta["source_event_seq"] == outcome.source_event_seq
        body = _body(tmp_path, session.session_id)
        normalized = re.sub(r"(?m)^- generated_at: .*$", "- generated_at: -", body)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        assert meta["content_sha256"] == digest

    def test_deleted_body_is_rebuilt_not_fake_skipped(self, tmp_path) -> None:
        """正文被外部删除（meta 还在）⇒ 重建，不假跳过（审查 P2-1 回归钉）。"""
        session = _session(tmp_path)
        apply_task_definition(session, task_text="T")
        assert _write(tmp_path, session).ok
        paths = progress_paths(tmp_path, session.session_id)
        paths.markdown.unlink()
        again = _write(tmp_path, session)
        assert again.ok and not again.skipped, "缺文件不得说成最新"
        assert "session_id:" in paths.markdown.read_text(encoding="utf-8")

    def test_meta_write_failure_reports_and_self_heals(self, tmp_path, monkeypatch) -> None:
        """meta 落盘失败 = 明确失败；下一次写自愈（审查 P3-6 补覆盖）。"""
        session = _session(tmp_path)
        apply_task_definition(session, task_text="T")

        import agent_harness.session.progress as pm

        real = pm._write_meta_atomic

        def boom(meta_path, payload):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(pm, "_write_meta_atomic", boom)
        outcome = _write(tmp_path, session)
        assert not outcome.ok and outcome.error_kind == "env"
        assert "meta" in outcome.reason
        monkeypatch.setattr(pm, "_write_meta_atomic", real)
        second = _write(tmp_path, session)
        assert second.ok and not second.skipped, "meta 失败下次写自愈"


# ── kill / 真实文件系统 / Git 可见面 ────────────────────────────────────────


_KILL_CHILD = r"""
import sys, time
from pathlib import Path
from agent_harness.session import JsonlSessionStore, Session
from agent_harness.session.event import USER_MESSAGE
from agent_harness.session.task import apply_task_definition
from agent_harness.session.progress import write_progress_file

root = Path(sys.argv[1])
session = Session.start(JsonlSessionStore(root=root / ".store"), cwd=str(root))
apply_task_definition(session, task_text="初始目标")
i = 0
while True:
    session.append(USER_MESSAGE, {"content": f"推进第 {i} 步——内容行" * 3})
    write_progress_file(root, session.session_id, session.events, now=f"2026-10-04T00:00:{i % 60:02d}+00:00")
    i += 1
    time.sleep(0.005)
"""


def _read_text_settling(path: Path, *, attempts: int = 50, delay: float = 0.02) -> str:
    """kill 后读取带界重试：只对 ``PermissionError`` 重试，内容判定不变。

    #919 全量门禁表外红（不在 flake 表签名内 ⇒ 按阻塞归因）：``proc.kill()``
    与 ``wait()`` 之后，对刚写完的 progress.md 读到过一次 ``[Errno 13]``。
    这是「暂时打不开」，不是内容损坏——写侧是同目录临时文件 + 原子替换，文件
    只可能是完整旧版或完整新版——所以按有界重试处理：只吃掉共享类拒绝，内容
    断言原样保留（截断/半文件仍由断言报错，牙齿探针实测）。界 = 50 × 20ms
    ≈ 1 秒；真锁死照样失败。
    """

    for attempt in range(attempts):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


@pytest.mark.skipif(sys.platform != "win32", reason="票面要求 Windows 真文件系统语义")
class TestKillMidWriteWindows:
    def test_kill_mid_write_leaves_complete_old_or_new_version(self, tmp_path) -> None:
        src = Path(__file__).resolve().parents[2] / "src"
        for round_no in range(3):
            root = tmp_path / f"killround-{round_no}"
            root.mkdir()
            env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(src))
            proc = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-c", _KILL_CHILD, str(root)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            target_dir = root / "agent-progress"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if list(target_dir.glob("*/progress.md")) or proc.poll() is not None:
                    break
                time.sleep(0.05)
            time.sleep(0.08)  # 再转若干轮写入，kill 落在写周期内而非启动期
            proc.kill()
            proc.wait(timeout=10)
            md = list(target_dir.glob("*/progress.md"))
            assert md, "至少已完成一次完整写入"
            body = _read_text_settling(md[0])
            assert body.rstrip().endswith(END_MARKER), "kill 后文件完整（旧版或新版）"
            meta_path = md[0].parent / "progress.meta.json"
            meta = json.loads(_read_text_settling(meta_path))
            assert meta["source_event_seq"] >= 1


class TestGitVisibility:
    @pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
    def test_file_visible_in_status_index_untouched(self, tmp_path) -> None:
        # 剥掉继承的 GIT_*：hook 注入的 GIT_DIR 会把 tmp 仓库操作劫持到外层仓库（#668）。
        git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        for args in (
            ["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
        ):
            subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                           capture_output=True, env=git_env)
        (tmp_path / "seed.txt").write_text("seed", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True,
                       capture_output=True, env=git_env)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "seed"], check=True,
                       capture_output=True, env=git_env)
        session = _session(tmp_path / ".store")
        apply_task_definition(session, task_text="T")
        outcome = write_progress_file(
            tmp_path, session.session_id, session.events, now="2026-10-04T12:00:00+00:00"
        )
        assert outcome.ok
        status = subprocess.run(
            ["git", "-C", str(tmp_path), "status", "--porcelain"],
            check=True, capture_output=True, text=True, encoding="utf-8", env=git_env,
        ).stdout
        assert any(line.startswith("??") and "agent-progress" in line
                   for line in status.splitlines()), "文件在 git status 可见（未跟踪）"
        staged = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
            check=True, capture_output=True, text=True, encoding="utf-8", env=git_env,
        ).stdout
        assert staged.strip() == "", "index 未被系统修改（不 git add）"

    def test_module_never_invokes_git(self) -> None:
        import agent_harness.session.progress as progress_module

        source = Path(progress_module.__file__).read_text(encoding="utf-8")
        assert "subprocess" not in source, "writer 不碰进程：git diff 可见性是天然事实"

# ── 服务层接线（S2）：task 命令成功 / run 终态 → best-effort 刷新 ───────────


def _svc_state(tmp_path):
    """与 test_task_delivery.py::_svc_state 同构的隔离 AppState 替身。"""
    from unittest.mock import AsyncMock, MagicMock

    from agent_harness.config import Settings
    from tests.session.ledger_doubles import idle_operation_ledger

    settings = Settings(
        _env_file=None,
        workspace_dir=str(tmp_path),
        model_api_key="sk-test",
    )
    state = MagicMock()
    state.settings = settings
    state.store = JsonlSessionStore(root=tmp_path / "sessions")
    state.workspaces_root = tmp_path
    state.workspace_registry = None
    state.workspace_index = None
    state.run_manager = MagicMock()
    state.run_manager.get_active = MagicMock(return_value=None)
    state.get_wiring = AsyncMock(return_value=(MagicMock(), MagicMock()))
    state.operation_ledger = idle_operation_ledger()
    state.ensure_stores = AsyncMock()
    state.stores = MagicMock()
    state.stores.delegation_tree_ledger.get_session_budget = AsyncMock(return_value=None)
    return state


class TestServiceWiring:
    def _seed(self, tmp_path) -> None:
        state = _svc_state(tmp_path)
        session = Session.start(state.store, session_id="sid", cwd=str(tmp_path))
        assert apply_task_definition(
            session, task_text="接线目标", criteria=[{"text": "A"}]
        ).ok
        return state, session

    def test_acceptance_command_refreshes_progress_file(self, tmp_path) -> None:
        state, _session = self._seed(tmp_path)

        async def run():
            from agent_harness.web.app import session_service

            service = session_service(state)
            return await service.task_acceptance(
                session_id="sid", decision="accepted", expected_version=0
            )

        outcome = asyncio.run(run())
        assert outcome.ok
        body = _body(tmp_path, "sid")
        assert "接线目标" in body
        assert "接受裁决：accepted" in body, "task 命令成功 → 进度文件同步刷新"

    def test_rejected_command_does_not_touch_file(self, tmp_path) -> None:
        state, session = self._seed(tmp_path)
        session.append(TASK_ACCEPTED, {"decision": "accepted", "reason": None})
        from agent_harness.web.app import session_service

        async def run():
            return await session_service(state).task_acceptance(
                session_id="sid", decision="accepted", expected_version=1
            )

        rejected = asyncio.run(run())
        assert not rejected.ok and rejected.error_kind == "conflict"
        assert not (tmp_path / "agent-progress" / "sid" / "progress.md").exists(), \
            "拒绝零事件：不写文件"

    def test_refresh_without_cwd_anchor_is_silent_skip(self, tmp_path) -> None:
        state = _svc_state(tmp_path)
        Session.start(state.store, session_id="sid")  # 无 cwd 锚

        async def run():
            from agent_harness.web.app import session_service

            await session_service(state).refresh_progress_file("sid")

        asyncio.run(run())  # 不抛即过：无处可写是合法跳过
        assert not (tmp_path / "agent-progress").exists()

    def test_on_run_terminal_refreshes_progress_file(self, tmp_path) -> None:
        state, session = self._seed(tmp_path)
        session.append(RUN_STARTED, {}, run_id="run-1")
        session.append(RUN_COMPLETED, {"summary": "收口"}, run_id="run-1")

        async def run():
            from agent_harness.web.app import session_service

            await session_service(state).on_run_terminal("sid")

        asyncio.run(run())
        body = _body(tmp_path, "sid")
        assert "Run 完成 run_id=run-1" in body, "run 终态边界刷新（里程碑）"

    def test_refresh_failure_is_swallowed_and_logged(self, tmp_path, monkeypatch, caplog) -> None:
        state, _session = self._seed(tmp_path)

        async def run():
            from agent_harness.web.app import session_service

            await session_service(state).refresh_progress_file("sid")

        def boom(*args, **kwargs):
            raise OSError(28, "No space left on device")

        # 服务持有自己的 from-import 引用——打点必须落在 service 模块的绑定名上
        monkeypatch.setattr("agent_harness.session.service.write_progress_file", boom)
        asyncio.run(run())  # 不上抛——主流程不被文件失败污染
        assert any("进度文件刷新失败" in r.message for r in caplog.records)
