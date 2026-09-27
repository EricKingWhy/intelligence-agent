"""`#317` T9：stuck 暂停的三类恢复依据（ADR-0048 D7/D8）。

判据来源：`02 §5.3`（恢复前置：相关 steer / 环境或策略变更；**观测到的**，不是声明的）、
`03 §3.4` / `03 §5`（恢复请求的形状与 CAS；`resume_basis` 是声明，证据由服务端现算）。

三层都在这里：
- `agent/resume_evidence.py` 的两个摘要函数（环境 revision / 策略版本）与 steer 判据；
- `run_budget.stuck_resume_evidence`（**唯一**判据：哪条依据成立、缺哪条就 409）；
- `validate_resume` 的 stuck 分支（不要求点名 ceiling、但仍过 headroom）。

"用户说他改了环境"不构成证据这件事没有单独的用例：请求体里**根本没有**能放依据值的
字段（`resume_basis` 只是三条里的哪一条），所以"伪造"在形状上不可达。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_harness.agent.budget import BudgetConflict, BudgetRejection
from agent_harness.agent.resume_evidence import (
    MAX_REVISION_ENTRIES,
    ResumeEvidence,
    StuckEvidencePort,
    digest_policy_inputs,
    environment_revision,
    evidence_port,
    paused_policy_inputs,
    policy_inputs,
    policy_version_of,
    relevant_steer_seq,
)
from agent_harness.agent.run_budget import (
    REASON_BUDGET_EXHAUSTED,
    REASON_STUCK,
    RESUME_BASIS_BUDGET_INCREASE,
    RESUME_BASIS_ENVIRONMENT_CHANGE,
    RESUME_BASIS_POLICY_CHANGE,
    RESUME_BASIS_RELEVANT_STEER,
    BudgetConsumed,
    PausedRun,
    RunLimits,
    stuck_resume_evidence,
    validate_resume,
)
from agent_harness.session.event import (
    MODEL_CHANGED,
    RUN_PAUSED,
    STEER_REQUESTED,
    SessionEvent,
)

RUN_ID = "run-stuck"


def _paused(
    *, reason: str = REASON_STUCK, pause_seq: int = 7,
    stuck: dict | None = None, consumed_turns: int = 4,
) -> PausedRun:
    return PausedRun(
        run_id=RUN_ID, pause_seq=pause_seq, step_id=6, reason=reason,
        trigger_dimension="stuck.tool_failure_loop", version=2,
        consumed=BudgetConsumed(agent_turns=consumed_turns, model_requests=8),
        limits=RunLimits(max_agent_turns_total=20),
        local_fuse=None, continuation={}, closeout_source="deterministic",
        resume_requirements=(), stuck=stuck,
    )


def _stuck_payload(**overrides) -> dict:
    payload = {
        "pattern": "stuck.tool_failure_loop", "threshold": 3, "count": 6,
        "replan_count": 1, "fingerprint": "sha256:abc",
        "environment_revision": "sha256:env-old", "policy_version": "sha256:pol-old",
    }
    payload.update(overrides)
    return payload


def _steer(seq: int, *, run_id: str | None = RUN_ID, content: str = "换个做法") -> SessionEvent:
    return SessionEvent(
        seq=seq, type=STEER_REQUESTED,
        data={"steer_id": f"s{seq}", "content": content}, run_id=run_id,
    )


# ── 摘要函数 ──────────────────────────────────────────────────────────────


class TestEnvironmentRevision:
    """摘要 = 相对路径 + 类型 + 尺寸 + mtime_ns（**不含内容**，见 ADR-0048 残余 2）。

    用例显式给 mtime，不靠挂钟：Windows 的文件时间粒度约 15.6ms，同一 tick 内的两次
    写入会得到同一个 `st_mtime_ns`——那是"看不见变化"（fail-closed：恢复侧报 409），
    不是"误报变化"（那才是要防的方向）。
    """

    def _stamp(self, path: Path, *, mtime_ns: int) -> None:
        os.utime(path, ns=(mtime_ns, mtime_ns))

    def test_the_same_tree_gives_the_same_revision(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("world", encoding="utf-8")
        first = environment_revision(tmp_path)
        assert first is not None
        assert first == environment_revision(tmp_path)
        assert first.startswith("sha256:")

    def test_a_new_file_changes_it(self, tmp_path: Path) -> None:
        first = environment_revision(tmp_path)
        (tmp_path / "new.txt").write_text("x", encoding="utf-8")
        assert environment_revision(tmp_path) != first

    def test_changed_content_changes_it(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("one", encoding="utf-8")
        self._stamp(target, mtime_ns=1_700_000_000_000_000_000)
        first = environment_revision(tmp_path)
        target.write_text("two", encoding="utf-8")
        self._stamp(target, mtime_ns=1_700_000_001_000_000_000)
        assert environment_revision(tmp_path) != first

    def test_removed_content_changes_it(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("one", encoding="utf-8")
        first = environment_revision(tmp_path)
        target.unlink()
        assert environment_revision(tmp_path) != first

    def test_an_unreadable_root_is_not_a_revision(self, tmp_path: Path) -> None:
        """读不到 ⇒ `None`（fail-closed：恢复侧按"无快照可比"拒，而不是当成"变了"）。"""
        assert environment_revision(tmp_path / "missing") is None
        assert environment_revision(tmp_path / "a.txt") is None
        empty = tmp_path / "empty"
        empty.mkdir()
        assert environment_revision(empty) is not None  # 空目录也是**可读的**状态
        assert environment_revision(empty) != environment_revision(tmp_path)

    def test_truncation_is_folded_into_the_digest(self, tmp_path: Path, monkeypatch) -> None:
        """条目超过上限 ⇒ 摘要里折进"已截断"，不是静默截断成"看起来一样"。"""
        for index in range(4):
            (tmp_path / f"f{index}.txt").write_text(str(index), encoding="utf-8")
        full = environment_revision(tmp_path)
        monkeypatch.setattr(
            "agent_harness.agent.resume_evidence.MAX_REVISION_ENTRIES", 2,
        )
        capped = environment_revision(tmp_path)
        assert capped != full, "截断必须改变摘要（否则超大目录的变化会被静默吞掉）"
        assert MAX_REVISION_ENTRIES >= 1


class TestPolicyVersion:
    def test_the_same_inputs_give_the_same_version(self) -> None:
        assert policy_version_of(permission_mode="workspace-write", model="m") == (
            policy_version_of(permission_mode="workspace-write", model="m")
        )

    def test_each_policy_input_moves_it(self) -> None:
        base = policy_version_of(permission_mode="workspace-write", model="m")
        assert policy_version_of(permission_mode="danger-full-access", model="m") != base
        assert policy_version_of(permission_mode="workspace-write", model="other") != base
        assert policy_version_of(
            permission_mode="workspace-write", model="m", agent_profile="p",
        ) != base
        assert policy_version_of(
            permission_mode="workspace-write", model="m", reasoning_effort="high",
        ) != base
        assert policy_version_of(
            permission_mode="workspace-write", model="m", context_providers=["a"],
        ) != base

    def test_provider_order_is_not_a_policy_change(self) -> None:
        assert policy_version_of(context_providers=["a", "b"]) == (
            policy_version_of(context_providers=["b", "a"])
        )

    def test_ceiling_dimensions_are_not_in_the_digest(self) -> None:
        """ceiling 类不入摘要（ADR-0048 D8）：否则抬一格 fuse 就能自己造出依据。

        `policy_version_of` 的签名里没有这些参数——从外部看就是"喂不进摘要"。
        """
        import inspect

        parameters = set(inspect.signature(policy_version_of).parameters)
        assert not parameters & {
            "max_agent_turns", "local_fuse_source", "max_total_tokens",
            "max_cost_usd", "deadline_at", "auto_approve",
        }


class TestRelevantSteer:
    def test_no_steer_after_the_pause_is_no_evidence(self) -> None:
        events = [_steer(3), _steer(5)]
        assert relevant_steer_seq(events, run_id=RUN_ID, pause_seq=7) is None

    def test_a_later_steer_for_this_run_counts(self) -> None:
        events = [_steer(3), _steer(9), _steer(11)]
        assert relevant_steer_seq(events, run_id=RUN_ID, pause_seq=7) == 11

    def test_a_session_level_steer_does_not_count(self) -> None:
        """`run_id=None` 的"会话级" steer **不算**依据（T9 审查 P2 的口径订正）。

        运行时 `_applicable_steers` 把它当陈旧 steer 丢弃（只认 run_id 相同），它只在
        run 走进**终态**时被当普通输入投递——stuck 暂停不是终态，所以那种 steer 这一次
        根本到不了模型：算成依据等于"没有新输入也放行"。两处现在共用
        `steer_applies_to_run`，所以这里与运行时同判。
        """
        from agent_harness.agent.resume_evidence import steer_applies_to_run

        assert relevant_steer_seq(
            [_steer(8, run_id=None)], run_id=RUN_ID, pause_seq=7,
        ) is None
        assert steer_applies_to_run(None, RUN_ID) is False
        assert steer_applies_to_run(RUN_ID, RUN_ID) is True
        assert steer_applies_to_run("run-other", RUN_ID) is False
        # 运行时的 run_id 恒非 None（`_drive` 里由 run/started 给）：两侧同判意味着
        # "谁也别想用一个没主儿的 steer 把恢复放行"。
        assert steer_applies_to_run(None, None) is False

    def test_another_runs_steer_does_not_count(self) -> None:
        assert relevant_steer_seq(
            [_steer(8, run_id="run-other")], run_id=RUN_ID, pause_seq=7,
        ) is None

    def test_the_boundary_is_strictly_after_the_pause(self) -> None:
        assert relevant_steer_seq([_steer(7)], run_id=RUN_ID, pause_seq=7) is None


class TestEvidencePort:
    def test_current_observes_both_values(self, tmp_path: Path) -> None:
        port = StuckEvidencePort(
            workspace=tmp_path, policy_version=policy_version_of(model="m"),
        )
        evidence = port.current()
        assert evidence.environment_revision == environment_revision(tmp_path)
        assert evidence.policy_version == policy_version_of(model="m")

    def test_an_unreadable_workspace_is_not_a_failure(self, tmp_path: Path) -> None:
        """端口故障不得变成异常（ADR-0048 D9）：读不到就是 `None`。"""
        port = StuckEvidencePort(workspace=tmp_path / "gone", policy_version="sha256:x")
        evidence = port.current()
        assert evidence.environment_revision is None
        assert evidence.policy_version == "sha256:x"


class TestPolicyInputsAreRecordedAndRestored:
    """`#317` T9 审查 P1：逐维值与摘要**同源**，恢复侧据此还原暂停时那一套策略。

    病灶：暂停侧记的是"那次执行生效的策略面"，恢复侧却只拿本次请求声明的 amend 重算
    ——两次请求的字段集合不同（CLI 的 resume 一个策略字段都不声明），摘要必不同，于是
    `policy_change` 在"其实什么都没变"的恢复上成立（fail-open）。修法是把逐维值也记进
    快照，恢复侧先还原再算。
    """

    def test_the_recorded_inputs_reproduce_the_recorded_digest(self) -> None:
        """`digest_policy_inputs(policy_inputs(...)) == policy_version_of(...)`。"""
        inputs = policy_inputs(
            permission_mode="danger-full-access", model="m", agent_profile="coding",
            reasoning_effort="deep", context_providers=["web", "memory"],
        )
        assert digest_policy_inputs(inputs) == policy_version_of(
            permission_mode="danger-full-access", model="m", agent_profile="coding",
            reasoning_effort="deep", context_providers=["memory", "web"],
        )
        port = evidence_port(
            workspace=None, permission_mode="workspace-write",
            model="m", agent_profile="coding",
        )
        evidence = port.current()
        assert evidence.policy_inputs is not None
        assert digest_policy_inputs(evidence.policy_inputs) == evidence.policy_version

    def test_the_pause_snapshot_is_read_back_as_values(self) -> None:
        """`paused_policy_inputs` 只认同 run 的 `run/paused`，返回的是**值**不是摘要。"""
        events = [
            SessionEvent(
                seq=9, type=RUN_PAUSED, session_id="s1", run_id=RUN_ID,
                data={
                    "reason": REASON_STUCK,
                    "stuck": _stuck_payload(
                        policy_inputs={"model": "m", "agent_profile": "coding"},
                    ),
                },
            ),
            SessionEvent(
                seq=10, type=RUN_PAUSED, session_id="s1", run_id="run-other",
                data={
                    "reason": REASON_STUCK,
                    "stuck": _stuck_payload(policy_inputs={"agent_profile": "main"}),
                },
            ),
        ]
        assert paused_policy_inputs(events, run_id=RUN_ID) == {
            "permission_mode": None, "model": "m", "agent_profile": "coding",
            "reasoning_effort": None, "context_providers": None,
        }
        assert paused_policy_inputs(events, run_id="run-nope") is None

    def test_a_budget_pause_has_no_policy_inputs_to_read_back(self) -> None:
        """非 stuck 暂停的载荷里没有这一格 ⇒ `None`（恢复侧没有可还原的东西）。"""
        events = [
            SessionEvent(
                seq=4, type=RUN_PAUSED, session_id="s1", run_id=RUN_ID,
                data={"reason": REASON_BUDGET_EXHAUSTED},
            ),
        ]
        assert paused_policy_inputs(events, run_id=RUN_ID) is None

    def test_a_request_that_declares_nothing_keeps_the_paused_policy(self) -> None:
        """空 amend ⇒ 还原成暂停时那一套 ⇒ 摘要相等 ⇒ `policy_change` 不成立。"""
        from agent_harness.session.amend import AmendOptions
        from agent_harness.session.model_switch import restore_policy_inputs

        recorded = {
            "permission_mode": "workspace-write", "model": "m",
            "agent_profile": "coding", "reasoning_effort": "deep",
            "context_providers": [],
        }
        restored = restore_policy_inputs(None, recorded, [])
        assert restored == AmendOptions(
            model="m", agent_profile="coding", reasoning_effort="deep",
            context_providers=[],
        )
        assert policy_version_of(
            permission_mode="workspace-write", model=restored.model,
            agent_profile=restored.agent_profile,
            reasoning_effort=restored.reasoning_effort,
            context_providers=restored.context_providers,
        ) == policy_version_of(
            permission_mode="workspace-write", model="m", agent_profile="coding",
            reasoning_effort="deep", context_providers=[],
        )

    def test_a_declared_input_wins_over_the_snapshot(self) -> None:
        """请求显式声明的维度优先——那才是用户真实的策略变更。"""
        from agent_harness.session.amend import AmendOptions
        from agent_harness.session.model_switch import restore_policy_inputs

        restored = restore_policy_inputs(
            AmendOptions(agent_profile="main"),
            {"model": "m", "agent_profile": "coding", "reasoning_effort": "deep"},
            [],
        )
        assert restored.agent_profile == "main"  # 声明优先
        assert restored.model == "m"  # 没声明的沿用快照
        assert restored.reasoning_effort == "deep"

    def test_a_session_model_switch_wins_over_the_snapshot(self) -> None:
        """暂停之后切过模型 ⇒ 快照里的模型不许盖掉它（那一跳正是合法的策略依据）。"""
        from agent_harness.session.amend import AmendOptions
        from agent_harness.session.model_switch import restore_policy_inputs

        switched = [
            SessionEvent(
                seq=12, type=MODEL_CHANGED, session_id="s1",
                data={"to_provider": "deepseek", "to_model_id": "m-new"},
            ),
        ]
        restored = restore_policy_inputs(
            AmendOptions(), {"model": "m-old", "agent_profile": "coding"}, switched,
        )
        # model 留在 None：让 `amend_with_session_model` 用会话派生的 m-new 补上。
        assert restored.model is None
        assert restored.agent_profile == "coding"


# ── 判据：`stuck_resume_evidence` ────────────────────────────────────────


class TestStuckResumeEvidence:
    def test_a_budget_pause_has_no_evidence_object(self) -> None:
        """预算 / deadline 暂停的恢复没有"依据"这一格：返回 `None`，载荷逐字不变。"""
        assert stuck_resume_evidence(
            _paused(reason=REASON_BUDGET_EXHAUSTED),
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
            evidence=ResumeEvidence(),
        ) is None

    def test_budget_increase_is_rejected_for_a_stuck_pause(self) -> None:
        with pytest.raises(BudgetConflict, match="budget_increase"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload()),
                resume_basis=RESUME_BASIS_BUDGET_INCREASE,
                evidence=ResumeEvidence(environment_revision="sha256:env-new"),
            )

    def test_missing_evidence_is_rejected(self) -> None:
        with pytest.raises(BudgetConflict, match="没有可用的"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload()),
                resume_basis=RESUME_BASIS_RELEVANT_STEER, evidence=None,
            )

    def test_relevant_steer_is_accepted_and_recorded(self) -> None:
        recorded = stuck_resume_evidence(
            _paused(stuck=_stuck_payload(), pause_seq=7),
            resume_basis=RESUME_BASIS_RELEVANT_STEER,
            evidence=ResumeEvidence(relevant_steer_seq=9),
        )
        assert recorded == {
            "basis": RESUME_BASIS_RELEVANT_STEER, "steer_seq": 9, "pause_seq": 7,
        }

    def test_relevant_steer_without_a_steer_is_rejected(self) -> None:
        with pytest.raises(BudgetConflict, match="没有"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload(), pause_seq=7),
                resume_basis=RESUME_BASIS_RELEVANT_STEER,
                evidence=ResumeEvidence(relevant_steer_seq=None),
            )

    def test_an_unchanged_environment_is_rejected(self) -> None:
        with pytest.raises(BudgetConflict, match="相同"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload(environment_revision="sha256:env-old")),
                resume_basis=RESUME_BASIS_ENVIRONMENT_CHANGE,
                evidence=ResumeEvidence(environment_revision="sha256:env-old"),
            )

    def test_a_changed_environment_is_accepted(self) -> None:
        recorded = stuck_resume_evidence(
            _paused(stuck=_stuck_payload(environment_revision="sha256:env-old")),
            resume_basis=RESUME_BASIS_ENVIRONMENT_CHANGE,
            evidence=ResumeEvidence(environment_revision="sha256:env-new"),
        )
        assert recorded == {
            "basis": RESUME_BASIS_ENVIRONMENT_CHANGE,
            "environment_revision": "sha256:env-new", "recorded": "sha256:env-old",
        }

    def test_a_missing_snapshot_is_rejected_not_assumed_changed(self) -> None:
        """暂停时没读到快照 ⇒ 该依据不可用（fail-closed），不是"当作变了"放行。"""
        with pytest.raises(BudgetConflict, match="无快照可比"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload(policy_version=None)),
                resume_basis=RESUME_BASIS_POLICY_CHANGE,
                evidence=ResumeEvidence(policy_version="sha256:pol-new"),
            )

    def test_a_missing_observation_is_rejected(self) -> None:
        with pytest.raises(BudgetConflict, match="缺席"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload()),
                resume_basis=RESUME_BASIS_ENVIRONMENT_CHANGE,
                evidence=ResumeEvidence(environment_revision=None),
            )

    def test_an_unchanged_policy_is_rejected(self) -> None:
        with pytest.raises(BudgetConflict, match="相同"):
            stuck_resume_evidence(
                _paused(stuck=_stuck_payload()),
                resume_basis=RESUME_BASIS_POLICY_CHANGE,
                evidence=ResumeEvidence(policy_version="sha256:pol-old"),
            )

    def test_a_changed_policy_is_accepted(self) -> None:
        recorded = stuck_resume_evidence(
            _paused(stuck=_stuck_payload()),
            resume_basis=RESUME_BASIS_POLICY_CHANGE,
            evidence=ResumeEvidence(policy_version="sha256:pol-new"),
        )
        assert recorded["basis"] == RESUME_BASIS_POLICY_CHANGE
        assert recorded["policy_version"] == "sha256:pol-new"


# ── 判定：`validate_resume` 的 stuck 分支 ────────────────────────────────


class TestValidateResume:
    def test_stuck_resume_does_not_require_an_absolute_ceiling(self) -> None:
        """stuck 缺的不是额度：不点名 ceiling 也合法（沿用暂停快照）。"""
        effective = validate_resume(
            _paused(stuck=_stuck_payload()), run_id=RUN_ID, expected_version=2,
            limits=RunLimits(), resume_basis=RESUME_BASIS_RELEVANT_STEER,
            resume_evidence=ResumeEvidence(relevant_steer_seq=9),
        )
        assert effective.max_agent_turns_total == 20  # 沿用暂停时的值

    def test_stuck_resume_still_checks_headroom(self) -> None:
        """恢复了却立刻再停 ⇒ 409（比一次假恢复诚实）。"""
        with pytest.raises(BudgetConflict, match="放不下"):
            validate_resume(
                _paused(stuck=_stuck_payload(), consumed_turns=20),
                run_id=RUN_ID, expected_version=2, limits=RunLimits(),
                resume_basis=RESUME_BASIS_RELEVANT_STEER,
                resume_evidence=ResumeEvidence(relevant_steer_seq=9),
            )

    def test_a_stuck_pause_rejects_a_budget_increase_basis(self) -> None:
        with pytest.raises(BudgetConflict, match="budget_increase"):
            validate_resume(
                _paused(stuck=_stuck_payload()), run_id=RUN_ID, expected_version=2,
                limits=RunLimits(max_agent_turns_total=99),
                resume_basis=RESUME_BASIS_BUDGET_INCREASE,
                resume_evidence=ResumeEvidence(relevant_steer_seq=9),
            )

    def test_a_budget_pause_still_rejects_the_three_change_bases(self) -> None:
        """预算暂停不假装校验过"变更依据"（那是 stuck 的责任域）。"""
        with pytest.raises(BudgetConflict, match="只接受"):
            validate_resume(
                _paused(reason=REASON_BUDGET_EXHAUSTED), run_id=RUN_ID,
                expected_version=2, limits=RunLimits(max_agent_turns_total=99),
                resume_basis=RESUME_BASIS_RELEVANT_STEER,
                resume_evidence=ResumeEvidence(relevant_steer_seq=9),
            )

    def test_an_unknown_resume_basis_is_a_shape_error(self) -> None:
        with pytest.raises(BudgetRejection, match="未知 resume_basis"):
            validate_resume(
                _paused(stuck=_stuck_payload()), run_id=RUN_ID, expected_version=2,
                limits=RunLimits(), resume_basis="whatever",
            )

    def test_a_stale_version_is_a_conflict(self) -> None:
        with pytest.raises(BudgetConflict, match="过期"):
            validate_resume(
                _paused(stuck=_stuck_payload()), run_id=RUN_ID, expected_version=1,
                limits=RunLimits(), resume_basis=RESUME_BASIS_RELEVANT_STEER,
                resume_evidence=ResumeEvidence(relevant_steer_seq=9),
            )

    def test_a_deadline_pause_still_needs_a_future_instant(self) -> None:
        """deadline 暂停的既有语义没被 stuck 改动（`#315` 回归面）。"""
        from agent_harness.agent.run_budget import REASON_DEADLINE

        paused = _paused(reason=REASON_DEADLINE)
        with pytest.raises(BudgetConflict):
            validate_resume(
                paused, run_id=RUN_ID, expected_version=2,
                limits=RunLimits(deadline_at=datetime.now(UTC) - timedelta(minutes=1)),
                resume_basis=RESUME_BASIS_BUDGET_INCREASE,
            )
        effective = validate_resume(
            paused, run_id=RUN_ID, expected_version=2,
            limits=RunLimits(deadline_at=datetime.now(UTC) + timedelta(minutes=5)),
            resume_basis=RESUME_BASIS_BUDGET_INCREASE,
        )
        assert effective.deadline_at is not None
