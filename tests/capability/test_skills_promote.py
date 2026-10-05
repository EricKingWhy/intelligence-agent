"""T-529-3：Skill 沉淀（#529 §3 判据 + §4 草稿模板 + §4 状态机）。

覆盖面：
- 沉淀判据五条（§3，函数化可测试）：目标达成 / 证据充分 / 可复现（前置条件
  声明）/ 去重 / 负面清单（一票否决，含凭证/隐私/单次偶发）；
- 草稿生成 prompt 模板（§4）：输入 = extractor 裁剪的 event stream +
  consolidation 产出 + 去重扫描；输出 = 标准 SKILL.md 形状；
- 状态机 draft → lint-pass → human-approved → registered：草稿落盘
  <workspace>/skills/.staging/<name>/SKILL.md 不进 catalog；任一步失败回
  draft 或丢弃；未确认草稿永不进 catalog；staging 残留可清理。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_harness.session.event import (
    RUN_COMPLETED,
    RUN_FAILED,
    TASK_ACCEPTED,
    TASK_PLAN_UPDATED,
    TOOL_RESULT,
    USER_MESSAGE,
    SessionEvent,
)
from agent_harness.skills.discovery import SkillDiscovery, parse_skill_markdown
from agent_harness.skills.promote import (
    STATUS_DRAFT,
    STATUS_HUMAN_APPROVED,
    STATUS_LINT_PASS,
    STATUS_REGISTERED,
    SkillPromoter,
    build_draft_prompt,
    evaluate_criteria,
)

# ── 测试脚手架 ────────────────────────────────────────────────────────────────

GOOD_BODY = (
    "## CONTEXT\n\n前置条件：用户已安装 pandoc。\n\n"
    "## INSTRUCTIONS\n\n1. 读取模板\n2. 导出 PDF\n\n## EXAMPLES\n\n略"
)

DRAFT_TEXT = (
    "---\nname: pdf-export\ndescription: \"当用户需要导出 PDF 报告时使用\"\n"
    "when_to_use: 需要把 markdown 导出为 PDF 时\n---\n\n" + GOOD_BODY
)


def _run_events(
    *,
    completed: bool = True,
    tool_ok: bool = True,
    plan_done: bool = True,
) -> list[SessionEvent]:
    """构造一条"成功 run"形状的 event stream（判据输入的最小形状）。"""
    events: list[SessionEvent] = [SessionEvent(seq=1, type=USER_MESSAGE, session_id="s",
                                               data={"content": "导出 PDF"})]
    if plan_done:
        events.append(SessionEvent(
            seq=2, type=TASK_PLAN_UPDATED, session_id="s",
            data={"items": [
                {"id": "1", "content": "读取模板", "activeForm": "读取中",
                 "status": "completed", "source": "agent"},
                {"id": "2", "content": "导出 PDF", "activeForm": "导出中",
                 "status": "completed", "source": "agent"},
            ]},
        ))
    if completed:
        events.append(SessionEvent(seq=3, type=TOOL_RESULT, session_id="s", data={
            "tool_call_id": "tc1",
            "content": '{"ok": true, "message": "已导出", "data": {}}',
        }))
        events.append(SessionEvent(seq=4, type=RUN_COMPLETED, session_id="s",
                                   data={"final_text": "done"}))
    else:
        events.append(SessionEvent(seq=5, type=RUN_FAILED, session_id="s",
                                   data={"error": "boom"}))
    return events


def _promoter(tmp_path: Path) -> tuple[SkillPromoter, SkillDiscovery, Path]:
    global_dir, project_dir = tmp_path / "global", tmp_path / "project"
    project_dir.mkdir(parents=True)
    discovery = SkillDiscovery(directories=[global_dir, project_dir], project_dir=project_dir)
    discovery.discover()
    return SkillPromoter(discovery, staging_root=project_dir / ".staging"), discovery, project_dir


# ── 判据（§3）────────────────────────────────────────────────────────────────


class TestCriteria:
    def test_idle_run_no_tool_calls_not_promotable(self):
        """空转 run（无 tool 调用）：run/completed 也不触发提议（§9-1）。"""
        events = [SessionEvent(seq=1, type=USER_MESSAGE, session_id="s", data={"content": "hi"}),
                  SessionEvent(seq=2, type=RUN_COMPLETED, session_id="s", data={})]
        verdict = evaluate_criteria(events, DRAFT_TEXT, name="x", description="d")
        assert verdict.ok is False
        assert any("证据" in r for r in verdict.failed)

    def test_failed_run_not_promotable(self):
        """失败 run：不触发提议（§9-1）。"""
        verdict = evaluate_criteria(_run_events(completed=False), DRAFT_TEXT,
                                    name="x", description="d")
        assert verdict.ok is False
        assert any("目标" in r or "失败" in r for r in verdict.failed)

    def test_goal_requires_plan_or_acceptance(self):
        """run/completed + tool 证据但 plan 未完成、无用户接受 → 目标未达成（§3-1）。"""
        events = _run_events(plan_done=False)
        assert not any(e.type == TASK_PLAN_UPDATED for e in events)
        verdict = evaluate_criteria(events, DRAFT_TEXT, name="x", description="d")
        assert verdict.ok is False
        assert any("目标" in r for r in verdict.failed)

    def test_user_acceptance_satisfies_goal(self):
        """用户明确确认成功（task/accepted）替代 plan 完成判据（§3-1）。"""
        events = [e for e in _run_events(plan_done=False) if e.type != RUN_COMPLETED]
        seq = max(e.seq for e in events)
        events.append(SessionEvent(seq=seq + 1, type=RUN_COMPLETED, session_id="s", data={}))
        events.append(SessionEvent(seq=seq + 2, type=TASK_ACCEPTED, session_id="s",
                                   data={"expected_version": 1}))
        verdict = evaluate_criteria(events, DRAFT_TEXT, name="x", description="d")
        assert verdict.ok is True

    def test_successful_run_with_plan_passes_criteria(self):
        verdict = evaluate_criteria(_run_events(), DRAFT_TEXT, name="x", description="d")
        assert verdict.ok is True, verdict.failed

    def test_missing_preconditions_block(self):
        """可复现性（§3-3）：正文未标注前置条件 → 不沉淀（外部偶然因素防线）。"""
        body = "## CONTEXT\n\n无。\n\n## INSTRUCTIONS\n\n做"
        verdict = evaluate_criteria(_run_events(), f"---\nname: x\ndescription: \"触发：d\"\n---\n\n{body}",
                                    name="x", description="d")
        assert verdict.ok is False
        assert any("前置条件" in r for r in verdict.failed)

    def test_duplicate_name_refused_with_update_hint(self):
        """去重（§3-4）：name 与既有重复 → 拒绝并提示更新分支。"""
        verdict = evaluate_criteria(_run_events(), DRAFT_TEXT, name="pdf-export",
                                    description="d",
                                    existing=[("pdf-export", "别的描述")])
        assert verdict.ok is False
        assert any("更新" in r or "update" in r for r in verdict.failed)

    def test_duplicate_description_refused(self):
        """去重（§3-4）：description 高度相似（规范化相等）→ 拒绝。"""
        verdict = evaluate_criteria(_run_events(), DRAFT_TEXT, name="fresh",
                                    description="当用户需要导出 PDF 报告时使用",
                                    existing=[("other", "当用户需要导出 PDF 报告时使用")])
        assert verdict.ok is False
        assert any("重复" in r or "dedup" in r.lower() or "更新" in r for r in verdict.failed)

    def test_credential_in_draft_is_vetoed(self):
        """负面清单（§3-5）：草稿含凭证/密钥 → 一票否决。"""
        text = DRAFT_TEXT.replace("2. 导出 PDF", '2. 设置 API_KEY="sk-live-abcdef123456"')
        verdict = evaluate_criteria(_run_events(), text, name="x", description="d")
        assert verdict.ok is False
        assert any("凭证" in r or "credential" in r.lower() for r in verdict.failed)

    def test_private_key_block_is_vetoed(self):
        text = DRAFT_TEXT.replace("## EXAMPLES\n\n略",
                                  "## EXAMPLES\n\n-----BEGIN RSA PRIVATE KEY-----")
        verdict = evaluate_criteria(_run_events(), text, name="x", description="d")
        assert verdict.ok is False
        assert any("隐私" in r or "凭证" in r for r in verdict.failed)

    def test_one_off_success_is_vetoed(self):
        """负面清单（§3-5）：单次偶发成功（one_off 标记）不沉淀。"""
        verdict = evaluate_criteria(_run_events(), DRAFT_TEXT, name="x", description="d",
                                    one_off=True)
        assert verdict.ok is False
        assert any("偶发" in r or "单次" in r for r in verdict.failed)

    def test_veto_listed_in_propose_refusal(self, tmp_path):
        """一票否决在 propose 入口生效：拒绝且不留 staging 残留。"""
        promoter, _discovery, project_dir = _promoter(tmp_path)
        text = DRAFT_TEXT.replace("2. 导出 PDF", '2. 设置 password="hunter2"')
        outcome = promoter.propose(text, events=_run_events())
        assert outcome.accepted is False
        assert not (project_dir / ".staging").exists() or not list((project_dir / ".staging").rglob("*"))


# ── prompt 模板（§4）─────────────────────────────────────────────────────────


class TestDraftPromptTemplate:
    def test_prompt_carries_clipped_events_consolidation_and_dedup(self):
        """输入 = 裁剪后 event stream + consolidation 产出 + 去重扫描（§4）。"""
        events = _run_events()
        events[1].data["content"] = "x" * 5000  # 超长工具输出必被裁剪
        prompt = build_draft_prompt(
            events, consolidation="用户偏好：中文回复", existing=[("old-skill", "旧技能")],
        )
        assert "old-skill" in prompt  # 去重扫描结果在输入里
        assert "用户偏好：中文回复" in prompt  # consolidation 产出在输入里
        assert "…" in prompt and "[truncated]" in prompt  # 事件经 extractor 裁剪有界
        assert "5000" + "x" not in prompt  # 超长内容没有整段进 prompt

    def test_prompt_prescribes_standard_skill_md_shape(self):
        """输出 = 标准 SKILL.md：frontmatter 必备 name/description，推荐
        when_to_use/allowed-tools；正文 CONTEXT → INSTRUCTIONS → EXAMPLES（§4）。"""
        prompt = build_draft_prompt(_run_events(), consolidation="", existing=[])
        for token in ("name", "description", "when_to_use", "allowed-tools",
                      "CONTEXT", "INSTRUCTIONS", "EXAMPLES", "SKILL.md"):
            assert token in prompt


# ── 状态机（§4）──────────────────────────────────────────────────────────────


class TestStateMachine:
    def test_propose_writes_staging_not_catalog(self, tmp_path):
        """草稿落盘 .staging/<name>/SKILL.md，不进 catalog（§4 草稿态）。"""
        promoter, discovery, project_dir = _promoter(tmp_path)
        outcome = promoter.propose(DRAFT_TEXT, events=_run_events(), source_run_id="run-1")
        assert outcome.accepted is True
        staging_file = project_dir / ".staging" / "pdf-export" / "SKILL.md"
        assert staging_file.is_file()
        assert promoter.status("pdf-export") == STATUS_DRAFT
        assert [e.name for e in discovery.catalog().entries] == []

    def test_staging_dir_never_enter_catalog_even_after_refresh(self, tmp_path):
        """结构防线：.staging 嵌套路径不会被单层扫描误收（未确认草稿永不进 catalog）。"""
        promoter, discovery, _project_dir = _promoter(tmp_path)
        promoter.propose(DRAFT_TEXT, events=_run_events())
        discovery.discover()  # 外部任何刷新都不该把 staging 草稿捞进 catalog
        assert all(e.name != "pdf-export" for e in discovery.catalog().entries)

    def test_lint_pass_transition(self, tmp_path):
        promoter, _d, _p = _promoter(tmp_path)
        promoter.propose(DRAFT_TEXT, events=_run_events())
        result = promoter.run_lint("pdf-export")
        assert result.ok is True
        assert promoter.status("pdf-export") == STATUS_LINT_PASS

    def test_lint_failure_back_to_draft(self, tmp_path):
        """lint 失败 → 回 draft（可改），不升级不注册（§4 状态机）。"""
        promoter, _d, _p = _promoter(tmp_path)
        bad = DRAFT_TEXT.replace('when_to_use: 需要把 markdown 导出为 PDF 时', "")\
                        .replace('description: "当用户需要导出 PDF 报告时使用"',
                                 'description: "导出 PDF 报告"')
        outcome = promoter.propose(bad, events=_run_events())
        assert outcome.accepted is True  # 判据过了（缺触发条件是 lint 的事）
        result = promoter.run_lint("pdf-export")
        assert result.ok is False
        assert promoter.status("pdf-export") == STATUS_DRAFT

    def test_register_requires_lint_pass(self, tmp_path):
        promoter, _d, _p = _promoter(tmp_path)
        promoter.propose(DRAFT_TEXT, events=_run_events())
        with pytest.raises(ValueError, match="lint"):
            promoter.register("pdf-export", confirmed_by="user:wang")

    def test_approve_requires_lint_pass(self, tmp_path):
        """人审门前置：未 lint 的草稿不能进入 human-approved（§5.2）。"""
        promoter, _d, _p = _promoter(tmp_path)
        promoter.propose(DRAFT_TEXT, events=_run_events())
        with pytest.raises(ValueError, match="lint"):
            promoter.approve("pdf-export", confirmed_by="user:wang")
        assert promoter.status("pdf-export") == STATUS_DRAFT

    def test_full_chain_to_registered_and_visible(self, tmp_path):
        """draft → lint-pass → human-approved → registered：注册后 catalog 可见可 load。"""
        promoter, discovery, project_dir = _promoter(tmp_path)
        promoter.propose(DRAFT_TEXT, events=_run_events(), source_run_id="run-1")
        promoter.run_lint("pdf-export")
        promoter.approve("pdf-export", confirmed_by="user:wang")
        assert promoter.status("pdf-export") == STATUS_HUMAN_APPROVED
        entry = promoter.register("pdf-export", confirmed_by="user:wang")
        assert promoter.status("pdf-export") == STATUS_REGISTERED
        # 注册后：project 目录文件即真相、catalog 可见、正文可读
        assert (project_dir / "pdf-export" / "SKILL.md").is_file()
        assert [e.name for e in discovery.catalog().entries] == ["pdf-export"]
        assert "INSTRUCTIONS" in entry.load_body()
        # staging 残留清干净
        assert not (project_dir / ".staging" / "pdf-export").exists()

    def test_register_failure_returns_to_draft_and_keeps_staging(self, tmp_path):
        """register 失败（如只读盘）→ 回 draft，staging 保留可重试（§4：失败回 draft）。"""
        from unittest import mock

        promoter, _d, project_dir = _promoter(tmp_path)
        promoter.propose(DRAFT_TEXT, events=_run_events())
        promoter.run_lint("pdf-export")
        promoter.approve("pdf-export", confirmed_by="user:wang")
        target = project_dir / "pdf-export" / "SKILL.md"
        real_write = Path.write_text

        def denied_write(self, *args, **kwargs):
            if self == target:
                raise PermissionError(13, "Permission denied")
            return real_write(self, *args, **kwargs)

        with mock.patch.object(Path, "write_text", denied_write), pytest.raises(OSError):
            promoter.register("pdf-export", confirmed_by="user:wang")
        assert promoter.status("pdf-export") == STATUS_DRAFT
        assert (project_dir / ".staging" / "pdf-export" / "SKILL.md").is_file()

    def test_discard_removes_staging(self, tmp_path):
        promoter, _d, project_dir = _promoter(tmp_path)
        promoter.propose(DRAFT_TEXT, events=_run_events())
        assert promoter.discard("pdf-export") is True
        assert promoter.status("pdf-export") is None
        assert not (project_dir / ".staging" / "pdf-export").exists()

    def test_cleanup_staging_removes_all_residue(self, tmp_path):
        """staging 残留可清理（§9-7）：一键清空全部未确认草稿。"""
        promoter, _d, project_dir = _promoter(tmp_path)
        promoter.propose(DRAFT_TEXT, events=_run_events())
        other = DRAFT_TEXT.replace("pdf-export", "pdf-export-2")
        promoter.propose(other, events=_run_events())
        removed = promoter.cleanup_staging()
        assert sorted(removed) == ["pdf-export", "pdf-export-2"]
        assert not (project_dir / ".staging").exists() or not list((project_dir / ".staging").rglob("*"))
        assert promoter.status("pdf-export") is None

    def test_proposed_staging_file_lints_clean_after_manual_roundtrip(self, tmp_path):
        """staging 里的草稿经 lint 用的解析器与 catalog 同一真相（依据 F）：落盘内容
        可被 parse_skill_markdown 解析回等价条目。"""
        promoter, _d, project_dir = _promoter(tmp_path)
        promoter.propose(DRAFT_TEXT, events=_run_events())
        entry, errors = parse_skill_markdown(project_dir / ".staging" / "pdf-export" / "SKILL.md")
        assert errors == []
        assert entry.name == "pdf-export"


# ── 更新分支（§3-4/§5.1/§10-5，审查处置）─────────────────────────────────────


class TestUpdateBranch:
    """同名 project skill = 更新分支入口（模型重写全文 + lint + 人审，与新建同流程）；
    global/manual 来源同名仍拒绝（闭环不写 global，不放行 shadow）。"""

    def test_same_name_project_skill_routes_to_update_branch(self, tmp_path):
        """§10-5：同名 project skill 的草稿不被去重判据/lint 规则 5 拒绝，
        全链路走通后覆盖旧文件。"""
        promoter, discovery, _project_dir = _promoter(tmp_path)
        promoter.propose(DRAFT_TEXT, events=_run_events(), source_run_id="run-1")
        promoter.run_lint("pdf-export")
        promoter.approve("pdf-export", confirmed_by="user:wang")
        promoter.register("pdf-export", confirmed_by="user:wang")

        updated = DRAFT_TEXT.replace("1. 读取模板", "1. 读取新模板")
        outcome = promoter.propose(updated, events=_run_events())
        assert outcome.accepted is True  # 不再被判据 4（去重）拒绝
        assert promoter.run_lint("pdf-export").ok is True  # 不再被 lint 规则 5 拒绝
        promoter.approve("pdf-export", confirmed_by="user:wang")
        entry = promoter.register("pdf-export", confirmed_by="user:wang")
        assert "读取新模板" in entry.load_body()
        assert [e.name for e in discovery.catalog().entries] == ["pdf-export"]

    def test_same_name_global_skill_still_refused(self, tmp_path):
        """同名但来源是 global 目录 → 仍拒绝并指引（§6.1：闭环不写 global）。"""
        global_dir, project_dir = tmp_path / "global", tmp_path / "project"
        (global_dir / "pdf-export").mkdir(parents=True)
        (global_dir / "pdf-export" / "SKILL.md").write_text(DRAFT_TEXT, encoding="utf-8")
        project_dir.mkdir()
        discovery = SkillDiscovery(directories=[global_dir, project_dir],
                                   project_dir=project_dir)
        discovery.discover()
        promoter = SkillPromoter(discovery, staging_root=project_dir / ".staging")

        outcome = promoter.propose(DRAFT_TEXT, events=_run_events())
        assert outcome.accepted is False
        assert any("更新" in r for r in outcome.reasons)
        assert promoter.status("pdf-export") is None  # 零落盘
