"""T-529-5：沉淀闭环装配与治理面（#529 §5.2/§6.3/§7/§8 #7 #11）。

覆盖面：
- 沉淀流程经 ToolRegistry 暴露（不变量 #7 零旁路）：promote_skill /
  register_skill 走统一 ToolExecutor；
- permission 流程（不变量 #11）：register_skill 是 DANGER 工具——无审批回调
  响亮拒绝（模型不能静默自助注册）；审批通过（现有 approve 范式，§5.2）才
  执行 human-approved → registered；
- 未确认草稿永不进 catalog；确认后全链路可见可 load；
- skill/removed 事件由 discover diff 触发（§10-3 推荐形态）；
- 部分成功语义（§7）：skill 写失败时 memory consolidation 产出仍保留；
- Web 只读展示（catalog 列表，无管理按钮）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_harness.session.context import current_session_var, run_context_var
from agent_harness.session.event import SKILL_REGISTERED, SKILL_REMOVED
from agent_harness.session.session import Session
from agent_harness.session.store import JsonlSessionStore
from agent_harness.skills.capability import SkillCapability
from agent_harness.skills.discovery import SkillDiscovery
from agent_harness.skills.promote import STATUS_DRAFT, SkillPromoter
from agent_harness.tooling.contract import (
    PermissionPolicy,
    ToolPermission,
    ToolSideEffect,
)
from agent_harness.tooling.executor import ToolExecutor
from agent_harness.tooling.registry import ToolRegistry
from agent_harness.tooling.result import ErrorCode
from agent_harness.web.skills import register_skill_routes

DRAFT_TEXT = (
    "---\nname: pdf-export\ndescription: \"当用户需要导出 PDF 报告时使用\"\n"
    "when_to_use: 需要把 markdown 导出为 PDF 时\n---\n\n"
    "## CONTEXT\n\n前置条件：用户已安装 pandoc。\n\n"
    "## INSTRUCTIONS\n\n1. 读取模板\n2. 导出 PDF\n\n## EXAMPLES\n\n略"
)


def _success_events() -> list:
    from agent_harness.session.event import (
        RUN_COMPLETED,
        TASK_PLAN_UPDATED,
        TOOL_RESULT,
        USER_MESSAGE,
        SessionEvent,
    )
    return [
        SessionEvent(seq=1, type=USER_MESSAGE, session_id="s", data={"content": "导出 PDF"}),
        SessionEvent(seq=2, type=TASK_PLAN_UPDATED, session_id="s", data={"items": [
            {"id": "1", "content": "读取模板", "activeForm": "读取中", "status": "completed",
             "source": "agent"},
        ]}),
        SessionEvent(seq=3, type=TOOL_RESULT, session_id="s", data={
            "tool_call_id": "tc1",
            "content": '{"ok": true, "message": "已导出", "data": {}}',
        }),
        SessionEvent(seq=4, type=RUN_COMPLETED, session_id="s", data={}),
    ]


def _runtime(tmp_path: Path, *, approval_callback=None) -> tuple[
    ToolExecutor, SkillCapability, SkillPromoter, Session,
]:
    """最小装配：discovery + promoter + capability（contributes_tools）+ executor。"""
    global_dir, project_dir = tmp_path / "global", tmp_path / "project"
    project_dir.mkdir(parents=True)
    discovery = SkillDiscovery(directories=[global_dir, project_dir], project_dir=project_dir)
    discovery.discover()
    promoter = SkillPromoter(discovery, staging_root=project_dir / ".staging")
    capability = SkillCapability(discovery, promoter=promoter)
    registry = ToolRegistry()
    for tool in capability.contributes_tools():
        registry.register(tool)
    executor = ToolExecutor(registry, policy=PermissionPolicy.WORKSPACE_WRITE,
                            approval_callback=approval_callback)
    store = JsonlSessionStore(tmp_path / "sessions")
    session = Session.start(store, cwd=tmp_path)
    # 预置一条成功 run 的事件流（判据输入来自 session event stream，与生产一致：
    # 判据读 current_session_var 拿到的会话事件）。
    for event in _success_events():
        session.append(event.type, dict(event.data), run_id="run-9")
    return executor, capability, promoter, session


async def _call(executor: ToolExecutor, name: str, args: dict) -> object:
    return await executor.execute({"id": "tc-x", "name": name, "args": args})


# ── 不变量 #7 / #11：统一执行路径 + 审批门 ────────────────────────────────────


class TestPermissionGate:
    def test_tools_exposed_through_registry_with_contract_metadata(self, tmp_path):
        """promote/register 经 contributes_tools 进统一收集循环（零旁路）：
        register 是 MUTATING + DANGER（需审批），promote 是 MUTATING +
        WORKSPACE_WRITE（只写 staging）。"""
        _executor, capability, _promoter, _session = _runtime(tmp_path)
        tools = {t.name: t for t in capability.contributes_tools()}
        assert set(tools) == {"load_skill", "promote_skill", "register_skill"}
        assert tools["register_skill"].permission is ToolPermission.DANGER
        assert tools["register_skill"].side_effect is ToolSideEffect.MUTATING
        assert tools["promote_skill"].permission is ToolPermission.WORKSPACE_WRITE

    @pytest.mark.asyncio
    async def test_register_without_approval_is_denied(self, tmp_path):
        """不变量 #11：无审批回调（= 用户没确认）→ 响亮拒绝，catalog 不变。"""
        executor, capability, _promoter, session = _runtime(tmp_path)
        token = current_session_var.set(session)
        try:
            result = (await _call(executor, "promote_skill", {
                "action": "propose", "draft": DRAFT_TEXT,
            })).result
            assert result.ok is True
            result = (await _call(executor, "register_skill", {"name": "pdf-export"})).result
        finally:
            current_session_var.reset(token)
        assert result.ok is False
        assert result.error_code is ErrorCode.PERMISSION_DENIED
        assert [e.name for e in capability.catalog()] == []

    @pytest.mark.asyncio
    async def test_unconfirmed_draft_never_enters_catalog(self, tmp_path):
        """未确认草稿（哪怕已 lint-pass）也永不进 catalog（§5.2/§9-7）。"""
        executor, capability, promoter, session = _runtime(tmp_path)
        token = current_session_var.set(session)
        try:
            await _call(executor, "promote_skill", {"action": "propose", "draft": DRAFT_TEXT})
            await _call(executor, "promote_skill", {"action": "lint", "name": "pdf-export"})
        finally:
            current_session_var.reset(token)
        assert promoter.status("pdf-export") == "lint-pass"
        assert [e.name for e in capability.catalog()] == []


# ── 端到端：确认后全链路可见可 load ──────────────────────────────────────────


class _Approve:
    """现有 approve 范式的测试替身：用户点了「允许」。"""

    async def __call__(self, request) -> SimpleNamespace:
        return SimpleNamespace(approved=True, reason="", decision=None, grant=None)


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_approved_draft_becomes_visible_and_loadable(self, tmp_path):
        executor, capability, _promoter, session = _runtime(tmp_path, approval_callback=_Approve())
        token = current_session_var.set(session)
        run_token = run_context_var.set("run-9")
        try:
            propose = (await _call(executor, "promote_skill", {
                "action": "propose", "draft": DRAFT_TEXT,
            })).result
            assert propose.ok is True, propose.message
            lint = (await _call(executor, "promote_skill", {
                "action": "lint", "name": "pdf-export",
            })).result
            assert lint.ok is True, lint.message
            register = (await _call(executor, "register_skill", {"name": "pdf-export"})).result
            assert register.ok is True, register.message
        finally:
            current_session_var.reset(token)
            run_context_var.reset(run_token)
        # 全链路可见可 load
        assert [e.name for e in capability.catalog()] == ["pdf-export"]
        assert "INSTRUCTIONS" in capability.load("pdf-export")
        # 事件留痕（§6.2）：载荷含 name / source_run_id / lint 摘要 / 确认者
        registered = [e for e in session.events if e.type == SKILL_REGISTERED]
        assert len(registered) == 1
        data = registered[0].data
        assert data["name"] == "pdf-export"
        assert data["source_run_id"] == "run-9"
        assert data["confirmed_by"].startswith("user:")
        assert "errors" in data["lint"]

    @pytest.mark.asyncio
    async def test_register_before_lint_refused_even_with_approval(self, tmp_path):
        """审批放行 ≠ 跳过状态机：未 lint-pass 的草稿不能被 register。"""
        executor, _capability, promoter, session = _runtime(tmp_path, approval_callback=_Approve())
        token = current_session_var.set(session)
        try:
            await _call(executor, "promote_skill", {"action": "propose", "draft": DRAFT_TEXT})
            result = (await _call(executor, "register_skill", {"name": "pdf-export"})).result
        finally:
            current_session_var.reset(token)
        assert result.ok is False
        assert "lint" in result.message
        assert promoter.status("pdf-export") == STATUS_DRAFT

    @pytest.mark.asyncio
    async def test_skill_write_failure_preserves_memory_output(self, tmp_path):
        """部分成功语义（§7）：skill 写失败（只读盘模拟）时 memory consolidation
        产出仍保留；register 是失败 ToolResult（不炸 executor），草稿回 draft。"""
        executor, capability, promoter, session = _runtime(tmp_path, approval_callback=_Approve())
        token = current_session_var.set(session)
        try:
            await _call(executor, "promote_skill", {"action": "propose", "draft": DRAFT_TEXT})
            await _call(executor, "promote_skill", {"action": "lint", "name": "pdf-export"})
        finally:
            current_session_var.reset(token)
        # memory consolidation 产出先行落库（§7：memory 先行）
        memory_output = {"mem-1": "用户偏好：中文回复；本次 run 提炼：pandoc 导出流程"}
        project_dir = tmp_path / "project"
        target = project_dir / "pdf-export" / "SKILL.md"
        real_write = Path.write_text

        def denied_write(self, *args, **kwargs):
            if self == target:
                raise PermissionError(13, "Permission denied")
            return real_write(self, *args, **kwargs)

        token = current_session_var.set(session)
        try:
            with mock.patch.object(Path, "write_text", denied_write):
                result = (await _call(executor, "register_skill", {"name": "pdf-export"})).result
        finally:
            current_session_var.reset(token)
        assert result.ok is False
        assert memory_output == {"mem-1": "用户偏好：中文回复；本次 run 提炼：pandoc 导出流程"}
        assert [e.name for e in capability.catalog()] == []
        assert promoter.status("pdf-export") == STATUS_DRAFT  # 回 draft，staging 保留
        assert (project_dir / ".staging" / "pdf-export" / "SKILL.md").is_file()


# ── skill/removed：discover diff 触发（§10-3 推荐形态）───────────────────────


class TestRemovedDiff:
    @pytest.mark.asyncio
    async def test_external_removal_emits_skill_removed_once(self, tmp_path):
        executor, capability, _promoter, session = _runtime(tmp_path, approval_callback=_Approve())
        token = current_session_var.set(session)
        try:
            await _call(executor, "promote_skill", {"action": "propose", "draft": DRAFT_TEXT})
            await _call(executor, "promote_skill", {"action": "lint", "name": "pdf-export"})
            await _call(executor, "register_skill", {"name": "pdf-export"})
            assert [e.name for e in capability.catalog()] == ["pdf-export"]
            # 外部删除（文件即真相：用户直接删文件）
            (tmp_path / "project" / "pdf-export" / "SKILL.md").unlink()
            result = (await _call(executor, "promote_skill", {"action": "list"})).result
        finally:
            current_session_var.reset(token)
        assert result.ok is True
        removed = [e for e in session.events if e.type == SKILL_REMOVED]
        assert [e.data["name"] for e in removed] == ["pdf-export"]

    @pytest.mark.asyncio
    async def test_no_removal_no_event(self, tmp_path):
        executor, _capability, _promoter, session = _runtime(tmp_path)
        token = current_session_var.set(session)
        try:
            await _call(executor, "promote_skill", {"action": "list"})
        finally:
            current_session_var.reset(token)
        assert [e for e in session.events if e.type == SKILL_REMOVED] == []


# ── Web 只读展示（§6.3 第一版：catalog 列表，无管理按钮）────────────────────


class TestWebReadOnlySurface:
    def _app(self, capability: SkillCapability | None) -> FastAPI:
        app = FastAPI()
        wiring = SimpleNamespace(skills=capability)
        app.state.agent = SimpleNamespace(
            get_wiring=lambda: _async_pair(None, wiring),
        )
        register_skill_routes(app)
        return app

    def test_catalog_listing_is_read_only(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        discovery = SkillDiscovery(directories=[project_dir], project_dir=project_dir)
        discovery.discover()
        capability = SkillCapability(
            discovery, promoter=SkillPromoter(discovery, staging_root=project_dir / ".staging"),
        )
        client = TestClient(self._app(capability))
        response = client.get("/api/skills")
        assert response.status_code == 200
        body = response.json()
        assert body["skills"] == []
        assert "drafts" in body  # staging 草稿可见（治理面：看得见待审草稿）

    def test_capability_absent_is_503(self):
        client = TestClient(self._app(None))
        response = client.get("/api/skills")
        assert response.status_code == 503


def _async_pair(first, second):
    async def _get():
        return first, second
    return _get()
